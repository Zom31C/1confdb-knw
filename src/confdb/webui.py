"""Веб-интерфейс confdb (только стандартная библиотека).

Повторяет функции консольного интерфейса (TUI) в браузере: запуск и управление
MCP-сервером, извлечение конфигураций (.cf/.cfe/.epf и выгрузка), сканирование
каталога cf по SHA-256 (новые/устаревшие), группы баз, настройки, FTS, бенчмарк.

Вся бизнес-логика — в общем слое ui_core.UiCore (том же, что использует TUI),
поэтому доработка логики достаётся обоим интерфейсам. Здесь только HTTP-транспорт,
JSON-API и раздача одностраничного приложения (webui.html).

Запуск: confdb-web.bat (или python -m confdb.webui, или confdb web).
Сервер слушает 127.0.0.1 (локально); браузер открывается автоматически.
"""
import argparse
import contextlib
import json
import os
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .ui_core import UiCore, resolve_launch_groups

HTML_PATH = os.path.join(os.path.dirname(__file__), 'webui.html')
LOG_LIMIT = 600          # строк журнала задачи держим в памяти (остальное отбрасываем)


class _TaskLog:
    """Перехват stdout задачи: строки в журнал + эхо в консоль сервера."""

    def __init__(self, task, echo):
        self.task = task
        self.echo = echo
        self._buf = ''

    def write(self, text):
        if self.echo is not None:
            self.echo.write(text)
        self._buf += text
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            if line.strip():
                self.task['log'].append(line)
                if len(self.task['log']) > LOG_LIMIT:
                    del self.task['log'][:len(self.task['log']) - LOG_LIMIT]

    def flush(self):
        if self.echo is not None:
            self.echo.flush()


class TaskManager:
    """Одна тяжёлая задача за раз (извлечение/скан/FTS/бенчмарк) в фоне.

    Прогресс — журнал строк (перехват stdout), статус running/done/error.
    Веб-интерфейс опрашивает GET /api/task и показывает журнал вживую.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.current = None

    def busy(self):
        return self.current is not None and self.current['status'] == 'running'

    def start(self, kind, title, fn):
        with self._lock:
            if self.busy():
                raise ValueError(f'уже выполняется задача: {self.current["title"]}')
            task = {'id': uuid.uuid4().hex[:8], 'kind': kind, 'title': title,
                    'status': 'running', 'log': [], 'result': None,
                    'error': None, 'started': time.time(), 'finished': None}
            self.current = task

        def run():
            log = _TaskLog(task, sys.__stdout__)
            try:
                with contextlib.redirect_stdout(log):
                    task['result'] = fn()
                task['status'] = 'done'
            except Exception as err:  # noqa: BLE001 — ошибка задачи не роняет сервер
                task['error'] = str(err)
                task['status'] = 'error'
            finally:
                task['finished'] = time.time()

        threading.Thread(target=run, daemon=True).start()
        return task

    def snapshot(self):
        if self.current is None:
            return None
        return dict(self.current)


class WebUi:
    """Связывает UiCore (логика) и TaskManager (фон) с HTTP-обработчиком."""

    def __init__(self):
        self.core = UiCore()
        self.tasks = TaskManager()

    # ---------- задачи ----------

    def scan(self):
        return self.tasks.start('scan', 'Сканирование каталога cf',
                                self.core.scan_cf)

    def extract(self, src, db=None, dump=None):
        title = f'Извлечение {os.path.basename(src)}'
        return self.tasks.start('extract', title,
                                lambda: self.core.extract(src, db, dump))

    def extract_batch(self, items):
        def run():
            done, failed = [], []
            for it in items:
                try:
                    stats = self.core.extract(it['src'], it.get('db'),
                                              source_sha256=it.get('sha'))
                    done.append({'src': it['src'], 'db': stats.get('db'),
                                 'skipped': bool(stats.get('skipped')),
                                 'elapsed': stats.get('elapsed')})
                except Exception as err:  # noqa: BLE001 — одна ошибка не роняет пакет
                    failed.append({'src': it['src'], 'error': str(err)})
            return {'done': done, 'failed': failed}
        return self.tasks.start('batch', f'Пакетное извлечение ({len(items)})', run)

    def fts(self, db):
        return self.tasks.start('fts', f'FTS-индекс {os.path.basename(db)}',
                                lambda: self.core.build_fts(db))

    def bench(self, src):
        return self.tasks.start('bench', f'Бенчмарк {os.path.basename(src)}',
                                lambda: self.core.bench(src))

    # ---------- MCP-сервер ----------

    def start_server(self, dbs, group_names, port):
        groups, missing = resolve_launch_groups(self.core.groups, group_names or [])
        if not dbs and not groups:
            raise ValueError('не выбрано ни одной базы или группы')
        real_port = self.core.start_server(dbs or [], groups, port)
        return {'status': self.core.server_status(), 'missing': missing,
                'port': real_port}


def _json_response(handler, payload, code=200):
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    handler.send_response(code)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Cache-Control', 'no-store')
    handler.end_headers()
    handler.wfile.write(body)


def make_handler(web):
    class Handler(BaseHTTPRequestHandler):
        server_version = f'confdb-web/{__version__}'

        def log_message(self, *args):  # тихий HTTP-лог (журнал задач отдельно)
            pass

        def _body(self):
            length = int(self.headers.get('Content-Length') or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode('utf-8'))
            except ValueError:
                raise ValueError('некорректный JSON в теле запроса')

        def do_GET(self):
            path = self.path.split('?', 1)[0]
            try:
                if path in ('/', '/index.html'):
                    with open(HTML_PATH, encoding='utf-8') as fh:
                        html = fh.read().encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(html)))
                    self.end_headers()
                    self.wfile.write(html)
                elif path == '/api/state':
                    snap = web.core.snapshot()
                    snap['task'] = web.tasks.snapshot()
                    _json_response(self, snap)
                elif path == '/api/task':
                    _json_response(self, {'task': web.tasks.snapshot()})
                elif path == '/api/server':
                    _json_response(self, web.core.server_status())
                elif path == '/api/server/log':
                    _json_response(self, {'log': web.core.server_llm_log(),
                                          'running': web.core.runner.running})
                else:
                    _json_response(self, {'error': f'нет такого пути: {path}'}, 404)
            except Exception as err:  # noqa: BLE001
                _json_response(self, {'error': str(err)}, 500)

        def do_POST(self):
            path = self.path.split('?', 1)[0]
            try:
                data = self._body()
                result = self._route(path, data)
                _json_response(self, result)
            except ValueError as err:
                _json_response(self, {'error': str(err)}, 400)
            except Exception as err:  # noqa: BLE001
                _json_response(self, {'error': str(err)}, 500)

        def _route(self, path, data):
            core, tasks = web.core, web.tasks
            if path == '/api/options':
                core.set_options(data)
                return {'ok': True, 'options': core.options_dict()}
            if path == '/api/db_dir':
                return {'ok': True, 'path': core.add_db_dir(data['path'])}
            if path == '/api/groups':
                core.set_group(data['name'], data.get('paths') or [])
                return {'ok': True, 'groups': core.groups}
            if path == '/api/groups/delete':
                core.delete_group(data['name'])
                return {'ok': True, 'groups': core.groups}
            if path == '/api/scan':
                if tasks.busy():
                    raise ValueError('уже выполняется задача')
                return {'task': web.scan()}
            if path == '/api/extract':
                if tasks.busy():
                    raise ValueError('уже выполняется задача')
                return {'task': web.extract(data['src'], data.get('db'),
                                            data.get('dump'))}
            if path == '/api/extract_batch':
                if tasks.busy():
                    raise ValueError('уже выполняется задача')
                return {'task': web.extract_batch(data['items'])}
            if path == '/api/fts':
                return {'task': web.fts(data['db'])}
            if path == '/api/bench':
                return {'task': web.bench(data['src'])}
            if path == '/api/server/start':
                return web.start_server(data.get('dbs') or [],
                                        data.get('groups') or [],
                                        int(data.get('port') or 8765))
            if path == '/api/server/stop':
                core.stop_server()
                return {'status': core.server_status()}
            if path == '/api/server/use':
                return {'status': core.server_use(data['alias'])}
            if path == '/api/server/open':
                alias, status = core.server_open_db(data['path'])
                return {'alias': alias, 'status': status}
            if path == '/api/server/close':
                return {'status': core.server_close_db(data['alias'])}
            if path == '/api/server/group/create':
                return {'status': core.server_create_group(data['name'])}
            if path == '/api/server/group/add':
                return {'status': core.server_add_db_to_group(
                    data['group'], data['alias'])}
            if path == '/api/server/group/close':
                return {'status': core.server_close_group(data['name'])}
            if path == '/api/server/group/use':
                return {'status': core.server_use_group(data['name'])}
            if path == '/api/server/apply_group':
                result, status = core.apply_config_group(
                    data['name'], replace=bool(data.get('replace')))
                return {'result': result, 'status': status}
            raise ValueError(f'нет такого пути: {path}')

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='confdb-web',
        description='Веб-интерфейс confdb: MCP-сервер, извлечение, каталог cf, '
                    'группы баз, настройки (логика общая с консольным TUI).')
    parser.add_argument('--host', default='127.0.0.1',
                        help='адрес (по умолчанию 127.0.0.1 — только локально)')
    parser.add_argument('--port', type=int, default=8766,
                        help='порт веб-интерфейса (по умолчанию 8766)')
    parser.add_argument('--no-browser', action='store_true',
                        help='не открывать браузер автоматически')
    args = parser.parse_args(argv)

    web = WebUi()
    handler = make_handler(web)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    url = f'http://{args.host}:{args.port}/'
    print(f'Веб-интерфейс confdb {__version__}: {url}')
    print('Остановка — Ctrl+C.')
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nОстанавливаю…')
    finally:
        web.core.stop_server()
        httpd.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
