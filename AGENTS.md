# AGENTS.md — guide for LLM agents working in this repository

## What this project is

`confdb` extracts a 1C:Enterprise 8 configuration (`.cf` / `.cfe` / `.epf`) into a
SQLite knowledge base — metadata objects, attributes with resolved types, tabular
sections, BSL modules/methods, SKD report queries — and serves it to LLM agents via
the MCP server **`1confdb-knw`** (stdio, read-only). The unpack algorithm is a port of
[v8unpack](https://github.com/saby-integration/v8unpack) (MIT; reference copy in
`_vendor/v8unpack`, see `NOTICE.md`). **Decode only** — never add encode/pack code.

Domain glossary: 1C = Russian business-automation platform; BSL = its built-in
(Russian-keyword) language; Catalog=справочник, Document=документ,
(Information/Accumulation)Register=регистр, Enum=перечисление, tabular
section=табличная часть (row table of an object).

Detail is not repeated here — it lives in the project knowledge base (state3 of the root
project; it is NOT shipped inside `dist\`), one `page {"op":"get","id":…}` away: `project`,
`db-schema`, `mcp-server-1confdb-knw`, `extraction-pipeline`, `query-validator`,
`tui-console`, `bsl-ls-integration`, `gotchas`, `publication-two-repos`, `test-data`,
`metadata-extraction-gaps`, `onboarding`.

## Hard constraints

- Python >= 3.9, **runtime stdlib only** (pytest is the only dev extra). All three venvs
  are 3.10, so green tests do NOT prove 3.9 compatibility — check new syntax by eye.
- Windows-oriented: `.bat` wrappers in repo root; venv in `.venv` (MS Store Python:
  `.venv\Scripts\python.exe` is a launcher, the real worker is a child process).
- Git is available (2.55+); the project root is **not** a repo — the published
  repo lives in `dist\1confdb-knw` (remote `Zom31C/1confdb-knw`).
- Comments, docstrings and user-facing text are in **Russian**.
- The test configuration `SmallBusinessKz_3_0_4_4_cf.cf` (885 MB = 844 MiB) lives in `cf/`
  (or repo root); never commit it and keep it out of unit tests — tests use small synthetic
  fixtures. Full-extract timings and the `bench` tuning: page `extraction-pipeline`; ready-made
  knowledge bases: page `test-data`.
- `_vendor/v8unpack` is the port's origin, **not an invariant** (user decision 2026-10-03):
  byte-identical dumps are not worth protecting — speed and completeness of the DB win.
  Correctness = tests + `confdb check` 375/375 + DB contents; `--dump-indent` still
  reproduces the v8unpack dump layout if a comparison is ever needed.

## Layout

- `src/confdb/extract.py` — pipeline stages 0/1/3 (containers → inflate → decode); the work dir
  defaults to the target's volume (`make_temp_dir`), not `%TEMP%`, and its cleanup is parallel
  (`remove_tree`) — deleting ~125k files costs ~30 s on the system volume vs ~10 s elsewhere.
  It also fingerprints the source file: `file_sha256` (1.7 s for 885 MB) is written to
  `source.file_sha256`, and `check_same_source(src, db, force)` lets the CLI/TUI skip a
  re-extraction of the very same file — old bases have no fingerprint, which reads as "unknown",
  never as "different".
- `src/confdb/__main__.py` — CLI: `extract`, `check`, `1confdb-knw`.
- `src/confdb/mcp_server.py` — MCP server `1confdb-knw <db…>`: 24 read-only tools,
  self-describing (schema primer + glossary + workflow in `initialize.instructions`).
  Multi-database (alias per base, optional `db` parameter, `db='*'` fan-out); the six search
  tools page (`limit` 1..200 + `offset`; the last line names the total and the next offset —
  `page_note`, `count_of` with a per-base cache); cross-base tools
  take explicit aliases (`compare_object`, `extension_diff`); errors are categorized
  (`error_text`) and `SQLITE_BUSY` is retried (`call_with_retry`); stdio by default,
  `--port N` for HTTP/SSE. Inventory and behaviour: page `mcp-server-1confdb-knw`.
- `src/confdb/tui.py` — console UI (the user chose console over GUI; do not suggest tkinter).
  In the file/db pickers a number selects a list item and ANY other text is a typed path.
  Menus and base groups: page `tui-console`.
- `src/confdb/bsl_parser.py` — splits BSL modules into procedures/functions.
- `src/confdb/bsl_analyzer.py` — lexical analysis of BSL for `method_dependencies` and
  `method_result_schema`: string/comment masking that understands 1C multi-line literals with
  `|`, query extraction + validation via `query_lang`, common-module/metadata resolution,
  client-vs-server context. **It deliberately does NOT check module syntax** — BSL Language
  Server does that, and it only exists in the `1confdb-knw-lsp` variant; do not add a
  `check_bsl`-style syntax checker here.
- `src/confdb/header_props.py` — reads `meta_object.header_json` without re-extracting:
  configuration version/synonym/compatibility mode/extension prefix, register
  dimension/resource/attribute collections with periodicity and write-mode flags, the target
  namespace of an XDTO package, and the kind of the loaded file — from the extension of
  `source.file`, NOT from the root type (`.erf` and `.epf` both decode into
  `ExternalDataProcessor`). Verified positions and uuids: pages `register-header-structure`,
  `configuration-header-props`.
- `src/confdb/xdto.py` — parses `XDTOPackage.bin` (plain UTF-8 XML with a BOM) into the
  `xdto_*` tables; verified against all 334 packages of the test configuration.
- `src/confdb/compare.py` — object snapshots and cross-database diff (`compare_object`,
  `extension_diff`); method/module bodies compared by sha1.
- `src/confdb/query_lang.py` — 1C query language lexer/parser/semantic validator
  (page `query-validator`).
- `src/confdb/db/writer.py` — SQLite schema + dump writer (batched inserts; BSL parsing
  parallelized via `workers`). Schema and write contracts: page `db-schema`.
- `src/confdb/bench.py` — hardware benchmark, saves the best `workers` to
  `~/.confdb/config.json` (`src/confdb/config.py` — shared user config, also used by the TUI).
- `src/confdb/v8/` — ported unpack core.
- `tests/` — fast tests (`test.bat`); `_tmp/` — throwaway probes (gitignored).

## Commands

```bat
.venv\Scripts\python.exe -m pip install -e ".[dev]"   :: once
test.bat                                              :: pytest
confdb.bat extract <file.cf> --db out.db --workers 8
confdb.bat extract <file.cf> --db out.db --force       :: rebuild even if the SHA-256 matches
confdb.bat extract <file.cf> --db out.db --no-fts      :: skip the body index (sidecar files)
confdb.bat fts out.db --workers 8                      :: build it later: 8 shards, 7.5 s
confdb.bat check out.db                               :: validate all SKD queries
confdb.bat bench <file.cf>                            :: tune workers to hardware
1confdb-knw.bat out.db                                :: MCP server (stdio)
1confdb-knw.bat out.db --port 8765                    :: MCP over HTTP (SSH tunnel)
.venv\Scripts\python.exe -m compileall -q src\confdb  :: static check
check-sync.bat                                        :: root src/tests vs both dist copies
```

Deployed copies used by the user: `dist\1confdb-knw` (published repo) and
`dist\1confdb-knw-lsp` (BSL-LS variant, remote `Zom31C/1confdb-knw-lsp`). The mainline venv
executes a real copy in `.venv\Lib\site-packages\confdb` (no editable `.pth` despite
`setup.bat`), so a `src` change has three destinations, not one. Sync order, what must never be
copied from the root, and how to check a copy without pytest: page `publication-two-repos`.

## Database schema

Tables: `source` (the file the base was built from, when, its size and SHA-256), `meta_object`,
`meta_attribute`, `meta_tabular`, `attribute_ref`, `module`,
`method`, `enum_value`, `predefined`, `predefined_subconto`, `common_target`,
`subsystem_content`, `skd_query`,
`xdto_import`/`xdto_type`/`xdto_property`, `file`. Column lists, the meaning of each
`type_str` form (including the three kinds of unresolved reference), how reference uuids are
resolved, and the SKD binary layout: page `db-schema`. Acceptance criterion for the query
validator: **375/375** SKD queries of the test configuration pass (`confdb check`) — keep it
green when touching `query_lang.py` / `writer.py`.

## Gotchas

The collection is the page `gotchas` (format, SQLite, Windows, MCP clients, query planner);
proven extraction gaps and their measurements are the page `metadata-extraction-gaps`. Read
the relevant one before changing the decoder, the writer or a query. Non-negotiable:

- **Never** `PRAGMA journal_mode=MEMORY` — an interrupted write leaves a "valid-looking"
  near-empty file.
- The brace-file parser returns numbers as **strings** — compare via `str(x)`.
- Field names are unique **within a tabular section**, not within an object — dedup by
  `(section, name)`.
- MCP server is read-only by design (`?mode=ro`, the `sql` tool rejects non-SELECT).
