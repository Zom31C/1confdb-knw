"""Тесты синтакс-помощника платформы: распаковка .hbk, сборка syntax.db,
контроль качества, поиск/карточки и MCP-инструменты syntax_*.

Данные — малые синтетические фикстуры: «настоящий» .hbk собирается из
обычного ZIP плюс 1656-байтный заголовок (ровно то, что делает платформа),
дерево справки — из нескольких HTML-страниц в разметке V8SH_* реального
shcntx_ru.hbk (эталонные пути BeginTransaction9/Ref23 сохранены — на них
настроены точечные проверки check).
"""
import io
import os
import sqlite3
import struct
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from confdb.__main__ import build_parser, main  # noqa: E402
from confdb.hbk import build as hbk_build  # noqa: E402
from confdb.hbk import check as hbk_check  # noqa: E402
from confdb.hbk import query as hbk_query  # noqa: E402
from confdb.hbk import unpack as hbk_unpack  # noqa: E402
from confdb.mcp_server import McpServer  # noqa: E402

HEADER_LEN = 1656  # длина заголовка 1С перед ZIP в настоящем .hbk


# ------------------------------------------------------------------- .hbk

def make_hbk(files, header=b'\xa5' * HEADER_LEN, tail=b''):
    """Синтетический .hbk: заголовок 1С + обычный ZIP (+ мусорный хвост)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return header + buf.getvalue() + tail


def bad_tail():
    """Хвостовой «сегмент» с EOCD, который не проходит валидацию
    (cd_size больше самого файла — base offset уходит в минус)."""
    return b'PK\x05\x06' + struct.pack('<HHHHIIH', 0, 0, 3, 3,
                                        0xFFFFFFFF, 0, 0)


def test_unpack_synthetic_hbk(tmp_path):
    files = {
        'objects/Global context.html': b'<html>page</html>',
        'objects/dir/': b'',
        'objects/dir/a.txt': b'hello',
        'tables/b.txt': 'кириллица'.encode('utf-8'),
    }
    hbk = tmp_path / 'test.hbk'
    hbk.write_bytes(make_hbk(files, tail=bad_tail()))
    out = tmp_path / 'extracted'

    lines = hbk_unpack.describe_segments(hbk.read_bytes())
    assert len(lines) == 2  # основной сегмент + невалидный хвост

    stats = hbk_unpack.unpack_hbk(str(hbk), str(out))
    assert stats['base'] == HEADER_LEN  # хвост пропущен, основной архив найден
    assert stats['files'] == 3 and stats['dirs'] == 1
    assert (out / 'objects' / 'Global context.html').read_bytes() == \
        b'<html>page</html>'
    assert (out / 'tables' / 'b.txt').read_text(encoding='utf-8') == 'кириллица'


def test_unpack_verify_only(tmp_path):
    hbk = tmp_path / 'test.hbk'
    hbk.write_bytes(make_hbk({'a.txt': b'data'}))
    out = tmp_path / 'extracted'
    stats = hbk_unpack.verify_hbk(str(hbk))
    assert stats['files'] == 1
    assert not out.exists()  # файлы не писались


def test_unpack_list_entries(tmp_path):
    hbk = tmp_path / 'test.hbk'
    hbk.write_bytes(make_hbk({'a.txt': b'1', 'b.txt': b'22'}))
    brief, total = hbk_unpack.list_entries(str(hbk))
    assert total == 2
    assert dict((n, s) for s, n in brief) == {'a.txt': 1, 'b.txt': 2}


def test_unpack_rejects_path_escape(tmp_path):
    hbk = tmp_path / 'evil.hbk'
    hbk.write_bytes(make_hbk({'../evil.txt': b'x'}))
    with pytest.raises(hbk_unpack.HbkError):
        hbk_unpack.unpack_hbk(str(hbk), str(tmp_path / 'out'))
    assert not (tmp_path / 'evil.txt').exists()


def test_unpack_not_an_archive(tmp_path):
    hbk = tmp_path / 'junk.hbk'
    hbk.write_bytes(b'PK\x03\x04 not really a zip')
    with pytest.raises(hbk_unpack.HbkError):
        hbk_unpack.unpack_hbk(str(hbk), str(tmp_path / 'out'))


# ------------------------------------------------------- дерево справки

ST_BEGIN = ('{1,\n{2,\n{"",1,0,"",""},\n{0,\n{"ru",0,0,"","НачатьТранзакцию(<?>);"}\n},\n'
            '{0,\n{"en",0,0,"","BeginTransaction(<?>);"}\n}\n}\n}')
ST_COMMIT = ('{1,\n{1,\n{"",1,0,"",""},\n{0,\n{"ru",0,0,"","ЗафиксироватьТранзакцию();"}\n}\n}\n}')
ST_REF = ('{1,\n{2,\n{"",1,0,"",""},\n{0,\n{"ru",0,0,"","Ссылка"}\n},\n'
          '{0,\n{"en",0,0,"","Ref"}\n}\n}\n}')

CATALOG8 = ('{4,"BeginTransaction9.html", 9, {"CONF_ENTERPRISE",'
            '"CONF_SRV_ENTERPRISE","8.0.4", "", ""},"BeginTransaction9.st", 9,'
            ' {"CONF_ENTERPRISE","CONF_SRV_ENTERPRISE","8.0.4", "", ""},'
            '"CommitTransaction10.html", 9, {"CONF_ENTERPRISE",'
            '"CONF_SRV_ENTERPRISE","8.0.4", "", ""},"CommitTransaction10.st",'
            ' 9, {"CONF_ENTERPRISE","CONF_SRV_ENTERPRISE","8.0.4", "", ""}}')

HEAD = ('<html><head><meta http-equiv="Content-Type" '
        'content="text/html; charset=utf-8"></head><body>')
TAIL = '<HR><p>Методическая информация</p></body></html>'
TYPE_HREF = ('v8help://SyntaxHelperContext/objects/catalog2/catalog1450/'
             'DataLockControlMode.html')

GLOBAL_CONTEXT_HTML = (
    HEAD +
    '<h1 class="V8SH_pagetitle">Глобальный контекст (Global context)</h1>'
    '<p class="V8SH_heading">Глобальный контекст (Global context)</p>'
    '<p class="V8SH_chapter">Описание:</p>'
    '<p>Методы и свойства, доступные в модулях всех видов.</p>'
    '<p class="V8SH_chapter">См. также:</p>'
    '<a href="v8help://SyntaxHelperContext/objects/Global context/methods/'
    'catalog8/BeginTransaction9.html">НачатьТранзакцию</a>' + TAIL)

BEGIN_HTML = (
    HEAD +
    '<h1 class="V8SH_pagetitle">Глобальный контекст.НачатьТранзакцию '
    '(Global context.BeginTransaction)</h1>'
    '<p class="V8SH_title">Глобальный контекст (Global context)</p>'
    '<p class="V8SH_heading">НачатьТранзакцию (BeginTransaction)</p>'
    '<div class="__SINCE_SHOW_STYLE__"><p class="not_used">Доступен, '
    'начиная с версии 8.0.</p></div>'
    '<p class="V8SH_chapter">Синтаксис:</p>'
    'НачатьТранзакцию(&lt;РежимБлокировок&gt;)'
    '<p class="V8SH_chapter">Параметры:</p>'
    '<div class="V8SH_rubric"> <p style="margin-top: 2px">'
    '&lt;РежимБлокировок&gt; (необязательный)</div>'
    f'Тип: <a href="{TYPE_HREF}">РежимУправленияБлокировкойДанных</a>. <br>'
    'Установка параметра имеет смысл в режиме '
    '<a href="v8help://SyntaxHelperContext/objects/catalog2/catalog1450/'
    'DataLockControlMode/properties/Automatic5833.html">Автоматический</a>.'
    '<p class="V8SH_chapter">Описание:</p>'
    '<p>Открывает транзакцию для записи согласованных изменений.</p>'
    '<p class="V8SH_chapter">Доступность: </p>'
    '<p>Сервер, толстый клиент, внешнее соединение.</p>'
    '<p class="V8SH_chapter">Пример:</p>'
    '<p>НачатьТранзакцию();<br>ЗафиксироватьТранзакцию();</p>'
    '<p class="V8SH_chapter">См. также:</p>'
    '<a href="v8help://SyntaxHelperContext/objects/Global context.html">'
    'Глобальный контекст</a>, метод '
    '<a href="v8help://SyntaxHelperContext/objects/Global context/methods/'
    'catalog8/CommitTransaction10.html">ЗафиксироватьТранзакцию</a><br>'
    '<a href="v8help://SyntaxHelperContext/objects/Global context.html">'
    'Глобальный контекст</a>, метод '
    '<a href="v8help://SyntaxHelperContext/objects/Global context/methods/'
    'catalog8/RollbackTransaction11.html">ОтменитьТранзакцию</a><br>' + TAIL)

COMMIT_HTML = (
    HEAD +
    '<h1 class="V8SH_pagetitle">ЗафиксироватьТранзакцию (CommitTransaction)</h1>'
    '<p class="V8SH_heading">ЗафиксироватьТранзакцию (CommitTransaction)</p>'
    '<p class="V8SH_chapter">Синтаксис:</p>ЗафиксироватьТранзакцию()'
    '<p class="V8SH_chapter">Описание:</p><p>Завершает транзакцию.</p>'
    '<p class="V8SH_chapter">Доступность: </p><p>Сервер, толстый клиент.</p>'
    + TAIL)

DATALOCK_HTML = (
    HEAD +
    '<h1 class="V8SH_pagetitle">РежимУправленияБлокировкойДанных '
    '(DataLockControlMode)</h1>'
    '<p class="V8SH_heading">РежимУправленияБлокировкойДанных '
    '(DataLockControlMode)</p>'
    '<p class="V8SH_chapter">Описание:</p>'
    '<p>Режим управления блокировкой данных в транзакции.</p>' + TAIL)

TABLE5_HTML = (
    HEAD +
    '<h1 class="V8SH_pagetitle">Документы (Documents)</h1>'
    '<p class="V8SH_heading">Документы (Documents)</p>'
    '<p class="V8SH_chapter">Описание:</p>'
    '<p>Системная таблица документов.</p>' + TAIL)

REF23_HTML = (
    HEAD +
    '<h1 class="V8SH_pagetitle">Ссылка (Ref)</h1>'
    '<p class="V8SH_heading">Ссылка (Ref)</p>'
    f'Тип: <a href="{TYPE_HREF}">РежимУправленияБлокировкойДанных</a>. <br>'
    'Содержит ссылку на документ.<br>' + TAIL)


def make_syntax_tree(root):
    """Мини-дерево выгрузки shcntx_ru.hbk в разметке реальных страниц."""
    def put(rel, text):
        path = os.path.join(root, *rel.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8-sig', newline='') as f:
            f.write(text)

    put('objects/Global context.html', GLOBAL_CONTEXT_HTML)
    put('objects/Global context/methods/catalog8/__categories__', CATALOG8)
    put('objects/Global context/methods/catalog8/BeginTransaction9.html',
        BEGIN_HTML)
    put('objects/Global context/methods/catalog8/BeginTransaction9.st',
        ST_BEGIN)
    put('objects/Global context/methods/catalog8/CommitTransaction10.html',
        COMMIT_HTML)
    put('objects/Global context/methods/catalog8/CommitTransaction10.st',
        ST_COMMIT)
    put('objects/catalog2/catalog1450/DataLockControlMode.html', DATALOCK_HTML)
    put('tables/table5.html', TABLE5_HTML)
    put('tables/table5/fields/Ref23.html', REF23_HTML)
    put('tables/table5/fields/Ref23.st', ST_REF)
    return root


@pytest.fixture()
def mini_db(tmp_path):
    """syntax.db, собранная из мини-дерева справки."""
    tree = make_syntax_tree(str(tmp_path / 'tree'))
    db = str(tmp_path / 'syntax.db')
    hbk_build.build_syntax_db(tree, db, progress=None)
    return db


# ------------------------------------------------------------------ сборка

def test_build_topics_and_tree(mini_db):
    conn = sqlite3.connect(mini_db)
    conn.row_factory = sqlite3.Row
    topics = {r['path']: dict(r) for r in conn.execute('SELECT * FROM topics')}
    # два синтетических корня + 6 страниц
    assert len(topics) == 8
    assert topics['objects']['kind'] == 'root'
    assert topics['objects']['name_ru'] == 'Объекты'
    assert topics['tables']['name_ru'] == 'Таблицы'

    gc = topics['objects/Global context.html']
    assert gc['kind'] == 'type' and gc['parent_id'] == topics['objects']['id']

    begin = topics['objects/Global context/methods/catalog8/'
                   'BeginTransaction9.html']
    assert begin['kind'] == 'method'
    assert begin['parent_id'] == gc['id']
    assert begin['name_ru'] == 'НачатьТранзакцию'
    assert begin['name_en'] == 'BeginTransaction'
    assert begin['num'] == 9
    assert begin['syntax'] == 'НачатьТранзакцию(<РежимБлокировок>)'
    assert begin['signature_ru'] == 'НачатьТранзакцию(<?>);'
    assert begin['signature_en'] == 'BeginTransaction(<?>);'
    assert begin['since'] == 'Доступен, начиная с версии 8.0.'
    assert begin['av_server'] == 1 and begin['av_thick_client'] == 1
    assert begin['av_external_connection'] == 1 and begin['av_web_client'] == 0
    assert begin['example'].startswith('НачатьТранзакцию();')
    # порядок и режимы из __categories__
    commit = topics['objects/Global context/methods/catalog8/'
                    'CommitTransaction10.html']
    assert begin['sort'] < commit['sort']
    assert 'CONF_ENTERPRISE' in begin['modes'] and '8.0.4' in begin['modes']

    # поле системной таблицы: вид field, владелец — тема таблицы
    ref = topics['tables/table5/fields/Ref23.html']
    table = topics['tables/table5.html']
    assert ref['kind'] == 'field' and table['kind'] == 'type'
    assert ref['parent_id'] == table['id']
    assert ref['name_en'] == 'Ref'
    conn.close()


def test_build_params_links_inline_ids(mini_db):
    conn = sqlite3.connect(mini_db)
    conn.row_factory = sqlite3.Row
    tid = conn.execute(
        "SELECT id FROM topics WHERE path LIKE '%BeginTransaction9.html'"
    ).fetchone()[0]
    type_id = conn.execute(
        "SELECT id FROM topics WHERE path LIKE '%DataLockControlMode.html'"
    ).fetchone()[0]

    params = conn.execute('SELECT * FROM params WHERE topic_id=? ORDER BY pos',
                          (tid,)).fetchall()
    assert len(params) == 1
    p = params[0]
    assert p['name'] == 'РежимБлокировок' and p['optional'] == 1
    assert p['type_id'] == type_id
    # хвостовой пробел перед точкой сохраняет и оригинальный код сборки
    # (rstrip('.') после strip()) — порт ведёт себя так же
    assert p['type_text'].strip() == 'РежимУправленияБлокировкойДанных'
    # разрешённая ссылка на тип заменена в тексте отметкой (id=N),
    # неразрешённая (Automatic5833) осталась просто текстом
    assert f'(id={type_id})' not in p['description']
    assert 'Автоматический' in p['description']

    kinds = {}
    for l in conn.execute('SELECT dest_id, kind FROM links WHERE src_id=?',
                          (tid,)):
        kinds.setdefault(l['kind'], []).append(l['dest_id'])
    # «См. также»: Глобальный контекст, ЗафиксироватьТранзакцию, Глобальный
    # контекст, ОтменитьТранзакцию (цель последней нет в фикстуре)
    assert len(kinds['see']) == 4
    assert kinds['see'].count(None) == 1
    assert kinds['type'] == [type_id]      # «Тип:» параметра
    assert kinds['body'] == [None]         # Automatic5833 — цель отсутствует

    # type-ссылка поля Ref23 разрешается
    ref_row = conn.execute(
        "SELECT t.id FROM topics t WHERE t.path LIKE '%Ref23.html'").fetchone()
    ref_type = conn.execute(
        "SELECT dest_id FROM links WHERE src_id=? AND kind='type'",
        (ref_row[0],)).fetchall()
    assert [r[0] for r in ref_type] == [type_id]

    meta = dict(conn.execute('SELECT key, value FROM meta'))
    # битые: Automatic5833 (свойство типа) и RollbackTransaction11 (метод)
    assert meta['links_broken'] == '2'
    conn.close()


def test_build_fts_and_meta(mini_db):
    conn = sqlite3.connect(mini_db)
    hits = conn.execute(
        "SELECT rowid FROM fts WHERE fts MATCH 'транзакци*'").fetchall()
    assert hits
    meta = dict(conn.execute('SELECT key, value FROM meta'))
    assert meta['topics'] == '6'           # страницы; два корня не в счёт
    assert meta['fts'] == '1'
    assert meta.get('extractor_version')
    conn.close()


# ----------------------------------------------------------------- качество

def test_check_quick_passes(mini_db):
    fails = hbk_check.check_syntax_db(mini_db, out=None, full=False)
    assert fails == []


def test_check_full_fails_only_on_thresholds(mini_db):
    fails = hbk_check.check_syntax_db(mini_db, out=None, full=True)
    # числовой порог полной базы не пройден, эталонные темы на месте
    assert 'ссылки на типы разрешаются' in fails
    assert 'тема найдена' not in fails and 'поле найдено' not in fails


# ------------------------------------------------------------------- запросы

def test_query_search_and_card(mini_db):
    conn = hbk_query.open_syntax_db(mini_db)
    cache = hbk_query.load_name_cache(conn)
    res = hbk_query.search(conn, 'транзакци', 10, cache)
    ids = [tid for tid, _, _ in res]
    row = conn.execute(
        "SELECT id, name_ru FROM topics WHERE name_ru='НачатьТранзакцию'"
    ).fetchone()
    assert row['id'] in ids

    # опечатка в EN-имени находится нечётким поиском (FTS-префикс не спасает)
    res = hbk_query.search(conn, 'BeginTransakcion', 10, cache)
    assert row['id'] in [tid for tid, _, _ in res]

    t = hbk_query.topic_full(conn, row['id'])
    assert t['breadcrumb'] == ['Объекты', 'Глобальный контекст',
                               'НачатьТранзакцию']
    assert t['availability'] == ['сервер', 'толстый клиент',
                                 'внешнее соединение']
    assert t['params'][0]['type_name'] == 'РежимУправленияБлокировкойДанных'
    assert len(t['links']['see']) == 4
    card = hbk_query.format_topic(t)
    assert 'Синтаксис:' in card and 'НачатьТранзакцию(<РежимБлокировок>)' in card
    assert 'Сигнатура RU: НачатьТранзакцию(<?>);' in card
    assert '(id=' in card  # отметки целей ссылок для перехода

    # корни и дети
    roots = hbk_query.children_of(conn, None)
    assert [r['name_ru'] for r in roots] == ['Объекты', 'Таблицы']
    gc_id = conn.execute(
        "SELECT id FROM topics WHERE kind='type' AND name_ru='Глобальный контекст'"
    ).fetchone()[0]
    kids = hbk_query.children_of(conn, gc_id)
    assert [k['name_ru'] for k in kids] == ['НачатьТранзакцию',
                                            'ЗафиксироватьТранзакцию']
    tree = hbk_query.format_tree(conn, gc_id, 2, 20)
    assert 'НачатьТранзакцию' in tree and '`-- ' in tree
    conn.close()


def test_query_rejects_foreign_db(tmp_path):
    other = tmp_path / 'other.db'
    conn = sqlite3.connect(str(other))
    conn.execute('CREATE TABLE t(x)')
    conn.commit()
    conn.close()
    with pytest.raises(ValueError):
        hbk_query.open_syntax_db(str(other))


# ------------------------------------------------------- MCP-инструменты

def _call(server, tool, **args):
    resp = server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                          'params': {'name': tool, 'arguments': args}})
    return resp['result']


def test_mcp_syntax_tools(mini_db):
    server = McpServer(None, syntax_db=mini_db)
    res = _call(server, 'syntax_search', query='НачатьТранзакцию', limit=5)
    assert not res.get('isError')
    text = res['content'][0]['text']
    assert '=== синтакс-помощник' in text
    assert 'НачатьТранзакцию' in text and 'syntax_card' in text

    tid = server.syntax['conn'].execute(
        "SELECT id FROM topics WHERE name_ru='НачатьТранзакцию'").fetchone()[0]
    res = _call(server, 'syntax_card', id=tid)
    text = res['content'][0]['text']
    assert 'Сигнатура RU: НачатьТранзакцию(<?>);' in text
    assert 'доступность: сервер' in text

    res = _call(server, 'syntax_children')
    assert 'Корни дерева' in res['content'][0]['text']
    res = _call(server, 'syntax_children', id=tid)
    assert '(потомков нет)' in res['content'][0]['text']

    # tools/list включает инструменты синтакс-помощника
    resp = server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    names = {t['name'] for t in resp['result']['tools']}
    assert {'syntax_search', 'syntax_card', 'syntax_children'} <= names
    # PRIMER рассказывает про помощник
    resp = server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
    assert 'PLATFORM SYNTAX HELPER' in resp['result']['instructions']


def test_mcp_syntax_not_attached(mini_db):
    server = McpServer(None)
    res = _call(server, 'syntax_search', query='транзакци')
    assert res.get('isError')
    assert '--syntax-db' in res['content'][0]['text']
    res = _call(server, 'syntax_card', id=1)
    assert res.get('isError')


def test_mcp_syntax_bad_file(tmp_path):
    junk = tmp_path / 'junk.db'
    conn = sqlite3.connect(str(junk))
    conn.execute('CREATE TABLE t(x)')
    conn.commit()
    conn.close()
    from confdb.mcp_server import UserError
    with pytest.raises(UserError):
        McpServer(None, syntax_db=str(junk))
    with pytest.raises(UserError):
        McpServer(None, syntax_db=str(tmp_path / 'нет-такого.db'))


# ---------------------------------------------------------------------- CLI

def test_cli_parser_hbk():
    args = build_parser().parse_args(
        ['hbk', 'unpack', 'x.hbk', '-o', 'out', '--verify-only'])
    assert args.cmd == 'hbk' and args.hbk_cmd == 'unpack'
    assert args.hbk == 'x.hbk' and args.out == 'out' and args.verify_only
    args = build_parser().parse_args(['hbk', 'build', 'tree', '--db', 's.db'])
    assert args.hbk_cmd == 'build' and args.root == 'tree' and args.db == 's.db'
    args = build_parser().parse_args(['hbk', 'search', 'транзакци',
                                      '--limit', '5'])
    assert args.query == ['транзакци'] and args.limit == 5
    args = build_parser().parse_args(['hbk', 'tree', '7', '-d', '3'])
    assert args.id == 7 and args.depth == 3
    args = build_parser().parse_args(['1confdb-knw', '--syntax-db', 's.db'])
    assert args.syntax_db == 's.db'
    assert build_parser().parse_args(['1confdb-knw']).syntax_db is None


def test_cli_hbk_queries(mini_db, capsys):
    assert main(['hbk', 'search', 'транзакци', '--db', mini_db]) == 0
    out = capsys.readouterr().out
    assert 'НачатьТранзакцию' in out and 'Найдено' in out

    tid = sqlite3.connect(mini_db).execute(
        "SELECT id FROM topics WHERE name_ru='НачатьТранзакцию'").fetchone()[0]
    assert main(['hbk', 'show', str(tid), '--db', mini_db]) == 0
    assert 'Сигнатура RU' in capsys.readouterr().out
    assert main(['hbk', 'tree', str(tid), '--db', mini_db]) == 0
    assert 'НачатьТранзакцию' in capsys.readouterr().out
    # несуществующая тема / база
    assert main(['hbk', 'show', '999999', '--db', mini_db]) == 1
    assert main(['hbk', 'search', 'x', '--db', mini_db + '.нет']) == 2


def test_cli_hbk_check_and_build(tmp_path, capsys):
    tree = make_syntax_tree(str(tmp_path / 'tree'))
    db = str(tmp_path / 'syntax.db')
    assert main(['hbk', 'build', tree, '--db', db]) == 0
    assert 'Результат:' in capsys.readouterr().out
    assert main(['hbk', 'check', db, '--quick']) == 0
    assert 'ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ' in capsys.readouterr().out
    assert main(['hbk', 'check', db]) == 1  # полный порог type-ссылок
    capsys.readouterr()
    # build без objects/ — ошибка входа
    assert main(['hbk', 'build', str(tmp_path), '--db', db]) == 2


def test_cli_hbk_unpack(tmp_path, capsys):
    hbk = tmp_path / 'test.hbk'
    hbk.write_bytes(make_hbk({'objects/a.html': b'<html/>'}))
    out = tmp_path / 'ex'
    assert main(['hbk', 'unpack', str(hbk), '-o', str(out)]) == 0
    assert 'Готово: файлов=1' in capsys.readouterr().out
    assert (out / 'objects' / 'a.html').exists()
    assert main(['hbk', 'unpack', str(hbk), '--list']) == 0
    assert 'objects/a.html' in capsys.readouterr().out
    assert main(['hbk', 'unpack', str(hbk), '--verify-only']) == 0
    assert 'CRC проверен' in capsys.readouterr().out
    assert main(['hbk', 'unpack', str(tmp_path / 'нет.hbk')]) == 2
