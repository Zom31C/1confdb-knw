r"""Семантические проверки модуля BSL поверх базы знаний (итерация 2).

Надстройка над \`bsl_syntax.analyze\` (структура: методы, переменные, события) и
\`bsl_analyzer.analyze\` (разрешение общих модулей и их методов по базе). Каждая
проверка даёт \`Diagnostic\` (из \`bsl_syntax\`) с кодом и severity ERROR/WARNING/INFO.
Синтаксис здесь НЕ проверяется — только семантика; инструмент MCP объединяет
результаты обоих слоёв.

Отбор правил — по уровню ложных срабатываний на живых вендорских конфигурациях
(\`_tmp/probe_semantic.py\` по УНФ/ЗУП/БП): включены только правила, которые на
компилируемом платформой коде не дают вала ложных тревог. Осознанно НЕ включены
шумные правила: неиспользуемый параметр (десятки тысяч на конфигурацию —
обработчики событий legitimately имеют неиспользуемые параметры), неразрешённые
обращения к метаданным (тысячи — общий платформенный тип, объект другой базы,
динамическое разрешение), функция без возвращаемого значения (сотни — спорно).
Глубокий вывод типов и разрешение имён по библиотеке платформы не выполняются.

Проверки (коды — имена диагностик BSL Language Server, где они есть):
- UnusedLocalVariable (WARNING) — локальная \`Перем\` объявлена и ни разу не
  встречена в теле метода (ни чтение, ни запись);
- BreakContinueOutsideLoop (ERROR) — \`Прервать\`/\`Продолжить\` вне цикла
  (платформа такой код не компилирует — на живых конфигурациях 0 срабатываний);
- MissingCommonModuleMethod / NonExportCommonModuleMethod (WARNING) — вызов
  \`ОбщийМодуль.Метод()\`, где общий модуль есть в ЭТОЙ базе, но метода нет или
  он не Экспорт. WARNING, а не ERROR: лексический разбор может спутать имя
  общего модуля с переменной той же имени (редкие ложные срабатывания), поэтому
  результат нужно сверять с \`method_dependencies\`. Неизвестное имя слева
  (объект другой базы/переменная) не проверяется вовсе.
- UsingGoto (INFO) — использование \`Перейти ~Метка\` (метка должна существовать).

Проверки текстов запросов и разрешения метаданных сюда НАМЕРЕННО не включены:
на живых конфигурациях они дают тысячи ложных срабатываний (запросы-шаблоны с
\`%\`, динамическая сборка, общий платформенный тип, объект другой базы). Для
запросов есть \`check_query\`, для связей кода и разрешений — \`method_dependencies\`.
"""
from .bsl_syntax import Diagnostic, analyze as syntax_analyze

__all__ = ['analyze_module']


def analyze_module(text, ctx=None):
    """Список семантических Diagnostic по тексту модуля (синтаксис не входит).

    :param text: полный текст модуля (как после bsl_parser.rebuild_module);
    :param ctx: bsl_analyzer.BslContext — для проверок по базе; None = только
        структурные проверки (общие модули не проверяются).
    """
    result = syntax_analyze(text)
    diags = []
    diags += _unused_locals(result)
    diags += _break_continue_outside_loop(result)
    diags += _goto_usage(result)
    if ctx is not None:
        diags += _common_module_calls(text, ctx)
    diags.sort(key=lambda d: (d.line, d.col))
    return diags


def _used_names_in(events, lo, hi):
    """Множество имён (lower), встреченных в событиях within строк lo..hi."""
    used = set()
    for e in events:
        ln = e[-1]
        if ln < lo or ln > hi:
            continue
        kind = e[0]
        if kind == 'chain':
            parts = e[1]
            if parts and parts[0]:
                used.add(parts[0].lower())
        elif kind == 'assign':
            for p in e[1]:
                if p:
                    used.add(p.lower())
    return used


def _unused_locals(result):
    diags = []
    for m in result.methods:
        lo, hi = m['line_start'], m['line_end']
        used = _used_names_in(result.events, lo, hi)
        for e in result.events:
            if e[0] == 'localvar' and lo <= e[2] <= hi:
                name = e[1]
                if name.lower() not in used:
                    diags.append(Diagnostic(
                        'UnusedLocalVariable',
                        'локальная переменная «%s» объявлена, но не используется'
                        % name, e[2], 1, severity='WARNING'))
    return diags


def _break_continue_outside_loop(result):
    diags = []
    for e in result.events:
        if e[0] in ('break', 'continue') and e[1] == 0:
            word = 'Прервать' if e[0] == 'break' else 'Продолжить'
            diags.append(Diagnostic(
                'BreakContinueOutsideLoop',
                '«%s» вне тела цикла' % word, e[2], 1, severity='ERROR'))
    return diags


def _goto_usage(result):
    diags = []
    labels = {e[1].lower() for e in result.events if e[0] == 'label'}
    for e in result.events:
        if e[0] == 'goto':
            name = e[1]
            msg = 'используется «Перейти ~%s»' % name
            if name.lower() not in labels:
                msg += ' — метка не найдена в модуле'
            diags.append(Diagnostic('UsingGoto', msg, e[2], 1, severity='INFO'))
    return diags


def _common_module_calls(text, ctx):
    """Вызовы методов общих модулей, неразрешимые в ЭТОЙ базе (WARNING: имя
    слева может оказаться переменной с тем же именем — нужна сверка)."""
    from .bsl_analyzer import analyze as lex_analyze
    diags = []
    try:
        report = lex_analyze(text, ctx)
    except Exception:  # noqa: BLE001 — семантика не должна ронять проверку
        return diags
    for mod, meth, line, state in report['modules']:
        if 'не найден' in state:
            diags.append(Diagnostic(
                'MissingCommonModuleMethod',
                'общий модуль «%s» не содержит метод «%s» (проверьте: имя может '
                'быть переменной)' % (mod, meth), line, 1, severity='WARNING'))
        elif 'не Экспорт' in state:
            diags.append(Diagnostic(
                'NonExportCommonModuleMethod',
                'метод «%s» общего модуля «%s» не Экспорт — вызов извне не '
                'работает' % (meth, mod), line, 1, severity='WARNING'))
    return diags
