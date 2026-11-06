"""Тесты синтаксического анализатора BSL (bsl_syntax) и rebuild_module."""
import pytest

from confdb.bsl_parser import parse_methods, rebuild_module
from confdb.bsl_syntax import analyze, check_text, lex


def diags(text):
    return check_text(text)


def messages(text):
    return [d.message for d in check_text(text)]


def ok(text):
    d = check_text(text)
    assert d == [], [(x.line, x.col, x.code, x.message) for x in d]


# ---------------------------------------------------------------------------
# Корректный синтаксис
# ---------------------------------------------------------------------------

def test_empty_and_comments():
    ok('')
    ok('// только комментарий\n')
    ok('\n\n\n')


def test_module_vars():
    ok('Перем А;\nПерем Б Экспорт, В;')
    ok('Перем А Экспорт;')


def test_procedure_function_basics():
    ok('''\
Процедура П()
    А = 1;
КонецПроцедуры

Функция Ф(Знач А, Б = 1, В = Неопределено) Экспорт
    Возврат А + Б;
КонецФункции
''')


def test_statements():
    ok('''\
Процедура П()
    Если А = 1 Тогда
        Б = 2;
    ИначеЕсли А = 2 Тогда
        Б = 3;
    Иначе
        Б = 4;
    КонецЕсли;
    Для Инд = 0 По 10 Цикл
        Продолжить;
    КонецЦикла;
    Для Каждого Элемент Из Список Цикл
        Прервать;
    КонецЦикла;
    Пока Условие Цикл
        Выполнить("А = 1;");
    КонецЦикла;
    Попытка
        ВызватьИсключение "Ошибка";
    Исключение
        Инфо = ОписаниеОшибки();
        ВызватьИсключение;
    КонецПопытки;
    Возврат;
КонецПроцедуры
''')


def test_goto_labels():
    ok('''\
Процедура П()
    Перейти ~Выход;
    ~Выход:
    Возврат;
КонецПроцедуры
''')


def test_async_await():
    ok('''\
&НаКлиенте
Асинх Процедура П()
    Ответ = Ждать ВопросАсинх("?", РежимДиалога.ДаНет);
    Ждать Метод();
КонецПроцедуры
''')


def test_await_outside_async_is_identifier():
    # Ждать вне Асинх-метода — обычное имя (переменная/метод), не ошибка
    ok('''\
Процедура П()
    Ждать = 1;
    Если Ждать Тогда
        Ждать.Метод();
    КонецЕсли;
КонецПроцедуры
''')


def test_async_mode_ends_at_method_end():
    ok('''\
Асинх Функция Ф()
    Возврат Ждать Сервис.Метод();
КонецФункции

Процедура П()
    Ждать = 2;
КонецПроцедуры
''')


def test_annotations_extensions():
    ok('''\
&Перед("ПередЗаписью")
Процедура Расш1_ПередЗаписью(Отказ)
    Выполнить КодКоманды;
КонецПроцедуры

&Вместо("Ф")
&ИзменениеИКонтроль("Проверить")
Функция Расш1_Ф()
    Возврат ПродолжитьВызов();
КонецФункции

&После("П")
Процедура Расш1_П()
КонецПроцедуры
''')


def test_compiler_directives():
    ok('''\
&НаКлиентеНаСервереБезКонтекста
Процедура А()
КонецПроцедуры

&НаСервереБезКонтекста
Функция Б()
КонецФункции
''')


def test_preprocessor_if_regions():
    ok('''\
#Если Сервер Или ТолстыйКлиентОбычноеПриложение Или ВнешнееСоединение Тогда
#Область СлужебныеПроцедуры
Процедура П()
    #Если Клиент Тогда
        А = 1;
    #ИначеЕсли Сервер Тогда
        А = 2;
    #Иначе
        А = 3;
    #КонецЕсли
КонецПроцедуры
#КонецОбласти
#КонецЕсли
''')


def test_preprocessor_unknown_symbol_allowed():
    ok('#Если МойСимвол Тогда\nА = 1;\n#КонецЕсли\n')


def test_preprocessor_not_and_parens():
    ok('#Если Не (Клиент Или ВебКлиент) И Сервер Тогда\n#КонецЕсли\n')


def test_preprocessor_inside_method_and_string():
    ok('''\
Процедура П()
    #Если Сервер Тогда
    Текст = "начало
    |#Если Что-то
    |продолжение";
    #КонецЕсли
КонецПроцедуры
''')


def test_delete_block_not_parsed():
    ok('''\
#Удаление
это вообще не код 1С @@@ !!!
Процедура
#КонецУдаления
Процедура Норм()
КонецПроцедуры
''')


def test_insert_markers_transparent():
    ok('''\
#Вставка
Процедура П()
КонецПроцедуры
#КонецВставки
''')


def test_multiline_string():
    ok('''\
Текст = "ВЫБРАТЬ
    |Поле1,
    |Поле2
    |ИЗ
    |   Таблица";
''')


def test_string_juxtaposition_and_escapes():
    ok('А = "часть1" "часть2";')
    ok('А = "с ""кавычками"" внутри";')


def test_datetime_and_numbers():
    ok("Д = '20240101';")
    ok("Д = '20240101123045';")
    ok("Д = '';")
    ok('А = 1; Б = 1.5; В = 0.123;')


def test_new_expression_forms():
    ok('А = Новый Массив;')
    ok('А = Новый Массив(10);')
    ok('А = Новый(Тип("Массив"));')
    ok('А = Новый("AddIn." + Идентификатор);')
    ok('А = Новый Структура("Ключ, Значение", 1, 2);')


def test_ternary_and_chains():
    ok('А = ?(Усл, 1, 2);')
    ok('А = ?(Усл, ?(Усл2, 1, 2), 3);')
    ok('А = Объект.Свойство.Вложенное[0].Метод(1, "стр");')
    ok('А = Таблица.Колонки.Добавить().Имя;')


def test_keyword_after_dot_is_member():
    ok('Объект.Если = 1;')
    ok('Объект.Пока = Объект.Для;')
    ok('Если Объект.Тогда Тогда\nКонецЕсли;')


def test_call_across_lines_after_dot():
    ok('''\
Результат = ВызовСервера
    .ПроверитьПодключение();
''')
    ok('''\
Результат = Объект.
    Метод(1);
''')


def test_trailing_dot_tolerated():
    # incompleteAccess в грамматике bsl-parser — не ошибка
    ok('А = Объект.\n')
    ok('Метод(Объект.);')


def test_statements_without_semicolon():
    ok('''\
Процедура П()
    А = 1
    Б = 2;
    ;;
КонецПроцедуры
''')


def test_module_level_code():
    ok('''\
Перем А;
А = 1;
Сообщить(А);
Процедура П()
КонецПроцедуры
А = 2;
''')


def test_english_keywords():
    ok('''\
Procedure DoIt() Export
    If A = 1 Then
        B = 2;
    ElsIf A = 2 Then
        B = 3;
    Else
        B = 4;
    EndIf;
    For Each Item In List Do
        Break;
    EndDo;
    Try
        Raise "err";
    Except
        Return;
    EndTry;
EndProcedure
''')


def test_case_insensitivity():
    ok('ПЕРЕМ А;\nперем б;\nПерем В;')
    ok('процедура п()\nконецпроцедуры')


def test_add_remove_handler():
    ok('''\
ДобавитьОбработчик Событие, Обработчик;
УдалитьОбработчик Событие, Обработчик;
''')


def test_execute_forms():
    ok('Выполнить("А = 1;");')
    ok('Выполнить КодКоманды;')
    ok('Выполнить("А = " + Значение);')


def test_raise_forms():
    ok('ВызватьИсключение;')
    ok('ВызватьИсключение "текст";')
    ok('ВызватьИсключение("текст");')
    ok('ВызватьИсключение ОписаниеОшибки();')


def test_unary_operators():
    ok('А = -1; Б = +2; В = Не А; Г = Не Не А;')


def test_index_and_call_chain():
    ok('А = Массив[0][1];')
    ok('ФункцияВозвращающаяМассив()[0].Свойство = 1;')


def test_invisible_chars_are_whitespace():
    ok('\ufeffПерем А;')
    ok('Перем\u00a0А;')


def test_local_vars_in_method():
    ok('''\
Процедура П()
    Перем Локальная;
    Локальная = 1;
КонецПроцедуры
''')


def test_shebang_and_use():
    ok('#!/usr/bin/env oscript\n#Использовать os\n#Использовать "path/to/lib"\n')


def test_native_stack_annotations():
    ok('#Native\n#Stack\nПерем А;')


def test_region_named_like_keyword():
    ok('#Область Если\n#КонецОбласти\n')


# ---------------------------------------------------------------------------
# Ошибки
# ---------------------------------------------------------------------------

def test_unclosed_procedure():
    d = diags('Процедура П()\n    А = 1;\n')
    assert any('КонецПроцедуры' in m for m in
               [x.message for x in d])


def test_mismatched_method_end():
    d = messages('Функция Ф()\nКонецПроцедуры\n')
    assert any('КонецФункции' in m for m in d)


def test_unclosed_string():
    d = messages('А = "незакрытая\nБ = 1;\n')
    assert any('строковый литерал' in m for m in d)


def test_unclosed_if():
    d = messages('Если А Тогда\nБ = 1;\n')
    assert any('КонецЕсли' in m for m in d)


def test_missing_then():
    d = messages('Если А\nБ = 1;\nКонецЕсли;\n')
    assert any('Тогда' in m for m in d)


def test_unclosed_paren():
    d = messages('Сообщить("а"\n')
    assert any('«)»' in m for m in d)


def test_unbalanced_preproc_if():
    d = messages('#Если Сервер Тогда\nА = 1;\n')
    assert any('#КонецЕсли' in m for m in d)


def test_unbalanced_region():
    d = messages('#Область Имя\nА = 1;\n')
    assert any('#КонецОбласти' in m for m in d)


def test_stray_endif():
    d = messages('#КонецЕсли\n')
    assert any('без #Если' in m for m in d)


def test_stray_endregion():
    d = messages('#КонецОбласти\n')
    assert any('без #Область' in m for m in d)


def test_elsif_after_else():
    d = messages('#Если А Тогда\n#Иначе\n#ИначеЕсли Б Тогда\n#КонецЕсли\n')
    assert any('после #Иначе' in m for m in d)


def test_double_else():
    d = messages('#Если А Тогда\n#Иначе\n#Иначе\n#КонецЕсли\n')
    assert any('повторное #Иначе' in m for m in d)


def test_region_without_name():
    d = messages('#Область\n#КонецОбласти\n')
    assert any('без имени' in m for m in d)


def test_preproc_if_without_then():
    d = messages('#Если Сервер\n#КонецЕсли\n')
    assert any('Тогда' in m for m in d)


def test_preproc_if_empty_condition():
    d = messages('#Если Тогда\n#КонецЕсли\n')
    assert any('Тогда' in m for m in d)


def test_unknown_preproc_directive():
    d = messages('#АбыЧто\n')
    assert any('неизвестн' in m.lower() for m in d)


def test_invalid_character():
    d = diags('А = 1 @ 2;\n')
    assert any(x.code == 'InvalidCharacterInFile' for x in d)


def test_bare_identifier_statement():
    d = messages('Процедура П()\n    Имя;\nКонецПроцедуры\n')
    assert any('вызов' in m or 'присваивание' in m for m in d)


def test_number_statement():
    d = diags('123;\n')
    assert d


def test_empty_param_after_comma():
    d = messages('Процедура П(А, )\nКонецПроцедуры\n')
    assert any('имя параметра' in m for m in d)


def test_non_constant_param_default():
    d = messages('Процедура П(А = Массив())\nКонецПроцедуры\n')
    assert any('константой' in m for m in d)


def test_directive_without_declaration():
    d = messages('&НаКлиенте\nА = 1;\n')
    assert any('&-директив' in m for m in d)


def test_hanging_operator():
    d = messages('А = 1 +\n')
    assert d


def test_assignment_expected_expression():
    d = messages('А = ;\n')
    assert any('выражение' in m for m in d)


def test_goto_without_label():
    d = messages('Процедура П()\n    Перейти Метка;\nКонецПроцедуры\n')
    assert any('~' in m for m in d)


def test_stray_endprocedure():
    d = messages('КонецПроцедуры\n')
    assert any('без процедуры' in m for m in d)


def test_diagnostic_positions():
    d = diags('А = 1;\nЕсли А Тогда')
    err = [x for x in d if 'КонецЕсли' in x.message]
    assert err
    # ошибка «ожидался КонецЕсли» сообщается на конце файла (строка 2)
    assert err[0].line == 2
    d2 = diags('А = 1;\nБ = 2 @ 3;\n')
    inv = [x for x in d2 if x.code == 'InvalidCharacterInFile']
    assert inv
    assert (inv[0].line, inv[0].col) == (2, 7)


def test_error_cap():
    # неограниченный каскад обрезается потолком диагностик
    text = 'А = ;\n' * 500
    result = analyze(text)
    assert result.truncated
    assert len(result.diags) <= 120


# ---------------------------------------------------------------------------
# Лексер и извлекаемые сведения
# ---------------------------------------------------------------------------

def test_lex_token_kinds():
    tokens, errs = lex('А = 1.5; // коммент\n')
    kinds = [t.kind for t in tokens]
    assert kinds == ['IDENT', 'ASSIGN', 'NUM', 'SEMI', 'EOF']
    assert errs == []


def test_analyze_methods_and_vars():
    r = analyze('''\
Перем Модульная Экспорт;

&НаСервере
Асинх Процедура П(Знач А, Б = 1) Экспорт
    Перем Локальная;
КонецПроцедуры

Функция Ф()
    Возврат 1;
КонецФункции
''')
    assert r.diags == []
    assert [m['name'] for m in r.methods] == ['П', 'Ф']
    m0 = r.methods[0]
    assert m0['kind'] == 'процедура'
    assert m0['is_async'] is True
    assert m0['is_export'] is True
    assert m0['directives'] == ['&НаСервере']
    assert [p['name'] for p in m0['params']] == ['А', 'Б']
    assert m0['params'][0]['by_value'] is True
    assert m0['params'][1]['default'] == '1'
    assert m0['locals'] == ['Локальная']
    assert r.module_vars == [
        {'name': 'Модульная', 'is_export': True, 'line': 1}]


def test_analyze_events():
    r = analyze('''\
Процедура П()
    Справочники.Номенклатура.Создать();
    ~Метка:
    Перейти ~Метка;
    А = Новый Массив;
КонецПроцедуры
''')
    kinds = {e[0] for e in r.events}
    assert {'chain', 'label', 'goto', 'new', 'assign'} <= kinds
    chains = [e[1] for e in r.events if e[0] == 'chain']
    assert ('Справочники', 'Номенклатура', 'Создать') in chains


# ---------------------------------------------------------------------------
# rebuild_module
# ---------------------------------------------------------------------------

ROUNDTRIP_MODULE = '''\
Перем Кэш;

#Область Служебные

&НаСервере
Процедура ПриСозданииНаСервере(Отказ,
        СтандартнаяОбработка)

    Кэш = Неопределено;
    // комментарий в теле
    Если Отказ Тогда
        Возврат;
    КонецЕсли;

КонецПроцедуры

&НаКлиенте
Функция ЗначениеПоУмолчанию() Экспорт

    Возврат 0;

КонецФункции

#КонецОбласти
'''


def test_rebuild_roundtrip_exact():
    body, methods = parse_methods(ROUNDTRIP_MODULE)
    rebuilt = rebuild_module(body, methods)
    assert rebuilt == ROUNDTRIP_MODULE.rstrip('\n')


def test_rebuild_roundtrip_via_reparse():
    body, methods = parse_methods(ROUNDTRIP_MODULE)
    rebuilt = rebuild_module(body, methods)
    _body2, methods2 = parse_methods(rebuilt)
    assert [m['name'] for m in methods2] == [m['name'] for m in methods]
    for m1, m2 in zip(methods, methods2):
        assert m1['body'] == m2['body']
        assert m1['line_start'] == m2['line_start']
        assert m1['line_end'] == m2['line_end']


def test_rebuild_mismatched_method_is_safe():
    # расхождение данных: сигнатура не совпала с методом — строки как есть
    body, methods = parse_methods(ROUNDTRIP_MODULE)
    methods[0]['name'] = 'ДругоеИмя'
    rebuilt = rebuild_module(body, methods)
    assert 'ПриСозданииНаСервере' in rebuilt


def test_rebuild_unclosed_method():
    broken = 'Процедура П()\nА = 1;\n'
    _body, methods = parse_methods(broken)
    rebuilt = rebuild_module(_body, methods)
    assert rebuilt.strip() == broken.strip()


def test_rebuild_no_methods():
    text = 'Перем А;\n#Область Х\n#КонецОбласти'
    body, methods = parse_methods(text)
    assert methods == []
    assert rebuild_module(body, methods) == text
