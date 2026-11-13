"""Тесты общего слоя интерфейсов (ui_core) и веб-интерфейса (webui)."""
import http.client
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

import pytest

from confdb import webui
from confdb.ui_core import UiCore, ServerRunner


def _source_db(path, src_name, sha, root_name=None, root_type='Configuration',
               extractor_version=None):
    from confdb import __version__
    if extractor_version is None:
        extractor_version = __version__
    conn = sqlite3.connect(str(path))
    conn.execute('CREATE TABLE source(id INTEGER PRIMARY KEY, file TEXT,'
                 ' created TEXT, file_sha256 TEXT, file_size INTEGER,'
                 ' root_name TEXT, root_type TEXT, extractor_version TEXT)')
    conn.execute('INSERT INTO source(file, created, file_sha256, file_size,'
                 ' root_name, root_type, extractor_version) VALUES (?,?,?,?,?,?,?)',
                 (src_name, '2026-01-01', sha, 1, root_name, root_type,
                  extractor_version))
    conn.commit()
    conn.close()
    return str(path)


def _real_db(tmp_path, name):
    from confdb.db.writer import write_db
    from test_writer import make_dump
    dump = str(tmp_path / ('dump_' + name))
    make_dump(dump)
    db = str(tmp_path / name)
    write_db(dump, db, source_file=name + '.cf')
    return db


@pytest.fixture
def core(monkeypatch):
    """UiCore без чтения/записи реального ~/.confdb/config.json."""
    monkeypatch.setattr('confdb.ui_core._load_config', lambda: {})
    monkeypatch.setattr('confdb.ui_core._save_config', lambda c: None)
    monkeypatch.setattr('confdb.ui_core.bench_workers', lambda: None)
    return UiCore()


# ---------- UiCore: настройки и группы ----------

def test_core_options_roundtrip(core):
    core.set_options({'workers': 4, 'build_fts': False, 'cf_dir': 'D:\\cf'})
    assert core.workers == 4 and core.build_fts is False
    assert core.options_dict()['cf_dir'] == 'D:\\cf'
    with pytest.raises(ValueError):
        core.set_options({'workers': 0})


def test_core_groups_crud(core):
    core.set_group('БП', ['a.db', 'b.db'])
    assert core.groups == {'БП': ['a.db', 'b.db']}
    core.set_group('БП', [])           # пустой список — удаление
    assert core.groups == {}
    core.set_group('ЗУП', ['z.db'])
    core.delete_group('ЗУП')
    assert core.groups == {}
    with pytest.raises(ValueError):
        core.set_group('  ', ['x.db'])


def test_core_scan_statuses(core, tmp_path, monkeypatch):
    from confdb.extract import file_sha256
    cf_dir = tmp_path / 'cf'
    cf_dir.mkdir()
    out_dir = tmp_path / 'out'
    monkeypatch.setattr('confdb.ui_core.out_defaults',
                        lambda src: (str(out_dir / (os.path.splitext(
                            os.path.basename(src))[0] + '.sqlite')), str(out_dir)))
    act = cf_dir / 'Актуальная.cf'
    new = cf_dir / 'Новая.cf'
    act.write_bytes(b'act')
    new.write_bytes(b'new')
    db_dir = tmp_path / 'dbs'
    db_dir.mkdir()
    db_act = _source_db(db_dir / 'act.sqlite', 'Актуальная.cf', file_sha256(str(act)),
                        root_name='ТестоваяКонфигурация')
    core.cf_dir = str(cf_dir)
    core.db_dirs = [str(db_dir)]
    recs = {os.path.basename(r['src']): r for r in core.scan_cf(log=lambda *a: None)}
    assert recs['Актуальная.cf']['status'] == 'current'
    assert recs['Актуальная.cf']['db'] == db_act
    assert recs['Актуальная.cf']['ext'] == 'cf'
    assert recs['Актуальная.cf']['name'] == 'ТестоваяКонфигурация'
    assert recs['Новая.cf']['status'] == 'new'
    assert recs['Новая.cf']['name'] is None


def test_core_server_llm_log_not_running(core):
    assert core.server_llm_log() == []


def test_core_server_llm_log_returns_server_lines(core):
    class _FakeServer:
        def llm_log_lines(self):
            return ['LLM → db_list {}', 'ответ сервера']

    class _FakeRunner:
        running = True
        server = _FakeServer()

    core.runner = _FakeRunner()
    assert core.server_llm_log() == ['LLM → db_list {}', 'ответ сервера']


# ---------- журнал LLM в отдельной базе ----------

@pytest.fixture
def llm_cfg(monkeypatch, tmp_path):
    """Конфиг журнала в памяти + путь базы журнала во временном каталоге."""
    path = str(tmp_path / 'llm.sqlite')
    store = {'options': {'llm_log_db': path}}
    monkeypatch.setattr('confdb.llm_log.load_config', lambda: store)

    def save(config):
        snapshot = dict(config)   # config может быть тем же словарём store
        store.clear()
        store.update(snapshot)

    monkeypatch.setattr('confdb.llm_log.save_config', save)
    return path


def test_core_llm_log_toggle_persists(core, llm_cfg):
    from confdb.ui_core import set_llm_log_enabled
    set_llm_log_enabled(True, llm_cfg)
    status = core.set_llm_log_enabled(False)
    assert status['enabled'] is False and status['path'] == llm_cfg
    status = core.set_llm_log_enabled(True)
    assert status['enabled'] is True and status['running'] is False


def test_core_llm_log_list_item_clear(core, llm_cfg):
    from confdb import llm_log
    from confdb.ui_core import set_llm_log_enabled
    set_llm_log_enabled(True, llm_cfg)
    writer = llm_log.LlmLogWriter(llm_cfg)
    writer.append('find_objects', {'mask': 'Товар'}, 'ответ сервера',
                  duration_ms=5, db='БП')
    writer.close()
    result = core.llm_log_calls()
    assert result['total'] == 1
    item = core.llm_log_call(result['rows'][0]['id'])
    assert item['response'] == 'ответ сервера'
    assert item['args'] == '{"mask": "Товар"}'
    status = core.llm_log_status()
    assert status['count'] == 1 and status['enabled'] is True
    assert core.llm_log_clear() == 1
    assert core.llm_log_calls()['total'] == 0


def test_core_llm_log_toggle_applies_to_running_server(core, llm_cfg):
    from confdb.ui_core import set_llm_log_enabled
    set_llm_log_enabled(False, llm_cfg)

    class _FakeServer:
        def __init__(self):
            self.calls = []

        def configure_llm_log(self, enabled, path=None):
            self.calls.append((enabled, path))

    class _FakeRunner:
        running = True
        server = _FakeServer()

    core.runner = _FakeRunner()
    core.set_llm_log_enabled(True)
    assert core.runner.server.calls == [(True, llm_cfg)]


def test_server_runner_applies_llm_log_config(tmp_path, monkeypatch, llm_cfg):
    """Старт сервера из UI включает запись журнала, если она включена в конфиге."""
    from confdb.ui_core import set_llm_log_enabled
    monkeypatch.setattr('confdb.mcp_server.resolve_syntax_db', lambda *a: None)
    set_llm_log_enabled(True, llm_cfg)
    db = _real_db(tmp_path, 'журнал_старт.sqlite')
    runner = ServerRunner()
    runner.start([db], [], port=0)
    try:
        assert runner.server.llm_writer is not None
        assert runner.server.llm_writer.path == llm_cfg
    finally:
        runner.stop()
    os.remove(db)


def test_http_llm_log_end_to_end(tmp_path, monkeypatch, llm_cfg):
    """Сквозной: включили запись в веб-API → запустили сервер → вызов LLM
    по HTTP → вызов и полный ответ видны в журнале через веб-API."""
    db = _real_db(tmp_path, 'журнал_e2e.sqlite')
    with _http(monkeypatch, tmp_path) as (web, port):
        code, r = _req(port, 'POST', '/api/llmlog/toggle', {'enabled': True})
        assert code == 200 and r['status']['enabled'] is True
        code, r = _req(port, 'POST', '/api/server/start',
                       {'dbs': [db], 'groups': [], 'port': 0})
        assert code == 200
        mcp_port = r['port']
        # запрос LLM к MCP-серверу (как это делает MCP-клиент)
        conn = http.client.HTTPConnection('127.0.0.1', mcp_port, timeout=10)
        msg = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
               'params': {'name': 'db_list', 'arguments': {}}}
        conn.request('POST', '/mcp', json.dumps(msg),
                     {'Content-Type': 'application/json'})
        resp = json.loads(conn.getresponse().read().decode('utf-8'))
        conn.close()
        answer = resp['result']['content'][0]['text']
        # журнал через веб-API
        code, r = _req(port, 'GET', '/api/llmlog')
        assert code == 200 and r['total'] == 1
        row = r['rows'][0]
        assert row['tool'] == 'db_list' and row['is_error'] is False
        assert r['status']['enabled'] is True
        code, item = _req(port, 'GET', '/api/llmlog/item?id=%d' % row['id'])
        assert code == 200 and item['response'] == answer
        # фильтры
        code, r = _req(port, 'GET', '/api/llmlog?tool=db_list')
        assert r['total'] == 1
        code, r = _req(port, 'GET', '/api/llmlog?errors=1')
        assert r['total'] == 0
        from urllib.parse import quote
        code, r = _req(port, 'GET',
                       '/api/llmlog?search=' + quote('несуществующий'))
        assert r['total'] == 0
        # очистка
        code, r = _req(port, 'POST', '/api/llmlog/clear', {})
        assert code == 200 and r['deleted'] == 1
        assert r['status']['count'] == 0
        _req(port, 'POST', '/api/server/stop', {})
    os.remove(db)


def test_http_llm_log_pagination_15(tmp_path, monkeypatch, llm_cfg):
    """Постраничный вывод журнала: по 15 записей, offset листает страницы."""
    from confdb import llm_log
    writer = llm_log.LlmLogWriter(llm_cfg)
    for i in range(35):
        writer.append('find_objects', {'mask': f'товар{i}'}, f'ответ {i}')
    writer.close()
    with _http(monkeypatch, tmp_path) as (web, port):
        # страница 1: 15 записей, всего 35
        code, r = _req(port, 'GET', '/api/llmlog?limit=15&offset=0')
        assert code == 200 and r['total'] == 35 and len(r['rows']) == 15
        first_ids = [row['id'] for row in r['rows']]
        # страница 2: следующие 15, без пересечений с первой
        code, r = _req(port, 'GET', '/api/llmlog?limit=15&offset=15')
        assert code == 200 and len(r['rows']) == 15
        second_ids = [row['id'] for row in r['rows']]
        assert set(first_ids).isdisjoint(second_ids)
        # страница 3: остаток 5
        code, r = _req(port, 'GET', '/api/llmlog?limit=15&offset=30')
        assert code == 200 and len(r['rows']) == 5
        assert r['total'] == 35
        # новые сверху: первая страница начинается с последней записи
        assert first_ids[0] == 35
        # в HTML есть нумерованная пагинация
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        conn.request('GET', '/')
        html = conn.getresponse().read().decode('utf-8')
        conn.close()
        assert 'id="llm-pager"' in html and 'LLM_PAGE_SIZE = 15' in html


def test_core_extract_calls_pipeline(core, tmp_path, monkeypatch):
    src = tmp_path / 'Конфигурация.cf'
    src.write_bytes(b'data')
    db = str(tmp_path / 'out.sqlite')
    calls = []

    def fake_extract(s, **kw):
        calls.append((s, kw))
        return {'db': kw['db_path'], 'elapsed': '0:00:01', 'db_rows': 1}

    monkeypatch.setattr('confdb.ui_core._extract_cf', fake_extract)
    stats = core.extract(str(src), db)
    assert stats['db'] == db and len(calls) == 1
    assert calls[0][1]['source_sha256']          # отпечаток посчитан и передан
    assert core.last_db == db and db in core.recent_db


def test_core_extract_skips_same_source(core, tmp_path, monkeypatch):
    from confdb.extract import file_sha256
    src = tmp_path / 'ТаЖе.cf'
    src.write_bytes(b'same')
    db = _source_db(tmp_path / 'same.sqlite', 'ТаЖе.cf', file_sha256(str(src)))
    monkeypatch.setattr('confdb.ui_core._extract_cf',
                        lambda *a, **k: pytest.fail('не должно извлекаться'))
    stats = core.extract(str(src), db)
    assert stats['skipped'] is True


# ---------- ServerRunner ----------

def test_server_runner_start_stop(tmp_path, monkeypatch):
    monkeypatch.setattr('confdb.mcp_server.resolve_syntax_db', lambda *a: None)
    db = _real_db(tmp_path, 'сервер.sqlite')
    runner = ServerRunner()
    port = runner.start([db], [], port=0)
    assert runner.running and port > 0
    assert runner.server.active is not None
    runner.stop()
    assert not runner.running
    os.remove(db)        # соединения закрыты — файл свободен (Windows)


# ---------- веб-интерфейс: HTTP API ----------

@contextmanager
def _http(monkeypatch, tmp_path):
    monkeypatch.setattr('confdb.ui_core._load_config', lambda: {})
    monkeypatch.setattr('confdb.ui_core._save_config', lambda c: None)
    monkeypatch.setattr('confdb.ui_core.bench_workers', lambda: None)
    monkeypatch.setattr('confdb.mcp_server.resolve_syntax_db', lambda *a: None)
    web = webui.WebUi()
    httpd = webui.ThreadingHTTPServer(('127.0.0.1', 0), webui.make_handler(web))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield web, httpd.server_address[1]
    finally:
        web.core.stop_server()
        httpd.shutdown()
        httpd.server_close()


def _req(port, method, path, body=None):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    payload = json.dumps(body) if body is not None else None
    headers = {'Content-Type': 'application/json'} if payload else {}
    conn.request(method, path, payload, headers)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode('utf-8'))
    conn.close()
    return resp.status, data


def test_http_state_options_and_html(tmp_path, monkeypatch):
    with _http(monkeypatch, tmp_path) as (web, port):
        code, state = _req(port, 'GET', '/api/state')
        assert code == 200 and state['version'] and 'options' in state
        code, r = _req(port, 'POST', '/api/options', {'workers': 3})
        assert code == 200 and r['options']['workers'] == 3
        # HTML-приложение отдаётся
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        conn.request('GET', '/')
        resp = conn.getresponse()
        html = resp.read().decode('utf-8')
        conn.close()
        assert resp.status == 200 and 'confdb' in html and 'MCP-сервер' in html


def test_http_groups_crud(tmp_path, monkeypatch):
    with _http(monkeypatch, tmp_path) as (web, port):
        code, r = _req(port, 'POST', '/api/groups', {'name': 'БП', 'paths': ['a.db']})
        assert code == 200 and r['groups'] == {'БП': ['a.db']}
        code, r = _req(port, 'POST', '/api/groups/delete', {'name': 'БП'})
        assert code == 200 and r['groups'] == {}


def test_http_server_start_stop(tmp_path, monkeypatch):
    db = _real_db(tmp_path, 'веб.sqlite')
    with _http(monkeypatch, tmp_path) as (web, port):
        code, r = _req(port, 'POST', '/api/server/start',
                       {'dbs': [db], 'groups': [], 'port': 0})
        assert code == 200 and r['status']['running'] is True
        assert r['status']['dbs'][0]['path'] == db
        code, r = _req(port, 'POST', '/api/server/stop', {})
        assert code == 200 and r['status']['running'] is False
    os.remove(db)


def test_http_add_config_group_to_running_server(tmp_path, monkeypatch):
    """«Добавить группы баз в сервер»: группа из конфига открывается в работающем
    сервере, прежние базы остаются (замена «открыть ещё базу» по пути)."""
    db1 = _real_db(tmp_path, 'основная.sqlite')
    db2 = _real_db(tmp_path, 'расширение.sqlite')
    with _http(monkeypatch, tmp_path) as (web, port):
        _req(port, 'POST', '/api/groups', {'name': 'БП', 'paths': [db2]})
        _req(port, 'POST', '/api/server/start',
             {'dbs': [db1], 'groups': [], 'port': 0})
        code, r = _req(port, 'POST', '/api/server/apply_group',
                       {'name': 'БП', 'replace': False})
        assert code == 200
        paths = {d['path'] for d in r['status']['dbs']}
        assert paths == {db1, db2}                 # прежняя база осталась
        assert any(g['name'] == 'БП' for g in r['status']['groups'])
        _req(port, 'POST', '/api/server/stop', {})
    os.remove(db1)
    os.remove(db2)


def test_db_meta_classification(tmp_path):
    """Классификация базы: тип исходника (cf) + имя/тип корневого объекта."""
    from confdb.ui_core import db_meta
    db = _real_db(tmp_path, 'классификация.sqlite')
    meta = db_meta(db)
    assert meta['ext'] == 'cf'                  # исходник — .cf
    assert meta['root_type'] == 'Configuration'
    assert meta['name']                         # имя корневого объекта есть
    # несуществующая база — пустая классификация, без ошибки
    assert db_meta(str(tmp_path / 'нет.sqlite'))['ext'] == ''


def test_http_state_includes_db_meta(tmp_path, monkeypatch):
    db = _real_db(tmp_path, 'мета.sqlite')
    with _http(monkeypatch, tmp_path) as (web, port):
        web.core.db_dirs = [str(tmp_path)]
        code, state = _req(port, 'GET', '/api/state')
        assert code == 200
        assert state['db_meta'][db]['ext'] == 'cf'
        assert state['db_meta'][db]['name']
    os.remove(db)


def test_http_open_db_to_running_server(tmp_path, monkeypatch):
    """«Добавить отдельную базу»: выбранный файл SQLite открывается в работающем
    сервере как отдельная база, прежние остаются."""
    db1 = _real_db(tmp_path, 'основная.sqlite')
    db2 = _real_db(tmp_path, 'дополнительная.sqlite')
    with _http(monkeypatch, tmp_path) as (web, port):
        _req(port, 'POST', '/api/server/start',
             {'dbs': [db1], 'groups': [], 'port': 0})
        code, r = _req(port, 'POST', '/api/server/open', {'path': db2})
        assert code == 200 and r['alias']
        paths = {d['path'] for d in r['status']['dbs']}
        assert paths == {db1, db2}
        _req(port, 'POST', '/api/server/stop', {})
    os.remove(db1)
    os.remove(db2)


def test_http_scan_task(tmp_path, monkeypatch):
    from confdb.extract import file_sha256
    cf_dir = tmp_path / 'cf'
    cf_dir.mkdir()
    (cf_dir / 'Новая.cf').write_bytes(b'new')
    monkeypatch.setattr('confdb.ui_core.out_defaults',
                        lambda src: (str(tmp_path / 'out.sqlite'), str(tmp_path)))
    with _http(monkeypatch, tmp_path) as (web, port):
        _req(port, 'POST', '/api/options', {'cf_dir': str(cf_dir)})
        code, r = _req(port, 'POST', '/api/scan', {})
        assert code == 200 and r['task']['status'] == 'running'
        deadline = time.time() + 10
        while time.time() < deadline:
            _, t = _req(port, 'GET', '/api/task')
            if t['task'] and t['task']['status'] != 'running':
                break
            time.sleep(0.05)
        assert t['task']['status'] == 'done'
        assert t['task']['result'][0]['status'] == 'new'
