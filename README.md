![FINN -- FINN Is Not NGINX](images/logo.png)

FINN reads **one** configuration file (JSON or JSONC) and produces a **fully
functional FastAPI backend backed by SQLite**, both as runnable source and as an
initialized database. The config describes two things:

1. **`database`** — the schema: tables, columns, types, keys, indexes, and relations.
2. **`api`** — the surface: endpoints, where each HTTP method maps to a SQL
   statement that FINN executes and returns as JSON.

FINN is opinionated and declarative: you do not write handler code; you write
SQL. FINN owns the plumbing — parameter binding, connection management,
validation, and error mapping. On build it generates a `/build` folder
containing both the SQLite database and the FastAPI source.

---

## 1. Overview

### 1.1 Goals

- **Single source of truth.** One file → schema + API.
- **Zero boilerplate handlers.** Each endpoint is just SQL.
- **Safe by default.** Parameterized SQL only; no raw string concatenation to build queries from user input.
- **Deterministic startup.** The config is validated fully before the app serves traffic; an invalid config refuses to start.
- **Idempotent bootstrapping.** FINN creates/migrates the SQLite schema so a fresh file produces a working server with no manual steps.
- **Build artifact.** `finn build` emits a self-contained `/build` directory: the generated FastAPI source plus the initialized `finn.db`.

### 1.2 Terminology

| Term | Meaning |
|---|---|
| **Config** | The single input file (`finn.json` / `finn.jsonc`). |
| **Build** | The generated `/build` output: FastAPI source + `finn.db`. |
| **Table** | A `database` entry describing one relation. |
| **Column** | A field of a table, with a name, type, and optional constraints. |
| **Relation / FK** | A column declaring a reference to another table's column. |
| **Endpoint** | An `api` entry identified by a path. |
| **Operation** | One HTTP method (`get`, `post`, …) on an endpoint, bound to a SQL statement. |

---

## 2. Config file format

### 2.1 Accepted extensions and parsing

- Extensions: `.json` and `.jsonc`.
- JSONC strips `//` line comments, `/* ... */` block comments, and trailing commas before parsing.
- Encoding: UTF-8 (BOM tolerated and stripped).
- One top-level **object** is required. Anything else is a parse error.

### 2.2 Validation

The config must conform to the schema in §3. Validation stages:

1. **Syntactic** — valid JSON/JSONC.
2. **Structural** — correct shape, allowed keys, correct types.
3. **Semantic** — referential integrity: every FK target exists; every endpoint's SQL references only declared parameters; every parameter maps to a real, allowed column/operator context; SQL parses (see §5.3).

Any failure produces a readable error with the JSON path of the offending
element (e.g. `database.tables.orders.columns.user_id`). The process **exits
non-zero** and does not produce a build or serve traffic.

---

## 3. Top-level schema

```jsonc
{
  "version": 1,
  "meta": { /* optional */ },
  "database": { ... },   // required
  "api": { ... }         // required
}
```

| Key | Type | Required | Notes |
|---|---|---|---|
| `version` | integer | yes | Config schema version. Currently `1`. |
| `meta` | object | no | Arbitrary metadata (name, description). Not used at runtime. |
| `database` | object | yes | Schema definition (§4). |
| `api` | object | yes | Endpoint definitions (§5). |

Unrecognized top-level keys are rejected (fail-fast) unless `"extraKeys": "ignore"` is set under `meta`.

---

## 4. The `database` object

```jsonc
"database": {
  "file": "finn.db",              // optional; output DB filename (see §4.7)
  "tables": {
    "<table_name>": { ...TableDef }
  },
  "indexes": [ ... ],        // optional
  "views":   { ... },        // optional
  "init":    [ "SQL", ... ]  // optional raw statements executed after schema creation
}
```

### 4.1 Table definition (`TableDef`)

```jsonc
{
  "columns":   { "<column_name>": { ...ColumnDef } },   // required, ≥1
  "primaryKey": ["id"],                                 // optional; list of column names
  "indexes":   [ ... ],                                 // optional, table-local indexes
  "withoutRowid": false                                 // optional
}
```

### 4.2 Column definition (`ColumnDef`)

```jsonc
{
  "type":      "integer",          // required
  "nullable":  false,              // optional, default false
  "default":   null,               // optional; literal or SQL expression string
  "primaryKey": false,             // optional, default false
  "autoincrement": false,          // optional, integer PKs only
  "unique":    false,              // optional
  "check":     "length(name) > 0", // optional SQL expression
  "references": "other_table.id",  // optional — relation (see §4.3)
  "onDelete":  "SET NULL",         // optional, with references
  "onUpdate":  "CASCADE",          // optional, with references
  "description": "..."             // optional
}
```

#### Column types (canonical set)

FINN normalizes a small, portable type vocabulary to SQLite storage classes:

| Config type | SQLite affinity | Notes |
|---|---|---|
| `integer` | INTEGER | Also `int`. |
| `real` | REAL | Also `float`, `double`. |
| `text` | TEXT | Also `string`, `varchar`. |
| `blob` | BLOB | Also `bytes`. |
| `boolean` | INTEGER | Stored 0/1; serialized to JSON `true`/`false` (§6.3). |
| `json` | TEXT | Stored as text; validated as JSON on write; returned embedded. |
| `datetime` | TEXT | ISO-8601 UTC text; validated on write. |

Unknown types are a config error.

### 4.3 Relations (foreign keys) — encoding decision

A relation is encoded **on the referencing column** via the `references` field:

- `"references": "other_table"` → resolves to `other_table.<primaryKey>` (single-column PK required).
- `"references": "other_table.column"` → explicit referenced column.

Runtime rules:

- The referenced **table** and **column** must exist (semantic validation).
- The referenced column must be declared `unique` or be the (sole) primary key.
- FINN emits the corresponding SQLite `FOREIGN KEY` clause and **always enables `PRAGMA foreign_keys = ON`** per connection.
- `onDelete` / `onUpdate` values (case-normalized): `NO ACTION`, `RESTRICT`, `SET NULL`, `SET DEFAULT`, `CASCADE`. If omitted, default `NO ACTION`.
- The referencing column's stored type is automatically coerced to match the target column's type if not explicitly set.

**Rationale:** column-level `references` keeps relations co-located with the attribute that expresses them, matches how developers read schemas, and avoids a parallel `relations` structure that can drift.

### 4.4 Indexes

```jsonc
{ "columns": ["user_id"], "unique": false, "name": "idx_orders_user" }
```

- Name auto-generated (`ix_<table>_<col>[_<col>…]`) when omitted.
- `unique: true` implies a uniqueness constraint.

### 4.5 `views`

```jsonc
"views": {
  "open_orders": { "sql": "SELECT ... FROM orders WHERE status='open'" }
}
```

Views are read-only SQL definitions, exposed for querying (endpoints may `SELECT` from them). A view column schema is inferred from SQLite at startup for validation purposes.

### 4.6 `init`

Ordered array of raw SQL statements (e.g. seed `INSERT`s, `PRAGMA`s) executed in a transaction immediately after schema creation, but only on **first** creation of the database (not on every boot). Documented as trusting content.

### 4.7 `database.file`

Filename (name only, no path) for the generated SQLite database. **Default: `finn.db`.** The database is **always emitted inside `/build`** (e.g. `/build/finn.db`); `database.file` names the file only. Overridable at build time with `FINN_DB_PATH` (a filename or path resolved relative to `/build`).

---

## 5. The `api` object

```jsonc
"api": {
  "basePath": "/api",                    // optional, default "/"
  "endpoints": {
    "<path>": { ...EndpointDef }
  }
}
```

### 5.1 Endpoint definition (`EndpointDef`)

An endpoint object's keys are **HTTP methods (lowercase)**; each value is an **operation** bound to SQL.

```jsonc
"endpoints": {
  "/users": {
    "get":  { "sql": "SELECT * FROM users", ... },   // full operation form
  },
  "/users/{id}": {
    "get":  "SELECT * FROM users WHERE id = :id",     // shorthand: bare SQL string
    "put":  { "sql": "...", ... },
    "delete": "DELETE FROM users WHERE id = :id"
  }
}
```

Supported method keys: `get`, `post`, `put`, `patch`, `delete`. Uppercase is
normalized to lowercase. Unknown keys are a config error.

**`head` and `options` are NOT configured directly** — FINN **delegates these to
FastAPI defaults**: FastAPI automatically derives `HEAD` from each `get`
operation and provides standard `OPTIONS` handling. Declaring them in the
config is unnecessary and ignored (with a warning).

### 5.2 Operation definition (`OperationDef`)

Full form:

```jsonc
{
  "sql": "SELECT ... WHERE id = :id",   // required
  "params": { ... },                    // optional, formal parameter spec (see §5.3)
  "response": { ... },                  // optional, shaping (see §6.3)
  "description": "...",
  "authentication": "none",             // optional (reserved, see §8)
  "rateLimit": null                     // optional (reserved)
}
```

Shorthand: a bare string is treated as `{ "sql": "..." }`.

### 5.3 SQL execution & parameter binding

- SQL is **SQLite dialect SQL** and is executed via the Python `sqlite3` driver.
- **Parameters must be named** and appear as `:name` (also allow `@name` / `$name`). Positional `?` is disallowed — it is ambiguous against HTTP inputs. (Question mark inside string literals/quotes is fine.)
- Parameter sources are bound in this precedence, and **each parameter must come from exactly one declared source**:

| Source | Syntax | Example |
|---|---|---|
| Path parameter | `{segment}` in the endpoint path | `/users/{id}` → `:id` |
| Query string | `:name` auto-bound from `?name=` | `?status=open` → `:status` |
| Request body (JSON) | `:name` from top-level JSON field | `{ "name": "Ada" }` → `:name` |
| Header | declared in `params` with `"source": "header"` | `:x_request_id` |

- **`params`** (optional) is a formal declaration used to disambiguate sources and enforce requiredness/type:

```jsonc
"params": {
  "status":  { "source": "query",  "required": true,  "type": "text" },
  "limit":   { "source": "query",  "type": "integer", "default": 50 },
  "x_req":   { "source": "header", "required": false }
}
```

- `params` is optional for `query`/`body`/`path` sources (auto-inferred from `:name` usage), but **required** to declare `header`-sourced parameters or to set defaults/coercions.
- **Every `:name` placeholder in SQL must resolve to a known source** at startup (semantic validation). Undeclared/unbindable placeholders are a config error.

#### Binding value semantics

- Query params lacking a formal default and absent at request time:
  - `required: true` → HTTP 400.
  - not required → bound as `NULL`.
- Bodily parameters missing → same rule; `NULL` if not required.
- Path parameters are always present (FastAPI guarantees it) and required.
- Coercion: values are coerced according to `type` where declared (integer parse failures → 400). Without a declared type, values arrive as their JSON/native type.

### 5.4 Single vs. multiple statements

- An operation's `sql` may be **one statement** (common case) or, when explicitly flagged `"multi": true`, multiple `;`-separated statements executed **in a transaction** (all-or-nothing). Without `multi`, embedded `;` is a config error.

### 5.5 Statement kind → HTTP semantics

FINN classifies the statement(s) and derives response behavior:

| Statement kind | Default method expectation | Response |
|---|---|---|
| `SELECT` | any | `200` + JSON array of rows (see §6.3). Empty set → `[]`. |
| `INSERT` | post | `200` + `{ "affected": n, "rowid": m }` (rowid when single insert). |
| `UPDATE` / `DELETE` | put/patch/delete | `200` + `{ "affected": n }`; **if `affected == 0`, return `404 NOT_FOUND`** (the edit changed nothing). |
| `CREATE`/`ALTER`/`DROP`/`PRAGMA` (DDL) | — | Rejected at runtime; **config error** if present in an endpoint (schema changes only via `database`/`init`). |

- Mismatch of method vs. statement (e.g. `delete` containing `SELECT`) is allowed but logged as a warning at startup.
- `POST`/`PUT` with a `SELECT` returning exactly zero rows → `404`; ≥1 rows → `200` with rows.

---

## 6. Response shaping

### 6.1 Default behavior

`SELECT` results are serialized as a **JSON array of objects**, keys = column names, values = values.

### 6.2 `response` overrides

```jsonc
"response": {
  "shape": "single",          // "list" (default) | "single" | "none"
  "key":    "row",            // wrap "single" as { "row": {...} }
  "columns": ["id","name"],   // optional projection/mapping
  "renames": { "id": "userId" }, // optional key rename
  "paginate": true            // optional, SELECT only
}
```

- `single`: return the first row as an object (404 if empty).
- `none`: return `204 No Content`.
- `paginate`: enables the pagination behavior detailed in §6.4.

### 6.3 Type serialization

- `integer`/`real` → JSON numbers.
- `boolean` → JSON `true`/`false`.
- `json` → **embedded** JSON (not a string).
- `datetime` → ISO-8601 string as stored.
- `NULL` → `null`.
- `blob` → base64 string with an empty-string marker by default (`"$base64:<data>"` configurable via `response.blob`).

### 6.4 Pagination

`paginate: true` on a `SELECT` operation provides first-class pagination:

- Query params: `page` (≥1, default 1) and `pageSize` (1–1000, default 50).
- Execution uses **two queries**:
  1. **Count query** — the operation SQL is wrapped as `SELECT COUNT(*) FROM ( <sql> ) AS _c` to compute `total`. This is correct across `GROUP BY`, joins, and subqueries. When the statement is trivially wrappable the wrapper is always used; for statements that cannot be safely wrapped (e.g. those already ending in `LIMIT`), FINN emits a dedicated `COUNT(*)` based on a detected row-source or, as a last resort, sets `total` to `null` and documents it for that operation.
  2. **Page query** — the operation SQL is wrapped as `SELECT * FROM ( <sql> ) AS _p LIMIT :__pageSize OFFSET :__offset`, where `offset = (page - 1) * pageSize`, preserving the author's ordering.
- Response envelope: `{ "items": [...], "page": n, "pageSize": s, "total": t, "totalPages": ceil(t/s) }`.
- `totalPages` is omitted (or `null`) when `total` could not be computed.
- `page` beyond available data returns `items: []` with the correct `total` (not an error).
- Out-of-range `page`/`pageSize` (`page < 1`, `pageSize > 1000`, non-integer) → `400 VALIDATION_ERROR`.
- Pagination is incompatible with `response.shape` values other than `list` — a config error.
- Reserved bind parameters `:__pageSize` and `:__offset` are auto-bound by FINN and must not be declared by the author.

---

## 7. HTTP error contract

Uniform error body:

```json
{ "error": { "code": "NOT_FOUND", "message": "...", "detail": "..." } }
```

| Code | HTTP | Cause |
|---|---|---|
| `VALIDATION_ERROR` | 400 | Missing/required param, type coercion failure, JSON body malformed. |
| `UNAUTHORIZED` | 401 | (Reserved — see §9.) |
| `FORBIDDEN` | 403 | (Reserved.) |
| `NOT_FOUND` | 404 | `single`/`select` on POST empty; path not matched (handled by FastAPI); **a mutation that affected zero rows** (§5.5). |
| `METHOD_NOT_ALLOWED` | 405 | Standard (FastAPI default). |
| `CONFLICT` | 409 | Unique/FK/check constraint violated on write (from SQLite error mapping). |
| `PAYLOAD_TOO_LARGE` | 413 | Body exceeds configured max (default 1 MB). |
| `UNPROCESSABLE_ENTITY` | 422 | (Reserved.) |
| `REQUEST_TIMEOUT` | 408 | Optional statement timeout (default 30 s). |
| `INTERNAL_ERROR` | 500 | Any unhandled failure. Message is generic; detail is in server logs only. |

SQLite errors are mapped by their error codes/constraint names (e.g. `UNIQUE constraint failed` → 409).

---

## 8. Build & run model

FINN has two entry points:

### 8.1 `finn build`

Validates the config (§2.2), creates the SQLite schema, runs `init` on first
creation, and **generates a self-contained `/build` folder** containing:

```
/build
├── main.py          # FastAPI entrypoint; assembles the app from API.py + DB.py
├── API.py           # generated routes (endpoints, params, response shaping)
├── DB.py            # generated schema, connection handling, data helpers
└── finn.db          # initialized SQLite database
```

- The build is **idempotent**: re-running against an existing DB does not destructively alter tables; it verifies compatibility and errors clearly on conflict (§9.4).
- The generated app is runnable standalone (`uvicorn build.main:app`, or `cd build && uvicorn main:app`); no FINN runtime is required to *serve* the built app.

### 8.2 `finn run [--config finn.jsonc]`

Builds (if out of date) and serves the generated app directly — a convenience
wrapper around build + serve.

### 8.3 Generated source layout

FINN does **not** emit a single monolith. The `/build` folder always contains
three Python modules plus the database:

- **`DB.py`** — schema + data layer. Contains the generated `CREATE TABLE` / index / view DDL (from §4), the `init` statements, a connection factory (applies `PRAGMA foreign_keys=ON`, `busy_timeout`, `journal_mode=WAL`), and helpers for serialization (§6.3) and SQLite-error mapping (§7). No HTTP concerns.
- **`API.py`** — HTTP layer. Contains the generated FastAPI `APIRouter` with one route per endpoint/operation, parameter models (from §5.3), response shaping (§6.2), and pagination (§6.4). Imports from `DB.py` only.
- **`main.py`** — entrypoint. Creates the `FastAPI` app, mounts the router under `api.basePath`, registers error handlers (§7), CORS, and `/healthz`; exposes `app` for `uvicorn build.main:app`.

This split keeps data-access and HTTP concerns decoupled, and leaves `main.py`
as a thin, human-readable bootstrap.

---

## 9. Security model

- **Parameterized queries only.** FINN never concatenates user input into SQL.
- **No DDL through the API.**
- **Foreign keys enforced** (`PRAGMA foreign_keys=ON`) per connection.
- **Statement timeout** and **request size limits** (§7) to bound abuse.
- **CORS:** off by default; enabled via explicit `api.cors` config.
- **Authentication/authorization:** declared in the vocabulary (`authentication`, scopes) but **out of scope for v1**; reserved keys are validated but have no effect yet, documented as such.
- **SQL injection surface from privileged paths** (`views`, `init`, `sql` fields) is curated-content-only: written by the config author (trusted), never from client input.

---

## 10. Runtime & generated application behavior

### 10.1 Bootstrap sequence

1. Load + validate config (§2.2).
2. Open (or create) the SQLite database file at `/build/<database.file>` (default `/build/finn.db`).
3. Create schema: tables, indexes, views, in dependency-safe order; `init` on first creation.
4. Inspect actual SQLite schema to resolve view/column metadata for validation.
5. Build FastAPI route tree from `api.endpoints`.
6. Serve.

### 10.2 Connection handling

- One SQLite file; per-request connection short-lived (open/close around the transaction) to avoid cross-thread issues, or a pool where available.
- `PRAGMA foreign_keys=ON`, `busy_timeout`, and `journal_mode=WAL` applied per connection.
- Simple lock/latency: SQLite serializes writes naturally; documented limitation.

### 10.3 Idempotent re-boot

On subsequent builds against an existing DB, FINN **does not** destructively alter existing tables; it verifies compatibility and errors clearly if the config conflicts with on-disk schema. (`init` runs only on first creation.)

### 10.4 Config hot-reload

**v1:** rebuild/restart to apply. Documented.

### 10.5 Logging & introspection

- Structured logs (request id, method, path, SQL statement id, duration, error code).
- Auto-generated OpenAPI (`/docs`, `/redoc`) reflects endpoints, params, and response shapes.
- `/healthz` (configurable) returns `200` + basic DB check.

---

## 11. Acceptance criteria (definition of done)

1. A single valid JSON/JSONC config produces a running FastAPI server with the described schema and endpoints, no manual steps.
2. `finn build` emits a `/build` folder containing `main.py`, `API.py`, `DB.py`, and the initialized `finn.db` (§8.1, §8.3).
3. Relations declared via column `references` are created and enforced.
4. Every documented parameter source (path/query/body/header) binds correctly.
5. Invalid configs fail at build/startup with a path-scoped, human-readable error and never partially serve.
6. Response shaping (`single`, `list`, pagination, renames, embedded JSON) behaves per §6.
7. A mutation (UPDATE/DELETE) that affects zero rows returns `404` (§5.5).
8. `HEAD`/`OPTIONS` requests are handled by FastAPI defaults.
9. Error contract (§7) is honored for validation, not-found, conflict, and internal errors.
10. No client input can alter the SQL text of an operation.

---

## 12. Full worked example

```jsonc
// finn.jsonc
{
  "version": 1,
  "database": {
    "file": "finn.db",
    "tables": {
      "users": {
        "columns": {
          "id":        { "type": "integer", "primaryKey": true, "autoincrement": true },
          "name":      { "type": "text" },
          "email":     { "type": "text", "unique": true },
          "created_at":{ "type": "datetime", "default": "datetime('now')" }
        }
      },
      "orders": {
        "columns": {
          "id":      { "type": "integer", "primaryKey": true, "autoincrement": true },
          "user_id": { "type": "integer", "references": "users.id", "onDelete": "CASCADE" },
          "total":   { "type": "real" },
          "status":  { "type": "text", "default": "'open'" }
        },
        "indexes": [ { "columns": ["user_id"] } ]
      }
    }
  },
  "api": {
    "basePath": "/api",
    "endpoints": {
      "/users": {
        "get":  "SELECT id, name, email FROM users",
        "post": { "sql": "INSERT INTO users (name, email) VALUES (:name, :email)" }
      },
      "/users/{id}": {
        "get":    { "sql": "SELECT * FROM users WHERE id = :id", "response": { "shape": "single" } },
        "delete": "DELETE FROM users WHERE id = :id"
      },
      "/orders": {
        "get": {
          "sql": "SELECT * FROM orders WHERE user_id = :user_id",
          "params": { "user_id": { "source": "query", "required": false } },
          "response": { "paginate": true }
        }
      }
    }
  }
}
```

---

## 13. Reserved / out of scope for v1 (explicit)

- AuthN/AuthZ enforcement (keys reserved only).
- Rate limiting.
- Migration (alter) of existing tables.
- Hot reload.
- Non-SQLite backends.
- Multiple config files / code generation to multiple targets.
