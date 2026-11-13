"""Общий слой логики консольного (TUI) и веб-интерфейса.

Здесь живёт ВСЯ бизнес-логика интерфейсов: состояние (~/.confdb/config.json),
кандидаты файлов/баз, сканирование каталога cf по SHA-256, извлечение (.cf и
выгрузка), FTS, бенчмарк, группы баз и жизненный цикл MCP-сервера. TUI
(`tui.py`) и веб-интерфейс (`webui.py`) — тонкие обёртки над этим слоем:
консольные меню и HTTP+SPA соответственно.

Правило сопровождения: новая возможность интерфейса добавляется СНАЧАЛА сюда
(метод UiCore или функция), затем подключается в TUI и web UI. Так доработка
одного интерфейса не расходится с другим — логика у них одна.
"""
import glob
import os
import threading

from . import __version__
from . import llm_log
from .config import bench_workers
from .config import load_config as _load_config
from .config import save_config as _save_config
from .extract import check_same_source
from .extract import extract as _extract_cf

# файлы 1С, которые умеет извлекать конвейер (внешние отчёты .erf тоже)
CF_EXTS = ('.cf', '.cfe', '.epf', '.erf')


def _project_root():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def out_defaults(src):
    """(путь базы, путь дампа) по умолчанию для исходника — каталог _out."""
    base = os.path.splitext(os.path.basename(src))[0]
    out_dir = os.path.join(_project_root(), '_out')
    return os.path.join(out_dir, base + '.sqlite'), os.path.join(out_dir, base)


def cf_files(directory):
    """Файлы 1С каталога (.cf/.cfe/.epf/.erf), отсортированные по имени."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    found = []
    for name in names:
        full = os.path.join(directory, name)
        if name.lower().endswith(CF_EXTS) and os.path.isfile(full):
            found.append(os.path.abspath(full))
    return sorted(found)


def cf_candidates(extra_dirs=()):
    """Файлы 1С для выбора: текущий каталог, корень проекта, cf/, доп. каталоги."""
    roots = [os.getcwd(), _project_root()]
    roots += [d for d in extra_dirs if d]
    found = []
    for root in dict.fromkeys(roots):
        for pattern in ('*.cf', '*.cfe', '*.epf', '*.erf',
                        os.path.join('cf', '*.cf'), os.path.join('cf', '*.cfe')):
            found += glob.glob(os.path.join(root, pattern))
    return sorted({os.path.abspath(p) for p in found})


def db_candidates(db_dirs=()):
    """Базы SQLite для выбора: текущий каталог, корень, _out, каталоги баз."""
    roots = [os.getcwd(), _project_root(), os.path.join(_project_root(), '_out')]
    roots += [d for d in db_dirs if os.path.isdir(d)]
    found = []
    for root in dict.fromkeys(roots):
        for pattern in ('*.db', '*.sqlite'):
            found += glob.glob(os.path.join(root, pattern))
    return sorted({os.path.abspath(p) for p in found if os.path.isfile(p)})


def load_groups(config):
    """Группы баз из конфига; старый ключ 'packs' читается как группы."""
    raw = config.get('groups')
    if raw is None:
        raw = config.get('packs') or {}
    if not isinstance(raw, dict):
        return {}
    return {str(name): [str(p) for p in dbs]
            for name, dbs in raw.items()
            if isinstance(dbs, (list, tuple))}


def shown_dbs(paths):
    """Список баз группы для печати: отсутствующие файлы помечены."""
    return ', '.join(
        os.path.basename(p) + ('' if os.path.isfile(p) else ' (нет файла)')
        for p in paths)


def first_db(dbs, groups=()):
    """База, которая станет активной при таком запуске (первая база первой группы)."""
    for _name, paths in groups or ():
        if paths:
            return paths[0]
    return dbs[0] if dbs else None


def group_cli_args(groups):
    """Флаги --group для запуска сервера: по одному на базу (';' в имени файла)."""
    args = []
    for name, paths in groups or ():
        for path in paths:
            args += ['--group', f'{name}={path}']
    return args


def resolve_launch_groups(config_groups, names):
    """Выбранные имена групп -> ([(имя, [пути])], [недоступные пути])."""
    groups, missing = [], []
    for name in names:
        raw = list(config_groups.get(name) or ())
        paths = [p for p in raw if os.path.isfile(p)]
        missing += [p for p in raw if not os.path.isfile(p)]
        if paths:
            groups.append((name, paths))
    return groups, missing


def extract_options(opts):
    """Опции декодера из настроек (store_blobs/prefix/skip_errors)."""
    options = {'store_blobs': bool(opts.get('store_blobs'))}
    if opts.get('prefix'):
        options['prefix'] = opts['prefix']
    if opts.get('skip_errors'):
        options['skip_errors'] = True
    return options


def _version_schema(version):
    """Первые две цифры версии (major, ревизия схемы) либо None.

    По ним решается, нужна ли пересборка базы: изменение ревизии схемы меняет
    данные/декодер, а патч (последняя цифра) — только инструменты MCP/CLI.
    None (нет версии/не распознана) != текущая ревизия — база на пересборку.
    """
    parts = str(version or '').split('.')
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def scan_cf_sources(cf_dir, db_pool, log=print):
    """Сверяет файлы 1С каталога с базами знаний по SHA-256 исходника.

    Для каждого файла каталога ищется база, собранная из него: сначала по
    совпадению отпечатка (source.file_sha256), затем по имени исходного файла
    (source.file). Отпечаток считается потоково (file_sha256), поэтому каталог
    с крупными .cf сканируется без чтения файлов в память целиком.

    :return: список записей {'src','sha','status','db','found','note',
        'ext','name','root_type','db_version','cur_version'}:
        'current'  — база уже собрана из этого же файла текущим распаковщиком
                     (извлечение не нужно);
        'outdated' — база из одноимённого файла, но отпечаток другой (замена);
        'version'  — файл тот же, но ревизия схемы распаковщика другая
                     (пересборка: новые данные/исправления декодера);
        'new'      — базы нет (извлечение в путь по умолчанию out_defaults).
        ext — тип файла (cf/cfe/epf/erf); name/root_type — имя и тип корневого
        объекта метаданных из собранной базы (для 'new' — None); db_version —
        версия распаковщика, которым собрана найденная база (source.extractor_version),
        cur_version — текущая версия.

        Версии сравниваются по ПЕРВЫМ ДВУМ цифрам (major.ревизия схемы):
        только их изменение требует пересборки. Последняя цифра — доработки
        инструментов (MCP/CLI), распаковщика не касается (правило пользователя
        2026-11-13). У старых баз версии нет (None) — они помечаются на пересборку.
    """
    from .extract import file_sha256, source_info
    files = cf_files(cf_dir)
    pool = {}
    for path in db_pool:
        if not path:
            continue
        key = os.path.abspath(path)
        if key in pool or not os.path.isfile(key):
            continue
        info = source_info(key)
        if info:
            pool[key] = info
    records = []
    for i, src in enumerate(files, 1):
        log(f'Хэш {i}/{len(files)}: {os.path.basename(src)}')
        sha = file_sha256(src)
        base = os.path.basename(src).lower()
        same_sha = sorted(p for p, inf in pool.items()
                          if inf.get('file_sha256') == sha)
        same_name = sorted(p for p, inf in pool.items()
                           if os.path.basename(inf.get('file') or '').lower() == base)
        default_db = out_defaults(src)[0]
        note = ''
        if same_sha:
            found = next((p for p in same_sha if p in same_name), same_sha[0])
            status, db = 'current', found
        elif same_name:
            found = (default_db if default_db in same_name
                     else max(same_name, key=lambda p: pool[p].get('created') or ''))
            status, db = 'outdated', found
            if not pool[found].get('file_sha256'):
                note = 'в базе нет отпечатка исходника — сравнение по имени'
        else:
            status, db, found = 'new', default_db, None
            if os.path.isfile(default_db):
                note = 'путь по умолчанию занят другой базой — будет заменён'
        # тип файла (cf/cfe/epf/erf) и имя/тип корневого объекта метаданных —
        # из собранной базы (source.root_name/root_type); для новой базы имени
        # ещё нет (оно появится после извлечения)
        ext = os.path.splitext(src)[1].lstrip('.').lower()
        meta = pool.get(found) or {}
        db_version = meta.get('extractor_version')
        # проверка версии распаковщика: пересборка нужна только при другой
        # ревизии схемы — первые две цифры версии (major.ревизия). Патч
        # (последняя цифра) — доработки инструментов, распаковщика не касается.
        # Версия берётся из source.extractor_version; у старых баз её нет (None).
        if (status == 'current'
                and _version_schema(db_version) != _version_schema(__version__)):
            status = 'version'
            note = (f'собрана распаковщиком {db_version or "неизвестной версии"}, '
                    f'актуальный {__version__} — другая ревизия схемы, пересобрать')
        records.append({'src': src, 'sha': sha, 'status': status,
                        'db': db, 'found': found, 'note': note,
                        'ext': ext, 'name': meta.get('root_name'),
                        'root_type': meta.get('root_type'),
                        'db_version': db_version, 'cur_version': __version__})
    return records


# ---------- классификация баз (тип исходника + имя корневого объекта) ----------

_DB_META_CACHE = {}


def db_meta(db_path):
    """Классификация базы знаний: {'ext','name','root_type'}.

    ext — тип ИСХОДНИКА (cf/cfe/epf/erf из source.file), name/root_type — имя и
    тип корневого объекта метаданных (source.root_name/root_type). Та же
    классификация, что в каталоге cf, но для готовой базы. Кэш по mtime/size:
    повторные снимки состояния не перечитывают неизменившиеся базы.
    """
    from .extract import source_info
    try:
        st = os.stat(db_path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {'ext': '', 'name': None, 'root_type': None}
    cached = _DB_META_CACHE.get(db_path)
    if cached and cached[0] == key:
        return cached[1]
    info = source_info(db_path) or {}
    ext = os.path.splitext(info.get('file') or '')[1].lstrip('.').lower()
    meta = {'ext': ext, 'name': info.get('root_name'),
            'root_type': info.get('root_type')}
    _DB_META_CACHE[db_path] = (key, meta)
    return meta


# ---------- вспомогательное для панели запущенного сервера ----------

def replace_server_dbs(server, paths):
    """Заменяет открытые базы запущенного сервера на указанный набор."""
    for alias in list(server.dbs):
        server.close_db(alias)
    opened, errors = [], []
    for path in paths:
        try:
            opened.append(server.open_db(path, activate=False))
        except ValueError as err:
            errors.append(str(err))
    return opened, errors


def schema_tail(server, alias):
    """' — схема N — устарела (сервер ждёт M)' для базы чужой ревизии, иначе ''."""
    if not server.outdated(alias):
        return ''
    from .mcp_server import schema_mark
    return ' — ' + schema_mark(server.dbs[alias]['schema'])


def compact_db_list(server):
    """Список открытых баз для панели: алиас, путь, отметка об устаревании."""
    if not server.dbs:
        return 'Нет открытых баз.'
    lines = ['Открытые базы (* — активная):']
    for alias, info in server.dbs.items():
        mark = '*' if alias == server.active else ' '
        lines.append(f'{mark} {alias} — {info["path"]}{schema_tail(server, alias)}')
    return '\n'.join(lines)


# ---------- журнал LLM в отдельной базе (общее для TUI и веб) ----------
# Запись делает MCP-сервер (mcp_server.McpServer.llm_writer), здесь —
# настройки, просмотр и очистка. Просмотр работает и при остановленном
# сервере: журнал — отдельный файл, не часть баз знаний.

def llm_log_settings():
    """Настройки журнала: {'enabled': bool, 'path': str}."""
    return llm_log.settings()


def set_llm_log_enabled(enabled, path=None):
    """Включает/выключает запись журнала (сохраняется в конфиге)."""
    return llm_log.save_settings(enabled, path)


def llm_log_status():
    """Настройки + сводка журнала (записи, ошибки, размер, инструменты)."""
    result = llm_log.settings()
    result.update(llm_log.stats(result['path']))
    return result


def llm_log_list(limit=50, offset=0, tool=None, errors_only=False, search=None):
    """Страница журнала вызовов (новые сверху), с фильтрами."""
    return llm_log.list_calls(llm_log.settings()['path'], limit=limit,
                              offset=offset, tool=tool,
                              errors_only=errors_only, search=search)


def llm_log_item(call_id):
    """Полная запись вызова: аргументы и ответ без обрезки."""
    return llm_log.get_call(llm_log.settings()['path'], call_id)


def llm_log_clear():
    """Удаляет все записи журнала; возвращает число удалённых."""
    return llm_log.clear(llm_log.settings()['path'])


def apply_llm_log_to_server(server):
    """Применяет настройки журнала к запущенному серверу (живой переключатель)."""
    llm_log.configure_server(server, strict=True)


class ServerRunner:
    """Запуск/остановка MCP-сервера (HTTP) внутри живого процесса.

    Общий для TUI и веб-интерфейса. Останавливая сервер, закрывает соединения
    (close_all) и убирает pidfile, чтобы файлы баз освободились для пересборки
    (Windows держит файл, пока живо sqlite-соединение).
    """

    def __init__(self):
        self.server = None
        self.httpd = None
        self.port = None
        self._thread = None
        self._pidfiles = []

    @property
    def running(self):
        return self.server is not None

    def start(self, dbs, groups=(), port=8765, host='127.0.0.1'):
        from .mcp_server import (McpServer, _write_pidfiles, resolve_syntax_db,
                                 start_http_server)
        if self.running:
            raise ValueError('сервер уже запущен')
        server = McpServer(dbs, groups=list(groups),
                           syntax_db=resolve_syntax_db())
        # запись журнала LLM, если включена в конфиге (переключатель в UI)
        llm_log.configure_server(server)
        httpd, real_port = start_http_server(server, host, int(port))
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()
        self._pidfiles = _write_pidfiles(server, real_port)
        self.server, self.httpd, self.port = server, httpd, real_port
        return real_port

    def stop(self):
        from .mcp_server import _remove_pidfiles
        if not self.running:
            return
        self.httpd.shutdown()
        self.httpd.server_close()
        _remove_pidfiles(self._pidfiles)
        self.server.close_all()
        self.server = self.httpd = self._thread = None
        self._pidfiles = []
        self.port = None


class UiCore:
    """Состояние и операции интерфейсов (общие для TUI и веб).

    Хранит настройки в ~/.confdb/config.json (те же ключи, что TUI), держит
    запущенный MCP-сервер (ServerRunner) и выполняет извлечение/сканирование/
    бенчмарк. Веб-интерфейс вызывает эти методы из HTTP-обработчиков; TUI —
    из консольных меню.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.runner = ServerRunner()
        self._load()

    # ---------- состояние ----------

    def _load(self):
        config = _load_config()
        self.recent_src = config.get('recent_src', [])
        self.recent_db = config.get('recent_db', [])
        self.db_dirs = config.get('db_dirs', [])
        self.last_db = config.get('last_db', '')
        opts = config.get('options', {})
        self.src = opts.get('src', '')
        self.db = opts.get('db', '')
        self.cf_dir = opts.get('cf_dir', '')
        self.dump = opts.get('dump', '')
        self.temp = opts.get('temp', '')
        self.prefix = opts.get('prefix', '')
        self.keep_temp = bool(opts.get('keep_temp'))
        self.store_blobs = bool(opts.get('store_blobs'))
        self.skip_errors = bool(opts.get('skip_errors'))
        self.build_fts = bool(opts.get('build_fts', True))
        self.workers = int(opts.get('workers') or bench_workers() or 1)
        self.groups = load_groups(config)

    def save(self):
        with self._lock:
            config = _load_config()
            config.update({
                'recent_src': self.recent_src,
                'recent_db': self.recent_db,
                'db_dirs': self.db_dirs,
                'last_db': self.last_db,
                'groups': self.groups,
                # слияние с текущими options: чужие ключи (журнал LLM и др.)
                # не затираются — их могли сохранить веб/другой интерфейс
                'options': {**(config.get('options') or {}),
                    'src': self.src, 'db': self.db, 'cf_dir': self.cf_dir,
                    'dump': self.dump, 'temp': self.temp,
                    'prefix': self.prefix, 'keep_temp': self.keep_temp,
                    'store_blobs': self.store_blobs, 'skip_errors': self.skip_errors,
                    'build_fts': self.build_fts, 'workers': self.workers,
                },
            })
            _save_config(config)

    def options_dict(self):
        return {'src': self.src, 'db': self.db, 'cf_dir': self.cf_dir,
                'dump': self.dump, 'temp': self.temp, 'prefix': self.prefix,
                'keep_temp': self.keep_temp, 'store_blobs': self.store_blobs,
                'skip_errors': self.skip_errors, 'build_fts': self.build_fts,
                'workers': self.workers}

    def set_options(self, values):
        """Обновляет настройки из словаря (веб-форма), с проверкой типов."""
        with self._lock:
            opts = self.options_dict()
            for key in opts:
                if key not in values:
                    continue
                val = values[key]
                if key in ('keep_temp', 'store_blobs', 'skip_errors', 'build_fts'):
                    opts[key] = bool(val)
                elif key == 'workers':
                    workers = int(val)
                    if workers < 1:
                        raise ValueError('workers должен быть >= 1')
                    opts[key] = workers
                else:
                    opts[key] = str(val or '')
            for key, val in opts.items():
                setattr(self, key, val)
            self.save()

    def add_db_dir(self, directory):
        directory = os.path.abspath(directory)
        if not os.path.isdir(directory):
            raise ValueError(f'нет такого каталога: {directory}')
        if directory not in self.db_dirs:
            self.db_dirs.append(directory)
            self.save()
        return directory

    # ---------- кандидаты ----------

    def cf_candidates(self):
        return cf_candidates([self.cf_dir])

    def db_candidates(self):
        return db_candidates(self.db_dirs)

    def db_pool(self):
        recent = [r for r in ([self.last_db] + self.recent_db)
                  if r and os.path.isfile(r)]
        return list(dict.fromkeys(self.db_candidates() + recent))

    # ---------- сканирование каталога cf ----------

    def scan_cf(self, log=print):
        if not self.cf_dir or not os.path.isdir(self.cf_dir):
            raise ValueError('каталог cf не задан или не существует')
        files = cf_files(self.cf_dir)
        if not files:
            return []
        pool = self.db_pool()
        for paths in self.groups.values():
            pool += paths
        pool += [out_defaults(src)[0] for src in files]
        return scan_cf_sources(self.cf_dir, pool, log=log)

    # ---------- извлечение ----------

    def extract_options(self):
        return extract_options(self.options_dict())

    def _remember_extract(self, src, db, stats):
        if stats.get('db'):
            self.last_db = stats['db']
        self.recent_src = [src] + [x for x in self.recent_src if x != src][:5]
        if db:
            self.recent_db = [db] + [x for x in self.recent_db if x != db][:5]
        self.save()

    def extract(self, src, db=None, dump=None, log=print, source_sha256=None):
        """Извлечение .cf/.cfe/.epf либо каталога выгрузки (автоопределение).

        source_sha256 — готовый отпечаток (пакетный режим после сканирования):
        с ним проверка «не та же ли база» не хэширует файл повторно.
        """
        src = os.path.abspath(src)
        if os.path.isdir(src):
            from .dump import is_dump_dir
            if is_dump_dir(src):
                return self.extract_dump(src, db, log=log)
        if not os.path.isfile(src):
            raise FileNotFoundError(src)
        db = db or None
        dump = dump or None
        if db and os.path.isdir(db):
            base = os.path.splitext(os.path.basename(src))[0]
            db = os.path.join(db, base + '.sqlite')
        if not db and not dump:
            db, dump = out_defaults(src)
        for out in (db, dump):
            if out:
                os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        if db and source_sha256 is None:
            source_sha256, skip = check_same_source(src, db, log=log)
            if skip:
                return {'skipped': True, 'db': db, 'src': src,
                        'elapsed': '0:00:00'}
        stats = _extract_cf(
            src, db_path=db, dump_dir=dump, temp_dir=self.temp or None,
            keep_temp=self.keep_temp, options=self.extract_options(),
            workers=self.workers, build_fts=self.build_fts,
            source_sha256=source_sha256)
        self._remember_extract(src, db, stats)
        return stats

    def extract_dump(self, src, db=None, log=print):
        """Сборка/инкрементальное обновление базы из выгрузки в файлы."""
        from .dump import build_db, update_db
        src = os.path.abspath(src)
        if not db:
            db = out_defaults(src)[0]
        if os.path.isdir(db):
            base = os.path.basename(os.path.normpath(src))
            db = os.path.join(db, base + '_dump.sqlite')
        os.makedirs(os.path.dirname(os.path.abspath(db)), exist_ok=True)
        if os.path.isfile(db):
            log('База уже есть — инкрементальное обновление (только изменённые объекты).')
            stats = update_db(src, db, workers=self.workers,
                              rebuild_fts=None if self.build_fts else False)
        else:
            stats = build_db(src, db, workers=self.workers,
                             build_fts=self.build_fts, store_blobs=self.store_blobs)
        self._remember_extract(src, db, stats)
        return stats

    def build_fts(self, db, log=print):
        from .db.writer import build_fts_index, fts_dir
        log(f'Строим FTS-индекс рядом с {db} в {self.workers} процессов…')
        methods = build_fts_index(db, workers=self.workers)
        log(f'Готово: {methods} методов. Файлы индекса: {fts_dir(db)}')
        return methods

    def bench(self, src, log=print):
        from .bench import bench as run_bench
        best = run_bench(os.path.abspath(src))
        self.workers = best
        self.recent_src = [src] + [x for x in self.recent_src if x != src][:5]
        self.save()
        return best

    # ---------- группы баз (конфиг) ----------

    def set_group(self, name, paths):
        name = str(name).strip()
        if not name:
            raise ValueError('имя группы не может быть пустым')
        if paths:
            self.groups[name] = [str(p) for p in paths]
        else:
            self.groups.pop(name, None)
        self.save()

    def delete_group(self, name):
        self.groups.pop(name, None)
        self.save()

    # ---------- MCP-сервер ----------

    def start_server(self, dbs, groups=(), port=8765):
        with self._lock:
            return self.runner.start(dbs, groups, port)

    def stop_server(self):
        with self._lock:
            self.runner.stop()

    def server_status(self):
        if not self.runner.running:
            return {'running': False}
        server = self.runner.server
        dbs = []
        for alias, info in server.dbs.items():
            dbs.append({'alias': alias, 'path': info['path'],
                        'active': alias == server.active,
                        'outdated': bool(server.outdated(alias)),
                        'tail': schema_tail(server, alias)})
        groups = [{'name': name, 'aliases': list(aliases),
                   'active': name == server.active_group}
                  for name, aliases in server.groups.items()]
        return {'running': True, 'port': self.runner.port,
                'url': f'http://127.0.0.1:{self.runner.port}/mcp',
                'active': server.active, 'active_group': server.active_group,
                'dbs': dbs, 'groups': groups}

    def _server(self):
        if not self.runner.running:
            raise ValueError('сервер не запущен')
        return self.runner.server

    def server_llm_log(self):
        """Строки журнала запросов LLM запущенного сервера (консоль лога).

        Пустой список, если сервер не запущен. Ответы на запросы LLM копятся
        в кольцевом буфере сервера (mcp_server.McpServer.llm_log).
        """
        if not self.runner.running:
            return []
        return self.runner.server.llm_log_lines()

    # ---------- журнал LLM в отдельной базе ----------

    def llm_log_status(self):
        """Настройки + сводка журнала + признак запущенного сервера."""
        result = llm_log_status()
        result['running'] = self.runner.running
        return result

    def set_llm_log_enabled(self, enabled):
        """Переключатель записи журнала: конфиг + живой сервер, если запущен."""
        llm_log.save_settings(enabled)
        if self.runner.running:
            llm_log.configure_server(self.runner.server, strict=True)
        return self.llm_log_status()

    def llm_log_calls(self, limit=50, offset=0, tool=None, errors_only=False,
                      search=None):
        return llm_log_list(limit=limit, offset=offset, tool=tool,
                            errors_only=errors_only, search=search)

    def llm_log_call(self, call_id):
        return llm_log_item(call_id)

    def llm_log_clear(self):
        return llm_log_clear()

    def server_use(self, alias):
        self._server().active = self._server()._alias(alias)
        return self.server_status()

    def server_open_db(self, path):
        alias = self._server().open_db(path)
        return alias, self.server_status()

    def server_close_db(self, alias):
        self._server().close_db(alias)
        return self.server_status()

    def server_create_group(self, name):
        self._server().create_group(name)
        return self.server_status()

    def server_add_db_to_group(self, group, alias):
        self._server().add_db_to_group(group, alias)
        return self.server_status()

    def server_close_group(self, name):
        self._server().close_group(name)
        return self.server_status()

    def server_use_group(self, name):
        self._server().use_group(name)
        return self.server_status()

    def apply_config_group(self, name, replace=False):
        """Группа из конфига -> работающий сервер: добавить либо заменить всё."""
        server = self._server()
        paths = self.groups.get(name)
        if paths is None:
            raise ValueError(f'группы «{name}» нет в конфиге')
        if replace:
            for group in list(server.groups):
                server.close_group(group)
            replace_server_dbs(server, ())
            result = server.add_group_paths(name, paths, activate=True)
        else:
            result = server.add_group_paths(name, paths)
        return result, self.server_status()

    # ---------- сводка для интерфейса ----------

    def db_meta_map(self):
        """Классификация всех баз, видимых интерфейсу: {путь: db_meta}."""
        paths = set(self.db_candidates())
        for grp in self.groups.values():
            paths.update(grp)
        paths.update(self.recent_db)
        if self.last_db:
            paths.add(self.last_db)
        if self.runner.running:
            paths.update(info['path'] for info in self.runner.server.dbs.values())
        return {p: db_meta(p) for p in sorted(paths) if os.path.isfile(p)}

    def snapshot(self):
        return {'version': __version__,
                'options': self.options_dict(),
                'groups': self.groups,
                'recent_src': self.recent_src,
                'recent_db': self.recent_db,
                'db_dirs': self.db_dirs,
                'last_db': self.last_db,
                'cf_candidates': self.cf_candidates(),
                'db_candidates': self.db_candidates(),
                'db_meta': self.db_meta_map(),
                'llm_log': llm_log.settings(),
                'server': self.server_status()}
