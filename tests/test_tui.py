"""Тесты вспомогательных функций консольного интерфейса."""
import builtins
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from confdb.tui import (Tui, _unquote, cf_files, first_db,  # noqa: E402
                        group_cli_args, load_groups, replace_server_dbs,
                        resolve_launch_groups, scan_cf_sources, schema_tail,
                        shown_dbs)


def test_unquote_stips_surrounding_quotes():
    assert _unquote('"D:\\base\\x.db"') == 'D:\\base\\x.db'
    assert _unquote('  "C:\\tmp"  ') == 'C:\\tmp'


def test_unquote_keeps_plain_paths():
    assert _unquote('D:\\base\\x.db') == 'D:\\base\\x.db'
    assert _unquote('') == ''


def test_load_groups_prefers_groups_over_packs():
    config = {'groups': {'а': ['1.db']}, 'packs': {'б': ['2.db']}}
    assert load_groups(config) == {'а': ['1.db']}


def test_load_groups_migrates_packs_when_no_groups():
    assert load_groups({'packs': {'б': ['2.db']}}) == {'б': ['2.db']}
    assert load_groups({}) == {}


def test_load_groups_skips_bad_entries():
    config = {'groups': {'ок': ['1.db'], 'плохая': 'не список'}}
    assert load_groups(config) == {'ок': ['1.db']}
    assert load_groups({'groups': 'мусор'}) == {}


class _FakeServer:
    """Минимальная копия реестра баз McpServer для проверки перезагрузки."""

    def __init__(self, aliases):
        self.dbs = dict.fromkeys(aliases)
        self.active = aliases[0] if aliases else None
        self.closed = []

    def close_db(self, alias):
        del self.dbs[alias]
        self.closed.append(alias)
        if self.active == alias:
            self.active = next(iter(self.dbs), None)
        return alias

    def open_db(self, path, activate=True):
        if 'битая' in path:
            raise ValueError(f'файл базы не найден: {path}')
        alias = os.path.splitext(os.path.basename(path))[0]
        self.dbs[alias] = {}
        if activate or self.active is None:
            self.active = alias
        return alias


def test_replace_server_dbs_closes_old_and_opens_new():
    server = _FakeServer(['старая'])
    opened, errors = replace_server_dbs(
        server, [os.path.join('x', 'первая.sqlite'),
                 os.path.join('x', 'вторая.db')])
    assert opened == ['первая', 'вторая']
    assert errors == []
    assert server.closed == ['старая']
    assert server.active == 'первая'  # первая открытая — активная


def test_replace_server_dbs_collects_errors():
    server = _FakeServer([])
    opened, errors = replace_server_dbs(
        server, ['битая.db', os.path.join('x', 'целая.sqlite')])
    assert opened == ['целая']
    assert len(errors) == 1 and 'не найден' in errors[0]
    assert server.active == 'целая'


# ---------- выбор пути: номер — пункт, остальной текст — путь ----------

class _Tui(Tui):
    """Tui без чтения ~/.confdb и без поиска баз по диску: список детерминирован."""

    def __init__(self, db_entries=()):
        self.last_db = ''
        self.recent_db = []
        self.db_dirs = []
        self._entries = list(db_entries)

    def _db_candidates(self):
        return list(self._entries)


@pytest.fixture
def tui(monkeypatch):
    monkeypatch.setattr('confdb.tui._cls', lambda: None)
    return _Tui()


def _feed(monkeypatch, answers):
    """Подменяет input(): ответы по очереди, без завершающего вопроса."""
    left = list(answers)
    monkeypatch.setattr(builtins, 'input',
                        lambda prompt='': left.pop(0) if left else '0')


def test_pick_path_number_selects_entry(tui, monkeypatch):
    _feed(monkeypatch, ['2'])
    assert tui._pick_path('Файл', ['D:\\a.cf', 'D:\\b.cfe'], []) == 'D:\\b.cfe'


def test_pick_path_typed_string_is_a_path(tui, monkeypatch):
    # путь можно набрать сразу, без предварительного «p»
    _feed(monkeypatch, ['D:\\cf\\Новая.cf'])
    assert tui._pick_path('Файл', ['D:\\a.cf'], []) == 'D:\\cf\\Новая.cf'


def test_pick_path_typed_path_is_unquoted(tui, monkeypatch):
    _feed(monkeypatch, ['"D:\\cf\\с пробелом.epf"'])
    assert tui._pick_path('Файл', [], []) == 'D:\\cf\\с пробелом.epf'


def test_pick_path_out_of_range_number_is_not_a_path(tui, monkeypatch):
    _feed(monkeypatch, ['9', '0'])
    assert tui._pick_path('Файл', ['D:\\a.cf'], []) is None


def test_pick_path_empty_input_reprompts(tui, monkeypatch):
    # случайный Enter не должен возвращать пустой путь
    _feed(monkeypatch, ['', '1'])
    assert tui._pick_path('Файл', ['D:\\a.cf'], []) == 'D:\\a.cf'


def test_pick_path_commands_still_work(tui, monkeypatch):
    # после перехода на авто-распознавание команды только '0' (отмена) и
    # 'e' (пусто): буква 'p' больше не команда, а путь, набранный вручную
    _feed(monkeypatch, ['0'])
    assert tui._pick_path('Файл', ['D:\\a.cf'], []) is None
    _feed(monkeypatch, ['p'])
    assert tui._pick_path('Файл', [], []) == 'p'


def test_pick_path_empty_option(tui, monkeypatch):
    _feed(monkeypatch, ['e'])
    assert tui._pick_path('Дамп', [], [], allow_empty=True,
                          empty_hint='не сохранять') == ''


def test_pick_db_typed_path_may_not_exist_yet(monkeypatch):
    tui = _Tui(['D:\\a.db', 'D:\\b.db'])
    _feed(monkeypatch, ['D:\\_out\\новая.sqlite'])
    # сюда же задают базу, которую извлечение только создаст
    assert tui._pick_db('База') == 'D:\\_out\\новая.sqlite'


def test_pick_db_number_selects_from_candidates_and_recents(monkeypatch):
    tui = _Tui(['D:\\a.db', 'D:\\b.db'])
    _feed(monkeypatch, ['2'])
    assert tui._pick_db('База') == 'D:\\b.db'


def test_pick_db_empty_input_reprompts(monkeypatch):
    tui = _Tui(['D:\\a.db'])
    _feed(monkeypatch, ['', '1'])
    assert tui._pick_db('База') == 'D:\\a.db'


def test_pick_db_empty_option(monkeypatch):
    tui = _Tui(['D:\\a.db'])
    _feed(monkeypatch, ['e'])
    assert tui._pick_db('База', allow_empty=True, empty_hint='не писать') == ''


# ---------- запуск сервера с несколькими группами ----------

def test_group_cli_args_one_flag_per_base():
    # по флагу на базу: ';' в имени файла Windows допустим, а повтор --group
    # сервер складывает в ту же группу
    assert group_cli_args([('УНФ', ['D:\\a.db', 'D:\\b.db']),
                           ('БП', ['D:\\c.db'])]) == [
        '--group', 'УНФ=D:\\a.db', '--group', 'УНФ=D:\\b.db',
        '--group', 'БП=D:\\c.db']
    assert group_cli_args([]) == [] and group_cli_args(None) == []


def test_first_db_prefers_the_first_group():
    assert first_db(['D:\\отдельная.db'],
                    [('УНФ', ['D:\\a.db', 'D:\\b.db'])]) == 'D:\\a.db'
    assert first_db(['D:\\отдельная.db'], []) == 'D:\\отдельная.db'
    assert first_db([], [('УНФ', [])]) is None


def test_resolve_launch_groups_keeps_order_and_reports_missing(tmp_path):
    good = tmp_path / 'a.db'
    good.write_text('')
    config = {'УНФ': [str(good), str(tmp_path / 'нет.db')],
              'БП': [str(tmp_path / 'тоже-нет.db')]}
    groups, missing = resolve_launch_groups(config, ['УНФ', 'БП'])
    # группа, в которой нет ни одной доступной базы, не запускается
    assert groups == [('УНФ', [str(good)])]
    assert len(missing) == 2 and str(good) not in missing
    assert resolve_launch_groups(config, []) == ([], [])


def _pick(monkeypatch, groups_config, answers, entries=()):
    """Прогоняет _pick_launch_set на файлах-заглушках и заданных ответах."""
    tui = _Tui(list(entries))
    tui.groups = dict(groups_config)
    monkeypatch.setattr('confdb.tui._cls', lambda: None)
    _feed(monkeypatch, answers)
    return tui._pick_launch_set()


def test_pick_launch_set_several_groups(monkeypatch, tmp_path):
    a, b = tmp_path / 'a.db', tmp_path / 'b.db'
    a.write_text(''), b.write_text('')
    dbs, groups, title, missing = _pick(
        monkeypatch, {'УНФ': [str(a)], 'БП': [str(b)]}, ['2', '1', ''])
    assert dbs == [] and missing == []
    # порядок выбора, а не порядок в конфиге: первая группа станет активной
    assert [name for name, _ in groups] == ['БП', 'УНФ']
    assert title == 'группы «БП», «УНФ»'


def test_pick_launch_set_reorders_groups_and_takes_loose_dbs(monkeypatch,
                                                             tmp_path):
    a, b, c = tmp_path / 'a.db', tmp_path / 'b.db', tmp_path / 'c.db'
    for p in (a, b, c):
        p.write_text('')
    # 1 и 2 — отметили обе группы, *2 — сделали второй выбор первым,
    # m + 1 + Enter — добавили отдельную базу, Enter — запустить
    dbs, groups, title, missing = _pick(
        monkeypatch, {'УНФ': [str(a)], 'БП': [str(b), str(c)]},
        ['1', '2', '*2', 'm', '1', '', ''], entries=[str(c)])
    assert [name for name, _ in groups] == ['БП', 'УНФ']
    assert groups[0][1] == [str(b), str(c)]
    assert dbs == [str(c)] and missing == []
    assert title == 'группы «БП», «УНФ»; отдельные базы: c.db'


def test_pick_launch_set_reports_missing_bases(monkeypatch, tmp_path):
    a = tmp_path / 'a.db'
    a.write_text('')
    dbs, groups, title, missing = _pick(
        monkeypatch,
        {'УНФ': [str(a), str(tmp_path / 'нет.db')],
         'Пустая': [str(tmp_path / 'тоже-нет.db')]},
        ['1', '2', ''])
    assert [name for name, _ in groups] == ['УНФ']
    assert groups[0][1] == [str(a)] and len(missing) == 2
    assert title == 'группы «УНФ»'


def test_pick_launch_set_cancel_and_empty_choice(monkeypatch):
    assert _pick(monkeypatch, {}, ['0']) == ([], [], '', [])
    # Enter без выбора не запускает пустой сервер — спрашивает снова
    assert _pick(monkeypatch, {'УНФ': ['D:\\a.db']}, ['', '0']) == (
        [], [], '', [])


def test_pick_launch_set_all_missing_names_them(monkeypatch, capsys, tmp_path):
    # группа отмечена, но файлы её баз исчезли: вместо «ничего не выбрано»
    # экран называет пропавшие пути и сервер не запускает
    gone = str(tmp_path / 'пропала.db')
    assert _pick(monkeypatch, {'qwe': [gone]}, ['1', '']) == ([], [], '', [])
    out = capsys.readouterr().out
    assert 'Базы выбранных групп не найдены' in out
    assert gone in out
    # отсутствующий файл помечен уже в списке групп
    assert 'пропала.db (нет файла)' in out


def test_shown_dbs_marks_missing(tmp_path):
    exists = tmp_path / 'a.db'
    exists.write_text('')
    assert shown_dbs([str(exists), str(tmp_path / 'нет.db')]) == \
        'a.db, нет.db (нет файла)'
    assert shown_dbs([]) == ''


def _real_db(tmp_path_factory, name):
    from confdb.db.writer import write_db

    from test_writer import make_dump
    dump = str(tmp_path_factory.mktemp('dump'))
    make_dump(dump)
    db = str(tmp_path_factory.mktemp('db') / name)
    write_db(dump, db, source_file=name + '.cf')
    return db


def test_apply_config_group_adds_without_touching_other_groups(tmp_path_factory):
    from confdb.mcp_server import McpServer
    first = _real_db(tmp_path_factory, 'первая.sqlite')
    second = _real_db(tmp_path_factory, 'вторая.sqlite')
    server = McpServer([first])
    tui = _Tui()
    name, aliases, errors = tui._apply_config_group(server, 'БП', [second])
    assert (name, aliases, errors) == ('БП', ['вторая'], [])
    # прежняя база осталась открытой и активной
    assert list(server.dbs) == ['первая', 'вторая']
    assert server.active == 'первая' and server.active_group == 'БП'
    # недоступный файл — ошибка, а не обрыв
    name, aliases, errors = tui._apply_config_group(
        server, 'Битая', ['D:\\нет\\такой.db'])
    assert (name, aliases) == ('Битая', []) and len(errors) == 1


def test_apply_config_group_replace_drops_the_rest(tmp_path_factory):
    from confdb.mcp_server import McpServer
    first = _real_db(tmp_path_factory, 'первая.sqlite')
    second = _real_db(tmp_path_factory, 'вторая.sqlite')
    server = McpServer(groups=[('УНФ', [first]), ('БП', [second])])
    tui = _Tui()
    name, aliases, errors = tui._apply_config_group(
        server, 'БП', [second], replace=True)
    assert (name, aliases, errors) == ('БП', ['вторая'], [])
    assert list(server.groups) == ['БП'] and list(server.dbs) == ['вторая']
    assert server.active == 'вторая' and server.active_group == 'БП'


def test_schema_tail_marks_an_outdated_base(tmp_path_factory):
    """В списке баз TUI устаревшая помечена до выбора: сервер её не читает."""
    import sqlite3

    from confdb.db.writer import SCHEMA_VERSION
    from confdb.mcp_server import McpServer

    fresh = _real_db(tmp_path_factory, 'свежая.sqlite')
    stale = _real_db(tmp_path_factory, 'старая.sqlite')
    conn = sqlite3.connect(stale)
    conn.execute(f'PRAGMA user_version={SCHEMA_VERSION - 1}')
    conn.commit()
    conn.close()
    server = McpServer([fresh, stale])
    assert schema_tail(server, 'свежая') == ''
    tail = schema_tail(server, 'старая')
    assert f'схема {SCHEMA_VERSION - 1} — устарела' in tail
    assert f'сервер ждёт {SCHEMA_VERSION}' in tail


def test_compact_db_list_hides_llm_details(tmp_path_factory):
    """Панель запущенного сервера: алиас и путь, без счётчиков/версии/SHA."""
    import sqlite3

    from confdb.db.writer import SCHEMA_VERSION
    from confdb.mcp_server import McpServer
    from confdb.tui import compact_db_list

    fresh = _real_db(tmp_path_factory, 'свежая.sqlite')
    stale = _real_db(tmp_path_factory, 'старая.sqlite')
    conn = sqlite3.connect(stale)
    conn.execute(f'PRAGMA user_version={SCHEMA_VERSION - 1}')
    conn.commit()
    conn.close()
    server = McpServer([fresh, stale])
    text = compact_db_list(server)
    assert f'* свежая — {fresh}' in text          # активная отмечена
    assert f'старая — {stale}' in text
    # отметка об устаревании сохранена — с ней базу видно до запроса к ней
    assert f'схема {SCHEMA_VERSION - 1} — устарела' in text
    # подробности db_list() (они для LLM) на панель не попадают
    for noise in ('объектов:', 'модулей:', 'методов:', 'SHA-256',
                  'configuration_info', 'режим совместимости',
                  f'схема {SCHEMA_VERSION})'):
        assert noise not in text
    assert compact_db_list(McpServer(None)) == 'Нет открытых баз.'


def test_serve_interactive_closes_dbs_on_stop(monkeypatch, tmp_path_factory):
    """HTTP-сервер остановлен из панели — базы ЗАКРЫТЫ, файлы можно пересобрать.

    Регрессия сценария пользователя: сервер выключен, окно confdb-ui открыто —
    sqlite-соединения держали файлы баз (Windows), и extract в тот же файл
    обрывался на удалении/записи.
    """
    from confdb import mcp_server

    db = _real_db(tmp_path_factory, 'серверная.sqlite')

    class _FakeHttpd:
        def serve_forever(self):
            pass

        def shutdown(self):
            pass

        def server_close(self):
            pass

    monkeypatch.setattr(mcp_server, 'start_http_server',
                        lambda server, host, port: (_FakeHttpd(), port))
    monkeypatch.setattr(mcp_server, 'resolve_syntax_db',
                        lambda explicit=None: None)
    monkeypatch.setattr('confdb.tui._cls', lambda: None)
    tui = _Tui()
    # порт → «0» (остановить сервер и выйти из панели) → Enter после остановки
    _feed(monkeypatch, ['8765', '0', ''])
    tui._serve_interactive([db])
    # соединения закрыты: файл базы удаляется (Windows держит его при живом
    # соединении), pidfile убран
    os.remove(db)
    assert not os.path.exists(db + '.server.json')


# ---------- сканирование каталога cf: новые/устаревшие конфигурации ----------

def _source_db(path, src_name, sha, root_name=None, root_type='Configuration',
               extractor_version=None):
    """Минимальная база confdb: только таблица source (для source_info).

    extractor_version по умолчанию — текущая версия (база «актуальна»);
    передача старой версии моделирует базу, собранную прежним распаковщиком.
    """
    import sqlite3
    from confdb import __version__
    if extractor_version is None:
        extractor_version = __version__
    conn = sqlite3.connect(str(path))
    conn.execute('CREATE TABLE source(id INTEGER PRIMARY KEY, file TEXT,'
                 ' created TEXT, file_sha256 TEXT, file_size INTEGER,'
                 ' root_name TEXT, root_type TEXT, extractor_version TEXT)')
    conn.execute('INSERT INTO source(file, created, file_sha256, file_size,'
                 ' root_name, root_type, extractor_version) VALUES (?,?,?,?,?,?,?)',
                 (src_name, '2026-01-01', sha, 1, root_name, root_type,
                  extractor_version))
    conn.commit()
    conn.close()
    return str(path)


def test_cf_files_lists_only_1c_files_sorted(tmp_path):
    for name in ('б.cf', 'а.cfe', 'в.epf', 'г.erf', 'з.txt', 'д.CF'):
        (tmp_path / name).write_bytes(b'x')
    (tmp_path / 'подкаталог').mkdir()
    found = cf_files(str(tmp_path))
    assert [os.path.basename(p) for p in found] == ['а.cfe', 'б.cf', 'в.epf', 'г.erf', 'д.CF']
    assert cf_files(str(tmp_path / 'нет-такого')) == []


def test_scan_cf_sources_statuses(tmp_path, monkeypatch):
    from confdb.extract import file_sha256

    cf_dir = tmp_path / 'cf'
    cf_dir.mkdir()
    out_dir = tmp_path / 'out'
    monkeypatch.setattr('confdb.ui_core.out_defaults',
                        lambda src: (str(out_dir / (os.path.splitext(
                            os.path.basename(src))[0] + '.sqlite')), str(out_dir)))
    act = cf_dir / 'Актуальная.cf'
    old = cf_dir / 'Устаревшая.cf'
    new = cf_dir / 'Новая.cf'
    for f in (act, old, new):
        f.write_bytes(f.name.encode('utf-8'))
    db_act = _source_db(tmp_path / 'act.sqlite', 'Актуальная.cf', file_sha256(str(act)),
                        root_name='ТестоваяКонфигурация')
    db_old = _source_db(tmp_path / 'old.sqlite', 'Устаревшая.cf', 'deadbeef')
    records = scan_cf_sources(str(cf_dir), [db_act, db_old], log=lambda *a: None)
    by_name = {os.path.basename(r['src']): r for r in records}
    assert by_name['Актуальная.cf']['status'] == 'current'
    assert by_name['Актуальная.cf']['db'] == db_act
    # тип файла и имя корневого объекта метаданных — из собранной базы
    assert by_name['Актуальная.cf']['ext'] == 'cf'
    assert by_name['Актуальная.cf']['name'] == 'ТестоваяКонфигурация'
    assert by_name['Актуальная.cf']['root_type'] == 'Configuration'
    assert by_name['Устаревшая.cf']['status'] == 'outdated'
    assert by_name['Устаревшая.cf']['db'] == db_old
    assert by_name['Новая.cf']['status'] == 'new'
    assert by_name['Новая.cf']['db'] == str(out_dir / 'Новая.sqlite')
    assert by_name['Новая.cf']['found'] is None
    assert by_name['Новая.cf']['ext'] == 'cf'
    assert by_name['Новая.cf']['name'] is None      # базы ещё нет — имени нет


def test_scan_cf_sources_matches_by_sha_and_notes_conflict(tmp_path, monkeypatch):
    from confdb.extract import file_sha256

    cf_dir = tmp_path / 'cf'
    cf_dir.mkdir()
    out_dir = tmp_path / 'out'
    out_dir.mkdir()
    monkeypatch.setattr('confdb.ui_core.out_defaults',
                        lambda src: (str(out_dir / (os.path.splitext(
                            os.path.basename(src))[0] + '.sqlite')), str(out_dir)))
    src = cf_dir / 'Конфигурация.cf'
    src.write_bytes(b'data')
    # база из того же файла, но имя исходника в source другое — совпадение по sha
    renamed = _source_db(tmp_path / 'renamed.sqlite', 'ДругоеИмя.cf', file_sha256(str(src)))
    rec = scan_cf_sources(str(cf_dir), [renamed], log=lambda *a: None)[0]
    assert rec['status'] == 'current' and rec['db'] == renamed
    # путь по умолчанию занят чужой базой — новая конфигурация получит пометку
    other = cf_dir / 'Другая.cf'
    other.write_bytes(b'other')
    _source_db(out_dir / 'Другая.sqlite', 'Чужая.cf', 'cafe')
    rec2 = next(r for r in scan_cf_sources(str(cf_dir), [], log=lambda *a: None)
                if os.path.basename(r['src']) == 'Другая.cf')
    assert rec2['status'] == 'new' and 'занят другой базой' in rec2['note']


def test_scan_cf_sources_flags_old_unpacker_version(tmp_path, monkeypatch):
    """Проверка версий распаковщика: тот же файл, но база собрана старой
    версией — статус 'version' (пересборка); текущая версия — 'current'."""
    from confdb.extract import file_sha256
    from confdb import __version__

    cf_dir = tmp_path / 'cf'
    cf_dir.mkdir()
    out_dir = tmp_path / 'out'
    monkeypatch.setattr('confdb.ui_core.out_defaults',
                        lambda src: (str(out_dir / (os.path.splitext(
                            os.path.basename(src))[0] + '.sqlite')), str(out_dir)))
    src = cf_dir / 'Конфигурация.cf'
    src.write_bytes(b'data')
    sha = file_sha256(str(src))
    # база из того же файла, но собрана прежним распаковщиком
    db_old = _source_db(tmp_path / 'old_ver.sqlite', 'Конфигурация.cf', sha,
                        extractor_version='1.0.0')
    rec = scan_cf_sources(str(cf_dir), [db_old], log=lambda *a: None)[0]
    assert rec['status'] == 'version'
    assert rec['db_version'] == '1.0.0'
    assert rec['cur_version'] == __version__
    assert '1.0.0' in rec['note'] and __version__ in rec['note']
    # база текущей версии — актуальна, пересборка не нужна
    db_cur = _source_db(tmp_path / 'cur_ver.sqlite', 'Конфигурация.cf', sha)
    rec2 = scan_cf_sources(str(cf_dir), [db_cur], log=lambda *a: None)[0]
    assert rec2['status'] == 'current'
    assert rec2['db_version'] == __version__


def test_scan_cf_sources_flags_missing_version(tmp_path, monkeypatch):
    """Старая база без колонки extractor_version (версия неизвестна) — пересборка."""
    import sqlite3
    from confdb.extract import file_sha256

    cf_dir = tmp_path / 'cf'
    cf_dir.mkdir()
    out_dir = tmp_path / 'out'
    monkeypatch.setattr('confdb.ui_core.out_defaults',
                        lambda src: (str(out_dir / (os.path.splitext(
                            os.path.basename(src))[0] + '.sqlite')), str(out_dir)))
    src = cf_dir / 'Конфигурация.cf'
    src.write_bytes(b'data')
    db = str(tmp_path / 'ancient.sqlite')
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE source(id INTEGER PRIMARY KEY, file TEXT,'
                 ' created TEXT, file_sha256 TEXT)')
    conn.execute('INSERT INTO source(file, created, file_sha256) VALUES (?,?,?)',
                 ('Конфигурация.cf', '2020-01-01', file_sha256(str(src))))
    conn.commit()
    conn.close()
    rec = scan_cf_sources(str(cf_dir), [db], log=lambda *a: None)[0]
    assert rec['status'] == 'version'
    assert rec['db_version'] is None


def test_scan_cf_dir_extracts_new_and_replaces_outdated(tmp_path, monkeypatch):
    """Пункт 5 меню извлечения: «a» извлекает новые и заменяет устаревшие."""
    from confdb.extract import file_sha256

    cf_dir = tmp_path / 'cf'
    cf_dir.mkdir()
    out_dir = tmp_path / 'out'
    out_dir.mkdir()
    monkeypatch.setattr('confdb.ui_core.out_defaults',
                        lambda src: (str(out_dir / (os.path.splitext(
                            os.path.basename(src))[0] + '.sqlite')), str(out_dir)))
    monkeypatch.setattr('confdb.tui._cls', lambda: None)
    new = cf_dir / 'Новая.cf'
    old = cf_dir / 'Устаревшая.cf'
    cur = cf_dir / 'Актуальная.cf'
    for f in (new, old, cur):
        f.write_bytes(f.name.encode('utf-8'))
    db_old = _source_db(out_dir / 'Устаревшая.sqlite', 'Устаревшая.cf', 'deadbeef')
    db_cur = _source_db(out_dir / 'Актуальная.sqlite', 'Актуальная.cf',
                        file_sha256(str(cur)))
    calls = []

    def fake_extract(src, **kw):
        calls.append((src, kw['db_path'], kw['source_sha256']))
        return {'db': kw['db_path'], 'elapsed': '0:00:01'}

    monkeypatch.setattr('confdb.tui.extract', fake_extract)
    tui = _Tui(db_entries=[db_old, db_cur])
    tui.cf_dir = str(cf_dir)
    tui.groups = {}
    tui.temp = ''
    tui.keep_temp = False
    tui.store_blobs = False
    tui.prefix = ''
    tui.skip_errors = False
    tui.workers = 1
    tui.build_fts = False
    tui.recent_src = []
    tui._save = lambda: None  # не трогаем реальный ~/.confdb/config.json
    # «a» — все новые/устаревшие; Enter — подтвердить; Enter — после извлечения
    _feed(monkeypatch, ['a', '', '', ''])
    tui._scan_cf_dir()
    extracted = {os.path.basename(src) for src, _db, _sha in calls}
    assert extracted == {'Новая.cf', 'Устаревшая.cf'}  # актуальная пропущена
    # устаревшая извлечена в тот же файл (замена), с готовым отпечатком
    old_call = next(c for c in calls if os.path.basename(c[0]) == 'Устаревшая.cf')
    assert old_call[1] == db_old and old_call[2] == file_sha256(str(old))
    assert tui.last_db and tui.recent_db
