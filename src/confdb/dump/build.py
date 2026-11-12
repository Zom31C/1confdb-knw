"""Полная сборка базы знаний из выгрузки конфигурации в файлы.

Схема и инструменты — от сборки из .cf (`db.writer`): SCHEMA, TYPE_RU,
TEXT_KINDS, пул разбора BSL-модулей, разбор пакетов XDTO, regex запросов
СКД, приставной FTS-индекс. Отличия баз из выгрузки (документированы в
docs/details.md):

- `meta_object.header_json` хранит текст XML объекта (для команд — фрагмент
  `<Command …>` из XML родителя: отдельного файла у команд нет);
- `attribute_ref` пишется с целью по пути (`target_path`), uuid/object_id
  заполняются одним UPDATE после вставки всех объектов — ссылки разрешаются
  независимо от порядка объектов;
- права ролей — из текстового `Ext\\Rights.xml` (`dump.rights`);
- состав файлов выгрузки с SHA-256 пишется в `dump_file` — основа
  инкрементального обновления (`dump.update`).

Порядок фаз (важен для update — он переиспользует фазы для одной гранулы):
1. объекты: meta_object (родители раньше детей; команды вставляет разбор
   родителя) + атрибуты/ТЧ/перечисления/предопределённые/подобъекты + СКД +
   XDTO + задачи на разбор модулей;
2. модули (пул процессов) + строки file + dump_file (хэши);
3. межобъектные связи: UPDATE attribute_ref по target_path, common_target,
   subsystem_content, predefined_subconto, права ролей;
4. source (root_*), dump_state, WAL, FTS.
"""
import hashlib
import os
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime

from .. import xdto
from ..db import writer as W
from ..v8 import helper
from . import model, xmlmeta

HASH_CHUNK = 1 << 20

# Пачка файлов для пула потоков в write_files: ограничивает число одновременно
# удерживаемых в памяти прочитанных содержимых (хэшируемые потоково файлы
# в память не берутся вовсе).
_READ_BATCH = 64


def _decode_text(blob):
    """Байты файла → UTF-8-байты текста (содержимое file.data).

    Повторяет W._read_text байт-в-байт: те же кодировки (utf-8-sig, затем
    cp1251, затем utf-8 с заменами) и то же преобразование переносов строк,
    которое давал текстовый режим чтения (universal newlines: CRLF/CR → LF).
    """
    for enc in ('utf-8-sig', 'cp1251'):
        try:
            text = blob.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = blob.decode('utf-8', errors='replace')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    return text.encode('utf-8')

# Соответствие имени тега ChildObjects конфигурации и типа объекта: теги
# обычно совпадают с типом, но классы-«множественные» (DocumentNumerators,
# Sequences) и возможные расхождения singular/plural разрешаются поиском.
def _top_path_by_tag(tag, name, known_paths):
    for cand in (f'{tag}/{name}', f'{tag}s/{name}', f'{tag.rstrip("s")}/{name}'):
        if cand in known_paths:
            return cand
    return None


def file_sha256(path, chunk=HASH_CHUNK):
    """SHA-256 файла (кусками по 1 МиБ)."""
    digest = hashlib.sha256()
    with open(helper.long_path(path), 'rb') as fh:
        for block in iter(lambda: fh.read(chunk), b''):
            digest.update(block)
    return digest.hexdigest()


class Builder:
    """Состояние и фазы сборки; update_db переиспользует её для гранул."""

    def __init__(self, dump_dir, conn, source_id, *, workers=1,
                 store_blobs=False, known_paths=frozenset(), warnings=None):
        self.dump_dir = dump_dir
        self.conn = conn
        self.source_id = source_id
        self.workers = workers
        self.store_blobs = store_blobs
        self.warnings = warnings if warnings is not None else []
        self.types = xmlmeta.Types(dump_dir, known_paths)
        self.known_paths = known_paths
        self.path_id = {}          # путь БД -> meta_object.id
        self.top_ord = {}          # путь объекта верхнего уровня -> ord
        self.child_orders = {}     # путь родителя -> {'Form': [...], ...}
        self.bsl_tasks = []        # (bsl_path, owner_path, code_name, context)
        self.common_targets = []   # (common_path, [«Класс.Имя», ...])
        self.subsystem_content = []  # (subsystem_path, [«Класс.Имя», ...])
        self.subconto_refs = []    # (object_path, item_ord, sub_ord, ref, flags)
        self.roles = []            # (role_id, role_path)
        # для адресной перелинковки при инкрементальном обновлении: что было
        # вставлено в этой сессии (uuid целей прав/ссылок и пути объектов)
        self.changed_uuids = set()
        self.changed_paths = set()
        self.stats = {
            'objects': 0, 'modules': 0, 'methods': 0, 'files': 0,
            'files_content': 0, 'skd': 0, 'attributes': 0, 'refs': 0,
            'enum_values': 0, 'predefined': 0, 'subconto': 0,
            'common_targets': 0, 'tabular': 0, 'xdto_types': 0,
            'xdto_properties': 0, 'role_rights': 0, 'role_rls_templates': 0,
            'role_rights_failed': 0, 'dump_files': 0, 'subsystem_content': 0,
        }

    # -- фаза 1: объекты -----------------------------------------------------

    def process_object(self, obj):
        """Разбирает XML объекта и вставляет его строки (meta_object и дети).

        Возвращает id вставленного объекта или None (команда — её строку
        вставил родитель; объект без XML).
        """
        kind = obj['kind']
        if kind == 'command':
            # строку meta_object вставил разбор родителя; здесь — только модуль
            self._collect_modules(obj, None)
            return self.path_id.get(obj['path'])
        if not obj['xml'] or not os.path.isfile(obj['xml']):
            if kind != 'root':
                self.warnings.append(f'нет XML объекта: {obj["path"]}')
            return None
        try:
            # файл читается ОДИН раз: байты идут и на разбор, и на header_json
            with open(obj['xml'], 'rb') as f:
                data = f.read()
            root = xmlmeta.class_element(xmlmeta.loadb(data))
        except (ET.ParseError, OSError) as err:
            self.warnings.append(f'XML не читается: {obj["path"]}: {err}')
            return None
        # прежний текстовый режим давал universal newlines (CRLF выгрузки →
        # LF в header_json) — повторям то же преобразование, байт-в-байт
        header_text = data.decode('utf-8', errors='replace')
        header_text = header_text.replace('\r\n', '\n').replace('\r', '\n')
        if kind == 'root':
            return self._process_root(obj, root, header_text)
        return self._process_meta_object(obj, root, header_text)

    def _process_root(self, obj, root, header_text):
        uuid, name, comment = xmlmeta.object_basics(root)
        # порядок объектов верхнего уровня — из <ChildObjects> конфигурации
        children = xmlmeta.config_children(root)
        known = self.known_paths
        for i, (tag, child_name) in enumerate(children):
            path = _top_path_by_tag(tag, child_name, known)
            if path is not None and path not in self.top_ord:
                self.top_ord[path] = i
        compat = xmlmeta.config_props_dict(root).get('CompatibilityMode') or ''
        digits = _compat_digits(compat)
        cur = self.conn.execute(
            W.INSERT_SQL['meta_object'],
            (self.source_id, None, None, '', obj['type'],
             W.TYPE_RU.get(obj['type'], obj['type']),
             name or '', uuid, comment, digits or None, header_text))
        self.path_id[''] = cur.lastrowid
        self.stats['objects'] += 1
        self.conn.execute(
            'UPDATE source SET root_type=?, root_name=?, root_uuid=? WHERE id=?',
            (obj['type'], name, uuid, self.source_id))
        # корневые модули: Ext\*.bsl
        self._collect_root_modules(obj)
        return cur.lastrowid

    def _collect_root_modules(self, obj):
        folder = obj['folder']
        if not folder:
            return
        for fn in sorted(os.listdir(folder)):
            if not fn.endswith('.bsl'):
                continue
            code = model.MODULE_CODE.get(fn)
            if code is None:
                self.warnings.append(f'неизвестный корневой модуль: Ext/{fn}')
                continue
            self.bsl_tasks.append((os.path.join(folder, fn), '', code, None))

    def _process_meta_object(self, obj, root, header_text):
        conn = self.conn
        otype = obj['type']
        uuid, name, comment = xmlmeta.object_basics(root)
        name = name or obj['name']
        path = obj['path']
        parent_id = self.path_id.get(obj['parent_path'])
        ord_no = self._ord_for(obj)
        header_text = self._header_text(obj, header_text)
        cur = conn.execute(
            W.INSERT_SQL['meta_object'],
            (self.source_id, parent_id, ord_no, path, otype,
             W.TYPE_RU.get(otype, otype), name, uuid, comment,
             _object_version(conn), header_text))
        obj_id = cur.lastrowid
        self.path_id[path] = obj_id
        self.stats['objects'] += 1
        self.changed_paths.add(path)
        if uuid:
            self.changed_uuids.add(uuid.lower())

        attrs, tabulars, commands, enum_values, subobjects = \
            xmlmeta.fields_and_sections(root, self.types)
        self.child_orders[path] = xmlmeta.child_name_lists(root)

        # команды объекта: метаданные встроены в XML родителя — вставляем здесь
        for c_ord, (c_name, c_uuid) in enumerate(commands):
            c_path = f'{path}/{model.command_type(otype)}/{c_name}'
            c_el = _command_element(root, c_name)
            c_header = ET.tostring(c_el, encoding='unicode') \
                if c_el is not None else ''
            ccur = conn.execute(
                W.INSERT_SQL['meta_object'],
                (self.source_id, obj_id, c_ord, c_path,
                 model.command_type(otype),
                 W.TYPE_RU.get(model.command_type(otype), 'Команда'),
                 c_name, c_uuid, None, _object_version(conn), c_header))
            self.path_id[c_path] = ccur.lastrowid
            self.stats['objects'] += 1
            self.changed_paths.add(c_path)
            if c_uuid:
                self.changed_uuids.add(c_uuid.lower())

        # реквизиты и поля табличных частей + ссылки
        if attrs:
            conn.executemany(
                W.INSERT_SQL['meta_attribute'],
                [(obj_id, i, a[0], a[1], a[3], a[4])
                 for i, a in enumerate(attrs)])
            first = conn.execute('SELECT last_insert_rowid()').fetchone()[0] \
                - len(attrs) + 1
            ref_params = []
            for i, a in enumerate(attrs):
                for l_ord, target_path in enumerate(a[2]):
                    if target_path:
                        ref_params.append((first + i, l_ord, '', None,
                                           target_path))
            if ref_params:
                conn.executemany(W.INSERT_SQL['attribute_ref'], ref_params)
            self.stats['attributes'] += len(attrs)
            self.stats['refs'] += len(ref_params)
        if tabulars:
            conn.executemany(
                W.INSERT_SQL['meta_tabular'],
                [(obj_id, i, t[0], t[1]) for i, t in enumerate(tabulars)])
            self.stats['tabular'] += len(tabulars)
            for _n, t_uuid in tabulars:
                if t_uuid:
                    self.changed_uuids.add(t_uuid.lower())
        if enum_values:
            conn.executemany(
                W.INSERT_SQL['enum_value'],
                [(obj_id, i, n, s) for i, (n, s) in enumerate(enum_values)])
            self.stats['enum_values'] += len(enum_values)
        if subobjects:
            conn.executemany(
                W.INSERT_SQL['meta_subobject'],
                [(obj_id, s[0], i, s[1], s[2])
                 for i, s in enumerate(subobjects)])
            for s in subobjects:
                if s[1]:
                    self.changed_uuids.add(s[1].lower())
        if attrs:
            for a in attrs:
                if a[4]:
                    self.changed_uuids.add(a[4].lower())

        # контекст и модули
        context = xmlmeta.common_module_context(root) \
            if otype == 'CommonModule' else None
        self._collect_modules(obj, context)

        # содержимое, зависящее от файлов папки
        if obj['folder']:
            self._collect_folder_content(obj, root)

        # составы: подсистема / общий реквизит
        if otype == 'Subsystem':
            content = xmlmeta.subsystem_content(root)
            if content:
                self.subsystem_content.append((path, content))
        elif otype == 'CommonAttribute':
            content = xmlmeta.common_attribute_content(root)
            if content:
                self.common_targets.append((path, content))
        elif otype == 'Role':
            self.roles.append((obj_id, path))
        return obj_id

    def _ord_for(self, obj):
        """ord объекта: порядок в ChildObjects родителя."""
        parent = obj['parent_path']
        if parent == '':
            return self.top_ord.get(obj['path'])
        orders = self.child_orders.get(parent)
        if not orders:
            return None
        key = {'form': 'Form', 'template': 'Template'}.get(obj['kind'])
        if obj['type'] == 'Subsystem' and obj['kind'] == 'object':
            key = 'Subsystem'
        if key:
            names = orders.get(key)
            if names and obj['name'] in names:
                return names.index(obj['name'])
        return None

    @staticmethod
    def _header_text(obj, text):
        """Текст для header_json: файл XML объекта, прочитанный в
        process_object (команды получают фрагмент из родителя — у них нет
        своего файла).

        Состав плана обмена живёт в отдельном Ext/Content.xml — дописываем его
        за маркером confdb:include, чтобы header_props.exchange_plan_content
        читал состав без обращения к таблице file (как в cf-базах).
        """
        if obj['type'] == 'ExchangePlan' and obj['folder']:
            content = os.path.join(obj['folder'], 'Ext', 'Content.xml')
            if os.path.isfile(content):
                try:
                    with open(content, 'r', encoding='utf-8',
                              errors='replace') as f:
                        text += ('\n<!--confdb:include:Ext/Content.xml-->\n'
                                 + f.read())
                except OSError:
                    pass
        return text

    def _collect_modules(self, obj, context):
        """Задачи на разбор BSL объекта/формы/общей формы."""
        folder = obj['folder']
        if not folder:
            return
        path = obj['path']
        ext = os.path.join(folder, 'Ext')
        if obj['kind'] == 'form' or obj['type'] == 'CommonForm':
            bsl = os.path.join(ext, 'Form', 'Module.bsl')
            if os.path.isfile(bsl):
                self.bsl_tasks.append((bsl, path, 'obj', None))
        elif obj['kind'] == 'command':
            bsl = os.path.join(ext, 'CommandModule.bsl')
            if os.path.isfile(bsl):
                self.bsl_tasks.append((bsl, path, 'obj', None))
        elif os.path.isdir(ext):
            for fn in sorted(os.listdir(ext)):
                if not fn.endswith('.bsl'):
                    continue
                code = model.MODULE_CODE.get(fn)
                if code is None:
                    self.warnings.append(
                        f'неизвестный файл модуля: {path}/Ext/{fn}')
                    continue
                self.bsl_tasks.append((os.path.join(ext, fn), path, code,
                                       context))

    def _collect_folder_content(self, obj, root):
        """СКД-запросы, XDTO и предопределённые данные из файлов папки."""
        folder = obj['folder']
        path = obj['path']
        obj_id = self.path_id.get(path)
        if obj_id is None:
            return
        conn = self.conn
        ext = os.path.join(folder, 'Ext')
        # предопределённые данные
        predef_xml = os.path.join(ext, 'Predefined.xml')
        if os.path.isfile(predef_xml):
            root_name = xmlmeta.PREDEF_ROOT_NAMES.get(obj['type'], 'Элементы')
            elements, subcontos = xmlmeta.predefined_items(predef_xml, root_name)
            if elements:
                conn.executemany(
                    W.INSERT_SQL['predefined'],
                    [(obj_id,) + e for e in elements])
                self.stats['predefined'] += len(elements)
            for item_ord, sub_ord, ref, flags in subcontos:
                self.subconto_refs.append((path, item_ord, sub_ord, ref, flags))
        # СКД: текстовый Template.xml у макета или XML внутри Template.bin
        if obj['kind'] == 'template' or obj['type'] == 'CommonTemplate':
            tmpl_xml = os.path.join(ext, 'Template.xml')
            tmpl_bin = os.path.join(ext, 'Template.bin')
            queries = xmlmeta.skd_queries_from_xml(tmpl_xml) \
                if os.path.isfile(tmpl_xml) else []
            if not queries and os.path.isfile(tmpl_bin):
                queries = W._extract_skd_queries(tmpl_bin) or []
            if queries:
                conn.executemany(
                    W.INSERT_SQL['skd_query'],
                    [(obj_id, i, q) for i, q in enumerate(queries)])
                self.stats['skd'] += len(queries)
        # XDTO: Package.bin
        if obj['type'] == 'XDTOPackage':
            package_bin = os.path.join(ext, 'Package.bin')
            if os.path.isfile(package_bin):
                package = xdto.parse_package(package_bin)
                if package:
                    W._insert_xdto(conn, obj_id, package, self.stats)

    # -- фаза 2: модули, файлы, хэши ----------------------------------------

    def write_modules(self):
        """Разбор BSL в пуле процессов и вставка module/method."""
        if not self.bsl_tasks:
            return
        tasks = self.bsl_tasks
        paths = [t[0] for t in tasks]
        if self.workers > 1 and len(paths) > 1:
            import multiprocessing
            pool_size = min(self.workers, W.MAX_PARSE_WORKERS, len(paths))
            mp_ctx = multiprocessing.get_context('spawn')
            with mp_ctx.Pool(pool_size) as pool:
                parsed = pool.map(
                    W._parse_bsl_worker, paths,
                    chunksize=max(1, len(paths) // (pool_size * 8)))
        else:
            parsed = [W._parse_bsl_worker(p) for p in paths]
        conn = self.conn
        for (bsl_path, owner_path, code_name, context), (body, methods) in zip(tasks, parsed):
            object_id = self.path_id.get(owner_path)
            if object_id is None:
                self.warnings.append(f'модуль без объекта: {bsl_path}')
                continue
            cur = conn.execute(
                W.INSERT_SQL['module'], (object_id, code_name, context, body))
            module_id = cur.lastrowid
            if methods:
                conn.executemany(
                    W.INSERT_SQL['method'],
                    [(module_id, i, m['kind'], m['name'], m['signature'],
                      int(m['is_export']), ', '.join(m['directives']),
                      m['description'], m['line_start'], m['line_end'],
                      m['body']) for i, m in enumerate(methods)])
            self.stats['modules'] += 1
            self.stats['methods'] += len(methods)

    def write_files(self, granule_files, objects, current=None,
                    write_dump_state=True):
        """Строки file (карта «объект → файл выгрузки») и dump_file (хэши).

        Владелец файла — подобъект с самым длинным совпадением папки, иначе
        объект верхнего уровня; файлы корня принадлежат корневому объекту.

        Stat, SHA-256 и чтение содержимого каждого файла — за ОДИН проход
        (раньше хэш и текст читали файл дважды), а проходы распараллелены
        потоками (ожидание диска и hashlib отпускают GIL); строки вставляются
        как и прежде последовательно, в прежнем порядке (он определяет id
        строк file). Файлы, которым содержимое не нужно (крупные макеты
        Template.*, .bsl), хэшируются потоково и в память целиком не берутся.

        :param current: кэш состояний {rel: (granule, size, mtime_ns, sha256)}
            из update.scan_changes — не пересчитывать stat/хэш (инкремент);
        :param write_dump_state: писать dump_file (False при обновлении —
            update_db пишет их сам вместе с удалением старых строк гранулы);
        :return: (manifest_sha256, total_size) — только для полной сборки.
        """
        # префиксы папок подобъектов: {гранула: [(префикс, путь объекта)]}
        prefixes = {}
        for obj in objects:
            if obj['folder'] and obj['path']:
                rel = os.path.relpath(obj['folder'], self.dump_dir)
                rel = rel.replace(os.sep, '/')
                prefixes.setdefault(obj['granule'], []).append((rel, obj['path']))
        for lst in prefixes.values():
            lst.sort(key=lambda p: -len(p[0]))
        root_id = self.path_id.get('')
        # XML самих объектов уже хранятся в header_json — их текст в file.data
        # дублировать незачем
        xml_rels = {os.path.relpath(o['xml'], self.dump_dir).replace(os.sep, '/')
                    for o in objects if o['xml']}

        # плоский список задач в прежнем порядке: сортированные гранулы и rel
        # внутри них (порядок обхода определяет id строк file)
        todo = []
        for granule in sorted(granule_files):
            for rel in sorted(granule_files[granule]):
                base = rel.rsplit('/', 1)[-1]
                ext = base.rsplit('.', 1)[-1].lower() if '.' in base else ''
                # текст файлов храним, кроме: тел модулей (живут в module),
                # ConfigDumpInfo.xml (32 МБ служебных версий), XML самих
                # объектов (уже в header_json) и содержимого макетов
                # Template.* — cf-базы бинарные макеты тоже не хранят, а
                # запросы СКД из них уже извлечены в skd_query (на БП это
                # 3.2 ГБ справочных листов, которые никто не читает)
                want_text = (ext in W.TEXT_KINDS and ext != 'bsl'
                             and base != 'ConfigDumpInfo.xml'
                             and rel not in xml_rels
                             and not base.startswith('Template.'))
                want_raw = (not want_text) and ext != 'bsl' and self.store_blobs
                todo.append((granule, rel, want_text, want_text or want_raw))

        def read_one(item):
            """(size, mtime_ns, sha, data, data_ok) по файлу; None — файла нет.

            Ошибка чтения хэша (как и прежде) не перехватывается: пропавший
            посреди сборки файл — исключение; ошибка чтения СОДЕРЖИМОГО —
            data=None (файл остаётся в file/dump_file без текста).
            """
            _granule, rel, want_text, want_content = item
            full = os.path.join(self.dump_dir, *rel.split('/'))
            state = (current or {}).get(rel)
            blob = None
            if state is not None:
                _g, size, mtime_ns, sha = state
            else:
                try:
                    st = os.stat(helper.long_path(full))
                except OSError:
                    return None
                size = st.st_size
                mtime_ns = st.st_mtime_ns
                if want_content:
                    with open(helper.long_path(full), 'rb') as f:
                        blob = f.read()
                    sha = hashlib.sha256(blob).hexdigest()
                else:
                    sha = file_sha256(full)
            data, ok = None, False
            if want_content:
                try:
                    if blob is None:
                        with open(helper.long_path(full), 'rb') as f:
                            blob = f.read()
                    data = _decode_text(blob) if want_text else blob
                    ok = True
                except OSError:
                    data, ok = None, False
            return size, mtime_ns, sha, data, ok

        file_params = []
        dump_params = []
        manifest_entries = []   # (rel, sha) — хэш состава считаем в конце
        total_size = 0

        def handle(item, res):
            nonlocal total_size
            granule, rel, _wt, _wc = item
            size, mtime_ns, sha, data, ok = res
            if write_dump_state:
                manifest_entries.append((rel, sha))
                total_size += size
            plist = prefixes.get(granule, [])
            owner_id = self.path_id.get(granule) if granule else root_id
            for pref, obj_path in plist:
                if rel.startswith(pref + '/'):
                    owner_id = self.path_id.get(obj_path, owner_id)
                    break
            base = rel.rsplit('/', 1)[-1]
            ext = base.rsplit('.', 1)[-1].lower() if '.' in base else ''
            file_params.append((self.source_id, owner_id, rel,
                                ext or 'bin', size, data))
            if ok:
                self.stats['files_content'] += 1
            if write_dump_state:
                dump_params.append((rel, granule, size, mtime_ns, sha))
            self.stats['files'] += 1
            if len(file_params) >= 2000:
                self._flush_files(file_params, dump_params)

        threads = min(8, max(1, self.workers))
        if threads > 1 and len(todo) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=threads) as pool:
                # пачки ограничивают память: в полёте не больше пачки содержимых
                for start in range(0, len(todo), _READ_BATCH):
                    chunk = todo[start:start + _READ_BATCH]
                    for item, res in zip(chunk, pool.map(read_one, chunk)):
                        if res is not None:
                            handle(item, res)
        else:
            for item in todo:
                res = read_one(item)
                if res is not None:
                    handle(item, res)
        self._flush_files(file_params, dump_params)
        # манифест состава — по ОТНОСИТЕЛЬНЫМ ПУТЯМ (тот же порядок, что
        # update._manifest_sha по таблице dump_file: иначе отпечаток базы
        # зависел бы от источника — сборка или обновление)
        manifest = hashlib.sha256()
        for rel, sha in sorted(manifest_entries):
            manifest.update(rel.encode('utf-8'))
            manifest.update(b'\0')
            manifest.update(sha.encode('ascii'))
            manifest.update(b'\n')
        return manifest.hexdigest(), total_size

    def _flush_files(self, file_params, dump_params):
        if file_params:
            self.conn.executemany(W.INSERT_SQL['file'], file_params)
            file_params.clear()
        if dump_params:
            self.conn.executemany(
                'INSERT OR REPLACE INTO dump_file (path, granule, size,'
                ' mtime_ns, sha256) VALUES (?, ?, ?, ?, ?)', dump_params)
            self.stats['dump_files'] += len(dump_params)
            dump_params.clear()

    # -- фаза 3: межобъектные связи -----------------------------------------

    def resolve_links(self):
        """Полная фаза связей (сборка): refs + составы + субконто."""
        self.relink_refs()
        self.rebuild_content_tables()
        self._resolve_subconto()

    def relink_refs(self):
        """Заполняет object_id/uuid ссылок по target_path.

        Карта path→(id,uuid) строится в памяти одним запросом: коррелированный
        UPDATE на 36k строк упирался бы в план запроса (полное сканирование
        meta_object на строку без source_id в условии).
        """
        conn = self.conn
        rows = conn.execute(
            'SELECT id, target_path FROM attribute_ref'
            ' WHERE target_path IS NOT NULL AND object_id IS NULL').fetchall()
        if not rows:
            return
        meta = {p: (i, u or '') for p, i, u in conn.execute(
            'SELECT path, id, uuid FROM meta_object')}
        params = [(meta[tp][0], meta[tp][1], rid)
                  for rid, tp in rows if tp in meta]
        if params:
            conn.executemany(
                'UPDATE attribute_ref SET object_id=?, uuid=? WHERE id=?',
                params)

    def rebuild_content_tables(self):
        """Вставка subsystem_content/common_target из собранных списков."""
        conn = self.conn
        name2path = {}
        for path in self.known_paths:
            parts = path.split('/')
            if len(parts) == 2:
                name2path.setdefault((parts[0], parts[1].lower()), path)
        sub_params = []
        for sub_path, content in self.subsystem_content:
            sub_id = self.path_id.get(sub_path)
            if sub_id is None:
                continue
            for ord_no, item in enumerate(content):
                target = self._content_target_id(item, name2path)
                if target is not None:
                    sub_params.append((sub_id, target, ord_no))
        if sub_params:
            conn.executemany(W.INSERT_SQL['subsystem_content'], sub_params)
        common_params = []
        for common_path, content in self.common_targets:
            common_id = self.path_id.get(common_path)
            if common_id is None:
                continue
            for item in content:
                target = self._content_target_id(item, name2path)
                if target is not None and target != common_id:
                    common_params.append((common_id, target))
        if common_params:
            conn.executemany(W.INSERT_SQL['common_target'], common_params)
        self.stats['subsystem_content'] = len(sub_params)
        self.stats['common_targets'] += len(common_params)

    def _content_target_id(self, item, name2path):
        """«Класс.Имя» из Content → id объекта."""
        if '.' not in item:
            return None
        cls, name = item.split('.', 1)
        # вложенные подсистемы: Subsystem.Р.Subsystem.Д — берём последний сегмент
        if '.' in name:
            parts = name.split('.')
            name = parts[-1]
        path = name2path.get((cls, name.lower()))
        return self.path_id.get(path) if path else None

    def _resolve_subconto(self):
        """Виды субконто предопределённых счетов: имя ПВХ-элемента → id."""
        if not self.subconto_refs:
            return
        conn = self.conn
        # uuid предопределённого -> id (uuid элемента ПВХ указан в ref)
        by_obj_uuid = {}
        for pid, obj_path, puuid in conn.execute(
                'SELECT p.id, o.path, p.uuid FROM predefined p'
                ' JOIN meta_object o ON o.id=p.object_id'
                ' WHERE p.uuid IS NOT NULL'):
            by_obj_uuid[(obj_path, puuid)] = pid
        obj_by_path = {}
        for path, oid in self.path_id.items():
            obj_by_path[path] = oid
        rows = []
        for obj_path, item_ord, sub_ord, ref, flags in self.subconto_refs:
            # ref = 'ChartOfCharacteristicTypes.ПВХ.Элемент'
            parts = ref.split('.')
            if len(parts) < 3:
                continue
            cls = xmlmeta.REF_CLASS.get(parts[0] + 'Ref') or \
                _PLURAL_CLASS.get(parts[0])
            if not cls:
                continue
            dt_path = f'{cls}/{parts[1]}'
            dt_id = obj_by_path.get(dt_path)
            if dt_id is None:
                continue
            # uuid элемента ПВХ неизвестен по имени — ищем по имени элемента
            kind_id = conn.execute(
                'SELECT id FROM predefined WHERE object_id=? AND name=? LIMIT 1',
                (dt_id, parts[2])).fetchone()
            predefined_id = conn.execute(
                'SELECT id FROM predefined WHERE object_id=? AND ord=? LIMIT 1',
                (obj_by_path.get(obj_path), item_ord)).fetchone()
            if not kind_id or not predefined_id:
                continue
            elem_uuid = conn.execute('SELECT uuid FROM predefined WHERE id=?',
                                     (kind_id[0],)).fetchone()
            rows.append((predefined_id[0], sub_ord,
                         elem_uuid[0] if elem_uuid else None, kind_id[0],
                         ';'.join(flags) or None))
        if rows:
            conn.executemany(W.INSERT_SQL['predefined_subconto'], rows)
        self.stats['subconto'] += len(rows)
        _ = by_obj_uuid  # зарезервировано для точного разрешения по uuid

    def write_rights(self):
        """Права ролей из Ext\\Rights.xml (после всех объектов)."""
        if not self.roles:
            return
        from . import rights as dump_rights
        dump_rights.write_role_rights(
            self.conn, self.dump_dir, self.roles, self.path_id, self.stats,
            warnings=self.warnings)


# классы во множественном числе в Content/правах → тип объекта в БД
_PLURAL_CLASS = {
    'ChartOfCharacteristicTypes': 'ChartOfCharacteristicType',
    'DocumentNumerators': 'DocumentNumerators',
    'Sequences': 'Sequences',
}


def _command_element(root, name):
    """Узел <Command> с данным именем из ChildObjects родителя."""
    children = xmlmeta.child_objects(root)
    if children is None:
        return None
    for child in children:
        if child.tag == 'Command':
            props = xmlmeta.properties(child)
            if xmlmeta._text(props, 'Name') == name:
                return child
    return None


_OBJ_VERSION_CACHE = {}


def _object_version(conn):
    """obj_version объектов: редакция конфигурации (например '803')."""
    if 'v' not in _OBJ_VERSION_CACHE:
        row = conn.execute(
            "SELECT obj_version FROM meta_object WHERE path=''").fetchone()
        full = row[0] if row and row[0] else None
        _OBJ_VERSION_CACHE['v'] = full[:3] if full else None
    return _OBJ_VERSION_CACHE['v']


def _compat_digits(compat):
    """'Version8_3_21' / '8.3.21' → '80321' (формат obj_version корня в cf)."""
    text = compat.replace('Version', '').replace('_', '.').strip()
    parts = [p for p in text.split('.') if p.isdigit()]
    if len(parts) >= 3:
        return '%s%02d%02d' % (parts[0], int(parts[1]), int(parts[2]))
    digits = ''.join(ch for ch in text if ch.isdigit())
    return digits or ''


def build_db(dump_dir, db_path, *, workers=1, build_fts=True,
             store_blobs=False, log=print):
    """Полная сборка базы знаний из выгрузки конфигурации в файлы.

    :param dump_dir: каталог иерархической выгрузки (с ConfigDumpInfo.xml)
    :param db_path: файл SQLite (существующий перезаписывается)
    :param workers: число процессов разбора BSL (1 — последовательно)
    :param build_fts: строить приставной FTS-индекс по телам методов
    :param store_blobs: хранить бинарные файлы (картинки/макеты) как BLOB
    :return: словарь статистики
    """
    begin = datetime.now()
    dump_dir = os.path.abspath(dump_dir)
    if not os.path.isfile(os.path.join(dump_dir, 'ConfigDumpInfo.xml')):
        raise ValueError(
            f'это не каталог выгрузки конфигурации (нет ConfigDumpInfo.xml): {dump_dir}')
    db_path = os.path.abspath(db_path)
    if os.path.isdir(db_path):
        raise ValueError(f'Путь БД указывает на каталог: {db_path}')
    parent = os.path.dirname(db_path)
    os.makedirs(helper.long_path(parent), exist_ok=True)

    log(f'Обход выгрузки {dump_dir}')
    objects, granule_files, warnings = model.scan(dump_dir)
    known_paths = frozenset(o['path'] for o in objects if o['path'])
    log(f'Объектов: {len(objects)}, файлов: '
        f'{sum(len(v) for v in granule_files.values())}')

    if os.path.exists(db_path):
        # Windows: файл может держать антивирус/индексатор — короткий retry
        import time
        for attempt in range(5):
            try:
                os.remove(db_path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(1 + attempt)
    W.drop_fts_index(db_path)
    _OBJ_VERSION_CACHE.clear()

    conn = sqlite3.connect(db_path)
    stats = None
    try:
        conn.execute('PRAGMA synchronous=OFF')
        conn.execute('PRAGMA cache_size=-262144')
        conn.execute('PRAGMA page_size=16384')
        conn.executescript(W.SCHEMA)
        conn.execute(f'PRAGMA user_version={W.SCHEMA_VERSION}')
        conn.execute('BEGIN')
        cur = conn.execute(
            W.INSERT_SQL['source'],
            (dump_dir, datetime.now().isoformat(timespec='seconds'),
             None, None, _extractor_version()))
        source_id = cur.lastrowid

        builder = Builder(dump_dir, conn, source_id, workers=workers,
                          store_blobs=store_blobs, known_paths=known_paths,
                          warnings=warnings)
        stats = builder.stats
        log('Фаза 1: объекты метаданных')
        for obj in objects:
            builder.process_object(obj)
        log('Фаза 2: модули BSL, файлы, хэши')
        builder.write_modules()
        sha, total_size = builder.write_files(granule_files, objects)
        log('Фаза 3: связи, составы, права ролей')
        builder.resolve_links()
        builder.write_rights()
        conn.execute(
            'UPDATE source SET file_size=?, file_sha256=? WHERE id=?',
            (total_size, sha, source_id))
        conn.execute(
            'INSERT OR REPLACE INTO dump_state (id, dump_dir, updated)'
            ' VALUES (1, ?, ?)',
            (dump_dir, datetime.now().isoformat(timespec='seconds')))
        conn.commit()
        # WAL: инкрементальное обновление работает при подключённом
        # read-only сервере (писатель и читатели не блокируют друг друга)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if warnings:
        log(f'Предупреждений: {len(warnings)} (первые 10):')
        for w in warnings[:10]:
            log(f'  {w}')
    if build_fts:
        log('Фаза 4: FTS-индекс')
        W.build_fts_index(db_path, workers=workers)
    stats['dump_dir'] = dump_dir
    stats['db'] = db_path
    stats['source_sha256'] = sha
    stats['warnings'] = len(warnings)
    stats['elapsed'] = str(datetime.now() - begin)
    log(f'Готово за {stats["elapsed"]}')
    return stats


def _extractor_version():
    try:
        from .. import __version__
        return __version__
    except ImportError:
        return None
