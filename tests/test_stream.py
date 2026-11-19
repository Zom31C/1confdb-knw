"""Потоковая сборка (stream): декодер отдаёт файлы мимо диска стадии 3.

Два уровня гарантий:
- перехват записи на уровне I/O (v8/helper) даёт виртуальное дерево, побайтово
  идентичное дисковому (включая CRLF текстовых файлов на Windows);
- write_db(virtual_tree=…) строит ту же базу, что и по дисковому дереву
  (построчный дифф всех таблиц на синтетической конфигурации паритета).
"""
import os
import sqlite3

import pytest

from confdb.v8 import helper


def _disk_tree(root):
    out = {}
    for dp, _dirs, fns in os.walk(root):
        for fn in fns:
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, root).replace(os.sep, '/')
            with open(full, 'rb') as f:
                out[rel] = f.read()
    return out


def test_helper_stream_capture_byte_identical(tmp_path):
    """txt/bin/json/copy_file в stream-режиме == байты дисковой записи."""
    root = str(tmp_path / 'virt')
    sink = str(tmp_path / 'payload')
    disk = str(tmp_path / 'disk')
    os.makedirs(disk)
    text = 'Процедура Тест()\n\tА = 1;\nКонецПроцедуры'
    blob = b'\x00\x01\x02\xff'
    obj = {'a': 'б', 'n': ['1', '2']}
    # эталон — дисковая запись
    helper.stream_end()
    helper.txt_write(text, disk, 'm.bsl')
    helper.bin_write(blob, disk, 'b.bin')
    helper.json_write(obj, disk, 'h.json')
    helper.copy_file(os.path.join(disk, 'b.bin'), os.path.join(disk, 'copy.bin'))
    # stream-запись тех же данных
    helper.stream_begin(root, sink)
    try:
        helper.txt_write(text, root, 'm.bsl')
        helper.bin_write(blob, root, 'b.bin')
        helper.json_write(obj, root, 'h.json')
        helper.copy_file(os.path.join(disk, 'b.bin'), os.path.join(root, 'copy.bin'))
        helper.makedirs(os.path.join(root, 'каталог'))  # no-op, не падает
    finally:
        helper.payload_close()
        helper.stream_end()
    vt = helper.payload_read(sink)
    disk_files = _disk_tree(disk)
    assert set(vt) == set(disk_files)
    for rel, raw in disk_files.items():
        assert vt[rel] == raw, rel
    # виртуальное дерево на диск не писалось
    assert not os.path.exists(os.path.join(root, 'm.bsl'))


def test_helper_stream_nested_paths(tmp_path):
    """rel-пути вложенных каталогов сохраняются через '/'."""
    root = str(tmp_path / 'virt')
    sink = str(tmp_path / 'payload')
    helper.stream_begin(root, sink)
    try:
        helper.txt_write('код', os.path.join(root, 'Catalog', 'Имя'), 'Catalog.obj.bsl')
    finally:
        helper.payload_close()
        helper.stream_end()
    vt = helper.payload_read(sink)
    assert 'Catalog/Имя/Catalog.obj.bsl' in vt


def _diff_all_tables(db_a, db_b):
    c1 = sqlite3.connect(db_a)
    c2 = sqlite3.connect(db_b)
    try:
        tables = [r[0] for r in c1.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for tbl in tables:
            cols = [c[1] for c in c1.execute(f'PRAGMA table_info("{tbl}")')]
            sel = [c for c in cols if not (tbl == 'source' and c == 'created')]
            q = 'SELECT ' + ','.join(f'"{c}"' for c in sel) + f' FROM "{tbl}" ORDER BY rowid'
            assert c1.execute(q).fetchall() == c2.execute(q).fetchall(), tbl
    finally:
        c1.close()
        c2.close()


def test_write_db_virtual_tree_parity(tmp_path):
    """write_db по виртуальному дереву == write_db по дисковому (все таблицы)."""
    from confdb.db.writer import write_db
    from test_parity import make_cf_tree
    cf_dir = str(tmp_path / 'stage3')
    os.makedirs(cf_dir)
    make_cf_tree(cf_dir)
    db_disk = str(tmp_path / 'disk.sqlite')
    db_virt = str(tmp_path / 'virt.sqlite')
    write_db(cf_dir, db_disk, source_file='x.cf', build_fts=False, workers=1)
    vt = _disk_tree(cf_dir)
    write_db(cf_dir, db_virt, source_file='x.cf', build_fts=False, workers=1,
             virtual_tree=vt)
    _diff_all_tables(db_disk, db_virt)


def test_write_db_virtual_store_blobs_parity(tmp_path):
    """store_blobs: байты файлов (включая CRLF) совпадают с дисковым путём."""
    from confdb.db.writer import write_db
    from test_parity import make_cf_tree
    cf_dir = str(tmp_path / 'stage3')
    os.makedirs(cf_dir)
    make_cf_tree(cf_dir)
    db_disk = str(tmp_path / 'disk.sqlite')
    db_virt = str(tmp_path / 'virt.sqlite')
    write_db(cf_dir, db_disk, source_file='x.cf', build_fts=False, workers=1,
             store_blobs=True)
    write_db(cf_dir, db_virt, source_file='x.cf', build_fts=False, workers=1,
             store_blobs=True, virtual_tree=_disk_tree(cf_dir))
    _diff_all_tables(db_disk, db_virt)