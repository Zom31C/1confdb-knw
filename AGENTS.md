# AGENTS.md — guide for LLM agents working in this repository

## What this project is

`confdb` extracts a 1C:Enterprise 8 configuration (`.cf` / `.cfe` / `.epf`) into a SQLite
knowledge base — metadata objects, attributes with resolved types, tabular sections, BSL
modules/methods, SKD report queries, role rights — and serves it to LLM agents via the MCP
server **`1confdb-knw`** (stdio, read-only). The unpack algorithm is a port of
[v8unpack](https://github.com/saby-integration/v8unpack) (MIT; reference copy in
`_vendor/v8unpack`, see `NOTICE.md`). **Decode only** — never add encode/pack code.

Domain glossary: 1C = Russian business-automation platform; BSL = its built-in
(Russian-keyword) language; Catalog=справочник, Document=документ,
(Information/Accumulation)Register=регистр, Enum=перечисление, tabular
section=табличная часть (row table of an object).

Details are not here: `page {"op":"list"}` names the knowledge-base pages (`db-schema`,
`gotchas`, `mcp-server-1confdb-knw`, `extraction-pipeline`, `publication-two-repos`,
`role-rights-format`, `1c-coding-guide`, `agent-rules`, …), and `docs/details.md` («Состав»,
«Схема базы данных») carries the module-by-module and column-by-column detail (README.md is
the short user-facing intro: MCP server first, features, install, quick start). state3 is
NOT shipped inside `dist\`, so there README+docs/details.md are the fallback.

## Rules files and documentation (user decision 2026-11-08)

`AGENTS.md` is the ONLY rules/instruction file in this repository. `CLAUDE.md`, `QWEN.md`
and `QWEN_ProjectInfo.md` were DELETED (this supersedes the 2026-09-25 "keep CLAUDE.md in
dist" decision; the CLAUDE.md content lives on as the state3 page `1c-coding-guide`). Work
is tracked as state3 tasks (`task_*` tools, state root `.state3\state.db`, one open task per
project) and ALL project documentation is kept in the state3 knowledge base
(`page`/`search`): a learned fact, decision or guide becomes a PAGE, never a new .md file in
the repo. README.md and docs/details.md remain the user-facing docs and — together with this
file and NOTICE.md — the only documentation shipped to `dist\`.

## Hard constraints

- Python >= 3.9, **runtime stdlib only** (pytest is the only dev extra). All three venvs are
  3.10, so green tests do NOT prove 3.9 compatibility — check new syntax by eye.
- Windows-oriented: `.bat` wrappers in repo root, venv in `.venv`. Comments, docstrings and
  user-facing text are in **Russian**.
- Package version is `<major>.<schema revision>.<tool patch>` — now **1.9.7** (schema rev 9).
  User rule (2026-11-08): the SECOND digit follows the DB schema revision
  (`writer.SCHEMA_VERSION` = `PRAGMA user_version` of built bases) and resets the last digit
  to 0; the LAST digit increments on tool improvements/fixes (MCP/CLI). Guards:
  `test_version_follows_schema_revision`, `test_version_places_are_in_sync`. The version
  lives in FOUR places that must move together: `src/confdb/__init__.__version__`,
  `pyproject.toml` (root AND dist — the files are identical), `src/confdb/v8/__init__.py`
  (the `v8unpack` stamp in dump JSON headers). After a bump reinstall both venvs
  (root `pip install -e .`, dist `pip install .`) and only then robocopy — the site-packages
  copies shadow the metadata and would keep reporting the old version;
  `source.extractor_version` of built DBs is informational (compatibility is the schema
  revision itself).
- The root is **not** a git repo. Published repo: `dist\1confdb-knw` (`Zom31C/1confdb-knw`) —
  the single unified build. (The old `dist\1confdb-knw-lsp` BSL-LS variant is retired: syntax
  checking is now native, `bsl_syntax.py` / the `check_syntax` tool; its GitHub repo is archived.)
  The dist venv executes a real copy in `.venv\Lib\site-packages\confdb`, so a `src` change has
  **two** destinations (dist `src/` + its site-packages). Sync order, and what must never be
  copied from the root: page `publication-two-repos`.
- `cf/SmallBusinessKz_3_0_4_4_cf.cf` (885 MB) is never committed and never used by unit
  tests — tests run on small synthetic fixtures. Timings: page `extraction-pipeline`;
  ready-made bases: page `test-data`.
- `_vendor/v8unpack` is the port's origin, **not an invariant** (user decision 2026-10-03):
  byte-identical dumps are not worth protecting — speed and completeness of the DB win.
  Correctness = tests + `confdb check` 375/375 + DB contents.

## Layout

One line each; detail in docs/details.md «Состав» and on the page named after it.

- `extract.py` — pipeline stages 0/1/3; work dir on the target's volume; `file_sha256` +
  `check_same_source` skip re-extracting the same file. Page `extraction-pipeline`.
- `dump/` — build the SAME knowledge base from a Designer **file dump** (a directory with
  `ConfigDumpInfo.xml`; `confdb extract <dir>` auto-detects): `model.py` (class-dir registry
  → DB types/paths 1:1 with the cf build, scanner, update granules = top-level objects),
  `xmlmeta.py` (XML metadata → the same `type_str` forms, fields/tabulars/enums/predefined
  +subconto, common-module contexts), `build.py` (full build; `header_json` stores the object
  XML — `header_props` has XML adapters; refs resolve via `attribute_ref.target_path` UPDATE;
  DB ends in WAL), `rights.py`+`rights_map.py` (textual `Ext\Rights.xml` → the same sparse
  role_right rows; right-name→uuid bijection MINED from a dump+cf pair of one config,
  RLS-anchored), `update.py` (`confdb update <dir> --db f`: stat-only scan, SHA-256 of changed
  files vs `dump_file`, dirty-granule delete+reinsert, global relink by uuid/path, content
  tables rebuilt, affected roles re-read, named (once-unresolved) right targets of
  reinstalled granules re-resolved; **works while the MCP server runs** — WAL + server
  hot reload via `PRAGMA data_version` cache invalidation + pidfile `<db>.server.json`;
  FTS rebuilds in shard GENERATIONS so open connections never block the swap). A dump DB is
  schema-rev 9 like cf DBs; the ONLY stop-the-server case is a full rebuild (file delete).
- `__main__.py` — CLI: `extract` (.cf OR a dump dir), `update`, `check`, `fts`, `bench`,
  `1confdb-knw`, `web`.
- `mcp_server.py` — the MCP server: read-only tools, multi-base plus configuration GROUPS,
  paged search, stdio and `--port`. Page `mcp-server-1confdb-knw`.
- `ui_core.py` — the SHARED logic layer of BOTH frontends (state in `~/.confdb/config.json`,
  cf/db candidates, `scan_cf_sources` SHA-256 catalog scan, extract/dump/FTS/bench orchestration,
  groups, `ServerRunner` MCP-HTTP lifecycle with `close_all` on stop). **Rule (user 2026-11-12):
  TUI and web UI evolve TOGETHER — new interface logic goes here FIRST, then both frontends call
  it.** Pages `tui-console`, `web-ui`.
- `tui.py` — console UI, a thin wrapper over `ui_core` (the user chose console over a desktop
  GUI; do not suggest tkinter). Page `tui-console`.
- `webui.py` + `webui.html` — web UI (stdlib `http.server` + vanilla-JS SPA, Russian, localhost):
  the same features as the TUI over `ui_core`; `confdb web` / `confdb-web.bat`, default port 8766;
  background tasks with a live log (`TaskManager`). Page `web-ui`.
- `bsl_parser.py`, `bsl_analyzer.py` — split BSL into methods; lexical analysis for
  dependencies and result schemas (`rebuild_module` restores a module's full text from the DB).
- `bsl_syntax.py` — native BSL syntax checker (lexer + recursive-descent parser, stdlib only),
  the `check_syntax` MCP tool: block constructs, preprocessor `#Если`/`#Область` balance,
  strings, expressions, `Асинх`/`Ждать`, `&`-directives/annotations — diagnostics with
  positions and BSL-LS-compatible codes. Grammar-only (no type/scope resolution). Grammar
  checklist = ANTLR `BSLParser.g4`/`BSLLexer.g4` (1c-syntax/bsl-parser, LGPL); the Python code
  is a clean-room implementation, NOT a port of the Java BSL Language Server. Acceptance:
  41241 modules of three real configs, zero false positives. This SUPERSEDES the old "syntax is
  only the `-lsp` variant's job, do not add a checker" rule — the unified build checks syntax
  natively and `1confdb-knw-lsp` is retired. Page `bsl-ls-integration`.
- `bsl_semantic.py` — opt-in semantic checks (the `check_syntax(semantic=True)` section) on top
  of `bsl_syntax.analyze` + the DB. Tier 2 (DB, scope-aware: params/Перем/module vars/method
  names/object attributes&tabulars/assign targets form the method scope): missing/non-export
  common-module method calls (ERROR; WARNING in form modules — form attributes are not
  extracted), `Менеджер.Объект` references to nonexistent metadata (chains with a prefix or a
  `[index]` are never flagged — dynamic composition; manager methods like ТипВсеСсылки are
  whitelisted), queries embedded in the module via `query_lang` (syntax + tables/fields;
  `%`/`#`/`[` templates and truncated texts skipped), pure-`&НаКлиенте` calls to server common
  modules («Вызов сервера» modules are legal; module-context fallback removed — ordinary
  application mode made 1675 FPs on УНФ). Tier 1/3-lite (no DB): unused local vars,
  break/continue outside a loop (the only ERROR, 0 on vendor corpus), goto, deprecated/insecure
  global calls by name (single-element calls only). Unknown names are NEVER flagged — a stated
  analysis boundary. Deliberately excluded as noisy: unused params (96631 on УНФ), function
  return-value checks, arbitrary name resolution. Corpus-tuned: every rule measured on УНФ
  before shipping.
- `header_props.py` — reads `meta_object.header_json` without re-extracting. Pages
  `register-header-structure`, `configuration-header-props`.
- `query_lang.py` — query language lexer/parser/validator (page `query-validator`);
  `rights.py` — `Role/<name>/Role.0.c1brace` → object rights and RLS, raising `ValueError` on
  an unrecognised format so one bad role never stops the write (page `role-rights-format`);
  also home of the SHARED `TargetResolver` (cf resolves brace targets by uuid via
  `resolve_uuid`, dump resolves `Rights.xml` dotted names via `resolve`; one convention —
  `target_object_id` of an attribute/tabular/subobject right is the OWNER object).
- `db/writer.py` — SQLite schema and writer: batched inserts, parallel BSL parsing, rights
  resolved via `rights.TargetResolver` (page `db-schema`). `INSERT_SQL` is the shared row-write
  layer: every common table's column list lives there ONCE, both `write_db` (cf) and
  `dump/build.py`+`dump/rights.py` insert through it (page `dump-file-source`).
- `xdto.py`, `compare.py`, `bench.py`/`config.py` — `XDTOPackage.bin` → `xdto_*`; object
  snapshots and cross-database diff; hardware benchmark, best `workers` in
  `~/.confdb/config.json`.
- `hbk/` — platform syntax helper: `.hbk` help files (a ZIP shifted by a 1656-byte 1C
  header; stdlib `zipfile` cannot read them) → `syntax.db` (~25300 topics: stdlib methods,
  types, availability per platform, FTS5). `unpack`/`build`/`check`/`query` modules; CLI
  `confdb hbk …`; MCP tools `syntax_search`/`syntax_card`/`syntax_children` via
  `--syntax-db` (or `syntax_db` in `~/.confdb/config.json`) — PLATFORM-level knowledge,
  separate from configuration bases (no meta_object, not a group member). Data files
  (.hbk + ready syntax.db) live in the root `hbk/` dir (gitignored, NOT in dist); detail:
  docs/details.md «Синтакс-помощник платформы». Ported from the user's retired project
  `1c-syntax-db-extractor`; the port is verbatim — a rebuild of the real shcntx_ru.hbk
  matches the original build counts exactly.
- `v8/` — the ported unpack core. The DB root uuid is the header's `file_uuid` (the
  Configuration metadata uuid, same as the dump root — configuration rights resolve against
  it); Enum and FilterCriterion modules decode as `mgr` (they ARE manager modules,
  `ManagerModule.bsl` in a file dump).
- `tests/` — fast tests (`test.bat`); `tests/test_parity.py` builds ONE synthetic config in
  both formats and diffs the two DBs table by table — the standing guard of cf/dump parity.
  `_tmp/` — throwaway probes (gitignored, safe to clean).

## Commands

```bat
test.bat                                              :: pytest
confdb.bat extract <file.cf> --db out.db --workers 8   :: --force, --no-fts, --skip-errors
confdb.bat extract <dump-dir> --db out.db              :: build from a Designer file dump
confdb.bat update <dump-dir> --db out.db               :: incremental; safe while MCP runs
confdb.bat check out.db                               :: validate all SKD queries (375/375)
confdb.bat fts out.db --workers 8                      :: body index later, as sidecar shards
confdb.bat hbk unpack <file.hbk> -o DIR                :: platform help unpacker (shcntx_ru.hbk)
confdb.bat hbk build DIR --db syntax.db                :: syntax-helper DB; check/search/show/tree/repl
1confdb-knw out.db [--port 8765] [--group УНФ=unf.db --group БП=bp.db] [--syntax-db syntax.db]
confdb-ui.bat                                         :: console UI (TUI) over ui_core
confdb-web.bat [--port 8766] [--no-browser]           :: web UI (browser) over the SAME ui_core
.venv\Scripts\python.exe -m compileall -q src\confdb  :: static check
check-sync.bat                                        :: root src/tests vs the dist copy + its site-packages
```

`confdb.bat bench <file.cf>` tunes `workers` to the hardware; the rest of the CLI flags are
in docs/details.md («Извлечение: подробности», опции `extract`).

## Database schema

Tables: `source`, `file`, `meta_object` / `meta_attribute` / `meta_tabular`,
`attribute_ref`, `module` / `method`, `enum_value`, `predefined` / `predefined_subconto`,
`common_target`, `subsystem_content`, `skd_query`, `role_right` / `role_rls_template` /
`role_rights_state`, `xdto_import` / `xdto_type` / `xdto_property`. Role rights are
**sparse**: no row means "the right is not set", never "denied". `meta_attribute.uuid` and
`meta_tabular.uuid` carry the sub-object ids that rights point at. Column lists, every
`type_str` form, reference-uuid resolution and the SKD binary layout: page `db-schema` and
docs/details.md «Схема базы данных». Acceptance criterion for the query validator: **375/375** SKD
queries pass (`confdb check`) — keep it green when touching `query_lang.py` or `writer.py`.

## Gotchas

The collection is the page `gotchas`; proven extraction gaps are `metadata-extraction-gaps`.
Read the relevant one before changing the decoder, the writer or a query. Non-negotiable:

- **Never** `PRAGMA journal_mode=MEMORY` — an interrupted write leaves a "valid-looking"
  near-empty file.
- The brace-file parser returns numbers as **strings** — compare via `str(x)`.
- Field names are unique **within a tabular section**, not within an object — dedup by
  `(section, name)`.
- MCP server is read-only by design (`?mode=ro`, the `sql` tool rejects non-SELECT).
