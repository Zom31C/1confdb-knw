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
    assert any(x.code == 'MissingCommonModuleMethod'
               and x.severity == 'WARNING' for x in d)


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
