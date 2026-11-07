"""Контроль качества собранной базы синтакс-помощника (syntax.db).

Проверяет целостность дерева, полноту полей, связи, FTS и точечные
эталоны. Числовые пороги и эталонные темы (НачатьТранзакцию, поле Ссылка)
настроены на ПОЛНУЮ базу из shcntx_ru.hbk (~25300 тем); для малых баз
(тестовые фикстуры) вызывайте с full=False — останутся только структурные
проверки (состав, дерево, наполненность, параметры, FTS).

Возвращает список проваленных проверок (пустой = всё в порядке).
"""
import sqlite3


def check_syntax_db(db_path, out=print, full=True):
    """Прогнать проверки базы; out(line) — печать протокола.

    Возвращает список имён проваленных проверок.
    """
    if out is None:
        out = lambda line: None  # noqa: E731
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    fails = []

    def check(name, ok, detail=''):
        out(f'  [{"OK" if ok else "FAIL"}] {name}' + (f': {detail}' if detail else ''))
        if not ok:
            fails.append(name)

    out('1. Состав:')
    total = cur.execute('SELECT COUNT(*) FROM topics').fetchone()[0]
    kinds = cur.execute(
        'SELECT kind, COUNT(*) FROM topics GROUP BY kind ORDER BY 2 DESC').fetchall()
    out(f'  всего тем: {total}')
    for k, c in kinds:
        out(f'    {k:<12} {c}')
    check('корней ровно 2',
          cur.execute("SELECT COUNT(*) FROM topics WHERE kind='root'").fetchone()[0] == 2)

    out('2. Дерево:')
    orphans = cur.execute(
        "SELECT COUNT(*) FROM topics WHERE parent_id IS NULL AND kind!='root'"
    ).fetchone()[0]
    check('нет тем без родителя (кроме корней)', orphans == 0, f'сирот: {orphans}')
    dangling = cur.execute("""
        SELECT COUNT(*) FROM topics t LEFT JOIN topics p ON t.parent_id=p.id
        WHERE t.parent_id IS NOT NULL AND p.id IS NULL""").fetchone()[0]
    check('все parent_id указывают на существующие темы', dangling == 0)
    selfref = cur.execute(
        'SELECT COUNT(*) FROM topics WHERE parent_id=id').fetchone()[0]
    check('нет самоссылок', selfref == 0)

    out('3. Наполненность:')
    members = "('method','property','event','ctor','formparam','field','param')"
    n_members = cur.execute(f'SELECT COUNT(*) FROM topics WHERE kind IN {members}').fetchone()[0]
    n_sig = cur.execute(
        f'SELECT COUNT(*) FROM topics WHERE kind IN {members} AND signature_ru IS NOT NULL'
    ).fetchone()[0]
    check('сигнатуры .st у членов типов', n_sig >= n_members - 40,
          f'{n_sig} из {n_members}')
    n_m = cur.execute("SELECT COUNT(*) FROM topics WHERE kind='method'").fetchone()[0]
    n_m_syn = cur.execute(
        "SELECT COUNT(*) FROM topics WHERE kind='method' AND syntax IS NOT NULL").fetchone()[0]
    check('синтаксис у методов', n_m == 0 or n_m_syn >= n_m * 0.95, f'{n_m_syn} из {n_m}')
    n_m_av = cur.execute(
        "SELECT COUNT(*) FROM topics WHERE kind='method' AND"
        ' (av_server OR av_thick_client OR av_thin_client OR av_web_client OR'
        '  av_external_connection OR av_mobile_client OR av_mobile_app_client OR'
        '  av_mobile_app_server OR av_mobile_autonomous_server OR av_integration)'
    ).fetchone()[0]
    check('флаги доступности у всех методов', n_m_av == n_m, f'{n_m_av} из {n_m}')
    n_desc = cur.execute(
        'SELECT COUNT(*) FROM topics WHERE description IS NOT NULL').fetchone()[0]
    # синтетические корни («Объекты»/«Таблицы») описания не имеют — порог
    # считается по остальным темам
    n_roots = cur.execute(
        "SELECT COUNT(*) FROM topics WHERE kind='root'").fetchone()[0]
    check('описание у большинства тем', n_desc >= (total - n_roots) * 0.9,
          f'{n_desc} из {total - n_roots}')
    n_names = cur.execute(
        'SELECT COUNT(*) FROM topics WHERE name_ru IS NOT NULL').fetchone()[0]
    check('имена у всех тем', n_names == total, f'{n_names} из {total}')

    out('4. Параметры:')
    n_p = cur.execute('SELECT COUNT(*) FROM params').fetchone()[0]
    n_pt = cur.execute('SELECT COUNT(*) FROM params WHERE type_id IS NOT NULL').fetchone()[0]
    n_fb = cur.execute(
        'SELECT COUNT(*) FROM params WHERE name IS NULL').fetchone()[0]
    out(f'  всего: {n_p}, с типом-ссылкой: {n_pt}, fallback-строк: {n_fb}')
    check('параметры разобраны по именам', n_fb <= 50, f'fallback: {n_fb}')

    out('5. Ссылки:')
    n_l = cur.execute('SELECT COUNT(*) FROM links').fetchone()[0]
    n_lb = cur.execute(
        "SELECT COUNT(*) FROM links WHERE dest_id IS NULL AND href LIKE '%.html'"
    ).fetchone()[0]
    n_lang = cur.execute(
        "SELECT COUNT(*) FROM links WHERE href LIKE 'v8help://SyntaxHelperLanguage/%'"
    ).fetchone()[0]
    n_see = cur.execute("SELECT COUNT(*) FROM links WHERE kind='see'").fetchone()[0]
    n_type = cur.execute("SELECT COUNT(*) FROM links WHERE kind='type'").fetchone()[0]
    out(f'  всего: {n_l}, битых внутренних: {n_lb}, на книгу языка: {n_lang}, '
        f'see: {n_see}, type: {n_type}')
    check('битых внутренних ссылок мало', n_lb <= 100)
    if full:
        check('ссылки на типы разрешаются', n_type > 1000, f'type-ссылок: {n_type}')

    if full:
        out('6. Пример: метод НачатьТранзакцию')
        row = cur.execute("""
            SELECT t.id, t.name_ru, t.name_en, t.kind, t.since,
                   t.syntax, t.signature_ru, t.returns, p.id AS parent_id, p.name_ru
            FROM topics t JOIN topics p ON t.parent_id=p.id
            WHERE t.path='objects/Global context/methods/catalog8/BeginTransaction9.html'
        """).fetchone()
        check('тема найдена', row is not None)
        if row:
            tid, nru, nen, kind, since, syntax, sig, rets, pid, pname = row
            out(f'    {nru} ({nen}), вид={kind}, родитель={pname!r}, seit={since!r}')
            out(f'    синтаксис: {syntax!r}')
            out(f'    сигнатура: {sig!r}')
            av = cur.execute(
                'SELECT av_server, av_thick_client, av_thin_client, av_web_client,'
                ' av_external_connection, av_mobile_client, av_mobile_app_client,'
                ' av_mobile_app_server, av_mobile_autonomous_server, av_integration,'
                ' availability_note FROM topics WHERE id=?', (tid,)).fetchone()
            av_names = ['сервер', 'толстый клиент', 'тонкий клиент', 'веб-клиент',
                        'внешнее соединение', 'мобильный клиент',
                        'мобильное приложение (клиент)',
                        'мобильное приложение (сервер)',
                        'мобильный автономный сервер', 'интеграция']
            avail = [n for n, v in zip(av_names, av) if v]
            out(f'    доступность: {avail} примечание={av[-1]!r}')
            check('у метода есть флаги доступности', len(avail) > 0)
            params = cur.execute(
                'SELECT name, optional, type_text, type_id, description FROM params'
                ' WHERE topic_id=? ORDER BY pos', (tid,)).fetchall()
            for name, opt, tt, ty, d in params:
                out(f'    параметр: {name} обяз={not opt} тип={tt!r}'
                    f' type_id={ty} описание={(d or "")[:60]!r}...')
            check('у метода есть параметр с типом', any(p[3] for p in params))
            chain, cur_id = [], tid
            while cur_id:
                r = cur.execute('SELECT name_ru, parent_id, kind FROM topics WHERE id=?',
                                (cur_id,)).fetchone()
                chain.append(f'{r[0]}[{r[2]}]')
                cur_id = r[1]
            out(f'    цепочка: {" -> ".join(chain)}')
            check('цепочка до корня', chain[-1] in ('Объекты[root]', 'Таблицы[root]')
                  or chain[-1].endswith('[root]'))
            sees = cur.execute("""
                SELECT d.name_ru FROM links l JOIN topics d ON l.dest_id=d.id
                WHERE l.src_id=? AND l.kind='see'""", (tid,)).fetchall()
            out(f'    см. также: {[s[0] for s in sees]}')
            check('есть ссылки «см. также»', len(sees) >= 2)

        out('7. Пример: поле таблицы Ссылка')
        row = cur.execute("""
            SELECT t.name_ru, t.signature_ru, l.dest_id, d.name_ru
            FROM topics t LEFT JOIN links l ON l.src_id=t.id AND l.kind='type'
            LEFT JOIN topics d ON l.dest_id=d.id
            WHERE t.path='tables/table5/fields/Ref23.html'""").fetchone()
        check('поле найдено', row is not None)
        if row:
            out(f'    {row[0]} сигнатура={row[1]!r} тип->{row[3]!r}')
            check('у поля разрешён тип', row[2] is not None)

    out('8. Полнотекстовый поиск:')
    try:
        hits = cur.execute("""
            SELECT t.name_ru, t.kind FROM fts
            JOIN topics t ON t.id=fts.rowid
            WHERE fts MATCH 'транзакци*' ORDER BY fts.rank LIMIT 5""").fetchall()
        out(f'    запрос «транзакци*»: {[f"{h[0]}[{h[1]}]" for h in hits]}')
        check('FTS работает и находит метод',
              any('Транзакцию' in (h[0] or '') for h in hits))
    except sqlite3.OperationalError as e:
        check('FTS работает', False, str(e))

    conn.close()
    out('\n' + ('ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ' if not fails
                else 'ПРОВАЛЕНО: ' + ', '.join(fails)))
    return fails
