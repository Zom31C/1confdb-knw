# -*- coding: utf-8 -*-
"""Журнал запросов LLM в отдельной базе SQLite (confdb.llm_log)."""
import os
import sqlite3

import pytest

from confdb import llm_log


def _writer(tmp_path, name='llm.sqlite'):
    return llm_log.LlmLogWriter(str(tmp_path / name))


def test_writer_creates_db_and_appends(tmp_path):
    writer = _writer(tmp_path)
    writer.append('find_objects', {'mask': 'Товар'}, 'ответ сервера',
                  duration_ms=7, db='БП', grp='Группа1')
    writer.append('sql', {'query': 'SELECT 1'}, 'ошибка', is_error=True)
    writer.close()
    path = str(tmp_path / 'llm.sqlite')
    assert os.path.isfile(path)
    result = llm_log.list_calls(path)
    assert result['total'] == 2
    # новые сверху
    assert result['rows'][0]['tool'] == 'sql'
    assert result['rows'][0]['is_error'] is True
    assert result['rows'][1]['tool'] == 'find_objects'
    assert result['rows'][1]['duration_ms'] == 7
    assert result['rows'][1]['db'] == 'БП'
    assert result['rows'][1]['grp'] == 'Группа1'
    assert result['tools'] == ['find_objects', 'sql']
    # сессия одна на writer, время с миллисекундами
    assert result['rows'][0]['session'] == result['rows'][1]['session']
    assert '.' in result['rows'][0]['ts']


def test_full_response_stored_without_truncation(tmp_path):
    writer = _writer(tmp_path)
    body = 'строка ответа\n' * 5000
    writer.append('object_card', {'path': 'Документ.Х'}, body)
    writer.close()
    path = str(tmp_path / 'llm.sqlite')
    row = llm_log.list_calls(path)['rows'][0]
    assert row['resp_len'] == len(body)
    full = llm_log.get_call(path, row['id'])
    assert full['response'] == body
    assert full['args'] == '{"path": "Документ.Х"}'


def test_list_filters_and_paging(tmp_path):
    writer = _writer(tmp_path)
    for i in range(5):
        writer.append('find_objects', {'mask': f'товар{i}'}, f'ответ {i}')
    writer.append('sql', {'query': 'SELECT'}, 'нет такой таблицы',
                  is_error=True)
    writer.close()
    path = str(tmp_path / 'llm.sqlite')
    assert llm_log.list_calls(path, tool='sql')['total'] == 1
    assert llm_log.list_calls(path, errors_only=True)['total'] == 1
    assert llm_log.list_calls(path, search='товар3')['total'] == 1
    assert llm_log.list_calls(path, search='нет такой')['total'] == 1
    page1 = llm_log.list_calls(path, limit=2, offset=0)
    page2 = llm_log.list_calls(path, limit=2, offset=2)
    assert page1['total'] == 6 and len(page1['rows']) == 2
    assert {r['id'] for r in page1['rows']}.isdisjoint(
        r['id'] for r in page2['rows'])
    # предпросмотр ответа обрезан, полные тексты — в get_call
    assert len(page1['rows'][0]['preview']) <= llm_log.RESP_PREVIEW


def test_get_call_missing_raises(tmp_path):
    writer = _writer(tmp_path)
    writer.close()
    with pytest.raises(ValueError):
        llm_log.get_call(str(tmp_path / 'llm.sqlite'), 999)
    with pytest.raises(ValueError):
        llm_log.get_call(str(tmp_path / 'нет.sqlite'), 1)


def test_clear_removes_all(tmp_path):
    writer = _writer(tmp_path)
    writer.append('db_list', {}, 'ответ')
    writer.append('db_list', {}, 'ответ')
    writer.close()
    path = str(tmp_path / 'llm.sqlite')
    assert llm_log.clear(path) == 2
    assert llm_log.list_calls(path)['total'] == 0
    assert llm_log.clear(str(tmp_path / 'нет.sqlite')) == 0


def test_stats(tmp_path):
    path = str(tmp_path / 'llm.sqlite')
    empty = llm_log.stats(path)
    assert empty['exists'] is False and empty['count'] == 0
    writer = _writer(tmp_path)
    writer.append('db_list', {}, 'ответ')
    writer.append('sql', {}, 'ошибка', is_error=True)
    writer.close()
    stats = llm_log.stats(path)
    assert stats['exists'] is True
    assert stats['count'] == 2 and stats['errors'] == 1
    assert stats['size_bytes'] > 0
    assert stats['tools'] == ['db_list', 'sql']


def test_settings_and_save_settings(monkeypatch):
    store = {}
    monkeypatch.setattr(llm_log, 'load_config', lambda: store)

    def save(config):
        snapshot = dict(config)   # config может быть тем же словарём store
        store.clear()
        store.update(snapshot)

    monkeypatch.setattr(llm_log, 'save_config', save)
    assert llm_log.settings() == {'enabled': False,
                                  'path': llm_log.DEFAULT_PATH}
    result = llm_log.save_settings(True, 'D:\\logs\\llm.db')
    assert result['enabled'] is True and result['path'] == 'D:\\logs\\llm.db'
    # чужие ключи конфига не затираются
    store['bench'] = {'workers': 8}
    llm_log.save_settings(False)
    assert store['bench'] == {'workers': 8}
    assert store['options']['llm_log_enabled'] is False
    assert store['options']['llm_log_db'] == 'D:\\logs\\llm.db'


def test_configure_server_applies_settings(monkeypatch, tmp_path):
    path = str(tmp_path / 'llm.sqlite')
    monkeypatch.setattr(llm_log, 'load_config',
                        lambda: {'options': {'llm_log_enabled': True,
                                             'llm_log_db': path}})

    class _Server:
        def __init__(self):
            self.calls = []

        def configure_llm_log(self, enabled, p=None):
            self.calls.append((enabled, p))

    server = _Server()
    llm_log.configure_server(server)
    assert server.calls == [(True, path)]


def test_configure_server_error_strict_and_soft(monkeypatch, capsys):
    monkeypatch.setattr(llm_log, 'load_config',
                        lambda: {'options': {'llm_log_enabled': True}})

    class _Server:
        def configure_llm_log(self, enabled, path=None):
            raise sqlite3.OperationalError('нет доступа')

    with pytest.raises(ValueError):
        llm_log.configure_server(_Server(), strict=True)
    llm_log.configure_server(_Server())  # не строго: предупреждение, не падение
    assert 'журнал' in capsys.readouterr().err.lower()


def test_writer_reopen_keeps_records(tmp_path):
    path = str(tmp_path / 'llm.sqlite')
    writer = llm_log.LlmLogWriter(path)
    writer.append('db_list', {}, 'первый')
    writer.close()
    writer2 = llm_log.LlmLogWriter(path)
    writer2.append('db_list', {}, 'второй')
    writer2.close()
    result = llm_log.list_calls(path)
    assert result['total'] == 2
    # разные сессии у разных запусков записи
    assert result['rows'][0]['session'] != result['rows'][1]['session']
