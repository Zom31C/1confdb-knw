"""Тесты семантических проверок BSL (bsl_semantic)."""
import sqlite3

from confdb.bsl_analyzer import BslContext
from confdb.bsl_semantic import analyze_module
from confdb.db.writer import write_db
from test_writer import make_dump


def _ctx(tmp_path_factory):
    """BslContext по стандартной тестовой базе (ОбщийМодуль1: Экспортная,
    Закрытая)."""
    dump = str(tmp_path_factory.mktemp('dump'))
    make_dump(dump)
    db = str(tmp_path_factory.mktemp('db') / 't.sqlite')
    write_db(dump, db, source_file='t.cf')
    return BslContext(sqlite3.connect(db))


def codes(diags):
    return [(d.code, d.severity) for d in diags]


# -- структурные (без базы) ---------------------------------------------------

def test_unused_local_var():
    d = analyze_module('Процедура П()\n    Перем Х;\n    А = 1;\nКонецПроцедуры\n')
    assert ('UnusedLocalVariable', 'WARNING') in codes(d)
    assert any('«Х»' in x.message for x in d)


def test_used_local_var_not_flagged():
    d = analyze_module('Процедура П()\n    Перем Х;\n    Х = 1;\nКонецПроцедуры\n')
    assert not any(x.code == 'UnusedLocalVariable' for x in d)


def test_local_var_read_not_flagged():
    d = analyze_module(
        'Процедура П()\n    Перем Х;\n    Если Х = 1 Тогда\n        А = 1;\n'
        '    КонецЕсли;\nКонецПроцедуры\n')
    assert not any(x.code == 'UnusedLocalVariable' for x in d)


def test_break_outside_loop():
    d = analyze_module('Процедура П()\n    Прервать;\nКонецПроцедуры\n')
    assert ('BreakContinueOutsideLoop', 'ERROR') in codes(d)


def test_continue_outside_loop():
    d = analyze_module('Процедура П()\n    Продолжить;\nКонецПроцедуры\n')
    assert ('BreakContinueOutsideLoop', 'ERROR') in codes(d)


def test_break_inside_loop_ok():
    d = analyze_module(
        'Процедура П()\n    Пока Истина Цикл\n        Прервать;\n'
        '    КонецЦикла;\nКонецПроцедуры\n')
    assert not any(x.code == 'BreakContinueOutsideLoop' for x in d)


def test_break_inside_nested_loop_ok():
    d = analyze_module(
        'Процедура П()\n    Для Каждого Х Из А Цикл\n        Пока Истина Цикл\n'
        '            Прервать;\n        КонецЦикла;\n    КонецЦикла;\n'
        'КонецПроцедуры\n')
    assert not any(x.code == 'BreakContinueOutsideLoop' for x in d)


def test_goto_info():
    d = analyze_module('Процедура П()\n    Перейти ~М;\n    ~М:\nКонецПроцедуры\n')
    assert any(x.code == 'UsingGoto' and x.severity == 'INFO' for x in d)
    assert not any('не найдена' in x.message for x in d)


def test_goto_missing_label():
    d = analyze_module('Процедура П()\n    Перейти ~Нет;\nКонецПроцедуры\n')
    assert any(x.code == 'UsingGoto' and 'не найдена' in x.message for x in d)


def test_syntax_errors_not_in_semantic():
    # analyze_module возвращает ТОЛЬКО семантику, без синтаксиса
    d = analyze_module('Процедура П()\n    А = 1 +\n')
    assert not any(x.code == 'ParseError' for x in d)


# -- по базе (общие модули) ---------------------------------------------------

def test_missing_common_method(tmp_path_factory):
    ctx = _ctx(tmp_path_factory)
    d = analyze_module(
        'Процедура П()\n    ОбщийМодуль1.НетТакого();\nКонецПроцедуры\n', ctx)
    # модуль объекта (не форма): область видимости полна → ERROR
    assert any(x.code == 'MissingCommonModuleMethod'
               and x.severity == 'ERROR' for x in d)


def test_missing_common_method_form_module_warning(tmp_path_factory):
    ctx = _ctx(tmp_path_factory)
    d = analyze_module(
        'Процедура П()\n    ОбщийМодуль1.НетТакого();\nКонецПроцедуры\n', ctx,
        is_form_module=True)
    # в модуле формы имя может быть атрибутом формы → WARNING
    assert any(x.code == 'MissingCommonModuleMethod'
               and x.severity == 'WARNING' for x in d)


def test_missing_common_method_variable_shadowing(tmp_path_factory):
    ctx = _ctx(tmp_path_factory)
    d = analyze_module(
        'Процедура П()\n    ОбщийМодуль1 = ПолучитьСписок();\n'
        '    ОбщийМодуль1.НетТакого();\nКонецПроцедуры\n', ctx)
    # имя назначено переменной в этом методе → не общий модуль
    assert not any(x.code == 'MissingCommonModuleMethod' for x in d)


def test_missing_common_method_module_var_shadowing(tmp_path_factory):
    ctx = _ctx(tmp_path_factory)
    d = analyze_module(
        'Перем ОбщийМодуль1;\n\n'
        'Процедура П()\n    ОбщийМодуль1.НетТакого();\nКонецПроцедуры\n', ctx)
    # модульная переменная с именем общего модуля — тоже затенение
    assert not any(x.code == 'MissingCommonModuleMethod' for x in d)


def test_self_module_nonexport_not_flagged(tmp_path_factory):
    ctx = _ctx(tmp_path_factory)
    d = analyze_module(
        'Процедура П()\n    ОбщийМодуль1.Закрытая();\nКонецПроцедуры\n', ctx,
        self_module='ОбщийМодуль1')
    # вызов своего не-Экспорт метода из того же модуля
    assert not any(x.code == 'NonExportCommonModuleMethod' for x in d)


def test_nonexport_common_method(tmp_path_factory):
    ctx = _ctx(tmp_path_factory)
    d = analyze_module(
        'Процедура П()\n    ОбщийМодуль1.Закрытая();\nКонецПроцедуры\n', ctx)
    assert any(x.code == 'NonExportCommonModuleMethod' for x in d)


def test_export_common_method_ok(tmp_path_factory):
    ctx = _ctx(tmp_path_factory)
    d = analyze_module(
        'Процедура П()\n    ОбщийМодуль1.Экспортная(1);\nКонецПроцедуры\n', ctx)
    assert not any(x.code in ('MissingCommonModuleMethod',
                              'NonExportCommonModuleMethod') for x in d)


def test_unknown_left_side_not_flagged(tmp_path_factory):
    # имя слева — не общий модуль этой базы (переменная/объект другой базы):
    # не проверяется, чтобы не давать ложных срабатываний
    ctx = _ctx(tmp_path_factory)
    d = analyze_module(
        'Процедура П()\n    НекийМодуль.Метод();\nКонецПроцедуры\n', ctx)
    assert not any(x.code == 'MissingCommonModuleMethod' for x in d)


# -- ярус 3-лайт: security/deprecated по именам ------------------------------

def test_execute_external_code_global_only():
    # глобальный Выполнить/Вычислить — динамический код (WARNING)
    d = analyze_module('Процедура П()\n    Выполнить("А = 1");\nКонецПроцедуры\n')
    assert ('ExecuteExternalCode', 'WARNING') in codes(d)
    # точечный Запрос.Выполнить() — штатный метод, НЕ помечается
    d2 = analyze_module(
        'Процедура П()\n    Запрос.Выполнить();\nКонецПроцедуры\n')
    assert not any(x.code == 'ExecuteExternalCode' for x in d2)


def test_vychislit_execute_external_code():
    d = analyze_module('Процедура П()\n    Вычислить("Имя");\nКонецПроцедуры\n')
    assert any(x.code == 'ExecuteExternalCode' for x in d)


def test_modal_windows():
    d = analyze_module(
        'Процедура П()\n    Предупреждение("текст");\n'
        '    Ответ = Вопрос("?", РежимДиалога.ДаНет);\nКонецПроцедуры\n')
    assert sum(1 for x in d if x.code == 'UsingModalWindows') == 2
    assert all(x.severity == 'WARNING'
               for x in d if x.code == 'UsingModalWindows')


def test_deprecated_global_calls():
    d = analyze_module(
        'Процедура П()\n    Д = ТекущаяДата();\n    Сообщить("х");\n'
        '    Где = Найти(Стр, "х");\nКонецПроцедуры\n')
    by_code = {x.code for x in d}
    assert {'DeprecatedCurrentDate', 'DeprecatedMessage', 'DeprecatedFind'} <= by_code
    assert all(x.severity == 'INFO' for x in d
               if x.code.startswith('Deprecated'))


def test_deprecated_find_not_for_method():
    # Коллекция.Найти() — метод объекта, не глобальная Найти
    d = analyze_module(
        'Процедура П()\n    Элемент = Список.Найти(1);\nКонецПроцедуры\n')
    assert not any(x.code == 'DeprecatedFind' for x in d)


def test_file_system_and_external_app():
    d = analyze_module(
        'Процедура П()\n    Ф = НайтиФайлы("C:\\", "*.txt");\n'
        '    ЗапуститьПриложение("notepad");\nКонецПроцедуры\n')
    assert any(x.code == 'FileSystemAccess' for x in d)
    assert any(x.code == 'ExternalAppStarting' for x in d)


def test_internet_access_via_new():
    d = analyze_module(
        'Процедура П()\n    Соед = Новый HTTPСоединение("host");\n'
        '    Почта = Новый ИнтернетПочта;\nКонецПроцедуры\n')
    assert sum(1 for x in d if x.code == 'InternetAccess') == 2


def test_name_checks_no_db_needed():
    # ярус 3-лайт работает без ctx (структурный)
    d = analyze_module('Процедура П()\n    Сообщить("х");\nКонецПроцедуры\n', None)
    assert any(x.code == 'DeprecatedMessage' for x in d)


def test_local_method_shadows_global_name():
    # модуль сам объявил метод «Найти» — его вызов не глобальная Найти
    d = analyze_module(
        'Функция Найти(Список)\n    Возврат Список;\nКонецФункции\n\n'
        'Процедура П()\n    Элемент = Найти(Список);\nКонецПроцедуры\n', None)
    assert not any(x.code == 'DeprecatedFind' for x in d)


def test_global_find_still_flagged():
    # глобальная Найти (без объявления в модуле) — помечается
    d = analyze_module(
        'Процедура П()\n    Где = Найти(Строка, "х");\nКонецПроцедуры\n', None)
    assert any(x.code == 'DeprecatedFind' for x in d)


# -- ярус 2: метаданные -------------------------------------------------------

def test_metadata_object_missing(tmp_path_factory):
    ctx = _ctx(tmp_path_factory)
    d = analyze_module(
        'Процедура П()\n    Ссылка = Справочники.НетТакого.НайтиПоКоду("1");\n'
        'КонецПроцедуры\n', ctx)
    assert any(x.code == 'MetadataObjectMissing'
               and x.severity == 'ERROR' for x in d)


def test_metadata_object_present_ok(tmp_path_factory):
    ctx = _ctx(tmp_path_factory)
    d = analyze_module(
        'Процедура П()\n    Ссылка = Справочники.Справочник1.НайтиПоКоду("1");\n'
        'КонецПроцедуры\n', ctx)
    assert not any(x.code == 'MetadataObjectMissing' for x in d)


def test_metadata_chain_with_prefix_not_flagged(tmp_path_factory):
    ctx = _ctx(tmp_path_factory)
    d = analyze_module(
        'Процедура П()\n'
        '    Если Метаданные.Справочники.Содержит(Имя) Тогда\n'
        '        А = 1;\n'
        '    КонецЕсли;\n'
        '    СтрокаТаблицы.Документы.Добавить();\n'
        'КонецПроцедуры\n', ctx)
    # голова цепочки — не менеджер (Метаданные / переменная): не проверяем
    assert not any(x.code == 'MetadataObjectMissing' for x in d)


def test_manager_method_whitelisted(tmp_path_factory):
    ctx = _ctx(tmp_path_factory)
    d = analyze_module(
        'Процедура П()\n    Узел = ПланыОбмена.ГлавныйУзел();\n'
        '    Типы = Справочники.ТипВсеСсылки();\nКонецПроцедуры\n', ctx)
    assert not any(x.code == 'MetadataObjectMissing' for x in d)


def test_metadata_manager_variable_shadowing(tmp_path_factory):
    ctx = _ctx(tmp_path_factory)
    d = analyze_module(
        'Процедура П()\n    Справочники = Новый Массив;\n'
        '    Справочники.Добавить(1);\nКонецПроцедуры\n', ctx)
    assert not any(x.code == 'MetadataObjectMissing' for x in d)


# -- ярус 2: запросы в модуле -------------------------------------------------

def test_query_in_module_bad_table(tmp_path_factory):
    ctx = _ctx(tmp_path_factory)
    d = analyze_module(
        'Процедура П()\n'
        '    Запрос = Новый Запрос;\n'
        '    Запрос.Текст = "ВЫБРАТЬ Т.Ссылка ИЗ Справочник.НетТакого КАК Т";\n'
        'КонецПроцедуры\n', ctx)
    assert any(x.code == 'QueryToMissingMetadata'
               and x.severity == 'ERROR' for x in d)


def test_query_in_module_ok(tmp_path_factory):
    ctx = _ctx(tmp_path_factory)
    d = analyze_module(
        'Процедура П()\n'
        '    Запрос.Текст = "ВЫБРАТЬ Т.СсылкаАтрибут ИЗ '
        'Справочник.Справочник1 КАК Т";\n'
        'КонецПроцедуры\n', ctx)
    assert not any(x.code in ('QueryToMissingMetadata', 'QueryParseError',
                              'ConfdbQueryValidation') for x in d)


def test_query_template_with_percent_skipped(tmp_path_factory):
    ctx = _ctx(tmp_path_factory)
    d = analyze_module(
        'Процедура П()\n'
        '    Текст = "ВЫБРАТЬ %1.Ссылка ИЗ Справочник.%2 КАК %1";\n'
        'КонецПроцедуры\n', ctx)
    assert not any(x.code in ('QueryToMissingMetadata', 'QueryParseError',
                              'ConfdbQueryValidation') for x in d)


def test_query_hash_temp_table_not_syntax_error(tmp_path_factory):
    ctx = _ctx(tmp_path_factory)
    d = analyze_module(
        'Процедура П()\n'
        '    Запрос.Текст = "ВЫБРАТЬ ПЕРВЫЕ 1 Х.Ссылка ИЗ #Врем КАК Х";\n'
        'КонецПроцедуры\n', ctx)
    # #ВременнаяТаблица — легальный синтаксис, чужой ПОМЕСТИТЬ-набор: не ошибка
    assert not any(x.code in ('QueryToMissingMetadata', 'QueryParseError')
                   for x in d)


def test_query_hash_template_skipped(tmp_path_factory):
    ctx = _ctx(tmp_path_factory)
    d = analyze_module(
        'Процедура П()\n'
        '    Запрос.Текст = "ВЫБРАТЬ #Таблица.Поле ИЗ Справочник.НетТакого";\n'
        'КонецПроцедуры\n', ctx)
    # '#' в тексте — шаблон с подстановкой: запрос не проверяется вовсе
    assert not any(x.code in ('QueryToMissingMetadata', 'QueryParseError',
                              'ConfdbQueryValidation') for x in d)


def test_query_versiya_dannyh_is_standard_field(tmp_path_factory):
    ctx = _ctx(tmp_path_factory)
    d = analyze_module(
        'Процедура П()\n'
        '    Запрос.Текст = "ВЫБРАТЬ Т.ВерсияДанных ИЗ '
        'Справочник.Справочник1 КАК Т";\n'
        'КонецПроцедуры\n', ctx)
    assert not any(x.code == 'ConfdbQueryValidation' for x in d)


# -- ярус 2: область видимости и контексты ------------------------------------

def test_bracket_index_chain_skipped(tmp_path_factory):
    ctx = _ctx(tmp_path_factory)
    d = analyze_module(
        'Процедура П()\n'
        '    Документы[ИмяДокумента].ПолучитьМакет("Основной");\n'
        '    Значение = ОбщийМодуль1[Индекс].Свойство;\n'
        'КонецПроцедуры\n', ctx)
    # [индекс] делает состав цепочки динамическим — не проверяем
    assert not any(x.code in ('MetadataObjectMissing',
                              'MissingCommonModuleMethod') for x in d)


def test_extra_scope_suppresses_common_module(tmp_path_factory):
    ctx = _ctx(tmp_path_factory)
    d = analyze_module(
        'Процедура П()\n    ОбщийМодуль1.НетТакого();\nКонецПроцедуры\n', ctx,
        extra_scope=frozenset({'ОбщийМодуль1'}))
    # реквизит/ТЧ объекта с именем общего модуля затеняет его
    assert not any(x.code == 'MissingCommonModuleMethod' for x in d)


def test_extra_scope_suppresses_manager(tmp_path_factory):
    ctx = _ctx(tmp_path_factory)
    code = 'Процедура П()\n    Документы.Удалить(0);\nКонецПроцедуры\n'
    # реквизит/ТЧ с именем менеджера затеняет менеджер
    d = analyze_module(code, ctx, extra_scope=frozenset({'Документы'}))
    assert not any(x.code == 'MetadataObjectMissing' for x in d)
    # без затенения «Удалить» — не объект метаданных и не метод менеджера
    d2 = analyze_module(code, ctx)
    assert any(x.code == 'MetadataObjectMissing' for x in d2)


def test_client_server_conflict(tmp_path_factory):
    ctx = _ctx(tmp_path_factory)
    d = analyze_module(
        '&НаКлиенте\nПроцедура П()\n    ОбщийМодуль1.Экспортная(1);\n'
        'КонецПроцедуры\n', ctx)
    # ОбщийМодуль1 в тестовой базе имеет контекст «Сервер»
    assert any(x.code == 'ClientServerCallConflict'
               and x.severity == 'WARNING' for x in d)


def test_context_conflict_server_call_is_legal():
    from confdb.bsl_analyzer import context_conflict
    # «Вызов сервера» — легальный вызов с клиента через серверный прокси
    assert context_conflict('client', 'Сервер,Вызов сервера') is None
    assert context_conflict('client', 'Сервер') is not None
    assert context_conflict('client', 'Клиент (управляемое приложение)') is None
    assert context_conflict(None, 'Сервер') is None
