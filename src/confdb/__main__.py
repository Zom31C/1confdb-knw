"""Точка входа CLI: confdb extract <файл.cf> --db out.sqlite [--dump DIR] ..."""
import argparse
import os
import sys

from . import __version__
from .extract import extract


def build_parser():
    parser = argparse.ArgumentParser(
        prog='confdb',
        description='Экстрактор конфигурации 1С:Предприятие 8 (.cf/.cfe/.epf) в SQLite. '
                    'Только распаковка и разбор (на базе алгоритма v8unpack, MIT).'
    )
    parser.add_argument('--version', action='version', version=f'confdb {__version__}')
    subparsers = parser.add_subparsers(dest='cmd', required=True)

    p = subparsers.add_parser('extract', help='распаковать файл конфигурации')
    p.add_argument('src', help='путь к файлу .cf/.cfe/.epf')
    p.add_argument('--db', metavar='FILE', help='записать результат в базу SQLite')
    p.add_argument('--dump', metavar='DIR', help='сохранить распакованное дерево в каталог')
    p.add_argument('--temp-dir', metavar='DIR', help='рабочий каталог для стадий 0-1')
    p.add_argument('--keep-temp', action='store_true', help='не удалять рабочий каталог')
    p.add_argument('--prefix', metavar='STR', help='префикс имён объектов для снятия')
    p.add_argument('--store-blobs', action='store_true',
                   help='хранить бинарные файлы в БД как BLOB')
    p.add_argument('--workers', metavar='N', type=int, default=1,
                   help='число процессов стадии 3 (1 — последовательно)')

    c = subparsers.add_parser('check', help='проверить запросы СКД в готовой базе')
    c.add_argument('db', help='путь к базе SQLite')

    m = subparsers.add_parser(
        '1confdb-knw',
        help='MCP-сервер (stdio) знаний по конфигурации 1С и BSL для внешних LLM')
    m.add_argument('db', help='путь к базе SQLite')
    return parser


def run_check(db_path):
    """Проверка всех запросов skd_query; код возврата 1 при наличии ошибок."""
    import sqlite3

    from .query_lang import MetaContext, check_query
    if not os.path.isfile(db_path):
        print(f'Файл базы не найден: {db_path}', file=sys.stderr)
        return 2
    conn = sqlite3.connect(db_path)
    try:
        ctx = MetaContext(conn)
        rows = conn.execute(
            'SELECT q.id, o.path, q.query FROM skd_query q '
            'JOIN meta_object o ON o.id=q.object_id ORDER BY q.id').fetchall()
    finally:
        conn.close()
    fails = 0
    for rid, path, text in rows:
        errs = check_query(text, ctx)
        if errs:
            fails += 1
            print(f'[{rid}] {path}')
            for err in errs[:10]:
                print('   ', err)
    print(f'Проверено запросов: {len(rows)}, с ошибками: {fails}')
    return 1 if fails else 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cmd == 'check':
        return run_check(args.db)

    if args.cmd == '1confdb-knw':
        from .mcp_server import main as mcp_main
        return mcp_main([args.db])

    if not args.db and not args.dump:
        parser.error('нужно указать хотя бы один из --db / --dump')

    if args.db and os.path.isdir(args.db):
        base = os.path.splitext(os.path.basename(args.src))[0]
        args.db = os.path.join(args.db, base + '.sqlite')
        print(f'Путь БД — каталог, файл будет создан как: {args.db}')

    if args.workers < 1:
        parser.error('--workers должен быть >= 1')

    options = {'store_blobs': args.store_blobs}
    if args.prefix:
        options['prefix'] = args.prefix

    try:
        stats = extract(
            args.src,
            db_path=args.db,
            dump_dir=args.dump,
            temp_dir=args.temp_dir,
            keep_temp=args.keep_temp,
            options=options,
            workers=args.workers,
        )
    except FileNotFoundError as err:
        print(f'Файл не найден: {err}', file=sys.stderr)
        return 2
    except Exception as err:
        print(f'Ошибка: {err}', file=sys.stderr)
        return 1

    for key in ('src', 'db', 'dump_dir', 'db_rows', 'elapsed'):
        if stats.get(key) is not None:
            print(f'{key}: {stats[key]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
