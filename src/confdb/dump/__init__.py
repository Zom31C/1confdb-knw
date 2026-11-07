"""Сборка базы знаний из выгрузки конфигурации 1С в файлы (Designer dump).

Вход — каталог иерархической выгрузки конфигуратора (признак: файл
`ConfigDumpInfo.xml` в корне). Метаданные объектов — текстовые XML
(`<Классы>\\<Имя>.xml` + папка `<Имя>\\` с содержимым), модули — готовые
`.bsl`, права ролей — `Roles\\<Имя>\\Ext\\Rights.xml`, СКД-макеты —
`Templates\\<Имя>\\Ext\\Template.xml`, XDTO — `Ext\\Package.bin` (тот же
бинарь, что в .cf).

Результат — та же схема SQLite, что после `extract` из .cf (`db.writer.SCHEMA`),
с теми же значениями type/path/code_name/context/type_str, поэтому базы из
двух источников совместимы с MCP-сервером и сравниваются между собой.
Отличие: `meta_object.header_json` хранит исходный XML объекта (адаптеры
чтения — в `header_props`), а состав файлов фиксирует таблица `dump_file` —
основа инкрементального обновления (`update_db`).

Импорты сборки/обновления — ленивые: модель и сканер (`dump.model`)
используются и без них (тесты, зонды).
"""
import os

__all__ = ['build_db', 'update_db', 'is_dump_dir']


def is_dump_dir(path):
    """True, если path — каталог иерархической выгрузки конфигурации."""
    return os.path.isdir(path) and os.path.isfile(
        os.path.join(path, 'ConfigDumpInfo.xml'))


def build_db(*args, **kwargs):
    """Полная сборка базы из выгрузки (см. dump.build.build_db)."""
    from .build import build_db as _build
    return _build(*args, **kwargs)


def update_db(*args, **kwargs):
    """Инкрементальное обновление базы по изменившимся объектам выгрузки
    (см. dump.update.update_db)."""
    from .update import update_db as _update
    return _update(*args, **kwargs)
