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
`role-rights-format`, …), and `README.md` («Состав», «Схема базы данных») carries the
module-by-module and column-by-column detail. state3 is NOT shipped inside `dist\`, so
there README is the fallback.

## Hard constraints

- Python >= 3.9, **runtime stdlib only** (pytest is the only dev extra). All three venvs are
  3.10, so green tests do NOT prove 3.9 compatibility — check new syntax by eye.
- Windows-oriented: `.bat` wrappers in repo root, venv in `.venv`. Comments, docstrings and
  user-facing text are in **Russian**.
- The root is **not** a git repo. Published repos: `dist\1confdb-knw` (`Zom31C/1confdb-knw`)
  and `dist\1confdb-knw-lsp` (BSL-LS variant, `Zom31C/1confdb-knw-lsp`). The mainline venv
  executes a real copy in `.venv\Lib\site-packages\confdb`, so a `src` change has **three**
  destinations. Sync order, and what must never be copied from the root: page
  `publication-two-repos`.
- `cf/SmallBusinessKz_3_0_4_4_cf.cf` (885 MB) is never committed and never used by unit
  tests — tests run on small synthetic fixtures. Timings: page `extraction-pipeline`;
  ready-made bases: page `test-data`.
- `_vendor/v8unpack` is the port's origin, **not an invariant** (user decision 2026-10-03):
  byte-identical dumps are not worth protecting — speed and completeness of the DB win.
  Correctness = tests + `confdb check` 375/375 + DB contents.

## Layout

One line each; detail in README «Состав» and on the page named after it.

- `extract.py` — pipeline stages 0/1/3; work dir on the target's volume; `file_sha256` +
  `check_same_source` skip re-extracting the same file. Page `extraction-pipeline`.
- `__main__.py` — CLI: `extract`, `check`, `fts`, `bench`, `1confdb-knw`.
- `mcp_server.py` — the MCP server: read-only tools, multi-base plus configuration GROUPS,
  paged search, stdio and `--port`. Page `mcp-server-1confdb-knw`.
- `tui.py` — console UI (the user chose console over GUI; do not suggest tkinter). Page
  `tui-console`.
- `bsl_parser.py`, `bsl_analyzer.py` — split BSL into methods; lexical analysis for
  dependencies and result schemas. The analyzer deliberately does **not** check module
  syntax — that is the BSL Language Server's job, only in the `-lsp` variant; do not add a
  `check_bsl`-style checker here. Page `bsl-ls-integration`.
- `header_props.py` — reads `meta_object.header_json` without re-extracting. Pages
  `register-header-structure`, `configuration-header-props`.
- `query_lang.py` — query language lexer/parser/validator (page `query-validator`);
  `rights.py` — `Role/<name>/Role.0.c1brace` → object rights and RLS, raising `ValueError` on
  an unrecognised format so one bad role never stops the write (page `role-rights-format`).
- `db/writer.py` — SQLite schema and writer: batched inserts, parallel BSL parsing, rights
  resolved against object/attribute/tabular uuids (page `db-schema`).
- `xdto.py`, `compare.py`, `bench.py`/`config.py` — `XDTOPackage.bin` → `xdto_*`; object
  snapshots and cross-database diff; hardware benchmark, best `workers` in
  `~/.confdb/config.json`.
- `v8/` — the ported unpack core. `tests/` — fast tests (`test.bat`). `_tmp/` — throwaway
  probes (gitignored, safe to clean).

## Commands

```bat
test.bat                                              :: pytest
confdb.bat extract <file.cf> --db out.db --workers 8   :: --force, --no-fts, --skip-errors
confdb.bat check out.db                               :: validate all SKD queries (375/375)
confdb.bat fts out.db --workers 8                      :: body index later, as sidecar shards
1confdb-knw.bat out.db [--port 8765] [--group УНФ=unf.db --group БП=bp.db]
.venv\Scripts\python.exe -m compileall -q src\confdb  :: static check
check-sync.bat                                        :: root src/tests vs both dist copies
```

`confdb.bat bench <file.cf>` tunes `workers` to the hardware; the rest of the CLI flags are
in README «Использование».

## Database schema

Tables: `source`, `file`, `meta_object` / `meta_attribute` / `meta_tabular`,
`attribute_ref`, `module` / `method`, `enum_value`, `predefined` / `predefined_subconto`,
`common_target`, `subsystem_content`, `skd_query`, `role_right` / `role_rls_template` /
`role_rights_state`, `xdto_import` / `xdto_type` / `xdto_property`. Role rights are
**sparse**: no row means "the right is not set", never "denied". `meta_attribute.uuid` and
`meta_tabular.uuid` carry the sub-object ids that rights point at. Column lists, every
`type_str` form, reference-uuid resolution and the SKD binary layout: page `db-schema` and
README «Схема базы данных». Acceptance criterion for the query validator: **375/375** SKD
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
