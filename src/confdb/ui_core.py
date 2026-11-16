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
from datetime import datetime

from . import __version__
from . import llm_log
from .config import bench_workers
from .config import load_config as _load_config
from .config import save_config as _save_config
from .dump import is_dump_dir
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


def dump_dirs(directory):
    """Каталоги выгрузки в файлы (признак ConfigDumpInfo.xml) среди детей."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    found = []
    for name in names:
        full = os.path.join(directory, name)
        if os.path.isdir(full) and is_dump_dir(full):
            found.append(os.path.abspath(full))
    return sorted(found)


def _is_dump_db(db_path):
    """База собрана из выгрузки в файлы (есть таблица состояния dump_state)."""
    import sqlite3
    uri = 'file:' + os.path.abspath(db_path).replace(os.sep, '/') + '?mode=ro'
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute("SELECT COUNT(*) FROM sqlite_master "
                           "WHERE type='table' AND name='dump_state'").fetchone()
        return bool(row and row[0])
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def cf_candidates(extra_dirs=()):
    """Файлы 1С для выбора: текущий каталог, корень проекта, cf/, доп. каталоги."""
    roots = [os.getcwd(), _project_root()]
    roots += [d for d in extra_dirs if d]
    found = []
    for root in dict.fromkeys(roots):
        for pattern in ('*.cf', '*.cfe', '*.epf', '*.erf',
                        os.path.join('cf', '*.cf'), os.path.join('cf', '*.cfe')):
            found += glob.glob(os.path.join(root, pattern))
        # выгрузки в файлы — тоже кандидаты извлечения: сам каталог,
        # дети-выгрузки и выгрузки внутри cf/
        if is_dump_dir(root):
            found.append(root)
        found += dump_dirs(root)
        found += dump_dirs(os.path.join(root, 'cf'))
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


def _count(value):
    """Количество элементов статистики: список — длина, число — как есть."""
    if isinstance(value, (list, tuple)):
        return len(value)
    return value if value is not None else 0


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
        Выгрузки в файлы (каталоги с ConfigDumpInfo.xml) попадают в список
        с ext='dump' и sha=None: изменённость определяется состоянием состава
        dump_file (stat-обход + хэши изменённых файлов), 'outdated' означает
        инкрементальное обновление; база ищется по source.file = путь выгрузки.
        ext — тип файла (cf/cfe/epf/erf); name/root_type — имя и тип корневого
        объекта метаданных из собранной базы (для 'new' — None); db_version —
        версия распаковщика, которым собрана найденная база (source.extractor_version),
        cur_version — текущая версия.

        Версии сравниваются по ПЕРВЫМ ДВУМ цифрам (major.ревизия схемы):
        только их изменение требует пересборки. Последняя цифра — доработки
        инструментов (MCP/CLI), распаковщика не касается (правило пользователя
        2026-11-13). У старых баз версии нет (None) — они помечаются на пересборку.
    """
    from .cf_update import _cf_db_state
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
        incremental = bool(status == 'outdated' and found
                           and _cf_db_state(found)[1])
        if incremental:
            note = ('другой отпечаток cf — инкрементальное обновление '
                    'изменённых гранул')
        records.append({'src': src, 'sha': sha, 'status': status,
                        'db': db, 'found': found, 'note': note,
                        'ext': ext, 'name': meta.get('root_name'),
                        'root_type': meta.get('root_type'),
                        'db_version': db_version, 'cur_version': __version__,
                        'incremental': incremental})

    # Выгрузки в файлы: каталоги с ConfigDumpInfo.xml среди детей каталога
    # (и сам каталог, если он выгрузка). Признак изменённости — не SHA-256
    # всего дерева (дорого), а состояние состава dump_file: stat-обход +
    # хэши только изменённых файлов, как в confdb update. База ищется по
    # source.file (путь выгрузки при сборке); 'outdated' для выгрузки означает
    # ИНКРЕМЕНТАЛЬНОЕ обновление, а не замену базы.
    import sqlite3

    from .dump import model as dump_model
    from .dump.update import scan_changes
    dirs = dump_dirs(cf_dir)
    if is_dump_dir(cf_dir):
        dirs.append(os.path.abspath(cf_dir))
    for i, src in enumerate(dirs, 1):
        log(f'Выгрузка {i}/{len(dirs)}: {os.path.basename(src)}')
        same_path = sorted(p for p, inf in pool.items()
                           if os.path.abspath(inf.get('file') or '') == src)
        default_db = out_defaults(src)[0]
        note = ''
        if same_path:
            found = default_db if default_db in same_path else same_path[0]
            status, db = 'current', found
            db_version = pool[found].get('extractor_version')
            if not _is_dump_db(found):
                status = 'outdated'
                note = 'база собрана не из выгрузки — будет полная сборка'
            elif _version_schema(db_version) != _version_schema(__version__):
                status = 'version'
                note = (f'собрана распаковщиком {db_version or "неизвестной версии"}, '
                        f'актуальный {__version__} — другая ревизия схемы, пересобрать')
            else:
                try:
                    _objects, granule_files, _warnings = dump_model.scan(src)
                    uri = ('file:' + os.path.abspath(found).replace(os.sep, '/')
                           + '?mode=ro')
                    conn = sqlite3.connect(uri, uri=True)
                    try:
                        dirty, _current, _stored = scan_changes(
                            conn, src, granule_files)
                    finally:
                        conn.close()
                except (sqlite3.Error, OSError, ValueError) as err:
                    status = 'outdated'
                    note = f'состояние выгрузки не читается ({err}) — обновление'
                else:
                    if dirty:
                        status = 'outdated'
                        note = (f'изменённых гранул: {len(dirty)} — '
                                f'инкрементальное обновление')
        else:
            status, db, found = 'new', default_db, None
            db_version = None
            if os.path.isfile(default_db):
                note = 'путь по умолчанию занят другой базой — будет заменён'
        meta = pool.get(found) or {}
        records.append({'src': src, 'sha': None, 'status': status,
                        'db': db, 'found': found, 'note': note,
                        'ext': 'dump', 'name': meta.get('root_name'),
                        'root_type': meta.get('root_type'),
                        'db_version': db_version, 'cur_version': __version__,
                        'incremental': bool(status == 'outdated'
                                            and 'гранул' in note)})
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


class RepoWatcher:
    """Фоновое слежение за git-источниками (общее для TUI и веб).

    Поток периодически синхронизирует источники с включённым флагом watch
    (repos.watch_loop): клонирует/подтягивает коммиты и обновляет базы.
    Каждый цикл перечитывает реестр и настройки из конфига — новые источники
    и правки опций подхватываются без перезапуска. Журнал — кольцевой буфер
    строк для панели интерфейса.
    """

    LOG_LIMIT = 200

    def __init__(self):
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._busy_fn = None
        self.interval = 300
        self.log = []

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def _log(self, line):
        with self._lock:
            self.log.append(line)
            if len(self.log) > self.LOG_LIMIT:
                del self.log[:len(self.log) - self.LOG_LIMIT]

    def start(self, interval=300, busy_fn=None):
        """Запускает слежение; busy_fn — «занята ли тяжёлая задача» (веб)."""
        from . import repos
        if self.running:
            raise ValueError('слежение за git-репозиториями уже запущено')
        self._stop.clear()
        self._busy_fn = busy_fn
        self.interval = max(10, int(interval))
        self._log(f'Слежение запущено: интервал {self.interval} с, '
                  'источники с флагом watch.')

        def sources_fn():
            return [r for r in repos.load_sources() if r.get('watch')]

        def sync_fn(source_id):
            # свежий UiCore каждый цикл: настройки/реестр могли изменить
            core = UiCore()
            results = core.repos_sync(ids=[source_id])
            return results[0] if results else {}

        def run():
            try:
                repos.watch_loop(sources_fn, sync_fn, interval=self.interval,
                                 log=self._log, stop_event=self._stop,
                                 busy_fn=self._busy_fn)
            finally:
                self._log('Слежение остановлено.')

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self):
        if not self.running:
            return
        self._stop.set()
        self._thread.join(timeout=10)
        self._thread = None

    def status(self):
        from . import repos
        with self._lock:
            log = list(self.log[-60:])
        try:
            watched = sum(1 for r in repos.load_sources() if r.get('watch'))
        except Exception:  # noqa: BLE001 — статус не должен ронять интерфейс
            watched = 0
        return {'running': self.running, 'interval': self.interval,
                'watched': watched, 'log': log}


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
        self.repo_watcher = RepoWatcher()
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
        from . import repos
        paths = db_candidates(self.db_dirs)
        # базы git-источников видны интерфейсу наравне с остальными
        paths += [r['db'] for r in repos.load_sources()
                  if r.get('db') and os.path.isfile(r['db'])]
        return sorted(set(paths))

    def db_pool(self):
        recent = [r for r in ([self.last_db] + self.recent_db)
                  if r and os.path.isfile(r)]
        return list(dict.fromkeys(self.db_candidates() + recent))

    # ---------- сканирование каталога cf ----------

    def scan_cf(self, log=print):
        if not self.cf_dir or not os.path.isdir(self.cf_dir):
            raise ValueError('каталог cf не задан или не существует')
        files = cf_files(self.cf_dir)
        dumps = dump_dirs(self.cf_dir)
        if is_dump_dir(self.cf_dir):
            dumps.append(os.path.abspath(self.cf_dir))
        if not files and not dumps:
            return []
        pool = self.db_pool()
        for paths in self.groups.values():
            pool += paths
        pool += [out_defaults(src)[0] for src in files + dumps]
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
        if db and os.path.isfile(db) and not dump:
            # существующая база из cf — инкрементальное обновление (только
            # изменённые гранулы); update_cf сам сделает полную сборку, если
            # база не из cf или прежней ревизии схемы
            from .cf_update import update_cf
            stats = update_cf(
                src, db, workers=self.workers,
                rebuild_fts=None if self.build_fts else False,
                store_blobs=self.store_blobs, log=log,
                source_sha256=source_sha256)
        else:
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

    # ---------- git-источники баз (репозитории выгрузок/cf/epf/cfe) ----------
    # Реестр и git-операции — в repos.py; здесь оркестрация сборки/обновления
    # базами общего конвейера (extract / dump.build_db / dump.update_db).
    # Частичное обновление возможно только для выгрузок (dump_file хранит
    # состав); файлы 1С пересобираются полностью при изменении SHA-256.

    def repos_list(self):
        """Источники с признаками для интерфейса (клон/база существуют)."""
        from . import repos
        result = []
        for rec in repos.load_sources():
            item = dict(rec)
            item['clone_exists'] = os.path.isdir(
                os.path.join(repos.clone_dir(rec), '.git'))
            item['db_exists'] = os.path.isfile(rec.get('db') or '')
            result.append(item)
        return result

    def repos_add(self, url, source_id=None, branch='', path='', db=None,
                  mode='auto', watch=False):
        from . import repos
        return repos.add_source(url, source_id=source_id, branch=branch,
                                path=path, db=db, mode=mode, watch=watch)

    def repos_update(self, source_id, **fields):
        from . import repos
        return repos.update_source(source_id, **fields)

    def repos_remove(self, source_id, purge=False):
        from . import repos
        return repos.remove_source(source_id, purge=purge)

    def _guard_full_rebuild(self, db):
        """Полная пересборка невозможна, пока базу держит живой MCP-сервер."""
        if not os.path.isfile(db):
            return
        from .dump.update import read_server_pidfile
        server = read_server_pidfile(db)
        if server:
            raise ValueError(
                f'полная пересборка невозможна: базу держит MCP-сервер '
                f'(pid {server.get("pid")}). Остановите сервер; частичное '
                'обновление выгрузки работает и при запущенном сервере.')

    def repos_sync(self, ids=None, force=False, log=print, workers=None,
                   build_fts=None, store_blobs=None):
        """Синхронизация git-источников: клон/pull + сборка/обновление базы.

        ids — список имён источников (None/пусто — все). force — пересобрать
        базы, даже если git-коммит и файл не изменились. Стратегия: выгрузка —
        частичное обновление (режим full/force — полная сборка), файл 1С —
        полная пересборка при изменении (SHA-256). Другая ревизия схемы
        распаковщика тоже требует пересборки (правило 2026-11-13). Ошибка
        одного источника не прерывает остальные; состояние пишется в реестр.
        """
        from . import repos
        from .extract import source_info
        sources = repos.load_sources()
        records = ([repos.find_source(sources, i) for i in ids]
                   if ids else list(sources))
        workers = self.workers if workers is None else int(workers)
        build_fts = self.build_fts if build_fts is None else bool(build_fts)
        store_blobs = self.store_blobs if store_blobs is None else bool(store_blobs)
        options = extract_options({'store_blobs': store_blobs,
                                   'prefix': self.prefix,
                                   'skip_errors': self.skip_errors})
        results = []
        for rec in records:
            result = {'id': rec['id'], 'ok': False, 'action': '',
                      'changed': False, 'build': '', 'commit': '',
                      'summary': '', 'error': None}
            try:
                git = repos.pull(rec, log=log)
                result.update(action=git['action'], changed=git['changed'],
                              commit=git['commit'])
                kind, rel = repos.detect_source(repos.clone_dir(rec),
                                                rec.get('path'))
                rec['kind'], rec['src_path'] = kind, rel
                directory = repos.clone_dir(rec)
                src = os.path.join(directory, rel) if rel else directory
                db = rec['db']
                os.makedirs(os.path.dirname(os.path.abspath(db)), exist_ok=True)
                info = source_info(db)
                stale_schema = bool(info) and (
                    _version_schema(info.get('extractor_version'))
                    != _version_schema(__version__))
                if stale_schema:
                    log('База собрана другой ревизией схемы распаковщика — '
                        'нужна пересборка.')
                stats, build = {}, 'skip'
                if (not git['changed'] and not force and not stale_schema
                        and os.path.isfile(db)):
                    result['summary'] = 'изменений нет — база актуальна'
                    rec['last_result'] = 'no-change'
                elif kind == 'dump':
                    from .dump import build_db, update_db
                    if force or rec.get('mode') == 'full' or stale_schema:
                        self._guard_full_rebuild(db)
                        log('Полная сборка базы из выгрузки…')
                        stats = build_db(src, db, workers=workers,
                                         build_fts=build_fts,
                                         store_blobs=store_blobs, log=log)
                        build = 'full'
                    else:
                        # update_db сам решает: нет базы/не из выгрузки —
                        # полная сборка; иначе частичное обновление (WAL,
                        # работает при живом MCP-сервере)
                        stats = update_db(
                            src, db, workers=workers,
                            rebuild_fts=None if build_fts else False, log=log)
                        changed = stats.get('changed_granules')
                        if isinstance(changed, list):
                            build = 'partial' if changed else 'skip'
                        else:
                            build = 'full'
                    if build == 'full':
                        result['summary'] = (
                            f'полная сборка: {_count(stats.get("objects"))} объектов')
                    elif build == 'partial':
                        result['summary'] = (
                            'частичное обновление: '
                            f'{len(stats["changed_granules"])} гранул')
                    else:
                        result['summary'] = 'изменений в выгрузке нет'
                    rec['last_result'] = {'full': 'built', 'partial': 'updated',
                                          'skip': 'skipped'}[build]
                else:
                    sha, skip = check_same_source(
                        src, db, force=force or stale_schema, log=log)
                    if skip:
                        result['summary'] = 'файл не изменился (SHA-256 совпал)'
                        rec['last_result'] = 'skipped'
                    elif (os.path.isfile(db) and not force and not stale_schema
                          and rec.get('mode') != 'full'):
                        # cf-инкремент: только изменённые гранулы декодированного
                        # дерева; живой MCP-сервер не останавливается
                        from .cf_update import update_cf
                        stats = update_cf(
                            src, db, workers=workers,
                            rebuild_fts=None if build_fts else False,
                            store_blobs=store_blobs, log=log,
                            source_sha256=sha)
                        changed = stats.get('changed_granules') or []
                        build = 'partial' if changed else 'skip'
                        result['summary'] = (
                            f'инкрементальное обновление: {len(changed)} гранул'
                            if changed else 'изменений в cf нет')
                        rec['last_result'] = 'updated' if changed else 'skipped'
                    else:
                        self._guard_full_rebuild(db)
                        stats = _extract_cf(
                            src, db_path=db, temp_dir=self.temp or None,
                            keep_temp=self.keep_temp, options=options,
                            workers=workers, build_fts=build_fts,
                            source_sha256=sha)
                        build = 'full'
                        result['summary'] = (
                            f'полная пересборка за {stats.get("elapsed")}')
                        rec['last_result'] = 'built'
                if git['action'] == 'clone':
                    result['summary'] = 'клонирован; ' + result['summary']
                elif git['action'] == 'pull':
                    result['summary'] = (f'коммит {git["commit"][:8]}; '
                                         + result['summary'])
                rec['last_sync'] = datetime.now().isoformat(timespec='seconds')
                rec['last_error'] = ''
                result.update(ok=True, build=build, stats=stats)
                if build in ('full', 'partial'):
                    self._remember_extract(src, db, stats)
            except Exception as err:  # noqa: BLE001 — один источник не роняет пакет
                rec['last_result'] = 'error'
                rec['last_error'] = str(err)
                result.update(error=str(err), summary=f'ошибка: {err}')
                log(f'Источник {rec["id"]}: ошибка — {err}')
            # только своя запись и со свежим реестром: добавленные/удалённые
            # во время задачи источники не затираются старым снимком списка
            repos.save_source(rec)
            results.append(result)
        return results

    def repos_auth(self, source_id=None, url=None, log=print):
        """Явная авторизация в git-хостинге (браузер/окно входа GCM).

        url — напрямую, иначе URL источника source_id. После входа сохранённые
        учётные данные использует обычная (неинтерактивная) синхронизация.
        """
        from . import repos
        if url is None:
            url = repos.find_source(repos.load_sources(), source_id)['url']
        repos.authorize(url, log=log)
        return {'ok': True, 'url': url}

    def repos_check(self, ids=None):
        """Проверка изменений в удалённых ветках без обновления зеркал/баз."""
        from . import repos
        sources = repos.load_sources()
        records = ([repos.find_source(sources, i) for i in ids]
                   if ids else list(sources))
        results = []
        for rec in records:
            item = {'id': rec['id'], 'ok': False}
            try:
                item.update(repos.remote_check(rec))
                item['ok'] = True
            except Exception as err:  # noqa: BLE001
                item['error'] = str(err)
            results.append(item)
        return results

    def repos_watch_start(self, interval=300, busy_fn=None):
        self.repo_watcher.start(interval=interval, busy_fn=busy_fn)
        return self.repo_watcher.status()

    def repos_watch_stop(self):
        self.repo_watcher.stop()
        return self.repo_watcher.status()

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
                'repos': self.repos_list(),
                'repo_watch': self.repo_watcher.status(),
                'server': self.server_status()}
