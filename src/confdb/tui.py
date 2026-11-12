"""Консольный интерфейс confdb (только стандартная библиотека).

Меню с обновлением экрана: запуск MCP-сервера 1confdb-knw (одна база,
несколько баз или именованная группа баз), извлечение конфигурации,
опции извлечения, группы баз.
Недавние пути .cf/.sqlite, опции и группы баз запоминаются
в ~/.confdb/config.json.
Запуск: confdb-ui.bat (или python -m confdb.tui)
"""
import json
import os
import subprocess
import sys
import threading
import time

from . import __version__
from .config import bench_workers
from .config import load_config as _load_config
from .config import save_config as _save_config
from .extract import check_same_source, extract
# общая логика интерфейсов (TUI и web UI используют один слой — ui_core)
from .ui_core import CF_EXTS, ServerRunner, cf_files, compact_db_list
from .ui_core import cf_candidates as _cf_candidates
from .ui_core import db_candidates as _db_candidates_fn
from .ui_core import extract_options as _extract_options_fn
from .ui_core import first_db, group_cli_args, load_groups
from .ui_core import out_defaults as _out_defaults
from .ui_core import (replace_server_dbs, resolve_launch_groups,
                      scan_cf_sources, schema_tail, shown_dbs)


def _cls():
    os.system('cls' if os.name == 'nt' else 'clear')


def _ask(prompt, default=''):
    text = input(f'{prompt} [{default}]: ').strip()
    return text or default


def _unquote(text):
    """Убирает окружающие двойные кавычки («копировать как путь» в Explorer)."""
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].strip()
    return text


def _ask_path(prompt, default=''):
    return _unquote(_ask(prompt, default))


def _yes_no(prompt, default=False):
    suffix = 'да/Нет' if not default else 'Да/нет'
    answer = input(f'{prompt} ({suffix}) [Enter]: ').strip().lower()
    if not answer:
        return default
    return answer in ('да', 'д', 'y', 'yes', '1')


def _remember(items, value, limit=6):
    return [value] + [x for x in items if x != value][:limit - 1]


class Tui:
    def __init__(self):
        config = _load_config()
        self.recent_src = config.get('recent_src', [])
        self.recent_db = config.get('recent_db', [])
        self.db_dirs = config.get('db_dirs', [])
        self.last_db = config.get('last_db', '')
        opts = config.get('options', {})
        self.src = opts.get('src', '')
        self.db = opts.get('db', '')
        self.cf_dir = opts.get('cf_dir', '')
        self.dump = opts.get('dump', '')
        self.temp = opts.get('temp', '')
        self.prefix = opts.get('prefix', '')
        self.keep_temp = bool(opts.get('keep_temp'))
        self.store_blobs = bool(opts.get('store_blobs'))
        self.skip_errors = bool(opts.get('skip_errors'))
        self.build_fts = bool(opts.get('build_fts', True))
        self.workers = int(opts.get('workers') or bench_workers() or 1)
        self.groups = load_groups(config)

    def _save(self):
        # сливаем с текущим конфигом, чтобы не затереть чужие ключи
        # (bench, lsp_workspace и т.п.)
        config = _load_config()
        config.update({
            'recent_src': self.recent_src,
            'recent_db': self.recent_db,
            'db_dirs': self.db_dirs,
            'last_db': self.last_db,
            'groups': self.groups,
            'options': {
                'src': self.src, 'db': self.db, 'cf_dir': self.cf_dir,
                'dump': self.dump, 'temp': self.temp,
                'prefix': self.prefix, 'keep_temp': self.keep_temp,
                'store_blobs': self.store_blobs, 'skip_errors': self.skip_errors,
                'build_fts': self.build_fts,
                'workers': self.workers,
            },
        })
        _save_config(config)

    # ---------- главное меню ----------

    def run(self):
        while True:
            _cls()
            # название — как у проекта/публикации: MCP-сервер на первом месте
            title = (f'1confdb-knw {__version__} — '
                     'MCP-сервер «база знаний конфигурации 1С:Предприятие 8»')
            print(title)
            print('=' * len(title))
            print(' 1. Запустить MCP-сервер (1confdb-knw)')
            print(' 2. Извлечь конфигурацию (.cf/.cfe/.epf -> SQLite)')
            print(' 3. Опции извлечения')
            print(' 4. Группы баз: именованные наборы для MCP-сервера')
            print(' 0. Выход')
            if self.last_db:
                print(f'Последняя БД: {self.last_db}')
            choice = input('Выбор: ').strip()
            if choice == '1':
                self._run_mcp()
            elif choice == '2':
                self._extract_menu()
            elif choice == '3':
                self._options_menu()
            elif choice == '4':
                self._groups_menu()
            elif choice in ('0', 'q', 'exit', 'выход'):
                _cls()
                print('До свидания.')
                return
            else:
                print('Неизвестный пункт меню.')
                input('Нажмите Enter…')

    # ---------- выбор пути ----------

    def _pick_path(self, title, candidates, recent, allow_empty=False, empty_hint=''):
        entries = list(dict.fromkeys(list(candidates) + [r for r in recent if os.path.isfile(r)]))
        while True:
            _cls()
            print(title)
            for i, path in enumerate(entries, 1):
                print(f' {i}. {path}')
            print(' Номер — из списка; буквы/слеш/двоеточие — путь вручную')
            if allow_empty:
                print(f' e. Пусто ({empty_hint})')
            print(' 0. Отмена')
            choice = input('Выбор: ').strip()
            if not choice:
                continue
            if choice == '0':
                return None
            if choice == 'e' and allow_empty:
                return ''
            if choice.isdigit():
                if 1 <= int(choice) <= len(entries):
                    return entries[int(choice) - 1]
                print('Нет такого пункта.')
                continue
            # всё, что не номер и не команда, — путь, набранный вручную
            return _unquote(choice)

    def _db_candidates(self):
        return _db_candidates_fn(self.db_dirs)

    def _pick_db(self, title, candidates=(), allow_empty=False, empty_hint=''):
        while True:
            recent = [r for r in ([self.last_db] + self.recent_db)
                      if r and os.path.isfile(r)]
            entries = list(dict.fromkeys(
                list(candidates) + self._db_candidates() + recent))
            _cls()
            print(title)
            for i, path in enumerate(entries, 1):
                print(f' {i}. {path}')
            print(' Номер — из списка; буквы/слеш/двоеточие — путь вручную')
            print(' k. Добавить каталог с базами (его файлы появятся в списке)')
            if allow_empty:
                print(f' e. Пусто ({empty_hint})')
            print(' 0. Отмена')
            choice = input('Выбор: ').strip()
            if not choice:
                continue
            if choice == '0':
                return None
            if choice == 'e' and allow_empty:
                return ''
            if choice == 'k':
                directory = _ask_path('Каталог с базами')
                if directory and os.path.isdir(directory):
                    directory = os.path.abspath(directory)
                    if directory not in self.db_dirs:
                        self.db_dirs.append(directory)
                        self._save()
                else:
                    print('Нет такого каталога.')
                    input('Нажмите Enter…')
                continue
            if choice.isdigit():
                if 1 <= int(choice) <= len(entries):
                    return entries[int(choice) - 1]
                print('Нет такого пункта.')
                continue
            # всё, что не номер и не команда, — путь, набранный вручную;
            # файла может ещё не быть (сюда же задают базу для извлечения)
            return _unquote(choice)

    # ---------- извлечение ----------

    def _extract_menu(self):
        while True:
            _cls()
            print('--- Извлечение ---')
            print(f' 1. Файл 1С:      {self.src or "<не задан>"}')
            print('    (.cf/.cfe/.epf или каталог выгрузки конфигурации в файлы)')
            print(f' 2. База SQLite:  {self.db or "<не писать>"}')
            print(f' 3. Дамп дерева:  {self.dump or "<не сохранять>"}')
            print(' 4. Запустить извлечение')
            print(f' 5. Сканировать каталог cf: {self.cf_dir or "<не задан>"}')
            print('    (новые и устаревшие конфигурации — по SHA-256)')
            print(' 0. Назад')
            choice = input('Выбор: ').strip()
            if choice == '0':
                self._save()
                return
            if choice == '1':
                picked = self._pick_path(
                    'Файл конфигурации 1С (.cf/.cfe/.epf) или каталог выгрузки',
                    _cf_candidates([self.cf_dir]), self.recent_src)
                if picked:
                    self.src = picked
                    # База по умолчанию — рядом с исходником, имя из его имени.
                    # Пересчитываем всегда при смене файла: если пользователь
                    # менял базу вручную, он задаст её в пункте 2.
                    self.db = _out_defaults(self.src)[0]
            elif choice == '2':
                candidates = [_out_defaults(self.src)[0]] if self.src else []
                picked = self._pick_db('База SQLite', candidates=candidates,
                                       allow_empty=True, empty_hint='не писать')
                if picked is not None:
                    self.db = picked
            elif choice == '3':
                candidates = [_out_defaults(self.src)[1]] if self.src else []
                picked = self._pick_path('Каталог дампа дерева', candidates, [],
                                         allow_empty=True, empty_hint='не сохранять')
                if picked is not None:
                    self.dump = picked
            elif choice == '4':
                self._run_extract()
            elif choice == '5':
                self._scan_cf_dir()
            else:
                print('Неизвестный пункт меню.')
                input('Нажмите Enter…')

    def _extract_options(self):
        """Опции декодера из настроек TUI (общие для одиночного и пакетного)."""
        return _extract_options_fn({'store_blobs': self.store_blobs,
                                    'prefix': self.prefix,
                                    'skip_errors': self.skip_errors})

    def _scan_cf_dir(self):
        """Пакетное извлечение: сканирует каталог cf и предлагает новые/устаревшие.

        Сверка по SHA-256 (scan_cf_sources): конфигурации, которых нет среди
        баз, извлекаются заново; одноимённые, но с другим отпечатком —
        пересобираются с заменой sqlite; актуальные пропускаются.
        """
        if not self.cf_dir or not os.path.isdir(self.cf_dir):
            picked = _ask_path('Каталог с файлами 1С (.cf/.cfe/.epf/.erf)',
                               self.cf_dir)
            if not picked or not os.path.isdir(picked):
                print('Каталог не задан или не существует.')
                input('Нажмите Enter…')
                return
            self.cf_dir = os.path.abspath(picked)
            self._save()
        files = cf_files(self.cf_dir)
        if not files:
            print(f'В каталоге нет файлов .cf/.cfe/.epf/.erf: {self.cf_dir}')
            input('Нажмите Enter…')
            return
        # пул баз-кандидатов: известные каталоги + группы + недавние + пути
        # по умолчанию для каждого файла каталога
        pool = list(self._db_candidates())
        for paths in self.groups.values():
            pool += paths
        pool += [p for p in ([self.last_db] + self.recent_db) if p]
        pool += [_out_defaults(src)[0] for src in files]
        _cls()
        print(f'Сканирую {self.cf_dir} (файлов: {len(files)})…')
        records = scan_cf_sources(self.cf_dir, pool)
        labels = {'new': 'новая   ', 'outdated': 'устарела', 'current': 'актуальна'}
        actionable = [i for i, r in enumerate(records, 1)
                      if r['status'] != 'current']
        while True:
            _cls()
            print(f'--- Каталог cf: {self.cf_dir} ---')
            for i, r in enumerate(records, 1):
                print(f' {i}. [{labels[r["status"]]}] {os.path.basename(r["src"])}')
                if r['status'] != 'current':
                    tail = ' (замена)' if r['status'] == 'outdated' else ''
                    print(f'      → {r["db"]}{tail}')
                if r['note']:
                    print(f'      {r["note"]}')
            print()
            if actionable:
                print('Номера через пробел/запятую — извлечь выбранные; '
                      'a — все новые и устаревшие.')
            else:
                print('Все конфигурации каталога уже собраны и актуальны.')
            print('0. Назад')
            choice = input('Выбор: ').strip().lower()
            if choice in ('0', ''):
                return
            if not actionable:
                continue
            if choice in ('a', 'а', 'all'):
                picked = list(actionable)
            else:
                nums = choice.replace(',', ' ').split()
                if not nums or not all(t.isdigit() for t in nums):
                    print('Непонятная команда.')
                    input('Нажмите Enter…')
                    continue
                picked = [int(t) for t in nums
                          if 1 <= int(t) <= len(records)
                          and records[int(t) - 1]['status'] != 'current']
            if not picked:
                continue
            break
        chosen = [records[i - 1] for i in picked]
        replace = sum(1 for r in chosen if os.path.isfile(r['db']))
        print(f'Будет извлечено: {len(chosen)}'
              + (f', из них с заменой существующих баз: {replace}' if replace else ''))
        if not _yes_no('Продолжить?', True):
            return
        ok, failed = 0, []
        for r in chosen:
            _cls()
            print(f'=== {os.path.basename(r["src"])} → {r["db"]} ===')
            try:
                os.makedirs(os.path.dirname(os.path.abspath(r['db'])),
                            exist_ok=True)
                stats = extract(
                    r['src'], db_path=r['db'], temp_dir=self.temp or None,
                    keep_temp=self.keep_temp, options=self._extract_options(),
                    workers=self.workers, build_fts=self.build_fts,
                    source_sha256=r['sha'])
            except PermissionError as err:
                print(f'Файл базы занят другим процессом (запущен MCP-сервер?): {err}')
                failed.append(os.path.basename(r['src']))
                input('Нажмите Enter…')
                continue
            except Exception as err:  # noqa: BLE001 — одна ошибка не роняет пакет
                print(f'Ошибка извлечения: {err}')
                failed.append(os.path.basename(r['src']))
                input('Нажмите Enter…')
                continue
            ok += 1
            self.last_db = stats['db']
            self.recent_src = _remember(self.recent_src, r['src'])
            self.recent_db = _remember(self.recent_db, r['db'])
            self._save()
            print(f'Готово за {stats["elapsed"]}.')
            input('Нажмите Enter…')
        _cls()
        print(f'Извлечено {ok} из {len(chosen)}.')
        if failed:
            print('С ошибками: ' + ', '.join(failed))
        input('Нажмите Enter…')

    def _run_extract(self):
        # каталог выгрузки конфигурации в файлы — свой конвейер (XML вместо
        # контейнеров; повторный запуск = инкрементальное обновление)
        if self.src and os.path.isdir(self.src):
            from .dump import is_dump_dir
            if is_dump_dir(self.src):
                self._run_extract_dump()
                return
        if not self.src or not os.path.isfile(self.src):
            print('Ошибка: укажите существующий файл 1С (пункт 1).')
            input('Нажмите Enter…')
            return
        if self.db and os.path.isdir(self.db):
            base = os.path.splitext(os.path.basename(self.src))[0]
            self.db = os.path.join(self.db, base + '.sqlite')
            print(f'Путь БД — каталог, файл будет создан как: {self.db}')
        if not self.db and not self.dump:
            db_def, dump_def = _out_defaults(self.src)
            print(f'Не заданы ни БД, ни дамп. По умолчанию: БД={db_def}')
            if _yes_no('Использовать значения по умолчанию?', True):
                self.db, self.dump = db_def, dump_def
            else:
                return
        for out in (self.db, self.dump):
            if out:
                os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

        options = self._extract_options()
        # отпечаток исходника считается один раз: он же отвечает, не собрана ли
        # целевая база из этого же файла (SHA-256 885 МБ — 1,7 с против ~70 с
        # извлечения), и уходит в source новой базы
        src_sha256 = None
        if self.db:
            src_sha256, skip = check_same_source(self.src, self.db)
            if skip and not _yes_no('Пересобрать базу всё равно?', False):
                input('Нажмите Enter…')
                return
        _cls()
        try:
            stats = extract(
                self.src,
                db_path=self.db or None,
                dump_dir=self.dump or None,
                temp_dir=self.temp or None,
                keep_temp=self.keep_temp,
                options=options,
                workers=self.workers,
                build_fts=self.build_fts,
                source_sha256=src_sha256,
            )
        except Exception as err:
            print(f'Ошибка извлечения: {err}')
            input('Нажмите Enter…')
            return
        if stats.get('db'):
            self.last_db = stats['db']
        self.recent_src = _remember(self.recent_src, self.src)
        if self.db:
            self.recent_db = _remember(self.recent_db, self.db)
        self._save()
        print(f'Готово за {stats["elapsed"]}. '
              f'Объектов/модулей/файлов: {stats.get("db_rows", "дамп без БД")}')
        if stats.get('db') and not self.build_fts and _yes_no(
                'Индекс не строился. Построить FTS-индекс по телам методов сейчас?', False):
            self._build_fts(stats['db'])
        input('Нажмите Enter…')

    def _run_extract_dump(self):
        """Сборка/инкрементальное обновление базы из выгрузки в файлы."""
        from .dump import build_db, update_db
        if not self.db:
            self.db = _out_defaults(self.src)[0]
            print(f'База не задана — используем {self.db}')
        if os.path.isdir(self.db):
            base = os.path.basename(os.path.normpath(self.src))
            self.db = os.path.join(self.db, base + '_dump.sqlite')
            print(f'Путь БД — каталог, файл будет создан как: {self.db}')
        os.makedirs(os.path.dirname(os.path.abspath(self.db)), exist_ok=True)
        _cls()
        try:
            if os.path.isfile(self.db):
                print('База уже есть — инкрементальное обновление '
                      '(только изменённые объекты).')
                stats = update_db(self.src, self.db, workers=self.workers,
                                  rebuild_fts=None if self.build_fts else False)
            else:
                stats = build_db(self.src, self.db, workers=self.workers,
                                 build_fts=self.build_fts,
                                 store_blobs=self.store_blobs)
        except PermissionError as err:
            print(f'Файл базы занят другим процессом (запущен MCP-сервер?): {err}')
            input('Нажмите Enter…')
            return
        except Exception as err:  # noqa: BLE001
            print(f'Ошибка сборки из выгрузки: {err}')
            input('Нажмите Enter…')
            return
        self.last_db = stats['db']
        self.recent_src = _remember(self.recent_src, self.src)
        self.recent_db = _remember(self.recent_db, self.db)
        self._save()
        changed = stats.get('changed_granules')
        summary = (f'изменённых объектов: {len(changed)}'
                   if changed is not None else
                   f'объектов: {stats.get("objects")}, '
                   f'модулей: {stats.get("modules")}')
        print(f'Готово за {stats["elapsed"]}. {summary}')
        input('Нажмите Enter…')

    def _build_fts(self, db_path):
        """Достраивает приставной FTS-индекс (то же, что `confdb fts <база>`)."""
        from .db.writer import build_fts_index, fts_dir
        print(f'Строим FTS-индекс рядом с {db_path} в {self.workers} процессов…')
        begin = time.time()
        try:
            methods = build_fts_index(db_path, workers=self.workers)
        except Exception as err:
            print(f'Ошибка построения индекса: {err}')
            input('Нажмите Enter…')
            return
        print(f'Готово: {methods} методов за {time.time() - begin:.1f} с')
        print(f'Файлы индекса: {fts_dir(db_path)} — без них база тоже рабочая')
        input('Нажмите Enter…')

    # ---------- опции ----------

    def _options_menu(self):
        while True:
            _cls()
            print('--- Опции извлечения ---')
            print(f' 1. Число процессов стадии 3: {self.workers}')
            print(f' 2. Рабочий каталог стадий 0-1: {self.temp or "<на томе базы>"}')
            print(f' 3. Префикс имён для снятия:   {self.prefix or "<нет>"}')
            print(f' 4. Не удалять рабочий каталог: {"да" if self.keep_temp else "нет"}')
            print(f' 5. Хранить бинарники (BLOB):  {"да" if self.store_blobs else "нет"}')
            print(f' 6. Пропускать ошибки объектов (--skip-errors): '
                  f'{"да" if self.skip_errors else "нет"}')
            print(f' 7. Строить FTS-индекс по телам методов: '
                  f'{"да" if self.build_fts else "нет (можно достроить позже)"}')
            print(f' 8. Каталог с файлами 1С (.cf/.cfe/.epf/.erf): '
                  f'{self.cf_dir or "<не задан>"}')
            print(' 9. Бенчмарк: подобрать число процессов под железо')
            print(' 0. Назад')
            choice = input('Выбор: ').strip()
            if choice == '0':
                self._save()
                return
            if choice == '1':
                value = _ask('Число процессов (1 — последовательно)', str(self.workers))
                if value.isdigit() and int(value) >= 1:
                    self.workers = int(value)
                else:
                    print('Нужно целое число >= 1.')
                    input('Нажмите Enter…')
            elif choice == '2':
                self.temp = _ask_path('Рабочий каталог (пусто — на томе базы/дампа)',
                                      self.temp)
            elif choice == '3':
                self.prefix = _ask('Префикс (пусто — нет)', self.prefix)
            elif choice == '4':
                self.keep_temp = _yes_no('Не удалять рабочий каталог?', self.keep_temp)
            elif choice == '5':
                self.store_blobs = _yes_no('Хранить бинарники в БД (BLOB)?', self.store_blobs)
            elif choice == '6':
                self.skip_errors = _yes_no(
                    'Пропускать объекты с ошибками декодирования (дамп/база будут неполными)?',
                    self.skip_errors)
            elif choice == '7':
                self.build_fts = _yes_no(
                    'Строить FTS-индекс по телам методов при записи базы? '
                    '(индекс — приставные файлы <база>.fts, по одному на процесс: '
                    'на УНФ 7.5 с в 8 шардов против 49 с одним; без него поиск по '
                    'телам идёт полным проходом, индекс достраивается после извлечения)',
                    self.build_fts)
            elif choice == '8':
                picked = _ask_path('Каталог с файлами 1С (пусто — сбросить)',
                                   self.cf_dir)
                if picked and not os.path.isdir(picked):
                    print('Нет такого каталога.')
                    input('Нажмите Enter…')
                else:
                    self.cf_dir = os.path.abspath(picked) if picked else ''
            elif choice == '9':
                self._run_bench()
            else:
                print('Неизвестный пункт меню.')
                input('Нажмите Enter…')

    # ---------- MCP-сервер ----------

    def _run_mcp(self):
        dbs, groups, title, missing = self._pick_launch_set()
        if not dbs and not groups:
            return
        self._launch_mcp(dbs, groups, title, missing)

    def _pick_launch_set(self):
        """Что открыть: несколько групп сразу и/или отдельные базы вручную.

        Возвращает (базы вне групп, [(имя группы, [пути])], заголовок,
        [недоступные пути]). Деление на группы доходит до сервера: первая
        группа становится активной, а LLM запрашивает группы раздельно
        параметром group.
        """
        picked, loose = [], []
        while True:
            _cls()
            print('--- MCP-сервер 1confdb-knw: что открыть ---')
            names = list(self.groups)
            if names:
                print('Группы баз (создать/изменить — пункт 4 главного меню).')
                print('Несколько групп открываются одновременно и остаются')
                print('раздельными — в квадратных скобках номер выбранной.')
                for i, name in enumerate(names, 1):
                    mark = str(picked.index(name) + 1) if name in picked else ' '
                    print(f' [{mark}] {i}. {name}: '
                          f'{shown_dbs(self.groups[name])}')
            else:
                print('Групп баз пока нет — создать можно в пункте 4.')
            print()
            if loose:
                print('Отдельные базы (вне групп):')
                for i, path in enumerate(loose, 1):
                    print(f'  {i}. {path}')
                print()
            print('Команды: номер — добавить/убрать группу; *N — сделать N-ю')
            print('выбранной первой (её первая база станет активной);')
            print('m — отдельные базы вручную; Enter — запустить; 0 — назад')
            choice = input('> ').strip()
            if choice == '0':
                return [], [], '', []
            if choice == 'm':
                picked_loose = self._multi_select(
                    'Отдельные базы вне групп (первая — основная)',
                    self._db_pool(), selected=loose, ordered=True)
                if picked_loose is not None:
                    loose = picked_loose
                continue
            if choice.startswith('*') and choice[1:].isdigit():
                n = int(choice[1:])
                if 1 <= n <= len(picked):
                    picked.insert(0, picked.pop(n - 1))
                continue
            if choice.isdigit() and 1 <= int(choice) <= len(names):
                name = names[int(choice) - 1]
                if name in picked:
                    picked.remove(name)
                else:
                    picked.append(name)
                continue
            if choice == '':
                groups, missing = resolve_launch_groups(self.groups, picked)
                if not groups and not loose:
                    if missing:
                        # выбор был, но все базы выбранных групп исчезли —
                        # это не «ничего не выбрано», а недоступные файлы
                        print('Базы выбранных групп не найдены:')
                        for p in missing:
                            print(f'  {p}')
                        print('Поправьте группу (пункт 4 главного меню) или '
                              'добавьте базы вручную (m).')
                    else:
                        print('Ничего не выбрано: отметьте группу или добавьте '
                              'базы вручную (m).')
                    input('Нажмите Enter…')
                    continue
                return loose, groups, self._launch_title(groups, loose), missing
            print('Неизвестный пункт.')
            input('Нажмите Enter…')

    @staticmethod
    def _launch_title(groups, loose):
        bits = []
        if groups:
            bits.append('группы ' + ', '.join(f'«{n}»' for n, _ in groups))
        if loose:
            bits.append('отдельные базы: '
                        + ', '.join(os.path.basename(p) for p in loose))
        return '; '.join(bits)

    # ---------- группы баз ----------

    def _db_pool(self):
        recent = [r for r in ([self.last_db] + self.recent_db)
                  if r and os.path.isfile(r)]
        return self._db_candidates() + recent

    def _groups_menu(self):
        while True:
            _cls()
            print('--- Группы баз для MCP-сервера ---')
            names = list(self.groups)
            if names:
                for i, name in enumerate(names, 1):
                    print(f' {i}. {name}: {shown_dbs(self.groups[name])}')
            else:
                print(' Групп пока нет.')
            print()
            print('База может входить в несколько групп; запуск — пункт 1.')
            print(' n. Создать/переписать группу')
            print(' d. Удалить группу')
            print(' 0. Назад')
            choice = input('Выбор: ').strip()
            if choice == '0':
                self._save()
                return
            if choice == 'n':
                self._edit_group()
            elif choice == 'd':
                self._delete_group()
            else:
                print('Неизвестный пункт.')
                input('Нажмите Enter…')

    def _edit_group(self):
        name = _ask('Имя группы')
        if not name:
            return
        dbs = self._multi_select(f'Базы группы «{name}» (первая — основная)',
                                 self._db_pool(),
                                 selected=self.groups.get(name, []), ordered=True)
        if dbs is None:
            return
        if dbs:
            self.groups[name] = dbs
        else:
            self.groups.pop(name, None)
        self._save()

    def _delete_group(self):
        names = list(self.groups)
        if not names:
            print('Групп нет.')
            input('Нажмите Enter…')
            return
        for i, n in enumerate(names, 1):
            print(f' {i}. {n}')
        pick = _ask('Номер группы для удаления')
        if pick.isdigit() and 1 <= int(pick) <= len(names):
            name = names[int(pick) - 1]
            if _yes_no(f'Удалить группу «{name}»?', False):
                self.groups.pop(name, None)
                self._save()

    # ---------- выбор нескольких файлов ----------

    def _multi_select(self, title, pool, selected=None, ordered=False):
        selected = list(selected or [])
        pool = list(dict.fromkeys(pool))
        while True:
            _cls()
            print(title)
            print()
            print('Выбрано' + (' (первый — основной)' if ordered else '') + ':')
            if selected:
                for i, p in enumerate(selected, 1):
                    print(f'  {i}. {p}')
            else:
                print('  <пусто>')
            print()
            avail = [p for p in pool if p not in selected]
            print('Доступно:')
            if avail:
                for i, p in enumerate(avail, 1):
                    print(f'  {i}. {p}')
            else:
                print('  <нет>')
            print()
            cmds = 'Команды: номер — добавить; -N — убрать N-й'
            if ordered:
                cmds += '; *N — сделать N-й первым'
            cmds += '; Enter — готово; 0 — отмена'
            print(cmds)
            choice = input('> ').strip()
            if choice in ('0', 'q'):
                return None
            if choice == '':
                return selected
            if choice.startswith('-') and choice[1:].isdigit():
                n = int(choice[1:])
                if 1 <= n <= len(selected):
                    selected.pop(n - 1)
                continue
            if ordered and choice.startswith('*') and choice[1:].isdigit():
                n = int(choice[1:])
                if 1 <= n <= len(selected):
                    selected.insert(0, selected.pop(n - 1))
                continue
            if choice.isdigit() and 1 <= int(choice) <= len(avail):
                selected.append(avail[int(choice) - 1])
                continue
            print('Непонятная команда.')
            input('Нажмите Enter…')

    # ---------- MCP: запуск выбранного набора баз ----------

    def _launch_mcp(self, dbs, groups, title, missing=()):
        main = first_db(dbs, groups)
        while True:
            _cls()
            print(f'--- MCP-сервер: {title} ---')
            for name, paths in groups:
                print(f' группа «{name}»:')
                for i, d in enumerate(paths, 1):
                    print(f'  {i}. {d}' + (' (основная)' if d == main else ''))
            if dbs:
                if groups:
                    print(' отдельные базы (вне групп):')
                for i, d in enumerate(dbs, 1):
                    print(f' {i}. {d}' + (' (основная)' if d == main else ''))
            for path in missing:
                print(f' пропущена (файл не найден): {path}')
            print()
            print(' Группы откроются одновременно и останутся раздельными:')
            print(' запрос к одной группе — параметр group, ко всем — group="*".')
            print()
            print(' 1. stdio — клиент сам запускает процесс')
            print(' 2. Сеть (HTTP) + управление базами и группами без перезапуска')
            print(' 0. Назад')
            choice = input('Выбор: ').strip()
            if choice == '0':
                return
            if choice == '1':
                args = (['-m', 'confdb.mcp_server'] + list(dbs)
                        + group_cli_args(groups))
                # синтакс-помощник платформы: syntax_db из ~/.confdb/config.json
                from .mcp_server import resolve_syntax_db
                syntax_db = resolve_syntax_db()
                if syntax_db:
                    args += ['--syntax-db', syntax_db]
                _cls()
                print('Сервер работает по stdio; остановка — Ctrl+C.')
                print(f'  {{"command": "{sys.executable}", '
                      f'"args": {json.dumps(args, ensure_ascii=False)}}}')
                print()
                try:
                    subprocess.run([sys.executable] + args)
                except KeyboardInterrupt:
                    pass
                print('Сервер остановлен.')
                input('Нажмите Enter…')
                return
            if choice == '2':
                self._serve_interactive(dbs, groups)
                return
            print('Неизвестный пункт.')
            input('Нажмите Enter…')

    def _serve_interactive(self, dbs, groups=()):
        port = _ask('Порт', '8765')
        if not port.isdigit():
            print('Нужно целое число.')
            input('Нажмите Enter…')
            return
        # жизненный цикл сервера — общий с веб-интерфейсом (ui_core.ServerRunner):
        # остановка закрывает соединения и убирает pidfile, освобождая файлы баз
        runner = ServerRunner()
        try:
            real_port = runner.start(dbs, groups, int(port))
        except ValueError as err:
            print(f'Не удалось открыть базы: {err}')
            input('Нажмите Enter…')
            return
        except OSError as err:
            print(f'Не удалось занять порт {port}: {err}')
            input('Нажмите Enter…')
            return
        _cls()
        print(f'Сервер слушает http://127.0.0.1:{real_port}/mcp '
              '(legacy SSE: /sse).')
        print('С другой машины — через SSH-туннель: '
              f'ssh -L {real_port}:127.0.0.1:{real_port} user@host')
        print('Конфигурация клиента: '
              f'{{"mcpServers": {{"1confdb-knw": '
              f'{{"url": "http://127.0.0.1:{real_port}/mcp"}}}}}}')
        print()
        try:
            self._server_control(runner.server)
        finally:
            runner.stop()
        print('Сервер остановлен, базы закрыты — файлы свободны для пересборки.')
        input('Нажмите Enter…')

    def _server_control(self, server):
        while True:
            _cls()
            print('--- Управление базами и группами запущенного MCP-сервера ---')
            print(compact_db_list(server))
            # Показываем группы конфигураций
            if server.groups:
                print()
                print('Группы конфигураций:')
                for group_name, db_aliases in server.groups.items():
                    mark = '*' if group_name == server.active_group else ' '
                    dbs_str = ', '.join(db_aliases) if db_aliases else '(пусто)'
                    print(f'  {mark} {group_name}: {dbs_str}')
            print()
            print(' 1. Сменить активную базу')
            print(' 2. Открыть ещё базу')
            print(' 3. Закрыть базу')
            print(' 4. Группы из конфига: добавить / заменить все / убрать / активировать')
            print(' 5. Создать группу конфигураций')
            print(' 6. Добавить базу в группу конфигураций')
            print(' 7. Удалить группу конфигураций')
            print(' 0. Остановить сервер и выйти')
            choice = input('Выбор: ').strip()
            if choice == '0':
                return
            aliases = list(server.dbs)
            if choice == '1':
                if not aliases:
                    print('Нет открытых баз.')
                    input('Нажмите Enter…')
                    continue
                for i, a in enumerate(aliases, 1):
                    print(f'  {i}. {a}' + (' *' if a == server.active else '')
                          + schema_tail(server, a))
                pick = _ask('Номер базы, которую сделать активной')
                if pick.isdigit() and 1 <= int(pick) <= len(aliases):
                    server.active = aliases[int(pick) - 1]
            elif choice == '2':
                path = self._pick_db('Открыть базу в запущенном сервере')
                if path:
                    try:
                        alias = server.open_db(path)
                        print(f'Открыта база: {alias}')
                    except ValueError as err:
                        print(f'Ошибка: {err}')
                    input('Нажмите Enter…')
            elif choice == '3':
                if not aliases:
                    print('Нет открытых баз.')
                    input('Нажмите Enter…')
                    continue
                for i, a in enumerate(aliases, 1):
                    print(f'  {i}. {a}')
                pick = _ask('Номер базы, которую закрыть')
                if pick.isdigit() and 1 <= int(pick) <= len(aliases):
                    try:
                        server.close_db(aliases[int(pick) - 1])
                    except ValueError as err:
                        print(f'Ошибка: {err}')
                    input('Нажмите Enter…')
            elif choice == '4':
                self._config_groups_menu(server)
            elif choice == '5':
                # Создать группу конфигураций
                name = _ask('Имя группы конфигураций')
                if name:
                    try:
                        server.create_group(name)
                        print(f'Группа «{name}» создана.')
                    except ValueError as err:
                        print(f'Ошибка: {err}')
                input('Нажмите Enter…')
            elif choice == '6':
                # Добавить базу в группу конфигураций
                if not server.groups:
                    print('Нет групп — создайте сначала (пункт 5).')
                    input('Нажмите Enter…')
                    continue
                aliases = list(server.dbs)
                if not aliases:
                    print('Нет открытых баз.')
                    input('Нажмите Enter…')
                    continue
                group_names = list(server.groups)
                print('Группы:')
                for i, g in enumerate(group_names, 1):
                    print(f'  {i}. {g}')
                pick = _ask('Номер группы')
                if not (pick.isdigit() and 1 <= int(pick) <= len(group_names)):
                    continue
                group_name = group_names[int(pick) - 1]
                print('Базы:')
                for i, a in enumerate(aliases, 1):
                    in_group = '✓' if a in server.groups[group_name] else ' '
                    print(f'  {in_group} {i}. {a}' + schema_tail(server, a))
                pick = _ask('Номер базы для добавления в группу')
                if pick.isdigit() and 1 <= int(pick) <= len(aliases):
                    alias = aliases[int(pick) - 1]
                    try:
                        server.add_db_to_group(group_name, alias)
                        print(f'База {alias} добавлена в группу {group_name}.')
                    except ValueError as err:
                        print(f'Ошибка: {err}')
                input('Нажмите Enter…')
            elif choice == '7':
                # Удалить группу конфигураций
                if not server.groups:
                    print('Нет групп.')
                    input('Нажмите Enter…')
                    continue
                group_names = list(server.groups)
                for i, g in enumerate(group_names, 1):
                    print(f'  {i}. {g}')
                pick = _ask('Номер группы для удаления')
                if pick.isdigit() and 1 <= int(pick) <= len(group_names):
                    group_name = group_names[int(pick) - 1]
                    try:
                        server.close_group(group_name)
                        print(f'Группа {group_name} удалена.')
                    except ValueError as err:
                        print(f'Ошибка: {err}')
                input('Нажмите Enter…')
            else:
                print('Неизвестный пункт.')
                input('Нажмите Enter…')

    # ---------- группы из конфига в запущенном сервере ----------

    @staticmethod
    def _server_group(server, name):
        """Имя группы сервера без учёта регистра либо None."""
        return next((g for g in server.groups
                     if g.lower() == str(name).lower()), None)

    def _pick_server_group(self, server, what):
        """Показывает группы сервера и возвращает выбранную (или None)."""
        if not server.groups:
            print('В сервере групп нет.')
            input('Нажмите Enter…')
            return None
        group_names = list(server.groups)
        for i, g in enumerate(group_names, 1):
            print(f'  {i}. {g}' + (' *' if g == server.active_group else ''))
        pick = _ask(f'Номер группы, которую {what}')
        if pick.isdigit() and 1 <= int(pick) <= len(group_names):
            return group_names[int(pick) - 1]
        return None

    def _apply_config_group(self, server, name, paths, replace=False):
        """Группа из конфига -> работающий сервер: добавить либо заменить всё.

        Возвращает (имя группы в сервере, [алиасы], [ошибки]). При замене
        прежние группы удаляются, а все открытые базы закрываются и
        открываются заново — набор сервера становится равен группе (это
        прежнее «применить группу», но с регистрацией группы в сервере).
        """
        if replace:
            for group in list(server.groups):
                server.close_group(group)
            replace_server_dbs(server, ())
            return server.add_group_paths(name, paths, activate=True)
        return server.add_group_paths(name, paths)

    def _config_groups_menu(self, server):
        names = list(self.groups)
        while True:
            _cls()
            print('--- Группы из конфига в запущенном сервере ---')
            if names:
                print('(* — группа уже открыта в сервере)')
                for i, name in enumerate(names, 1):
                    mark = '*' if self._server_group(server, name) else ' '
                    print(f' {mark} {i}. {name}: '
                          f'{shown_dbs(self.groups[name])}')
            else:
                print(' В конфиге групп нет — создать можно в пункте 4 '
                      'главного меню.')
            print()
            print(server.group_list())
            print()
            print(' 1. Добавить группу (её базы откроются, прочие группы останутся)')
            print(' 2. Заменить все группы и базы сервера одной из конфига')
            print(' 3. Убрать группу сервера (её базы закроются, если не нужны другим)')
            print(' 4. Сделать группу сервера активной')
            print(' 0. Назад')
            choice = input('Выбор: ').strip()
            if choice == '0':
                return
            if choice in ('1', '2'):
                if not names:
                    print('В конфиге групп нет.')
                    input('Нажмите Enter…')
                    continue
                pick = _ask('Номер группы из конфига')
                if not (pick.isdigit() and 1 <= int(pick) <= len(names)):
                    continue
                name = names[int(pick) - 1]
                try:
                    group, aliases, errors = self._apply_config_group(
                        server, name, self.groups[name], replace=(choice == '2'))
                except ValueError as err:
                    print(f'Ошибка: {err}')
                    input('Нажмите Enter…')
                    continue
                for err in errors:
                    print(f'Ошибка: {err}')
                if aliases:
                    print(f'Группа {group}: ' + ', '.join(aliases)
                          + (' (активна)'
                             if group == server.active_group else ''))
                else:
                    print(f'Группа {group} осталась без баз.')
                input('Нажмите Enter…')
            elif choice == '3':
                group = self._pick_server_group(server, 'нужно убрать')
                if not group:
                    continue
                try:
                    name, closed = server.drop_group(group)
                except ValueError as err:
                    print(f'Ошибка: {err}')
                    input('Нажмите Enter…')
                    continue
                print(f'Группа {name} удалена' + (
                    f'; закрыты базы: {", ".join(closed)}' if closed
                    else '; базы остались открытыми'))
                input('Нажмите Enter…')
            elif choice == '4':
                group = self._pick_server_group(server, 'нужно сделать активной')
                if not group:
                    continue
                try:
                    name = server.use_group(group)
                except ValueError as err:
                    print(f'Ошибка: {err}')
                    input('Нажмите Enter…')
                    continue
                print(f'Активная группа: {name}; активная база: {server.active}')
                input('Нажмите Enter…')
            else:
                print('Неизвестный пункт.')
                input('Нажмите Enter…')

    # ---------- бенчмарк ----------

    def _run_bench(self):
        if not self.src or not os.path.isfile(self.src):
            picked = self._pick_path('Файл конфигурации 1С для бенчмарка',
                                     _cf_candidates([self.cf_dir]), self.recent_src)
            if not picked:
                return
            self.src = picked
        _cls()
        print('Бенчмарк: прогон сэмпла объектов при разном числе процессов.')
        print('Стадии 0/1 выполняются один раз, затем сэмпл стадии 3 + запись БД.')
        print()
        from .bench import bench
        try:
            best = bench(self.src)
        except Exception as err:
            print(f'Ошибка бенчмарка: {err}')
            input('Нажмите Enter…')
            return
        self.workers = best
        self.recent_src = _remember(self.recent_src, self.src)
        self._save()
        input('Нажмите Enter…')


def main():
    try:
        Tui().run()
    except (EOFError, KeyboardInterrupt):
        print()
        print('Прервано. До свидания.')
        sys.exit(0)


if __name__ == '__main__':
    main()
