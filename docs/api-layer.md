# FINN API Layer — Integration Guide

**Audience:** the CLI/build team (and anyone wiring FINN together).
**Companion spec:** [`README.md`](../README.md) §5 (api/endpoints/operations/params), §5.3 (binding),
§5.5 (statement kinds → HTTP semantics), §6.2/§6.4 (response shaping + pagination),
§7 (error contract), §8.1/§8.3 (generated `API.py`/`main.py`).
**Sibling layer:** see [`docs/database-layer.md`](database-layer.md) — this package mirrors it.

This package turns the `api` block of the config into a runnable FastAPI app. It parses
and validates the config, and generates two **self-contained** source files (`API.py`
routing + `main.py` entrypoint) that import FastAPI and the generated `DB.py`. The
**generator is standard-library only**; FastAPI is only needed to *run* the output.

The whole thing is callable through one function:

```python
api_init(output_dir, api_spec) -> (bool, error_code | None)
```

`api_init` never raises; it returns `True` on success or `(False, code)` on failure and logs
the detail — exactly like `db_init` on the database side.

---

## 1. Importing

```python
from api import (
    parse_api,
    statement_kind, extract_params, split_statements,
    ApiSpec, Endpoint, Operation, Param, Response, METHODS,
    render_api_module, render_main_module,
    generate, api_init,
    API_INIT_CONFIG, API_INIT_FILE, API_INIT_UNKNOWN,
    ConfigError, ValidationError, normalize_type,   # re-exported from database
)
```

As with the DB layer, `from api ...` works when the repo root is on `sys.path`.

---

## 2. Data model

`parse_api()` returns an `ApiSpec`:

| Class | Fields |
|---|---|
| `ApiSpec` | `base_path="/"`, `endpoints: dict`, `cors=None` (reserved, accepted+ignored) |
| `Endpoint` | `path`, `operations: dict[method, Operation]` |
| `Operation` | `method`, `sql`, `params: dict`, `response: Response`, `multi=False`, `description=None`, `authentication=None`, `rate_limit=None` (last two reserved) |
| `Param` | `name`, `source` ∈ {path,query,body,header}, `fin_type=None`, `required=False`, `default=None` |
| `Response` | `shape` ∈ {list,single,none}, `key=None`, `columns=None`, `renames=None`, `paginate=False`, `blob_format=None` |

`statement_kind(sql)` classifies SELECT/INSERT/UPDATE/DELETE (leading `WITH`→SELECT,
`REPLACE`→INSERT; anything else → `OTHER`). `extract_params(sql)` lists `:name`/`@name`/`$name`
placeholders (skipping strings/comments; a bare `?` raises). `split_statements(sql)` splits on
`;` outside strings/comments.

---

## 3. Parsing

```python
try:
    api_spec = parse_api(config["api"])
except ConfigError as err:
    print(err.path, err)   # e.g. api.endpoints./users.get.sql
```

`parse_api` is strict: unknown keys and methods rejected (`get/post/put/patch/delete` only;
`head`/`options` are skipped — FastAPI derives them), DDL rejected, `;`-multi requires
`multi: true`, `paginate` requires `shape=list`, and every `:name` resolves to a source:
path `{seg}` → `path`; declared `params` source; otherwise `query` (GET/DELETE) or `body`
(POST/PUT/PATCH). Reserved generated names (`page`, `pageSize`, `payload`, `params`, `result`,
`_value`) are rejected.

---

## 4. Codegen

```python
api_py  = render_api_module(api_spec)   # -> API.py source (str)
main_py = render_main_module(api_spec)  # -> main.py source (str)
```

`API.py` imports `from DB import query, coerce, map_sqlite_error, connect, serialize_rows, ...`
so it must sit next to the generated `DB.py`. `main.py` builds `app`, mounts the router under
`basePath` (omitting the `prefix` argument when `basePath` is `/`), registers a
`RequestValidationError → 400` handler, and exposes `GET /healthz`.
If `uvicorn build.main:app` can't resolve `API`/`DB`, run `cd build && uvicorn main:app`
(README §8.1).

---

## 5. Build emission + the `api_init` entry

```python
written = generate("build", api_spec)     # -> [<abs path to API.py>, <abs path to main.py>]
ok, code = api_init("build", api_spec)    # -> (True, None) or (False, "FILE_ERROR"/"CONFIG_ERROR"/"UNKNOWN")
```

`generate` `mkdir -p`s the output dir and writes both files UTF-8. `api_init` wraps it, never
raises, and logs via the `api` logger. Error codes mirror `db_init`:
`API_INIT_CONFIG="CONFIG_ERROR"`, `API_INIT_FILE="FILE_ERROR"`, `API_INIT_UNKNOWN="UNKNOWN"`.

---

## 6. Full Finn-in-one-place flow (for the CLI team)

```python
from database import parse_database, bootstrap, render_db_module
from api import parse_api, api_init

db    = parse_database(config["database"])   # validate schema
bootstrap("build/finn.db", db)               # create/verify SQLite (+ init once)
open("build/DB.py", "w").write(render_db_module(db))

api   = parse_api(config["api"])             # validate routes
ok, code = api_init("build", api)            # write build/API.py + build/main.py
if not ok:
    raise SystemExit(f"api_init failed: {code}")
```

---

## 7. Behavior contract (what the generated app does)

- `SELECT` → list (`shape=list`), first row / 404 (`single`, `key` wraps), `204` (`none`).
- `POST`/`PUT` whose SQL is a `SELECT` returning zero rows → `404 NOT_FOUND`.
- `INSERT` → `{"affected", "rowid"}`; `UPDATE`/`DELETE` → `{"affected"}` or `404` when 0.
- `paginate` → `{items, page, pageSize, total, totalPages}` (count + `LIMIT/OFFSET` wrapper).
- Errors use `{"error": {"code","message","detail"}}`; validation (incl. bad `page`) → `400
  VALIDATION_ERROR`, sqlite `IntegrityError` → `409 CONFLICT`, else `500 INTERNAL_ERROR`.
- `multi: true` runs all statements on one connection in a transaction (all-or-nothing).

Signature/docstring and error-code details live in the source; the tests in
`tests/test_api.py` pin the contract.
