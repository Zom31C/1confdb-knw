"""Точка входа CLI: confdb extract <файл.cf> --db out.sqlite [--dump DIR] ..."""
import argparse
import os
import sys

from . import __version__
from .extract import check_same_source, extract


def build_parser():
    parser = argparse.ArgumentParser(
        prog='confdb',
        description='Экстрактор конфигурации 1С:Предприятие 8 (.cf/.cfe/.epf) в SQLite. '
                    'Только распаковка и разбор (на базе алгоритма v8unpack, MIT).'
    )
    parser.add_argument('--version', action='version', version=f'confdb {__version__}')
    subparsers = parser.add_subparsers(dest='cmd', required=True)

    p = subparsers.add_parser('extract', help='распаковать файл конфигурации')
    p.add_argument('src', help='путь к файлу .cf/.cfe/.epf либо к каталогу '
                               'выгрузки конфигурации в файлы (с '
                               'ConfigDumpInfo.xml — соберётся база из '
                               'выгрузки, повторный запуск = инкрементальное '
                               'обновление)')
    p.add_argument('--db', metavar='FILE', help='записать результат в базу SQLite')
    p.add_argument('--force', action='store_true',
                   help='пересобрать базу, даже если она уже собрана из этого же '
                        'файла: до распаковки сравнивается SHA-256 исходника '
                        '(885 МБ — 1,7 с) с отпечатком в source')
    p.add_argument('--dump', metavar='DIR', help='сохранить распакованное дерево в каталог')
    p.add_argument('--temp-dir', metavar='DIR', help='рабочий каталог для стадий 0-1')
    p.add_argument('--keep-temp', action='store_true', help='не удалять рабочий каталог')
    p.add_argument('--prefix', metavar='STR', help='префикс имён объектов для снятия')
    p.add_argument('--store-blobs', action='store_true',
                   help='хранить бинарные файлы в БД как BLOB')
    p.add_argument('--workers', metavar='N', type=int, default=None,
                   help='число процессов стадии 3 и записи БД '
                        '(по умолчанию — результат "confdb bench" или 1)')
    p.add_argument('--skip-errors', action='store_true',
                   help='не прерываться на ошибке декодирования объекта: '
                        'пропустить его и продолжить (дамп и база будут неполными)')
    p.add_argument('--no-fts', action='store_true',
                   help='не строить FTS-индекс по телам методов (приставные файлы '
                        '<база>.fts, на УНФ 7.5 с в 8 шардов); поиск по телам останется '
                        'рабочим, но медленным, индекс собирается позже — '
                        '"confdb fts <база>"')
    p.add_argument('--dump-indent', action='store_true',
                   help='писать JSON дампа с отступами, как v8unpack — для побайтового '
                        'сравнения дампов; по умолчанию вывод компактный, с ним стадия 3 '
                        'заметно быстрее, а дамп примерно втрое меньше')

    c = subparsers.add_parser('check', help='проверить запросы СКД в готовой базе')
    c.add_argument('db', help='путь к базе SQLite')

    u = subparsers.add_parser(
        'update',
        help='инкрементальное обновление базы из выгрузки конфигурации в '
             'файлы ИЛИ прямо из .cf/.cfe/.epf/.erf: перезагружаются только '
             'изменённые объекты/гранулы (хэши файлов в dump_file / cf_granule); '
             'работает при запущенном MCP-сервере (WAL + hot reload), без '
             'полной пересборки')
    u.add_argument('src', help='каталог выгрузки конфигурации в файлы или '
                               'файл .cf/.cfe/.epf/.erf')
    u.add_argument('--db', metavar='FILE', required=True,
                   help='база знаний (нет или собрана из .cf — будет полная '
                        'сборка из выгрузки)')
    u.add_argument('--workers', metavar='N', type=int, default=None,
                   help='число процессов разбора модулей BSL')
    u.add_argument('--no-fts', action='store_true',
                   help='не пересобирать FTS-индекс (по умолчанию '
                        'пересобирается, если менялись модули)')
    u.add_argument('--fts', action='store_true',
                   help='пересобрать FTS-индекс, даже если модули не менялись')

    f = subparsers.add_parser(
        'fts', help='построить FTS-индекс по телам методов в готовой базе')
    f.add_argument('db', help='путь к базе SQLite')
    f.add_argument('--workers', metavar='N', type=int, default=None,
                   help='число процессов сборки = число файлов-шардов индекса '
                        '(по умолчанию — результат "confdb bench" или 1)')

    b = subparsers.add_parser(
        'bench',
        help='бенчмарк: подбор числа процессов под железо (результат в конфиг)')
    b.add_argument('src', help='путь к файлу .cf/.cfe/.epf')
    b.add_argument('--sample', metavar='N', type=int, default=1000,
                   help='размер сэмпла объектов верхнего уровня (по умолчанию 1000)')

    h = subparsers.add_parser(
        'hbk',
        help='синтакс-помощник платформы 1С: распаковка .hbk, сборка '
             'syntax.db и запросы к ней (поиск/карточки/дерево тем)')
    hsub = h.add_subparsers(dest='hbk_cmd', required=True)

    hu = hsub.add_parser(
        'unpack',
        help='распаковать .hbk (контекстная справка 1С) на чистом Python: '
             'ZIP со смещением на заголовок 1С, стандартный zipfile его не читает')
    hu.add_argument('hbk', help='файл .hbk (например, hbk\\shcntx_ru.hbk)')
    hu.add_argument('-o', '--out', default='extracted',
                    help='каталог назначения (по умолчанию extracted)')
    hu.add_argument('--list', action='store_true',
                    help='только показать список файлов архива')
    hu.add_argument('--verify-only', action='store_true',
                    help='проверить CRC всех записей, не записывая файлы')

    hb = hsub.add_parser(
        'build', help='собрать syntax.db из распакованного дерева .hbk')
    hb.add_argument('root', help='каталог с objects/ и tables/ (результат "hbk unpack")')
    hb.add_argument('--db', metavar='FILE', default='syntax.db',
                    help='файл базы (по умолчанию syntax.db; существующий перезаписывается)')
    hb.add_argument('--platform', metavar='STR', default=None,
                    help='строка источника для meta.platform (по умолчанию '
                         '«1С:Предприятие 8.3.26.1498 / shcntx_ru.hbk»)')

    hc = hsub.add_parser('check', help='контроль качества syntax.db')
    hc.add_argument('db', nargs='?', default=None,
                    help='база (по умолчанию syntax.db или hbk\\syntax.db)')
    hc.add_argument('--quick', action='store_true',
                    help='только структурные проверки: числовые пороги и '
                         'эталонные темы рассчитаны на полную базу shcntx_ru.hbk')

    hse = hsub.add_parser('search', help='нечёткий поиск тем синтакс-помощника')
    hse.add_argument('query', nargs='+', help='текст запроса')
    hse.add_argument('--db', metavar='FILE', default=None,
                     help='база (по умолчанию syntax.db или hbk\\syntax.db)')
    hse.add_argument('--limit', type=int, default=20,
                     help='максимум результатов (по умолчанию 20)')

    hsh = hsub.add_parser('show', help='карточка темы по id')
    hsh.add_argument('id', type=int, help='id темы (стоит в результатах поиска)')
    hsh.add_argument('--db', metavar='FILE', default=None,
                     help='база (по умолчанию syntax.db или hbk\\syntax.db)')

    ht = hsub.add_parser('tree', help='поддерево темы')
    ht.add_argument('id', type=int, help='id темы-корня поддерева')
    ht.add_argument('-d', '--depth', type=int, default=2,
                    help='глубина (по умолчанию 2)')
    ht.add_argument('--limit', type=int, default=20,
                    help='максимум узлов (по умолчанию 20)')
    ht.add_argument('--db', metavar='FILE', default=None,
                    help='база (по умолчанию syntax.db или hbk\\syntax.db)')

    hr = hsub.add_parser('repl', help='интерактивный обход дерева тем')
    hr.add_argument('--db', metavar='FILE', default=None,
                    help='база (по умолчанию syntax.db или hbk\\syntax.db)')
    hr.add_argument('--limit', type=int, default=20,
                    help='максимум результатов поиска (по умолчанию 20)')

    m = subparsers.add_parser(
        '1confdb-knw',
        help='MCP-сервер знаний по конфигурации 1С и BSL для внешних LLM '
             '(stdio; --port — HTTP для SSH-туннеля)')
    m.add_argument('db', nargs='*', default=None,
                   help='пути к базам SQLite (можно несколько: основная '
                        'конфигурация + расширения/обработки); без путей — '
                        'last_db из конфига или автопоиск *.db/*.sqlite')
    m.add_argument('--host', default='127.0.0.1',
                   help='адрес для HTTP-режима (по умолчанию 127.0.0.1)')
    m.add_argument('--port', type=int, default=0,
                   help='порт HTTP-режима (без него — stdio)')
    m.add_argument('--group', action='append', default=None,
                   metavar='ИМЯ=ПУТЬ',
                   help='группа конфигураций (повторяемый): имя и путь к базе, '
                        'несколько путей — через ";". Группы открываются '
                        'одновременно и остаются раздельными')
    m.add_argument('--syntax-db', metavar='FILE', default=None,
                   help='база синтакс-помощника платформы (syntax.db, '
                        'собирается через "confdb hbk build"): добавляет '
                        'инструменты syntax_search/syntax_card/syntax_children. '
                        'Без флага — путь из syntax_db в ~/.confdb/config.json')
    w = subparsers.add_parser(
        'web', help='веб-интерфейс в браузере: MCP-сервер, извлечение, каталог cf, '
                    'группы баз, настройки (логика общая с консольным TUI)')
    w.add_argument('--host', default='127.0.0.1',
                   help='адрес (по умолчанию 127.0.0.1 — только локально)')
    w.add_argument('--port', type=int, default=8766,
                   help='порт веб-интерфейса (по умолчанию 8766)')
    w.add_argument('--no-browser', action='store_true',
                   help='не открывать браузер автоматически')

    r = subparsers.add_parser(
        'repos',
        help='git-источники баз знаний: репозитории выгрузок в файлы, .cf, '
             '.epf, .cfe — клонирование, сборка SQLite, слежение за '
             'изменениями и частичное/полное обновление баз')
    rsub = r.add_subparsers(dest='repos_cmd', required=True)

    ra = rsub.add_parser(
        'add', help='зарегистрировать git-репозиторий как источник базы')
    ra.add_argument('url', help='URL репозитория (https://… или git@…) '
                                'либо локальный путь')
    ra.add_argument('--id', metavar='NAME',
                    help='короткое имя источника (по умолчанию из URL)')
    ra.add_argument('--branch', metavar='B', default='',
                    help='ветка (по умолчанию — основная ветка репозитория)')
    ra.add_argument('--path', metavar='P', default='',
                    help='путь внутри репозитория: каталог выгрузки или файл '
                         '.cf/.cfe/.epf/.erf (по умолчанию — автоопределение)')
    ra.add_argument('--db', metavar='FILE',
                    help='база SQLite (по умолчанию ~/.confdb/dbs/<имя>.sqlite)')
    ra.add_argument('--mode', choices=('auto', 'partial', 'full'),
                    default='auto',
                    help='стратегия обновления: auto — выгрузка частично, '
                         'файл 1С полностью; partial — выгрузка только '
                         'частично; full — всегда полная пересборка')
    ra.add_argument('--watch', action='store_true',
                    help='включить источник в слежение (confdb repos watch)')
    ra.add_argument('--sync', action='store_true',
                    help='сразу клонировать и собрать базу')

    rsub.add_parser('list', help='список источников и их состояние')

    rs = rsub.add_parser(
        'sync', help='клонировать/подтянуть изменения и собрать/обновить базы')
    rs.add_argument('ids', nargs='*', help='имена источников (пусто — все)')
    rs.add_argument('--force', action='store_true',
                    help='пересобрать базы, даже если изменений нет')

    rcheck = rsub.add_parser(
        'check', help='проверить изменения в удалённых ветках (без сборки)')
    rcheck.add_argument('ids', nargs='*', help='имена источников (пусто — все)')

    ra_auth = rsub.add_parser(
        'auth', help='авторизоваться в git-хостинге (браузер/окно входа GCM): '
                     'учётные данные сохранятся для синхронизации приватных '
                     'репозиториев')
    ra_auth.add_argument('id', nargs='?', default=None,
                         help='имя источника (или --url)')
    ra_auth.add_argument('--url', metavar='URL', default=None,
                         help='URL репозитория напрямую (без источника)')

    rr = rsub.add_parser('remove', help='убрать источник из реестра')
    rr.add_argument('id', help='имя источника')
    rr.add_argument('--purge', action='store_true',
                    help='удалить и каталог клона (база остаётся)')

    rw = rsub.add_parser(
        'watch', help='следить за изменениями: циклы проверки и обновления '
                      'источников с флагом watch (Ctrl+C — остановка)')
    rw.add_argument('--interval', metavar='SEC', type=int, default=300,
                    help='пауза между циклами, с (по умолчанию 300, минимум 10)')
    rw.add_argument('--once', action='store_true',
                    help='один проход и выход (для планировщика/cron)')
    rw.add_argument('--all', action='store_true',
                    help='все источники, а не только с флагом watch')
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


def run_fts(db_path, workers=None):
    """Построение приставного FTS-индекса по телам методов (после --no-fts)."""
    import sqlite3
    import time

    from .db.writer import build_fts_index, fts_dir
    if not os.path.isfile(db_path):
        print(f'Файл базы не найден: {db_path}', file=sys.stderr)
        return 2
    if workers is None:
        from .config import bench_workers
        workers = bench_workers() or 1
    if workers < 1:
        print('--workers должен быть >= 1', file=sys.stderr)
        return 2
    begin = time.time()
    try:
        methods = build_fts_index(db_path, workers=workers)
    except (sqlite3.Error, OSError, ValueError) as err:
        print(f'Ошибка: {err}', file=sys.stderr)
        return 1
    print(f'FTS-индекс построен: {methods} методов, {workers} шардов '
          f'за {time.time() - begin:.1f} с')
    print(f'Файлы индекса: {fts_dir(db_path)} — их можно удалить, база остаётся '
          f'рабочей (поиск по телам пойдёт через body_has)')
    return 0


def run_update(args):
    """Инкрементальное обновление базы из выгрузки в файлы или прямо из cf."""
    from .dump import is_dump_dir, update_db
    workers = args.workers
    if workers is None:
        from .config import bench_workers
        workers = bench_workers() or 1
    if workers < 1:
        print('--workers должен быть >= 1', file=sys.stderr)
        return 2
    rebuild_fts = None
    if args.no_fts:
        rebuild_fts = False
    elif args.fts:
        rebuild_fts = True
    if not is_dump_dir(args.src):
        ext = os.path.splitext(args.src)[1].lower()
        if os.path.isfile(args.src) and ext in ('.cf', '.cfe', '.epf', '.erf'):
            from .cf_update import update_cf
            try:
                stats = update_cf(args.src, args.db, workers=workers,
                                  rebuild_fts=rebuild_fts)
            except PermissionError as err:
                print(f'Файл базы занят другим процессом (запущен MCP-сервер?): {err}',
                      file=sys.stderr)
                _hint_db_holder(args.db)
                return 1
            except (ValueError, OSError) as err:
                print(f'Ошибка: {err}', file=sys.stderr)
                return 1
            for key in ('changed_granules', 'objects', 'modules', 'methods',
                        'elapsed'):
                value = stats.get(key)
                if isinstance(value, list):
                    print(f'{key}: {len(value)}')
                elif value is not None:
                    print(f'{key}: {value}')
            return 0
        print(f'Каталог не похож на выгрузку конфигурации '
              f'(нет ConfigDumpInfo.xml) и не файл 1С: {args.src}', file=sys.stderr)
        return 2
    try:
        stats = update_db(args.src, args.db, workers=workers,
                          rebuild_fts=rebuild_fts)
    except PermissionError as err:
        print(f'Файл базы занят другим процессом (запущен MCP-сервер?): {err}',
              file=sys.stderr)
        _hint_db_holder(args.db)
        return 1
    except (ValueError, OSError) as err:
        print(f'Ошибка: {err}', file=sys.stderr)
        return 1
    for key in ('changed_granules', 'objects', 'modules', 'methods',
                'role_rights', 'elapsed'):
        value = stats.get(key)
        if isinstance(value, list):
            print(f'{key}: {len(value)}')
        elif value is not None:
            print(f'{key}: {value}')
    return 0


def run_extract_dump(args):
    """Сборка/обновление базы из каталога выгрузки конфигурации в файлы."""
    from .dump import build_db, update_db
    workers = args.workers
    if workers is None:
        from .config import bench_workers
        workers = bench_workers() or 1
    if not args.db:
        parser_error = ('для выгрузки в файлы нужен --db (само дерево уже '
                        'является «дампом»)')
        print(parser_error, file=sys.stderr)
        return 2
    try:
        if os.path.isfile(args.db) and not args.force:
            # уже собрана из выгрузки — инкрементальное обновление
            stats = update_db(args.src, args.db, workers=workers,
                              rebuild_fts=False if args.no_fts else None)
        else:
            if args.force and os.path.isfile(args.db):
                from .dump.update import read_server_pidfile
                server = read_server_pidfile(args.db)
                if server:
                    print('Полная пересборка невозможна: базу держит '
                          f'MCP-сервер (pid {server.get("pid")}). Остановите '
                          'сервер или используйте "confdb update" — он '
                          'обновляет базу на ходу (WAL).', file=sys.stderr)
                    return 1
            stats = build_db(args.src, args.db, workers=workers,
                             build_fts=not args.no_fts,
                             store_blobs=args.store_blobs)
    except PermissionError as err:
        print(f'Файл базы занят другим процессом (запущен MCP-сервер?): {err}',
              file=sys.stderr)
        _hint_db_holder(args.db)
        return 1
    except (ValueError, OSError) as err:
        print(f'Ошибка: {err}', file=sys.stderr)
        return 1
    for key in ('objects', 'attributes', 'modules', 'methods', 'role_rights',
                'changed_granules', 'elapsed'):
        value = stats.get(key)
        if isinstance(value, list):
            print(f'{key}: {len(value)}')
        elif value is not None:
            print(f'{key}: {value}')
    return 0


def _hint_db_holder(db_path):
    """Подсказка к «файл базы занят»: назвать живой MCP-сервер по pidfile."""
    server = None
    if db_path:
        try:
            from .dump.update import read_server_pidfile
            server = read_server_pidfile(db_path)
        except Exception:  # noqa: BLE001
            server = None
    if server:
        print(f'Файл держит MCP-сервер (pid {server.get("pid")}). Остановите '
              'его; остановка HTTP-сервера из консольного интерфейса закрывает '
              'базы и освобождает файлы.', file=sys.stderr)
    else:
        print('Возможно, базу держит незакрытый MCP-сервер или консольный '
              'интерфейс (confdb-ui).', file=sys.stderr)


def _hbk_db_path(explicit):
    """База синтакс-помощника: явный путь либо автопоиск в cwd и hbk/."""
    if explicit:
        return explicit
    for cand in ('syntax.db', os.path.join('hbk', 'syntax.db')):
        if os.path.isfile(cand):
            return cand
    return 'syntax.db'


def run_hbk(args):
    """Команды синтакс-помощника: распаковка .hbk, сборка и запросы к базе."""
    from .hbk import unpack as hbk_unpack

    if args.hbk_cmd == 'unpack':
        try:
            if args.list:
                brief, total = hbk_unpack.list_entries(args.hbk)
                for usize, name in brief:
                    print(f'  {usize:>10}  {name}')
                if total > len(brief):
                    print(f'  ... и ещё {total - len(brief)}')
                return 0
            if args.verify_only:
                stats = hbk_unpack.verify_hbk(args.hbk, progress=print)
                print(f'CRC проверен: записей={stats["entries"]}, '
                      f'файлов={stats["files"]}, ошибок=0')
                return 0
            print(f'Распаковка {args.hbk} в {os.path.abspath(args.out)}...')
            stats = hbk_unpack.unpack_hbk(args.hbk, args.out, progress=print)
            print(f'Готово: файлов={stats["files"]}, каталогов={stats["dirs"]}, '
                  f'дубликатов имён={stats["duplicates"]}, ошибок CRC=0')
            return 0
        except FileNotFoundError:
            print(f'Файл не найден: {args.hbk}', file=sys.stderr)
            return 2
        except hbk_unpack.HbkError as err:
            print(f'Ошибка: {err}', file=sys.stderr)
            return 1

    if args.hbk_cmd == 'build':
        from .hbk import build as hbk_build
        if not os.path.isdir(os.path.join(args.root, 'objects')):
            print(f'в {args.root} нет каталога objects/ '
                  f'(нужен результат "confdb hbk unpack")', file=sys.stderr)
            return 2
        result = hbk_build.build_syntax_db(args.root, args.db,
                                           platform=args.platform)
        print(hbk_build.format_build_result(result))
        return 0

    if args.hbk_cmd == 'check':
        from .hbk import check as hbk_check
        path = _hbk_db_path(args.db)
        if not os.path.isfile(path):
            print(f'Файл базы не найден: {path}', file=sys.stderr)
            return 2
        fails = hbk_check.check_syntax_db(path, full=not args.quick)
        return 1 if fails else 0

    # search / show / tree / repl — запросы к готовой базе
    from .hbk import query as hbk_query
    path = _hbk_db_path(args.db)
    if not os.path.isfile(path):
        print(f'Файл базы не найден: {path} (соберите: '
              f'confdb hbk build <каталог> --db {path})', file=sys.stderr)
        return 2
    try:
        conn = hbk_query.open_syntax_db(path)
    except (ValueError, OSError) as err:
        print(f'Ошибка: {err}', file=sys.stderr)
        return 2
    try:
        if args.hbk_cmd == 'search':
            query = ' '.join(args.query)
            res = hbk_query.search(conn, query, args.limit,
                                   hbk_query.load_name_cache(conn))
            ids = [tid for tid, _, _ in res]
            rows = hbk_query.fetch_brief(conn, ids)
            print(f'Найдено {len(rows)} по запросу «{query}»:')
            if rows:
                print(hbk_query.format_results(
                    rows, hbk_query.parents_with_children(conn)))
            return 0
        if args.hbk_cmd == 'show':
            topic = hbk_query.topic_full(conn, args.id)
            if topic is None:
                print(f'Тема id={args.id} не найдена.')
                return 1
            print(hbk_query.format_topic(topic))
            return 0
        if args.hbk_cmd == 'tree':
            print(hbk_query.format_tree(conn, args.id, args.depth, args.limit))
            return 0
        # repl
        hbk_query.repl(conn, limit=args.limit)
        return 0
    finally:
        conn.close()


def _print_repos_results(results):
    """Печать итогов синхронизации; код возврата 1 при ошибках."""
    fails = 0
    for r in results:
        mark = 'ОК' if r['ok'] else 'ОШИБКА'
        print(f'[{mark}] {r["id"]}: {r["summary"]}')
        if not r['ok']:
            fails += 1
    return 1 if fails else 0


def run_repos(args):
    """Git-источники баз: реестр, клонирование, сборка, слежение (ui_core)."""
    from . import repos
    from .ui_core import UiCore
    core = UiCore()
    cmd = args.repos_cmd

    if cmd == 'add':
        try:
            rec = core.repos_add(args.url, source_id=args.id,
                                 branch=args.branch, path=args.path,
                                 db=args.db, mode=args.mode, watch=args.watch)
        except ValueError as err:
            print(f'Ошибка: {err}', file=sys.stderr)
            return 2
        print(f'Источник «{rec["id"]}» зарегистрирован: {rec["url"]}')
        print(f'База: {rec["db"]}')
        if args.sync:
            return _print_repos_results(core.repos_sync(ids=[rec['id']]))
        print(f'Клонирование и сборка — confdb repos sync {rec["id"]}')
        return 0

    if cmd == 'list':
        rows = core.repos_list()
        if not rows:
            print('Git-источников нет — добавьте: confdb repos add <URL>')
            return 0
        for r in rows:
            commit = (r.get('commit') or '')[:8] or 'не клонирован'
            state = r.get('last_result') or 'новый'
            db_mark = 'есть' if r['db_exists'] else 'нет'
            print(f'{r["id"]}: {r["url"]}')
            print(f'   тип {r.get("kind") or "?"} · ветка '
                  f'{r.get("branch") or "основная"} · коммит {commit} · '
                  f'режим {r.get("mode", "auto")} · '
                  f'{"watch" if r.get("watch") else "без слежения"}')
            print(f'   база {r["db"]} ({db_mark}) · последний результат: {state}')
            if r.get('last_error'):
                print(f'   ошибка: {r["last_error"]}')
        return 0

    if cmd == 'sync':
        return _print_repos_results(
            core.repos_sync(ids=args.ids or None, force=args.force))

    if cmd == 'check':
        results = core.repos_check(ids=args.ids or None)
        fails = 0
        for r in results:
            if not r['ok']:
                print(f'{r["id"]}: ошибка — {r.get("error")}')
                fails += 1
            elif not r['cloned']:
                print(f'{r["id"]}: не клонирован (нужен confdb repos sync)')
            elif r['changed']:
                print(f'{r["id"]}: есть изменения '
                      f'{r["local"][:8]} → {r["remote"][:8]}')
            else:
                print(f'{r["id"]}: изменений нет ({r["local"][:8]})')
        return 1 if fails else 0

    if cmd == 'auth':
        if not args.id and not args.url:
            print('Укажите имя источника или --url', file=sys.stderr)
            return 2
        try:
            core.repos_auth(source_id=args.id, url=args.url)
        except ValueError as err:
            print(f'Ошибка: {err}', file=sys.stderr)
            return 1
        print('Доступ есть: авторизация не требуется или выполнена успешно.')
        return 0

    if cmd == 'remove':
        try:
            rec = core.repos_remove(args.id, purge=args.purge)
        except ValueError as err:
            print(f'Ошибка: {err}', file=sys.stderr)
            return 2
        tail = (' Клон удалён.' if args.purge
                else ' Клон и база остались на диске (--purge удаляет клон).')
        print(f'Источник «{rec["id"]}» удалён из реестра.' + tail)
        return 0

    # watch
    if not repos.git_available():
        print('git не найден — установите Git и добавьте его в PATH',
              file=sys.stderr)
        return 2

    def sources_fn():
        return [r for r in repos.load_sources() if args.all or r.get('watch')]

    def sync_fn(source_id):
        # свежий UiCore каждый цикл — настройки могли изменить в интерфейсе
        results = UiCore().repos_sync(ids=[source_id])
        return results[0] if results else {}

    interval = max(10, args.interval)
    print(f'Слежение за git-источниками (интервал {interval} с). '
          'Ctrl+C — остановка.')
    try:
        repos.watch_loop(sources_fn, sync_fn, interval=interval,
                         once=args.once)
    except KeyboardInterrupt:
        print('\nСлежение остановлено.')
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cmd == 'repos':
        return run_repos(args)

    if args.cmd == 'hbk':
        return run_hbk(args)

    if args.cmd == 'check':
        return run_check(args.db)

    if args.cmd == 'update':
        return run_update(args)

    if args.cmd == 'extract':
        from .dump import is_dump_dir
        if is_dump_dir(args.src):
            return run_extract_dump(args)

    if args.cmd == 'fts':
        return run_fts(args.db, args.workers)

    if args.cmd == '1confdb-knw':
        from .mcp_server import main as mcp_main
        argv = list(args.db or ())
        if args.port:
            argv += ['--host', args.host, '--port', str(args.port)]
        for spec in args.group or ():
            argv += ['--group', spec]
        if args.syntax_db:
            argv += ['--syntax-db', args.syntax_db]
        return mcp_main(argv)

    if args.cmd == 'web':
        from .webui import main as web_main
        argv = ['--host', args.host, '--port', str(args.port)]
        if args.no_browser:
            argv.append('--no-browser')
        return web_main(argv)

    if args.cmd == 'bench':
        from .bench import bench as run_bench
        run_bench(args.src, sample=args.sample)
        return 0

    if args.workers is None:
        from .config import bench_workers
        args.workers = bench_workers() or 1

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
    if args.skip_errors:
        options['skip_errors'] = True
    if args.dump_indent:
        options['dump_indent'] = 2

    try:
        # отпечаток исходника считывается один раз: он и решает, нужна ли
        # распаковка, и уходит в source собранной базы
        src_sha256 = None
        if args.db:
            src_sha256, skip = check_same_source(args.src, args.db, args.force)
            if skip:
                return 0
        stats = extract(
            args.src,
            db_path=args.db,
            dump_dir=args.dump,
            temp_dir=args.temp_dir,
            keep_temp=args.keep_temp,
            options=options,
            workers=args.workers,
            build_fts=not args.no_fts,
            source_sha256=src_sha256,
        )
    except FileNotFoundError as err:
        print(f'Файл не найден: {err}', file=sys.stderr)
        return 2
    except PermissionError as err:
        # Windows: открытое sqlite-соединение чужого процесса держит файл —
        # пересборка базы (os.remove) невозможна без внятной подсказки
        print(f'Файл базы занят другим процессом: {err}', file=sys.stderr)
        _hint_db_holder(args.db)
        return 1
    except Exception as err:
        import sqlite3
        if isinstance(err, sqlite3.OperationalError) \
                and 'locked' in str(err).lower():
            print(f'База занята другим процессом: {err}', file=sys.stderr)
            _hint_db_holder(args.db)
            return 1
        print(f'Ошибка: {err}', file=sys.stderr)
        return 1

    for key in ('src', 'src_sha256', 'db', 'dump_dir', 'db_rows', 'elapsed'):
        if stats.get(key) is not None:
            print(f'{key}: {stats[key]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
