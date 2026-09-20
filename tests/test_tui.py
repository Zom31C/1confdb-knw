"""Тесты вспомогательных функций консольного интерфейса."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from confdb.tui import _unquote, load_groups, replace_server_dbs  # noqa: E402


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
