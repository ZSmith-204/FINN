# FINN CLI Specification

**Status:** Draft v0.1
**Date:** 2026-11-15
**Companions:** [`README.md`](../README.md) (authoritative product spec), [`docs/gui-spec.md`](gui-spec.md) (GUI that reuses this contract), [`docs/database-layer.md`](database-layer.md) ("DB Layer Guide" — existing database-layer package)

---

## 1. Scope

FINN reads **one** JSON/JSONC config file (README §2) and produces a self-contained
`/build` directory — FastAPI source plus an initialized SQLite database (README §8.1).
This document specifies the `finn` command-line interface that performs that work.

Core commands:

| Command | Purpose | README |
|---|---|---|
| `finn init [TARGET_DIR]` | Scaffold a new project by writing a starter `finn.jsonc` | new (this spec) |
| `finn build [CONFIG]` | Validate the config and emit `/build` (sources + database + manifest) | §8.1, §8.3 |
| `finn run [CONFIG]` | Build if stale, then serve the generated app | §8.2 |
| `finn validate [CONFIG]` | Run parse/structural/semantic validation only; write nothing | §2.2 |

`validate` is a supporting command, not a fourth core verb: it exists for CI and as the
machine-readable interface the GUI reuses (gui-spec §3.1).

**Audience:** CLI implementers, GUI implementers, and users scripting FINN.
**Normative language:** MUST / SHOULD / MAY (RFC 2119).

## 2. Invocation, streams, and output modes

- Commands are invoked as `finn <command> [args] [flags]`.
- **stdout** carries results: summaries, banners, and `--json` documents.
- **stderr** carries diagnostics, warnings, and logs. In `--json` mode, diagnostics
  appear inside the JSON document on stdout instead; stderr then carries only
  truly fatal/usage messages (which MAY remain human-readable).
- **Color** is applied only when the target stream is a TTY. It MUST be disabled when
  the stream is not a TTY or when `NO_COLOR` or `FINN_NO_COLOR` is set (to any value).
- **`--json`** switches the command's primary output to a single JSON document on
  stdout (UTF-8). Exceptions: `run --json` emits newline-delimited JSON (NDJSON)
  events (§6.3). The `--json` shapes in this spec are the integration contract the
  GUI consumes (gui-spec §3).

## 3. Config discovery

All file-taking commands resolve the config in this precedence order:

1. `--config PATH` (explicit)
2. `$FINN_CONFIG`
3. `./finn.jsonc`
4. `./finn.json`

Rules:

- If **both** `finn.jsonc` and `finn.json` exist in the search directory, `finn.jsonc`
  wins and a warning diagnostic (`FINN-W0001`) is emitted.
- If no config is found: `FINN-E0011`, exit 1.
- Parsing follows README §2.1: UTF-8 (BOM tolerated and stripped), `//` and `/* */`
  comments stripped, trailing commas stripped, one top-level object required.
  Syntactic failures are `FINN-E0012` with a filled `span` when available.

## 4. Diagnostics

Every command reports problems through one diagnostic schema:

```json
{
  "severity": "error",
  "code": "FINN-E0031",
  "stage": "semantic",
  "path": "database.tables.orders.columns.user_id",
  "message": "referenced column 'users.mail' is neither unique nor a primary key",
  "hint": "add \"unique\": true to users.mail, or reference users.id",
  "span": { "line": 14, "column": 7 }
}
```

| Field | Meaning |
|---|---|
| `severity` | `error` \| `warning` \| `info` |
| `code` | Stable machine code (table below) |
| `stage` | `parse` \| `structural` \| `semantic` \| `build` \| `runtime` |
| `path` | JSON path into the config (e.g. `database.tables.orders.columns.user_id`), or `null` |
| `message` | Human-readable description |
| `hint` | Actionable suggestion, or `null` |
| `span` | `{line, column}` (1-based) or `null`; best-effort, filled for parse errors in v1 |

The `path` format MUST be identical to the JSON paths produced by
`database`'s `ConfigError` (DB Layer Guide §3) — see §12.

Human format (stderr):

```
error [FINN-E0031] database.tables.orders.columns.user_id: referenced column 'users.mail' is neither unique nor a primary key
  hint: add "unique": true to users.mail, or reference users.id
```

### 4.1 Error codes

Codes are stable; the table may grow. Unknown codes MUST be displayed, never dropped.

| Code | Stage | Cause |
|---|---|---|
| `FINN-E0011` | parse | Config file not found |
| `FINN-E0012` | parse | JSONC parse error (span filled when available) |
| `FINN-E0021` | structural | Structural validation error (README §2.2 stage 2) |
| `FINN-E0031` | semantic | Semantic validation error (README §2.2 stage 3) |
| `FINN-E0041` | build | Build I/O error (unreadable/unwritable path, missing build dir for `run --no-build`) |
| `FINN-E0042` | build | On-disk schema conflicts with the config (README §10.3); human must resolve |
| `FINN-E0051` | runtime | Server failed to start (e.g. port already in use) |
| `FINN-E0061` | build | `init` target already exists |
| `FINN-E0091` | — | Usage error (unknown command/flag) |

### 4.2 Warning codes

Warnings never block and never change the exit code.

| Code | Cause |
|---|---|
| `FINN-W0001` | Both `finn.jsonc` and `finn.json` found; using `finn.jsonc` |
| `FINN-W0002` | Declared `head`/`options` operation ignored (README §5.1) |
| `FINN-W0003` | Method/statement mismatch, e.g. `delete` containing `SELECT` (README §5.5) |

## 5. Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Validation failure: at least one `severity: "error"` diagnostic from the parse/structural/semantic stages (README §2.2) |
| `2` | Runtime/I-O failure: `FINN-E0041`, `FINN-E0042`, `FINN-E0051`, `FINN-E0061`, or unexpected filesystem failure |
| `3` | Usage error (unknown command/flag; `FINN-E0091`) |
| `130` | Interrupted: SIGINT/SIGTERM during a non-interactive command (e.g. a build aborted mid-run) |

Notes:

- Exit codes are unchanged in `--json` mode; the document carries the same outcome.
- A graceful `run` shutdown via Ctrl+C / SIGTERM exits `0` (§6.3).

## 6. Commands

### 6.1 `finn init [TARGET_DIR]`

Scaffolds a new project. FINN is a **single-file-config** project (README §1.2), so
`init` creates ONLY the config file — never `build/`, never a database.

**Flags**

| Flag | Default | Meaning |
|---|---|---|
| `--template example\|minimal\|blank` | `example` | Starter template (§6.1.1–§6.1.3) |
| `--force` | off | Overwrite the target config |
| `--name NAME` | — | Write `meta.name` into the generated config |
| `--json` | off | Machine output |

**Target resolution**

- Default target directory is the current directory; the positional `TARGET_DIR`
  names a directory (created if missing). The output file is always `<dir>/finn.jsonc`.
- If `<dir>/finn.jsonc` **or** `<dir>/finn.json` already exists: `FINN-E0061`, exit 2,
  naming the offending file, hint `--force`. With `--force`, only the file `init`
  would write is overwritten.

**Post-write output (human)**

```
Wrote finn.jsonc (template: example)
Next steps:
  finn build
  finn run
```

**`--json`**

```json
{ "ok": true, "template": "example", "path": "finn.jsonc", "created": true }
```

#### 6.1.1 Template `example` (default)

The full worked example from README §12, verbatim, emitted as JSONC with a one-line
header comment:

```jsonc
// FINN starter config (template: example) — see README.md §12
{ /* ...exact README §12 content... */ }
```

#### 6.1.2 Template `minimal`

```jsonc
{
  "version": 1,
  "database": {
    "file": "finn.db",
    "tables": {
      "users": {
        "columns": {
          "id":         { "type": "integer", "primaryKey": true, "autoincrement": true },
          "name":       { "type": "text" },
          "email":      { "type": "text", "unique": true },
          "created_at": { "type": "datetime", "default": "datetime('now')" }
        }
      }
    }
  },
  "api": {
    "basePath": "/api",
    "endpoints": {
      "/users": {
        "get":  "SELECT id, name, email FROM users",
        "post": "INSERT INTO users (name, email) VALUES (:name, :email)"
      }
    }
  }
}
```

#### 6.1.3 Template `blank`

```jsonc
{
  "version": 1,
  "meta": {},
  "database": { "tables": {} },
  "api": { "endpoints": {} }
}
```

> Assumption: README does not require `database.tables` to be non-empty (§4.1 requires
> ≥1 column *per table*, not ≥1 table). `finn validate` MUST therefore accept an empty
> tables map. If that assumption is invalidated upstream, the `blank` template MUST be
> updated to remain valid.

### 6.2 `finn build [CONFIG]`

Validates the config (README §2.2) and emits the build directory (README §8.1/§8.3).

**Flags**

| Flag | Default | Meaning |
|---|---|---|
| `--config PATH` | discovery (§3) | Config file |
| `--out DIR` | `<config-dir>/build` | Output directory |
| `--quiet` | off | Suppress non-diagnostic output |
| `--json` | off | Machine output |

There are **no destructive bypass flags** in v1. A config that conflicts with an
on-disk database (`FINN-E0042`) must be resolved by a human; FINN never destructively
alters existing tables (README §10.3).

**Pipeline**

| Stage | Work | Failure |
|---|---|---|
| 0 | Locate config (§3) | `E0011` |
| 1 | Parse JSONC (README §2.1) | `E0012` |
| 2 | Structural validation (README §2.2) | `E0021` |
| 3 | Semantic validation (README §2.2): FK targets, parameter resolution, statement classification; **DDL in endpoints rejected** (README §5.5) | `E0031` |
| 4 | Emit `main.py`, `API.py`, `DB.py` into `<out>` | `E0041` |
| 5 | Create/verify SQLite DB at `<out>/<database.file>` (default `finn.db`); `FINN_DB_PATH` overrides, resolved relative to `<out>` (README §4.7); `init` runs only on first creation | `E0041`, `E0042` |
| 6 | Write manifest `<out>/.finn-build.json` (§6.2.1) | `E0041` |
| 7 | Print summary | — |

Generated-source rules (README §8.3):

- `main.py`, `API.py`, `DB.py` are **FINN-owned**: overwritten on every build, each
  beginning with a `# generated by FINN — do not edit` header.
- `DB.py` MUST be produced by `render_db_module` (§12); the CLI MUST NOT re-derive DDL.

**Failure semantics**

- Any `severity: "error"` diagnostic at stages 0–3 aborts the build **before anything
  is written** — no partial build output (README §11 criterion 5). Exit 1.
- I/O failures during stages 4–6: `FINN-E0041`, exit 2, message includes the path.
- On-disk schema conflict at stage 5: `FINN-E0042`, exit 2, with the conflict detail
  (table/column, declared vs. on-disk) from the underlying error (§12).

**Summary (human)**

```
Built finn.jsonc → build/  (0.4s)
  tables: 2   columns: 7   indexes: 1   views: 0
  endpoints: 3   operations: 4
  database: build/finn.db (created)
  artifacts:
    build/main.py     1.2 KB  created
    build/API.py      4.6 KB  updated
    build/DB.py       9.1 KB  updated
    build/finn.db    40.0 KB  updated
    build/.finn-build.json  0.4 KB  created
```

**`--json`**

```json
{
  "ok": true,
  "config": "finn.jsonc",
  "outDir": "build",
  "artifacts": [
    { "path": "build/main.py", "bytes": 1229, "action": "created" },
    { "path": "build/API.py",  "bytes": 4710, "action": "updated" },
    { "path": "build/DB.py",   "bytes": 9318, "action": "updated" },
    { "path": "build/finn.db", "bytes": 40960, "action": "updated" }
  ],
  "db": { "path": "build/finn.db", "created": false },
  "diagnostics": [],
  "durationMs": 412
}
```

#### 6.2.1 Build manifest `<out>/.finn-build.json`

```json
{
  "finnVersion": "0.1.0",
  "configSha256": "<sha256 of the config file bytes>",
  "builtAt": "2026-11-15T12:00:00Z",
  "artifacts": [ { "file": "main.py", "sha256": "<sha256>", "bytes": 1229 } ],
  "db": { "file": "finn.db", "created": true }
}
```

The manifest is the staleness input for `finn run` (§6.3) and the artifact record for
the GUI (gui-spec §6.6). `db.created` is `true` only when the database was newly
created (and therefore `init` ran).

### 6.3 `finn run [CONFIG]`

Builds if stale, then serves the generated app (README §8.2).

**Flags**

| Flag | Default | Meaning |
|---|---|---|
| `--config PATH` | discovery (§3) | Config file |
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8000` | Port |
| `--no-build` | off | Serve the existing build as-is; skip the staleness check |
| `--quiet` | off | Suppress banner |
| `--json` | off | NDJSON events on stdout |

**Staleness rule.** Read `<out>/.finn-build.json`; rebuild if it is missing, OR
`configSha256` differs from the config file's current SHA-256, OR `finnVersion`
differs from the running CLI's version. Otherwise skip the rebuild.
`--no-build` skips the check entirely and raises `FINN-E0041` if `build/` is missing.

**Serving.** Launch the generated app programmatically (e.g. uvicorn targeting
`build.main:app` — implementation hint, non-normative). The built app is runnable
standalone without FINN (README §8.1); `run` is a convenience wrapper.

**Banner (human)**

```
FINN dev server
  App:     http://127.0.0.1:8000
  Docs:    http://127.0.0.1:8000/docs
  Redoc:   http://127.0.0.1:8000/redoc
  Health:  http://127.0.0.1:8000/healthz
  PID:     4242
  Config changes require a restart (no hot reload in v1 — README §10.4).
  Standalone: uvicorn build.main:app
```

**Signals.** SIGINT/SIGTERM shut down gracefully; `run` exits `0`. A port already in
use is `FINN-E0051`, exit 2, hint: choose another `--port`.

**No hot reload in v1.** Config changes require stop + re-run (README §10.4, §13).
`run` MUST NOT offer a `--reload` flag in v1.

**`--json` events (NDJSON on stdout)**

| Event | Fields |
|---|---|
| `building` | — |
| `build_ok` | — |
| `listening` | `host`, `port`, `pid` |
| `stopped` | `exitCode` |
| `error` | `diagnostic` (§4 object) |

```
{"event":"building"}
{"event":"build_ok"}
{"event":"listening","host":"127.0.0.1","port":8000,"pid":4242}
{"event":"stopped","exitCode":0}
```

Server logs go to stderr in all modes.

### 6.4 `finn validate [CONFIG]`

Runs pipeline stages 0–3 only (§6.2). Writes nothing. Exit `0` iff no
`severity: "error"` diagnostic; warnings and infos are reported but do not affect the
exit code.

**`--json`**

```json
{ "ok": false, "diagnostics": [ { "severity": "error", "code": "FINN-E0031", "...": "..." } ] }
```

## 7. Global options and environment

- `finn --help`, `finn help [command]` — usage text.
- `finn --version` prints `finn <semver>`.
- `finn --version --json` prints:

```json
{ "version": "0.1.0", "configSchema": 1 }
```

`configSchema` is the supported config `version` (README §3) and is the handshake
field the GUI checks (gui-spec §3.2).

**Environment variables (v1 — no others)**

| Variable | Meaning |
|---|---|
| `FINN_CONFIG` | Default config path (§3 precedence) |
| `FINN_DB_PATH` | Overrides the output DB filename/path, resolved relative to the build dir (README §4.7) |
| `NO_COLOR`, `FINN_NO_COLOR` | Disable color output (§2) |

## 8. Cross-cutting invariants

1. SQL reaching the generated API is parameterized with named placeholders only;
   positional `?` in endpoint SQL is rejected at stage 3 (README §5.3).
2. DDL (`CREATE`/`ALTER`/`DROP`/`PRAGMA`) in endpoint SQL is a config error (README §5.5).
3. SQLite is the only engine (README §1.2).
4. Generated files are FINN-owned and overwritten; user data lives only in the config
   and the database.
5. An existing database is never destructively altered; conflicts are `FINN-E0042`
   requiring human action (README §10.3).
6. `init` SQL runs only on first database creation (README §4.6).
7. Diagnostics carry JSON paths in the same format as `database` `ConfigError.path`.
8. The config remains a single file; the build is a single directory (README §1.2).

## 9. Non-goals (v1)

Watch/daemon mode; migrations beyond create-if-missing; multi-config projects;
deploy tooling; auth enforcement; non-SQLite engines; plugins; config hot-reload.
Deferred features are tracked in README §13.

## 10. Acceptance criteria

1. Each `init` template (`example`, `minimal`, `blank`) passes `finn validate` immediately after `finn init`.
2. `finn init` refuses to overwrite an existing `finn.jsonc`/`finn.json` without `--force` (`FINN-E0061`, exit 2).
3. `finn build` emits `main.py`, `API.py`, `DB.py`, `<database.file>`, and `.finn-build.json`; every artifact is listed in the manifest.
4. An invalid config exits non-zero with a path-scoped diagnostic and produces **no** partial build output.
5. Rebuilding against an existing compatible database is idempotent and non-destructive; `db.created` is `false` and `init` does not re-run.
6. A config conflicting with the on-disk schema exits `2` with `FINN-E0042` and detail identifying the conflicting column.
7. `finn run` auto-builds when the manifest is missing or stale, and serves `/healthz` with `200`.
8. `finn run --json` emits parseable NDJSON events; every other `--json` output parses as a single JSON document.
9. Exit codes follow §5 exactly.
10. DDL or positional-`?` SQL in an endpoint fails at stage 3 with a path-scoped `FINN-E0031` and never reaches runtime.

## 11. Open items

- **API-block parser ownership:** the `database` block parser exists (`database`,
  §12); the API-block parser is owned by another team. The CLI orchestrates both and
  requires the same `ConfigError.path` diagnostic shape from each.
- **Entry point/module name:** see §12; final name pending team coordination.
- **Minimum-version policy for the GUI handshake:** the Studio release declares a
  minimum `finn` version (gui-spec §3.2); the versioning scheme is defined here as
  semver but the policy lives with the GUI spec.
- **README gaps** (`api.cors`, `response.blob`, configurable `/healthz`,
  query-vs-body inference, multi-statement response semantics, database-level index
  `table`, coerced-type vs. required-`type`) are consolidated in gui-spec §11.

## 12. Existing substrate / implementation mapping (non-normative)

The `database` package already implements the data layer (DB Layer Guide). The
CLI orchestrates it — it MUST NOT re-derive schema DDL, type maps, or validation.

| CLI concern | Existing surface | Guide |
|---|---|---|
| Stage 3 (`database` block) | `parse_database(block)` → `DatabaseSpec`; `ConfigError` carries `.path` — the exact diagnostic `path` format (§4) | §3 |
| Stage 5 (create/verify DB) | `bootstrap(path, spec) -> bool` (`True` = newly created ⇒ manifest `db.created: true`; raises `SchemaConflictError` → `FINN-E0042` before any DDL) or `db_init(path, spec) -> (ok, code)` (never raises) | §5 |
| `db_init` code mapping | `SCHEMA_CONFLICT` → `E0042`; `CONFIG_ERROR` → `E0031`/`E0021` by stage; `DB_ERROR`/`FILE_ERROR`/`UNKNOWN` → `E0041` | §5 |
| Stage 4 (`DB.py`) | `render_db_module(spec, db_filename)` → standalone, stdlib-only source | §9 |
| Preview/logging support | `schema_ddl`, `table_ddl`, `index_ddl`, `view_ddl`, `topological_table_order`, `quote_ident` (used by the GUI's DDL preview, gui-spec §6.3.8) | §4 |
| Error taxonomy | `ConfigError` / `SchemaConflictError` (subclass) / `ValidationError` — catching `ConfigError` covers the first two | §10.6 |

Packaging note: `api` and `database` are top-level packages at the repository root.
Suggested (non-normative) CLI entry: a `cli.py` module at the repository root exposed
via a console script, or `python -m cli`.

---

**End of spec (v0.1 Draft).**
