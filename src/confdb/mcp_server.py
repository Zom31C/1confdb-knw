"""1confdb-knw — MCP-сервер знаний по конфигурации 1С и BSL.

Рассчитан на использование любой LLM без контекста проекта: инструкции
протокола и описания инструментов содержат справочник по базе данных,
глоссарий 1С и рекомендуемые рабочие процессы.

Транспорты:
- stdio (по умолчанию): JSON-RPC 2.0, сообщения по одному на строку stdin/stdout;
  запуск, в т.ч. через SSH со стороны MCP-клиента:
    1confdb-knw <путь-к-базе.sqlite>
    python -m confdb.mcp_server <путь-к-базе.sqlite>
- HTTP (опция --port): сервер слушает порт, клиент подключается по URL
  (Streamable HTTP: POST /mcp; legacy SSE: GET /sse + POST /messages).
  Для доступа с другой машины — SSH-туннель:
    ssh -L 8765:127.0.0.1:8765 user@host
  и в конфиге клиента {"url": "http://127.0.0.1:8765/mcp"}.
"""
import argparse
import json
import queue
import sqlite3
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PROTOCOL_VERSION = '2024-11-05'

PRIMER = """1confdb-knw: MCP server over a knowledge base of a 1C:Enterprise 8 configuration — metadata, BSL code and SKD queries, extracted from a binary .cf file into SQLite. 1C is a Russian business-automation platform; a configuration contains metadata objects, their fields, modules of 1C-language code (Russian keywords) and SKD report queries. All object/field names are in Russian.

GLOSSARY: Catalog=справочник (directory), Document=документ, InformationRegister/AccumulationRegister=регистры, Enum=перечисление, DataProcessor=обработка, Report=отчет, DefinedType=определяемый тип, CommonAttribute=общий реквизит. Tabular section (табличная часть) = row table of an object (e.g. Document.ЗаказПокупателя has section Запасы with fields Номенклатура, Цена…).

DATABASE SCHEMA:
- meta_object(id, path, type, type_ru, name, uuid, comment, parent_id, ord). path like 'Catalog/Номенклатура' or nested 'Catalog/Х/CatalogForm/ФормаЭлемента'; type = English stem (Catalog, Document, InformationRegister, Enum, CommonModule, DefinedType…); type_ru = Russian label as in the configurator.
- meta_attribute(object_id, ord, name, type_str, tabular). Object fields; tabular NULL = header attribute, else the tabular section the field belongs to. type_str examples: 'Строка(50)', 'Число', 'Ссылка: Catalog/Валюты', 'ОпределяемыйТип: DefinedType/Х (Ссылка: …)', composites joined with ' | '; 'Ссылка' alone = abstract/any reference.
- meta_tabular(object_id, ord, name) — tabular sections in declaration order.
- module(object_id, code_name, context, body). code_name: 'obj' (object module), 'mgr' (manager module), form/common modules etc.; context = execution context for common modules (Сервер/Клиент/…); body = module text WITHOUT method bodies (signatures, comments, #Если regions) — a table of contents.
- method(id, module_id, ord, kind, name, signature, is_export, directives, description, line_start, line_end, body). Procedures/functions of the 1C code; directives like '&НаСервере'/'&НаКлиенте'; description = comment block above the method.
- attribute_ref(attribute_id, ord, uuid, object_id) — which metadata objects a field's type references (one row per member; NULL object = abstract). Use for joins and impact analysis ('who references X').
- skd_query(object_id, ord, query) — report queries in the 1C query language (Russian keywords ВЫБРАТЬ/ИЗ/ГДЕ/СОЕДИНЕНИЕ/ОБЪЕДИНИТЬ).
- enum_value(object_id, ord, name) — enum values; predefined(object_id, ord, name, code, display) — predefined elements; common_target(common_id, target_id) — objects a common attribute is attached to; subsystem_content — subsystem composition; source, file.

1C QUERY LANGUAGE: Russian keywords, dotted paths, table names 'Справочник.Имя', 'Документ.Имя', 'РегистрСведений.Имя', 'РегистрНакопления.Имя.Обороты' (virtual tables: Остатки, Обороты, СрезПоследних…). Example: ВЫБРАТЬ Т.Запасы.Номенклатура.Наименование ИЗ Документ.ЗаказПокупателя КАК Т ГДЕ Т.Сумма > 0.

RECOMMENDED WORKFLOW to write a query or 1C code: 1) find_objects to locate objects; 2) object_card for its fields, sections and references; 3) skd_of / find_skd to see how THIS configuration queries the same tables (best examples); 4) find_methods + get_method to reuse existing code instead of inventing; 5) check_query to validate your query before use.

All tools are read-only. Prefer the dedicated tools over raw sql; use sql only for what is not covered."""


class McpServer:
    """Обработчик JSON-RPC сообщений MCP поверх базы SQLite (read-only)."""

    def __init__(self, db_path):
        self.db_path = db_path
        self._conn = None
        self._ctx = None

    # -- инфраструктура ----------------------------------------------------
    def conn(self):
        if self._conn is None:
            self._conn = sqlite3.connect(
                f'file:{self.db_path}?mode=ro', uri=True,
                check_same_thread=False)  # HTTP-транспорт: потоки под блокировкой
        return self._conn

    def ctx(self):
        if self._ctx is None:
            from .query_lang import MetaContext
            self._ctx = MetaContext(self.conn())
        return self._ctx

    def handle(self, msg):
        method = msg.get('method')
        msg_id = msg.get('id')
        if method == 'initialize':
            return {'jsonrpc': '2.0', 'id': msg_id, 'result': {
                'protocolVersion': PROTOCOL_VERSION,
                'capabilities': {'tools': {}},
                'serverInfo': {'name': '1confdb-knw', 'version': '1.0'},
                'instructions': PRIMER}}
        if msg_id is None or (method or '').startswith('notifications/'):
            return None  # уведомления
        if method == 'ping':
            return {'jsonrpc': '2.0', 'id': msg_id, 'result': {}}
        if method == 'tools/list':
            return {'jsonrpc': '2.0', 'id': msg_id,
                    'result': {'tools': [t.spec() for t in TOOLS]}}
        if method == 'tools/call':
            params = msg.get('params', {})
            name = params.get('name')
            args = params.get('arguments', {}) or {}
            tool = next((t for t in TOOLS if t.name == name), None)
            if tool is None:
                return {'jsonrpc': '2.0', 'id': msg_id, 'error': {
                    'code': -32602, 'message': f'unknown tool: {name}'}}
            try:
                text = tool.run(self, **args)
                return {'jsonrpc': '2.0', 'id': msg_id, 'result': {
                    'content': [{'type': 'text', 'text': text}]}}
            except Exception as err:  # noqa: BLE001 — ошибка инструмента, не сервера
                return {'jsonrpc': '2.0', 'id': msg_id, 'result': {
                    'content': [{'type': 'text', 'text': f'ошибка: {err}'}],
                    'isError': True}}
        return {'jsonrpc': '2.0', 'id': msg_id, 'error': {
            'code': -32601, 'message': f'method not found: {method}'}}

    # -- инструменты ---------------------------------------------------------
    def find_objects(self, mask, type=None, limit=20):  # noqa: A002
        like = f'%{mask}%'
        sql = ('SELECT path, type, type_ru, name FROM meta_object '
               'WHERE name LIKE ? OR path LIKE ?')
        params = [like, like]
        if type:
            sql += ' AND (type = ? OR type_ru = ?)'
            params += [type, type]
        sql += ' ORDER BY length(path), path LIMIT ?'
        params.append(int(limit))
        rows = self.conn().execute(sql, params).fetchall()
        if not rows:
            return 'ничего не найдено'
        return '\n'.join(f'{p} — {ru} ({t})' for p, t, ru, _ in rows)

    def object_card(self, path):
        q = self.conn().execute
        row = q('SELECT type, type_ru, name, comment FROM meta_object '
                'WHERE path=?', (path,)).fetchone()
        if not row:
            return f'объект не найден: {path}'
        oid = q('SELECT id FROM meta_object WHERE path=?', (path,)).fetchone()[0]
        out = [f'{path} — {row[1]} ({row[0]}), имя {row[2]}' +
               (f'; комментарий: {row[3]}' if row[3] else '')]
        attrs = q('SELECT name, type_str FROM meta_attribute '
                  'WHERE object_id=? AND tabular IS NULL ORDER BY ord',
                  (oid,)).fetchall()
        out.append('Реквизиты: ' + ('; '.join(
            f'{n}: {t or "?"}' for n, t in attrs) if attrs else 'нет'))
        tabs = q('SELECT t.name, a.name, a.type_str FROM meta_tabular t '
                 'LEFT JOIN meta_attribute a ON a.object_id=t.object_id '
                 'AND a.tabular=t.name WHERE t.object_id=? '
                 'ORDER BY t.ord, a.ord', (oid,)).fetchall()
        sections = {}
        for sec, fname, ftype in tabs:
            sections.setdefault(sec, []).append(f'{fname}: {ftype or "?"}')
        for sec, fields in sections.items():
            out.append(f'Табличная часть {sec}: ' + '; '.join(fields))
        mods = q('SELECT code_name, context FROM module WHERE object_id=?',
                 (oid,)).fetchall()
        if mods:
            out.append('Модули: ' + ', '.join(
                c + (f' [{x}]' if x else '') for c, x in mods))
        nskd = q('SELECT COUNT(*) FROM skd_query WHERE object_id=?',
                 (oid,)).fetchone()[0]
        if nskd:
            out.append(f'Запросов СКД: {nskd} (см. skd_of)')
        fwd = [r[0] for r in q(
            'SELECT DISTINCT t.path FROM attribute_ref r '
            'JOIN meta_attribute a ON a.id=r.attribute_id '
            'JOIN meta_object v ON v.id=a.object_id '
            'JOIN meta_object t ON t.id=r.object_id '
            'WHERE v.path=? AND t.path IS NOT NULL LIMIT 12', (path,))]
        if fwd:
            out.append('Ссылается на: ' + ', '.join(fwd))
        rev = [r[0] for r in q(
            'SELECT DISTINCT v.path FROM attribute_ref r '
            'JOIN meta_attribute a ON a.id=r.attribute_id '
            'JOIN meta_object v ON v.id=a.object_id '
            'JOIN meta_object t ON t.id=r.object_id '
            'WHERE t.path=? LIMIT 12', (path,))]
        if rev:
            out.append('На него ссылаются: ' + ', '.join(rev))
        return '\n'.join(out)

    def object_tree(self, path='', depth=2):
        rows = self.conn().execute(
            'SELECT id, parent_id, path, type_ru FROM meta_object '
            'ORDER BY ord').fetchall()
        children = {}
        ids = {}
        for oid, pid, p, ru in rows:
            children.setdefault(pid, []).append((p, ru, oid))
            ids[oid] = (p, ru)
        root_id = None
        for oid, (p, _) in ids.items():
            if p == path:
                root_id = oid
                break
        if root_id is None:
            return f'объект не найден: {path}'
        out = []

        def walk(oid, lvl):
            if lvl > depth:
                return
            for p, ru, cid in children.get(oid, []):
                out.append('  ' * lvl + f'{p} — {ru}')
                walk(cid, lvl + 1)

        out.append(f'{path or "(корень)"} — {ids[root_id][1]}')
        walk(root_id, 1)
        return '\n'.join(out)

    def find_field(self, name, limit=20):
        rows = self.conn().execute(
            'SELECT o.path, a.name, a.tabular, a.type_str FROM meta_attribute a '
            'JOIN meta_object o ON o.id=a.object_id '
            'WHERE a.name LIKE ? ORDER BY o.path LIMIT ?',
            (f'%{name}%', int(limit))).fetchall()
        if not rows:
            return 'ничего не найдено'
        return '\n'.join(
            f'{p} :: поле {n} ({t or "?"})' + (f' [табчасть {s}]' if s else '')
            for p, n, s, t in rows)

    def refs_of(self, path, direction='both', limit=30):
        q = self.conn().execute
        out = []
        if direction in ('both', 'forward'):
            rows = q(
                'SELECT DISTINCT t.path FROM attribute_ref r '
                'JOIN meta_attribute a ON a.id=r.attribute_id '
                'JOIN meta_object v ON v.id=a.object_id '
                'JOIN meta_object t ON t.id=r.object_id '
                'WHERE v.path=? AND t.path IS NOT NULL LIMIT ?',
                (path, int(limit))).fetchall()
            out.append('Ссылается на: ' + (', '.join(r[0] for r in rows)
                       if rows else '—'))
        if direction in ('both', 'reverse'):
            rows = q(
                'SELECT DISTINCT v.path FROM attribute_ref r '
                'JOIN meta_attribute a ON a.id=r.attribute_id '
                'JOIN meta_object v ON v.id=a.object_id '
                'JOIN meta_object t ON t.id=r.object_id '
                'WHERE t.path=? LIMIT ?', (path, int(limit))).fetchall()
            out.append('На него ссылаются: ' + (', '.join(r[0] for r in rows)
                       if rows else '—'))
        return '\n'.join(out)

    def module_outline(self, path, code_name='obj'):
        row = self.conn().execute(
            'SELECT m.body FROM module m JOIN meta_object o ON o.id=m.object_id '
            'WHERE o.path=? AND m.code_name=?', (path, code_name)).fetchone()
        if not row or not row[0]:
            return f'модуль не найден: {path} ({code_name})'
        return row[0]

    def get_method(self, path, code_name, name):
        row = self.conn().execute(
            'SELECT mt.kind, mt.name, mt.signature, mt.directives, '
            'mt.description, mt.body, mt.is_export FROM method mt '
            'JOIN module m ON m.id=mt.module_id '
            'JOIN meta_object o ON o.id=m.object_id '
            'WHERE o.path=? AND m.code_name=? AND LOWER(mt.name)=LOWER(?)',
            (path, code_name, name)).fetchone()
        if not row:
            return f'метод не найден: {path} ({code_name}) :: {name}'
        head = f'{row[0]} {row[1]}({row[2]})' + (' Экспорт' if row[6] else '')
        parts = [head]
        if row[3]:
            parts.append('директивы: ' + row[3])
        if row[4]:
            parts.append('описание:\n' + row[4])
        parts.append('тело:\n' + row[5])
        return '\n'.join(parts)

    def find_methods(self, mask, path=None, limit=20):
        like = f'%{mask}%'
        sql = ('SELECT o.path, m.code_name, mt.kind, mt.name, mt.signature, '
               'mt.directives, mt.description FROM method mt '
               'JOIN module m ON m.id=mt.module_id '
               'JOIN meta_object o ON o.id=m.object_id '
               'WHERE mt.name LIKE ? OR mt.signature LIKE ? '
               'OR mt.description LIKE ?')
        params = [like, like, like]
        if path:
            sql += ' AND o.path=?'
            params.append(path)
        sql += ' LIMIT ?'
        params.append(int(limit))
        rows = self.conn().execute(sql, params).fetchall()
        if not rows:
            return 'ничего не найдено'
        out = []
        for p, code, kind, name, sig, dirs, desc in rows:
            line = f'{p} ({code}) — {kind} {name}({sig})'
            if dirs:
                line += f' [{dirs}]'
            if desc:
                line += ' | ' + desc.splitlines()[0][:80]
            out.append(line)
        return '\n'.join(out)

    def skd_of(self, path):
        rows = self.conn().execute(
            'SELECT q.query FROM skd_query q JOIN meta_object o '
            'ON o.id=q.object_id WHERE o.path=? ORDER BY q.ord',
            (path,)).fetchall()
        if not rows:
            return f'у объекта нет запросов СКД: {path}'
        return ('\n;\n'.join(r[0] for r in rows))[:20000]

    def find_skd(self, mask, limit=10):
        rows = self.conn().execute(
            'SELECT q.id, o.path, q.query FROM skd_query q '
            'JOIN meta_object o ON o.id=q.object_id '
            'WHERE q.query LIKE ? LIMIT ?',
            (f'%{mask}%', int(limit))).fetchall()
        if not rows:
            return 'ничего не найдено'
        out = []
        for rid, path, text in rows:
            pos = text.lower().find(mask.lower())
            snippet = text[max(0, pos - 120):pos + 240].replace('\n', ' ')
            out.append(f'[{rid}] {path} … {snippet} …')
        return '\n'.join(out)

    def check_query(self, text):
        from .query_lang import check_query as _check
        errs = _check(text, self.ctx())
        if not errs:
            return 'OK: синтаксис корректен, таблицы/поля/цепочки существуют'
        return 'Ошибки:\n' + '\n'.join(errs)

    def sql(self, query):
        stripped = query.strip().rstrip(';')
        head = stripped.upper()
        if not (head.startswith('SELECT') or head.startswith('WITH')):
            raise ValueError('разрешены только SELECT/WITH (read-only)')
        if ' LIMIT ' not in head:
            stripped += ' LIMIT 200'
        cur = self.conn().execute(stripped)
        cols = [c[0] for c in cur.description] if cur.description else []
        rows = cur.fetchall()
        if not rows:
            return '(пусто)'
        out = [' | '.join(cols)]
        for row in rows:
            cells = []
            for v in row:
                s = str(v)
                cells.append(s[:120] + ('…' if len(s) > 120 else ''))
            out.append(' | '.join(cells))
        return '\n'.join(out)


class Tool:
    def __init__(self, name, description, schema, fn):
        self.name = name
        self.description = description
        self.schema = schema
        self.fn = fn

    def spec(self):
        return {'name': self.name, 'description': self.description,
                'inputSchema': self.schema}

    def run(self, server, **args):
        return self.fn(server, **args)


def _schema(props, required=()):
    return {'type': 'object', 'properties': props, 'required': list(required)}


_STR = {'type': 'string'}
_INT = {'type': 'integer'}

TOOLS = [
    Tool('find_objects',
         'Search metadata objects by name or path substring. Returns '
         "'path — Russian type (English type)'. First step for anything: "
         "locate Catalog/Документ/регистр by its Russian name.",
         _schema({'mask': _STR, 'type': _STR,
                  'limit': _INT}, ('mask',)),
         McpServer.find_objects),
    Tool('object_card',
         "Full 'passport' of one object in a single call: type, header "
         'attributes with types, tabular sections with their fields, modules, '
         'SKD query count, forward/reverse references. Use right after '
         'find_objects.',
         _schema({'path': _STR}, ('path',)),
         McpServer.object_card),
    Tool('object_tree',
         "Browse the metadata tree 'as in the configurator' (subsystems, "
         'nested forms/commands). path empty = configuration root.',
         _schema({'path': _STR, 'depth': _INT}),
         McpServer.object_tree),
    Tool('find_field',
         'Reverse search: which objects contain a field/tabular-section field '
         'with this name. Use to discover join paths between tables.',
         _schema({'name': _STR, 'limit': _INT}, ('name',)),
         McpServer.find_field),
    Tool('refs_of',
         "Reference links of an object via attribute types: forward ('on what "
         "it references') and reverse ('who references it') — impact analysis.",
         _schema({'path': _STR, 'direction': _STR, 'limit': _INT}, ('path',)),
         McpServer.refs_of),
    Tool('module_outline',
         'Table of contents of a 1C module: signatures, comments, #Если '
         "regions, WITHOUT method bodies. code_name: 'obj' (object module), "
         "'mgr' (manager module) etc. Cheap way to inspect a module.",
         _schema({'path': _STR, 'code_name': _STR}, ('path',)),
         McpServer.module_outline),
    Tool('get_method',
         'Full source of one procedure/function: signature, directives '
         '(&НаСервере…), description comment and body. Use after '
         'find_methods/module_outline.',
         _schema({'path': _STR, 'code_name': _STR, 'name': _STR},
                 ('path', 'code_name', 'name')),
         McpServer.get_method),
    Tool('find_methods',
         'Search 1C methods by name/signature/description substring '
         "(e.g. 'ПриПроведении'). Reuse existing code instead of inventing.",
         _schema({'mask': _STR, 'path': _STR, 'limit': _INT}, ('mask',)),
         McpServer.find_methods),
    Tool('skd_of',
         'All SKD (report) queries of an object — the best examples of how '
         'THIS configuration queries its own tables.',
         _schema({'path': _STR}, ('path',)),
         McpServer.skd_of),
    Tool('find_skd',
         'Search across all SKD query texts (e.g. a table name like '
         "'РегистрНакопления.Запасы'). Returns snippets around the match.",
         _schema({'mask': _STR, 'limit': _INT}, ('mask',)),
         McpServer.find_skd),
    Tool('check_query',
         'Validate a 1C query: syntax (Russian keywords) + existence of '
         'tables/fields/reference chains against this configuration. ALWAYS '
         'run it on a query you wrote before using it.',
         _schema({'text': _STR}, ('text',)),
         McpServer.check_query),
    Tool('sql',
         'Read-only SELECT escape hatch for anything not covered by the '
         'dedicated tools. Non-SELECT is rejected; LIMIT 200 enforced.',
         _schema({'query': _STR}, ('query',)),
         McpServer.sql),
]


def make_handler(server):
    """HTTP-обработчик MCP: Streamable HTTP (POST /mcp) и legacy SSE (/sse)."""
    state = {'lock': threading.Lock(), 'sessions': {}}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        server_version = '1confdb-knw'

        def _send(self, code, body=None, extra=None):
            data = None if body is None else (
                json.dumps(body, ensure_ascii=False).encode('utf-8'))
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            for key, val in (extra or {}).items():
                self.send_header(key, val)
            self.send_header('Content-Length', str(len(data) if data else 0))
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _read_msg(self):
            length = int(self.headers.get('Content-Length') or 0)
            try:
                return json.loads(self.rfile.read(length))
            except ValueError:
                return None

        def do_OPTIONS(self):
            self._send(204, extra={
                'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type, Mcp-Session-Id'})

        def do_GET(self):
            path = urlparse(self.path)
            if path.path != '/sse':
                return self._send(404, {'error': f'not found: {path.path}'})
            sid = uuid.uuid4().hex
            events = queue.Queue()
            state['sessions'][sid] = events
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(
                    f'event: endpoint\n'
                    f'data: /messages?session_id={sid}\n\n'.encode('utf-8'))
                self.wfile.flush()
                while True:
                    try:
                        msg = events.get(timeout=20)
                    except queue.Empty:
                        self.wfile.write(b': keep-alive\n\n')
                        self.wfile.flush()
                        continue
                    self.wfile.write((
                        f'event: message\n'
                        f'data: {json.dumps(msg, ensure_ascii=False)}\n\n'
                    ).encode('utf-8'))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            finally:
                state['sessions'].pop(sid, None)

        def do_POST(self):
            path = urlparse(self.path)
            if path.path not in ('/mcp', '/messages'):
                return self._send(404, {'error': f'not found: {path.path}'})
            msg = self._read_msg()
            if msg is None:
                return self._send(400, {'error': 'body must be a JSON-RPC message'})
            with state['lock']:
                resp = server.handle(msg)
            if path.path == '/mcp':
                if resp is None:
                    return self._send(202)
                return self._send(200, resp)
            sid = parse_qs(path.query).get('session_id', [''])[0]
            events = state['sessions'].get(sid)
            if events is None:
                return self._send(404, {'error': 'unknown session_id'})
            if resp is not None:
                events.put(resp)
            return self._send(202, {'status': 'accepted'})

    return Handler


def start_http_server(server, host='127.0.0.1', port=0):
    """Поднимает ThreadingHTTPServer; возвращает (httpd, фактический порт)."""
    httpd = ThreadingHTTPServer((host, port), make_handler(server))
    httpd.daemon_threads = True
    return httpd, httpd.server_address[1]


def serve_http(db_path, host='127.0.0.1', port=8765):
    server = McpServer(db_path)
    httpd, real_port = start_http_server(server, host, port)
    print(f'1confdb-knw: слушаю http://{host}:{real_port}/mcp '
          f'(legacy SSE: /sse); остановка — Ctrl+C.')
    if host == '127.0.0.1':
        print('С другой машины — через SSH-туннель: '
              f'ssh -L {real_port}:127.0.0.1:{real_port} user@host')
        print('Конфигурация клиента: '
              f'{{"mcpServers": {{"1confdb-knw": '
              f'{{"url": "http://127.0.0.1:{real_port}/mcp"}}}}}}')
    else:
        print('Внимание: порт открыт для внешних подключений без аутентификации; '
              'база отдаётся read-only.')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('Сервер остановлен.')
    finally:
        httpd.server_close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='1confdb-knw',
        description='MCP-сервер знаний по конфигурации 1С и BSL '
                    '(stdio по умолчанию; --port — HTTP для SSH-туннеля)')
    parser.add_argument('db', help='путь к базе SQLite')
    parser.add_argument('--host', default='127.0.0.1',
                        help='адрес для HTTP-режима (по умолчанию 127.0.0.1)')
    parser.add_argument('--port', type=int, default=0,
                        help='порт HTTP-режима (без него — stdio)')
    args = parser.parse_args(argv)
    if args.port:
        return serve_http(args.db, args.host, args.port)
    server = McpServer(args.db)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        resp = server.handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + '\n')
            sys.stdout.flush()
    return 0


if __name__ == '__main__':
    sys.exit(main())
