"""confdb — экстрактор конфигурации 1С:Предприятие 8 в базу данных SQLite.

Версия пакета (штампуется в source.extractor_version собранных баз и в meta
syntax.db) — `<major>.<ревизия схемы базы>.<доработки инструментов>`:
вторая цифра идёт за ревизией схемы (db.writer.SCHEMA_VERSION, она же
PRAGMA user_version баз) и сбрасывает последнюю в 0, последняя цифра
растёт на доработки/исправления инструментов (MCP/CLI). Рассинхрон второй
цифры с ревизией схемы ловит тест test_version_follows_schema_revision.
Поднимать синхронно здесь, в pyproject.toml (корень и dist) и в
v8/__init__.py — сверяется тестом test_version_places_are_in_sync.
"""

__version__ = '1.9.6'
