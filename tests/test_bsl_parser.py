"""Тесты разбора модулей 1С на процедуры/функции."""
from confdb.bsl_parser import parse_methods

MODULE = '''Перем Кэш;

#Область Служебные

&НаСервере
Процедура ПриСозданииНаСервере(Отказ,
		СтандартнаяОбработка)

	Кэш = Неопределено;

КонецПроцедуры

&НаКлиенте
Функция ЗначениеПоУмолчанию() Экспорт

	Возврат 0;

КонецФункции

#КонецОбласти

Функция Простая(А, Б = 1)

	Возврат А + Б;

КонецФункции
'''

MODULE_TEXT = '''Перем Кэш;

#Область Служебные

&НаСервере
Процедура ПриСозданииНаСервере(Отказ,
		СтандартнаяОбработка)

&НаКлиенте
Функция ЗначениеПоУмолчанию() Экспорт

#КонецОбласти

Функция Простая(А, Б = 1)'''


def test_methods_count_and_kinds():
    _, methods = parse_methods(MODULE)
    assert [m['name'] for m in methods] == [
        'ПриСозданииНаСервере', 'ЗначениеПоУмолчанию', 'Простая']
    assert [m['kind'] for m in methods] == ['процедура', 'функция', 'функция']


def test_directives():
    _, methods = parse_methods(MODULE)
    assert methods[0]['directives'] == ['&НаСервере']
    assert methods[1]['directives'] == ['&НаКлиенте']
    assert methods[2]['directives'] == []


def test_multiline_signature_and_export():
    _, methods = parse_methods(MODULE)
    assert methods[0]['signature'] == 'Отказ, СтандартнаяОбработка'
    assert methods[0]['is_export'] is False
    assert methods[1]['is_export'] is True
    assert methods[2]['signature'] == 'А, Б = 1'


def test_module_text_without_method_code():
    module_text, methods = parse_methods(MODULE)
    # модуль как есть, но без кода методов; сигнатуры и #… сохранены
    assert module_text == MODULE_TEXT
    assert 'Кэш = Неопределено' not in module_text
    assert 'Возврат' not in module_text


def test_line_numbers_and_body():
    _, methods = parse_methods(MODULE)
    m0 = methods[0]
    # сигнатура — строка 6, КонецПроцедуры — строка 11
    assert m0['line_start'] == 6
    assert m0['line_end'] == 11
    assert m0['body'] == ('Процедура ПриСозданииНаСервере(Отказ,\n'
                          '\t\tСтандартнаяОбработка)\n'
                          '\n'
                          '\tКэш = Неопределено;\n'
                          '\n'
                          'КонецПроцедуры')
    assert '#Область' not in m0['body']


def test_description_and_module_comments():
    module = ('// заголовок модуля\n'
              '\n'
              '#Если Сервер Тогда\n'
              '\n'
              '// Параметры:\n'
              '//   А - Число\n'
              'Процедура А(А)\n'
              '\tБ = А;\n'
              'КонецПроцедуры\n'
              '\n'
              '#КонецЕсли\n')
    module_text, methods = parse_methods(module)
    m = methods[0]
    # прилипший блок комментариев — в description, как есть
    assert m['description'] == '// Параметры:\n//   А - Число'
    assert m['body'] == 'Процедура А(А)\n\tБ = А;\nКонецПроцедуры'
    assert m['line_start'] == 7 and m['line_end'] == 9
    # оторванный комментарий и препроцессор остаются в тексте модуля
    assert '// заголовок модуля' in module_text
    assert '#Если Сервер Тогда' in module_text
    assert '#КонецЕсли' in module_text
    assert '\tБ = А;' not in module_text
    # подстановка тела вместо сигнатуры восстанавливает исходник
    rebuilt = module_text.replace('Процедура А(А)', m['body'], 1)
    assert rebuilt == module.rstrip('\n')


def test_description_through_directive():
    module = ('// Возвращает значение по умолчанию\n'
              '&НаКлиенте\n'
              'Функция Значение() Экспорт\n'
              '\tВозврат 0;\n'
              'КонецФункции\n')
    _, methods = parse_methods(module)
    assert methods[0]['description'] == '// Возвращает значение по умолчанию'
    assert methods[0]['directives'] == ['&НаКлиенте']


def test_empty_module():
    module_text, methods = parse_methods('')
    assert module_text == '' and methods == []


def test_module_without_methods():
    module_text, methods = parse_methods('Перем А;\nПерем Б;\n')
    assert methods == []
    assert module_text == 'Перем А;\nПерем Б;'


ASYNC_MODULE = '''&НаКлиенте
Асинх Процедура ПоказатьДанные(Команда)
\tУспех = Ждать ВопросАсинх("?");
КонецПроцедуры

Асинх Функция ПолучитьАсинх(А) Экспорт
\tВозврат Ждать Сервис.Данные(А);
КонецФункции

Процедура Обычная()
\tА = 1;
КонецПроцедуры
'''


def test_async_methods_extracted():
    _, methods = parse_methods(ASYNC_MODULE)
    assert [m['name'] for m in methods] == [
        'ПоказатьДанные', 'ПолучитьАсинх', 'Обычная']
    assert [m['is_async'] for m in methods] == [True, True, False]
    assert methods[0]['kind'] == 'процедура'
    assert methods[1]['kind'] == 'функция'
    assert methods[1]['is_export'] is True
    # тело включает строку с «Асинх»
    assert methods[0]['body'].startswith('Асинх Процедура ПоказатьДанные')


def test_async_module_text_and_roundtrip():
    from confdb.bsl_parser import rebuild_module
    module_text, methods = parse_methods(ASYNC_MODULE)
    # код асинх-методов вырезан из текста модуля, сигнатуры сохранены
    assert 'ВопросАсинх' not in module_text
    assert 'Асинх Процедура ПоказатьДанные(Команда)' in module_text
    assert 'Асинх Функция ПолучитьАсинх(А) Экспорт' in module_text
    # реконструкция точная
    rebuilt = rebuild_module(module_text, methods)
    assert rebuilt == ASYNC_MODULE.rstrip('\n')


def test_rebuild_old_db_async_inline_is_safe():
    """Старая база: асинх-метод НЕ в списке methods (остался в module.body
    целиком) — реконструкция не должна его съесть или сдвинуть остальные."""
    from confdb.bsl_parser import rebuild_module
    # эмуляция старой базы: текст модуля с цельным асинх-методом,
    # а в списке методов — только обычный
    old_body = '''Асинх Процедура АсинхМетод()
\tЖдать Что-то();
КонецПроцедуры

Процедура Обычная()
'''
    methods = [{'kind': 'процедура', 'name': 'Обычная',
                'body': 'Процедура Обычная()\n\tА = 1;\nКонецПроцедуры'}]
    rebuilt = rebuild_module(old_body, methods)
    assert 'Асинх Процедура АсинхМетод()' in rebuilt
    assert 'Ждать Что-то();' in rebuilt
    assert 'А = 1;' in rebuilt
    assert rebuilt.count('Процедура Обычная()') == 1


def test_async_with_directive_and_multiline_signature():
    module = ('&НаКлиенте\n'
              'Асинх Процедура Обработать(\n'
              '\t\tПарам1,\n'
              '\t\tПарам2) Экспорт\n'
              '\tЖдать ВнешнийСервис.Метод(Парам1);\n'
              'КонецПроцедуры\n')
    module_text, methods = parse_methods(module)
    assert len(methods) == 1
    m = methods[0]
    assert m['is_async'] is True
    assert m['is_export'] is True
    assert m['signature'] == 'Парам1, Парам2'
    assert m['directives'] == ['&НаКлиенте']
    assert m['line_start'] == 2 and m['line_end'] == 6
    # многострочная сигнатура целиком остаётся в тексте модуля, тело вырезано
    assert 'ВнешнийСервис.Метод' not in module_text
    assert 'Асинх Процедура Обработать(' in module_text
    from confdb.bsl_parser import rebuild_module
    assert rebuild_module(module_text, methods) == module.rstrip('\n')
