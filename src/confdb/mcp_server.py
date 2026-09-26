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

Путь к базе можно не указывать — тогда берётся last_db из
~/.confdb/config.json, а при его отсутствии база ищется сама:
*.db/*.sqlite в текущем каталоге, db/ и _out/ (и в корне установки,
если запуск из venv). Свежая установка с привезённой базой работает
без ручной правки конфига.

Баз можно открыть несколько одновременно (например, основная
конфигурация + расширения/обработки): перечислите несколько путей
при запуске либо открывайте базы инструментом db_open уже на ходу;
активная база переключается инструментом db_use.
"""
import argparse
import glob
import json
import os
import queue
import re
import sqlite3
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import compare
from . import header_props
from .config import load_config
from .db.writer import TYPE_RU

PROTOCOL_VERSION = '2024-11-05'

# пути объектов наружу — «как в конфигураторе»: Справочник.Имя[.Подобъект];
# на вход принимается и старый слэш-формат Catalog/Имя
_RU2TYPES = {}
for _stem, _ru in TYPE_RU.items():
    _RU2TYPES.setdefault(_ru, []).append(_stem)
    _RU2TYPES.setdefault(_ru.replace(' ', ''), []).append(_stem)
# Русское имя типа принимается в любом регистре и без пробелов: сервер печатает
# 'Регистр накопления.Имя', а язык запросов и BSL пишут 'РегистрНакопления.Имя'
# — это один и тот же тип.
_RU2TYPES_LOW = {}
for _key, _stems in _RU2TYPES.items():
    for _stem in _stems:
        if _stem not in _RU2TYPES_LOW.setdefault(_key.lower(), []):
            _RU2TYPES_LOW[_key.lower()].append(_stem)
_TYPE_SLASH_RE = re.compile(
    '(?:' + '|'.join(map(re.escape, sorted(TYPE_RU, key=len, reverse=True))) + ')/')
# 'Справочник.Имя' в строковых литералах sql -> внутренний 'Catalog/Имя'
_RU_PATH_LIT_RE = re.compile(
    "'(" + '|'.join(map(re.escape, sorted(_RU2TYPES, key=len, reverse=True))) +
    r")\.([^'.]+)'", re.IGNORECASE)


def _type_stems(name):
    """Англ. stem'ы типа метаданных по его русскому имени (регистр не важен)."""
    return _RU2TYPES_LOW.get((name or '').strip().lower())


def _sql_rewrite(query):
    def sub(match):
        stems = _type_stems(match.group(1))
        return f"'{stems[0]}/{match.group(2)}'" if stems else match.group(0)
    return _RU_PATH_LIT_RE.sub(sub, query)


def ru_path(path):
    """'Catalog/Х/CatalogForm/У' -> 'Справочник.Х.У'."""
    parts = str(path).split('/')
    if len(parts) % 2 or parts[0] not in TYPE_RU:
        return str(path)
    return '.'.join([TYPE_RU[parts[0]]] + parts[1::2])


_REGISTER_TYPES = frozenset({
    'InformationRegister', 'AccumulationRegister',
    'AccountingRegister', 'CalculationRegister',
})

_PERIODICITY = header_props.PERIODICITY


def _register_card_info(obj_type, header_json):
    """Свойства регистра из header_json: периодичность, режим записи."""
    return header_props.register_props(obj_type, header_json)


def ru_text(text):
    """Внутренние слэш-пути 'Catalog/Х' -> 'Справочник.Х' в произвольном тексте.

    Применяется и к сообщениям валидатора запросов: наружу всё отдаётся в форме
    «как в конфигураторе», внутренний формат пути не должен попадать в ответ.
    """
    if not text:
        return text
    return _TYPE_SLASH_RE.sub(lambda m: TYPE_RU[m.group(0)[:-1]] + '.', text)


def ru_type_str(text):
    """'Ссылка: Catalog/Валюты' -> 'Ссылка: Справочник.Валюты'.

    Голая 'Ссылка' без целевого объекта помечается '(цель не определена)'.
    """
    if not text:
        return text
    text = ru_text(text)
    parts = [p.strip() for p in text.split(' | ')]
    annotated = []
    for part in parts:
        if part == 'Ссылка':
            annotated.append('Ссылка (цель не определена)')
        else:
            annotated.append(part)
    return ' | '.join(annotated)


def body_hits(body, needle, line_start=1, limit=3, width=150):
    """Строки тела метода, содержащие подстроку: ['строка N: <текст>', …].

    Номер строки — в модуле, а не в теле (line_start метода уже учтён), поэтому
    по нему работают get_method и find_method_context. Сравнение без учёта
    регистра: LIKE в SQLite сворачивает регистр только для ASCII, а имена 1С
    кириллические, поэтому здесь сворачиваем сами.
    """
    low = needle.lower()
    hits = []
    for i, line in enumerate(body.split('\n')):
        if low in line.lower():
            hits.append(f'строка {line_start + i}: {line.strip()[:width]}')
            if len(hits) >= limit:
                break
    return hits


PRIMER = """1confdb-knw: MCP server over one or several knowledge bases of a 1C:Enterprise 8 configuration — metadata, BSL code and SKD queries, extracted from binary .cf/.cfe/.epf files into SQLite. 1C is a Russian business-automation platform; a configuration contains metadata objects, their fields, modules of 1C-language code (Russian keywords) and SKD report queries. All object/field names are in Russian.

GLOSSARY: Catalog=справочник (directory), Document=документ, InformationRegister/AccumulationRegister=регистры, Enum=перечисление, DataProcessor=обработка, Report=отчет, DefinedType=определяемый тип, CommonAttribute=общий реквизит, CommonModule=общий модуль. Tabular section (табличная часть) = row table of an object (e.g. Документ.ЗаказПокупателя has section Запасы with fields Номенклатура, Цена…).

OBJECT PATHS: tools return and accept configurator-style Russian dotted paths: 'Справочник.Номенклатура', nested 'Справочник.Х.ФормаЭлемента' (legacy 'Catalog/Х/…' slash form is also accepted as input). In the 1C query language the table name for an object is exactly this dotted form: 'Справочник.Имя', 'Документ.Имя', 'РегистрСведений.Имя'…

COMMON MODULES: in BSL code a common module is called by its bare name: 'ИмяМодуля.Функция(...)'. Prefixes like 'ОбщийМодуль.', 'Общий модуль.', 'ОбщМодуль.' are NOT valid code — never write them. The dotted 'Общий модуль.Имя' form only identifies the object in this knowledge base.

DATABASE FILE: the SQLite file is internal to the server. Do NOT search for it, open it, read it from disk, or ask the user for its location — you have no filesystem access to it. Everything is available through the tools below; the sql tool runs arbitrary read-only SELECTs.

MULTIPLE DATABASES: the server can hold several knowledge bases at once — typically the MAIN configuration plus extensions/data processors (.cfe/.epf extracted into their own .db files). Each open base has an alias. All tools query the ACTIVE base; to query a specific base without switching, pass its alias as the db parameter (e.g. find_objects(mask=…, db='расш_интеграция')). Management tools: db_list (what is open, which is active), db_open (open another base file while the server runs — the path comes from the user), db_use (switch the active base), db_close. An extension usually adds/overrides objects of the main configuration — if something is not found in one base, check the other. Special db value '*': run a tool on every open base at once (the answer is sectioned per base) — one call to compare the main configuration with all extensions.

DATABASE IDENTIFIER: every tool response includes a header line identifying the source database: '=== база <алиас> (<путь>) ==='. This lets you compare configurations (e.g. standard vs customized) or understand which base contains a method (main configuration vs extension). Use db='*' to query all bases at once and compare results side-by-side.

COMPARING BASES: compare_object(path, db_left, db_right) diffs ONE object between two open bases in a single call — attributes and their types, tabular sections, register dimensions/resources, forms and commands, modules, methods (signature, directives, body), SKD queries. Use it for standard-vs-customized or release-to-release analysis instead of fetching two passports and diffing them by hand. extension_diff(extension_db, base_db) answers the task-level question 'what does this extension do': new objects (carrying the extension name prefix), borrowed objects, the extension methods and whether one REPLACES a stock method (&Вместо) or inserts code around it (&После/&Перед), the attributes it adds, and its external dependencies. configuration_info says WHICH configuration and release a base holds (name, version, compatibility mode, source file, build date). These three take explicit base aliases (db_left/db_right, extension_db/base_db), not the db parameter, and db='*' does not apply to them.

REGISTERS: object_card of a РегистрСведений/РегистрНакопления lists Измерения (dimensions — they form the record key), Ресурсы (resources — the stored values) and Реквизиты (attributes) as SEPARATE groups, plus Периодичность and Режим записи (независимый / подчинение регистратору). Before writing СрезПоследних or joining a register, check whether the field you rely on is a dimension: only dimensions guarantee one row per key. A periodicity code that could not be decoded is shown as the raw code, never as a guessed name.

DATABASE SCHEMA (for the sql tool; path columns store the legacy slash form 'Catalog/Имя', but string literals in the Russian dotted form ('Справочник.Имя') are auto-converted — either form works in WHERE path = …):
- meta_object(id, path, type, type_ru, name, uuid, comment, parent_id, ord). path like 'Catalog/Номенклатура'; type = English stem (Catalog, Document, InformationRegister, Enum, CommonModule, DefinedType…); type_ru = Russian label as in the configurator.
- meta_attribute(object_id, ord, name, type_str, tabular). Object fields; tabular NULL = header attribute, else the tabular section the field belongs to. type_str examples: 'Строка(50)', 'Число', 'Ссылка: Справочник.Валюты', 'ОпределяемыйТип: … (Ссылка: …)', composites joined with ' | '; 'Ссылка' alone = abstract/any reference.
- meta_tabular(object_id, ord, name) — tabular sections in declaration order.
- module(object_id, code_name, context, body). code_name: 'obj' (object module), 'mgr' (manager module), form/common modules etc.; context = execution context for common modules (Сервер/Клиент/…); body = module text WITHOUT method bodies (signatures, comments, #Если regions) — a table of contents.
- method(id, module_id, ord, kind, name, signature, is_export, directives, description, line_start, line_end, body). Procedures/functions of the 1C code; directives like '&НаСервере'/'&НаКлиенте'; description = comment block above the method.
- attribute_ref(attribute_id, ord, uuid, object_id) — which metadata objects a field's type references (one row per member; NULL object = abstract). Use for joins and impact analysis ('who references X').
- skd_query(object_id, ord, query) — report queries in the 1C query language (Russian keywords ВЫБРАТЬ/ИЗ/ГДЕ/СОЕДИНЕНИЕ/ОБЪЕДИНИТЬ).
- enum_value(object_id, ord, name) — enum values; predefined(object_id, ord, name, code, display) — predefined elements; common_target(common_id, target_id) — objects a common attribute is attached to; subsystem_content — subsystem composition; source, file.

1C QUERY LANGUAGE: Russian keywords, dotted paths, table names 'Справочник.Имя', 'Документ.Имя', 'РегистрСведений.Имя', 'РегистрНакопления.Имя.Обороты' (virtual tables: Остатки, Обороты, СрезПоследних…). Grouping clause is 'СГРУППИРОВАТЬ ПО' — the form 'СГРУППИРОВАНО' does NOT exist in the 1C query language. Example: ВЫБРАТЬ Т.Запасы.Номенклатура.Наименование ИЗ Документ.ЗаказПокупателя КАК Т ГДЕ Т.Сумма > 0.

RECOMMENDED WORKFLOW to write a query or 1C code: 1) configuration_info to know which configuration and release you are in, find_objects to locate objects; 2) object_card for its fields, sections and references; 3) skd_of / find_skd to see how THIS configuration queries the same tables (best examples); 4) find_methods — by mask for a name/signature/description, or by text to search INSIDE method bodies: that is how you find EVERY place touching something (all writes to a register, all calls of a common module, all uses of a field) without falling back to a full-text sql query, and every hit carries its module line number; then find_method_context for a window around the call you need (it also gives stable insertion markers) and get_method for the full body — reuse existing code instead of inventing; 5) check_query to validate your query before use; 6) method_dependencies before porting code to another configuration (it lists everything the code needs there), compare_object / extension_diff to see how two configurations differ; 7) method_result_schema when a stock function returns a temporary table and you need its columns. This server does NOT check 1C code syntax — for that use the 1confdb-knw-lsp variant (BSL Language Server).

All tools are read-only. Prefer the dedicated tools over raw sql; use sql only for what is not covered. ANTI-LOOP: never issue more than two sql calls in a row — if sql did not answer the question, switch to the dedicated tools (find_objects, object_card, find_field, skd_of, refs_of). The schema is EXACTLY as documented above — never waste calls on PRAGMA / sqlite_master / schema guessing."""


class McpServer:
    """Обработчик JSON-RPC сообщений MCP поверх баз SQLite (read-only).

    Держит несколько баз одновременно (например, основная конфигурация
    плюс расширения/обработки): каждая видна под алиасом, инструменты
    работают с активной базой либо с явно указанной параметром db.
    """

    def __init__(self, db_paths=None):
        self.dbs = {}      # алиас -> {'path':…, 'conn':…, 'ctx':…}
        self.active = None
        if isinstance(db_paths, str):
            db_paths = [db_paths]
        for path in db_paths or ():
            # активна первая указанная база, а не последняя
            self.open_db(path, activate=False)

    # -- реестр баз ----------------------------------------------------------
    def _make_alias(self, path):
        base = os.path.splitext(os.path.basename(path))[0] or 'db'
        alias, num = base, 1
        while alias in self.dbs:
            num += 1
            alias = f'{base}_{num}'
        return alias

    def open_db(self, path, alias=None, activate=True):
        """Открывает базу и возвращает её алиас.

        Файл проверяется: должен существовать и содержать таблицу
        meta_object (база знаний confdb). Повторное открытие того же
        файла просто возвращает прежний алиас.
        """
        path = os.path.abspath(path)
        known = next((a for a, d in self.dbs.items()
                      if os.path.abspath(d['path']) == path), None)
        if known is not None:
            if activate:
                self.active = known
            return known
        if not os.path.isfile(path):
            raise ValueError(f'файл базы не найден: {path}')
        if alias is not None:
            if not str(alias).strip():
                raise ValueError('алиас не может быть пустым')
            alias = str(alias).strip()
            if alias.lower() in (a.lower() for a in self.dbs):
                raise ValueError(f'алиас уже занят: {alias}')
        conn = sqlite3.connect(
            f'file:{path}?mode=ro', uri=True,
            check_same_thread=False)  # HTTP-транспорт: потоки под блокировкой
        # sqlite-LOWER не знает кириллицу — регистрируем питоний lower
        conn.create_function(
            'lower_ru', 1, lambda v: v.lower() if isinstance(v, str) else v)
        # регистронезависимый поиск подстроки в больших текстах (тела методов):
        # LIKE сворачивает регистр только для ASCII, поэтому кириллическая игла
        # в нижнем регистре не нашла бы текст в смешанном. Своя функция ещё и
        # в полтора раза быстрее lower_ru(body) LIKE — замер на базе УНФ
        # (241566 методов): 3,6 с против 5,5 с. Регистр сворачиваем у обоих
        # аргументов: функция видна модели через инструмент sql, и требовать
        # от вызывающего заранее переведённой в нижний регистр иглы нельзя —
        # иначе она молча вернула бы ноль
        conn.create_function(
            'body_has', 2,
            lambda body, needle: isinstance(body, str)
            and isinstance(needle, str) and needle.lower() in body.lower())
        try:
            conn.execute('SELECT COUNT(*) FROM meta_object').fetchone()
        except sqlite3.Error:
            conn.close()
            raise ValueError(
                f'это не база знаний confdb (нет таблицы meta_object): {path}')
        if alias is None:
            alias = self._make_alias(path)
        self.dbs[alias] = {'path': path, 'conn': conn, 'ctx': None}
        if activate or self.active is None:
            self.active = alias
        return alias

    def close_db(self, alias=None):
        """Закрывает базу (по умолчанию активную); возвращает её алиас."""
        alias = self._alias(alias)
        info = self.dbs.pop(alias)
        info['conn'].close()
        if self.active == alias:
            self.active = next(iter(self.dbs), None)
        return alias

    def _alias(self, alias=None):
        """Разрешает алиас (None = активная база); ValueError, если не найден."""
        if not alias:
            if self.active is None:
                raise ValueError('нет открытых баз — укажите путь в db_open')
            return self.active
        for key in self.dbs:
            if key.lower() == str(alias).strip().lower():
                return key
        raise ValueError(
            f'база не открыта: {alias}' +
            ('; открыты: ' + ', '.join(self.dbs) if self.dbs
             else ' — откройте через db_open'))

    def db_stats(self, alias=None):
        """(объекты, модули, методы) базы — для отчётов пользователю."""
        return self.conn(alias).execute(
            'SELECT (SELECT COUNT(*) FROM meta_object), '
            '(SELECT COUNT(*) FROM module), '
            '(SELECT COUNT(*) FROM method)').fetchone()

    # -- инфраструктура ----------------------------------------------------
    def conn(self, db=None):
        return self.dbs[self._alias(db)]['conn']

    def ctx(self, db=None):
        alias = self._alias(db)
        info = self.dbs[alias]
        if info['ctx'] is None:
            from .query_lang import MetaContext
            info['ctx'] = MetaContext(info['conn'])
        return info['ctx']

    def resolve_path(self, value, db=None):
        """Русский точечный путь ('Справочник.Х.Форма') -> внутренний слэш-путь."""
        value = (value or '').strip()
        if not value or '/' in value or '.' not in value:
            return value
        parts = value.split('.')
        stems = _type_stems(parts[0])
        if not stems:
            return value
        conn = self.conn(db)
        row = conn.execute(
            'SELECT id, path FROM meta_object WHERE name=? AND type IN (%s)'
            % ','.join('?' * len(stems)), [parts[1]] + stems).fetchone()
        if not row:
            return value
        oid, path = row
        for name in parts[2:]:
            row = conn.execute(
                'SELECT id, path FROM meta_object '
                'WHERE parent_id=? AND name=? ORDER BY ord', (oid, name)).fetchone()
            if not row:
                return value
            oid, path = row
        return path

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
                    'content': [{'type': 'text', 'text': error_text(err)}],
                    'isError': True}}
        return {'jsonrpc': '2.0', 'id': msg_id, 'error': {
            'code': -32601, 'message': f'method not found: {method}'}}

    # -- инструменты ---------------------------------------------------------
    def find_objects(self, mask, type=None, limit=20, db=None):  # noqa: A002
        like = f'%{mask}%'
        # имена в 1С пишутся Слитно, а маски часто приходят с пробелами
        # и в другой раскладке регистра
        like_ns = f'%{mask.replace(" ", "").lower()}%'
        sql = ('SELECT path, type, type_ru, name FROM meta_object '
               'WHERE name LIKE ? OR path LIKE ? '
               "OR lower_ru(REPLACE(name, ' ', '')) LIKE ? "
               "OR lower_ru(REPLACE(path, ' ', '')) LIKE ?")
        params = [like, like, like_ns, like_ns]
        if type:
            sql += ' AND (type = ? OR type_ru = ?)'
            params += [type, type]
        sql += ' ORDER BY length(path), path LIMIT ?'
        params.append(int(limit))
        rows = self.conn(db).execute(sql, params).fetchall()
        if not rows:
            return 'ничего не найдено'
        return '\n'.join(f'{ru_path(p)} — {ru} ({t})' for p, t, ru, _ in rows)

    def object_card(self, path, db=None):
        path = self.resolve_path(path, db)
        q = self.conn(db).execute
        row = q('SELECT type, type_ru, name, comment, header_json '
                'FROM meta_object WHERE path=?', (path,)).fetchone()
        if not row:
            return f'объект не найден: {path}'
        oid = q('SELECT id FROM meta_object WHERE path=?', (path,)).fetchone()[0]
        out = [f'{ru_path(path)} — {row[1]} ({row[0]}), имя {row[2]}' +
               (f'; комментарий: {row[3]}' if row[3] else '')]
        # свойства регистра (периодичность, режим записи)
        out.extend(_register_card_info(row[0], row[4]))
        attrs = q('SELECT name, type_str FROM meta_attribute '
                  'WHERE object_id=? AND tabular IS NULL ORDER BY ord',
                  (oid,)).fetchall()
        kinds = (header_props.register_field_kinds(row[4])
                 if row[0] in _REGISTER_TYPES else {})
        if kinds:
            # у регистра измерения/ресурсы/реквизиты показываются раздельно:
            # по плоскому списку не понять, что входит в ключ записи
            groups = {}
            for name, tstr in attrs:
                groups.setdefault(kinds.get(name, 'Прочие поля'), []).append(
                    f'{name}: {ru_type_str(tstr) or "?"}')
            for kind in header_props.REGISTER_KINDS + ('Прочие поля',):
                if kind in groups:
                    out.append(f'{kind}: ' + '; '.join(groups[kind]))
            if not attrs:
                out.append('Реквизиты: нет')
        else:
            out.append('Реквизиты: ' + ('; '.join(
                f'{n}: {ru_type_str(t) or "?"}' for n, t in attrs)
                if attrs else 'нет'))
        tabs = q('SELECT t.name, a.name, a.type_str FROM meta_tabular t '
                 'LEFT JOIN meta_attribute a ON a.object_id=t.object_id '
                 'AND a.tabular=t.name WHERE t.object_id=? '
                 'ORDER BY t.ord, a.ord', (oid,)).fetchall()
        sections = {}
        for sec, fname, ftype in tabs:
            sections.setdefault(sec, []).append(
                f'{fname}: {ru_type_str(ftype) or "?"}')
        for sec, fields in sections.items():
            out.append(f'Табличная часть {sec}: ' + '; '.join(fields))
        mods = q('SELECT code_name, context FROM module WHERE object_id=?',
                 (oid,)).fetchall()
        if mods:
            out.append('Модули: ' + ', '.join(
                c + (f' [{x}]' if x else '') for c, x in mods))
        out.extend(self._children_lines(q, oid))
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
            out.append('Ссылается на: ' + ', '.join(ru_path(p) for p in fwd))
        rev = [r[0] for r in q(
            'SELECT DISTINCT v.path FROM attribute_ref r '
            'JOIN meta_attribute a ON a.id=r.attribute_id '
            'JOIN meta_object v ON v.id=a.object_id '
            'JOIN meta_object t ON t.id=r.object_id '
            'WHERE t.path=? LIMIT 12', (path,))]
        if rev:
            out.append('На него ссылаются: ' + ', '.join(ru_path(p) for p in rev))
        return '\n'.join(out)

    @staticmethod
    def _children_lines(q, oid):
        """Строки вложенных объектов: формы, команды, макеты.

        Путь к ним — '<путь родителя>.<имя>', поэтому имена достаточно
        перечислить: object_card/get_method принимают его целиком.
        """
        buckets = {}
        for name, ktype in q('SELECT name, type FROM meta_object '
                             'WHERE parent_id=? ORDER BY ord', (oid,)):
            if ktype.endswith('Form'):
                key = 'Формы'
            elif ktype.endswith('Command'):
                key = 'Команды'
            elif ktype.endswith('Template'):
                key = 'Макеты'
            else:
                key = 'Прочие подобъекты'
            buckets.setdefault(key, []).append(name)
        lines = []
        for key in ('Формы', 'Команды', 'Макеты', 'Прочие подобъекты'):
            if key in buckets:
                lines.append(f'{key}: ' + ', '.join(buckets[key]))
        return lines

    def configuration_info(self, db=None):
        """Паспорт конфигурации/расширения: имя, версия, режим совместимости."""
        alias = self._alias(db)
        props = self.cfg_props(alias)
        if not props:
            return 'корневой объект конфигурации не найден'
        label = {'Configuration': 'Конфигурация',
                 'ConfigurationExtension': 'Расширение конфигурации',
                 'ExternalDataProcessor': 'Внешняя обработка'}.get(
                     props.get('root_type'),
                     props.get('root_type_ru') or props.get('root_type') or '?')
        head = f'{label}: {props.get("name") or "?"}'
        if props.get('synonym') and props['synonym'] != props.get('name'):
            head += f' ({props["synonym"]})'
        out = [head]
        root_ru = props.get('root_type_ru')
        root_type = props.get('root_type')
        # type_ru в meta_object допускает NULL — без запасного варианта
        # получилось бы «Тип корня: None (Configuration)»
        out.append('Тип корня: '
                   + (f'{root_ru} ({root_type})' if root_ru and root_type
                      else (root_ru or root_type or '?')))
        out.append('Версия '
                   + ('расширения'
                      if props.get('root_type') == 'ConfigurationExtension'
                      else 'конфигурации') + ': '
                   + (props.get('version') or 'в файле не указана'))
        if props.get('name_prefix'):
            out.append(f'Префикс имён расширения: {props["name_prefix"]}')
        out.append('Режим совместимости: '
                   + (props.get('compatibility') or 'не определён'))
        out.append('Версия платформы: в файле конфигурации не хранится '
                   '(см. режим совместимости)')
        if props.get('obj_version'):
            out.append(f'Формат метаданных (obj_version): {props["obj_version"]}')
        src = self.conn(db).execute(
            'SELECT file, created, root_uuid FROM source '
            'ORDER BY id LIMIT 1').fetchone()
        if src:
            if src[0]:
                out.append(f'Источник выгрузки: {src[0]}')
            if src[1]:
                out.append(f'База знаний собрана: {src[1]}')
            if src[2]:
                out.append(f'UUID корня: {src[2]}')
        nobj, nmod, nmeth = self.db_stats(db)
        nskd = self.conn(db).execute(
            'SELECT COUNT(*) FROM skd_query').fetchone()[0]
        out.append(f'Состав: объектов {nobj}, модулей {nmod}, методов {nmeth}, '
                   f'запросов СКД {nskd}')
        return '\n'.join(out)

    # -- сравнение двух баз --------------------------------------------------
    def compare_object(self, path, db_left, db_right, path_right=None):
        """Различия одного объекта в двух базах знаний."""
        left_alias = self._alias(db_left)
        right_alias = self._alias(db_right)
        left_path = self.resolve_path(path, left_alias)
        right_path = (self.resolve_path(path_right, right_alias)
                      if path_right else left_path)
        left = compare.object_snapshot(self.conn(left_alias), left_path)
        right = compare.object_snapshot(self.conn(right_alias), right_path)
        title = ru_path(left_path if left else right_path) or path
        head = [f'Сравнение объекта: {title}',
                f'  {left_alias}: {self.dbs[left_alias]["path"]} '
                f'(путь {left_path})',
                f'  {right_alias}: {self.dbs[right_alias]["path"]} '
                f'(путь {right_path})']
        if left is None and right is None:
            head.append(f'объект не найден ни в базе «{left_alias}», '
                        f'ни в базе «{right_alias}»')
            return '\n'.join(head)
        if left is None:
            head.append(f'объект есть только в базе «{right_alias}»; '
                        f'в базе «{left_alias}» его нет')
            return '\n'.join(head)
        if right is None:
            head.append(f'объект есть только в базе «{left_alias}»; '
                        f'в базе «{right_alias}» его нет')
            return '\n'.join(head)
        head.append(
            f'Состав ({left_alias} / {right_alias}): реквизитов и полей '
            f'{len(left["attrs"])} / {len(right["attrs"])}, методов '
            f'{len(left["methods"])} / {len(right["methods"])}, модулей '
            f'{len(left["modules"])} / {len(right["modules"])}, запросов СКД '
            f'{len(left["skd"])} / {len(right["skd"])}')
        lines = compare.diff_snapshots(left, right, left_alias, right_alias,
                                       fmt_type=ru_type_str)
        head.append('Различий нет: метаданные и код объекта совпадают'
                    if not lines else 'Различия:')
        return '\n'.join(head + lines)

    def extension_diff(self, extension_db, base_db, limit=30):
        """Что делает расширение относительно основной конфигурации."""
        ext_alias = self._alias(extension_db)
        base_alias = self._alias(base_db)
        ext = self.cfg_props(ext_alias)
        base = self.cfg_props(base_alias)
        out = []
        name = ext.get('name') or ext_alias
        extra = [text for text in (
            f'версия {ext["version"]}' if ext.get('version') else None,
            f'режим совместимости {ext["compatibility"]}'
            if ext.get('compatibility') else None) if text]
        out.append(f'Расширение: {name}'
                   + (' (' + '; '.join(extra) + ')' if extra else ''))
        if ext.get('name_prefix'):
            out.append(f'Префикс имён новых объектов: {ext["name_prefix"]}')
        out.append(f'  база {ext_alias}: {self.dbs[ext_alias]["path"]}')
        base_name = base.get('name') or base_alias
        base_ver = f' {base["version"]}' if base.get('version') else ''
        out.append(f'Основная конфигурация: {base_name}{base_ver}')
        out.append(f'  база {base_alias}: {self.dbs[base_alias]["path"]}')
        if ext.get('root_type') != 'ConfigurationExtension':
            out.append(f'Внимание: корень базы {ext_alias} — '
                       f'{ext.get("root_type_ru") or ext.get("root_type")}, '
                       'а не расширение конфигурации; отчёт показывает '
                       'различия двух баз как есть')
        out.append('')
        out.extend(compare.extension_report(
            self.conn(ext_alias), self.conn(base_alias), fmt=ru_path,
            prefix=ext.get('name_prefix'), limit=int(limit),
            fmt_type=ru_type_str))
        return '\n'.join(out)

    def object_tree(self, path='', depth=2, db=None):
        path = self.resolve_path(path, db)
        rows = self.conn(db).execute(
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
                out.append('  ' * lvl + f'{ru_path(p)} — {ru}')
                walk(cid, lvl + 1)

        out.append(f'{ru_path(path) or "(корень)"} — {ids[root_id][1]}')
        walk(root_id, 1)
        return '\n'.join(out)

    def find_field(self, name, limit=20, db=None):
        like = f'%{name}%'
        like_ns = f'%{name.replace(" ", "").lower()}%'
        rows = self.conn(db).execute(
            'SELECT o.path, a.name, a.tabular, a.type_str FROM meta_attribute a '
            'JOIN meta_object o ON o.id=a.object_id '
            "WHERE a.name LIKE ? OR lower_ru(REPLACE(a.name, ' ', '')) LIKE ? "
            'ORDER BY o.path LIMIT ?',
            (like, like_ns, int(limit))).fetchall()
        if not rows:
            return 'ничего не найдено'
        return '\n'.join(
            f'{ru_path(p)} :: поле {n} ({ru_type_str(t) or "?"})' +
            (f' [табчасть {s}]' if s else '')
            for p, n, s, t in rows)

    def refs_of(self, path, direction='both', limit=30, db=None):
        path = self.resolve_path(path, db)
        q = self.conn(db).execute
        out = []
        if direction in ('both', 'forward'):
            rows = q(
                'SELECT DISTINCT t.path FROM attribute_ref r '
                'JOIN meta_attribute a ON a.id=r.attribute_id '
                'JOIN meta_object v ON v.id=a.object_id '
                'JOIN meta_object t ON t.id=r.object_id '
                'WHERE v.path=? AND t.path IS NOT NULL LIMIT ?',
                (path, int(limit))).fetchall()
            out.append('Ссылается на: ' + (', '.join(ru_path(r[0]) for r in rows)
                       if rows else '—'))
        if direction in ('both', 'reverse'):
            rows = q(
                'SELECT DISTINCT v.path FROM attribute_ref r '
                'JOIN meta_attribute a ON a.id=r.attribute_id '
                'JOIN meta_object v ON v.id=a.object_id '
                'JOIN meta_object t ON t.id=r.object_id '
                'WHERE t.path=? LIMIT ?', (path, int(limit))).fetchall()
            out.append('На него ссылаются: ' + (', '.join(ru_path(r[0]) for r in rows)
                       if rows else '—'))
        return '\n'.join(out)

    def module_outline(self, path, code_name='obj', db=None):
        path = self.resolve_path(path, db)
        row = self.conn(db).execute(
            'SELECT m.body FROM module m JOIN meta_object o ON o.id=m.object_id '
            'WHERE o.path=? AND m.code_name=?', (path, code_name)).fetchone()
        if not row or not row[0]:
            return f'модуль не найден: {path} ({code_name})'
        return row[0]

    def _method_row(self, path, code_name, name, db=None):
        """Строка метода: kind, name, signature, directives, description, body,
        is_export, контекст модуля, line_start — либо None."""
        path = self.resolve_path(path, db)
        return self.conn(db).execute(
            'SELECT mt.kind, mt.name, mt.signature, mt.directives, '
            'mt.description, mt.body, mt.is_export, m.context, mt.line_start '
            'FROM method mt JOIN module m ON m.id=mt.module_id '
            'JOIN meta_object o ON o.id=m.object_id '
            'WHERE o.path=? AND m.code_name=? AND LOWER(mt.name)=LOWER(?)',
            (path, code_name, name)).fetchone()

    def bsl_ctx(self, db=None):
        """Контекст анализа BSL базы (кэш: строится секунды на большой базе)."""
        alias = self._alias(db)
        info = self.dbs[alias]
        if info.get('bsl') is None:
            from .bsl_analyzer import BslContext
            info['bsl'] = BslContext(info['conn'])
        return info['bsl']

    def other_aliases(self, db=None):
        """Алиасы остальных открытых баз, кроме указанной/активной.

        Расширение и внешняя обработка обращаются к объектам основной
        конфигурации, которых в их собственной базе нет. Когда основная база
        открыта рядом, искать надо и в ней — иначе ответ «объект не найден»
        уводит модель в ручную проверку.
        """
        alias = self._alias(db)
        return [a for a in self.dbs if a != alias]

    def foreign_manager(self, db, manager, name):
        """(алиас, путь) — объект метаданных, найденный в другой открытой базе."""
        for alias in self.other_aliases(db):
            target = self.bsl_ctx(alias).resolve_manager(manager, name)
            if target is not None:
                return alias, target
        return None, None

    def foreign_module(self, db, module, method):
        """Пометка о общем модуле, который живёт в другой открытой базе."""
        for alias in self.other_aliases(db):
            ctx = self.bsl_ctx(alias)
            found = ctx.common_module(module)
            if found is None:
                continue
            row = ctx.common_method(found['name'], method)
            if row is None:
                return (f'общий модуль есть в базе «{alias}», но метода '
                        f'{method} в нём нет')
            state = ('Экспорт' if row[0]
                     else 'не Экспорт — вызов извне не работает')
            return f'общий модуль в базе «{alias}»: {method} — {state}'
        return None

    def get_method(self, path, code_name, name, db=None):
        row = self._method_row(path, code_name, name, db)
        if not row:
            path = self.resolve_path(path, db)
            return f'метод не найден: {path} ({code_name}) :: {name}'
        head = f'{row[0]} {row[1]}({row[2]})' + (' Экспорт' if row[6] else '')
        parts = [head]
        if row[3]:
            parts.append('директивы: ' + row[3])
        if row[4]:
            parts.append('описание:\n' + row[4])
        parts.append('тело:\n' + row[5])
        return '\n'.join(parts)

    def find_method_context(self, path, code_name, name, match='', before=20,
                            after=20, db=None):
        """Фрагмент тела метода вокруг вхождения match + маркеры точки вставки."""
        row = self._method_row(path, code_name, name, db)
        if not row:
            return (f'метод не найден: {self.resolve_path(path, db)} '
                    f'({code_name}) :: {name}')
        lines = (row[5] or '').splitlines()
        start = row[8] or 1
        before, after = max(0, int(before)), max(0, int(after))
        out = [f'{row[0]} {row[1]}({row[2]}) — строки модуля '
               f'{start}..{start + len(lines) - 1}'
               + (f'; директивы {row[3]}' if row[3] else '')]
        needle = (match or '').strip()
        if needle:
            hits = [i for i, line in enumerate(lines)
                    if needle.lower() in line.lower()]
            if not hits:
                out.append(f'в теле метода не найдено: {needle}')
                out.append('Полное тело — get_method; поиск по другим методам '
                           '— find_methods')
                return '\n'.join(out)
        else:
            out.append('match не задан — показано начало тела; передайте match '
                       '(строку или вызов), чтобы получить точку вставки')
            hits = [0]
        for i in hits[:3]:
            low, high = max(0, i - before), min(len(lines), i + after + 1)
            out.append(f'--- вхождение: строка {start + i} '
                       f'(показано {start + low}..{start + high - 1}) ---')
            for j in range(low, high):
                out.append(f'{">>" if j == i else "  "} {start + j}: {lines[j]}')
            prev_op = next((lines[k].strip() for k in range(i - 1, -1, -1)
                            if lines[k].strip()), '')
            next_op = next((lines[k].strip() for k in range(i + 1, len(lines))
                            if lines[k].strip()), '')
            out.append(f'Маркеры вставки у строки {start + i}:')
            out.append(f'  предыдущий оператор: {prev_op or "(начало метода)"}')
            out.append(f'  следующий оператор:  {next_op or "(конец метода)"}')
        if len(hits) > 3:
            out.append(f'… и ещё {len(hits) - 3} вхождений (уточните match)')
        return '\n'.join(out)

    def method_dependencies(self, path, code_name, name, db=None):
        """Что использует метод: общие модули, метаданные, запросы, параметры."""
        from .bsl_analyzer import analyze, caller_context, split_params
        row = self._method_row(path, code_name, name, db)
        if not row:
            return (f'метод не найден: {self.resolve_path(path, db)} '
                    f'({code_name}) :: {name}')
        kind, mname, sig, dirs, _desc, body, exp, mod_ctx, start = row
        params = split_params(sig)
        ctx = self.bsl_ctx(db)
        report = analyze(body or '', ctx, params=params,
                         caller_context=caller_context(dirs, mod_ctx),
                         self_name=mname)
        out = [f'{kind} {mname}({sig})' + (' Экспорт' if exp else '')]
        out.append(f'Контекст: {dirs or "директив нет"}'
                   + (f'; контекст модуля: {mod_ctx}' if mod_ctx else ''))
        out.append(f'Параметры: {", ".join(params) if params else "нет"}')

        out.append('\nОбщие модули и их методы:')
        if not report['modules']:
            out.append('  вызовов общих модулей не найдено')
        for module, method, line, state in report['modules']:
            out.append(f'  строка {start + line - 1}: {module}.{method} — {state}')

        out.append('\nМетаданные в коде:')
        if not report['metadata']:
            out.append('  обращений к менеджерам метаданных не найдено')
        for manager, obj, line, target in report['metadata']:
            if target:
                shown = ru_path(target)
            else:
                alias, other = self.foreign_manager(db, manager, obj)
                if other:
                    shown = ('в этой базе НЕТ — есть в базе '
                             f'«{alias}»: ' + ru_path(other))
                else:
                    shown = 'НЕ НАЙДЕНО в этой базе' + (
                        ' и в открытых рядом' if self.other_aliases(db) else '')
            out.append(f'  строка {start + line - 1}: {manager}.{obj} -> {shown}')

        out.append('\nЗапросы в коде:')
        if not report['queries']:
            out.append('  текстов запросов не найдено')
        had_errors = False
        for line, query, complete, errors, unverified in report['queries']:
            head = f'  строка {start + line - 1}'
            if not complete:
                out.append(f'{head}: запрос собирается по частям — '
                           'проверка парсером пропущена')
                continue
            had_errors = had_errors or bool(errors)
            out.append(f'{head}: ошибок {len(errors)}, '
                       f'непроверенных полей {len(unverified)}')
            for err in errors[:6]:
                out.append(f'    ! {ru_text(err)}')
            for ref in unverified[:6]:
                out.append(f'    ? {ref} (схема параметра-таблицы неизвестна)')
        others = self.other_aliases(db)
        if had_errors and others:
            # запрос расширения может обращаться к таблицам основной конфигурации;
            # контексты двух баз не сливаются, поэтому подсказываем явную проверку
            names = ', '.join(others)
            out.append('  ошибки запросов получены по метаданным ЭТОЙ базы; если '
                       f'запрос про таблицы другой открытой базы ({names}), '
                       'проверьте тот же текст в её контексте: check_query(text, '
                       'db=<алиас>)')
        if report['tables']:
            out.append('  таблицы запросов: ' + ', '.join(report['tables'][:20]))
        if report['fields']:
            out.append('  поля запросов: ' + ', '.join(report['fields'][:30]))

        if report['context_warnings']:
            out.append('\nКонтекст клиент/сервер:')
            out.extend(f'  ! {w}' for w in report['context_warnings'])
        if report['unknown']:
            foreign, unresolved = [], []
            for left, right, line in report['unknown'][:20]:
                note = self.foreign_module(db, left, right)
                item = f'  строка {start + line - 1}: {left}.{right}'
                if note:
                    foreign.append(f'{item} — {note}')
                else:
                    unresolved.append(item)
            if foreign:
                out.append('\nЗависимости из другой открытой базы (в этой их нет '
                           '— так расширение или внешняя обработка обращается '
                           'к основной конфигурации):')
                out.extend(foreign)
            if unresolved:
                out.append('\nНе разрешено (не общий модуль, не менеджер '
                           'метаданных и не локальная переменная — вероятно, '
                           'реквизит формы/объекта или глобальный контекст):')
                out.extend(unresolved)
        if report['plain_calls']:
            out.append('\nВызовы без точки (методы этого модуля или глобальные '
                       'методы платформы — не проверялись): '
                       + ', '.join(report['plain_calls'][:25]))
        return '\n'.join(out)

    def method_result_schema(self, path, code_name, name, db=None):
        """Из чего состоит таблица/структура, которую возвращает метод."""
        from .bsl_analyzer import result_schema
        row = self._method_row(path, code_name, name, db)
        if not row:
            return (f'метод не найден: {self.resolve_path(path, db)} '
                    f'({code_name}) :: {name}')
        kind, mname, sig, _dirs, _desc, body, _exp, _ctx, start = row
        found, notes = result_schema(body or '')
        out = [f'{kind} {mname}({sig})']
        if not found and not notes:
            out.append('Признаков создания таблицы значений, структуры или '
                       'запроса в теле не найдено — состав результата по коду '
                       'не определяется (возможно, он возвращается из другого '
                       'метода или это объект метаданных).')
            return '\n'.join(out)
        by_kind = {}
        for line_kind, column, line in found:
            by_kind.setdefault(line_kind, []).append((column, line))
        for line_kind, items in by_kind.items():
            out.append(f'{line_kind.capitalize()} '
                       f'({len(items)}): ' + '; '.join(
                           f'{c} [стр. {start + ln - 1}]' for c, ln in items[:40]))
        for note in notes:
            out.append(note)
        out.append('Внимание: это эвристика по тексту — имена из переменных и '
                   'колонки, добавленные в цикле или другом методе, сюда не '
                   'попадают. Точный состав даёт только выполнение кода.')
        return '\n'.join(out)

    def find_methods(self, mask='', text='', path=None, limit=20, db=None):
        """Поиск методов: mask — имя/сигнатура/описание, text — подстрока в теле.

        text отвечает на вопрос «где в коде это упоминается» — все обращения к
        объекту, все точки записи регистра, все вызовы общего модуля. По каждому
        совпадению выдаётся номер строки модуля и сама строка, поэтому искать
        дальше инструментом sql не нужно.
        """
        path = self.resolve_path(path, db) if path else None
        limit = max(1, int(limit))
        like = f'%{mask}%'
        like_ns = f'%{mask.replace(" ", "").lower()}%'
        # OR-группа обязана быть в скобках: AND связывается сильнее OR, и без
        # скобок фильтр по объекту применялся только к последней ветке — поиск
        # «в одном объекте» молча возвращал методы всей конфигурации
        cond = ('(mt.name LIKE ? OR mt.signature LIKE ? '
                'OR mt.description LIKE ? '
                "OR lower_ru(REPLACE(mt.name, ' ', '')) LIKE ?)")
        params = [like, like, like, like_ns]
        if text:
            # body_has — своя SQL-функция, регистронезависимая в обе стороны:
            # LIKE сворачивает регистр только для ASCII, а пара LIKE через OR
            # ловила бы лишь тот регистр, в котором игла передана
            cond += ' AND body_has(mt.body, ?)'
            params.append(text.lower())
        sql = ('SELECT o.path, m.code_name, mt.kind, mt.name, mt.signature, '
               'mt.directives, mt.description'
               + (', mt.body, mt.line_start' if text else '')
               + ' FROM method mt '
               'JOIN module m ON m.id=mt.module_id '
               'JOIN meta_object o ON o.id=m.object_id WHERE ' + cond)
        if path:
            sql += ' AND o.path=?'
            params.append(path)
        sql += ' LIMIT ?'
        params.append(limit)
        rows = self.conn(db).execute(sql, params).fetchall()
        if not rows:
            return f'в телах методов ничего не найдено: {text}' if text \
                else 'ничего не найдено'
        out = []
        for row in rows:
            p, code, kind, name, sig, dirs, desc = row[:7]
            line = f'{ru_path(p)} ({code}) — {kind} {name}({sig})'
            if dirs:
                line += f' [{dirs}]'
            if text:
                hits = body_hits(row[7] or '', text, row[8] or 1)
                if hits:
                    line += '\n    ' + '\n    '.join(hits)
            elif desc:
                line += ' | ' + desc.splitlines()[0][:80]
            out.append(line)
        if len(rows) >= limit:
            out.append(f'… показаны первые {limit}; уточните mask/text/path '
                       'или увеличьте limit')
        if text:
            out.append('тело метода целиком — get_method, окно строк вокруг '
                       'нужного вызова — find_method_context')
        return '\n'.join(out)

    def skd_of(self, path, db=None):
        path = self.resolve_path(path, db)
        rows = self.conn(db).execute(
            'SELECT q.query FROM skd_query q JOIN meta_object o '
            'ON o.id=q.object_id WHERE o.path=? ORDER BY q.ord',
            (path,)).fetchall()
        if not rows:
            return f'у объекта нет запросов СКД: {path}'
        return ('\n;\n'.join(r[0] for r in rows))[:20000]

    def find_skd(self, mask, limit=10, db=None):
        rows = self.conn(db).execute(
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
            out.append(f'[{rid}] {ru_path(path)} … {snippet} …')
        return '\n'.join(out)

    def check_query(self, text, db=None):
        from .query_lang import check_query_full
        errs, unverified = check_query_full(text, self.ctx(db))
        parts = []
        if errs:
            parts.append('Ошибки:\n' + '\n'.join(ru_text(e) for e in errs))
        else:
            msg = 'OK: синтаксис корректен, таблицы/поля/цепочки существуют'
            if unverified:
                msg += ('\n\nНепроверенные поля параметров-таблиц '
                        '(схема неизвестна):\n' +
                        '\n'.join(f'  {ref}' for ref in unverified))
            parts.append(msg)
        return '\n'.join(parts)

    def sql(self, query, db=None):
        stripped = _sql_rewrite(query).strip().rstrip(';')
        head = stripped.upper()
        if not (head.startswith('SELECT') or head.startswith('WITH')):
            raise ValueError('разрешены только SELECT/WITH (read-only)')
        if ' LIMIT ' not in head:
            stripped += ' LIMIT 200'
        cur = self.conn(db).execute(stripped)
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

    # -- управление базами ---------------------------------------------------
    def cfg_props(self, alias):
        """Свойства конфигурации базы; разбираются один раз и кэшируются.

        Заголовок корня большой (у УНФ ~4 МБ из-за списка versions), поэтому
        повторный разбор на каждый db_list/configuration_info не делается.
        """
        info = self.dbs[alias]
        if info.get('cfg') is None:
            row = info['conn'].execute(
                'SELECT type, type_ru, name, header_json FROM meta_object '
                'WHERE parent_id IS NULL ORDER BY id LIMIT 1').fetchone()
            props = header_props.config_props(row[3]) if row else {}
            if row:
                props.setdefault('name', row[2])
                props['root_type'] = row[0]
                props['root_type_ru'] = row[1]
            info['cfg'] = props
        return info['cfg']

    def cfg_summary(self, alias):
        """(имя конфигурации, строка версий) базы — коротко для db_list."""
        props = self.cfg_props(alias)
        bits = [props.get('version') or 'версия не указана']
        if props.get('compatibility'):
            bits.append(f'режим совместимости {props["compatibility"]}')
        if props.get('name_prefix'):
            bits.append(f'префикс имён {props["name_prefix"]}')
        return props.get('name') or '?', '; '.join(bits)

    def db_list(self):
        if not self.dbs:
            return 'нет открытых баз — откройте через db_open'
        out = []
        for alias, info in self.dbs.items():
            nobj, nmod, nmeth = self.db_stats(alias)
            mark = '*' if alias == self.active else ' '
            out.append(f'{mark} {alias} — {info["path"]} '
                       f'(объектов: {nobj}, модулей: {nmod}, методов: {nmeth})')
            name, meta = self.cfg_summary(alias)
            out.append(f'    {name}: {meta} (configuration_info — подробно)')
        return 'Открытые базы (* — активная):\n' + '\n'.join(out)

    def db_open(self, path, alias=None):
        alias = self.open_db(path, alias)
        nobj, nmod, nmeth = self.db_stats(alias)
        return (f'база открыта: {alias} — {self.dbs[alias]["path"]} '
                f'(объектов: {nobj}, модулей: {nmod}, методов: {nmeth}); '
                'сделана активной')

    def db_use(self, alias):
        alias = self._alias(alias)
        self.active = alias
        return f'активная база: {alias} — {self.dbs[alias]["path"]}'

    def db_close(self, alias=None):
        alias = self.close_db(alias)
        if not self.dbs:
            return f'база {alias} закрыта; открытых баз не осталось'
        return f'база {alias} закрыта; активная: {self.active}'


# Категории ошибок инструмента: клиенту нужен понятный код, а не «Unknown».
LOCKED_RE = re.compile(r'lock|busy', re.I)
RETRY_DELAY = 0.05


def error_text(err):
    """'ошибка [КАТЕГОРИЯ]: сообщение' — категория по типу исключения.

    Отдельно выделена занятая база (SQLITE_BUSY/locked): она транзитна, и
    вызывающая сторона может повторить запрос.
    """
    if isinstance(err, sqlite3.OperationalError):
        low = str(err).lower()
        if LOCKED_RE.search(low):
            code = 'DB_LOCKED'
        elif 'no such' in low:
            code = 'DB_SCHEMA'
        else:
            code = 'DB_ERROR'
    elif isinstance(err, sqlite3.Error):
        code = 'DB_ERROR'
    elif isinstance(err, TypeError):
        code = 'BAD_ARGS'
    elif isinstance(err, ValueError):
        code = 'BAD_REQUEST'
    elif isinstance(err, OSError):
        code = 'IO_ERROR'
    else:
        code = 'INTERNAL'
    detail = f'{type(err).__name__}: {err}' if code == 'INTERNAL' else str(err)
    return f'ошибка [{code}]: {detail}'


def call_with_retry(fn, *args, **kwargs):
    """Вызов инструмента с повтором, если база оказалась занята."""
    for attempt in range(3):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as err:
            if attempt == 2 or not LOCKED_RE.search(str(err)):
                raise
            time.sleep(RETRY_DELAY * (attempt + 1))


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
        if args.get('db') == '*' and 'db' in self.schema.get('properties', {}):
            # db='*' — выполнить инструмент по всем открытым базам сразу
            parts = []
            for alias, info in server.dbs.items():
                part = call_with_retry(self.fn, server, **dict(args, db=alias))
                parts.append(f'=== база {alias} ({info["path"]}) ===\n{part}')
            if not parts:
                raise ValueError('нет открытых баз — укажите путь в db_open')
            return '\n\n'.join(parts)

        # Обычный запрос к одной базе
        result = call_with_retry(self.fn, server, **args)

        # Если у инструмента есть параметр db — добавляем заголовок с идентификатором базы
        if 'db' in self.schema.get('properties', {}):
            alias = server._alias(args.get('db'))  # разрешает None → активная база
            info = server.dbs[alias]
            return f'=== база {alias} ({info["path"]}) ===\n{result}'

        return result


def _schema(props, required=()):
    return {'type': 'object', 'properties': props, 'required': list(required)}


_STR = {'type': 'string'}
_INT = {'type': 'integer'}
_DB = {'type': 'string',
       'description': 'Alias of the knowledge base to query INSTEAD of the '
                      'active one (see db_list). Omit to use the active base. '
                      "Special value '*': run the tool on EVERY open base at "
                      'once; the answer comes back sectioned per base.'}

TOOLS = [
    Tool('find_objects',
         'Search metadata objects by name or path substring. Returns '
         "configurator-style dotted paths ('Справочник.Имя') with Russian and "
         'English type labels. First step for anything: locate '
         'справочник/документ/регистр by its Russian name.',
         _schema({'mask': _STR, 'type': _STR,
                  'limit': _INT, 'db': _DB}, ('mask',)),
         McpServer.find_objects),
    Tool('object_card',
         "Full 'passport' of one object in a single call: type, header "
         'attributes with types, tabular sections with their fields, modules, '
         'SKD query count, forward/reverse references. Use right after '
         'find_objects.',
         _schema({'path': _STR, 'db': _DB}, ('path',)),
         McpServer.object_card),
    Tool('object_tree',
         "Browse the metadata tree 'as in the configurator' (subsystems, "
         'nested forms/commands). path empty = configuration root.',
         _schema({'path': _STR, 'depth': _INT, 'db': _DB}),
         McpServer.object_tree),
    Tool('find_field',
         'Reverse search: which objects contain a field/tabular-section field '
         'with this name. Use to discover join paths between tables.',
         _schema({'name': _STR, 'limit': _INT, 'db': _DB}, ('name',)),
         McpServer.find_field),
    Tool('refs_of',
         "Reference links of an object via attribute types: forward ('on what "
         "it references') and reverse ('who references it') — impact analysis.",
         _schema({'path': _STR, 'direction': _STR, 'limit': _INT, 'db': _DB},
                 ('path',)),
         McpServer.refs_of),
    Tool('module_outline',
         'Table of contents of a 1C module: signatures, comments, #Если '
         "regions, WITHOUT method bodies. code_name: 'obj' (object module), "
         "'mgr' (manager module) etc. Cheap way to inspect a module.",
         _schema({'path': _STR, 'code_name': _STR, 'db': _DB}, ('path',)),
         McpServer.module_outline),
    Tool('get_method',
         'Full source of one procedure/function: signature, directives '
         '(&НаСервере…), description comment and body. Use after '
         'find_methods/module_outline.',
         _schema({'path': _STR, 'code_name': _STR, 'name': _STR, 'db': _DB},
                 ('path', 'code_name', 'name')),
         McpServer.get_method),
    Tool('find_method_context',
         'A WINDOW into a method body instead of the whole body: lines around '
         'each occurrence of match, with the module line numbers, plus stable '
         'insertion markers (the previous and the next statement). Cheaper '
         'than get_method on a big method and the right way to pick a place '
         'to insert code. before/after = how many lines to show (default 20).',
         _schema({'path': _STR, 'code_name': _STR, 'name': _STR,
                  'match': _STR, 'before': _INT, 'after': _INT, 'db': _DB},
                 ('path', 'code_name', 'name')),
         McpServer.find_method_context),
    Tool('method_dependencies',
         'Static analysis of ONE method: its parameters, the common modules it '
         'calls (and whether those methods exist and are Экспорт), the '
         'metadata it touches (Справочники.Х, Документы.Х…), the tables and '
         'fields of the queries inside it (each query is validated), and what '
         'could not be resolved. When another knowledge base is open — a main '
         'configuration next to an extension or an external data processor — '
         'references missing from this base are looked up in the others too, '
         'and the answer names the base each one was found in instead of just '
         'saying "not found". Use it before porting a customization to '
         'another configuration — it lists everything the code needs there.',
         _schema({'path': _STR, 'code_name': _STR, 'name': _STR, 'db': _DB},
                 ('path', 'code_name', 'name')),
         McpServer.method_dependencies),
    Tool('method_result_schema',
         'Best-effort shape of the value a method RETURNS: columns added with '
         'Колонки.Добавить, keys of Новая Структура, and the result columns of '
         'the queries in the body (aliases / field names). Use it when a stock '
         'function returns a temporary table and you need to know its columns '
         'without guessing. HEURISTIC: names built at runtime are reported as '
         'dynamic, and the answer says what it could not see.',
         _schema({'path': _STR, 'code_name': _STR, 'name': _STR, 'db': _DB},
                 ('path', 'code_name', 'name')),
         McpServer.method_result_schema),
    Tool('find_methods',
         'Search 1C methods. mask = substring of name/signature/description '
         "(e.g. 'ПриПроведении') — reuse existing code instead of inventing. "
         'text = substring inside method BODIES: the way to find EVERY place '
         'that touches something (all writes to a register, all calls of a '
         'common module, all uses of a field) without falling back to sql — '
         'each hit comes with its module line number and the line itself. '
         'mask and text may be combined; path narrows the search to one object '
         '(it is a real filter now). Body search is case-insensitive and scans '
         'every method, so it takes seconds on a large base.',
         _schema({'mask': _STR, 'text': _STR, 'path': _STR, 'limit': _INT,
                  'db': _DB}),
         McpServer.find_methods),
    Tool('skd_of',
         'All SKD (report) queries of an object — the best examples of how '
         'THIS configuration queries its own tables.',
         _schema({'path': _STR, 'db': _DB}, ('path',)),
         McpServer.skd_of),
    Tool('find_skd',
         'Search across all SKD query texts (e.g. a table name like '
         "'РегистрНакопления.Запасы'). Returns snippets around the match.",
         _schema({'mask': _STR, 'limit': _INT, 'db': _DB}, ('mask',)),
         McpServer.find_skd),
    Tool('check_query',
         'Validate a 1C query: syntax (Russian keywords) + existence of '
         'tables/fields/reference chains against this configuration. ALWAYS '
         'run it on a query you wrote before using it.',
         _schema({'text': _STR, 'db': _DB}, ('text',)),
         McpServer.check_query),
    Tool('sql',
         'Read-only SELECT escape hatch for anything not covered by the '
         'dedicated tools. Non-SELECT is rejected; LIMIT 200 enforced.',
         _schema({'query': _STR, 'db': _DB}, ('query',)),
         McpServer.sql),
    Tool('compare_object',
         'Compare ONE metadata object between two open knowledge bases in a '
         'single call: presence, attributes and their types, tabular sections, '
         'register dimensions/resources, forms and commands, modules, methods '
         '(signature, directives, body), SKD queries. Use it for a standard vs '
         'a customized configuration, or for the same object in two releases. '
         'db_left/db_right are aliases from db_list (NOT the db parameter); '
         'path_right is needed only when the object is named differently in '
         'the right base.',
         _schema({'path': _STR, 'db_left': _STR, 'db_right': _STR,
                  'path_right': _STR}, ('path', 'db_left', 'db_right')),
         McpServer.compare_object),
    Tool('extension_diff',
         'What an EXTENSION does relative to the main configuration, in one '
         'call: new objects (marked with the extension name prefix), borrowed '
         'objects, the extension methods inside them with their directives '
         '(&Вместо replaces a stock method, &После/&Перед insert code around '
         'it), attributes the extension adds, and external dependencies '
         '(references to objects living outside the extension). Both arguments '
         'are aliases from db_list.',
         _schema({'extension_db': _STR, 'base_db': _STR, 'limit': _INT},
                 ('extension_db', 'base_db')),
         McpServer.extension_diff),
    Tool('configuration_info',
         'Passport of the knowledge base itself: configuration/extension name '
         'and synonym, its VERSION, compatibility mode (режим совместимости), '
         'extension name prefix, source .cf/.cfe/.epf file and the date the '
         'base was built, object/module/method counts. Call it first when you '
         'need to know WHICH configuration and which release you are looking '
         'at (e.g. before porting code between configurations).',
         _schema({'db': _DB}),
         McpServer.configuration_info),
    Tool('db_list',
         'List the knowledge bases open on this server: alias, file path, '
         'object/module/method counts; * marks the ACTIVE base that the other '
         'tools query by default.',
         _schema({}),
         McpServer.db_list),
    Tool('db_open',
         'Open one more knowledge base file while the server is running '
         '(e.g. an extension or a data processor extracted next to the main '
         'configuration) and make it active. path = path to the .db/.sqlite '
         'file given by the user; alias = optional short name (default: the '
         'file name without extension).',
         _schema({'path': _STR, 'alias': _STR}, ('path',)),
         McpServer.db_open),
    Tool('db_use',
         'Switch the ACTIVE knowledge base — the one all other tools query '
         'when the db parameter is omitted.',
         _schema({'alias': _STR}, ('alias',)),
         McpServer.db_use),
    Tool('db_close',
         'Close a knowledge base. alias omitted = the active one. The other '
         'open bases keep working.',
         _schema({'alias': _STR}),
         McpServer.db_close),
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
            if path.path in ('/sse', '/mcp'):
                return self._sse_stream()
            return self._send(404, {'error': f'not found: {path.path}'})

        def do_DELETE(self):
            self._send(405, {'error': 'сессии не сохраняются'},
                       extra={'Allow': 'GET, POST'})

        def _sse_stream(self):
            # endpoint-событие нужно legacy SSE-клиентам; streamable-клиенты
            # (POST /mcp) по спецификации игнорируют неизвестные события
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


def _scan_roots():
    """Каталоги автопоиска базы: текущий и корень установки (при запуске из venv)."""
    roots = [os.getcwd()]
    if sys.prefix != getattr(sys, 'base_prefix', sys.prefix):
        # venv: два уровня вверх от python.exe — корень установки с bat-обёртками
        roots.append(os.path.dirname(os.path.dirname(
            os.path.dirname(sys.executable))))
    return list(dict.fromkeys(roots))


def find_db_candidates():
    """Базы .db/.sqlite в типовых местах: корень, db/, _out/ (без рекурсии)."""
    found = []
    for root in _scan_roots():
        for sub in ('', 'db', '_out'):
            directory = os.path.join(root, sub) if sub else root
            if not os.path.isdir(directory):
                continue
            for pattern in ('*.db', '*.sqlite'):
                for path in sorted(glob.glob(os.path.join(directory, pattern))):
                    path = os.path.abspath(path)
                    if os.path.isfile(path) and path not in found:
                        found.append(path)
    return found


def resolve_db(db):
    """Проверяет явный путь либо сам ищет базу; SystemExit(2), если не нашёл."""
    if db:
        if os.path.isfile(db):
            return db
        print(f'Файл базы не найден: {db}', file=sys.stderr)
        print('Укажите существующий путь к базе SQLite.', file=sys.stderr)
        raise SystemExit(2)
    return _find_single_db()


def resolve_dbs(dbs):
    """Список баз к открытию: проверяет явные пути; без путей — автопоиск одной."""
    if not dbs:
        return [_find_single_db()]
    resolved = []
    for path in dbs:
        if not os.path.isfile(path):
            print(f'Файл базы не найден: {path}', file=sys.stderr)
            print('Укажите существующий путь к базе SQLite.', file=sys.stderr)
            raise SystemExit(2)
        resolved.append(path)
    return resolved


def _find_single_db():
    """last_db из конфига либо автопоиск единственной базы; SystemExit(2)."""
    last = load_config().get('last_db') or ''
    if last and os.path.isfile(last):
        print(f'База из ~/.confdb/config.json (last_db): {last}', file=sys.stderr)
        return last
    found = find_db_candidates()
    if len(found) == 1:
        print(f'База найдена автоматически: {found[0]}', file=sys.stderr)
        return found[0]
    if found:
        print('Найдено несколько баз — укажите путь явно:', file=sys.stderr)
        for path in found:
            print(f'  {path}', file=sys.stderr)
    else:
        print('База SQLite не найдена. Положите файл .db/.sqlite в текущий '
              'каталог (или в db/, _out/) либо укажите путь явно:',
              file=sys.stderr)
    print('Пример: 1confdb-knw база.sqlite', file=sys.stderr)
    raise SystemExit(2)


def _print_dbs(server):
    """Сообщает открытые базы и алиасы (в stderr — не в поток протокола)."""
    for alias, info in server.dbs.items():
        mark = '*' if alias == server.active else ' '
        print(f' {mark} база {alias}: {info["path"]}', file=sys.stderr)


def serve_http(db_paths, host='127.0.0.1', port=8765):
    if isinstance(db_paths, str):
        db_paths = [db_paths]
    for path in db_paths:
        if not os.path.isfile(path):
            print(f'Файл базы не найден: {path}', file=sys.stderr)
            return 2
    server = McpServer(db_paths)
    _print_dbs(server)
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
    # stdio-транспорт MCP обязан быть UTF-8; на Windows в пайпе stdout/stdin
    # по умолчанию cp1251 — клиенты (Claude Code и др.) получали кракозябры
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding='utf-8')
        except Exception:  # noqa: BLE001
            pass
    parser = argparse.ArgumentParser(
        prog='1confdb-knw',
        description='MCP-сервер знаний по конфигурации 1С и BSL '
                    '(stdio по умолчанию; --port — HTTP для SSH-туннеля). '
                    'Можно открыть несколько баз сразу (основная конфигурация '
                    '+ расширения/обработки) — остальные через db_open на ходу.')
    parser.add_argument(
        'db', nargs='*', default=None,
        help='пути к базам SQLite (можно несколько); без путей — last_db из '
             '~/.confdb/config.json или автопоиск *.db/*.sqlite '
             '(текущий каталог, db/, _out/)')
    parser.add_argument('--host', default='127.0.0.1',
                        help='адрес для HTTP-режима (по умолчанию 127.0.0.1)')
    parser.add_argument('--port', type=int, default=0,
                        help='порт HTTP-режима (без него — stdio)')
    args = parser.parse_args(argv)
    dbs = resolve_dbs(args.db)
    if args.port:
        return serve_http(dbs, args.host, args.port)
    server = McpServer(dbs)
    _print_dbs(server)
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
