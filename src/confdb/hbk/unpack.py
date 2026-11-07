"""Распаковщик файлов контекстной справки 1С:Предприятие (.hbk).

Формат shcntx_ru.hbk (проверено на файле из 1cv8 8.3.26.1498):

  [заголовок 1С, 1656 байт]
  [основной ZIP: ~51629 файлов objects/... и tables/...]   <- то, что нужно
  [хвостовые сегменты: индексные «паки» 1С]                 <- 7-Zip игнорирует

Особенности, из-за которых стандартный zipfile не справляется:
  * ZIP записан не с начала файла, а со смещением (base offset = 1656):
    все offset'ы central directory относительные, физически архив сдвинут;
  * после основного ZIP лежат ещё два EOCD (хвостовые сегменты), и zipfile
    берёт последний из них — у него записанный offset CD не совпадает с
    реальным положением, отсюда «Bad magic number for central directory».

Алгоритм:
  1. найти в файле все EOCD (PK\\x05\\x06);
  2. для каждого (с конца файла) проверить целостность: central directory
     из n записей заканчивается ровно на EOCD, и у каждой записи на месте
     base+local_ofs стоит локальный заголовок PK\\x03\\x04;
  3. первый валидный сегмент (ближайший к концу) считается основным архивом;
  4. файлы извлекаются по записям CD, CRC32 и размеры сверяются.

Имена записей: bit 11 флагов = UTF-8, иначе OEM-кодировка 1С (cp866),
fallback cp437. Пути проверяются на выход за пределы каталога (safe_join).
"""

import os
import struct
import zlib

SIG_LOCAL = b'PK\x03\x04'
SIG_CD = b'PK\x01\x02'
SIG_EOCD = b'PK\x05\x06'

ST_EOCD = struct.Struct('<4sHHHHIIH')
ST_CD = struct.Struct('<4sHHHHHHIIIHHHHHII')
ST_LOCAL = struct.Struct('<4sHHHHHIIIHH')

METHOD_STORED = 0
METHOD_DEFLATE = 8


class HbkError(Exception):
    """Файл .hbk не читается: разрушена структура ZIP или данные записи."""


def find_eocds(data):
    """Все позиции EOCD в файле (по возрастанию)."""
    offs = []
    start = 0
    while True:
        i = data.find(SIG_EOCD, start)
        if i == -1:
            break
        offs.append(i)
        start = i + 1
    return offs


def parse_eocd(data, pos):
    (sig, disk, cd_disk, n_disk, n_total, cd_size, cd_offset,
     comment_len) = ST_EOCD.unpack_from(data, pos)
    if sig != SIG_EOCD:
        raise HbkError(f'нет EOCD на {pos}')
    if disk != 0 or cd_disk != 0 or n_disk != n_total:
        raise HbkError(f'многотомные ZIP не поддерживаются (EOCD на {pos})')
    if n_total > 0xFFFF - 1 or cd_size >= 0xFFFFFFFF:
        raise HbkError(f'ZIP64 не поддерживается (EOCD на {pos})')
    return dict(pos=pos, n_total=n_total, cd_size=cd_size,
                cd_offset=cd_offset, comment_len=comment_len)


def decode_name(raw, flags):
    """Имя записи: bit 11 = UTF-8, иначе OEM-кодировка 1С (cp866)."""
    if flags & 0x800:
        return raw.decode('utf-8')
    try:
        return raw.decode('cp866')
    except UnicodeDecodeError:
        return raw.decode('cp437', 'replace')


def validate_segment(data, eocd_pos):
    """Проверка ZIP-сегмента: CD целиком валиден и все local-заголовки на месте.

    Возвращает (concat, entries) или None, если сегмент не читается.
    concat — смещение начала архива в файле (у основного архива = 1656).
    """
    try:
        eocd = parse_eocd(data, eocd_pos)
    except HbkError:
        return None
    concat = eocd_pos - eocd['cd_size'] - eocd['cd_offset']
    if concat < 0:
        return None

    entries = []
    pos = concat + eocd['cd_offset']
    end = pos + eocd['cd_size']
    for _ in range(eocd['n_total']):
        if pos + ST_CD.size > end or data[pos:pos + 4] != SIG_CD:
            return None
        (sig, ver_made, ver_need, flags, method, mtime, mdate, crc,
         csize, usize, nlen, elen, clen, disk, iattr, eattr,
         lofs) = ST_CD.unpack_from(data, pos)
        if nlen == 0 or nlen > 4096 or pos + 46 + nlen + elen + clen > end:
            return None
        name = decode_name(data[pos + 46:pos + 46 + nlen], flags)
        entries.append(dict(name=name, flags=flags, method=method, crc=crc,
                            csize=csize, usize=usize, lofs=lofs))
        pos += 46 + nlen + elen + clen
    if pos != eocd_pos:  # CD должен заканчиваться ровно на EOCD
        return None

    for e in entries:
        lp = concat + e['lofs']
        if lp < concat or lp + ST_LOCAL.size > len(data):
            return None
        if data[lp:lp + 4] != SIG_LOCAL:
            return None
    return concat, entries


def pick_main_segment(data):
    """Основной архив: последний EOCD с полностью валидным сегментом."""
    for eocd_pos in reversed(find_eocds(data)):
        seg = validate_segment(data, eocd_pos)
        if seg is not None:
            return eocd_pos, seg
    raise HbkError('в файле не найдено ни одного читаемого ZIP-сегмента')


def read_entry(data, concat, e):
    """Распаковать одну запись; возвращает байты содержимого."""
    lp = concat + e['lofs']
    (sig, ver, flags, method, mtime, mdate, crc, csize_loc, usize_loc,
     nlen, elen) = ST_LOCAL.unpack_from(data, lp)
    if sig != SIG_LOCAL:
        raise HbkError(f'разрушен local-заголовок {e["name"]!r}')
    start = lp + ST_LOCAL.size + nlen + elen
    blob = data[start:start + e['csize']]
    if len(blob) != e['csize']:
        raise HbkError(f'обрезаны данные {e["name"]!r}')

    if e['method'] == METHOD_STORED:
        raw = blob
    elif e['method'] == METHOD_DEFLATE:
        d = zlib.decompressobj(-15)
        raw = d.decompress(blob) + d.flush()
    else:
        raise HbkError(f'{e["name"]!r}: метод сжатия {e["method"]} '
                       f'не поддерживается')

    if len(raw) != e['usize']:
        raise HbkError(f'{e["name"]!r}: размер {len(raw)} != {e["usize"]}')
    if zlib.crc32(raw) & 0xFFFFFFFF != e['crc']:
        raise HbkError(f'{e["name"]!r}: CRC32 не совпадает')
    return raw


def safe_join(outdir, name):
    """outdir + имя записи; защита от выхода за пределы каталога."""
    rel = name.replace('\\', '/').lstrip('/')
    parts = [p for p in rel.split('/') if p and p != '.']
    if any(p == '..' for p in parts):
        raise HbkError(f'небезопасный путь в архиве: {name!r}')
    target = os.path.normpath(os.path.join(outdir, *parts))
    if not target.startswith(os.path.normpath(outdir) + os.sep):
        raise HbkError(f'путь вне каталога назначения: {name!r}')
    return target


def extract(data, concat, entries, outdir, verify_only=False, progress=None):
    """Извлечь записи в outdir. Возвращает (файлов, каталогов, дубликатов).

    CRC32 проверяется всегда — и при verify_only, и при записи файлов.
    progress(line) — печать хода работы (каждые 10000 записей).
    """
    n_files = n_dirs = n_dup = 0
    seen = set()
    total = len(entries)
    for i, e in enumerate(entries):
        if e['name'].endswith('/'):
            if not verify_only:
                os.makedirs(safe_join(outdir, e['name']), exist_ok=True)
            n_dirs += 1
            continue
        raw = read_entry(data, concat, e)  # CRC проверяется всегда
        if e['name'] in seen:
            n_dup += 1
        seen.add(e['name'])
        if not verify_only:
            target = safe_join(outdir, e['name'])
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, 'wb') as f:
                f.write(raw)
        n_files += 1
        if progress and ((i + 1) % 10000 == 0 or i + 1 == total):
            progress(f'  обработано {i + 1}/{total} записей...')
    return n_files, n_dirs, n_dup


def describe_segments(data):
    """Сводка по всем EOCD-сегментам файла — список строк для печати."""
    lines = []
    for eocd_pos in find_eocds(data):
        try:
            eocd = parse_eocd(data, eocd_pos)
        except HbkError as exc:
            lines.append(f'  EOCD@{eocd_pos}: {exc}')
            continue
        seg = validate_segment(data, eocd_pos)
        status = 'валиден' if seg else 'не читается как обычный ZIP'
        lines.append(f'  EOCD@{eocd_pos}: записей={eocd["n_total"]}, '
                     f'base={seg[0] if seg else "—"}, {status}')
    return lines


def unpack_hbk(path, outdir='extracted', verify_only=False, progress=None):
    """Распаковать файл .hbk в каталог outdir.

    Возвращает словарь-сводку: size, eocd_pos, base, entries, files, dirs,
    duplicates. verify_only=True — только проверка CRC, файлы не пишутся.
    """
    with open(path, 'rb') as f:
        data = f.read()
    eocd_pos, (concat, entries) = pick_main_segment(data)
    n_files, n_dirs, n_dup = extract(data, concat, entries, outdir,
                                     verify_only=verify_only,
                                     progress=progress)
    return {'size': len(data), 'eocd_pos': eocd_pos, 'base': concat,
            'entries': len(entries), 'files': n_files, 'dirs': n_dirs,
            'duplicates': n_dup}


def list_entries(path, limit=50):
    """Список записей основного архива: [(usize, name)], всего записей."""
    with open(path, 'rb') as f:
        data = f.read()
    _eocd_pos, (_concat, entries) = pick_main_segment(data)
    brief = [(e['usize'], e['name']) for e in entries[:limit]]
    return brief, len(entries)


def verify_hbk(path, progress=None):
    """Проверить CRC32 и размеры всех записей, не записывая файлы.

    Возвращает словарь-сводку (как unpack_hbk).
    """
    with open(path, 'rb') as f:
        data = f.read()
    eocd_pos, (concat, entries) = pick_main_segment(data)
    n_files, n_dirs, n_dup = extract(data, concat, entries, '',
                                     verify_only=True, progress=progress)
    return {'size': len(data), 'eocd_pos': eocd_pos, 'base': concat,
            'entries': len(entries), 'files': n_files, 'dirs': n_dirs,
            'duplicates': n_dup}
