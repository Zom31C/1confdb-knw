# -*- coding: utf-8 -*-
"""Git-источники баз знаний (repos.py + UiCore.repos_*).

Реестр и определение источника проверяются без git; полный цикл
(клон → сборка → частичное обновление → пересборка) — на локальных
git-репозиториях с синтетической выгрузкой из фикстуры паритета.
Крупные реальные .cf в тестах не используются.
"""
import os
import shutil
import sqlite3
import subprocess
import threading

import pytest

from confdb import repos
from confdb.__main__ import build_parser
from confdb.ui_core import UiCore
from test_parity import make_xml_dump

GIT = shutil.which('git')
needs_git = pytest.mark.skipif(GIT is None, reason='git не установлен')


def _git(cwd, *args):
    subprocess.run(
        ['git', '-c', 'user.email=test@example.com', '-c', 'user.name=test',
         *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        encoding='utf-8', errors='replace')


@pytest.fixture
def home(monkeypatch, tmp_path):
    """Изолированный конфиг и каталоги клонов/баз (не трогаем ~/.confdb)."""
    monkeypatch.setattr('confdb.config.CONFIG_PATH',
                        str(tmp_path / 'config.json'))
    monkeypatch.setattr(repos, 'clone_root', lambda: str(tmp_path / 'repos'))
    monkeypatch.setattr(repos, 'db_root', lambda: str(tmp_path / 'dbs'))
    return tmp_path


@pytest.fixture
def origin(home):
    """Локальный git-репозиторий с синтетической выгрузкой конфигурации."""
    if GIT is None:
        pytest.skip('git не установлен')
    root = home / 'origin'
    root.mkdir()
    make_xml_dump(str(root))
    _git(root, 'init', '-b', 'main')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'исходная выгрузка')
    return root


def _add_dump(home, origin, **kw):
    return repos.add_source(str(origin), source_id='test', **kw)


def _sync(core, **kw):
    kw.setdefault('build_fts', False)
    kw.setdefault('log', lambda *a: None)
    return core.repos_sync(**kw)


# ---------- реестр ----------

def test_add_source_defaults(home):
    rec = repos.add_source('https://example.com/org/MyConfig.git')
    assert rec['id'] == 'myconfig'
    assert rec['mode'] == 'auto' and rec['watch'] is False
    assert rec['db'] == os.path.join(repos.db_root(), 'myconfig.sqlite')
    assert repos.load_sources()[0]['url'] == 'https://example.com/org/MyConfig.git'
    # тот же URL+путь+ветка второй раз нельзя
    with pytest.raises(ValueError):
        repos.add_source('https://example.com/org/MyConfig.git', source_id='other')
    # то же имя нельзя
    with pytest.raises(ValueError):
        repos.add_source('https://example.com/org/Другой.git', source_id='myconfig')


def test_add_source_validation(home):
    with pytest.raises(ValueError):
        repos.add_source('')
    with pytest.raises(ValueError):
        repos.add_source('https://x/y.git', mode='magic')
    with pytest.raises(ValueError):
        repos.add_source('https://x/y.git', source_id='../побег')
    rec = repos.add_source('https://x/y.git', source_id='мой-источник_1',
                           branch='dev', path='dump\\конф', watch=True)
    assert rec['branch'] == 'dev'
    assert rec['path'] == 'dump/конф'      # слеши нормализованы
    assert rec['watch'] is True


def test_make_id():
    assert repos.make_id('https://github.com/org/Repo.git') == 'repo'
    assert repos.make_id('git@host:org/My_Config.git/') == 'my_config'
    assert repos.make_id('https://x/%%%') == 'repo'


def test_update_source(home):
    repos.add_source('https://x/y.git', source_id='s')
    rec = repos.update_source('s', branch='dev', mode='full', watch=True,
                              path='sub/dir')
    assert rec['branch'] == 'dev' and rec['mode'] == 'full'
    assert rec['watch'] is True and rec['path'] == 'sub/dir'
    with pytest.raises(ValueError):
        repos.update_source('s', url='https://другой')
    with pytest.raises(ValueError):
        repos.update_source('s', mode='magic')
    with pytest.raises(ValueError):
        repos.update_source('нет-такого', watch=True)


def test_remove_source_purge(home):
    rec = repos.add_source('https://x/y.git', source_id='s')
    os.makedirs(repos.clone_dir(rec))
    repos.remove_source('s', purge=True)
    assert repos.load_sources() == []
    assert not os.path.isdir(repos.clone_dir(rec))


# ---------- определение источника ----------

def test_detect_dump_root(tmp_path):
    (tmp_path / 'ConfigDumpInfo.xml').write_text('<x/>', encoding='utf-8')
    assert repos.detect_source(str(tmp_path)) == ('dump', '')


def test_detect_single_cf(tmp_path):
    (tmp_path / 'Конфигурация.cf').write_bytes(b'x')
    assert repos.detect_source(str(tmp_path)) == ('cf', 'Конфигурация.cf')


def test_detect_subdirs(tmp_path):
    sub = tmp_path / 'dump'
    sub.mkdir()
    (sub / 'ConfigDumpInfo.xml').write_text('<x/>', encoding='utf-8')
    assert repos.detect_source(str(tmp_path)) == ('dump', 'dump')
    other = tmp_path / 'other'
    other.mkdir()
    ext = other / 'ext'
    ext.mkdir()
    (ext / 'Расширение.cfe').write_bytes(b'x')
    assert repos.detect_source(str(other)) == ('cfe', 'ext/Расширение.cfe')


def test_detect_ambiguous_and_empty(tmp_path):
    (tmp_path / 'a.cf').write_bytes(b'x')
    (tmp_path / 'b.epf').write_bytes(b'x')
    with pytest.raises(ValueError, match='несколько кандидатов'):
        repos.detect_source(str(tmp_path))
    empty = tmp_path / 'empty'
    empty.mkdir()
    with pytest.raises(ValueError, match='не найдено'):
        repos.detect_source(str(empty))


def test_detect_explicit_path(tmp_path):
    sub = tmp_path / 'вложенная'
    sub.mkdir()
    (sub / 'ConfigDumpInfo.xml').write_text('<x/>', encoding='utf-8')
    (tmp_path / 'обработка.epf').write_bytes(b'x')
    assert repos.detect_source(str(tmp_path), 'вложенная') == ('dump', 'вложенная')
    assert repos.detect_source(str(tmp_path), 'обработка.epf') == ('epf', 'обработка.epf')
    with pytest.raises(ValueError, match='не каталог выгрузки'):
        repos.detect_source(str(tmp_path), 'нет-такого')


# ---------- git: клон, проверка, слежение ----------

@needs_git
def test_pull_clone_and_nochange(home, origin):
    rec = _add_dump(home, origin)
    first = repos.pull(rec, log=lambda *a: None)
    assert first['action'] == 'clone' and first['changed'] is True
    assert len(first['commit']) == 40 and first['branch'] == 'main'
    assert os.path.isdir(os.path.join(repos.clone_dir(rec), '.git'))
    second = repos.pull(rec, log=lambda *a: None)
    assert second['action'] == 'none' and second['changed'] is False
    assert second['commit'] == first['commit']


@needs_git
def test_remote_check(home, origin):
    rec = _add_dump(home, origin)
    repos.pull(rec, log=lambda *a: None)
    assert repos.remote_check(rec)['changed'] is False
    module = origin / 'Catalogs' / 'Справочник1' / 'Ext' / 'ObjectModule.bsl'
    module.write_text(module.read_text(encoding='utf-8') + '\n// правка\n',
                      encoding='utf-8')
    _git(origin, 'add', '-A')
    _git(origin, 'commit', '-m', 'правка')
    check = repos.remote_check(rec)
    assert check['changed'] is True and check['remote'] != check['local']
    # зеркало не тронуто проверкой
    assert repos._head_commit(repos.clone_dir(rec)) == check['local']


@needs_git
def test_sync_dump_full_then_partial(home, origin):
    rec = _add_dump(home, origin)
    core = UiCore()
    results = _sync(core, ids=['test'])
    assert results[0]['ok'] is True
    assert results[0]['action'] == 'clone' and results[0]['build'] == 'full'
    # .git клона не должен давать предупреждений сканера выгрузки
    assert results[0]['stats'].get('warnings') == 0
    assert os.path.isfile(rec['db'])
    conn = sqlite3.connect(rec['db'])
    try:
        assert conn.execute('SELECT COUNT(*) FROM meta_object').fetchone()[0] == 11
    finally:
        conn.close()
    # повтор без изменений — сборка не нужна
    results = _sync(core, ids=['test'])
    assert results[0]['ok'] is True and results[0]['build'] == 'skip'
    assert repos.load_sources()[0]['last_result'] == 'no-change'
    # правка модуля в origin → частичное обновление только этой гранулы
    marker = '// маркер-изменения-12345'
    module = origin / 'Catalogs' / 'Справочник1' / 'Ext' / 'ObjectModule.bsl'
    module.write_text(module.read_text(encoding='utf-8') + '\n' + marker + '\n',
                      encoding='utf-8')
    _git(origin, 'add', '-A')
    _git(origin, 'commit', '-m', 'правка модуля')
    results = _sync(core, ids=['test'])
    r = results[0]
    assert r['ok'] is True and r['action'] == 'pull' and r['build'] == 'partial'
    assert r['stats']['changed_granules']
    conn = sqlite3.connect(rec['db'])
    try:
        assert conn.execute('SELECT COUNT(*) FROM module WHERE body LIKE ?',
                            ('%' + marker + '%',)).fetchone()[0] == 1
        # остальные объекты не пересобирались
        assert conn.execute('SELECT COUNT(*) FROM meta_object').fetchone()[0] == 11
    finally:
        conn.close()
    assert repos.load_sources()[0]['last_result'] == 'updated'


@needs_git
def test_sync_force_rebuild(home, origin):
    _add_dump(home, origin)
    core = UiCore()
    assert _sync(core, ids=['test'])[0]['build'] == 'full'
    r = _sync(core, ids=['test'], force=True)[0]
    assert r['ok'] is True and r['action'] == 'none' and r['build'] == 'full'


@needs_git
def test_sync_mode_full(home, origin):
    _add_dump(home, origin, mode='full')
    core = UiCore()
    _sync(core, ids=['test'])
    module = origin / 'Catalogs' / 'Справочник1' / 'Ext' / 'ObjectModule.bsl'
    module.write_text(module.read_text(encoding='utf-8') + '\n// ещё\n',
                      encoding='utf-8')
    _git(origin, 'add', '-A')
    _git(origin, 'commit', '-m', 'правка')
    r = _sync(core, ids=['test'])[0]
    assert r['ok'] is True and r['build'] == 'full'
    assert 'changed_granules' not in r['stats']


@needs_git
def test_schema_revision_forces_rebuild(home, origin, monkeypatch):
    _add_dump(home, origin)
    core = UiCore()
    _sync(core, ids=['test'])
    # другая ревизия схемы распаковщика → пересборка без изменений git
    monkeypatch.setattr('confdb.ui_core.__version__', '99.99.0')
    r = _sync(core, ids=['test'])[0]
    assert r['ok'] is True and r['action'] == 'none' and r['build'] == 'full'


@needs_git
def test_sync_error_recorded(home, origin):
    rec = _add_dump(home, origin, path='нет-такого-пути')
    core = UiCore()
    r = _sync(core, ids=['test'])[0]
    assert r['ok'] is False and r['error']
    stored = repos.load_sources()[0]
    assert stored['last_result'] == 'error' and stored['last_error']
    # клон при этом создан и коммит записан
    assert stored['commit'] and os.path.isdir(repos.clone_dir(rec))


@needs_git
def test_repos_list_enriched(home, origin):
    _add_dump(home, origin)
    core = UiCore()
    row = core.repos_list()[0]
    assert row['clone_exists'] is False and row['db_exists'] is False
    _sync(core, ids=['test'])
    row = core.repos_list()[0]
    assert row['clone_exists'] is True and row['db_exists'] is True
    assert row['kind'] == 'dump'
    # база git-источника видна в кандидатах интерфейса
    assert row['db'] in core.db_candidates()


def test_watch_loop_once_and_busy(home):
    calls = []
    records = [{'id': 'a'}, {'id': 'b'}]
    repos.watch_loop(lambda: records, lambda sid: calls.append(sid) or {},
                     once=True, log=lambda *a: None)
    assert calls == ['a', 'b']
    # занятая задача пропускает цикл
    calls.clear()
    repos.watch_loop(lambda: records, lambda sid: calls.append(sid) or {},
                     once=True, log=lambda *a: None, busy_fn=lambda: True)
    assert calls == []
    # ошибка одного источника не роняет цикл
    calls.clear()

    def flaky(sid):
        calls.append(sid)
        if sid == 'a':
            raise ValueError('сбой')
        return {}

    repos.watch_loop(lambda: records, flaky, once=True, log=lambda *a: None)
    assert calls == ['a', 'b']
    # stop_event останавливает до первого источника
    stop = threading.Event()
    stop.set()
    calls.clear()
    repos.watch_loop(lambda: records, lambda sid: calls.append(sid) or {},
                     log=lambda *a: None, stop_event=stop)
    assert calls == []


def test_cli_repos_parser():
    args = build_parser().parse_args(
        ['repos', 'add', 'https://x/y.git', '--id', 's', '--branch', 'dev',
         '--path', 'dump', '--mode', 'full', '--watch', '--sync'])
    assert args.repos_cmd == 'add' and args.watch and args.sync
    assert args.mode == 'full' and args.branch == 'dev'
    args = build_parser().parse_args(['repos', 'sync', 'a', 'b', '--force'])
    assert args.ids == ['a', 'b'] and args.force
    args = build_parser().parse_args(['repos', 'watch', '--interval', '60',
                                      '--once', '--all'])
    assert args.interval == 60 and args.once and args.all
    args = build_parser().parse_args(['repos', 'remove', 's', '--purge'])
    assert args.purge