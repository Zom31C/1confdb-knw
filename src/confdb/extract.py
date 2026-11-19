"""Конвейер: cf/cfe/epf → распакованные файлы → база данных SQLite.

Стадии повторяют поток v8unpack (только распаковка):
  0 — чтение внешних контейнеров (32/64-бит) в файлы как есть;
  1 — inflate (raw deflate) + рекурсивные вложенные контейнеры;
  3 — декодирование метаданных: скобкофайлы → JSON, тексты модулей → .bsl.
(стадия 2 оригинала — конвертация в json отдельным прогоном — отключена и там)
"""
import concurrent.futures
import hashlib
import os
import shutil
import tempfile
from datetime import datetime

from .v8 import container_reader
from .v8 import decoder as v8_decoder
from .v8.helper import long_path

HASH_CHUNK = 1 << 20


def file_sha256(path, chunk=HASH_CHUNK):
    """SHA-256 файла, прочитанного кусками по 1 МиБ.

    .cf весит сотни мегабайт (УНФ — 885 МБ), поэтому файл не читается в память
    целиком. Полный обход стоит 1,74 с (замер `_tmp/hash_bench.py`) — 2,5% от
    извлечения той же конфигурации (~70 с), так что отпечаток можно считать ДО
    стадии 0 и отсечь повторную распаковку того же файла.

    :param path: путь к .cf/.cfe/.epf
    :return: шестнадцатеричный SHA-256
    """
    digest = hashlib.sha256()
    with open(long_path(path), 'rb') as fh:
        for block in iter(lambda: fh.read(chunk), b''):
            digest.update(block)
    return digest.hexdigest()


def source_info(db_path):
    """Что записано в `source` готовой базы: file, created, file_sha256, file_size.

    :param db_path: путь к базе знаний (может отсутствовать)
    :return: словарь либо None — файла нет, это не база знаний confdb или строки
        source в ней нет. Ключи `file_sha256`/`file_size` присутствуют всегда, но
        равны None в базах, собранных до появления отпечатка: сравнение с такой
        базой невозможно, и это не ошибка.
    """
    if not db_path or not os.path.isfile(db_path):
        return None
    # sqlite импортируется здесь: извлечение без --db не должно его тянуть
    import sqlite3
    uri = 'file:' + os.path.abspath(db_path).replace(os.sep, '/') + '?mode=ro'
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return None
    try:
        cols = {row[1] for row in conn.execute('PRAGMA table_info(source)')}
        if 'file' not in cols:
            return None
        names = [c for c in ('file', 'created', 'file_sha256', 'file_size',
                             'root_name', 'root_type', 'extractor_version')
                 if c in cols]
        row = conn.execute(
            f'SELECT {", ".join(names)} FROM source ORDER BY id LIMIT 1'
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row:
        return None
    info = dict(zip(names, row))
    info.setdefault('file_sha256', None)
    info.setdefault('file_size', None)
    info.setdefault('root_name', None)
    info.setdefault('root_type', None)
    info.setdefault('extractor_version', None)
    return info


def check_same_source(src_file, db_path, force=False, log=print):
    """Не собрана ли целевая база из этого же файла — до распаковки.

    Сравнивает SHA-256 исходника с отпечатком в `source` базы `db_path`. Ответ на
    вопрос пользователя «не извлекаю ли я одну и ту же конфигурацию повторно»:
    совпавший отпечаток означает, что база уже содержит ровно этот файл, и
    ~70 с распаковки можно не платить.

    :param src_file: путь к .cf/.cfe/.epf
    :param db_path: целевая база (может отсутствовать или быть старой — без отпечатка)
    :param force: пересобрать даже при совпадении (аналог --force)
    :param log: куда печатать пояснения (по умолчанию print)
    :return: (sha256, skip). sha256 передают в `extract(source_sha256=…)`, чтобы не
        считать его второй раз; skip=True — извлечение не нужно.
    """
    sha256 = file_sha256(src_file)
    stored = source_info(db_path)
    if not stored or not stored.get('file_sha256'):
        return sha256, False
    if stored['file_sha256'] != sha256:
        log(f'Целевая база {db_path} собрана из другого файла '
            f'({stored.get("file") or "?"}) — она будет перезаписана.')
        return sha256, False
    if force:
        log('Отпечаток совпал с уже собранной базой, но задано принудительное '
            'извлечение — пересобираю.')
        return sha256, False
    log(f'Извлечение не требуется: {db_path} уже собрана из этого же файла.')
    log(f'SHA-256 {sha256} совпал; база собрана {stored.get("created")} '
        f'из {stored.get("file")}.')
    log('Пересобрать принудительно — --force.')
    return sha256, True


def remove_tree(root, workers=8):
    """Удаляет дерево параллельно, проглатывая ошибки, как shutil.rmtree(ignore_errors=True).

    Рабочий каталог стадий 0/1/3 — это ~125 тысяч файлов и 5.2 ГиБ, и однопоточный
    `shutil.rmtree` стоит на нём 13-24 с, которые целиком входят в `elapsed`.
    `os.unlink` освобождает GIL, поэтому пул потоков по каталогам даёт ~2.4x: замер
    `_tmp/prof_cleanup_ab.py` на реальном дереве — 23.8 с против 10.1 с при 8 потоках
    (16 потоков не быстрее). Стоимость удаления определяет число файлов, а не объём:
    4488 файлов на 979 МиБ удаляются за 0.32 с.

    :param root: каталог для удаления (несуществующий — не ошибка)
    :param workers: число потоков
    """
    if not os.path.isdir(root):
        return
    entries = [(dirpath, filenames) for dirpath, _dirnames, filenames in os.walk(root)]

    def clear(entry):
        dirpath, filenames = entry
        for name in filenames:
            try:
                os.unlink(os.path.join(dirpath, name))
            except OSError:
                pass

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            list(pool.map(clear, entries))
    except Exception:  # noqa: BLE001 — удаление рабочего каталога не должно ронять извлечение
        shutil.rmtree(root, ignore_errors=True)
        return
    # каталоги снизу вверх: к этому моменту они пусты
    for dirpath, _filenames in sorted(entries, key=lambda e: e[0].count(os.sep), reverse=True):
        try:
            os.rmdir(dirpath)
        except OSError:
            pass
    if os.path.isdir(root):
        shutil.rmtree(root, ignore_errors=True)


def make_temp_dir(target=None):
    """Создаёт рабочий каталог стадий 0-1 на томе результата, а не в %TEMP%.

    Удаление ~125 тысяч файлов рабочего каталога на системном томе стоит 29.9 с и
    почти не распараллеливается (`remove_tree` даёт 1.04x), на несистемном — 23.8 с
    однопоточно и 10.1 с пулом потоков; прогон УНФ целиком 93 с против 67.5 с
    (замер `_tmp/prof_cleanup_ab.py`). Если каталог результата недоступен,
    остаётся временный каталог ОС.

    :param target: путь к базе или дампу, чей том предпочтителен (None — %TEMP%)
    :return: путь к созданному каталогу
    """
    base = os.path.dirname(os.path.abspath(target)) if target else None
    try:
        return tempfile.mkdtemp(prefix='confdb_', dir=base)
    except OSError:
        return tempfile.mkdtemp(prefix='confdb_')


def decode_tree(src_file, temp_dir, options=None, workers=1, dump_dir=None,
                headers_dir=None):
    """Стадии 0/1/3: контейнеры → inflate → декодированное дерево stage 3.

    Общая часть полного извлечения (`extract`) и инкрементального обновления
    базы прямо из cf (`cf_update`): возвращает каталог stage 3. С headers_dir
    заголовки объектов уходят потоком (helper.sink_put), минуя файлы
    <Класс>.json дампа; без него дерево полное (заголовки в JSON-файлах).
    """
    if options is None:
        options = {}
    stage0 = os.path.join(temp_dir, 'decode_stage_0')
    stage1 = os.path.join(temp_dir, 'decode_stage_1')
    print(f'Стадия 0: читаем контейнеры {src_file}')
    container_reader.extract(src_file, stage0, deflate=False, recursive=False)
    print('Стадия 1: разжимаем файлы контейнеров')
    container_reader.decompress_and_extract(stage0, stage1)
    if dump_dir:
        dump_dir = os.path.abspath(dump_dir)
        stage3 = dump_dir
    else:
        stage3 = os.path.join(temp_dir, 'decode_stage_3')
    decode_options = options
    if headers_dir:
        decode_options = dict(options)
        decode_options['header_sink'] = headers_dir
        decode_options['dump_headers'] = bool(dump_dir)
    print(f'Стадия 3: декодируем метаданные в {stage3}')
    v8_decoder.decode(stage1, stage3, options=decode_options, workers=workers)
    return stage3


def decode_tree_stream(src_file, temp_dir, options=None, workers=1):
    """Стадии 0/1 + декодирование сразу в виртуальное дерево (без диска стадии 3).

    Возвращает (virtual_root, payload_dir, headers_dir): файлы декодера
    перехватываются на уровне I/O и пишутся потоком payload-<pid>.pkl (v8/helper),
    запись БД читает их как дерево {rel-путь: байты}; заголовки дополнительно идут
    быстрым потоком headers_dir. Дисковое дерево стадии 3 не создаётся — экономия
    записи, обратного чтения и очистки. Для полной сборки БД; cf-инкремент и дамп
    используют decode_tree с дисковым деревом.
    """
    from .v8 import helper
    if options is None:
        options = {}
    stage0 = os.path.join(temp_dir, 'decode_stage_0')
    stage1 = os.path.join(temp_dir, 'decode_stage_1')
    print(f'Стадия 0: читаем контейнеры {src_file}')
    container_reader.extract(src_file, stage0, deflate=False, recursive=False)
    print('Стадия 1: разжимаем файлы контейнеров')
    container_reader.decompress_and_extract(stage0, stage1)
    virtual_root = os.path.join(temp_dir, 'decode_stage_3')
    payload_dir = os.path.join(temp_dir, 'payload')
    headers_dir = os.path.join(temp_dir, 'headers')
    decode_options = dict(options)
    decode_options['stream_root'] = virtual_root
    decode_options['stream_sink'] = payload_dir
    decode_options['header_sink'] = headers_dir
    # заголовки идут и в быстрый поток (запись БД), и в виртуальное дерево
    # (отпечатки гранул cf_granule считают все файлы, включая заголовки)
    decode_options['dump_headers'] = True
    print('Стадия 3: декодируем метаданные в поток (без диска)')
    v8_decoder.decode(stage1, virtual_root, options=decode_options, workers=workers)
    helper.stream_end()
    return virtual_root, payload_dir, headers_dir


def extract(src_file, *, db_path=None, dump_dir=None, temp_dir=None, keep_temp=False, options=None,
            workers=1, build_fts=True, source_sha256=None, stream=None):
    """Распаковывает файл 1С и (опционально) загружает результат в SQLite.

    :param src_file: путь к .cf/.cfe/.epf
    :param db_path: путь к целевой базе SQLite (None — не писать БД)
    :param dump_dir: каталог для распакованного дерева (стадия 3); None — не сохранять
    :param temp_dir: рабочий каталог для стадий 0-1; None — на томе базы или дампа
        (`make_temp_dir`), а при его недоступности — временный каталог ОС
    :param keep_temp: не удалять рабочий каталог стадий 0-1
    :param options: словарь опций декодера (prefix, auto_include и т.п.)
    :param workers: число процессов стадии 3 (1 — последовательно)
    :param build_fts: строить FTS5-индекс по телам методов (половина времени
        записи БД; без него база рабочая, индекс собирается позже — `confdb fts`)
    :param source_sha256: готовый отпечаток исходного файла (`file_sha256`).
        None — посчитать здесь; вызывающая сторона передаёт его, когда уже
        считала для проверки «не та же ли это конфигурация»
    :param stream: полная сборка БД потоком мимо дискового дерева стадии 3
        (декодер отдаёт файлы в память, запись читает их оттуда). None — авто:
        включено для сборки БД без дампа; False — дисковое дерево (`--no-stream`,
        отладка). На сборку дампа (`dump_dir`) и инкремент не влияет
    :return: словарь со статистикой
    """
    src_file = os.path.abspath(src_file)
    if not os.path.isfile(src_file):
        raise FileNotFoundError(src_file)

    if options is None:
        options = {}

    # stream=None — авто: полная сборка БД (без дампа) идёт потоком мимо диска
    # стадии 3; явные True/False переопределяют (False — дисковое дерево, --no-stream)
    if stream is None:
        stream = bool(db_path) and not dump_dir

    # отпечаток исходника пишется в source: по нему повторное извлечение той же
    # конфигурации видно до распаковки, а две базы — сравниваются на происхождение
    src_size = os.path.getsize(src_file)
    if source_sha256 is None:
        source_sha256 = file_sha256(src_file)
    print(f'SHA-256 исходного файла: {source_sha256} ({src_size} байт)')

    own_temp = temp_dir is None
    if own_temp:
        temp_dir = make_temp_dir(db_path or dump_dir)

    stats = {'src': src_file, 'src_sha256': source_sha256, 'src_size': src_size,
             'temp_dir': temp_dir, 'dump_dir': dump_dir, 'db': db_path}
    begin = datetime.now()
    try:
        if stream and db_path and not dump_dir:
            # полная сборка без дискового дерева стадии 3: декодер отдаёт файлы
            # потоком, запись БД читает их из памяти (v8/helper.payload_read)
            from .v8 import helper
            from .db.writer import write_db
            virtual_root, payload_dir, headers_dir = decode_tree_stream(
                src_file, temp_dir, options=options, workers=workers)
            vt = helper.payload_read(payload_dir)
            db_path = os.path.abspath(db_path)
            print(f'Пишем базу данных {db_path} (поток, без диска стадии 3)')
            stats['db_rows'] = write_db(virtual_root, db_path, source_file=src_file,
                                        source_sha256=source_sha256,
                                        source_size=src_size,
                                        store_blobs=options.get('store_blobs', False),
                                        workers=workers, build_fts=build_fts,
                                        headers_dir=headers_dir, virtual_tree=vt)
            stats['stream'] = True
        else:
            # Дисковое дерево (--no-stream или дамп). Поток заголовков включаем
            # только когда дерево дампа и так пишет заголовки (dump_dir). Для
            # --no-stream сборки БД дерево полное, с заголовками: отпечатки гранул
            # cf_granule тогда совпадают с cf_update (он считает по полному дереву
            # с заголовками); sink-дерево без заголовков давало бы рассинхрон.
            headers_dir = os.path.join(temp_dir, 'headers') if (db_path and dump_dir) else None
            stage3 = decode_tree(src_file, temp_dir, options=options, workers=workers,
                                 dump_dir=dump_dir, headers_dir=headers_dir)

            if db_path:
                # импорт здесь, чтобы не тянуть sqlite при работе без БД
                from .db.writer import write_db
                db_path = os.path.abspath(db_path)
                print(f'Пишем базу данных {db_path}')
                stats['db_rows'] = write_db(stage3, db_path, source_file=src_file,
                                            source_sha256=source_sha256,
                                            source_size=src_size,
                                            store_blobs=options.get('store_blobs', False),
                                            workers=workers, build_fts=build_fts,
                                            headers_dir=headers_dir)

        stats['dump_dir'] = dump_dir if dump_dir else None
    finally:
        if own_temp and not keep_temp:
            remove_tree(temp_dir, workers=max(workers, 4))
            stats['temp_dir'] = None

    stats['elapsed'] = str(datetime.now() - begin)
    print(f'Готово за {stats["elapsed"]}')
    return stats
