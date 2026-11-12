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


def _source_db(path, src_name, sha, root_name=None, root_type='Configuration'):
    conn = sqlite3.connect(str(path))
    conn.execute('CREATE TABLE source(id INTEGER PRIMARY KEY, file TEXT,'
                 ' created TEXT, file_sha256 TEXT, file_size INTEGER,'
                 ' root_name TEXT, root_type TEXT)')
    conn.execute('INSERT INTO source(file, created, file_sha256, file_size,'
                 ' root_name, root_type) VALUES (?,?,?,?,?,?)',
                 (src_name, '2026-01-01', sha, 1, root_name, root_type))
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
