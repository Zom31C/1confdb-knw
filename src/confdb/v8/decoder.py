"""Диспетчер типов/версий и рекурсивный разбор метаданных.

Порт decode-части v8unpack.decoder (MIT) — только разбор, без сборки.

Отклонение от эталона (осознанное): опция options['skip_errors'] — объект,
упавший при декодировании, пропускается с сообщением, разбор продолжается
(у эталона любая ошибка прерывает весь файл). В штатном режиме поведение
идентично эталону.

Отклонение от эталона (осознанное, 2026-11-19): определение версии терпимо к
режиму совместимости — номер версии платформы во внутреннем блоке version/
configinfo может быть замещён флагом '1' (реальная версия рядом, {1,80312}).
Эталон на этом падает «Не удалось определить парсер» (cfe) или слепо маппит
'1' в 8.2 (cf); здесь версия ищется по блоку (_find_platform_version), а для
cfe при отсутствии версии берётся базовый '803' (расширения существуют только
в 8.3). Обычные файлы разбираются идентично эталону.
"""
import os
import shutil
import sys
from datetime import datetime

from . import helper
from . import progress
from .meta_object import Configuration, ConfigurationExtension, ExternalDataProcessor
from .ext_exception import ExtException
from .metadata_types import MetaDataTypes

available_types = {
    'ExternalDataProcessor': ExternalDataProcessor,
    'ExternalReport': ExternalDataProcessor,  # .erf has the same binary format as .epf
    'Configuration': Configuration,
    'ConfigurationExtension': ConfigurationExtension
}

# счётчик пропущенных объектов (общий для пула процессов)
_ERR_COUNT = None


def _err_counter():
    global _ERR_COUNT
    if _ERR_COUNT is None:
        import multiprocessing
        _ERR_COUNT = multiprocessing.get_context('spawn').Value('I', 0)
    return _ERR_COUNT


def _reset_err_counter():
    global _ERR_COUNT
    _ERR_COUNT = None


def _worker_init(shared, err_count):
    """Инициализатор дочернего процесса пула: прогресс + счётчик ошибок."""
    global _ERR_COUNT
    _ERR_COUNT = err_count
    progress.attach_shared(shared)


def _skip_error(err, include_type):
    """В режиме skip_errors: учесть и напечатать ошибку вместо прерывания."""
    counter = _err_counter()
    with counter.get_lock():
        counter.value += 1
    inner = ''
    stack = getattr(err, 'stack', None)
    if stack:
        last = stack[-1]
        inner = f' ({last.get("message", "")}: {last.get("detail", "")})'
    title = getattr(err, 'title', None) or str(err)
    print(f'\nПропуск объекта {include_type}: {title}{inner}', file=sys.stderr)


def _find_platform_version(block, prefix='80'):
    """Ищет номер версии платформы ('802…'/'803…') в блоке версий.

    Обычно блок плоский — {80316,0}. Но в выгрузках в режиме совместимости
    первым элементом может быть флаг ('1'), а сама версия платформы — лежать
    рядом ({1,80312}); поэтому обходим все элементы, включая вложенные.
    Возвращает первую подходящую строку или None. Числа формата ('216') и
    флаги ('0', '1') под маску '80…' не попадают.
    """
    if isinstance(block, str):
        return block if block.startswith(prefix) and len(block) >= 3 else None
    if isinstance(block, (list, tuple)):
        for item in block:
            found = _find_platform_version(item, prefix)
            if found:
                return found
    return None


class Decoder:
    @staticmethod
    def get_handler_by_version_file(*, version=None, root=None, header=None, options=None, configinfo=None, **kwargs):
        if root:
            if int(version[0][0][0]) >= 216:
                _tmp = len(version[0][0])
                obj_type = MetaDataTypes(header[0][3][0])
                if _tmp == 2:
                    obj_version = '802'
                elif _tmp == 3:
                    obj_version = version[0][0][2][0][:3]
                    if len(obj_version) == 3:
                        pass
                    elif obj_version in ['1', '2']:
                        # '1'/'2' — маркер старого формата либо флаг режима
                        # совместимости. Во втором случае реальная версия
                        # платформы лежит рядом во внутреннем блоке ({1,80312}):
                        # ищем её, чтобы не принять 8.3 за 8.2; если версии нет —
                        # это legacy 8.2, как раньше.
                        real = _find_platform_version(version[0][0][2])
                        obj_version = real[:3] if real else '802'
                    else:
                        raise Exception(f'Not supported version {obj_version}')
                else:
                    raise Exception(f'Not supported version {_tmp}')
                try:
                    # if not options.get('version'):
                    options['obj_version'] = obj_version
                    return available_types[obj_type.name](options=options, obj_version=options['obj_version'])
                except KeyError:
                    raise Exception(f'Not supported type {obj_type.name}')
            elif version[0][0][0] == "106":
                options['obj_version'] = '801'
                return ExternalDataProcessor(options=options, obj_version=options['obj_version'])
        if configinfo:
            try:
                fmt = int(configinfo[0][1][0])
            except (IndexError, TypeError, ValueError):
                fmt = 0
            if fmt >= 216:
                # Расширения (cfe) существуют только в 8.3, поэтому наличие
                # configinfo с форматом >= 216 однозначно определяет парсер.
                # Номер версии платформы во внутреннем блоке может быть замещён
                # флагом режима совместимости ('1'), а реальная версия — лежать
                # рядом ({1,80312}). Ищем '803…' по блоку версий; если не нашли —
                # берём базовый '803' (младшая версия на логику разбора не влияет).
                obj_version = _find_platform_version(configinfo[0][1], '803') or '803'
                options['obj_version'] = obj_version
                return ConfigurationExtension(options=options, obj_version=obj_version)
        raise Exception('Не удалось определить парсер')

    @classmethod
    def detect_version(cls, src_dir, options=None):
        version = None
        root = None
        header = None
        configinfo = None
        if os.path.isfile(os.path.join(src_dir, 'root')):
            version = helper.brace_file_read(src_dir, 'version')
            root = helper.brace_file_read(src_dir, 'root')
            header = helper.brace_file_read(src_dir, root[0][1])
        if os.path.isfile(os.path.join(src_dir, 'configinfo')):
            configinfo = helper.brace_file_read(src_dir, 'configinfo')

        return cls.get_handler_by_version_file(
            version=version,
            root=root,
            header=header,
            options=options,
            configinfo=configinfo
        )

    @classmethod
    def decode(cls, src_dir, dest_dir, *, pool=None, options=None, workers=1):
        begin = datetime.now()
        print(f'{"Разбираем объект":30}')
        # обход всего дерева стадии 1 нужен только проценту индикатора: когда он не
        # рисуется (пайп, редирект), это десятки тысяч os.path.getsize впустую
        show_progress = progress.is_enabled()
        total = 0
        if show_progress:
            for dirpath, _, filenames in os.walk(src_dir):
                for fn in filenames:
                    total += os.path.getsize(os.path.join(dirpath, fn))

        own_pool = False
        _reset_err_counter()
        err_count = _err_counter()
        if workers and workers > 1 and pool is None:
            import multiprocessing
            ctx = multiprocessing.get_context('spawn')
            shared = ctx.Value('Q', 0) if show_progress else None
            progress.start('Декодируем метаданные', total, shared=shared)
            pool = ctx.Pool(workers, initializer=_worker_init, initargs=(shared, err_count))
            own_pool = True
        else:
            progress.start('Декодируем метаданные', total)
        try:
            decoder = cls.detect_version(src_dir, options=options)
            if options and options.get('stream_sink'):
                helper.stream_begin(options['stream_root'], options['stream_sink'])
            helper.clear_dir(dest_dir)
            tasks = decoder.decode(src_dir, dest_dir)  # возвращает список вложенных объектов MetaDataObject
            while tasks:  # рекурсивно декодируем вложенные объекты MetaDataObject
                tasks = helper.run_in_pool(cls.decode_include, tasks, pool, title=f'{"Разбираем вложенные объекты":30}',
                                           need_result=True)
        finally:
            if own_pool:
                pool.close()
                pool.join()
            progress.finish()
            # поток заголовков должен попасть на диск до того, как его прочитает
            # запись БД: в рабочих процессах его закрывает atexit, здесь — свой
            helper.sink_close()
            helper.payload_close()
        if err_count.value:
            print(f'ВНИМАНИЕ: пропущено объектов с ошибками декодирования: '
                  f'{err_count.value} — дамп и база неполные.')
        print(f'{"Разбор объекта закончен":30}: {datetime.now() - begin}')

    @classmethod
    def decode_include(cls, params):
        include_type, (obj_uuid, src_dir, dest_dir, new_dest_path, parent_container_uuid, options) = params
        if options and options.get('stream_sink'):
            helper.stream_begin(options['stream_root'], options['stream_sink'])
        try:
            handler = helper.get_class_metadata_object(include_type)
            tasks = handler.decode(obj_uuid, src_dir, dest_dir, new_dest_path, options,
                                   parent_container_uuid=parent_container_uuid,
                                   parent_type=include_type)
            return tasks
        except ExtException as err:
            if (options or {}).get('skip_errors'):
                _skip_error(err, include_type)
                return []
            raise ExtException(
                parent=err,
                action=f'{cls.__name__}.decode_include {include_type}'
            )
        except Exception as err:
            if (options or {}).get('skip_errors'):
                _skip_error(ExtException(parent=err), include_type)
                return []
            raise ExtException(parent=err, action=f'{cls.__name__}.decode_include')


def decode(src_dir, dest_dir, *, pool=None, options=None, workers=1):
    containers = sorted(os.listdir(src_dir), key=lambda x: int(x) if x.isdigit() else x)
    containers_count = len(containers)
    if containers_count not in [1, 2]:
        raise NotImplementedError(f'Количество контейнеров {containers_count}')

    _src_dir = os.path.join(src_dir, containers[-1])
    Decoder.decode(_src_dir, dest_dir, pool=pool, options=options, workers=workers)
    if containers_count == 2:
        dummy_base = os.path.join(dest_dir, 'dummy')
        shutil.make_archive(dummy_base, 'zip', os.path.join(src_dir, '0'))
        if helper.stream_active():
            dummy_zip = dummy_base + '.zip'
            with open(helper.long_path(dummy_zip), 'rb') as file:
                helper._stream_put(dest_dir, 'dummy.zip', file.read())
            os.remove(helper.long_path(dummy_zip))
            helper.payload_close()
