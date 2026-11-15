# FINN Database Layer — Integration Guide

**Audience:** the API team and the GUI/CLI team.
**Companion spec:** the top-level [`README.md`](../README.md) is authoritative; the sections
this package implements are §4 (schema), §4.2 (types), §4.3 (relations), §4.6 (`init`),
§6.3 (serialization), §7 (SQLite error mapping), §8.3 (generated `DB.py`), §10.2/§10.3
(connections + re-boot).

This package has **no HTTP/API concerns**. It does not know about endpoints, routing,
parameters, pagination, or response shaping. It is a data layer:

* parse & validate the `database` block of the config,
* generate SQLite DDL,
* create/maintain the SQLite file,
* coerce Python values for storage,
* serialize SQLite values for JSON output,
* map SQLite errors to the FINN error contract,
* emit the standalone `DB.py` module that your generated `/build` app imports.

Everything is **standard-library only** (`sqlite3`, `json`, `base64`, `dataclasses`).
No third-party import. FastAPI/uvicorn/pydantic are only needed to *run* the generated
app, never to use this package.

---

## 1. Importing

```python
from database import (
    # schema model + parsing
    DatabaseSpec, Table, Column, Index, View,
    parse_database, normalize_type, normalize_fk_action, affinity_for_declared_type,
    TYPE_ALIASES, CANONICAL_TYPES, FK_ACTIONS,
    # DDL
    quote_ident, table_ddl, index_ddl, view_ddl, schema_ddl, topological_table_order,
    # runtime: connections, init, data in/out, errors
    connect, bootstrap, db_init, describe_schema,
    DB_INIT_CONFLICT, DB_INIT_CONFIG, DB_INIT_DB, DB_INIT_FILE, DB_INIT_UNKNOWN,
    coerce, coerce_value, serialize_value, serialize_row, serialize_rows,
    map_sqlite_error,
    DEFAULT_BUSY_TIMEOUT_MS, DEFAULT_BLOB_FORMAT,
    # codegen
    render_db_module,
    # errors
    ConfigError, SchemaConflictError, ValidationError,
)
```

**Layout note:** `database` (and its sibling `api`) are top-level packages at the
repository root. `from database import ...` works as long as the repository root is
on `sys.path` (it is, when running from the repo root).

---

## 2. Data model

`parse_database()` returns a `DatabaseSpec`. The dataclasses are:

| Class | Fields |
|---|---|
| `DatabaseSpec` | `file="finn.db"`, `tables: dict`, `indexes: list`, `views: dict`, `init: list` |
| `Table` | `name`, `columns: dict`, `primary_key: list`, `indexes: list`, `without_rowid: bool` |
| `Column` | `name`, `fin_type`, `affinity`, `py_type`, `nullable=False`, `default=None`, `default_is_sql=False`, `primary_key=False`, `autoincrement=False`, `unique=False`, `check=None`, `references=None` (`(table, column)`), `on_delete`, `on_update`, `description=None` |
| `Index` | `name`, `table`, `columns: list`, `unique=False` |
| `View` | `name`, `sql` |

`DatabaseSpec` has two helper methods the API team will rely on:

```python
spec.column_types("orders")   # -> {"id": "integer", "user_id": "integer", ...}  (per table)
spec.all_column_types()       # -> flat name->type, but OMITS names whose type differs
                              #    across tables (see "gotchas" in §10).
```

### 2.1 Canonical column types

`Column.fin_type` is always one of the canonical names (never an alias). Aliases are
normalized during parsing:

| `fin_type` | aliases | SQLite affinity | Python type |
|---|---|---|---|
| `integer` | `int` | INTEGER | `int` |
| `real` | `float`, `double` | REAL | `float` |
| `text` | `string`, `varchar` | TEXT | `str` |
| `blob` | `bytes` | BLOB | `bytes` |
| `boolean` | — | INTEGER (stored 0/1) | `bool` |
| `json` | — | TEXT (JSON text) | `dict` |
| `datetime` | — | TEXT (ISO-8601) | `str` |

`normalize_type(value) -> Optional[str]` maps a raw type string to the canonical name
(returns `None` for unknown input); `parse_database` rejects unknown types with a
`ConfigError`. `affinity_for_declared_type(declared_type)` gives the SQLite affinity
class for a declared type string.

FK referential actions (`FK_ACTIONS`, `normalize_fk_action`): `NO ACTION`, `RESTRICT`,
`SET NULL`, `SET DEFAULT`, `CASCADE` (case/whitespace-insensitive; default `NO ACTION`).

---

## 3. Parsing the `database` block

```python
from database import parse_database, ConfigError

database_block = config["database"]      # the JSON object
try:
    spec = parse_database(database_block)
except ConfigError as err:
    # err.path is a JSON-path like "database.tables.orders.columns.user_id"
    print(err.path, err)                  # fail the build with a path-scoped message
```

`parse_database` is **strict**: unknown keys are rejected (fail-fast per spec §3), and
it performs full semantic validation — FK targets exist, a bare `references` resolves to
the target's single-column primary key, referenced columns must be `unique` or the sole
PK, `autoincrement` only on an `INTEGER` primary key, index columns exist, and so on.
Every failure is a `ConfigError` carrying a `.path`.

You should call this **once at build time**; keep the resulting `DatabaseSpec` in memory
and hand it to the DDL/codegen functions below.

---

## 4. DDL generation

```python
from database import schema_ddl, table_ddl, index_ddl, view_ddl, topological_table_order

statements = schema_ddl(spec)      # tables (dependency-safe order) + indexes + views
```

* `table_ddl(spec)` — `CREATE TABLE IF NOT EXISTS ...` for each table, with
  `PRIMARY KEY`, `AUTOINCREMENT`, `NOT NULL`, `UNIQUE`, `CHECK (...)`, `DEFAULT (...)`,
  and `FOREIGN KEY (...) REFERENCES ... ON DELETE/UPDATE ...`.
* `index_ddl(spec)` — `CREATE [UNIQUE] INDEX ...`; auto-named `ix_<table>_<col>[_<col>…]`
  when the config omits `name`.
* `view_ddl(spec)` — `CREATE VIEW IF NOT EXISTS ... AS <sql>`.
* `topological_table_order(spec)` — table names ordered so every FK target is created
  before its referencing table (stable, cycle-safe, self-references allowed).
* `quote_ident(name)` — safe identifier quoting (doubles internal `"`); use it for any
  identifier you interpolate into SQL you build yourself.

You normally do **not** need these directly: `bootstrap()` (§5) runs the schema for you,
and `render_db_module()` (§9) embeds the same DDL into the generated `DB.py`. They exist
so the CLI/build layer can preview or log the schema, and for tests.

---

## 5. Connecting & bootstrapping

```python
from database import connect, bootstrap

# Open a connection. Parent directory is created as needed. Caller owns close().
conn = connect("build/finn.db")          # also accepts ":memory:" and ""
try:
    ...
finally:
    conn.close()
```

`connect(path, *, create=True, busy_timeout_ms=5000)` applies the mandated pragmas on a
**per-connection** basis (§10.2): `PRAGMA foreign_keys=ON`, `PRAGMA busy_timeout=…`,
`PRAGMA journal_mode=WAL`, and sets `row_factory = sqlite3.Row`. With `create=False` a
missing file raises `FileNotFoundError`.

```python
created = bootstrap("build/finn.db", spec)      # -> bool
```

`bootstrap(path, spec)` returns `True` only when the database had no schema objects yet,
in which case it creates tables/indexes/views **and** runs the `init` statements — all in
one transaction (§4.6). On an **existing** database it:

1. verifies the on-disk schema matches the spec (`_verify_compatible`: table/column
   existence, affinity, `NOT NULL`, PK membership, uniqueness) and raises
   `SchemaConflictError` on the first mismatch (§10.3) — **before** running any DDL;
2. then applies only net-new objects (driven by `IF NOT EXISTS`) — never drops or alters
   existing tables.

Catch `SchemaConflictError` (a `ConfigError` subclass) at build time and tell the user
their config conflicts with the existing `/build/DB.py` database.

For callers that only need a success flag plus a machine-readable failure reason, use
`db_init(path, spec)`. It wraps `bootstrap()` and never raises:

```python
success, error_code = db_init("build/finn.db", spec)   # -> (bool, str | None)
```

* Success -> `(True, None)`.
* Failure -> `(False, "<error_code>")`; the exception detail is logged through the
  `database` logger.

| Code | Source exception |
|---|---|
| `SCHEMA_CONFLICT` (`DB_INIT_CONFLICT`) | `SchemaConflictError` |
| `CONFIG_ERROR` (`DB_INIT_CONFIG`) | `ConfigError` (non-conflict) |
| `DB_ERROR` (`DB_INIT_DB`) | `sqlite3.Error` |
| `FILE_ERROR` (`DB_INIT_FILE`) | `OSError` |
| `UNKNOWN` (`DB_INIT_UNKNOWN`) | any other exception (defensive) |

`bootstrap()` remains available for callers that need the "newly created" boolean or
the exception detail (path/column message, declared vs. on-disk type) at build time.

`describe_schema(connection)` returns column metadata for every table **and** view:

```python
{
  "users":  {"id": {"type": "INTEGER", "notnull": True,  "pk": True, "default": None}, ...},
  "orders": {...},
  "active_users": {...},   # views included
}
```

---

## 6. Writing data (coercion)

Before binding a value into `sqlite3`, coerce it through the column's type so the stored
form matches §4.2 / §6.3:

```python
from database import coerce, coerce_value, ValidationError

try:
    stored = coerce("boolean", True)        # -> 1
    stored = coerce_value(column, {...})    # column: Column OR a fin_type string
except ValidationError as err:
    ...   # 400 to the client
```

`coerce_value(column_or_type, value)` rules (also applies to `coerce(fin_type, value)`):

| type | accepted input | stored form | invalid → |
|---|---|---|---|
| `boolean` | `bool`, `0`/`1`, `"true"/"false"/"1"/"0"` | `0` or `1` (int) | `ValidationError` |
| `json` | JSON string (validated with `json.loads`, stored verbatim), or `dict`/`list`/`tuple` (`json.dumps`) | TEXT | `ValidationError` |
| `datetime` | ISO-8601 string (`Z` accepted as UTC) | string verbatim | `ValidationError` |
| `integer` | `int` (bool accepted) | int | `ValidationError` |
| `real` | `int`/`float` | float | `ValidationError` |
| `text` | `str` | str | `ValidationError` |
| `blob` | `bytes`/`bytearray`/`memoryview` | bytes | `ValidationError` |

`None` always passes through (→ `NULL`); whether `NULL` is acceptable is the column's
`nullable`/DDL concern, not the coercion layer's.

---

## 7. Reading data (serialization)

After a `SELECT`, convert rows for JSON output (§6.3):

```python
from database import serialize_rows, serialize_row, serialize_value

rows = conn.execute("SELECT ...").fetchall()
typed = spec.column_types("users")                 # authoritative per-table map
payload = serialize_rows(rows, typed)              # -> list[dict], JSON-ready

# or single row / single value:
serialize_row(row, typed, columns=["id", "name"])
serialize_value(True, "boolean")                    # -> True
```

`serialize_rows(rows, column_types=None, *, columns=None, blob_format="$base64:{}")`:

* `integer`/`real`/`text`/`datetime` → passed through (numbers, strings, ISO text).
* `boolean` → JSON `true`/`false` (SQLite stores `0`/`1`).
* `json` → **embedded** value (`json.loads`), not a string.
* `blob` → `"$base64:<b64>"` (marker `blob_format` is configurable).
* `NULL` → `null`; unknown/absent column type → stored value passed through.

`rows` may be `sqlite3.Row` or plain `dict` (including `dict(zip(...))` results);
a `columns` list disambiguates order when rows are bare tuples.

---

## 8. Error mapping

```python
from database import map_sqlite_error

try:
    conn.execute(statement, params)
    conn.commit()
except sqlite3.Error as err:
    mapped = map_sqlite_error(err)          # -> dict | None
    if mapped:                              # -> {"code":"CONFLICT","http_status":409,
        ...                                  #     "message":..., "detail":...}
    else:
        ...  # 500 generic
```

`map_sqlite_error` maps `sqlite3.IntegrityError` (UNIQUE / FOREIGN KEY / CHECK / NOT NULL)
to `{"code": "CONFLICT", "http_status": 409, "message": ..., "detail": <sqlite message>}`,
distinguishing the four kinds in `message`. Any other SQLite error returns `None` so you
can fall back to a generic 500. (`http_status` is present for convenience; the API layer
may re-derive status from `code` if it prefers.)

---

## 9. Emitting `DB.py`

At build time, generate the standalone data-layer module (§8.3):

```python
from database import render_db_module

source = render_db_module(spec, db_filename="finn.db")   # db_filename defaults to spec.file
open("build/DB.py", "w", encoding="utf-8").write(source)
```

The emitted `DB.py` is **self-contained** (standard library only, never `import finn`),
so a `/build` directory runs without the FINN generator. It exposes:

| Symbol | Purpose |
|---|---|
| `DB_PATH` | `Path(__file__).resolve().parent / <db_filename>` |
| `BUSY_TIMEOUT_MS`, `BLOB_FORMAT` | mirrors `DEFAULT_BUSY_TIMEOUT_MS`, `DEFAULT_BLOB_FORMAT` |
| `SCHEMA_DDL`, `INIT_STATEMENTS` | the embedded DDL + `init` |
| `COLUMN_TYPES`, `COLUMN_TYPES_BY_NAME` | per-table and flat (ambiguous-excluded) type maps |
| `connect(path=None, *, create=True, busy_timeout_ms=...)` | same pragmas as §5 |
| `bootstrap(path=None)` | create schema; `init` only on first creation (returns `bool`) |
| `db_init(path=None)` | like `bootstrap`, but returns `(bool, error_code)` and never raises |
| `query(sql, params=None, *, path=None)` | run one parameterized statement |
| `coerce` / `coerce_value` / `serialize_value` / `serialize_row` / `serialize_rows` | as in §6/§7 |
| `map_sqlite_error(exc)` | as in §8 |
| `ValidationError` | as in §6 |

`DB.query()` returns `{"rows": [...], "affected": n, "rowid": m}` — `rows` already
serialized (§7), `rowid` set only when exactly one row was inserted, and it
auto-`bootstrap()`s the file if it is missing. This is the function the generated API
routes should call; the API layer can then shape the response (envelope, pagination,
status codes) on top.

---

## 10. Gotchas & multi-team contract

1. **Per-table type maps are authoritative.** `spec.column_types(table)` and
   `DB.COLUMN_TYPES` are exact. `all_column_types()` / `COLUMN_TYPES_BY_NAME` **omit** a
   column name that is declared with *different* types in two tables (it would be
   ambiguous to serialize from the name alone). When you serialize an ad-hoc `SELECT`
   spanning tables, prefer the per-table map for its own columns; untyped columns are
   passed through unchanged.
2. **Connections are short-lived and caller-owned.** `connect()` returns a fresh
   connection with `foreign_keys=ON` etc.; close it when done. `check_same_thread` is
   `sqlite3`'s default, so do not share a connection across threads.
3. **FK enforcement is per-connection.** Always use `connect()` (or the generated
   `DB.connect()`); a raw `sqlite3.connect` will *not* enable `foreign_keys`.
4. **Don't rebuild the DDL or types yourself.** If you need schema/serialization, import
   from `database` (build time) or `DB.py` (runtime). Re-deriving it elsewhere risks
   the two layers drifting.
5. **`bootstrap` vs `init`.** `init` runs once, only at first creation. Re-running
   `bootstrap` on an existing DB verifies compatibility and does not touch data.
6. **Error taxonomy.** `ConfigError` (parse/validation, `.path`), `SchemaConflictError`
   (subclass, §10.3 re-boot), `ValidationError` (subclass of `ValueError`, write-time).
   Catch `ConfigError` to cover the first two.

---

## 11. End-to-end example (build side)

```python
from database import (
    parse_database, db_init, bootstrap, render_db_module,
    coerce, serialize_rows, map_sqlite_error,
)

# 1. parse + validate the database block
spec = parse_database(config["database"])

# 2. create/verify the SQLite file (init runs only on first creation).
#    Recommended entry: db_init returns (bool, error_code) and never raises.
ok, code = db_init("build/finn.db", spec)
if not ok:
    print(f"db init failed: {code}"); raise SystemExit(1)

# bootstrap() is the richer alternative when you need the exception detail:
#   created = bootstrap("build/finn.db", spec)   # -> bool (True = newly created)

# 3. emit the standalone data layer
open("build/DB.py", "w", encoding="utf-8").write(
    render_db_module(spec, db_filename="finn.db")
)

# 4. example write path (also available at runtime via DB.query/coerce)
from database import connect, coerce
conn = connect("build/finn.db")
try:
    name, email = "Ada", "ada@example.com"
    conn.execute(
        "INSERT INTO users (name, email) VALUES (?, ?)",
        (coerce("text", name), coerce("text", email)),
    )
    conn.commit()
finally:
    conn.close()
```

At **runtime** (inside the generated app) the API layer instead imports the emitted
`DB.py` and calls `DB.query()`, `DB.coerce()`, `DB.serialize_rows()`, and
`DB.map_sqlite_error()` — no `database` import needed in `/build`.

---

_All signatures documented here are the stable public surface; treat them as a contract._
_They are covered by `tests/test_database.py` (stdlib `unittest`)._
