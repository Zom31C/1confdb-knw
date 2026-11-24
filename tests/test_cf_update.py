# -*- coding: utf-8 -*-
"""cf-инкремент: частичное обновление базы прямо из декодированного дерева cf.

Полный цикл на синтетической фикстуре паритета (make_cf_tree): полная сборка
пишет состояние гранул (cf_granule), обновление переустанавливает только
изменившиеся гранулы, перелинковывает права/ссылки/составы. Реальные .cf в
тестах не используются; обёртка update_cf проверяется с подменой распаковки.
"""
import json
import os
import shutil
import sqlite3

import pytest

from confdb import cf_update
from confdb.db.writer import SCHEMA_VERSION, write_db
from test_parity import make_cf_tree, make_xml_dump

NEW_UUID = 'cccccccc-0000-0000-0000-00000000c001'


def cf_update_file_sha256():
    from confdb.extract import file_sha256
    return file_sha256


def _tree_bytes(tree):
    """Виртуальное дерево {rel: байты} из дискового дерева (для подмены потока)."""
    out = {}
    for dp, _dirs, fns in os.walk(str(tree)):
        for fn in fns:
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, str(tree)).replace(os.sep, '/')
            with open(full, 'rb') as f:
                out[rel] = f.read()
    return out


@pytest.fixture
def built(tmp_path):
    """Дерево cf + полная база ревизии текущей схемы (с состоянием гранул)."""
    tree = tmp_path / 'tree'
    tree.mkdir()
    make_cf_tree(str(tree))
    db = str(tmp_path / 'base.sqlite')
    write_db(str(tree), db, source_file='Тест.cf', source_sha256='aaa',
             build_fts=False, workers=1)
    return tree, db


def _conn(db):
    return sqlite3.connect(db)


def _update(tree, db, **kw):
    kw.setdefault('rebuild_fts', False)
    kw.setdefault('log', lambda *a: None)
    return cf_update.update_cf_tree(
        str(tree), db, source_file='Тест.cf', source_sha256='bbb', **kw)


def test_full_build_populates_cf_granule(built):
    tree, db = built
    conn = _conn(db)
    try:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        granules = dict(conn.execute('SELECT path, sha256 FROM cf_granule'))
        assert '' in granules and 'Catalog/Справочник1' in granules
        assert len(granules) >= 5
        assert all(len(s) == 64 for s in granules.values())
    finally:
        conn.close()


def test_update_nochange_skips(built):
    tree, db = built
    stats = _update(tree, db)
    assert stats['skipped'] is True and stats['changed_granules'] == []
    conn = _conn(db)
    try:
        assert conn.execute('SELECT file_sha256 FROM source').fetchone()[0] == 'bbb'
    finally:
        conn.close()


def test_update_module_change_partial(built):
    tree, db = built
    conn = _conn(db)
    try:
        clean_id = conn.execute(
            "SELECT id FROM meta_object WHERE path='CommonModule/ОбщийМодуль1'"
        ).fetchone()[0]
        methods_before = conn.execute('SELECT COUNT(*) FROM method').fetchone()[0]
    finally:
        conn.close()
    mod = tree / 'Catalog' / 'Справочник1' / 'Catalog.obj.bsl'
    marker = '// маркер-cf-инкремента'
    mod.write_text(mod.read_text(encoding='utf-8') + '\n' + marker + '\n',
                   encoding='utf-8')
    stats = _update(tree, db)
    assert stats['skipped'] is False
    assert stats['changed_granules'] == ['Catalog/Справочник1']
    assert stats['modules'] >= 1
    conn = _conn(db)
    try:
        assert conn.execute('SELECT COUNT(*) FROM module WHERE body LIKE ?',
                            ('%' + marker + '%',)).fetchone()[0] == 1
        # чистые гранулы не переустанавливались: id объекта тот же
        assert conn.execute(
            "SELECT id FROM meta_object WHERE path='CommonModule/ОбщийМодуль1'"
        ).fetchone()[0] == clean_id
        assert conn.execute('SELECT COUNT(*) FROM method').fetchone()[0] == \
            methods_before
        # состояние гранулы обновлено, остальные прежние
        granules = dict(conn.execute('SELECT path, sha256 FROM cf_granule'))
        assert granules['Catalog/Справочник1'] != granules['CommonModule/ОбщийМодуль1']
    finally:
        conn.close()


def test_update_removed_granule(built):
    tree, db = built
    shutil.rmtree(str(tree / 'CommonModule' / 'ОбщийМодуль1'))
    stats = _update(tree, db)
    assert 'CommonModule/ОбщийМодуль1' in stats['changed_granules']
    conn = _conn(db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM meta_object WHERE path='CommonModule/ОбщийМодуль1'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM module WHERE object_id NOT IN "
            "(SELECT id FROM meta_object)").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM cf_granule WHERE path='CommonModule/ОбщийМодуль1'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_update_new_granule(built):
    tree, db = built
    src = tree / 'CommonModule' / 'ОбщийМодуль1'
    dst = tree / 'CommonModule' / 'ОбщийМодуль2'
    shutil.copytree(str(src), str(dst))
    old_uuid = json.loads((src / 'CommonModule.id.json').read_text(
        encoding='utf-8'))['uuid']
    for path in dst.iterdir():
        if path.suffix in ('.json', '.bsl'):
            text = path.read_text(encoding='utf-8')
            path.write_text(text.replace(old_uuid, NEW_UUID)
                            .replace('ОбщийМодуль1', 'ОбщийМодуль2'),
                            encoding='utf-8')
    stats = _update(tree, db)
    assert 'CommonModule/ОбщийМодуль2' in stats['changed_granules']
    conn = _conn(db)
    try:
        row = conn.execute(
            "SELECT id, uuid FROM meta_object WHERE path='CommonModule/ОбщийМодуль2'"
        ).fetchone()
        assert row and row[1] == NEW_UUID
        assert conn.execute('SELECT COUNT(*) FROM module WHERE object_id=?',
                            (row[0],)).fetchone()[0] == 1
    finally:
        conn.close()


def test_update_relinks_role_rights(built):
    tree, db = built
    conn = _conn(db)
    try:
        before = conn.execute(
            'SELECT role_id, target_uuid, target_object_id, target_attr_id'
            ' FROM role_right ORDER BY role_id, target_uuid').fetchall()
    finally:
        conn.close()
    assert before, 'фикстура должна содержать права ролей'
    mod = tree / 'Catalog' / 'Справочник1' / 'Catalog.obj.bsl'
    mod.write_text(mod.read_text(encoding='utf-8') + '\n// правка\n',
                   encoding='utf-8')
    _update(tree, db)
    conn = _conn(db)
    try:
        after = conn.execute(
            'SELECT role_id, target_uuid, target_object_id, target_attr_id'
            ' FROM role_right ORDER BY role_id, target_uuid').fetchall()
        # цели существуют и разрешены (id могли переехать — важнее не NULL)
        assert len(after) == len(before)
        for (_rid, uuid, obj_id, attr_id), (_r2, u2, o2, a2) in zip(before, after):
            assert uuid == u2
            if obj_id is not None:
                assert o2 is not None
                assert conn.execute('SELECT COUNT(*) FROM meta_object WHERE id=?',
                                    (o2,)).fetchone()[0] == 1
            if attr_id is not None:
                assert a2 is not None
    finally:
        conn.close()


def test_update_cf_wrapper_skip_and_tree(built, monkeypatch):
    tree, db = built
    # вне дерева: иначе файл попал бы в гранулу корня и изменил её отпечаток
    src = str(tree.parent / 'dummy.cf')
    with open(src, 'wb') as f:
        f.write(b'cf-bytes')
    calls = []
    real_sha = cf_update_file_sha256()
    sha_val = ['aaa']
    monkeypatch.setattr('confdb.extract.decode_tree_stream',
                        lambda *a, **kw: calls.append(1) or (str(tree), 'pl', 'hd'))
    monkeypatch.setattr('confdb.v8.helper.payload_read', lambda sink: _tree_bytes(tree))
    monkeypatch.setattr('confdb.extract.file_sha256',
                        lambda p, **kw: sha_val[0] if p == src else real_sha(p, **kw))
    stats = cf_update.update_cf(src, db, log=lambda *a: None)
    assert stats['skipped'] is True and not calls
    sha_val[0] = 'bbb'
    stats = cf_update.update_cf(src, db, log=lambda *a: None, rebuild_fts=False)
    assert calls and stats['skipped'] is True   # дерево то же — гранулы целы
    mod = tree / 'Catalog' / 'Справочник1' / 'Catalog.obj.bsl'
    mod.write_text(mod.read_text(encoding='utf-8') + '\n// ещё\n', encoding='utf-8')
    sha_val[0] = 'ccc'
    stats = cf_update.update_cf(src, db, log=lambda *a: None, rebuild_fts=False)
    assert stats['changed_granules'] == ['Catalog/Справочник1']


def test_cf_db_state_guards(tmp_path):
    from confdb.dump import build_db
    dump = tmp_path / 'dump'
    dump.mkdir()
    make_xml_dump(str(dump))
    db = str(tmp_path / 'dump.sqlite')
    build_db(str(dump), db, workers=1, build_fts=False, log=lambda *a: None)
    info, ok = cf_update._cf_db_state(db)
    assert info and ok is False          # база из выгрузки — не cf-инкремент
    conn = sqlite3.connect(db)
    conn.execute('PRAGMA user_version=14')
    conn.commit()
    conn.close()
    # подменим source.file на cf — ревизия всё равно не годится
    conn = sqlite3.connect(db)
    conn.execute("UPDATE source SET file='x.cf'")
    conn.commit()
    conn.close()
    assert cf_update._cf_db_state(db)[1] is False
    assert cf_update._cf_db_state(str(tmp_path / 'нет.sqlite')) == (None, False)