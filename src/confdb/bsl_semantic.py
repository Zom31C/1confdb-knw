r"""Семантические проверки модуля BSL поверх базы знаний.

Надстройка над \`bsl_syntax.analyze\` (структура: методы, переменные, события)
и базой знаний (общие модули и их методы, объекты метаданных, query_lang).
Каждая проверка даёт \`Diagnostic\` (из \`bsl_syntax\`) с кодом и severity
ERROR/WARNING/INFO. Синтаксис здесь НЕ проверяется — только семантика;
инструмент MCP \`check_syntax(semantic=True)\` объединяет оба слоя.

Ярус 1 — структурные проверки (без базы), отобраны по корпусным замерам:
- UnusedLocalVariable (WARNING) — локальная Перем объявлена и ни разу не
  встречена в теле метода (ни чтение, ни запись);
- BreakContinueOutsideLoop (ERROR) — Прервать/Продолжить вне цикла (0 на
  корпусе УНФ — вендорский код компилируется платформой);
- UsingGoto (INFO) — Перейти ~Метка (+ отметка, если метка не найдена);
- ярус 3-лайт security/deprecated по именам глобальных вызовов и типов
  (GLOBAL_CALL_CHECKS / NEW_TYPE_CHECKS) — только одноэлементные вызовы,
  точечные методы объектов и имена локальных методов модуля не помечаются.

Ярус 2 — проверки по базе (нужен ctx). Построены на ЦЕПОЧКАХ обращений из
событий парсера и области видимости метода (параметры, локальные и модульные
Перем, имена методов модуля, цели присваиваний = переменные циклов):
- MissingCommonModuleMethod — вызов \`ОбщийМодуль.Метод()\`, где модуль есть в
  этой (или соседней открытой) базе, а метода нет. ERROR в модулях объектов и
  общих модулях (там область видимости полна); WARNING в модулях форм —
  атрибут формы может совпадать с именем общего модуля (атрибуты форм не
  извлекаются в meta_attribute). Для баз, собранных до поддержки Асинх,
  делается фолбэк-поиск объявления в тексте модуля;
- NonExportCommonModuleMethod (WARNING) — вызов не-Экспортного метода общего
  модуля (вызов из того же модуля не помечается, если он известен);
- MetadataObjectMissing — обращение \`Менеджер.Имя\` (Справочники.Х, Документы.Х
  и т.п.), где объекта Имя нет ни в этой базе, ни в соседних. Цепочки с
  префиксом (Метаданные.Справочники.Содержит, СтрокаТаблицы.Документы.Добавить)
  НЕ помечаются — проверяется только голова цепочки; цепочки с индексом
  \`Менеджер[Имя].Метод\` пропускаются (состав динамический); платформенные
  методы самих менеджеров (ТипВсеСсылки, СоздатьНабор, ГлавныйУзел, …)
  whitelisted; реквизиты/табличные части объекта (extra_scope) затеняют
  одноимённые менеджеры и общие модули. Имя головы, не разрешённое ни во что
  известное, не помечается НИКОГДА — это граница анализа (переменная из чужого
  контекста, атрибут формы);
- QueryInModule — тексты запросов внутри модуля (extract_queries +
  query_lang.check_query_full): синтаксические ошибки (QueryParseError) и
  неизвестные таблицы/поля (QueryToMissingMetadata / ConfdbQueryValidation).
  Шаблоны (текст с %, # или [) пропускаются; усечённые тексты (ошибка
  «найдено ''» / «Неожиданный токен ''» в конце) — тоже: запрос часто
  собирают по частям;
- ClientServerCallConflict (WARNING) — метод с чистой директивой &НаКлиенте
  напрямую вызывает общий модуль с контекстом «Сервер». Откат по контексту
  модуля намеренно не используется (в обычном приложении клиент легально
  вызывает серверные модули — 1675 ложных на УНФ); модули с признаком
  «Вызов сервера» конфликтом не считаются — это штатный клиент-серверный вызов.

Проверки, от которых отказались по корпусным замерам (тысячи ложных на
компилируемом вендорском коде): разрешение произвольных «неизвестных» имён,
неиспользуемые параметры (96631 на УНФ — обработчики событий), функция без
Возврат (912, спорно). Для зависимостей кода есть method_dependencies.
"""
import re

from .bsl_syntax import Diagnostic, analyze as syntax_analyze

__all__ = ['analyze_module']

# Платформенные методы САМИХ менеджеров коллекций (Менеджер.Метод(), а не
# Менеджер.Объект.Метод()) — не объекты метаданных. Собрано по живым
# конфигурациям (УНФ): ТипВсеСсылки встречается у 6 менеджеров, методы
# ПлановОбмена — в подсистеме обмена.
MANAGER_METHODS = frozenset({
    'типвсессылки', 'типвсессылкиточекмаршрутабизнеспроцессов',
    'создатьнабор', 'главныйузел', 'установитьглавныйузел',
    'зарегистрироватьизменения', 'удалитьрегистрациюизменений',
    'изменениезарегистрировано', 'выбратьизменения', 'прочитатьизменения',
    'записатьизменения', 'создатьзаписьсообщения', 'создатьчтениесообщения',
    'создатьсообщение', 'создатьначальныйобраз', 'создатьменеджерзаписи',
})

# Ярус 3-лайт: платформенные глобальные вызовы по имени (security/deprecated).
# Флагаются ТОЛЬКО одноэлементные вызовы (глобальная функция), не точечные:
# «Выполнить("код")» — динамическое выполнение, а «Запрос.Выполнить()» — штатный
# метод объекта и не помечается. Значение: (код BSL LS, severity, сообщение).
GLOBAL_CALL_CHECKS = {
    'предупреждение': ('UsingModalWindows', 'WARNING',
                       'модальный вызов «Предупреждение» блокирует работу в '
                       'управляемом приложении — используйте неблокающие способы'),
    'вопрос': ('UsingModalWindows', 'WARNING',
               'модальный вызов «Вопрос» блокирует работу в управляемом '
               'приложении — используйте неблокающие способы'),
    'вычислить': ('ExecuteExternalCode', 'WARNING',
                  '«Вычислить» выполняет произвольный код — проверьте '
                  'безопасность входных данных'),
    'запуститьприложение': ('ExternalAppStarting', 'WARNING',
                            '«ЗапуститьПриложение» запускает внешнее приложение '
                            '— проверьте безопасность'),
    'найтифайлы': ('FileSystemAccess', 'WARNING',
                   'обращение к файловой системе («НайтиФайлы»)'),
    'создатькаталог': ('FileSystemAccess', 'WARNING',
                       'обращение к файловой системе («СоздатьКаталог»)'),
    'удалитьфайлы': ('FileSystemAccess', 'WARNING',
                     'обращение к файловой системе («УдалитьФайлы»)'),
    'копироватьфайл': ('FileSystemAccess', 'WARNING',
                       'обращение к файловой системе («КопироватьФайл»)'),
    'переместитьфайл': ('FileSystemAccess', 'WARNING',
                        'обращение к файловой системе («ПереместитьФайл»)'),
    'текущаядата': ('DeprecatedCurrentDate', 'INFO',
                    '«ТекущаяДата» устарела — используйте «ТекущаяДатаСеанса»'),
    'сообщить': ('DeprecatedMessage', 'INFO',
                 '«Сообщить» устарело — используйте «СообщениеПользователю»'),
    'найти': ('DeprecatedFind', 'INFO',
              'глобальная «Найти» устарела — используйте «СтрНайти» '
              '(«Найти» как метод коллекции остаётся допустимым)'),
}

# Типы, создаваемые через «Новый», которые означают обращение к интернету.
NEW_TYPE_CHECKS = {
    'httpсоединение': ('InternetAccess', 'WARNING',
                       'создание «HTTPСоединение» — обращение к интернету'),
    'httpзапрос': ('InternetAccess', 'WARNING',
                   'создание «HTTPЗапрос» — обращение к интернету'),
    'интернетпочта': ('InternetAccess', 'WARNING',
                      'создание «ИнтернетПочта» — обращение к интернету'),
    'ftpсоединение': ('InternetAccess', 'WARNING',
                      'создание «FTPСоединение» — обращение к интернету'),
}

# Объявление метода в тексте модуля (фолбэк для баз до поддержки Асинх)
_RE_METHOD_IN_BODY = re.compile(
    r'(?:^|\n)[ \t]*(?:(?:Асинх|Async)[ \t]+)?(?:Процедура|Функция)[ \t]+',
    re.IGNORECASE)


def analyze_module(text, ctx=None, extra_ctxs=(), self_module=None,
                   is_form_module=False, module_context=None,
                   extra_scope=frozenset()):
    """Список семантических Diagnostic по тексту модуля (синтаксис не входит).

    :param text: полный текст модуля (как после bsl_parser.rebuild_module);
    :param ctx: bsl_analyzer.BslContext — для проверок по базе; None = только
        структурные проверки;
    :param extra_ctxs: контексты соседних открытых баз (расширение/EPF
        ссылаются на объекты основной конфигурации — имя, найденное там,
        не ошибка);
    :param self_module: имя собственного общего модуля (вызовы своих
        не-Экспортных методов из того же модуля не помечаются);
    :param is_form_module: модуль формы — атрибуты форм не извлекаются,
        поэтому имя может оказаться атрибутом: common-module проверки
        снижаются до WARNING;
    :param module_context: контекст модуля из БД (Сервер/Клиент/…) — пока
        не используется (конфликт контекстов определяется только по
        &-директивам метода);
    :param extra_scope: имена, объявленные вне кода модуля — реквизиты и
        табличные части объекта (в модулях объектов они доступны напрямую,
        без префикса); добавляются в область видимости каждого метода.
    """
    result = syntax_analyze(text)
    diags = []
    diags += _unused_locals(result)
    diags += _break_continue_outside_loop(result)
    diags += _goto_usage(result)
    diags += _name_based_checks(result)
    if ctx is not None:
        diags += _db_checks(result, text, ctx, tuple(extra_ctxs), self_module,
                            is_form_module, frozenset(extra_scope))
        diags += _query_checks(text, ctx)
    diags.sort(key=lambda d: (d.line, d.col))
    return diags


# ---------------------------------------------------------------------------
# Ярус 1 — структурные проверки
# ---------------------------------------------------------------------------

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


def _name_based_checks(result):
    """Ярус 3-лайт: security/deprecated по именам глобальных вызовов и типов."""
    diags = []
    local_methods = {m['name'].lower() for m in result.methods}
    for e in result.events:
        if e[0] == 'chain':
            parts, called, line = e[1], e[2], e[3]
            if called and len(parts) == 1 and parts[0] is not None:
                low = parts[0].lower()
                if low in local_methods:
                    continue
                hit = GLOBAL_CALL_CHECKS.get(low)
                if hit:
                    code, sev, msg = hit
                    diags.append(Diagnostic(code, msg, line, 1, severity=sev))
        elif e[0] == 'new' and e[1]:
            hit = NEW_TYPE_CHECKS.get(e[1].lower())
            if hit:
                code, sev, msg = hit
                diags.append(Diagnostic(code, msg, e[2], 1, severity=sev))
        elif e[0] == 'execute':
            diags.append(Diagnostic(
                'ExecuteExternalCode',
                '«Выполнить» выполняет произвольный код — проверьте '
                'безопасность входных данных', e[1], 1, severity='WARNING'))
    return diags


# ---------------------------------------------------------------------------
# Ярус 2 — проверки по базе
# ---------------------------------------------------------------------------

def _all_ctxs(ctx, extra_ctxs):
    return (ctx,) + tuple(extra_ctxs)


def _find_common_module(name, ctxs):
    """(инфо общего модуля, владеющий ctx) по всем открытым базам."""
    low = name.lower()
    for c in ctxs:
        info = c.common.get(low)
        if info is not None:
            return info, c
    return None, None


def _common_method_info(module_name, method_name, ctxs):
    """(is_export, kind, signature) или None по всем базам."""
    low_m, low_n = module_name.lower(), method_name.lower()
    for c in ctxs:
        found = c.common_methods.get((low_m, low_n))
        if found is not None:
            return found
    return None


def _object_exists(name, type_stem, ctxs):
    for c in ctxs:
        if c.objects.get((type_stem, name.lower())):
            return True
    return False


def _async_fallback(ctx, module_name, method_name):
    """Для баз, собранных до поддержки «Асинх» в bsl_parser: асинх-метод
    отсутствует в таблице method, но его объявление осталось в module.body.
    Возвращает 'found' / 'found_export' / None."""
    conn = getattr(ctx, 'conn', None)
    if conn is None:
        return None
    row = conn.execute(
        'SELECT m.body FROM module m JOIN meta_object o ON o.id=m.object_id '
        "WHERE o.type='CommonModule' AND LOWER(o.name)=LOWER(?)",
        (module_name,)).fetchone()
    if not row or not row[0]:
        return None
    body = row[0]
    rx = re.compile(
        r'(?:Асинх|Async)[ \t]+(?:Процедура|Функция)[ \t]+%s[ \t]*\('
        % re.escape(method_name), re.IGNORECASE)
    m = rx.search(body)
    if m is None:
        return None
    # экспорт: ищем ')' с балансом скобок, затем слово Экспорт
    i = body.find('(', m.end() - 1)
    depth = 0
    j = i
    while j < len(body):
        if body[j] == '(':
            depth += 1
        elif body[j] == ')':
            depth -= 1
            if depth == 0:
                break
        j += 1
    tail = body[j + 1:j + 40]
    if re.match(r'\s*Экспорт\b', tail, re.IGNORECASE):
        return 'found_export'
    return 'found'


def _caller_context_of(m, module_context):
    """Контекст вызывающего метода — ТОЛЬКО по &-директивам.

    Откат по контексту модуля (module_context) намеренно не используется:
    в режиме обычного приложения (толстый клиент) клиентский код легально
    вызывает серверные общие модули напрямую — на корпусе УНФ это дало бы
    1675 ложных WARNING. Чистый &НаКлиенте — управляемая форма, там прямой
    вызов серверного модуля действительно ошибка.
    """
    from .bsl_analyzer import caller_context
    try:
        return caller_context(', '.join(m['directives']), None)
    except Exception:  # noqa: BLE001
        return None


def _db_checks(result, text, ctx, extra_ctxs, self_module, is_form_module,
               extra_scope):
    from .bsl_analyzer import BSL_MANAGERS, context_conflict
    ctxs = _all_ctxs(ctx, extra_ctxs)
    diags = []
    seen = set()          # дедуп одинаковых находок (повторные вызовы)
    module_method_names = {m['name'].lower() for m in result.methods}
    module_var_names = {v['name'].lower() for v in result.module_vars}
    extra_low = {str(x).lower() for x in extra_scope}

    for m in result.methods:
        lo, hi = m['line_start'], m['line_end']
        scope = set(module_var_names)
        scope |= module_method_names
        scope |= extra_low
        scope |= {p['name'].lower() for p in m['params']}
        scope |= {x.lower() for x in (m['locals'] or [])}
        for e in result.events:
            if e[0] == 'assign' and lo <= e[-1] <= hi:
                for p in e[1]:
                    if p:
                        scope.add(p.lower())
        caller_ctx = _caller_context_of(m, None)

        for e in result.events:
            if e[0] != 'chain' or not (lo <= e[-1] <= hi):
                continue
            parts, called, line = e[1], e[2], e[3]
            if not parts or parts[0] is None:
                continue
            if any(p is None for p in parts):
                continue    # динамический сегмент [индекс] — состав неизвестен
            head = parts[0]
            low = head.lower()
            if low in scope:
                continue        # переменная/локальный метод — не в базу
            if low in BSL_MANAGERS and len(parts) >= 2 and parts[1]:
                obj = parts[1]
                if obj.lower() in MANAGER_METHODS:
                    continue
                key = ('meta', low, obj.lower())
                if key in seen:
                    continue
                seen.add(key)
                if not _object_exists(obj, BSL_MANAGERS[low], ctxs):
                    sev = 'WARNING' if is_form_module else 'ERROR'
                    diags.append(Diagnostic(
                        'MetadataObjectMissing',
                        'объект метаданных «%s.%s» не найден в этой базе%s'
                        % (head, obj,
                           '' if extra_ctxs else ' (и в открытых рядом)'),
                        line, 1, severity=sev))
                continue
            info, owner = _find_common_module(head, ctxs)
            if info is None:
                continue        # неизвестное имя — граница анализа, молчим
            if len(parts) == 2 and called and parts[1]:
                meth = parts[1]
                key = ('cm', low, meth.lower())
                if key in seen:
                    continue
                seen.add(key)
                found = _common_method_info(info['name'], meth, ctxs)
                if found is None:
                    fb = _async_fallback(owner, info['name'], meth)
                    if fb is None:
                        sev = 'WARNING' if is_form_module else 'ERROR'
                        diags.append(Diagnostic(
                            'MissingCommonModuleMethod',
                            'общий модуль «%s» не содержит метод «%s»'
                            % (info['name'], meth), line, 1, severity=sev))
                elif not found[0]:
                    if self_module is None or \
                            self_module.lower() != info['name'].lower():
                        diags.append(Diagnostic(
                            'NonExportCommonModuleMethod',
                            'метод «%s» общего модуля «%s» не Экспорт — вызов '
                            'извне не работает' % (meth, info['name']),
                            line, 1, severity='WARNING'))
                # клиент-серверный контекст
                if caller_ctx:
                    conflict = context_conflict(caller_ctx, info['context'])
                    if conflict:
                        ckey = ('ctx', low, meth.lower())
                        if ckey not in seen:
                            seen.add(ckey)
                            diags.append(Diagnostic(
                                'ClientServerCallConflict',
                                '%s.%s: %s' % (info['name'], meth, conflict),
                                line, 1, severity='WARNING'))
    return diags


def _query_checks(text, ctx):
    """Запросы внутри модуля: синтаксис + таблицы/поля по метаданным базы."""
    from .bsl_analyzer import extract_queries
    from .query_lang import check_query_full
    diags = []
    for line, query, complete in extract_queries(text):
        if not complete:
            continue
        if '%' in query or '[' in query or '#' in query:
            continue    # шаблон с подстановками (%1, #Имя) / экзотика ([])
        try:
            errors, _unverified = check_query_full(query, ctx.meta)
        except Exception:  # noqa: BLE001
            continue
        for err in errors:
            if "найдено ''" in err or "Неожиданный токен ''" in err:
                continue    # текст обрывается — запрос собирали по частям
            if err.startswith('синтаксис'):
                code = 'QueryParseError'
            elif err.startswith('неизвестная таблица'):
                code = 'QueryToMissingMetadata'
            else:
                code = 'ConfdbQueryValidation'
            diags.append(Diagnostic(
                code, 'запрос в модуле (строка %d): %s' % (line, err),
                line, 1, severity='ERROR'))
    return diags
