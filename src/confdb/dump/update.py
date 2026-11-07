"""Инкрементальное обновление базы знаний, собранной из выгрузки в файлы.

Принцип: LLM (или пользователь) правит файлы выгрузки; `update_db` находит
изменённые объекты и перезагружает только их.

Как определяется изменение (scan_changes):
- состояние каждого файла выгрузки хранится в `dump_file` (size, mtime_ns,
  sha256); быстрый проход — только `os.stat`: совпали размер и mtime → файл
  не менялся, хэш не пересчитывается (обход выгрузки 5.7 ГБ — секунды);
- stat не совпал → считаем SHA-256; отличается от сохранённого → файл (а с
  ним и гранула) изменён. Совпал → правили и вернули обратно, гранула чистая;
- гранула = объект верхнего уровня со всем деревом (XML, Ext, формы,
  команды, макеты): перезагружается целиком — DELETE meta_object по пути
  (FK-каскад убирает атрибуты/модули/методы/права-цели) + повторный разбор.

Межобъектные связи переразрешаются глобально (дешево): attribute_ref — по
target_path, role_right — по target_uuid (FK у них ON DELETE SET NULL, строки
ДРУГИХ объектов переживают переустановку цели), predefined_subconto — по
uuid, common_target/subsystem_content пересобираются целиком (~80 XML).
Права ролей, ссылавшихся на изменённые объекты, перечитываются из своих
Rights.xml, даже если сама роль не менялась.

Работа при запущенном MCP-сервере: база из выгрузки строится в WAL, поэтому
обновление идёт in-place одной транзакцией — сервер (read-only соединения)
продолжает отвечать и видит старое состояние до COMMIT, новое после; кэши
сервер сбрасывает по смене `PRAGMA data_version` (hot reload, см.
mcp_server). Остановка нужна только для ПОЛНОЙ пересборки (файл базы
удаляется — Windows не даст, пока сервер держит его открытым).
"""
import hashlib
import os
import sqlite3
from datetime import datetime

from ..db import writer as W
from ..v8 import helper
from . import model, xmlmeta
from .build import Builder, _OBJ_VERSION_CACHE, file_sha256


def scan_changes(conn, dump_dir, granule_files):
    """Сравнение выгрузки с сохранённым состоянием dump_file.

    :return: (dirty_granules, current, stored)
        dirty_granules — гранулы с изменёнными/новыми/удалёнными файлами;
        current — {rel_path: (granule, size, mtime_ns, sha256)} фактическое
            состояние (для неизменённых файлов хэш взят из базы, без чтения);
        stored — {rel_path: (granule, size, mtime_ns, sha256)} из dump_file.
    """
    stored = {}
    for path, granule, size, mtime, sha in conn.execute(
            'SELECT path, granule, size, mtime_ns, sha256 FROM dump_file'):
        stored[path] = (granule, size, mtime, sha)
    current = {}
    dirty = set()
    for granule, rels in granule_files.items():
        for rel in rels:
            full = os.path.join(dump_dir, *rel.split('/'))
            try:
                st = os.stat(helper.long_path(full))
            except OSError:
                continue
            prev = stored.get(rel)
            if prev and prev[1] == st.st_size and prev[2] == st.st_mtime_ns:
                sha = prev[3]
            else:
                sha = file_sha256(full)
                if not prev or prev[3] != sha:
                    dirty.add(granule)
            current[rel] = (granule, st.st_size, st.st_mtime_ns, sha)
    for rel, (granule, _s, _m, _sha) in stored.items():
        if rel not in current:
            dirty.add(granule)
    return dirty, current, stored


def server_pidfile(db_path):
    """Путь к pidfile работающего MCP-сервера: `<база>.server.json`."""
    return os.path.abspath(db_path) + '.server.json'


def read_server_pidfile(db_path):
    """(pid, port, cmdline) живого сервера или None.

    Файл пишет mcp_server при старте; pid проверяется на существование
    процесса (Windows: OpenProcess через ctypes — stdlib).
    """
    import json
    path = server_pidfile(db_path)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            info = json.load(f)
        pid = int(info.get('pid', 0))
    except (OSError, ValueError):
        return None
    if pid and not _pid_alive(pid):
        return None
    return info


def _pid_alive(pid):
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    code = ctypes.c_ulong()
    ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel32.CloseHandle(handle)
    return bool(ok) and code.value == 259   # STILL_ACTIVE


def _chunked(seq, size=500):
    seq = sorted(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _relink_attribute_refs(conn, paths=None):
    """Заполняет object_id/uuid ссылок по target_path.

    :param paths: сузить до перечня путей (инкремент — только переустанов-
        ленные объекты); None — глобально (сборка/миграция).
    """
    base = ('UPDATE attribute_ref SET'
            ' object_id=(SELECT id FROM meta_object'
            '            WHERE path=attribute_ref.target_path),'
            ' uuid=IFNULL((SELECT uuid FROM meta_object'
            '              WHERE path=attribute_ref.target_path), uuid)'
            ' WHERE target_path IS NOT NULL AND object_id IS NULL')
    if not paths:
        conn.execute(base)
        return
    for chunk in _chunked(paths):
        marks = ','.join('?' * len(chunk))
        conn.execute(base + f' AND target_path IN ({marks})', chunk)


def _relink_role_rights(conn, uuids=None):
    """Восстанавливает target_* у прав, обнулённые удалением цели.

    Порядок: реквизит/ТЧ/подобъект — по своим таблицам, затем target_object_id
    — сам объект по uuid либо объект-ВЛАДЕЛЕЦ уже разрешённой цели (соглашение
    общее для cf- и dump-баз, confdb.rights.TargetResolver: все права,
    затрагивающие объект, выбираются одним условием по target_object_id).

    :param uuids: сузить до перечня uuid (инкремент); None — глобально.
    """
    steps = (
        ('target_attr_id',
         '(SELECT a.id FROM meta_attribute a'
         ' WHERE a.uuid=role_right.target_uuid LIMIT 1)',
         'meta_attribute'),
        ('target_tabular_id',
         '(SELECT t.id FROM meta_tabular t'
         ' WHERE t.uuid=role_right.target_uuid LIMIT 1)',
         'meta_tabular'),
        ('target_subobject_id',
         '(SELECT s.id FROM meta_subobject s'
         ' WHERE s.uuid=role_right.target_uuid LIMIT 1)',
         'meta_subobject'),
        ('target_object_id',
         '(SELECT id FROM meta_object WHERE uuid=role_right.target_uuid)',
         'meta_object'),
    )
    for col, expr, table in steps:
        where = f'{col} IS NULL AND target_uuid IN (SELECT uuid FROM {table}' \
                f' WHERE uuid IS NOT NULL)'
        sql = f'UPDATE role_right SET {col}={expr} WHERE {where}'
        if not uuids:
            conn.execute(sql)
        else:
            for chunk in _chunked(uuids):
                marks = ','.join('?' * len(chunk))
                conn.execute(sql + f' AND target_uuid IN ({marks})', chunk)
    # владелец уже разрешённой цели: реквизита, ТЧ или подобъекта
    conn.execute(
        'UPDATE role_right SET target_object_id=COALESCE('
        '(SELECT object_id FROM meta_attribute WHERE id=target_attr_id),'
        '(SELECT object_id FROM meta_tabular WHERE id=target_tabular_id),'
        '(SELECT object_id FROM meta_subobject WHERE id=target_subobject_id))'
        ' WHERE target_object_id IS NULL AND (target_attr_id IS NOT NULL'
        ' OR target_tabular_id IS NOT NULL OR target_subobject_id IS NOT NULL)')


def _named_target_prefixes(dirty):
    """Точечные префиксы имён прав для переустановленных гранул.

    Имя цели из Rights.xml начинается с «Класс.Имя» — пути гранул
    ('Catalog/НовыйСправочник') переводятся в точечную форму; классы с
    множественными XML-именами (ChartOfCharacteristicTypes/Sequences/
    DocumentNumerators — CLASS_ALIAS) дают все варианты написания.
    """
    from ..rights import CLASS_ALIAS
    xml_names = {}
    for alias, db_type in CLASS_ALIAS.items():
        xml_names.setdefault(db_type, set()).add(alias)
    prefixes = set()
    for granule in dirty:
        if not granule or '/' not in granule:
            continue
        cls, name = granule.split('/', 1)
        for alias in xml_names.get(cls, (cls,)):
            prefixes.add(f'{alias}.{name}')
    return prefixes


def _reresolve_named_targets(conn, prefixes):
    """Повторяет разрешение целей, хранящихся ИМЕНЕМ, для переустановленных гранул.

    У неразрешённой строки `target_uuid` несёт точечное имя из Rights.xml
    (uuid содержит '-', имена — '.'). Цель могла появиться позже — например,
    гранула переустановлена новым кодом, который извлекает больше подобъектов
    (найдено на реальной базе: 8 прав на каналы сервиса интеграции висели
    именем, хотя строки в meta_subobject были). Имена переразрешаются общим
    резолвером (`rights.TargetResolver`), при успехе строка переписывается на
    uuid + id — так роли, чей Rights.xml не менялся, получают разрешённые цели.

    ОБЯЗАТЕЛЬНО сузить до префиксов переустановленных гранул
    (`_named_target_prefixes`): полный перебор неразрешённых имён стоит
    ~27 мс/имя (запросы резолвера), на БП это 3068 имён ≈ 84 с на каждое
    обновление; неиспользуемые префиксы ничего не стоят.

    :return: число имён, ставших разрешимыми.
    """
    if not prefixes:
        return 0
    names = set()
    for prefix in prefixes:
        for (name,) in conn.execute(
                'SELECT DISTINCT target_uuid FROM role_right'
                ' WHERE target_object_id IS NULL AND target_attr_id IS NULL'
                ' AND target_tabular_id IS NULL AND target_subobject_id IS NULL'
                ' AND (target_uuid=? OR target_uuid LIKE ?)',
                (prefix, prefix + '.%')):
            names.add(name)
    if not names:
        return 0
    from ..rights import TargetResolver
    resolver = TargetResolver(conn)
    fixed = 0
    for name in sorted(names):
        resolved = resolver.resolve(name)
        if resolved is None:
            continue
        target_uuid, obj_id, attr_id, tab_id, sub_id = resolved
        conn.execute(
            'UPDATE role_right SET target_uuid=?, target_object_id=?,'
            ' target_attr_id=?, target_tabular_id=?, target_subobject_id=?'
            ' WHERE target_uuid=?',
            (target_uuid, obj_id, attr_id, tab_id, sub_id, name))
        fixed += 1
    return fixed


def _relink_subconto(conn):
    conn.execute(
        'UPDATE predefined_subconto SET kind_id='
        ' (SELECT id FROM predefined WHERE uuid=predefined_subconto.uuid'
        '  LIMIT 1)'
        ' WHERE kind_id IS NULL AND uuid IS NOT NULL'
        " AND uuid != '00000000-0000-0000-0000-000000000000'"
        ' AND uuid IN (SELECT uuid FROM predefined WHERE uuid IS NOT NULL)')


def _rebuild_content(conn, objects, builder):
    """Полный пересчёт subsystem_content/common_target по XML выгрузки."""
    conn.execute('DELETE FROM subsystem_content')
    conn.execute('DELETE FROM common_target')
    builder.subsystem_content = []
    builder.common_targets = []
    for obj in objects:
        if obj['type'] not in ('Subsystem', 'CommonAttribute'):
            continue
        if not obj['xml'] or not os.path.isfile(obj['xml']):
            continue
        try:
            root = xmlmeta.class_element(xmlmeta.load(obj['xml']))
        except Exception:  # noqa: BLE001
            continue
        content = xmlmeta.subsystem_content(root) if obj['type'] == 'Subsystem' \
            else xmlmeta.common_attribute_content(root)
        if not content:
            continue
        if obj['type'] == 'Subsystem':
            builder.subsystem_content.append((obj['path'], content))
        else:
            builder.common_targets.append((obj['path'], content))
    builder.rebuild_content_tables()


def _manifest_sha(conn):
    """Отпечаток состава выгрузки по dump_file (формат = сборка)."""
    digest = hashlib.sha256()
    for path, sha in conn.execute(
            'SELECT path, sha256 FROM dump_file ORDER BY path'):
        digest.update(path.encode('utf-8'))
        digest.update(b'\0')
        digest.update(sha.encode('ascii'))
        digest.update(b'\n')
    return digest.hexdigest()


def update_db(dump_dir, db_path, *, workers=1, rebuild_fts=None, log=print):
    """Инкрементальное обновление базы из выгрузки; возвращает статистику.

    :param dump_dir: каталог выгрузки (тот же, из которого база собрана, или
        перемещённый — состояние ищется по относительным путям)
    :param db_path: база знаний; отсутствует или собрана из .cf — будет
        полная пересборка (`build_db`)
    :param workers: число процессов разбора BSL
    :param rebuild_fts: None — авто (пересборка, если менялись .bsl),
        True/False — принудительно
    :param log: функция вывода сообщений
    """
    from .build import build_db
    begin = datetime.now()
    dump_dir = os.path.abspath(dump_dir)
    if not os.path.isfile(os.path.join(dump_dir, 'ConfigDumpInfo.xml')):
        raise ValueError(
            f'это не каталог выгрузки конфигурации (нет ConfigDumpInfo.xml): {dump_dir}')
    db_path = os.path.abspath(db_path)

    def _full(reason):
        log(reason)
        return build_db(dump_dir, db_path, workers=workers,
                        build_fts=rebuild_fts if rebuild_fts is not None else True,
                        log=log)

    if not os.path.isfile(db_path):
        return _full('Базы нет — полная сборка из выгрузки.')
    conn = sqlite3.connect(db_path, timeout=60)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='dump_state'").fetchone()
        if not row or not conn.execute(
                'SELECT COUNT(*) FROM dump_state').fetchone()[0]:
            conn.close()
            return _full('База собрана не из выгрузки — полная пересборка.')
    except sqlite3.Error as err:
        conn.close()
        return _full(f'База не читается ({err}) — полная пересборка.')

    server = read_server_pidfile(db_path)
    if server:
        log(f'MCP-сервер запущен (pid {server.get("pid")}'
            f'{", порт " + str(server["port"]) if server.get("port") else ""})'
            ' — обновление идёт in-place (WAL), сервер подхватит изменения'
            ' автоматически; остановка не требуется.')

    log(f'Обход выгрузки {dump_dir}')
    import time as _time
    _t0 = _time.monotonic()
    objects, granule_files, warnings = model.scan(dump_dir)
    _t1 = _time.monotonic()
    dirty, current, stored = scan_changes(conn, dump_dir, granule_files)
    _t2 = _time.monotonic()
    stats = {'changed_granules': sorted(dirty), 'objects': 0, 'modules': 0,
             'methods': 0, 'files': 0, 'dump_files': 0, 'role_rights': 0,
             'role_rls_templates': 0, 'role_rights_failed': 0, 'warnings': 0}
    if not dirty:
        log('Изменений нет: выгрузка соответствует базе, обновление не нужно.')
        stats['elapsed'] = str(datetime.now() - begin)
        stats['dump_dir'] = dump_dir
        stats['db'] = db_path
        return stats
    log(f'Изменённых гранул: {len(dirty)}'
        f' (из {len(granule_files)}): {", ".join(sorted(dirty)[:8])}'
        f'{"…" if len(dirty) > 8 else ""}')

    _OBJ_VERSION_CACHE.clear()
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=60000')
    conn.execute('PRAGMA synchronous=NORMAL')
    known_paths = frozenset(o['path'] for o in objects if o['path'])
    bsl_before = False
    if rebuild_fts is None:
        bsl_before = any(rel.endswith('.bsl')
                         for g in dirty for rel in granule_files.get(g, ()))
    try:
        conn.execute('BEGIN IMMEDIATE')
        source_id = conn.execute(
            'SELECT id FROM source ORDER BY id LIMIT 1').fetchone()[0]
        builder = Builder(dump_dir, conn, source_id, workers=workers,
                          known_paths=known_paths, warnings=warnings)
        builder.path_id = dict(conn.execute('SELECT path, id FROM meta_object'))
        # top_ord нужен для ord переустанавливаемых объектов верхнего уровня —
        # Configuration.xml читается всегда (один файл)
        cfg_xml = os.path.join(dump_dir, 'Configuration.xml')
        if os.path.isfile(cfg_xml):
            cfg_root = xmlmeta.class_element(xmlmeta.load(cfg_xml))
            from .build import _top_path_by_tag
            for i, (tag, child_name) in enumerate(
                    xmlmeta.config_children(cfg_root)):
                p = _top_path_by_tag(tag, child_name, known_paths)
                if p is not None and p not in builder.top_ord:
                    builder.top_ord[p] = i

        by_granule = {}
        for obj in objects:
            by_granule.setdefault(obj['granule'], []).append(obj)
        stored_granules = {g for g, _s, _m, _h in stored.values()}

        affected_roles = set()
        removed_granules = 0
        structural = False
        # группировка текущего состояния по гранулам — один проход
        # (иначе O(грязные × все файлы) на каждой грануле)
        current_by_granule = {}
        for rel, (g, _s, _m, _h) in current.items():
            current_by_granule.setdefault(g, []).append(rel)
        for granule in sorted(dirty):
            objs = by_granule.get(granule)
            if not objs:
                # гранула исчезла из выгрузки — объект(ы) удалены
                removed_granules += 1
                structural = True
                _delete_granule(conn, granule, affected_roles, source_id)
                conn.execute('DELETE FROM dump_file WHERE granule=?',
                             (granule,))
                continue
            if granule == '':
                _update_root(conn, builder, objs[0], log)
            else:
                if not _delete_granule(conn, granule, affected_roles,
                                       source_id):
                    structural = True     # гранула новая — объект добавлен
                for obj in objs:
                    builder.process_object(obj)
            # файлы и хэши гранулы
            conn.execute('DELETE FROM dump_file WHERE granule=?', (granule,))
            conn.executemany(
                'INSERT INTO dump_file (path, granule, size, mtime_ns, sha256)'
                ' VALUES (?, ?, ?, ?, ?)',
                [(rel, granule, *current[rel][1:])
                 for rel in current_by_granule.get(granule, ())])
        # строки file — только для грязных гранул (хэши/размеры из кэша
        # scan_changes, без повторного чтения); у старой гранулы они уже
        # удалены каскадом, у корня — чистим явно
        _t3 = _time.monotonic()
        if '' in dirty:
            conn.execute('DELETE FROM file WHERE object_id='
                         "(SELECT id FROM meta_object WHERE path='')")
        dirty_granule_files = {g: current_by_granule.get(g, [])
                               for g in dirty}
        builder.write_modules()
        builder.write_files(dirty_granule_files, objects, current=current,
                            write_dump_state=False)
        stats['dump_files'] = sum(len(v) for v in dirty_granule_files.values())
        _t4 = _time.monotonic()
        # права ролей, затронутых переустановкой целей
        if affected_roles:
            names = conn.execute(
                'SELECT id, path FROM meta_object WHERE id IN (%s)'
                % ','.join('?' * len(affected_roles)),
                tuple(affected_roles)).fetchall()
            have = {rid for rid, _p in builder.roles}
            for rid, rpath in names:
                if rid not in have:
                    builder.roles.append((rid, rpath))
        if builder.roles:
            ids = [rid for rid, _p in builder.roles]
            marks = ','.join('?' * len(ids))
            conn.execute(f'DELETE FROM role_right WHERE role_id IN ({marks})',
                         ids)
            conn.execute(
                f'DELETE FROM role_rls_template WHERE role_id IN ({marks})',
                ids)
            conn.execute(
                f'DELETE FROM role_rights_state WHERE role_id IN ({marks})',
                ids)
            builder.write_rights()
        _t5 = _time.monotonic()
        # глобальная перелинковка
        _relink_attribute_refs(conn)
        _relink_role_rights(conn)
        named_fixed = _reresolve_named_targets(
            conn, _named_target_prefixes(dirty))
        _relink_subconto(conn)
        _t6 = _time.monotonic()
        _rebuild_content(conn, objects, builder)
        # ord верхнего уровня — из свежего Configuration.xml; только когда
        # корень менялся и ОБЯЗАТЕЛЬНО с source_id в WHERE: без него UPDATE
        # по path не использует ux_meta_object_path (полное сканирование на
        # каждую строку — 16.5 тыс. строк стоят 450 с против 0.2 с)
        if '' in dirty:
            conn.executemany(
                'UPDATE meta_object SET ord=? WHERE source_id=? AND path=?',
                [(i, source_id, p) for p, i in builder.top_ord.items()])
        # отпечаток состава и штамп времени
        conn.execute(
            'UPDATE source SET file_sha256=?, file=?, '
            'created=IFNULL(created, ?) WHERE id=?',
            (_manifest_sha(conn), dump_dir,
             datetime.now().isoformat(timespec='seconds'), source_id))
        conn.execute('UPDATE dump_state SET dump_dir=?, updated=? WHERE id=1',
                     (dump_dir, datetime.now().isoformat(timespec='seconds')))
        conn.commit()
        _t7 = _time.monotonic()
        log('тайминг: обход=%.1fс хэши=%.1fс гранулы=%.1fс модули+файлы=%.1fс '
            'права=%.1fс перелинковка=%.1fс составы+ord+коммит=%.1fс'
            % (_t1 - _t0, _t2 - _t1, _t3 - _t2, _t4 - _t3, _t5 - _t4,
               _t6 - _t5, _t7 - _t6))
        if named_fixed:
            log(f'Целей прав, хранившихся именем, разрешено повторно: {named_fixed}')
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    for key in ('objects', 'modules', 'methods', 'files',
                'role_rights', 'role_rls_templates', 'role_rights_failed'):
        stats[key] = builder.stats.get(key, 0)
    stats['removed_granules'] = removed_granules
    stats['structural'] = structural
    stats['rights_reresolved'] = named_fixed
    stats['warnings'] = len(warnings)
    if warnings:
        log(f'Предупреждений: {len(warnings)} (первые 5):')
        for w in warnings[:5]:
            log(f'  {w}')
    do_fts = rebuild_fts if rebuild_fts is not None else bsl_before
    if do_fts:
        log('Пересборка FTS-индекса')
        W.build_fts_index(db_path, workers=workers)
    stats['dump_dir'] = dump_dir
    stats['db'] = db_path
    stats['elapsed'] = str(datetime.now() - begin)
    log(f'Обновление готово за {stats["elapsed"]}')
    return stats


def _delete_granule(conn, granule, affected_roles, source_id=None):
    """Удаляет объекты гранулы (FK-каскад) и помечает затронутые роли.

    :return: True, если в базе были объекты гранулы (False — гранула новая,
        это структурное изменение: составы подсистем/общих реквизитов и
        перелинковка ссылок могут затрагивать другие объекты).
    """
    if source_id is not None:
        rows = conn.execute(
            'SELECT id FROM meta_object WHERE source_id=?'
            " AND (path=? OR path LIKE ? || '/%')",
            (source_id, granule, granule)).fetchall()
    else:
        rows = conn.execute(
            "SELECT id FROM meta_object WHERE path=? OR path LIKE ? || '/%'",
            (granule, granule)).fetchall()
    ids = [r[0] for r in rows]
    if not ids:
        return False
    marks = ','.join('?' * len(ids))
    for col, sub in (
            ('target_object_id', None),
            ('target_attr_id', 'SELECT id FROM meta_attribute WHERE object_id IN (%s)' % marks),
            ('target_tabular_id', 'SELECT id FROM meta_tabular WHERE object_id IN (%s)' % marks),
            ('target_subobject_id', 'SELECT id FROM meta_subobject WHERE object_id IN (%s)' % marks)):
        sql = f'SELECT DISTINCT role_id FROM role_right WHERE {col} IN '
        sql += f'({marks})' if sub is None else f'({sub})'
        for (rid,) in conn.execute(sql, ids):
            affected_roles.add(rid)
    conn.execute(f'DELETE FROM meta_object WHERE id IN ({marks})', ids)


def _update_root(conn, builder, root_obj, log):
    """Корневая гранула: UPDATE строки конфигурации + её модули."""
    if not root_obj['xml'] or not os.path.isfile(root_obj['xml']):
        log('Configuration.xml отсутствует — корень не обновлён')
        return
    root = xmlmeta.class_element(xmlmeta.load(root_obj['xml']))
    uuid, name, comment = xmlmeta.object_basics(root)
    with open(root_obj['xml'], 'r', encoding='utf-8', errors='replace') as f:
        header_text = f.read()
    from .build import _compat_digits
    digits = _compat_digits(
        xmlmeta.config_props_dict(root).get('CompatibilityMode') or '')
    conn.execute(
        "UPDATE meta_object SET name=?, uuid=?, comment=?, obj_version=?,"
        " header_json=? WHERE path=''",
        (name or '', uuid, comment, digits or None, header_text))
    conn.execute('UPDATE source SET root_type=?, root_name=?, root_uuid=?',
                 ('Configuration', name, uuid))
    row = conn.execute("SELECT id FROM meta_object WHERE path=''").fetchone()
    if row:
        builder.path_id[''] = row[0]
        conn.execute('DELETE FROM module WHERE object_id=?', (row[0],))
    builder._collect_root_modules(root_obj)
