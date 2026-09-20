"""Тесты MCP-сервера: протокол и инструменты на синтетической базе."""
import json
import os
import sqlite3
import sys
import threading
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import confdb.mcp_server as mcp_server  # noqa: E402
from confdb.db.writer import write_db  # noqa: E402
from confdb.mcp_server import McpServer, resolve_db, start_http_server  # noqa: E402

from test_writer import make_dump  # noqa: E402


def _server(tmp_path_factory):
    dump = str(tmp_path_factory.mktemp('dump'))
    make_dump(dump)
    db = str(tmp_path_factory.mktemp('db') / 't.sqlite')
    write_db(dump, db, source_file='t.cf')
    return McpServer(db)


def _call(server, tool, **args):
    resp = server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                          'params': {'name': tool, 'arguments': args}})
    return resp['result']


def test_initialize_has_primer(tmp_path_factory):
    server = _server(tmp_path_factory)
    resp = server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
    assert 'meta_object' in resp['result']['instructions']
    assert resp['result']['protocolVersion']


def test_tools_list(tmp_path_factory):
    server = _server(tmp_path_factory)
    resp = server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    names = {t['name'] for t in resp['result']['tools']}
    assert names == {'find_objects', 'object_card', 'object_tree', 'find_field',
                     'refs_of', 'module_outline', 'get_method', 'find_methods',
                     'skd_of', 'find_skd', 'check_query', 'sql',
                     'db_list', 'db_open', 'db_use', 'db_close'}


def test_find_objects_and_card(tmp_path_factory):
    server = _server(tmp_path_factory)
    text = _call(server, 'find_objects', mask='Справочник1')['content'][0]['text']
    assert 'Справочник.Справочник1' in text
    # вход в русском точечном формате
    card = _call(server, 'object_card', path='Справочник.Справочник1')['content'][0]['text']
    assert 'СсылкаАтрибут' in card and 'Товары' not in card
    # и в старом слэш-формате
    card_doc = _call(server, 'object_card',
                     path='Document/ЗаказПокупателя')['content'][0]['text']
    assert 'Документ.ЗаказПокупателя' in card_doc
    assert 'Табличная часть Товары' in card_doc and 'ТоварыНоменклатура' in card_doc


def test_tree_and_field(tmp_path_factory):
    server = _server(tmp_path_factory)
    tree = _call(server, 'object_tree', path='', depth=3)['content'][0]['text']
    assert 'Справочник.Справочник1' in tree
    assert 'Справочник.Справочник1.ФормаЭлемента' in tree
    fields = _call(server, 'find_field', name='Товары')['content'][0]['text']
    assert 'Документ.ЗаказПокупателя' in fields and '[табчасть Товары]' in fields


def test_methods(tmp_path_factory):
    server = _server(tmp_path_factory)
    found = _call(server, 'find_methods', mask='Тест')['content'][0]['text']
    assert 'процедура Тест()' in found
    method = _call(server, 'get_method', path='Catalog/Справочник1',
                   code_name='obj', name='Тест')['content'][0]['text']
    assert 'КонецПроцедуры' in method
    outline = _call(server, 'module_outline',
                    path='Catalog/Справочник1')['content'][0]['text']
    assert 'Процедура Тест()' in outline


def test_check_and_sql(tmp_path_factory):
    server = _server(tmp_path_factory)
    ok = _call(server, 'check_query',
               text='ВЫБРАТЬ Т.СсылкаАтрибут ИЗ Справочник.Справочник1 КАК Т')
    text = ok['content'][0]['text']
    # Ответ содержит заголовок базы и 'OK'
    assert '=== база' in text and 'OK' in text
    bad = _call(server, 'check_query',
                text='ВЫБРАТЬ Т.Х ИЗ Справочник.Нет КАК Т')['content'][0]['text']
    assert 'неизвестная таблица' in bad
    res = _call(server, 'sql', query='SELECT COUNT(*) AS n FROM meta_object')
    assert 'n' in res['content'][0]['text']
    # русский точечный путь в литерале sql конвертируется во внутренний формат
    res = _call(server, 'sql',
                query="SELECT path FROM meta_object WHERE path = 'Справочник.Справочник1'")
    assert 'Catalog/Справочник1' in res['content'][0]['text']
    denied = _call(server, 'sql', query='DELETE FROM meta_object')
    assert denied.get('isError')


def test_refs_and_skd_empty(tmp_path_factory):
    server = _server(tmp_path_factory)
    refs = _call(server, 'refs_of', path='Catalog/Справочник1')['content'][0]['text']
    assert 'Ссылается на' in refs
    skd = _call(server, 'skd_of', path='Catalog/Справочник1')['content'][0]['text']
    assert 'нет запросов' in skd


def test_jsonrpc_roundtrip(tmp_path_factory):
    server = _server(tmp_path_factory)
    resp = server.handle(json.loads('{"jsonrpc":"2.0","id":9,'
                                    '"method":"notifications/initialized"}'))
    assert resp is None
    resp = server.handle({'jsonrpc': '2.0', 'id': 10, 'method': 'nope'})
    assert resp['error']['code'] == -32601


def _post(port, msg, path='/mcp'):
    req = urllib.request.Request(
        f'http://127.0.0.1:{port}{path}',
        data=json.dumps(msg).encode('utf-8'),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read()


def test_http_transport(tmp_path_factory):
    server = _server(tmp_path_factory)
    httpd, port = start_http_server(server, '127.0.0.1', 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        status, body = _post(port, {'jsonrpc': '2.0', 'id': 1,
                                    'method': 'initialize'})
        assert status == 200
        assert 'instructions' in json.loads(body)['result']
        status, _ = _post(port, {'jsonrpc': '2.0',
                                 'method': 'notifications/initialized'})
        assert status == 202
        status, body = _post(port, {'jsonrpc': '2.0', 'id': 2,
                                    'method': 'tools/call',
                                    'params': {'name': 'find_objects',
                                               'arguments': {'mask': 'Справочник1'}}})
        assert status == 200
        assert 'Справочник.Справочник1' in json.loads(body)['result']['content'][0]['text']
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_streamable_http_get_stream(tmp_path_factory):
    # клиенты Streamable HTTP (Claude) открывают SSE-поток через GET /mcp
    server = _server(tmp_path_factory)
    httpd, port = start_http_server(server, '127.0.0.1', 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/mcp',
                                    timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers.get('Content-Type') == 'text/event-stream'
            # legacy SSE-клиенты получают endpoint; streamable игнорируют его
            assert resp.readline().decode().strip() == 'event: endpoint'
            assert 'session_id=' in resp.readline().decode()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_sse_transport(tmp_path_factory):
    server = _server(tmp_path_factory)
    httpd, port = start_http_server(server, '127.0.0.1', 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/sse',
                                    timeout=5) as sse:
            assert sse.readline().decode().strip() == 'event: endpoint'
            data = sse.readline().decode().strip()
            sid = data.split('session_id=')[1]
            assert sse.readline() == b'\n'  # разделитель события
            status, _ = _post(port, {'jsonrpc': '2.0', 'id': 7,
                                     'method': 'tools/list'},
                              path=f'/messages?session_id={sid}')
            assert status == 202
            assert sse.readline().decode().strip() == 'event: message'
            payload = json.loads(sse.readline().decode().split('data: ', 1)[1])
            assert payload['id'] == 7 and 'tools' in payload['result']
    finally:
        httpd.shutdown()
        httpd.server_close()


# -- несколько баз одновременно: алиасы, переключение, параметр db -------

def _make_db(tmp_path_factory, name):
    dump = str(tmp_path_factory.mktemp('dump'))
    make_dump(dump)
    db = str(tmp_path_factory.mktemp('db') / name)
    write_db(dump, db, source_file=name + '.cf')
    return db


def _two_dbs(tmp_path_factory):
    """Основная база и «расширение» с объектом, которого нет в основной."""
    main_db = _make_db(tmp_path_factory, 'основная.sqlite')
    ext_db = _make_db(tmp_path_factory, 'расширение.sqlite')
    conn = sqlite3.connect(ext_db)
    conn.execute(
        'INSERT INTO meta_object(source_id, ord, path, type, name, type_ru, '
        'header_json) VALUES (1, 99, ?, ?, ?, ?, ?)',
        ('DataProcessor/ДопОбработка', 'DataProcessor', 'ДопОбработка',
         'Обработка', '{}'))
    conn.commit()
    conn.close()
    return main_db, ext_db


def test_multidb_routing(tmp_path_factory):
    main_db, ext_db = _two_dbs(tmp_path_factory)
    server = McpServer([main_db, ext_db])
    assert list(server.dbs) == ['основная', 'расширение']
    assert server.active == 'основная'  # первая указанная — активная
    # объекта расширения нет в активной основной базе…
    miss = _call(server, 'find_objects', mask='ДопОбработка')
    assert 'ничего не найдено' in miss['content'][0]['text']
    # …но находится по явному алиасу (без переключения активной)
    hit = _call(server, 'find_objects', mask='ДопОбработка',
                db='расширение')
    assert 'Обработка.ДопОбработка' in hit['content'][0]['text']
    assert server.active == 'основная'
    # алиасы нечувствительны к регистру
    hit = _call(server, 'object_card', path='DataProcessor/ДопОбработка',
                db='РАСШИРЕНИЕ')
    assert 'ДопОбработка' in hit['content'][0]['text']
    # переключение активной базы — инструменты работают уже без db
    use = _call(server, 'db_use', alias='расширение')
    assert 'расширение' in use['content'][0]['text']
    hit = _call(server, 'find_objects', mask='ДопОбработка')
    assert 'Обработка.ДопОбработка' in hit['content'][0]['text']
    # check_query/sql тоже смотрят в выбранную базу
    ok = _call(server, 'check_query',
               text='ВЫБРАТЬ Т.СсылкаАтрибут ИЗ Справочник.Справочник1 КАК Т',
               db='основная')
    text = ok['content'][0]['text']
    # Ответ содержит заголовок базы 'основная' и 'OK'
    assert '=== база основная' in text and 'OK' in text
    res = _call(server, 'sql',
                query="SELECT path FROM meta_object WHERE name='ДопОбработка'",
                db='расширение')
    assert 'DataProcessor/ДопОбработка' in res['content'][0]['text']


def test_db_identifier_in_responses(tmp_path_factory):
    """Каждый ответ инструмента данных содержит идентификатор базы."""
    main_db, ext_db = _two_dbs(tmp_path_factory)
    server = McpServer([main_db, ext_db])
    
    # Запрос к активной базе (основная)
    res = _call(server, 'find_objects', mask='Справочник1')['content'][0]['text']
    assert '=== база основная' in res
    assert 'Справочник.Справочник1' in res
    
    # Запрос к указанной базе (расширение)
    res = _call(server, 'find_objects', mask='ДопОбработка',
                db='расширение')['content'][0]['text']
    assert '=== база расширение' in res
    assert 'Обработка.ДопОбработка' in res
    
    # SQL тоже содержит идентификатор
    res = _call(server, 'sql', query='SELECT COUNT(*) AS n FROM meta_object')['content'][0]['text']
    assert '=== база основная' in res
    assert 'n' in res
    
    # Инструменты управления базами НЕ содержат идентификатор (они сами управляют базами)
    res = _call(server, 'db_list')['content'][0]['text']
    assert '=== база' not in res  # db_list уже содержит алиасы в своём формате


def test_db_star_queries_every_base_at_once(tmp_path_factory):
    main_db, ext_db = _two_dbs(tmp_path_factory)
    server = McpServer([main_db, ext_db])
    # db='*' — инструмент выполняется по всем открытым базам сразу
    res = _call(server, 'find_objects', mask='ДопОбработка',
                db='*')['content'][0]['text']
    assert '=== база основная' in res and '=== база расширение' in res
    assert 'Обработка.ДопОбработка' in res  # нашлась во второй базе
    assert server.active == 'основная'  # активная база не меняется
    # '*' по пустому серверу — та же ошибка, что у обычного вызова
    _call(server, 'db_close', alias='основная')
    _call(server, 'db_close', alias='расширение')
    err = _call(server, 'find_objects', mask='Тест', db='*')
    assert err.get('isError') and 'нет открытых баз' in err['content'][0]['text']


def test_db_management_tools(tmp_path_factory):
    main_db, ext_db = _two_dbs(tmp_path_factory)
    server = McpServer(main_db)  # строка тоже принимается (одна база)
    lst = _call(server, 'db_list')['content'][0]['text']
    assert '* основная' in lst and 'объектов:' in lst
    # db_open на ходу: вторая база открывается и становится активной
    opened = _call(server, 'db_open', path=ext_db, alias='расш')
    assert 'расш' in opened['content'][0]['text']
    assert server.active == 'расш'
    # повторное открытие того же файла — тот же алиас, без дублей
    again = _call(server, 'db_open', path=ext_db)
    assert 'расш' in again['content'][0]['text']
    assert list(server.dbs) == ['основная', 'расш']
    # алиас из имени файла, совпадениям — суффикс; новая база становится активной
    third = _make_db(tmp_path_factory, 'основная.sqlite')
    _call(server, 'db_open', path=third)
    assert 'основная_2' in server.dbs and server.active == 'основная_2'
    # db_list помечает активную
    lst = _call(server, 'db_list')['content'][0]['text']
    assert '* основная_2 —' in lst
    assert ' основная —' in lst and ' расш —' in lst
    # неизвестный алиас — ошибка со списком открытых
    bad = _call(server, 'db_use', alias='неттакой')
    assert bad.get('isError') and 'не открыта' in bad['content'][0]['text']
    # вернули активную и закрываем: сначала неактивную, потом активную
    _call(server, 'db_use', alias='расш')
    closed = _call(server, 'db_close', alias='основная_2')
    assert 'закрыта' in closed['content'][0]['text']
    closed = _call(server, 'db_close')  # активная ('расш')
    assert 'закрыта' in closed['content'][0]['text']
    assert server.active == 'основная'
    # закрыли последнюю — инструменты сообщают открыть базу
    _call(server, 'db_close')
    assert server.dbs == {} and server.active is None
    err = _call(server, 'find_objects', mask='Тест')
    assert err.get('isError') and 'нет открытых баз' in err['content'][0]['text']


def test_db_open_rejects_bad_files(tmp_path_factory):
    server = McpServer(_make_db(tmp_path_factory, 'т.sqlite'))
    miss = _call(server, 'db_open', path=str(tmp_path_factory.mktemp('x') / 'нет.db'))
    assert miss.get('isError') and 'не найден' in miss['content'][0]['text']
    # sqlite-файл без таблицы meta_object — не база знаний
    foreign = str(tmp_path_factory.mktemp('y') / 'чужая.db')
    conn = sqlite3.connect(foreign)
    conn.execute('CREATE TABLE t (x)')
    conn.close()
    bad = _call(server, 'db_open', path=foreign)
    assert bad.get('isError') and 'не база знаний' in bad['content'][0]['text']
    assert foreign not in [d['path'] for d in server.dbs.values()]
    # занятый алиас
    busy = _call(server, 'db_open',
                 path=_make_db(tmp_path_factory, 'вторая.sqlite'),
                 alias='т')
    assert busy.get('isError') and 'занят' in busy['content'][0]['text']


def test_resolve_dbs(tmp_path, capsys):
    one = tmp_path / 'одна.db'
    two = tmp_path / 'две.db'
    one.write_bytes(b'x')
    two.write_bytes(b'x')
    assert mcp_server.resolve_dbs([str(one), str(two)]) == [str(one), str(two)]
    with pytest.raises(SystemExit) as exc:
        mcp_server.resolve_dbs([str(one), str(tmp_path / 'нет.db')])
    assert exc.value.code == 2
    assert 'не найден' in capsys.readouterr().err


# -- запуск без пути: проверка и автопоиск базы --------------------------

def test_resolve_db_explicit(tmp_path, capsys):
    db = tmp_path / 't.sqlite'
    db.write_bytes(b'x')
    assert resolve_db(str(db)) == str(db)
    with pytest.raises(SystemExit) as exc:
        resolve_db(str(tmp_path / 'нет.sqlite'))
    assert exc.value.code == 2
    assert 'не найден' in capsys.readouterr().err


def test_resolve_db_last_db(tmp_path, monkeypatch):
    # last_db из конфига имеет приоритет над автопоиском
    db = tmp_path / 'база.sqlite'
    db.write_bytes(b'x')
    cfg = tmp_path / 'config.json'
    cfg.write_text(json.dumps({'last_db': str(db)}), encoding='utf-8')
    monkeypatch.setattr('confdb.config.CONFIG_PATH', str(cfg))
    empty = tmp_path / 'empty'
    empty.mkdir()
    monkeypatch.chdir(empty)
    assert resolve_db(None) == str(db)


def test_resolve_db_autofind(tmp_path, monkeypatch, capsys):
    # без конфига (свежая установка) база находится обходом каталогов
    monkeypatch.setattr('confdb.config.CONFIG_PATH', str(tmp_path / 'нет.json'))
    monkeypatch.setattr(mcp_server, '_scan_roots', lambda: [str(tmp_path)])
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        resolve_db(None)
    assert exc.value.code == 2
    assert 'не найдена' in capsys.readouterr().err
    (tmp_path / 'db').mkdir()
    one = tmp_path / 'db' / 'одна.sqlite'
    one.write_bytes(b'x')
    assert resolve_db(None) == str(one)
    two = tmp_path / 'вторая.db'
    two.write_bytes(b'x')
    with pytest.raises(SystemExit) as exc:
        resolve_db(None)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert str(one) in err and str(two) in err


def test_find_db_candidates_dedup(tmp_path, monkeypatch):
    # db/ и _out/ просматриваются, повторы и каталоги не попадают в список
    monkeypatch.setattr(mcp_server, '_scan_roots', lambda: [str(tmp_path)])
    (tmp_path / 'db').mkdir()
    (tmp_path / '_out').mkdir()
    (tmp_path / 'a.db').write_bytes(b'x')
    (tmp_path / 'db' / 'b.sqlite').write_bytes(b'x')
    (tmp_path / '_out' / 'c.db').write_bytes(b'x')
    (tmp_path / 'каталог.db').mkdir()
    found = mcp_server.find_db_candidates()
    assert found == [str(tmp_path / 'a.db'),
                     str(tmp_path / 'db' / 'b.sqlite'),
                     str(tmp_path / '_out' / 'c.db')]
