"""Инкрементальное обновление базы знаний прямо из .cf/.cfe/.epf/.erf.

Без конвертации в выгрузку конфигуратора: cf распаковывается во временное
дерево (стадии 0/1/3, `extract.decode_tree`), отпечатки гранул (объект
верхнего уровня со всем деревом, таблица `cf_granule`) сверяются с состоянием
в базе, и переустанавливаются ТОЛЬКО изменившиеся гранулы: DELETE-каскад по
meta_object + вставка общими помощниками writer (`build_infos`,
`collect_object_rows`, `_role_right_rows`) + глобальная перелинковка
(attribute_ref, права ролей, субконто, составы подсистем/общих реквизитов).
FTS-индекс пересобирается поколениями шардов; живой MCP-сервер подхватывает
изменения сам (WAL + hot reload по PRAGMA data_version) — остановка не нужна.

Состояние гранул пишут полные сборки ревизии 15+ (`write_db`). Базы прежних
ревизий, базы из выгрузки и отсутствующие базы falls back на полную сборку
(как `dump.update_db`). Полная пересборка при живом сервере невозможна
(файл базы занят) — внятная ошибка с подсказкой.

Гранула cf = объект верхнего уровня ('Catalog/Имя') со всем деревом
(подобъекты/формы/команды/макеты/модули); '' — корень конфигурации.
Отпечаток гранулы — sha256 по (rel-путь + sha256 содержимого) всех её файлов:
меняется любой файл гранулы — гранула переустанавливается целиком.
"""
import json
import os
import re
import sqlite3
from datetime import datetime

from . import rights, xdto
from .db import writer as W
from .dump.update import (_delete_granule, _relink_attribute_refs,
                          _relink_role_rights, _relink_subconto,
                          read_server_pidfile)

CF_EXTS = ('.cf', '.cfe', '.epf', '.erf')


def _cf_db_state(db_path):
    """(source_info, годна_ли_база_для_cf-инкремента).

    Годна: собрана из файла 1С, есть таблица cf_granule и ревизия схемы
    совпадает с текущей (иначе состояние гранул неполно/несовместимо).
    """
    from .extract import source_info
    info = source_info(db_path)
    if not info:
        return None, False
    ext = os.path.splitext(info.get('file') or '')[1].lower()
    if ext not in CF_EXTS:
        return info, False
    uri = 'file:' + os.path.abspath(db_path).replace(os.sep, '/') + '?mode=ro'
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return info, False
    try:
        rev = conn.execute('PRAGMA user_version').fetchone()[0]
        has = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                           "AND name='cf_granule'").fetchone()[0]
        return info, bool(has) and rev == W.SCHEMA_VERSION
    except sqlite3.Error:
        return info, False
    finally:
        conn.close()


class _UuidIndex:
    """uuid -> id объекта: первичные uuid + алиасы заголовков.

    Алиасы ссылочных uuid новых платформ не хранятся отдельной колонкой.
    Карта алиасов строится ОДИН раз лениво (при первом промахе) полным
    проходом header_json: точечный LIKE по 322 МБ заголовков на каждый
    неразрешённый uuid стоил часы на реальной конфигурации (замер БП).
    """

    def __init__(self, conn, uuid_to_id):
        self.conn = conn
        self.map = uuid_to_id
        self._aliases = None

    def get(self, uuid):
        target = self.map.get(uuid)
        if target is not None:
            return target
        if self._aliases is None:
            self._aliases = {}
            for oid, header_json in self.conn.execute(
                    'SELECT id, header_json FROM meta_object'):
                try:
                    header = json.loads(header_json)
                except ValueError:
                    continue
                for alias in W._header_alias_uuids(header):
                    self._aliases.setdefault(alias, oid)
        return self._aliases.get(uuid)


def _relink_refs_by_uuid(conn, index):
    """Ссылки без target_path (цель не была известна при вставке) — по uuid."""
    rows = conn.execute(
        'SELECT id, uuid FROM attribute_ref'
        ' WHERE object_id IS NULL AND target_path IS NULL').fetchall()
    for rid, uuid in rows:
        target = index.get(uuid)
        if target is not None:
            path = conn.execute('SELECT path FROM meta_object WHERE id=?',
                                (target,)).fetchone()[0]
            conn.execute('UPDATE attribute_ref SET object_id=?, target_path=?'
                         ' WHERE id=?', (target, path, rid))


def _rebuild_content(conn, index):
    """Полный пересчёт subsystem_content/common_target по заголовкам базы."""
    conn.execute('DELETE FROM subsystem_content')
    conn.execute('DELETE FROM common_target')
    sub_rows, com_rows = [], []
    rows = conn.execute(
        'SELECT id, type, header_json, uuid, parent_id FROM meta_object'
        " WHERE type IN ('Subsystem', 'CommonAttribute')").fetchall()
    parents = dict(conn.execute('SELECT id, uuid FROM meta_object'))
    for oid, typ, header_json, uuid, parent_id in rows:
        try:
            header = json.loads(header_json)
        except ValueError:
            continue
        if typ == 'Subsystem':
            flat = []
            W._flatten_uuids(header, flat)
            parent_uuid = parents.get(parent_id)
            seen = set()
            ord_no = 0
            for value in flat:
                target = index.get(value)
                if target is None or value == uuid or value == parent_uuid:
                    continue
                if value in seen:
                    continue
                seen.add(value)
                sub_rows.append((oid, target, ord_no))
                ord_no += 1
        else:
            for target in W._extract_common_targets(header, index.map, oid):
                com_rows.append((oid, target))
    conn.executemany(W.INSERT_SQL['subsystem_content'], sub_rows)
    conn.executemany(W.INSERT_SQL['common_target'], com_rows)


def _module_re(stem):
    return re.compile(rf'^{re.escape(stem)}\.(?P<code>.+)\.(?P<ext>bsl|text|image|bin)$')


def _write_granule_files(conn, tree, rel_by_dir, objects, infos, path_id,
                         granule, source_id, workers, store_blobs, stats):
    """Модули/методы/файлы/СКД/XDTO гранулы; возвращает признак правки .bsl."""
    dirs = [(dirpath, files) for dirpath, files in tree
            if W.granule_of(rel_by_dir[dirpath]) == granule]
    bsl_tasks = []
    for dirpath, files in dirs:
        rel = rel_by_dir[dirpath]
        info = infos.get(rel)
        stem = objects.get(dirpath)
        if not info or rel not in path_id:
            continue
        for fn, _size in files:
            m = _module_re(stem).match(fn)
            if m and m.group('ext') == 'bsl':
                context = None
                if stem == 'CommonModule':
                    context = W._common_module_context(info['header'])
                bsl_tasks.append((os.path.join(dirpath, fn), path_id[rel],
                                  m.group('code'), context))
    if workers > 1 and len(bsl_tasks) > 1:
        import multiprocessing
        pool_size = min(workers, W.MAX_PARSE_WORKERS, len(bsl_tasks))
        mp_ctx = multiprocessing.get_context('spawn')
        with mp_ctx.Pool(pool_size) as pool:
            parsed = pool.map(W._parse_bsl_worker, [t[0] for t in bsl_tasks],
                              chunksize=max(1, len(bsl_tasks) // (pool_size * 8)))
    else:
        parsed = [W._parse_bsl_worker(t[0]) for t in bsl_tasks]
    bsl_changed = False
    for (full, obj_id, code_name, context), (body, methods) in zip(bsl_tasks, parsed):
        cur = conn.execute(W.INSERT_SQL['module'],
                           (obj_id, code_name, context, body))
        module_id = cur.lastrowid
        conn.executemany(W.INSERT_SQL['method'], [
            (module_id, o, m['kind'], m['name'], m['signature'],
             int(m['is_export']), ', '.join(m['directives']), m['description'],
             m['line_start'], m['line_end'], m['body'])
            for o, m in enumerate(methods)])
        stats['modules'] += 1
        stats['methods'] += len(methods)
        bsl_changed = True
    for dirpath, files in dirs:
        rel = rel_by_dir[dirpath]
        obj_id = path_id.get(rel)
        stem = objects.get(dirpath)
        prefix = rel + '/' if rel else ''
        for fn, size in files:
            full = os.path.join(dirpath, fn)
            if obj_id and fn in (f'{stem}.json', f'{stem}.id.json'):
                continue
            m = _module_re(stem).match(fn) if stem else None
            if m and m.group('ext') == 'bsl':
                # тело модуля живёт в module; file — карта «объект → файл»
                conn.execute(W.INSERT_SQL['file'],
                             (source_id, obj_id, prefix + fn, 'bsl', size, None))
                stats['files'] += 1
                continue
            ext = fn.rsplit('.', 1)[-1].lower() if '.' in fn else ''
            data = None
            if ext in W.TEXT_KINDS:
                data = W._read_text(full).encode('utf-8')
            elif store_blobs:
                with open(W.helper.long_path(full), 'rb') as f:
                    data = f.read()
            if obj_id and ext == 'bin':
                skd = W._extract_skd_queries(full)
                if skd:
                    conn.executemany(W.INSERT_SQL['skd_query'],
                                     [(obj_id, o, q) for o, q in enumerate(skd)])
                    stats['skd'] = stats.get('skd', 0) + len(skd)
                package = xdto.parse_package(full)
                if package:
                    W._insert_xdto(conn, obj_id, package, stats)
            conn.execute(W.INSERT_SQL['file'],
                         (source_id, obj_id, prefix + fn, ext or 'bin', size, data))
            stats['files'] += 1
    return bsl_changed


def update_cf_tree(tree_dir, db_path, *, source_file, source_sha256,
                   source_size=None, workers=1, rebuild_fts=None,
                   store_blobs=False, log=print):
    """Инкрементальное обновление базы по декодированному дереву cf (stage 3).

    Сверяет отпечатки гранул с cf_granule, переустанавливает изменившиеся,
    перелинковывает связи глобально. Возвращает статистику (changed_granules,
    objects/modules/methods/files, elapsed). Дерево должно быть полным
    (заголовки в JSON-файлах, без потока headers).
    """
    tree_dir = os.path.abspath(tree_dir)
    db_path = os.path.abspath(db_path)
    begin = datetime.now()
    tree = W._scan_tree(tree_dir)
    rel_by_dir = W._rel_paths(tree, tree_dir)
    objects = W._find_objects(tree, tree_dir)
    if tree_dir not in objects:
        raise ValueError(f'В {tree_dir} не найден корневой объект (Configuration.json и т.п.)')
    infos = W.build_infos(tree_dir, tree, rel_by_dir, objects)
    fingerprints = W.granule_fingerprints(tree_dir, tree, rel_by_dir)

    conn = sqlite3.connect(db_path, timeout=60)
    stats = {'changed_granules': [], 'objects': 0, 'modules': 0, 'methods': 0,
             'files': 0, 'skd': 0, 'role_rights': 0, 'role_rls_templates': 0,
             'role_rights_failed': 0, 'skipped': False}
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=60000')
        conn.execute('PRAGMA synchronous=NORMAL')
        # WAL: читатели живого MCP-сервера не блокируют писателя и наоборот
        conn.execute('PRAGMA journal_mode=WAL')
        stored = dict(conn.execute('SELECT path, sha256 FROM cf_granule'))
        dirty = {g for g, s in fingerprints.items() if stored.get(g) != s}
        dirty |= {g for g in stored if g not in fingerprints}
        source_id = conn.execute(
            'SELECT id FROM source ORDER BY id LIMIT 1').fetchone()[0]
        if not dirty:
            conn.execute('UPDATE source SET file_sha256=?, file_size=? WHERE id=?',
                         (source_sha256, source_size, source_id))
            conn.commit()
            log('Изменений нет: декодированное дерево cf соответствует гранулам базы.')
            stats['skipped'] = True
            stats['db'] = db_path
            stats['elapsed'] = str(datetime.now() - begin)
            return stats
        log(f'Изменённых гранул: {len(dirty)} (из {len(fingerprints)}): '
            f'{", ".join(sorted(dirty)[:8])}{"…" if len(dirty) > 8 else ""}')
        conn.execute('BEGIN IMMEDIATE')
        path_id = dict(conn.execute('SELECT path, id FROM meta_object'))
        uuid_to_path = {u: p for p, u in conn.execute(
            'SELECT path, uuid FROM meta_object WHERE uuid IS NOT NULL')}
        uuid_to_id = {u: path_id[p] for u, p in uuid_to_path.items() if p in path_id}
        name2paths = {}
        for p, n in conn.execute('SELECT path, name FROM meta_object'):
            name2paths.setdefault(n, []).append(p)
        root_stem = objects.get(tree_dir, 'Configuration')
        ref2name = W._read_refmap(os.path.join(tree_dir, f'{root_stem}.10.json'))
        dt_map, dt_members = W._defined_type_map(infos)
        resolver = W._TypeResolver(uuid_to_path, name2paths, ref2name,
                                   dt_map, dt_members)
        by_granule = {}
        for rel, info in infos.items():
            by_granule.setdefault(W.granule_of(rel), []).append((rel, info))

        affected_roles = set()
        reinserted_roles = {}
        bsl_changed = False
        for granule in sorted(dirty):
            objs = by_granule.get(granule)
            if not objs:
                # гранула исчезла из cf — объекты удалены (FK-каскад)
                _delete_granule(conn, granule, affected_roles, source_id)
                conn.execute('DELETE FROM cf_granule WHERE path=?', (granule,))
                continue
            _delete_granule(conn, granule, affected_roles, source_id)
            for rel, info in sorted(objs, key=lambda ri: ri[0].count('/')):
                cur = conn.execute(W.INSERT_SQL['meta_object'], (
                    source_id, path_id.get(info['parent_rel']), info['ord'],
                    rel, info['stem'], W.TYPE_RU.get(info['stem'], info['stem']),
                    info['name'], info['uuid'], info['header'].get('comment'),
                    info['header'].get('obj_version'),
                    info['header_text'] or json.dumps(info['header'],
                                                      ensure_ascii=False)))
                obj_id = cur.lastrowid
                path_id[rel] = obj_id
                if info['uuid']:
                    uuid_to_path[info['uuid']] = rel
                    uuid_to_id[info['uuid']] = obj_id
                for alias in W._header_alias_uuids(info['header']):
                    uuid_to_path.setdefault(alias, rel)
                    uuid_to_id.setdefault(alias, obj_id)
                # реквизиты + ссылки: как в полной записи, но id атрибутов
                # берём из lastrowid (вставка по строке, гранула мала)
                bags = []
                attrs = W._extract_attributes(info['header'], resolver, bags)
                info['tabular'] = W._tabular_rows(bags)
                for ord_no, (name, type_str, links, tabular, uuid) in enumerate(attrs):
                    cur = conn.execute(W.INSERT_SQL['meta_attribute'],
                                       (obj_id, ord_no, name, type_str,
                                        tabular, uuid))
                    attr_id = cur.lastrowid
                    for l_ord, (l_uuid, l_path) in enumerate(links):
                        conn.execute(W.INSERT_SQL['attribute_ref'], (
                            attr_id, l_ord, l_uuid,
                            uuid_to_id.get(l_path) if l_path else None, l_path))
                rows = W.collect_object_rows(info, obj_id, resolver,
                                             uuid_to_id, uuid_to_path)
                for table in ('meta_tabular', 'meta_subobject', 'enum_value',
                              'common_target', 'predefined', 'form_attribute',
                              'form_command', 'form_event', 'form_parameter',
                              'object_property', 'http_method',
                              'webservice_operation'):
                    if rows[table]:
                        conn.executemany(W.INSERT_SQL[table], rows[table])
                for (o, n, s, u, f) in rows['subconto_raw']:
                    # kind_id разрешится глобальным relink после вставки
                    conn.execute(W.INSERT_SQL['predefined_subconto'],
                                 (o, n, s, u, None, f))
                stats['objects'] += 1
                if info['stem'] == 'Role':
                    reinserted_roles[obj_id] = info['dirpath']
            bsl_changed |= _write_granule_files(
                conn, tree, rel_by_dir, objects, infos, path_id, granule,
                source_id, workers, store_blobs, stats)
            conn.execute(W.INSERT_SQL['cf_granule'],
                         (granule, fingerprints[granule]))

        # глобальная перелинковка ссылок: по target_path и по uuid (алиасы)
        _relink_attribute_refs(conn)
        index = _UuidIndex(conn, uuid_to_id)
        _relink_refs_by_uuid(conn, index)
        # права ролей — после всех объектов (как в полной записи): цели
        # переустановленных гранул уже на месте; затронутые чужие роли
        # перечитываются (их Rights.xml не менялся, но цели переехали)
        rights_resolver = rights.TargetResolver(conn)
        role_dirs = dict(reinserted_roles)
        for rid in sorted(affected_roles - set(reinserted_roles)):
            row = conn.execute('SELECT path FROM meta_object WHERE id=?',
                               (rid,)).fetchone()
            if row:
                role_dirs[rid] = os.path.join(tree_dir, *row[0].split('/'))
        for rid, role_dir in sorted(role_dirs.items()):
            conn.execute('DELETE FROM role_right WHERE role_id=?', (rid,))
            conn.execute('DELETE FROM role_rls_template WHERE role_id=?', (rid,))
            conn.execute('DELETE FROM role_rights_state WHERE role_id=?', (rid,))
            rp, tp, sp = W._role_right_rows(conn, rid, role_dir,
                                            rights_resolver, stats)
            conn.executemany(W._ROLE_RIGHT_SQL, rp)
            W.insert_rows(conn, 'role_rls_template', tp)
            conn.executemany(W._ROLE_STATE_SQL, sp)
        null_uuids = [u for (u,) in conn.execute(
            'SELECT DISTINCT target_uuid FROM role_right'
            ' WHERE target_object_id IS NULL AND target_attr_id IS NULL'
            ' AND target_tabular_id IS NULL AND target_subobject_id IS NULL')]
        _relink_role_rights(conn, null_uuids or None)
        _relink_subconto(conn)
        _rebuild_content(conn, index)
        # корень: имя/uuid конфигурации и ord верхнего уровня из нового заголовка
        if '' in dirty:
            root_info = infos.get('')
            if root_info:
                conn.execute(
                    'UPDATE source SET root_type=?, root_name=?, root_uuid=?'
                    ' WHERE id=?',
                    (root_info['stem'], root_info['name'], root_info['uuid'],
                     source_id))
                conn.executemany(
                    'UPDATE meta_object SET ord=? WHERE source_id=? AND path=?',
                    [(i['ord'], source_id, r) for r, i in infos.items()
                     if r and i['parent_rel'] == ''])
        conn.execute('UPDATE source SET file_sha256=?, file_size=? WHERE id=?',
                     (source_sha256, source_size, source_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if rebuild_fts is None:
        rebuild_fts = bsl_changed
    if rebuild_fts:
        W.build_fts_index(db_path, workers=workers)
    stats['changed_granules'] = sorted(dirty)
    stats['db'] = db_path
    stats['elapsed'] = str(datetime.now() - begin)
    return stats


def update_cf(src_file, db_path, *, workers=1, rebuild_fts=None,
              store_blobs=False, force=False, log=print, source_sha256=None):
    """Инкрементальное обновление базы прямо из файла 1С (.cf/.cfe/.epf/.erf).

    Отпечаток cf совпадает с source.file_sha256 — распаковка не нужна вовсе.
    Иначе распаковка во временное дерево + granule-diff (update_cf_tree).
    База отсутствует/не из cf/прежней ревизии — полная сборка (`extract`),
    при живом MCP-сервере полная пересборка невозможна (файл занят).
    """
    from .extract import (decode_tree, extract as _extract, file_sha256,
                          make_temp_dir, remove_tree)
    src_file = os.path.abspath(src_file)
    db_path = os.path.abspath(db_path)
    begin = datetime.now()

    def _full(reason):
        server = read_server_pidfile(db_path)
        if server:
            raise ValueError(
                f'полная пересборка невозможна: базу держит MCP-сервер '
                f'(pid {server.get("pid")}). Остановите сервер; частичное '
                'обновление работает и при запущенном сервере.')
        log(reason)
        return _extract(src_file, db_path=db_path, workers=workers,
                        build_fts=True if rebuild_fts is None else rebuild_fts,
                        options={'store_blobs': store_blobs},
                        source_sha256=source_sha256)

    if not os.path.isfile(db_path):
        return _full('Базы нет — полная сборка из cf.')
    info, ok = _cf_db_state(db_path)
    if not ok:
        return _full('База собрана не из cf или прежней ревизией схемы — '
                     'полная пересборка.')
    sha = source_sha256 or file_sha256(src_file)
    if sha == info.get('file_sha256') and not force:
        log('Изменений нет: отпечаток cf совпадает с базой, обновление не нужно.')
        return {'changed_granules': [], 'skipped': True, 'db': db_path,
                'src': src_file, 'elapsed': str(datetime.now() - begin)}
    temp = make_temp_dir(db_path)
    try:
        stage3 = decode_tree(src_file, temp, options={}, workers=workers)
        stats = update_cf_tree(
            stage3, db_path, source_file=src_file, source_sha256=sha,
            source_size=os.path.getsize(src_file), workers=workers,
            rebuild_fts=rebuild_fts, store_blobs=store_blobs, log=log)
    finally:
        remove_tree(temp, workers=max(workers, 4))
    stats['src'] = src_file
    stats['elapsed'] = str(datetime.now() - begin)
    return stats