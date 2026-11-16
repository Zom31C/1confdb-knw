"""Git-источники баз знаний: репозитории выгрузок, .cf, .epf, .cfe → SQLite.

Реестр источников хранится в ~/.confdb/config.json (ключ `git_sources`).
Каждый источник — git-репозиторий, внутри которого лежит либо каталог
выгрузки конфигурации в файлы (Designer dump, признак ConfigDumpInfo.xml),
либо файл .cf/.cfe/.epf/.erf. Репозиторий клонируется в управляемое зеркало
~/.confdb/repos/<id> (shallow, depth 1): история не нужна — состав выгрузки
фиксирует сама база (dump_file), а файл 1С сравнивается по SHA-256.

Синхронизация (`pull`): clone при первом запуске, иначе fetch + reset --hard
до коммита удалённой ветки. Зеркало управляемое — локальные правки в нём не
предполагаются (reset + clean отбрасывают их). Сборку/обновление базы делает
общий слой ui_core (UiCore.repos_sync): выгрузка обновляется частично
(dump.update_db, работает при живом MCP-сервере), файлы 1С пересобираются
полностью при изменении. Здесь — только git, реестр и определение источника.

Git вызывается через subprocess (стандартная библиотека); нужен установленный
Git в PATH. GIT_TERMINAL_PROMPT=0 — запросы пароля в консоли не подвешивают
фоновое слежение (для приватных репозиториев используйте SSH или URL с токеном).
"""
import os
import re
import shutil
import subprocess
import time
from datetime import datetime

from .config import load_config, save_config

# файлы 1С, которые умеет извлекать конвейер (внешние отчёты .erf тоже)
CF_EXTS = ('.cf', '.cfe', '.epf', '.erf')
# стратегии обновления: auto — выгрузка частично, файл полностью;
# partial — выгрузка только частично (для файла = auto); full — всегда пересборка
MODES = ('auto', 'partial', 'full')
# глубина клона: история не нужна, только текущий срез
CLONE_DEPTH = '1'


class RepoError(ValueError):
    """Ошибка git-источника (сообщение для пользователя, по-русски)."""


def clone_root():
    """Каталог управляемых клонов (~/.confdb/repos)."""
    return os.path.join(os.path.expanduser('~'), '.confdb', 'repos')


def db_root():
    """Каталог баз git-источников по умолчанию (~/.confdb/dbs)."""
    return os.path.join(os.path.expanduser('~'), '.confdb', 'dbs')


def clone_dir(rec):
    """Каталог зеркала источника."""
    return os.path.join(clone_root(), rec['id'])


# ---------- git ----------

def _git(args, cwd=None, timeout=120):
    """Вызов git с перехватом вывода; ошибка — RepoError с текстом git.

    core.longpaths=true — выгрузки 1С дают пути длиннее 260 символов
    (Windows); GIT_TERMINAL_PROMPT=0 — без интерактивных запросов пароля.
    """
    env = dict(os.environ)
    env['GIT_TERMINAL_PROMPT'] = '0'
    cmd = ['git', '--no-pager', '-c', 'core.longpaths=true'] + list(args)
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout,
                              capture_output=True, text=True,
                              encoding='utf-8', errors='replace')
    except FileNotFoundError:
        raise RepoError('git не найден — установите Git и добавьте его в PATH')
    except subprocess.TimeoutExpired:
        raise RepoError(f'git не ответил за {timeout} с: {" ".join(args[:3])}…')
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or '').strip()
        raise RepoError(f'git {" ".join(args[:3])}: {detail or "код " + str(proc.returncode)}')
    return (proc.stdout or '').strip()


def git_available():
    """Версия git строкой либо None (git не установлен)."""
    try:
        return _git(['--version'], timeout=15)
    except RepoError:
        return None


def _current_branch(directory):
    return _git(['rev-parse', '--abbrev-ref', 'HEAD'], cwd=directory)


def _head_commit(directory):
    return _git(['rev-parse', 'HEAD'], cwd=directory)


def _commit_date(directory):
    try:
        return _git(['log', '-1', '--format=%cI'], cwd=directory)
    except RepoError:
        return ''


def pull(rec, log=print):
    """Клонирует зеркало источника или подтягивает изменения удалённой ветки.

    Возвращает {'action': 'clone'|'pull'|'none', 'changed': bool,
    'commit', 'commit_date', 'branch'} и обновляет поля записи (branch,
    commit, commit_date) — сохранение реестра делает вызывающая сторона.
    Зеркало управляемое: reset --hard + clean -fd отбрасывают локальные правки.
    """
    directory = clone_dir(rec)
    url = rec['url']
    branch = (rec.get('branch') or '').strip()
    if not os.path.isdir(os.path.join(directory, '.git')):
        if os.path.isdir(directory):
            # прерванный клон без .git — начинаем заново
            shutil.rmtree(directory, ignore_errors=True)
        os.makedirs(clone_root(), exist_ok=True)
        log(f'Клонирую {url} (ветка {branch or "по умолчанию"}, depth {CLONE_DEPTH})…')
        args = ['clone', '--depth', CLONE_DEPTH, '--single-branch', '--no-tags']
        if branch:
            args += ['--branch', branch]
        args += ['--', url, directory]
        _git(args, timeout=3600)
        if not branch:
            branch = _current_branch(directory)
        rec['branch'] = branch
        rec['commit'] = _head_commit(directory)
        rec['commit_date'] = _commit_date(directory)
        return {'action': 'clone', 'changed': True, 'commit': rec['commit'],
                'commit_date': rec['commit_date'], 'branch': branch}
    if not branch:
        branch = _current_branch(directory)
        rec['branch'] = branch
    log(f'Проверяю обновления {url} (ветка {branch})…')
    _git(['fetch', '--depth', CLONE_DEPTH, '--no-tags', 'origin', branch],
         cwd=directory, timeout=600)
    remote = _git(['rev-parse', 'FETCH_HEAD'], cwd=directory)
    local = _head_commit(directory)
    if remote == local:
        rec['commit'] = local
        return {'action': 'none', 'changed': False, 'commit': local,
                'commit_date': rec.get('commit_date') or '', 'branch': branch}
    log(f'Новый коммит {remote[:8]} (был {local[:8]}) — обновляю зеркало.')
    _git(['reset', '--hard', 'FETCH_HEAD'], cwd=directory)
    _git(['clean', '-fd'], cwd=directory)
    rec['commit'] = remote
    rec['commit_date'] = _commit_date(directory)
    return {'action': 'pull', 'changed': True, 'commit': remote,
            'commit_date': rec['commit_date'], 'branch': branch}


def remote_check(rec):
    """Есть ли изменения в удалённой ветке — без обновления зеркала и базы.

    {'cloned': bool, 'changed': bool, 'local', 'remote'}; неклонированный
    источник считается изменённым (нужен первый клон).
    """
    directory = clone_dir(rec)
    if not os.path.isdir(os.path.join(directory, '.git')):
        return {'cloned': False, 'changed': True, 'local': '', 'remote': ''}
    branch = (rec.get('branch') or '').strip() or _current_branch(directory)
    _git(['fetch', '--depth', CLONE_DEPTH, '--no-tags', 'origin', branch],
         cwd=directory, timeout=600)
    remote = _git(['rev-parse', 'FETCH_HEAD'], cwd=directory)
    local = _head_commit(directory)
    return {'cloned': True, 'changed': remote != local,
            'local': local, 'remote': remote}


# ---------- реестр источников (config.json) ----------

def load_sources():
    """Список записей git_sources из конфига (всегда список словарей)."""
    raw = load_config().get('git_sources')
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict) and r.get('id') and r.get('url')]


def save_sources(sources):
    config = load_config()
    config['git_sources'] = sources
    save_config(config)


def find_source(sources, source_id):
    for rec in sources:
        if rec['id'] == source_id:
            return rec
    raise RepoError(f'источник «{source_id}» не найден (confdb repos list)')


def make_id(url):
    """Короткое имя источника из URL: basename без .git, безопасные символы."""
    name = url.rstrip('/').split('/')[-1]
    if name.lower().endswith('.git'):
        name = name[:-4]
    name = re.sub(r'[^\w\-]+', '-', name, flags=re.UNICODE).strip('-').lower()
    return name[:48] or 'repo'


def add_source(url, source_id=None, branch='', path='', db=None,
               mode='auto', watch=False):
    """Регистрирует источник (без клонирования — его делает sync)."""
    url = (url or '').strip()
    if not url:
        raise RepoError('укажите URL git-репозитория (https://… или git@…)')
    if mode not in MODES:
        raise RepoError(f'режим обновления должен быть одним из: {", ".join(MODES)}')
    sources = load_sources()
    source_id = (source_id or '').strip() or make_id(url)
    if not re.fullmatch(r'[\w\-]{1,64}', source_id, flags=re.UNICODE):
        raise RepoError('имя источника: буквы/цифры/дефис/подчёркивание, до 64 символов')
    if any(r['id'] == source_id for r in sources):
        raise RepoError(f'источник «{source_id}» уже есть — выберите другое имя')
    if any(r['url'] == url and (r.get('path') or '') == (path or '')
           and (r.get('branch') or '') == (branch or '') for r in sources):
        raise RepoError('такой репозиторий (URL + путь + ветка) уже зарегистрирован')
    db = os.path.abspath(db) if db else os.path.join(db_root(), source_id + '.sqlite')
    rec = {'id': source_id, 'url': url, 'branch': (branch or '').strip(),
           'path': (path or '').strip().replace('\\', '/'),
           'db': db, 'mode': mode, 'watch': bool(watch),
           'added': datetime.now().isoformat(timespec='seconds'),
           'commit': '', 'commit_date': '', 'kind': '', 'src_path': '',
           'last_sync': '', 'last_result': '', 'last_error': ''}
    sources.append(rec)
    save_sources(sources)
    return rec


def update_source(source_id, **fields):
    """Правит поля источника: branch, path, db, mode, watch (URL неизменен)."""
    sources = load_sources()
    rec = find_source(sources, source_id)
    allowed = ('branch', 'path', 'db', 'mode', 'watch')
    for key, value in fields.items():
        if key not in allowed:
            raise RepoError(f'поле «{key}» изменить нельзя (URL — только пересозданием источника)')
        if key == 'mode' and value not in MODES:
            raise RepoError(f'режим обновления должен быть одним из: {", ".join(MODES)}')
        if key == 'watch':
            rec[key] = bool(value)
        elif key == 'db':
            rec[key] = os.path.abspath(value) if value else os.path.join(
                db_root(), source_id + '.sqlite')
        elif key == 'path':
            rec[key] = str(value or '').strip().replace('\\', '/')
        else:
            rec[key] = str(value or '').strip()
    save_sources(sources)
    return rec


def remove_source(source_id, purge=False):
    """Убирает источник из реестра; purge — удаляет и каталог клона (база остаётся)."""
    sources = load_sources()
    rec = find_source(sources, source_id)
    sources.remove(rec)
    save_sources(sources)
    if purge:
        directory = clone_dir(rec)
        if os.path.isdir(directory):
            from .extract import remove_tree
            remove_tree(directory)
    return rec


# ---------- определение источника внутри репозитория ----------

def _is_dump(directory):
    return os.path.isfile(os.path.join(directory, 'ConfigDumpInfo.xml'))


def _cf_ext(name):
    ext = os.path.splitext(name)[1].lower()
    return ext if ext in CF_EXTS else ''


def detect_source(directory, path_hint=''):
    """Что собирать из зеркала: ('dump'|'cf'|'cfe'|'epf'|'erf', отн. путь).

    Явный путь (path_hint) проверяется строго. Автоопределение: корень —
    выгрузка или единственный файл 1С; иначе один уровень подкаталогов.
    Несколько кандидатов — ошибка со списком (нужен явный путь), чтобы не
    угадывать, какую конфигурацию собирать.
    """
    directory = os.path.abspath(directory)
    hint = (path_hint or '').strip().replace('\\', '/').strip('/')
    if hint:
        target = os.path.join(directory, hint)
        if _is_dump(target):
            return 'dump', hint
        if os.path.isfile(target) and _cf_ext(target):
            return _cf_ext(target)[1:], hint
        raise RepoError(
            f'путь «{hint}» в репозитории — не каталог выгрузки '
            f'(ConfigDumpInfo.xml) и не файл .cf/.cfe/.epf/.erf')
    if _is_dump(directory):
        return 'dump', ''
    candidates = []
    for name in sorted(os.listdir(directory)):
        full = os.path.join(directory, name)
        if os.path.isfile(full) and _cf_ext(name):
            candidates.append((_cf_ext(name)[1:], name))
        elif os.path.isdir(full) and name != '.git':
            if _is_dump(full):
                candidates.append(('dump', name))
            else:
                for sub in sorted(os.listdir(full)):
                    subfull = os.path.join(full, sub)
                    if os.path.isfile(subfull) and _cf_ext(sub):
                        candidates.append((_cf_ext(sub)[1:], name + '/' + sub))
                    elif os.path.isdir(subfull) and _is_dump(subfull):
                        candidates.append(('dump', name + '/' + sub))
    if not candidates:
        raise RepoError(
            'в репозитории не найдено выгрузки (ConfigDumpInfo.xml) или файла '
            '.cf/.cfe/.epf/.erf — укажите путь внутри репозитория')
    if len(candidates) > 1:
        listing = ', '.join(f'{kind}:{path}' for kind, path in candidates[:8])
        raise RepoError(
            f'в репозитории несколько кандидатов ({listing}) — '
            f'укажите путь внутри репозитория явно')
    return candidates[0]


# ---------- слежение за изменениями ----------

def watch_loop(sources_fn, sync_fn, interval=300, once=False, log=print,
               stop_event=None, busy_fn=None):
    """Цикл слежения: периодически синхронизирует источники из sources_fn().

    :param sources_fn: callable без аргументов — записи для проверки (реестр
        перечитывается каждый цикл, новые/удалённые источники подхватываются)
    :param sync_fn: callable(id) — синхронизация одного источника; ошибка
        одного источника не прерывает цикл
    :param interval: пауза между циклами, с
    :param once: один проход и выход (для планировщика/cron)
    :param stop_event: threading.Event — мягкая остановка
    :param busy_fn: callable() -> bool — занята ли тяжёлая задача (веб):
        пока занята, цикл пропускается, чтобы не столкнуться с извлечением
    """
    interval = max(10, int(interval))
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        if busy_fn is not None and busy_fn():
            log('Слежение: пропуск цикла — выполняется другая задача.')
        else:
            try:
                records = list(sources_fn())
            except Exception as err:  # noqa: BLE001 — реестр мог стать недоступен
                log(f'Слежение: не удалось прочитать реестр: {err}')
                records = []
            if not records:
                log('Слежение: нет источников с включённым флагом watch.')
            for rec in records:
                if stop_event is not None and stop_event.is_set():
                    return
                stamp = datetime.now().strftime('%H:%M:%S')
                try:
                    result = sync_fn(rec['id'])
                    log(f'[{stamp}] {rec["id"]}: {result.get("summary") or "готово"}')
                except Exception as err:  # noqa: BLE001 — один источник не роняет цикл
                    log(f'[{stamp}] {rec["id"]}: ошибка — {err}')
        if once:
            return
        if stop_event is not None:
            if stop_event.wait(interval):
                return
        else:
            time.sleep(interval)