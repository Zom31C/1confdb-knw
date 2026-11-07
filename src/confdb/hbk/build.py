"""Сборка базы синтакс-помощника 1С (syntax.db) из распакованного дерева .hbk.

Вход: дерево, извлечённое `hbk.unpack` (каталоги objects/ и tables/):
  * <тема>.html      — страница справки (узлы и листья);
  * <тема>.st        — краткая сигнатура листа в формате 1С (RU и EN);
  * __categories__   — доступность файлов по режимам 1С + порядок в каталоге;
  * каталоги methods/properties/events/ctors/formparams/fields/params —
    группы членов типа; внутри них могут быть подкатегории catalogN.

Выход: один файл SQLite:
  * topics  — все темы: дерево (parent_id), вид (kind), имена RU/EN,
              синтаксис, сигнатуры .st, возвращаемое значение, описание,
              пример, доступность по платформам (булевы колонки av_*)
              и примечание к ней (availability_note), версия, режимы 1С,
              порядок; в текстах на месте ссылок оригинала, чья цель есть
              в базе, стоят отметки «(id=N)»;
  * params  — параметры методов/конструкторов (имя, обязательность,
              тип со ссылкой на тему типа, описание);
  * links   — перекрёстные ссылки (see / type / body), в т.ч. битые;
  * fts     — полнотекстовый поиск (FTS5) по именам/синтаксису/описанию;
  * meta    — служебная информация о сборке.

HTML разбирается регулярными выражениями (разметка на CSS-классах V8SH_*),
кодировка страниц — UTF-8 с BOM. Только стандартная библиотека.
"""

import html as html_mod
import json
import os
import re
import sqlite3
from datetime import datetime, timezone

GROUP_DIRS = {'methods', 'properties', 'events', 'ctors',
              'formparams', 'fields', 'params'}
V8HELP_PREFIX = 'v8help://SyntaxHelperContext/'
SKIP_CHAPTERS = {'Свойства:', 'Методы:', 'События:', 'Значения',
                 'Элементы коллекции:', 'Конструкторы:', 'Поля'}
AV_PLATFORMS = [  # (колонка в topics, имя платформы в секции «Доступность»)
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
AV_NAME_TO_COL = {name: col for col, name in AV_PLATFORMS}

RE_PAGETITLE = re.compile(r'<h1 class="V8SH_pagetitle">(.*?)</h1>', re.S)
RE_TITLE = re.compile(r'<p class="V8SH_title">(.*?)</p>', re.S)
RE_HEADING = re.compile(r'<p class="V8SH_heading">(.*?)</p>', re.S)
RE_SINCE_DIV = re.compile(
    r'<div class="__SINCE_SHOW_STYLE__">.*?<p class="not_used">(.*?)</p>.*?</div>',
    re.S)
RE_CHAPTER = re.compile(r'<p class="V8SH_chapter">(.*?)</p>', re.S)
RE_ANCHOR = re.compile(r'<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
RE_INLINE_ID = re.compile(r'\(id=(\d+)\)')
RE_RUBRIC = re.compile(r'<div class="V8SH_rubric">(.*?)</div>', re.S)
RE_FILENUM = re.compile(r'^(.*?)(\d+)$')
RE_RUEN = re.compile(r'^(.*?)\s*\(([^()]*)\)\s*$', re.S)
RE_VERSION = re.compile(r'\d+\.\d+(?:\.\d+)?')


# ---------------------------------------------------------------- 1С-литералы

def parse_1c(text):
    """Разбор литерала формата 1С: {1,{2,{"ru",0,0,"","текст"}}} и т.п."""
    pos = 0
    n = len(text)

    def skip_ws():
        nonlocal pos
        while pos < n and text[pos] in ' \t\r\n':
            pos += 1

    def value():
        skip_ws()
        if pos >= n:
            raise ValueError('неожиданный конец литерала')
        c = text[pos]
        if c == '{':
            return braces()
        if c == '"':
            return string()
        return number()

    def braces():
        nonlocal pos
        pos += 1  # {
        items = []
        skip_ws()
        if pos < n and text[pos] == '}':
            pos += 1
            return items
        while True:
            items.append(value())
            skip_ws()
            if pos >= n:
                raise ValueError('нет закрывающей }')
            if text[pos] == ',':
                pos += 1
                continue
            if text[pos] == '}':
                pos += 1
                return items
            raise ValueError(f'ожидалась , или }} в позиции {pos}')

    def string():
        nonlocal pos
        pos += 1  # "
        out = []
        while pos < n:
            c = text[pos]
            if c == '"':
                if pos + 1 < n and text[pos + 1] == '"':
                    out.append('"')
                    pos += 2
                    continue
                pos += 1
                return ''.join(out)
            out.append(c)
            pos += 1
        raise ValueError('незакрытая строка')

    def number():
        nonlocal pos
        start = pos
        if pos < n and text[pos] in '+-':
            pos += 1
        while pos < n and (text[pos].isdigit() or text[pos] == '.'):
            pos += 1
        if start == pos:
            raise ValueError(f'не число в позиции {pos}')
        s = text[start:pos]
        return float(s) if '.' in s else int(s)

    v = value()
    skip_ws()
    if pos != n:
        raise ValueError(f'лишние данные после литерала: {text[pos:pos + 20]!r}')
    return v


def collect_st_signatures(node, found):
    """Из .st-дерева собрать сигнатуры: {'ru': ..., 'en': ...}."""
    if not isinstance(node, list):
        return
    if len(node) == 5 and node[0] in ('ru', 'en') and isinstance(node[4], str):
        found[node[0]] = node[4]
    for item in node:
        collect_st_signatures(item, found)


def parse_categories(text):
    """__categories__ -> {имя_файла: {'modes': [...], 'version': str}, ...}
    и список имён в порядке перечисления."""
    try:
        root = parse_1c(text)
    except ValueError:
        return {}, []
    order, info = [], {}
    i = 1  # первый элемент — счётчик
    while i < len(root):
        name = root[i]
        if not isinstance(name, str):
            i += 1
            continue
        modes, version = [], ''
        j = i + 1
        if j < len(root) and isinstance(root[j], (int, float)):
            j += 1
        if j < len(root) and isinstance(root[j], list):
            for x in root[j]:
                if isinstance(x, str):
                    if RE_VERSION.fullmatch(x):
                        version = x
                    elif x:
                        modes.append(x)
            i = j + 1
        else:
            i += 1
        order.append(name)
        info[name] = {'modes': modes, 'version': version}
    return info, order


# ------------------------------------------------------------------ HTML-текст

def clean_text(s):
    """HTML-фрагмент -> чистый текст с сохранением переносов строк."""
    s = re.sub(r'(?i)<br\s*/?>', '\n', s)
    s = re.sub(r'(?i)</(p|div|tr|table|li|h[1-6])>', '\n', s)
    s = re.sub(r'<[^>]+>', '', s)
    s = html_mod.unescape(s)
    s = s.replace('\xa0', ' ')
    lines = [re.sub(r'[ \t]+', ' ', ln).strip() for ln in s.split('\n')]
    return '\n'.join(ln for ln in lines if ln).strip()


def cut_tail(s):
    """Отрезать хвост страницы: <HR> и блок «Методическая информация»."""
    m = re.search(r'(?i)<HR', s)
    return s[:m.start()] if m else s


def split_name(s):
    """«НачатьТранзакцию (BeginTransaction)» -> (ru, en)."""
    s = clean_text(s)
    m = RE_RUEN.match(s)
    if m and m.group(2):
        return m.group(1).strip(), m.group(2).strip()
    return s, None


def parse_availability(text):
    """Секция «Доступность» -> (флаги платформ, примечание, неизвестные токены).
    Первая строка — список платформ через запятую («Сервер, толстый клиент,
    внешнее соединение.»); остальные строки — примечание (сериализуемость,
    обращения к серверу и т.п.)."""
    lines = [ln.strip() for ln in text.split('\n') if ln.strip()]
    flags = {col: 0 for col, _ in AV_PLATFORMS}
    unknown = []
    for tok in lines[0].rstrip('.').split(','):
        tok = tok.strip().rstrip('.').lower()
        if not tok:
            continue
        col = AV_NAME_TO_COL.get(tok)
        if col:
            flags[col] = 1
        else:
            unknown.append(tok)
    note = '\n'.join(lines[1:]) if len(lines) > 1 else None
    return flags, note, unknown


def parse_html_page(raw, resolve):
    """Разбор страницы. Возвращает словарь полей темы и список ссылок
    [(href, kind), ...].
    resolve: href -> id темы; якоря, у которых цель нашлась в базе,
    заменяются в тексте на «текст (id=N)»."""
    body_m = re.search(r'(?i)<body>(.*)</body>', raw, re.S)
    body = body_m.group(1) if body_m else raw
    body = cut_tail(body)

    res = {'title': None, 'name_ru': None, 'name_en': None, 'since': None,
           'syntax': None, 'returns': None, 'availability': None,
           'example': None, 'chapters_extra': []}
    links = []

    m = RE_PAGETITLE.search(body)
    if m:
        res['title'] = clean_text(m.group(1))
    m = RE_HEADING.search(body) or RE_TITLE.search(body) or RE_PAGETITLE.search(body)
    if m:
        res['name_ru'], res['name_en'] = split_name(m.group(1))
    m = RE_SINCE_DIV.search(body)
    if m:
        res['since'] = clean_text(m.group(1))

    # разметка на секции по V8SH_chapter
    marks = list(RE_CHAPTER.finditer(body))
    spans = []  # (название, контент)
    for k, mm in enumerate(marks):
        end = marks[k + 1].start() if k + 1 < len(marks) else len(body)
        spans.append((clean_text(mm.group(1)).rstrip(':'), body[mm.end():end]))
    preamble = body[:marks[0].start()] if marks else body
    for tag in (RE_PAGETITLE, RE_TITLE, RE_HEADING, RE_SINCE_DIV):
        preamble = tag.sub('', preamble)

    def process_links(content, section_name):
        """Собрать ссылки и заменить якоря на «текст (id=N)»."""
        def kind_of(pos):
            if section_name.startswith('См. также'):
                return 'see'
            before = re.sub(r'<[^>]*>', '', content[max(0, pos - 60):pos])
            return 'type' if before.rstrip().endswith('Тип:') else 'body'

        def repl(m):
            href, text = m.group(1), m.group(2)
            if href.startswith(('http:', 'https:', 'mailto:')):
                return text
            links.append((href, kind_of(m.start())))
            dest = resolve(href)
            if dest is not None and text.strip():
                return f'{text} (id={dest})'
            return text

        return RE_ANCHOR.sub(repl, content)

    preamble = process_links(preamble, '')
    descr_parts = []
    pre = clean_text(preamble)
    if pre:
        descr_parts.append(pre)
    syntax_parts = []
    for name, content in spans:
        content = process_links(content, name)
        if name in SKIP_CHAPTERS:
            continue
        if name == 'Синтаксис':
            syntax_parts.append(clean_text(content))
        elif name.startswith('Вариант синтаксиса'):
            syntax_parts.append(f'[{name}]\n{clean_text(content)}')
        elif name in ('Параметры', 'Параметры формы'):
            res['params_raw'] = content
        elif name == 'Возвращаемое значение':
            res['returns'] = clean_text(content)
        elif name == 'Описание':
            descr_parts.append(clean_text(content))
        elif name == 'Примечание':
            descr_parts.append('Примечание: ' + clean_text(content))
        elif name.startswith('Описание варианта метода'):
            descr_parts.append(clean_text(content))
        elif name == 'Использование':
            descr_parts.append(clean_text(content))
        elif name == 'Доступность':
            res['availability'] = clean_text(content)
        elif name == 'Пример':
            res['example'] = clean_text(content)
        elif name == 'Использование в версии':
            res['since'] = clean_text(content)
        # прочие секции не несут данных для помощника
    if syntax_parts:
        res['syntax'] = '\n\n'.join(p for p in syntax_parts if p)
    if descr_parts:
        res['description'] = '\n\n'.join(descr_parts)
    else:
        res['description'] = None
    return res, links


def parse_params(content):
    """Секция «Параметры» -> (список параметров, fallback-текст).
    Ссылки в content уже заменены на «текст (id=N)» в parse_html_page."""
    rubrics = list(RE_RUBRIC.finditer(content))
    if not rubrics:
        txt = clean_text(content)
        return ([], txt) if txt else ([], None)
    params = []
    for k, rb in enumerate(rubrics):
        nm = re.search(r'<p[^>]*>(.*)', rb.group(1), re.S)
        raw_name = clean_text(nm.group(1)) if nm else ''
        optional = 'необязательный' in raw_name
        pname = re.sub(r'\(.*?\)', '', raw_name).strip().strip('<>')
        tail_end = rubrics[k + 1].start() if k + 1 < len(rubrics) else len(content)
        tail = content[rb.end():tail_end]
        type_text, type_id, descr = None, None, None
        tm = re.search(r'Тип:(.*)', tail, re.S | re.I)
        if tm:
            seg = tm.group(1)
            first_br = re.search(r'(?i)<br\s*/?>', seg)
            type_line = seg[:first_br.start()] if first_br else seg
            im = RE_INLINE_ID.search(type_line)
            type_id = int(im.group(1)) if im else None
            type_text = RE_INLINE_ID.sub('', clean_text(type_line))
            type_text = type_text.strip().rstrip('.') or None
            seg = seg[first_br.end():] if first_br else ''
            descr = clean_text(seg) or None
        else:
            descr = clean_text(tail) or None
        params.append({'name': pname or None, 'optional': int(optional),
                       'type_text': type_text, 'type_id': type_id,
                       'description': descr})
    return params, None


def resolve_href(href, topic_dir, path_to_id):
    """href -> id темы или None (внешние книги и битые ссылки)."""
    if href.startswith(V8HELP_PREFIX):
        rel = href[len(V8HELP_PREFIX):]
    elif ':' in href:  # другие книги (SyntaxHelperLanguage...) и внешние
        return None
    else:
        rel = '/'.join([d for d in topic_dir.split('/') if d] + [href])
    rel = os.path.normpath(rel).replace('\\', '/')
    return path_to_id.get(rel)


def norm_href_path(href, topic_dir):
    """href -> нормализованный путь цели (для хранения в links.href)."""
    if href.startswith('v8help://'):
        return href
    if href.startswith(V8HELP_PREFIX):
        rel = href[len(V8HELP_PREFIX):]
    else:
        rel = '/'.join([d for d in topic_dir.split('/') if d] + [href])
    return os.path.normpath(rel).replace('\\', '/')


# ------------------------------------------------------------------ дерево

def walk_tree(root):
    """Пройти дерево. Возвращает список тем:
    {rel, dir, filename, parent_hint, kind, sort, cat}.
    kind листа определяется ближайшей сверху групповой папкой."""
    topics = []
    html_exists = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for f in filenames:
            if f.endswith('.html'):
                rel = os.path.relpath(os.path.join(dirpath, f), root)
                html_exists[rel.replace('\\', '/')] = True

    def owner_of(dir_rel):
        """Тема-владелец каталога: ../<имя>.html, если существует."""
        if not dir_rel:
            return None
        parent, base = dir_rel.rsplit('/', 1) if '/' in dir_rel else ('', dir_rel)
        cand = f'{parent}/{base}.html' if parent else f'{base}.html'
        return cand if cand in html_exists else None

    def group_kind_of(dir_rel):
        """Ближайшая сверху групповая папка (methods/properties/...)."""
        d = dir_rel
        while d:
            base = d.rsplit('/', 1)[1] if '/' in d else d
            if base in GROUP_DIRS:
                return base
            d = d.rsplit('/', 1)[0] if '/' in d else ''
        return None

    for dirpath, dirnames, filenames in os.walk(root):
        dir_rel = os.path.relpath(dirpath, root).replace('\\', '/')
        if dir_rel == '.':
            dir_rel = ''
        cat_info, cat_order = {}, []
        if '__categories__' in filenames:
            with open(os.path.join(dirpath, '__categories__'),
                      encoding='utf-8-sig') as f:
                cat_info, cat_order = parse_categories(f.read())

        for f in sorted(filenames):
            if not f.endswith('.html'):
                continue
            rel = f'{dir_rel}/{f}' if dir_rel else f
            # родитель: вверх по каталогам до ближайшего владельца
            parent_rel, d = None, dir_rel
            while d:
                parent_rel = owner_of(d)
                if parent_rel and parent_rel != rel:
                    break
                parent_rel = None
                d = d.rsplit('/', 1)[0] if '/' in d else ''
            kind = 'node' if f[:-5] in dirnames else \
                (group_kind_of(dir_rel) or 'node')
            sort = cat_order.index(f) if f in cat_order else 10 ** 6
            topics.append({'rel': rel, 'dir': dir_rel, 'filename': f,
                           'parent_hint': parent_rel, 'kind': kind,
                           'sort': sort, 'cat': cat_info.get(f)})
    return topics


# -------------------------------------------------------------------- сборка

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE topics(
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    parent_id INTEGER REFERENCES topics(id),
    kind TEXT NOT NULL,
    name_ru TEXT,
    name_en TEXT,
    title TEXT,
    num INTEGER,
    syntax TEXT,
    signature_ru TEXT,
    signature_en TEXT,
    returns TEXT,
    description TEXT,
    example TEXT,
    av_server INTEGER NOT NULL DEFAULT 0,
    av_thick_client INTEGER NOT NULL DEFAULT 0,
    av_thin_client INTEGER NOT NULL DEFAULT 0,
    av_web_client INTEGER NOT NULL DEFAULT 0,
    av_external_connection INTEGER NOT NULL DEFAULT 0,
    av_mobile_client INTEGER NOT NULL DEFAULT 0,
    av_mobile_app_client INTEGER NOT NULL DEFAULT 0,
    av_mobile_app_server INTEGER NOT NULL DEFAULT 0,
    av_mobile_autonomous_server INTEGER NOT NULL DEFAULT 0,
    av_integration INTEGER NOT NULL DEFAULT 0,
    availability_note TEXT,
    since TEXT,
    modes TEXT,
    sort INTEGER
);
CREATE INDEX ix_topics_parent ON topics(parent_id);
CREATE INDEX ix_topics_kind ON topics(kind);
CREATE INDEX ix_topics_name_ru ON topics(name_ru);
CREATE INDEX ix_topics_name_en ON topics(name_en);
CREATE TABLE params(
    topic_id INTEGER NOT NULL REFERENCES topics(id),
    pos INTEGER NOT NULL,
    name TEXT,
    optional INTEGER NOT NULL DEFAULT 0,
    type_text TEXT,
    type_id INTEGER REFERENCES topics(id),
    description TEXT,
    PRIMARY KEY(topic_id, pos)
);
CREATE INDEX ix_params_type ON params(type_id);
CREATE TABLE links(
    src_id INTEGER NOT NULL REFERENCES topics(id),
    dest_id INTEGER REFERENCES topics(id),
    href TEXT NOT NULL,
    kind TEXT NOT NULL
);
CREATE INDEX ix_links_src ON links(src_id);
CREATE INDEX ix_links_dest ON links(dest_id);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE fts USING fts5(
    name_ru, name_en, signature_ru, signature_en, syntax, description
);
"""


def build_syntax_db(root, db_path, progress=print, platform=None):
    """Собрать syntax.db из распакованного дерева root.

    progress(line) — печать хода сборки; platform — строка источника для
    таблицы meta (по умолчанию «1С:Предприятие 8.3.26.1498 / shcntx_ru.hbk»).
    Возвращает словарь: topics, kinds, links, links_broken, links_lang,
    params, params_fallback, st, av_unknown, fts, db_path, size.
    Существующий файл db_path перезаписывается.
    """
    if progress is None:
        progress = lambda line: None  # noqa: E731
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    cur = conn.cursor()

    progress('Обход дерева...')
    topics = walk_tree(root)
    progress(f'  тем: {len(topics)}')

    # синтетические корни objects и tables
    roots = sorted({t['rel'].split('/')[0] for t in topics})
    root_ids = {}
    for r in roots:
        cur.execute(
            'INSERT INTO topics(path, parent_id, kind, name_ru, name_en, title, sort)'
            ' VALUES (?,?,?,?,?,?,?)',
            (r, None, 'root',
             'Объекты' if r == 'objects' else 'Таблицы', r, r, 0))
        root_ids[r] = cur.lastrowid

    # первый проход: вставка тем, путь -> id
    path_to_id = {}
    rows = []
    for t in topics:
        stem = t['filename'][:-5]
        m = RE_FILENUM.match(stem)
        num = int(m.group(2)) if m and m.group(1) else None
        rows.append((t, num))
        cur.execute('INSERT INTO topics(path, kind, num, sort) VALUES (?,?,?,?)',
                    (t['rel'], t['kind'], num, t['sort']))
        path_to_id[t['rel']] = cur.lastrowid
        t['id'] = cur.lastrowid

    # второй проход: содержимое страниц
    stats = {'links': 0, 'links_broken': 0, 'links_lang': 0, 'params': 0,
             'params_fallback': 0, 'st': 0, 'av_unknown': 0}
    link_rows = []
    n = len(rows)
    for i, (t, num) in enumerate(rows):
        full = os.path.join(root, *t['rel'].split('/'))
        with open(full, encoding='utf-8-sig') as f:
            raw = f.read()
        page, hrefs = parse_html_page(
            raw, lambda href: resolve_href(href, t['dir'], path_to_id))

        # сигнатура .st
        st_path = full[:-5] + '.st'
        sig_ru = sig_en = None
        if os.path.exists(st_path):
            try:
                with open(st_path, encoding='utf-8-sig') as f:
                    found = {}
                    collect_st_signatures(parse_1c(f.read()), found)
                sig_ru, sig_en = found.get('ru'), found.get('en')
                stats['st'] += 1
            except ValueError:
                pass

        # параметры
        params, params_fallback = [], None
        if 'params_raw' in page:
            params, params_fallback = parse_params(page['params_raw'])
            stats['params'] += len(params)
            if params_fallback:
                stats['params_fallback'] += 1

        # ссылки
        for href, kind in hrefs:
            dest = resolve_href(href, t['dir'], path_to_id)
            link_rows.append((t['id'], dest, norm_href_path(href, t['dir']),
                              kind))
            stats['links'] += 1
            if dest is None:
                if href.startswith('v8help://SyntaxHelperLanguage/'):
                    stats['links_lang'] += 1
                else:
                    stats['links_broken'] += 1

        modes = json.dumps(t['cat'], ensure_ascii=False) if t['cat'] else None

        # доступность: булевы флаги платформ + примечание
        av_flags = {col: 0 for col, _ in AV_PLATFORMS}
        av_note = None
        if page['availability']:
            av_flags, av_note, unknown = parse_availability(page['availability'])
            stats['av_unknown'] += len(unknown)

        cur.execute(
            'UPDATE topics SET parent_id=?, name_ru=?, name_en=?, title=?,'
            ' syntax=?, signature_ru=?, signature_en=?, returns=?,'
            ' description=?, example=?, availability_note=?, since=?, modes=?,'
            ' av_server=?, av_thick_client=?, av_thin_client=?, av_web_client=?,'
            ' av_external_connection=?, av_mobile_client=?,'
            ' av_mobile_app_client=?, av_mobile_app_server=?,'
            ' av_mobile_autonomous_server=?, av_integration=?'
            ' WHERE id=?',
            (None, page['name_ru'], page['name_en'], page['title'],
             page['syntax'], sig_ru, sig_en, page['returns'],
             page.get('description'), page['example'], av_note,
             page['since'], modes,
             *(av_flags[col] for col, _ in AV_PLATFORMS), t['id']))
        for pos, p in enumerate(params):
            cur.execute('INSERT INTO params VALUES (?,?,?,?,?,?,?)',
                        (t['id'], pos, p['name'], p['optional'],
                         p['type_text'], p['type_id'], p['description']))
        if params_fallback:
            cur.execute('INSERT INTO params VALUES (?,?,?,?,?,?,?)',
                        (t['id'], 0, None, 0, None, None, params_fallback))

        if progress and ((i + 1) % 5000 == 0 or i + 1 == n):
            progress(f'  обработано {i + 1}/{n} тем...')

    # родители
    for t in topics:
        hint = t['parent_hint']
        pid = path_to_id.get(hint) if hint else None
        if pid is None or pid == t['id']:
            pid = root_ids.get(t['rel'].split('/')[0])
        cur.execute('UPDATE topics SET parent_id=? WHERE id=?', (pid, t['id']))

    # виды: листья — по групповой папке; узлы — type, если их собственный
    # каталог содержит групповые папки, иначе group/section
    kind_map = {'methods': 'method', 'properties': 'property',
                'events': 'event', 'ctors': 'ctor',
                'formparams': 'formparam', 'fields': 'field',
                'params': 'param'}
    dirs_with_groups = set()
    for dirpath, dirnames, _ in os.walk(root):
        if any(d in GROUP_DIRS for d in dirnames):
            dirs_with_groups.add(
                os.path.relpath(dirpath, root).replace('\\', '/'))
    for t in topics:
        kind = t['kind']
        if kind in kind_map:
            kind = kind_map[kind]
        elif kind == 'node':
            stem = t['filename'][:-5]
            owned = f"{t['dir']}/{stem}" if t['dir'] else stem
            if owned.replace('\\', '/') in dirs_with_groups:
                kind = 'type'
            elif t['rel'].count('/') == 1:  # прямо в objects/ или tables/
                kind = 'section'
            else:
                kind = 'group'
        cur.execute('UPDATE topics SET kind=? WHERE id=?', (kind, t['id']))

    conn.executemany('INSERT INTO links VALUES (?,?,?,?)', link_rows)

    # FTS
    fts_ok = True
    try:
        conn.execute(FTS_SCHEMA)
        conn.execute("""
            INSERT INTO fts(rowid, name_ru, name_en, signature_ru,
                            signature_en, syntax, description)
            SELECT id, COALESCE(name_ru,''), COALESCE(name_en,''),
                   COALESCE(signature_ru,''), COALESCE(signature_en,''),
                   COALESCE(syntax,''), COALESCE(description,'')
            FROM topics""")
    except sqlite3.OperationalError as e:
        fts_ok = False
        progress(f'  FTS5 недоступен: {e}')

    kind_counts = dict(cur.execute(
        'SELECT kind, COUNT(*) FROM topics GROUP BY kind ORDER BY 2 DESC'))
    from .. import __version__
    meta_rows = [
        ('source', os.path.abspath(root)),
        ('built_at', datetime.now(timezone.utc).isoformat(timespec='seconds')),
        ('platform', platform or '1С:Предприятие 8.3.26.1498 / shcntx_ru.hbk'),
        ('topics', str(len(topics))),
        ('kinds', json.dumps(kind_counts)),
        ('links', str(stats['links'])),
        ('links_broken', str(stats['links_broken'])),
        ('links_lang', str(stats['links_lang'])),
        ('params', str(stats['params'])),
        ('av_unknown', str(stats['av_unknown'])),
        ('fts', str(int(fts_ok))),
        ('extractor_version', __version__),
    ]
    cur.executemany('INSERT INTO meta VALUES (?,?)', meta_rows)
    conn.commit()
    conn.close()

    result = dict(stats)
    result.update({'topics': len(topics), 'kinds': kind_counts, 'fts': fts_ok,
                   'db_path': db_path, 'size': os.path.getsize(db_path)})
    return result


def format_build_result(result):
    """Сводка сборки — текст для печати (итоги build_syntax_db)."""
    lines = ['Результат:']
    for kind, c in result['kinds'].items():
        lines.append(f'  {kind:<12} {c}')
    lines.append(f'  параметров: {result["params"]} '
                 f'(нераспознанных секций: {result["params_fallback"]})')
    lines.append(f'  ссылок: {result["links"]} '
                 f'(битых внутренних: {result["links_broken"]},'
                 f' на книгу языка: {result["links_lang"]})')
    lines.append(f'  сигнатур .st: {result["st"]}')
    lines.append(f'  доступность: нераспознанных платформ: {result["av_unknown"]}')
    lines.append(f'  БД: {result["db_path"]} '
                 f'({result["size"] / 1e6:.1f} МБ)')
    return '\n'.join(lines)
