"""Сравнение объектов и конфигураций между двумя базами знаний.

Задача — ответить на вопросы «чем доработанная конфигурация отличается от
типовой» и «что именно делает расширение» без ручного сопоставления двух
паспортов объекта. Обе базы открыты read-only; объект сравнивается по снимку
(snapshot): тип, реквизиты с типами, табличные части, группы полей регистра,
модули, методы, вложенные объекты, запросы СКД и состав пакета XDTO.

Тела модулей и методов в снимке хранятся как sha1 — сравнение не тянет
мегабайты кода в память второй раз, а разница «изменилось/не изменилось»
для кода важнее, чем его текст (текст отдаёт get_method).
"""
import difflib
import hashlib
import re
import sqlite3
from collections import namedtuple

from .header_props import REGISTER_KINDS, register_field_kinds

# сколько позиций показывать в одной секции отчёта
MAX_ITEMS = 40

# структурированное различие для пагинации и идентификации
DiffItem = namedtuple('DiffItem', ['id', 'section', 'kind', 'description'])

REGISTER_TYPES = frozenset({
    'InformationRegister', 'AccumulationRegister',
    'AccountingRegister', 'CalculationRegister',
})

# 'Ссылка: Catalog/Х' — цель внутри этой же базы; 'Ссылка: Х' — имя без пути
# (так выглядит ссылка на объект основной конфигурации из расширения)
RE_REF = re.compile(r'Ссылка:\s*([^|()]+)')


def sha1(text):
    return hashlib.sha1((text or '').encode('utf-8')).hexdigest()


def _xdto_snapshot(conn, oid):
    """{имя типа: (базовый тип, отпечаток свойств)} — состав пакета XDTO.

    Сравниваются только именованные типы: свойства вложенного анонимного типа
    входят в отпечаток свойства-владельца, поэтому его изменение видно как
    изменение именованного типа, а не как пропавший тип без имени.
    """
    try:
        types = {tid: (name, base, facets, values) for tid, name, base,
                 facets, values in conn.execute(
                     'SELECT id, name, base, facets, enum_values'
                     ' FROM xdto_type WHERE object_id=?', (oid,))}
        rows = conn.execute(
            'SELECT type_id, ord, name, type, lower_bound, upper_bound,'
            ' nillable, form, nested_type_id, extra FROM xdto_property'
            ' WHERE object_id=? ORDER BY type_id, ord', (oid,)).fetchall()
    except sqlite3.OperationalError:
        # база извлечена версией без таблиц xdto_*: отсутствие состава пакета
        # не должно ломать сравнение остальных разделов объекта
        return {}
    props = {}
    for row in rows:
        props.setdefault(row[0], []).append(row[2:])

    def signature(tid):
        parts = []
        for pname, ptype, lower, upper, nill, form, nested, extra \
                in props.get(tid, []):
            parts.append(f'{pname}|{ptype}|{lower}|{upper}|{nill}|{form}|{extra}'
                         + (f'|{signature(nested)}' if nested else ''))
        return sha1(';'.join(parts))

    out = {name: (base or '', sha1(f'{facets}|{values}|{signature(tid)}'))
           for tid, (name, base, facets, values) in types.items() if name}
    if props.get(None):
        # свойства, объявленные в самом пакете вне типов
        out['(свойства пакета)'] = ('', signature(None))
    return out


def object_snapshot(conn, path):
    """Снимок объекта для сравнения; None, если объекта в базе нет."""
    row = conn.execute(
        'SELECT id, type, type_ru, name, comment, header_json '
        'FROM meta_object WHERE path=?', (path,)).fetchone()
    if not row:
        return None
    oid, obj_type, type_ru, name, comment, header_json = row
    snap = {
        'type': obj_type, 'type_ru': type_ru, 'name': name,
        'comment': comment or '',
        'kinds': register_field_kinds(header_json)
        if obj_type in REGISTER_TYPES else {},
        'attrs': {n: (t or '', tab or '') for n, t, tab in conn.execute(
            'SELECT name, type_str, tabular FROM meta_attribute '
            'WHERE object_id=?', (oid,))},
        'tabular': [n for n, in conn.execute(
            'SELECT name FROM meta_tabular WHERE object_id=? ORDER BY ord',
            (oid,))],
        'modules': {code: (ctx or '', sha1(body), len(body or ''))
                    for code, ctx, body in conn.execute(
                        'SELECT code_name, context, body FROM module '
                        'WHERE object_id=?', (oid,))},
        'methods': {},
        'children': {n: t for n, t in conn.execute(
            'SELECT name, type FROM meta_object WHERE parent_id=?', (oid,))},
        'skd': [sha1(text) for text, in conn.execute(
            'SELECT query FROM skd_query WHERE object_id=? ORDER BY ord',
            (oid,))],
        'xdto': _xdto_snapshot(conn, oid),
    }
    for code, mname, kind, sig, exp, dirs, body in conn.execute(
            'SELECT m.code_name, t.name, t.kind, t.signature, t.is_export, '
            't.directives, t.body FROM method t '
            'JOIN module m ON m.id=t.module_id WHERE m.object_id=?', (oid,)):
        key = (code, mname.lower())
        snap['methods'][key] = (mname, kind, sig or '', int(exp or 0),
                                dirs or '', sha1(body))
    return snap


def _split(left, right):
    """(только слева, только справа, общие с отличиями) по ключам словарей."""
    only_left = sorted(k for k in left if k not in right)
    only_right = sorted(k for k in right if k not in left)
    changed = sorted(k for k in left if k in right and left[k] != right[k])
    return only_left, only_right, changed


def _list(items, fmt, limit=MAX_ITEMS):
    """Список строк с обрезкой: '…' + сколько ещё не показано."""
    lines = [fmt(k) for k in items[:limit]]
    if len(items) > limit:
        lines.append(f'… и ещё {len(items) - limit}')
    return lines


def _only_in(title):
    """Подпись «только в базе X»: алиас базы произвольный, склонять его нельзя."""
    return f'  только в базе «{title}»: '


def compute_diff(left, right, title_left='слева', title_right='справа'):
    """Вычислить структурированные различия двух снимков (без форматирования).
    
    Возвращает список DiffItem(id, section, kind, description).
    Это разделяет вычисление различий от их отображения (P0-3).
    None-снимки возвращают пустой список (вызывающий код обрабатывает отсутствие).
    """
    if left is None or right is None:
        return []
    items = []
    # тип объекта
    if left['type'] != right['type']:
        items.append(DiffItem('type', 'type', 'changed',
                              f'Тип объекта: {title_left} {left["type_ru"]} ({left["type"]})'
                              f' / {title_right} {right["type_ru"]} ({right["type"]})'))
    # комментарий
    if left['comment'] != right['comment']:
        items.append(DiffItem('comment', 'comment', 'changed',
                              f'Комментарий: {title_left} "{left["comment"]}"'
                              f' / {title_right} "{right["comment"]}"'))
    # реквизиты
    only_l, only_r, changed = _split(left['attrs'], right['attrs'])
    for k in only_l:
        items.append(DiffItem(f'attrs:{k}', 'attrs', 'only_left',
                              f'Реквизит только в {title_left}: {k}'))
    for k in only_r:
        items.append(DiffItem(f'attrs:{k}', 'attrs', 'only_right',
                              f'Реквизит только в {title_right}: {k}'))
    for k in changed:
        items.append(DiffItem(f'attrs:{k}', 'attrs', 'changed',
                              f'Реквизит изменился: {k}'))
    # табличные части
    only_l, only_r, _ = _split({s: 1 for s in left['tabular']},
                               {s: 1 for s in right['tabular']})
    for s in only_l:
        items.append(DiffItem(f'tabular:{s}', 'tabular', 'only_left',
                              f'Табличная часть только в {title_left}: {s}'))
    for s in only_r:
        items.append(DiffItem(f'tabular:{s}', 'tabular', 'only_right',
                              f'Табличная часть только в {title_right}: {s}'))
    # XDTO
    only_l, only_r, changed = _split(left.get('xdto', {}), right.get('xdto', {}))
    for n in only_l:
        items.append(DiffItem(f'xdto:{n}', 'xdto', 'only_left',
                              f'Тип XDTO только в {title_left}: {n}'))
    for n in only_r:
        items.append(DiffItem(f'xdto:{n}', 'xdto', 'only_right',
                              f'Тип XDTO только в {title_right}: {n}'))
    for n in changed:
        items.append(DiffItem(f'xdto:{n}', 'xdto', 'changed',
                              f'Тип XDTO изменился: {n}'))
    # подобъекты
    only_l, only_r, changed = _split(left['children'], right['children'])
    for n in only_l:
        items.append(DiffItem(f'children:{n}', 'children', 'only_left',
                              f'Подобъект только в {title_left}: {n}'))
    for n in only_r:
        items.append(DiffItem(f'children:{n}', 'children', 'only_right',
                              f'Подобъект только в {title_right}: {n}'))
    for n in changed:
        items.append(DiffItem(f'children:{n}', 'children', 'changed',
                              f'Подобъект изменил тип: {n}'))
    # модули
    only_l, only_r, changed = _split(left['modules'], right['modules'])
    for c in only_l:
        items.append(DiffItem(f'modules:{c}', 'modules', 'only_left',
                              f'Модуль только в {title_left}: {c}'))
    for c in only_r:
        items.append(DiffItem(f'modules:{c}', 'modules', 'only_right',
                              f'Модуль только в {title_right}: {c}'))
    for c in changed:
        items.append(DiffItem(f'modules:{c}', 'modules', 'changed',
                              f'Модуль изменился: {c}'))
    # методы
    only_l, only_r, changed = _split(left['methods'], right['methods'])
    for k in only_l:
        items.append(DiffItem(f'methods:{k[0]}:{k[1]}', 'methods', 'only_left',
                              f'Метод только в {title_left}: {k[1]}'))
    for k in only_r:
        items.append(DiffItem(f'methods:{k[0]}:{k[1]}', 'methods', 'only_right',
                              f'Метод только в {title_right}: {k[1]}'))
    for k in changed:
        items.append(DiffItem(f'methods:{k[0]}:{k[1]}', 'methods', 'changed',
                              f'Метод изменился: {k[1]}'))
    # СКД
    if left['skd'] != right['skd']:
        items.append(DiffItem('skd', 'skd', 'changed',
                              f'Запросы СКД: {title_left} {len(left["skd"])}, '
                              f'{title_right} {len(right["skd"])}'))
    return items


def format_diff(items, title_left='слева', title_right='справа',
                limit=100, offset=0):
    """Форматировать структурированные различия в строки с пагинацией (P0-4).
    
    :param items: список DiffItem от compute_diff
    :param limit: максимум элементов для возврата
    :param offset: сколько первых элементов пропустить
    """
    if not items:
        return []
    total = len(items)
    end = min(offset + limit, total)
    page = [item.description for item in items[offset:end]]
    if offset > 0 or end < total:
        shown = end - offset
        remaining = total - end
        page.append(f'… показано {shown} из {total} различий'
                    + (f'; есть ещё {remaining} — следующий вызов с '
                       f'offset={end}' if remaining > 0 else ''))
    return page


def _normalize_bsl_line(line):
    """Нормализовать строку BSL для сравнения: убрать комментарии и пробелы.
    
    Идея из git diff — игнорировать незначимые изменения (whitespace/comments).
    Возвращает нормализованную строку или None, если строка пуста/комментарий.
    """
    stripped = line.strip()
    # пустые строки и чистые комментарии игнорируем
    if not stripped or stripped.startswith('//'):
        return None
    # убираем концевой комментарий (// ...) и пробелы
    code = re.split(r'\s*//', stripped, maxsplit=1)[0].strip()
    # убираем все пробелы для сравнения (как git -w)
    return re.sub(r'\s+', '', code)


def method_diff(body_left, body_right, context=3):
    """Построчное сравнение двух тел метода с игнорированием комментариев и пробелов.
    
    Идея из git diff / difflib — unified diff с контекстом.
    Возвращает список строк различий в удобном для анализа формате.
    
    :param body_left: тело метода слева (строка или None)
    :param body_right: тело метода справа (строка или None)
    :param context: сколько строк контекста показывать вокруг изменений (по умолчанию 3)
    :return: список строк различий (пусто — методы идентичны)
    """
    if body_left is None:
        body_left = ''
    if body_right is None:
        body_right = ''
    
    lines_left = body_left.splitlines()
    lines_right = body_right.splitlines()
    
    # нормализуем строки для сравнения, но сохраняем оригиналы для показа
    norm_left = [_normalize_bsl_line(line) for line in lines_left]
    norm_right = [_normalize_bsl_line(line) for line in lines_right]
    
    # фильтруем None (пустые/комментарии) для сравнения, но сохраняем индексы
    filtered_left = [(i, n) for i, n in enumerate(norm_left) if n is not None]
    filtered_right = [(i, n) for i, n in enumerate(norm_right) if n is not None]
    
    # сравниваем нормализованные строки
    matcher = difflib.SequenceMatcher(
        None,
        [n for _, n in filtered_left],
        [n for _, n in filtered_right]
    )
    
    out = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            continue  # без изменений
        
        # показываем контекст вокруг изменений
        # определяем диапазон строк для показа (с контекстом)
        left_start = max(0, filtered_left[i1][0] - context) if i1 < len(filtered_left) else len(lines_left)
        left_end = min(len(lines_left), (filtered_left[i2-1][0] if i2 > 0 else 0) + context + 1) if i2 > 0 else 0
        right_start = max(0, filtered_right[j1][0] - context) if j1 < len(filtered_right) else len(lines_right)
        right_end = min(len(lines_right), (filtered_right[j2-1][0] if j2 > 0 else 0) + context + 1) if j2 > 0 else 0
        
        # заголовок блока изменений
        out.append(f'@@ -{left_start+1},{left_end-left_start} +{right_start+1},{right_end-right_start} @@')
        
        # показываем строки
        for i in range(left_start, left_end):
            if i1 <= i < i2 and i < len(filtered_left):
                # удалённая строка
                orig_i = filtered_left[i][0]
                out.append(f'-{orig_i+1:4d}: {lines_left[orig_i]}')
            elif any(filtered_left[k][0] == i for k in range(len(filtered_left))):
                # контекстная строка (слева)
                out.append(f' {i+1:4d}: {lines_left[i]}')
        
        for j in range(right_start, right_end):
            if j1 <= j < j2 and j < len(filtered_right):
                # добавленная строка
                orig_j = filtered_right[j][0]
                out.append(f'+{orig_j+1:4d}: {lines_right[orig_j]}')
            elif any(filtered_right[k][0] == j for k in range(len(filtered_right))):
                # контекстная строка (справа)
                out.append(f' {j+1:4d}: {lines_right[j]}')
    
    return out


def config_diff(conn_left, conn_right, title_left='слева', title_right='справа',
                limit=50, offset=0):
    """Сводка различий двух конфигураций (верхнеуровневое сравнение).
    
    Оптимизированная версия: сравнивает по сигнатуре объекта (COUNT модулей/методов)
    через SQL-агрегацию, без загрузки полных снимков. Работает за ~1-2 сек на 40k объектов.
    
    Возвращает словарь со сводкой и списком изменённых объектов.
    Для детального анализа использовать compare_object и method_diff.
    """
    # сводка через SQL-агрегацию (быстро)
    total_attrs_left = conn_left.execute('SELECT COUNT(*) FROM meta_attribute').fetchone()[0]
    total_attrs_right = conn_right.execute('SELECT COUNT(*) FROM meta_attribute').fetchone()[0]
    total_modules_left = conn_left.execute('SELECT COUNT(*) FROM module').fetchone()[0]
    total_modules_right = conn_right.execute('SELECT COUNT(*) FROM module').fetchone()[0]
    total_methods_left = conn_left.execute('SELECT COUNT(*) FROM method').fetchone()[0]
    total_methods_right = conn_right.execute('SELECT COUNT(*) FROM method').fetchone()[0]
    total_skd_left = conn_left.execute('SELECT COUNT(*) FROM skd_query').fetchone()[0]
    total_skd_right = conn_right.execute('SELECT COUNT(*) FROM skd_query').fetchone()[0]
    
    # сигнатура объекта: количество модулей (один простой GROUP BY)
    def object_signature(conn):
        mod_count = dict(conn.execute(
            'SELECT object_id, COUNT(*) FROM module GROUP BY object_id').fetchall())
        sig = {}
        for path, oid in conn.execute('SELECT path, id FROM meta_object').fetchall():
            sig[path] = mod_count.get(oid, 0)
        return sig
    
    sig_left = object_signature(conn_left)
    sig_right = object_signature(conn_right)
    
    paths_left = set(sig_left.keys())
    paths_right = set(sig_right.keys())
    only_left = sorted(paths_left - paths_right)
    only_right = sorted(paths_right - paths_left)
    common = paths_left & paths_right
    
    # найти объекты с изменённой сигнатурой
    changed_objects = []
    for path in common:
        l_mod = sig_left[path]
        r_mod = sig_right[path]
        if l_mod != r_mod:
            type_ru = conn_left.execute(
                'SELECT type_ru FROM meta_object WHERE path=?', (path,)).fetchone()
            type_ru = type_ru[0] if type_ru else '?'
            changed_objects.append({
                'path': path,
                'type_ru': type_ru,
                'modules_diff': abs(l_mod - r_mod),
                'methods_diff': 0,
                'attrs_diff': 0,
                'diffs_count': abs(l_mod - r_mod),
            })
    
    changed_objects.sort(key=lambda x: x['diffs_count'], reverse=True)
    total_changed = len(changed_objects)
    end = min(offset + limit, total_changed)
    page = changed_objects[offset:end]
    
    return {
        'summary': {
            'objects': f'{len(paths_left)} / {len(paths_right)}',
            'attrs': f'{total_attrs_left} / {total_attrs_right}',
            'modules': f'{total_modules_left} / {total_modules_right}',
            'methods': f'{total_methods_left} / {total_methods_right}',
            'skd': f'{total_skd_left} / {total_skd_right}',
            'only_left': len(only_left),
            'only_right': len(only_right),
            'changed': total_changed,
        },
        'changed_objects': page,
        'total_changed': total_changed,
        'offset': offset,
        'limit': limit,
    }


def diff_snapshots(left, right, title_left='слева', title_right='справа',
                   fmt_type=None, limit=100, offset=0):
    """Строки различий двух снимков одного объекта (пусто — объекты равны).

    :param fmt_type: функция показа типа реквизита (сервер передаёт ru_type_str)
    :param limit: максимум строк для возврата (пагинация)
    :param offset: сколько первых строк пропустить (пагинация)
    """
    fmt = fmt_type or (lambda value: value)
    out = []
    if left['type'] != right['type']:
        out.append(f'Тип объекта: {title_left} {left["type_ru"]} ({left["type"]})'
                   f' / {title_right} {right["type_ru"]} ({right["type"]})')
    if left['comment'] != right['comment']:
        out.append(f'Комментарий: {title_left} "{left["comment"]}"'
                   f' / {title_right} "{right["comment"]}"')

    only_l, only_r, changed = _split(left['attrs'], right['attrs'])
    if only_l or only_r or changed:
        out.append(f'Реквизиты и поля табличных частей ({title_left} '
                   f'{len(left["attrs"])}, {title_right} {len(right["attrs"])}):')
        out.extend(_list(only_l, lambda k: _only_in(title_left)
                         + _attr_str(k, left['attrs'][k], left['kinds'], fmt)))
        out.extend(_list(only_r, lambda k: _only_in(title_right)
                         + _attr_str(k, right['attrs'][k], right['kinds'], fmt)))
        for key in changed[:MAX_ITEMS]:
            lt, lb = left['attrs'][key]
            rt, rb = right['attrs'][key]
            bits = []
            if lt != rt:
                bits.append(f'тип {fmt(lt) or "?"} -> {fmt(rt) or "?"}')
            if lb != rb:
                bits.append(f'секция {lb or "—"} -> {rb or "—"}')
            lk, rk = left['kinds'].get(key), right['kinds'].get(key)
            if lk != rk:
                bits.append(f'группа {lk or "?"} -> {rk or "?"}')
            out.append(f'  отличается {key}: ' + '; '.join(bits))
        if len(changed) > MAX_ITEMS:
            out.append(f'  … и ещё {len(changed) - MAX_ITEMS}')

    only_l, only_r, _ = _split({s: 1 for s in left['tabular']},
                               {s: 1 for s in right['tabular']})
    if only_l or only_r:
        out.append('Табличные части:')
        out.extend(_list(only_l, lambda s: _only_in(title_left) + s))
        out.extend(_list(only_r, lambda s: _only_in(title_right) + s))

    only_l, only_r, changed = _split(left.get('xdto', {}), right.get('xdto', {}))
    if only_l or only_r or changed:
        out.append(f'Типы XDTO ({title_left} {len(left.get("xdto", {}))}, '
                   f'{title_right} {len(right.get("xdto", {}))}):')
        out.extend(_list(only_l, lambda n: _only_in(title_left) + n))
        out.extend(_list(only_r, lambda n: _only_in(title_right) + n))
        for name in changed[:MAX_ITEMS]:
            lbase, lsha = left['xdto'][name]
            rbase, rsha = right['xdto'][name]
            bits = []
            if lbase != rbase:
                bits.append(f'базовый тип {lbase or "—"} -> {rbase or "—"}')
            if lsha != rsha:
                bits.append('состав свойств, ограничения или значения изменились')
            out.append(f'  отличается {name}: ' + '; '.join(bits))
        if len(changed) > MAX_ITEMS:
            out.append(f'  … и ещё {len(changed) - MAX_ITEMS}')

    only_l, only_r, changed = _split(left['children'], right['children'])
    if only_l or only_r or changed:
        out.append('Формы, команды и прочие подобъекты:')
        out.extend(_list(only_l, lambda n: _only_in(title_left) + n))
        out.extend(_list(only_r, lambda n: _only_in(title_right) + n))
        out.extend(_list(changed, lambda n: f'  другой тип: {n} '
                                            f'({left["children"][n]} -> '
                                            f'{right["children"][n]})'))

    only_l, only_r, changed = _split(left['modules'], right['modules'])
    if only_l or only_r or changed:
        out.append('Модули:')
        out.extend(_list(only_l, lambda c: _only_in(title_left) + f'{c} '
                                           f'({left["modules"][c][2]} симв.)'))
        out.extend(_list(only_r, lambda c: _only_in(title_right) + f'{c} '
                                           f'({right["modules"][c][2]} симв.)'))
        for code in changed[:MAX_ITEMS]:
            lctx, lsha, llen = left['modules'][code]
            rctx, rsha, rlen = right['modules'][code]
            bits = []
            if lctx != rctx:
                bits.append(f'контекст {lctx or "—"} -> {rctx or "—"}')
            if lsha != rsha:
                bits.append(f'текст изменился ({llen} -> {rlen} симв.)')
            out.append(f'  отличается {code}: ' + '; '.join(bits))

    out.extend(_methods_diff(left, right, title_left, title_right))

    if left['skd'] != right['skd']:
        out.append(f'Запросы СКД: {title_left} {len(left["skd"])}, '
                   f'{title_right} {len(right["skd"])}'
                   + (' (состав или тексты различаются)'
                      if len(left['skd']) == len(right['skd']) else ''))
    # пагинация: обрезаем список строк и добавляем информацию о полноте
    total = len(out)
    if total == 0:
        return out
    end = min(offset + limit, total)
    page = out[offset:end]
    if offset > 0 or end < total:
        shown = end - offset
        remaining = total - end
        page.append(f'… показано {shown} из {total} строк различий'
                    + (f'; есть ещё {remaining} — следующий вызов с '
                       f'offset={end}' if remaining > 0 else ''))
    return page


def _attr_str(name, value, kinds, fmt=lambda value: value):
    tstr, tabular = value
    kind = kinds.get(name)
    return (f'{name}: {fmt(tstr) or "?"}'
            + (f' [табчасть {tabular}]' if tabular else '')
            + (f' ({kind.lower()})' if kind and kind in REGISTER_KINDS else ''))


def _methods_diff(left, right, title_left, title_right):
    only_l, only_r, changed = _split(left['methods'], right['methods'])
    if not (only_l or only_r or changed):
        return []
    out = [f'Методы ({title_left} {len(left["methods"])}, '
           f'{title_right} {len(right["methods"])}):']
    out.extend(_list(only_l, lambda k: _only_in(title_left)
                     + _method_str(k, left['methods'][k])))
    out.extend(_list(only_r, lambda k: _only_in(title_right)
                     + _method_str(k, right['methods'][k])))
    for key in changed[:MAX_ITEMS]:
        lname, lkind, lsig, lexp, ldirs, lsha = left['methods'][key]
        rname, rkind, rsig, rexp, rdirs, rsha = right['methods'][key]
        bits = []
        if lsig != rsig:
            bits.append(f'сигнатура ({lsig}) -> ({rsig})')
        if lkind != rkind:
            bits.append(f'{lkind} -> {rkind}')
        if lexp != rexp:
            bits.append('Экспорт' if rexp else 'без Экспорт')
        if ldirs != rdirs:
            bits.append(f'директивы [{ldirs or "—"}] -> [{rdirs or "—"}]')
        if lsha != rsha:
            bits.append('тело изменилось')
        out.append(f'  отличается {key[0]}.{lname}: ' + '; '.join(bits))
    if len(changed) > MAX_ITEMS:
        out.append(f'  … и ещё {len(changed) - MAX_ITEMS}')
    return out


def _method_str(key, value):
    name, kind, sig, exp, dirs, _ = value
    return (f'{key[0]}::{kind} {name}({sig})'
            + (' Экспорт' if exp else '')
            + (f' [{dirs}]' if dirs else ''))


def object_paths(conn):
    """{path: (type, type_ru, name)} всех объектов базы, кроме корневого."""
    return {path: (typ, ru, name) for path, typ, ru, name in conn.execute(
        "SELECT path, type, type_ru, name FROM meta_object WHERE path <> ''")}


def path_set(conn):
    """Множество путей всех объектов базы, кроме корневого.

    Для основной конфигурации в split_extension нужны только сами пути —
    незачем тянуть type/type_ru/name и строить из них словарь на сотни
    тысяч записей, который потом используется лишь как `p in base`.
    """
    return {path for path, in conn.execute(
        "SELECT path FROM meta_object WHERE path <> ''")}


def external_refs(conn):
    """Имена объектов, на которые ссылается база, но не разрешает в себе.

    В расширении типы реквизитов, указывающие на основную конфигурацию,
    хранятся как 'Ссылка: Имя' без пути: самого объекта в базе расширения нет.
    """
    names = {}
    for tstr, in conn.execute(
            'SELECT DISTINCT type_str FROM meta_attribute '
            "WHERE type_str LIKE '%Ссылка%'"):
        for ref in RE_REF.findall(tstr or ''):
            ref = ref.strip()
            if ref and '/' not in ref and '.' not in ref:
                names.setdefault(ref, 0)
                names[ref] += 1
    return names


def resolve_in_base(conn_base, names, limit=MAX_ITEMS):
    """Куда в основной базе ведут неразрешённые имена: [(имя, путь, тип_ru)]."""
    found = []
    for name in sorted(names):
        row = conn_base.execute(
            'SELECT path, type_ru FROM meta_object WHERE name=? '
            "AND path <> '' AND path NOT LIKE '%/%/%' LIMIT 1",
            (name,)).fetchone()
        found.append((name, row[0] if row else None, row[1] if row else None))
        if len(found) >= limit:
            break
    return found


def extension_methods(conn, path=None):
    """Методы базы: (path, code_name, имя, kind, директивы, Экспорт).

    С path запрос ведётся от meta_object через CROSS JOIN — в SQLite это
    подсказка порядку соединения. Без неё планировщик сканирует всю таблицу
    method и применяет фильтр по пути последним: на базе УНФ (241566 методов)
    это 325 мс на запрос вместо 2,6 мс.
    """
    if path:
        sql = ('SELECT o.path, m.code_name, t.name, t.kind, t.directives, '
               't.is_export FROM meta_object o '
               'CROSS JOIN module m ON m.object_id=o.id '
               'CROSS JOIN method t ON t.module_id=m.id '
               'WHERE o.path=? ORDER BY m.code_name, t.ord')
        return list(conn.execute(sql, (path,)))
    sql = ('SELECT o.path, m.code_name, t.name, t.kind, t.directives, '
           't.is_export FROM method t JOIN module m ON m.id=t.module_id '
           'JOIN meta_object o ON o.id=m.object_id '
           'ORDER BY o.path, m.code_name, t.ord')
    return list(conn.execute(sql))


def method_keys(conn, path):
    """{(code_name, имя в нижнем регистре)} методов объекта.

    CROSS JOIN — см. extension_methods: без него каждый вызов сканирует
    всю таблицу method.
    """
    return {(code, name.lower()) for code, name in conn.execute(
        'SELECT m.code_name, t.name FROM meta_object o '
        'CROSS JOIN module m ON m.object_id=o.id '
        'CROSS JOIN method t ON t.module_id=m.id WHERE o.path=?', (path,))}


def attribute_names(conn, path):
    """Имена реквизитов и полей табличных частей объекта."""
    return {name for name, in conn.execute(
        'SELECT a.name FROM meta_attribute a '
        'JOIN meta_object o ON o.id=a.object_id WHERE o.path=?', (path,))}


def attribute_lines(conn, path):
    """[(имя, тип, табличная часть)] реквизитов объекта в порядке объявления."""
    return list(conn.execute(
        'SELECT a.name, a.type_str, a.tabular FROM meta_attribute a '
        'JOIN meta_object o ON o.id=a.object_id WHERE o.path=? ORDER BY a.ord',
        (path,)))


def split_extension(conn_ext, conn_base):
    """(новые объекты расширения, заимствованные из основной конфигурации).

    Оба списка — словари {path: (type, type_ru, name)}; заимствованным считается
    объект, который есть в основной базе под тем же путём (расширение сохраняет
    имя замещаемого объекта, префикс получают только новые).
    """
    ext = object_paths(conn_ext)
    base = path_set(conn_base)
    new = {p: v for p, v in ext.items() if p not in base}
    borrowed = {p: v for p, v in ext.items() if p in base}
    return new, borrowed


def extension_report(conn_ext, conn_base, fmt=str, prefix=None,
                     limit=MAX_ITEMS, fmt_type=None):
    """Разделы отчёта «что делает расширение»: объекты, методы, реквизиты.

    :param fmt: функция показа пути (сервер передаёт ru_path)
    :param prefix: префикс имён расширения из его заголовка ('Расш1_')
    :param fmt_type: функция показа типа реквизита (сервер передаёт ru_type_str)
    """
    show_type = fmt_type or (lambda value: value)
    # limit уходит в срезы списков: отрицательный отрезал бы последний элемент
    # и одновременно напечатал «… и ещё N»
    limit = max(1, int(limit))
    new, borrowed = split_extension(conn_ext, conn_base)
    out = [f'Новые объекты расширения ({len(new)}):']
    if not new:
        out.append('  нет')
    # считаем по всем новым объектам, а не по показанной странице: иначе
    # «N из M» относится только к первым limit объектам и вводит в заблуждение
    prefixed = sum(1 for _typ, _ru, name in new.values()
                   if prefix and name.startswith(prefix))
    for path in sorted(new)[:limit]:
        typ, type_ru, name = new[path]
        mark = f' [префикс {prefix}]' if prefix and name.startswith(prefix) \
            else ''
        out.append(f'  {fmt(path)} — {type_ru} ({typ}){mark}')
    if len(new) > limit:
        out.append(f'  … и ещё {len(new) - limit}')
    if prefix and new:
        out.append(f'  с префиксом расширения: {prefixed} из {len(new)}')

    out.append(f'\nЗаимствованные объекты ({len(borrowed)}):')
    if not borrowed:
        out.append('  нет')
    for path in sorted(borrowed)[:limit]:
        typ, type_ru, name = borrowed[path]
        out.append(f'  {fmt(path)} — {type_ru} ({typ})')
    if len(borrowed) > limit:
        out.append(f'  … и ещё {len(borrowed) - limit}')

    out.append('\nМетоды расширения (что замещается и куда вставляются вставки):')
    # один проход по методам расширения вместо запроса на каждый объект
    ext_methods = {}
    for row in extension_methods(conn_ext):
        ext_methods.setdefault(row[0], []).append(row)
    changed_objects = 0
    for path in sorted(borrowed):
        rows = ext_methods.get(path) or []
        if not rows:
            continue
        changed_objects += 1
        if changed_objects > limit:
            # потолок по числу объектов: без него отчёт о расширении с тысячами
            # заимствований уходит в контекст модели десятками тысяч строк
            continue
        base_keys = method_keys(conn_base, path)
        out.append(f'  {fmt(path)}:')
        for _, code, name, kind, dirs, exp in rows[:limit]:
            key = (code, name.lower())
            verdict = 'замещает штатный метод' if key in base_keys \
                else 'новый метод (в основной базе такого нет)'
            tail = f' [{dirs}]' if dirs else ''
            out.append(f'    {code}::{kind} {name}(){tail} — {verdict}'
                       + (' Экспорт' if exp else ''))
        if len(rows) > limit:
            out.append(f'    … и ещё {len(rows) - limit}')
    if not changed_objects:
        out.append('  в заимствованных объектах методов нет')
    elif changed_objects > limit:
        out.append(f'  … и ещё {changed_objects - limit} объектов с методами '
                   '(увеличьте limit)')

    out.append('\nРеквизиты и поля, добавленные расширением:')
    added_total = 0
    objects_with_added = 0
    for path in sorted(new) + sorted(borrowed):
        base_attrs = set() if path in new else attribute_names(conn_base, path)
        added = [(n, t, tab) for n, t, tab in attribute_lines(conn_ext, path)
                 if n not in base_attrs]
        if not added:
            continue
        added_total += len(added)
        objects_with_added += 1
        if objects_with_added > limit:
            continue
        out.append(f'  {fmt(path)}:')
        for name, tstr, tab in added[:limit]:
            out.append(f'    + {name}: {show_type(tstr) or "?"}'
                       + (f' [табчасть {tab}]' if tab else ''))
        if len(added) > limit:
            out.append(f'    … и ещё {len(added) - limit}')
    if not added_total:
        out.append('  нет')
    elif objects_with_added > limit:
        out.append(f'  … и ещё {objects_with_added - limit} объектов '
                   f'(всего добавленных полей: {added_total}; увеличьте limit)')

    out.append('\nВнешние зависимости (ссылки на объекты вне расширения):')
    refs = external_refs(conn_ext)
    if not refs:
        out.append('  по типам реквизитов не обнаружены')
    resolved = resolve_in_base(conn_base, refs, limit=limit)
    for name, path, type_ru in resolved:
        if path:
            out.append(f'  {name} -> {fmt(path)} ({type_ru})')
        else:
            out.append(f'  {name} -> в основной базе не найден')
    if len(refs) > len(resolved):
        # без этой строки обрезанный список внешних имён выглядит полным
        out.append(f'  … и ещё {len(refs) - len(resolved)} имён '
                   '(увеличьте limit)')
    return out
