"""Синтакс-помощник платформы 1С:Предприятие 8: файлы .hbk → база SQLite.

Конвейер (перенесён из проекта 1c-syntax-db-extractor):

    shcntx_ru.hbk ──unpack──> дерево objects/ tables/ ──build──> syntax.db
                                                      │            │
                                                  check        query (CLI,
                                                  (пороги       MCP-инструменты
                                                   качества)    syntax_*)

Файл `.hbk` — контекстная справка платформы (ZIP, сдвинутый на заголовок 1С:
стандартный zipfile и 7-Zip читают его неверно — см. `unpack`). База
`syntax.db` — знание УРОВНЯ ПЛАТФОРМЫ (что вообще даёт встроенный язык 1С:
методы глобального контекста, типы и их члены, поля системных таблиц —
сигнатуры RU/EN, параметры, доступность по платформам, описания, примеры,
перекрёстные ссылки, FTS5-поиск), в отличие от баз знаний confdb, которые
описывают содержимое конкретной конфигурации.

Модули:

- `unpack` — распаковщик .hbk на чистом Python (struct + zlib, CRC32);
- `build`  — разбор HTML/.st/`__categories__` и сборка `syntax.db`;
- `check`  — контроль качества сборки (пороги настроены на полную базу
  из shcntx_ru.hbk; `full=False` оставляет только структурные проверки);
- `query`  — нечёткий поиск, карточка темы, обход дерева: функции
  возвращают ТЕКСТ — его печатает CLI и отдают инструменты MCP-сервера
  (`syntax_search`, `syntax_card`, `syntax_children`, ключ --syntax-db).

Только стандартная библиотека; HTML разбирается регулярными выражениями.
"""

from .build import build_syntax_db
from .check import check_syntax_db
from .unpack import HbkError, unpack_hbk

__all__ = ['HbkError', 'unpack_hbk', 'build_syntax_db', 'check_syntax_db']
