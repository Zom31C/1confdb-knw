"""Тесты CLI confdb (разбор аргументов)."""
import pytest

from confdb.__main__ import build_parser, main


def test_parser_full():
    args = build_parser().parse_args([
        'extract', 'config.cf', '--db', 'out.sqlite', '--dump', 'out',
        '--keep-temp', '--store-blobs', '--prefix', 'Префикс_',
    ])
    assert args.cmd == 'extract'
    assert args.src == 'config.cf'
    assert args.db == 'out.sqlite'
    assert args.dump == 'out'
    assert args.keep_temp and args.store_blobs
    assert args.prefix == 'Префикс_'
    assert not args.skip_errors
    args = build_parser().parse_args(['extract', 'config.cf', '--skip-errors'])
    assert args.skip_errors


def test_main_requires_target():
    with pytest.raises(SystemExit) as exc:
        main(['extract', 'config.cf'])
    assert exc.value.code == 2


def test_main_missing_file(tmp_path):
    rc = main(['extract', str(tmp_path / 'нет-такого.cf'), '--dump', str(tmp_path / 'out')])
    assert rc == 2


def test_mcp_db_optional():
    # без пути база ищется автоматически (last_db из конфига, затем обход)
    args = build_parser().parse_args(['1confdb-knw'])
    assert args.db == []
    args = build_parser().parse_args(['1confdb-knw', 'out.db', '--port', '8765'])
    assert args.db == ['out.db'] and args.port == 8765
    # несколько баз сразу: основная конфигурация + расширения/обработки
    args = build_parser().parse_args(['1confdb-knw', 'main.db', 'ext.db'])
    assert args.db == ['main.db', 'ext.db']


def test_main_mcp_missing_db(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(['1confdb-knw', str(tmp_path / 'нет.db')])
    assert exc.value.code == 2


def test_mcp_group_flags():
    # несколько групп одновременно: флаг повторяется, порядок сохраняется
    args = build_parser().parse_args(
        ['1confdb-knw', '--group', 'УНФ=D:\\a.db', '--group', 'УНФ=D:\\b.db',
         '--group', 'БП=D:\\c.db'])
    assert args.group == ['УНФ=D:\\a.db', 'УНФ=D:\\b.db', 'БП=D:\\c.db']
    assert args.db == []
    assert build_parser().parse_args(['1confdb-knw']).group is None


def test_main_mcp_group_is_forwarded(tmp_path):
    # недостижимая база группы — тот же код возврата, что и у позиционного пути
    with pytest.raises(SystemExit) as exc:
        main(['1confdb-knw', '--group', f'унф={tmp_path / "нет.db"}'])
    assert exc.value.code == 2


def test_parser_no_fts_and_fts_command():
    args = build_parser().parse_args(['extract', 'config.cf', '--db', 'o.db', '--no-fts'])
    assert args.no_fts
    assert not build_parser().parse_args(['extract', 'config.cf', '--db', 'o.db']).no_fts
    args = build_parser().parse_args(['fts', 'out.db', '--workers', '4'])
    assert args.cmd == 'fts' and args.db == 'out.db' and args.workers == 4
    assert build_parser().parse_args(['fts', 'out.db']).workers is None


def test_main_fts_missing_db(tmp_path):
    assert main(['fts', str(tmp_path / 'нет.db')]) == 2


def test_main_fts_rejects_foreign_db(tmp_path):
    import sqlite3

    other = str(tmp_path / 'other.sqlite')
    conn = sqlite3.connect(other)
    conn.execute('CREATE TABLE t (x)')
    conn.commit()
    conn.close()
    assert main(['fts', other]) == 1


def test_fts_command_builds_index(tmp_path):
    """confdb fts достраивает приставной индекс для базы, собранной с --no-fts."""
    import sqlite3

    from confdb.db.writer import FTS_TABLE, fts_index_info, fts_shard_paths, write_db

    from test_writer import make_dump
    dump = str(tmp_path / 'dump')
    make_dump(dump)
    db = str(tmp_path / 'out.sqlite')
    write_db(dump, db, source_file='t.cf', build_fts=False)

    assert main(['fts', db, '--workers', '2']) == 0
    shards = fts_shard_paths(db)
    info = fts_index_info(db)
    assert info and shards and info['shards'] == len(shards)
    conn = sqlite3.connect(db)
    methods = conn.execute('SELECT COUNT(*) FROM method').fetchone()[0]
    conn.close()
    assert info['methods'] == methods
    total = 0
    for path in shards:
        shard = sqlite3.connect(path)
        total += shard.execute(f'SELECT COUNT(*) FROM {FTS_TABLE}').fetchone()[0]
        shard.close()
    assert total == methods


def test_parser_force_flag():
    assert build_parser().parse_args(
        ['extract', 'config.cf', '--db', 'o.db', '--force']).force
    assert not build_parser().parse_args(
        ['extract', 'config.cf', '--db', 'o.db']).force


def test_main_skips_when_the_source_is_already_extracted(tmp_path, monkeypatch):
    """Целевая база собрана из этого же файла — распаковка не запускается вовсе."""
    import sqlite3

    import confdb.__main__ as cli
    from confdb.extract import file_sha256

    src = tmp_path / 'config.cf'
    src.write_bytes(b'1CV8' + b'x' * 1024)
    db = str(tmp_path / 'out.sqlite')
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE source (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                 'file TEXT NOT NULL, created TEXT NOT NULL, '
                 'file_size INTEGER, file_sha256 TEXT)')
    conn.execute('INSERT INTO source (file, created, file_size, file_sha256) '
                 'VALUES (?, ?, ?, ?)',
                 (str(src), '2026-10-05T00:00:00', src.stat().st_size,
                  file_sha256(str(src))))
    conn.commit()
    conn.close()

    calls = []
    monkeypatch.setattr(cli, 'extract',
                        lambda *a, **kw: calls.append(kw) or {'src': a[0]})
    assert main(['extract', str(src), '--db', db]) == 0
    assert not calls

    # --force пересобирает, а посчитанный отпечаток уходит в extract: второй раз
    # файл не хэшируется
    assert main(['extract', str(src), '--db', db, '--force']) == 0
    assert len(calls) == 1
    assert calls[0]['source_sha256'] == file_sha256(str(src))


# ---------------------------------------------------------------------------
# CLI режима «выгрузка конфигурации в файлы»
# ---------------------------------------------------------------------------

def test_parser_update_command():
    args = build_parser().parse_args(
        ['update', r'cf\БП_РФ', '--db', 'out.db', '--workers', '4', '--no-fts'])
    assert args.cmd == 'update'
    assert args.src == r'cf\БП_РФ' and args.db == 'out.db'
    assert args.workers == 4 and args.no_fts and not args.fts
    args = build_parser().parse_args(['update', 'd', '--db', 'o.db', '--fts'])
    assert args.fts and not args.no_fts
    with pytest.raises(SystemExit):
        build_parser().parse_args(['update', 'd'])   # --db обязателен


def test_extract_dump_dir_requires_db(tmp_path):
    from test_dump import make_dump_dir
    dump = tmp_path / 'cfg'
    dump.mkdir()
    make_dump_dir(str(dump))
    assert main(['extract', str(dump)]) == 2


def test_extract_dump_dir_then_update(tmp_path):
    """extract на каталоге выгрузки = полная сборка; повтор = инкремент."""
    import sqlite3

    from test_dump import make_dump_dir, _w
    dump = tmp_path / 'cfg'
    dump.mkdir()
    make_dump_dir(str(dump))
    db = str(tmp_path / 'out.sqlite')
    assert main(['extract', str(dump), '--db', db, '--no-fts',
                 '--workers', '1']) == 0
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM meta_object "
                        "WHERE path='Catalog/Справочник1'").fetchone()[0] == 1
    assert conn.execute('SELECT COUNT(*) FROM dump_file').fetchone()[0] >= 15
    conn.close()
    # повторный extract той же выгрузки — инкремент без изменений
    assert main(['extract', str(dump), '--db', db, '--no-fts',
                 '--workers', '1']) == 0
    # явный update после правки модуля
    _w(str(dump), 'CommonModules/ОбщийМодуль1/Ext/Module.bsl',
       'Процедура Обновлённая()\nКонецПроцедуры')
    assert main(['update', str(dump), '--db', db, '--no-fts',
                 '--workers', '1']) == 0
    conn = sqlite3.connect(db)
    names = [r[0] for r in conn.execute(
        "SELECT t.name FROM method t JOIN module m ON m.id=t.module_id "
        "JOIN meta_object o ON o.id=m.object_id "
        "WHERE o.path='CommonModule/ОбщийМодуль1'")]
    conn.close()
    assert names == ['Обновлённая']


def test_update_rejects_non_dump_dir(tmp_path):
    db = str(tmp_path / 'o.db')
    assert main(['update', str(tmp_path), '--db', db]) == 2
