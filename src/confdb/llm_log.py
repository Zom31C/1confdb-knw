"""Журнал запросов LLM в отдельной базе SQLite (анализ поведения агента).

MCP-сервер записывает сюда каждый вызов инструмента (tools/call): время,
сессию, инструмент, аргументы, ПОЛНЫЙ текст ответа, признак ошибки,
длительность, базу и группу. В отличие от кольцевого буфера консоли лога
(mcp_server.McpServer.llm_log) журнал переживает остановку сервера и
пересборку баз знаний — это отдельный файл, по умолчанию ~/.confdb/llm_log.db.

Запись включается в интерфейсах (TUI и веб): ключи options.llm_log_enabled
и options.llm_log_db в ~/.confdb/config.json. Просмотр (list_calls/get_call)
работает и при остановленном сервере — отдельным соединением.

База журнала НЕ является базой знаний confdb: своя схема (ревизия 1),
таблица llm_call. Журнал не должен мешать серверу: ошибки записи
перехватываются на стороне mcp_server и не роняют ответ клиенту.
"""
import datetime
import json
import os
import sqlite3
import sys
import uuid

from .config import load_config, save_config

# база журнала по умолчанию — рядом с config.json
DEFAULT_PATH = os.path.join(os.path.expanduser('~'), '.confdb', 'llm_log.db')

SCHEMA_VERSION = 1  # ревизия схемы ЖУРНАЛА (не базы знаний)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_call(
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    session TEXT NOT NULL,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    response TEXT NOT NULL,
    is_error INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    db TEXT,
    grp TEXT
);
CREATE INDEX IF NOT EXISTS llm_call_ts ON llm_call(ts);
CREATE INDEX IF NOT EXISTS llm_call_tool ON llm_call(tool);
"""

# пределы предпросмотра в списке (полные тексты отдаёт get_call)
ARGS_PREVIEW = 400
RESP_PREVIEW = 200


def settings(config=None):
    """Настройки журнала из конфига: {'enabled': bool, 'path': str}."""
    if config is None:
        config = load_config()
    opts = config.get('options') or {}
    return {'enabled': bool(opts.get('llm_log_enabled')),
            'path': str(opts.get('llm_log_db') or DEFAULT_PATH)}


def save_settings(enabled, path=None):
    """Включает/выключает запись журнала в конфиге (слияние, не затирание)."""
    config = load_config()
    opts = config.setdefault('options', {})
    opts['llm_log_enabled'] = bool(enabled)
    if path:
        opts['llm_log_db'] = str(path)
    save_config(config)
    return settings(config)


def _ensure_schema(conn):
    conn.executescript(_SCHEMA)
    conn.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
    conn.commit()


class LlmLogWriter:
    """Пишет вызовы LLM в базу журнала (одно соединение на сервер).

    WAL + synchronous=NORMAL: журнал пишется часто, ответы не должны ждать
    дисковую синхронизацию; читатели (интерфейсы) видят записи сразу.
    auto_vacuum=INCREMENTAL задаётся при создании — очистка журнала
    возвращает место файлу без полной пересборки (PRAGMA incremental_vacuum).
    """

    def __init__(self, path):
        self.path = os.path.abspath(path)
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        fresh = not os.path.isfile(self.path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute('PRAGMA busy_timeout=5000')
        if fresh:
            # auto_vacuum действует только на пустой базе — до создания таблиц
            self.conn.execute('PRAGMA auto_vacuum=INCREMENTAL')
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=NORMAL')
        _ensure_schema(self.conn)
        # сессия = один запуск записи (включили/выключили — новая сессия)
        self.session = uuid.uuid4().hex[:12]

    def append(self, tool, args, response, is_error=False, duration_ms=None,
               db=None, grp=None):
        """Записывает один вызов; полный текст ответа без обрезки."""
        try:
            argstr = json.dumps(args, ensure_ascii=False)
        except (TypeError, ValueError):
            argstr = str(args)
        body = response if isinstance(response, str) else str(response)
        ts = datetime.datetime.now().isoformat(sep=' ', timespec='milliseconds')
        self.conn.execute(
            'INSERT INTO llm_call(ts, session, tool, args, response, is_error,'
            ' duration_ms, db, grp) VALUES (?,?,?,?,?,?,?,?,?)',
            (ts, self.session, str(tool), argstr, body, int(bool(is_error)),
             duration_ms, db, grp))
        self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except sqlite3.Error:
            pass


def configure_server(server, config=None, strict=False):
    """Применяет настройки журнала к серверу (McpServer.configure_llm_log).

    strict=True — ошибки открытия базы поднимаются (переключатель в UI);
    иначе предупреждение в stderr (автоприменение при старте сервера).
    """
    opts = settings(config)
    try:
        server.configure_llm_log(opts['enabled'], opts['path'])
    except (OSError, sqlite3.Error, ValueError) as err:
        if strict:
            raise ValueError(f'журнал LLM: {err}')
        print(f'Журнал LLM: не открыть базу журнала: {err}', file=sys.stderr)


def _connect(path):
    """Соединение для чтения/очистки (обычное, не ro: WAL-журнал сервера)."""
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def stats(path):
    """Сводка журнала: число записей/ошибок, размер файла, инструменты."""
    result = {'exists': False, 'count': 0, 'errors': 0, 'size_bytes': 0,
              'tools': []}
    if not os.path.isfile(path):
        return result
    result['exists'] = True
    result['size_bytes'] = os.path.getsize(path)
    wal = path + '-wal'
    if os.path.isfile(wal):
        result['size_bytes'] += os.path.getsize(wal)
    conn = _connect(path)
    try:
        result['count'] = conn.execute(
            'SELECT COUNT(*) FROM llm_call').fetchone()[0]
        result['errors'] = conn.execute(
            'SELECT COUNT(*) FROM llm_call WHERE is_error=1').fetchone()[0]
        result['tools'] = [r[0] for r in conn.execute(
            'SELECT DISTINCT tool FROM llm_call ORDER BY tool')]
    finally:
        conn.close()
    return result


def list_calls(path, limit=50, offset=0, tool=None, errors_only=False,
               search=None):
    """Страница журнала (новые сверху) + общее число под фильтром.

    В строках — предпросмотр аргументов и ответа; полные тексты у get_call.
    Фильтры: инструмент (точно), только ошибки, текст (tool/args/response).
    """
    result = {'total': 0, 'rows': [], 'tools': []}
    if not os.path.isfile(path):
        return result
    where, params = [], []
    if tool:
        where.append('tool = ?')
        params.append(tool)
    if errors_only:
        where.append('is_error = 1')
    if search:
        where.append('(tool LIKE ? OR args LIKE ? OR response LIKE ?)')
        params += [f'%{search}%'] * 3
    cond = (' WHERE ' + ' AND '.join(where)) if where else ''
    conn = _connect(path)
    try:
        result['total'] = conn.execute(
            'SELECT COUNT(*) FROM llm_call' + cond, params).fetchone()[0]
        result['tools'] = [r[0] for r in conn.execute(
            'SELECT DISTINCT tool FROM llm_call ORDER BY tool')]
        rows = conn.execute(
            'SELECT id, ts, session, tool, args, is_error, duration_ms, db,'
            ' grp, LENGTH(response) AS resp_len, SUBSTR(response, 1, ?) AS prev'
            ' FROM llm_call' + cond + ' ORDER BY id DESC LIMIT ? OFFSET ?',
            params + [RESP_PREVIEW, int(limit), int(offset)]).fetchall()
        for row in rows:
            args = row['args']
            result['rows'].append({
                'id': row['id'], 'ts': row['ts'], 'session': row['session'],
                'tool': row['tool'],
                'args': args if len(args) <= ARGS_PREVIEW
                else args[:ARGS_PREVIEW] + '…',
                'is_error': bool(row['is_error']),
                'duration_ms': row['duration_ms'],
                'db': row['db'], 'grp': row['grp'],
                'resp_len': row['resp_len'],
                'preview': row['prev'],
            })
    finally:
        conn.close()
    return result


def get_call(path, call_id):
    """Полная запись вызова (аргументы и ответ без обрезки)."""
    if not os.path.isfile(path):
        raise ValueError(f'запись не найдена: {call_id}')
    conn = _connect(path)
    try:
        row = conn.execute(
            'SELECT * FROM llm_call WHERE id=?', (int(call_id),)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError(f'запись не найдена: {call_id}')
    return {'id': row['id'], 'ts': row['ts'], 'session': row['session'],
            'tool': row['tool'], 'args': row['args'],
            'response': row['response'], 'is_error': bool(row['is_error']),
            'duration_ms': row['duration_ms'], 'db': row['db'],
            'grp': row['grp']}


def clear(path):
    """Удаляет все записи журнала; возвращает число удалённых."""
    if not os.path.isfile(path):
        return 0
    conn = _connect(path)
    try:
        count = conn.execute('SELECT COUNT(*) FROM llm_call').fetchone()[0]
        conn.execute('DELETE FROM llm_call')
        conn.commit()
        try:
            conn.execute('PRAGMA incremental_vacuum')
        except sqlite3.Error:
            pass  # место вернётся позже — записи уже удалены
    finally:
        conn.close()
    return count
