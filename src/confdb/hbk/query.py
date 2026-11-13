"""Выборка из базы синтакс-помощника (syntax.db): поиск, карточки, дерево.

Нечёткий поиск сливает три метода с приоритетами: FTS5-префиксы
(«слово»*, OR между словами, ранжирование bm25) -> подстрока в
name_ru/name_en/syntax -> расстояние Левенштейна по именам (для опечаток;
слова короче 3 букв не учитываются, дистанция 1-2). Ищет по RU и EN именам.

Функции format_* возвращают ТЕКСТ: его печатает CLI (confdb hbk
search/show/tree) и отдают инструменты MCP-сервера (syntax_search,
syntax_card, syntax_children). База открывается только для чтения.

В строке результата после имени выводится родитель темы
(«Колонки (Columns) — ТабличноеПоле») — это снимает неоднозначность
одинаковых членов разных типов. Пометка «+» у вида означает, что у темы
есть потомки. В текстах (описания, параметры, примеры) и в ссылках стоят
отметки «(id=N)» — id тем этой же базы для перехода (syntax_card).
"""
import io
import re
import sqlite3

# Колонки доступности -> подписи платформ (порядок как в build.py).
AV_COLUMNS = [
    ('av_server', 'сервер'),
    ('av_thick_client', 'толстый клиент'),
    ('av_thin_client', 'тонкий клиент'),
    ('av_web_client', 'веб-клиент'),
    ('av_external_connection', 'внешнее соединение'),
    ('av_mobile_client', 'мобильный клиент'),
    ('av_mobile_app_client', 'мобильное приложение (клиент)'),
    ('av_mobile_app_server', 'мобильное приложение (сервер)'),
    ('av_mobile_autonomous_server', 'мобильный автономный сервер'),
    ('av_integration', 'интеграция'),
]

HELP = """Команды (можно краткие формы):
Пометка «+» у вида в списках — у темы есть потомки (откройте её, чтобы
посмотреть их).

  поиск <текст>        нечёткий поиск тем (также: s, search)
  открыть <id|N>       карточка темы по id или номеру из последнего списка
                       (также: o, open)
  дети                 дети текущей темы; вверху — два корня (d, children)
  вверх                к родителю (u, up)
  показать             карточка текущей темы (p, show)
  путь                 цепочка от корня до текущей темы (path)
  дерево <id> [глуб.]  поддерево темы (t, tree)
  помощь               эта справка (h, ?, help)
  выход                выход (q, exit)
Ввод номера без команды открывает соответствующий элемент последнего списка."""


def open_syntax_db(path):
    """Открыть базу синтакс-помощника read-only.

    Проверяет наличие таблицы topics; ValueError — это не база
    синтакс-помощника (или файл не найден: sqlite3.OperationalError).
    """
    # check_same_thread=False: соединение открывает CLI в своём потоке, а
    # MCP-сервер в HTTP-режиме — из потоков-обработчиков (вызовы всё равно
    # сериализует RLock сервера)
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('SELECT COUNT(*) FROM topics').fetchone()
    except sqlite3.Error:
        conn.close()
        raise ValueError(
            f'это не база синтакс-помощника (нет таблицы topics): {path}')
    return conn


# ------------------------------------------------------------------- утилиты

def levenshtein(a, b):
    """Расстояние Левенштейна, две строки в нижнем регистре."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _tokens(query):
    """Слова запроса: буквы/цифры/дефис, Unicode (включая кириллицу)."""
    return re.findall(r'[\w-]+', query, flags=re.UNICODE)


# --------------------------------------------------------------------- поиск

def fts_ids(conn, tokens, cap):
    """FTS5: префиксное совпадение по всем полям, OR между словами."""
    if not tokens:
        return []
    match = ' OR '.join(f'"{t}"*' for t in tokens)
    try:
        rows = conn.execute(
            'SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY rank LIMIT ?',
            (match, cap)).fetchall()
        return [r[0] for r in rows]
    except sqlite3.OperationalError:
        return []


def substring_ids(conn, token, cap):
    """Подстрока в RU/EN именах и синтаксисе (без учёта регистра)."""
    like = f'%{token.lower()}%'
    rows = conn.execute(
        'SELECT id FROM topics WHERE lower(name_ru) LIKE ?'
        ' OR lower(name_en) LIKE ? OR lower(syntax) LIKE ? LIMIT ?',
        (like, like, like, cap)).fetchall()
    return [r[0] for r in rows]


def fuzzy_ids(tokens, name_cache, max_dist=2):
    """Опечатки: Левенштейн между словами запроса и именами тем.
    name_cache — список (id, name_ru, name_en)."""
    scored = {}
    for tok in tokens:
        tl = tok.lower()
        if len(tl) < 3:  # короткие слова дадут слишком много шума
            continue
        dist = 1 if len(tl) <= 5 else max_dist
        for tid, nru, nen in name_cache:
            for nm in (nru, nen):
                if not nm:
                    continue
                nml = nm.lower()
                if abs(len(nml) - len(tl)) > dist:
                    continue
                d = levenshtein(tl, nml)
                if d <= dist and (tid not in scored or d < scored[tid]):
                    scored[tid] = d
    return scored


def exact_ids(tokens, name_cache):
    """Точное совпадение имени темы с запросом (RU/EN, без регистра).

    Запрос «ДвоичныеДанные» обязан выдавать одноимённый тип первым, а не
    темы, где он упомянут в описании (отзыв LLM 2026-11-13).
    """
    if not tokens:
        return []
    whole = ' '.join(tokens).lower()
    joined = ''.join(tokens).lower()
    out = []
    for tid, nru, nen in name_cache:
        for nm in (nru, nen):
            if nm and nm.lower() in (whole, joined):
                out.append(tid)
                break
    return out


def type_method_ids(conn, tokens, name_cache):
    """«Тип Метод» (ТабличныйДокумент Прочитать): имя темы == последний
    токен, кто-то из предков (до 4 уровней) == первый токен/склейка остальных.

    Имена методов в справке повторяются у разных типов, поэтому запрос из
    двух слов разрешается в конкретный метод конкретного типа. Регистр
    сворачивается питоним lower: sqlite lower() кириллицу не знает.
    """
    if len(tokens) < 2:
        return []
    method = tokens[-1].lower()
    type_variants = {tokens[0].lower(), ''.join(tokens[:-1]).lower()}
    cand = [tid for tid, nru, nen in name_cache
            if (nru and nru.lower() == method)
            or (nen and nen.lower() == method)]
    if not cand:
        return []
    cache = {}

    def topic(tid):
        if tid not in cache:
            cache[tid] = conn.execute(
                'SELECT name_ru, name_en, parent_id FROM topics WHERE id=?',
                (tid,)).fetchone()
        return cache[tid]

    out = []
    for tid in cand:
        row = topic(tid)
        cur = row[2] if row else None
        depth, matched = 0, False
        while cur is not None and depth < 4 and not matched:
            r = topic(cur)
            if r is None:
                break
            if any(nm and nm.lower() in type_variants
                   for nm in (r[0], r[1])):
                matched = True
            cur = r[2]
            depth += 1
        if matched:
            out.append(tid)
    return out


def search(conn, query, limit, name_cache):
    """Слить результаты методов, вернуть [(id, оценка, источник)].

    Тиры: точное имя (200) > «тип+метод» (190) > FTS (100-pos) >
    подстрока (70-pos) > нечёткий (40-d*10). Точные совпадения всегда сверху.
    """
    tokens = _tokens(query)
    if not tokens:
        return []
    scored = {}  # id -> (score, source)
    cap = limit * 5

    for tid in exact_ids(tokens, name_cache):
        scored[tid] = (200, 'точное имя')
    for tid in type_method_ids(conn, tokens, name_cache):
        if tid not in scored or scored[tid][0] < 190:
            scored[tid] = (190, 'тип+метод')
    for pos, tid in enumerate(fts_ids(conn, tokens, cap)):
        s = 100 - min(pos, 60)
        if tid not in scored or s > scored[tid][0]:
            scored[tid] = (s, 'fts')
    for tok in tokens:
        for pos, tid in enumerate(substring_ids(conn, tok, cap)):
            s = 70 - min(pos, 40)
            if tid not in scored or s > scored[tid][0]:
                scored[tid] = (s, 'подстрока')
    for tid, d in fuzzy_ids(tokens, name_cache).items():
        s = 40 - d * 10
        if tid not in scored or s > scored[tid][0]:
            scored[tid] = (s, f'нечёткий(d={d})')

    ordered = sorted(scored.items(), key=lambda kv: (-kv[1][0], kv[0]))
    return [(tid, sc, src) for tid, (sc, src) in ordered[:limit]]


def load_name_cache(conn):
    """Кэш имён для нечёткого поиска: [(id, name_ru, name_en)]."""
    return conn.execute(
        'SELECT id, name_ru, name_en FROM topics'
        ' WHERE name_ru IS NOT NULL OR name_en IS NOT NULL').fetchall()


def fetch_brief(conn, ids):
    """Краткие строки для списка id (для печати результатов)."""
    if not ids:
        return []
    ph = ','.join('?' * len(ids))
    rows = conn.execute(
        'SELECT t.id, t.kind, t.name_ru, t.name_en, p.name_ru AS parent_name'
        ' FROM topics t LEFT JOIN topics p ON t.parent_id=p.id'
        f' WHERE t.id IN ({ph})', ids).fetchall()
    by_id = {r['id']: r for r in rows}
    return [by_id[i] for i in ids if i in by_id]


# --------------------------------------------------------------- карточка темы

def availability_of(row):
    """Список подписей платформ, у которых флаг = 1."""
    return [label for col, label in AV_COLUMNS if row[col]]


def breadcrumb(conn, tid):
    """Цепочка имен от корня до темы."""
    chain, cur = [], tid
    while cur is not None:
        r = conn.execute(
            'SELECT name_ru, parent_id FROM topics WHERE id=?', (cur,)).fetchone()
        if r is None:
            break
        chain.append(r[0] or f'#{cur}')
        cur = r[1]
    return list(reversed(chain))


def topic_full(conn, tid):
    """Полный структурированный ответ по теме (словарь или None)."""
    row = conn.execute('SELECT * FROM topics WHERE id=?', (tid,)).fetchone()
    if row is None:
        return None
    t = dict(row)
    t['availability'] = availability_of(row)

    params = []
    for p in conn.execute(
        'SELECT pos, name, optional, type_text, type_id, description'
        ' FROM params WHERE topic_id=? ORDER BY pos', (tid,)):
        d = dict(p)
        d['type_name'] = None
        if d['type_id'] is not None:
            r = conn.execute(
                'SELECT name_ru FROM topics WHERE id=?',
                (d['type_id'],)).fetchone()
            d['type_name'] = r[0] if r else None
        params.append(d)
    t['params'] = params

    links = {'see': [], 'type': [], 'body': []}
    for l in conn.execute(
        'SELECT dest_id, href, kind FROM links WHERE src_id=?', (tid,)):
        name = None
        if l['dest_id'] is not None:
            r = conn.execute(
                'SELECT name_ru FROM topics WHERE id=?', (l['dest_id'],)).fetchone()
            name = r[0] if r else None
        bucket = l['kind'] if l['kind'] in links else 'body'
        links[bucket].append({'name': name, 'id': l['dest_id'],
                              'href': l['href']})
    t['links'] = links

    t['breadcrumb'] = breadcrumb(conn, tid)
    return t


def children_of(conn, tid):
    """Дети узла (или корни, если tid=None), по sort затем имени."""
    base = ('SELECT t.id, t.kind, t.name_ru, t.name_en,'
            ' p.name_ru AS parent_name'
            ' FROM topics t LEFT JOIN topics p ON t.parent_id=p.id')
    if tid is None:
        q = base + " WHERE t.kind='root'"
        args = ()
    else:
        q = base + ' WHERE t.parent_id=?'
        args = (tid,)
    return conn.execute(q + ' ORDER BY t.sort, t.name_ru', args).fetchall()


def parents_with_children(conn):
    """Множество id тем, у которых есть потомки."""
    return {r[0] for r in conn.execute(
        'SELECT DISTINCT parent_id FROM topics WHERE parent_id IS NOT NULL')}


# ------------------------------------------------------------------ печать

def format_topic(t):
    """Человекочитаемая карточка темы — текст (без завершающего \\n)."""
    buf = io.StringIO()
    p = lambda *a: print(*a, file=buf)  # noqa: E731
    p(f"=== {t['name_ru'] or t['title'] or '#' + str(t['id'])}"
      + (f" ({t['name_en']})" if t['name_en'] else '') + ' ===')
    p(f"id={t['id']}  вид={t['kind']}"
      + (f"  с версии: {t['since']}" if t['since'] else ''))
    p(f"путь: {' > '.join(t['breadcrumb'])}")
    if t['availability']:
        p('доступность: ' + ', '.join(t['availability']))
    if t['availability_note']:
        p(f"примечание: {t['availability_note']}")
    if t['syntax']:
        p(f"\nСинтаксис:\n{t['syntax']}")
    if t['signature_ru']:
        p(f"\nСигнатура RU: {t['signature_ru']}")
    if t['signature_en']:
        p(f"Сигнатура EN: {t['signature_en']}")
    if t['returns']:
        p(f"\nВозвращаемое значение:\n{t['returns']}")
    if t['params']:
        p('\nПараметры:')
        for prm in t['params']:
            if prm['name'] is None:
                p(f"  (неразобранная секция) {prm['description']}")
                continue
            opt = ' [необязательный]' if prm['optional'] else ''
            ty = prm['type_name'] or prm['type_text'] or ''
            p(f"  {prm['name']}{opt}" + (f': {ty}' if ty else ''))
            if prm['description']:
                p(f"      {prm['description']}")
    if t['description']:
        p(f"\nОписание:\n{t['description']}")
    if t['example']:
        p(f"\nПример:\n{t['example']}")
    for kind, title in (('see', 'См. также'), ('type', 'Типы'), ('body', 'Ссылки')):
        items = []
        for l in t['links'][kind]:
            if l['name'] and l['id'] is not None:
                items.append(f"{l['name']} (id={l['id']})")
            else:
                items.append(l['name'] or l['href'])
        if items:
            uniq = list(dict.fromkeys(items))
            p(f"\n{title}: {', '.join(uniq)}")
    return buf.getvalue().rstrip('\n')


def format_results(rows, parents, show_parent=True):
    """Нумерованный список результатов поиска/детей — текст.

    «+» у вида — у темы есть потомки, её можно открыть и посмотреть;
    после имени — родитель (для однозначности одинаковых членов разных типов).
    """
    buf = io.StringIO()
    has_kids = False
    for i, r in enumerate(rows, 1):
        name = r['name_ru'] or r['name_en'] or f"#{r['id']}"
        en = f" ({r['name_en']})" if r['name_ru'] and r['name_en'] else ''
        par = ''
        if show_parent and r['parent_name']:
            par = f" — {r['parent_name']}"
        kid = r['id'] in parents
        has_kids |= kid
        kind = r['kind'] + '+' if kid else r['kind']
        print(f"  [{i}] {kind:<10} {name}{en}{par}  id={r['id']}", file=buf)
    if has_kids:
        print('(«+» — есть потомки; откройте тему, чтобы посмотреть их)',
              file=buf)
    return buf.getvalue().rstrip('\n')


def format_tree(conn, tid, depth, limit):
    """ASCII-поддерево с ограничением глубины и общего числа узлов — текст."""
    buf = io.StringIO()

    def walk(pid, prefix, depth_left, seen):
        if depth_left < 0:
            return
        kids = children_of(conn, pid)
        for i, k in enumerate(kids):
            seen[0] += 1
            if seen[0] > limit:
                print(prefix + '...(обрезано)', file=buf)
                return
            last = i == len(kids) - 1
            tee = '`-- ' if last else '|-- '
            en = f" ({k['name_en']})" if k['name_ru'] and k['name_en'] else ''
            print(f"{prefix}{tee}#{k['id']} [{k['kind']}] "
                  f"{k['name_ru'] or k['name_en']}{en}", file=buf)
            walk(k['id'], prefix + ('    ' if last else '|   '),
                 depth_left - 1, seen)

    r = conn.execute(
        'SELECT id, kind, name_ru, name_en FROM topics WHERE id=?',
        (tid,)).fetchone()
    if r is None:
        return f'тема id={tid} не найдена'
    en = f" ({r['name_en']})" if r['name_ru'] and r['name_en'] else ''
    print(f"#{r['id']} [{r['kind']}] {r['name_ru'] or r['name_en']}{en}",
          file=buf)
    walk(tid, '', depth - 1, [0])
    return buf.getvalue().rstrip('\n')


# ------------------------------------------------------------------- REPL

def _pick_id(arg, last_ids, out=print):
    """id темы напрямую либо номер в последнем списке."""
    if not arg.isdigit():
        out('Нужен числовой id или номер из последнего списка.')
        return None
    n = int(arg)
    # малые числа трактуем как номер в списке, если список есть
    if last_ids and 1 <= n <= len(last_ids):
        return last_ids[n - 1]
    return n


def repl(conn, limit=20, out=print, inp=input):
    """Интерактивный обход дерева тем (курсор, поиск, карточки)."""
    name_cache = load_name_cache(conn)
    parents = parents_with_children(conn)
    current = None     # id текущей темы в обходе
    last_ids = []      # id из последнего списка (поиск/дети)
    out('Синтакс-помощник 1С. Введите «помощь» для списка команд.')
    while True:
        try:
            raw = inp('1С> ')
        except (EOFError, KeyboardInterrupt):
            out('\nВыход.')
            break
        line = raw.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ''

        if cmd in ('выход', 'q', 'exit', 'quit'):
            break
        elif cmd in ('помощь', 'h', '?', 'help'):
            out(HELP)
        elif cmd in ('поиск', 's', 'search'):
            if not arg:
                out('Укажите текст: поиск <запрос>')
                continue
            res = search(conn, arg, limit, name_cache)
            last_ids = [tid for tid, _, _ in res]
            rows = fetch_brief(conn, last_ids)
            out(f'Найдено {len(rows)} по запросу «{arg}»:')
            out(format_results(rows, parents))
        elif cmd in ('открыть', 'o', 'open'):
            tid = _pick_id(arg, last_ids, out)
            if tid is not None:
                t = topic_full(conn, tid)
                if t is None:
                    out(f'Тема id={tid} не найдена.')
                else:
                    out(format_topic(t))
                    current = tid
        elif cmd in ('дети', 'd', 'children'):
            kids = children_of(conn, current)
            if current is None:
                out('Корни дерева:')
            else:
                name = conn.execute(
                    'SELECT name_ru FROM topics WHERE id=?',
                    (current,)).fetchone()[0]
                out(f'Дети «{name}» ({len(kids)}):')
            out(format_results(kids, parents, show_parent=False))
            last_ids = [k['id'] for k in kids]
        elif cmd in ('вверх', 'u', 'up'):
            if current is None:
                out('Вы и так наверху.')
                continue
            pid = conn.execute(
                'SELECT parent_id FROM topics WHERE id=?', (current,)).fetchone()[0]
            if pid is None:
                out('Это корень, выше некуда.')
                current = None
                continue
            name = conn.execute(
                'SELECT name_ru FROM topics WHERE id=?', (pid,)).fetchone()[0]
            out(f'Вверх -> «{name}» (id={pid}). «дети» для списка.')
            current = pid
        elif cmd in ('показать', 'p', 'show'):
            if current is None:
                out('Нет текущей темы. Сначала «дети» или «открыть <id».')
            else:
                out(format_topic(topic_full(conn, current)))
        elif cmd in ('путь', 'path'):
            if current is None:
                out('Нет текущей темы.')
            else:
                out(' > '.join(breadcrumb(conn, current)))
        elif cmd in ('дерево', 't', 'tree'):
            tparts = arg.split()
            if not tparts or not tparts[0].isdigit():
                out('Укажите id: дерево <id> [глубина]')
                continue
            tdepth = int(tparts[1]) if len(tparts) > 1 and tparts[1].isdigit() else 2
            out(format_tree(conn, int(tparts[0]), tdepth, limit))
        elif line.isdigit():
            tid = _pick_id(line, last_ids, out)
            if tid is not None:
                t = topic_full(conn, tid)
                if t is None:
                    out(f'Тема id={tid} не найдена.')
                else:
                    out(format_topic(t))
                    current = tid
        else:
            out(f'Неизвестная команда: {cmd}. Введите «помощь».')
