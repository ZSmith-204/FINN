# FINN — Relational Todo · Live Demo Presenter Notes

> One `finn.json` → schema + API + frontend. Write the backend live, prove
> relational data with curl, and connect it to the UI — all with zero handler code.

## 1. The 30-second pitch

FINN turns **one declarative JSON file** into a working FastAPI + SQLite backend.
Every endpoint is **one line of SQL** — FINN owns parameter binding, validation,
connection handling, and error mapping. Relationships are **first-class**: a
column declares `references`, FINN emits the foreign key, enforces it, and
returns SQL `JOIN`s straight to JSON. And FINN **serves your frontend**, so the
whole demo — UI and data — runs same-origin with no CORS fiddling.

## 2. Setup — establish your working shell (once)

- `cd /home/zsmith204/FINN` — stay at the repo root for the whole demo.
- Define the command alias so on-stage typing reads cleanly:
  ```
  alias finn='.venv/bin/python -m cli'
  ```
  If your shell restarts mid-demo, re-run that line. In doubt, the full form is
  always `.venv/bin/python -m cli`.
- You need **two terminals**: Terminal A runs the server (`finn run examples/todo/finn.json --port 8000`); Terminal B runs `curl`.
- Warm caches the day before: run one throwaway `finn build examples/todo/finn.json`,
  then `rm -rf examples/todo/build` so the on-stage build is genuinely live.
- Confirm port `8000` is free (`curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/healthz`
  should NOT return 200). If something holds it, free it with `fuser -k 8000/tcp`.
  Do NOT silently switch ports — every URL below is hard-coded to `:8000`.
- Large terminal font; browser tab pre-opened to `http://127.0.0.1:8000/`.

## 3. Act 1 — Declarative startup (one table, no relations)

Open `examples/todo/finn.json` in your editor (the committed file is your answer
key) and type this config live — start from an empty editor for the "write it"
effect. This is a strict subset; it builds on its own:

```json
{
  "version": 1,
  "database": {
    "file": "finn.db",
    "tables": {
      "todos": {
        "columns": {
          "id":    { "type": "integer", "primaryKey": true, "autoincrement": true },
          "title": { "type": "text" },
          "done":  { "type": "boolean", "default": "false" }
        }
      }
    }
  },
  "api": {
    "basePath": "/api",
    "endpoints": {
      "/todos": {
        "get":  "SELECT id, title, done FROM todos ORDER BY id",
        "post": "INSERT INTO todos (title) VALUES (:title)"
      },
      "/todos/{id}": {
        "patch":  "UPDATE todos SET done = :done WHERE id = :id",
        "delete": "DELETE FROM todos WHERE id = :id"
      }
    }
  }
}
```

Terminal A (leave the server running):

```
rm -rf examples/todo/build                        # clean start → fresh database
finn build examples/todo/finn.json                # "no handler code — each endpoint is one SQL statement"
finn run examples/todo/finn.json --port 8000      # "deterministic startup: fully validated before it serves"
```

Terminal B:

```
curl -s -X POST -H 'Content-Type: application/json' -d '{"title":"Ship demo"}' http://127.0.0.1:8000/api/todos
curl -s http://127.0.0.1:8000/api/todos
```

Beat note: FINN owns the plumbing — parameter binding, coercion, connection
management, and error mapping. You write SQL, not handlers.

## 4. Act 2 — Add relational data

Edit the config (still `examples/todo/finn.json`):

1. Add a `lists` table (id PK autoincrement, title, created_at).
2. Add `"list_id": { "type": "integer", "references": "lists.id", "onDelete": "CASCADE" }`
   to `todos`, plus an index on it.
3. Add endpoints `/lists`, `/lists/{id}`, `/lists/{id}/todos`, and `/board` (a JOIN).
4. Add `"static": { "/": { "file": "index.html" } }` (frontend comes later, Act 3).

**Critical beat:** the schema changed, so rebuild from a clean database:

```
(Ctrl-C in Terminal A)
rm -rf examples/todo/build
finn build examples/todo/finn.json
finn run examples/todo/finn.json --port 8000
```

> FINN never destructively alters an existing database (README §10.3). It
> refuses to guess at a migration and silently change your data. For a demo we
> rebuild clean; in production you'd point at a new DB file or script the change.

## 5. Act 3 — Connect the frontend

```
rm -rf examples/todo/build && finn build examples/todo/finn.json && finn run examples/todo/finn.json --port 8000
# open http://127.0.0.1:8000/
```

FINN serves `index.html` at `/` while the API lives under `/api` — same origin,
no CORS. Live walk: add a list → add a todo → toggle done (PATCH) → delete the
list and watch its todos vanish (the cascade, now visible in the UI) → click
**SHOW JOIN VIEW** to reveal the raw `/api/board` JOIN output.

## 6. Full curl script (verified; fresh-database output)

These bodies assume a **fresh database**. Start clean, then serve:

```
rm -rf examples/todo/build && finn build examples/todo/finn.json
finn run examples/todo/finn.json --port 8000    # Terminal A; leave running
```

Then, in Terminal B:

```bash
# health
curl -s http://127.0.0.1:8000/healthz
# {"status":"ok"}

# create two lists
curl -s -X POST -H 'Content-Type: application/json' -d '{"title":"Groceries"}' http://127.0.0.1:8000/api/lists
# {"affected":1,"rowid":1}
curl -s -X POST -H 'Content-Type: application/json' -d '{"title":"Work"}' http://127.0.0.1:8000/api/lists
# {"affected":1,"rowid":2}

# add a todo (child of list 1)
curl -s -X POST -H 'Content-Type: application/json' -d '{"list_id":1,"title":"Milk"}' http://127.0.0.1:8000/api/todos
# {"affected":1,"rowid":1}

# FK enforcement: nonexistent parent -> 409 CONFLICT
curl -s -X POST -H 'Content-Type: application/json' -d '{"list_id":999,"title":"Orphan"}' http://127.0.0.1:8000/api/todos
# 409 {"error":{"code":"CONFLICT","message":"A foreign key constraint was violated.","detail":"FOREIGN KEY constraint failed"}}

# the JOIN resolves the parent name
curl -s http://127.0.0.1:8000/api/board
# [{"id":1,"title":"Milk","done":false,"list_id":1,"list_title":"Groceries"}]

curl -s http://127.0.0.1:8000/api/lists/1/todos
# [{"id":1,"title":"Milk","done":false}]

# toggle done
curl -s -X PATCH -H 'Content-Type: application/json' -d '{"done":true}' http://127.0.0.1:8000/api/todos/1
# {"affected":1}

# delete the parent; the child cascades
curl -s -X DELETE http://127.0.0.1:8000/api/lists/1
# {"affected":1}
curl -s http://127.0.0.1:8000/api/board
# []

# deleting a missing row -> 404
curl -s -X DELETE http://127.0.0.1:8000/api/lists/999
# 404 {"error":{"code":"NOT_FOUND","message":"No matching record was found.","detail":null}}
```

## 7. "What this line proves" — talking-point map

| Construct | Relational concept it demonstrates |
|---|---|
| `"references": "lists.id"` | Emits a SQLite `FOREIGN KEY`; `PRAGMA foreign_keys=ON` per connection |
| `"onDelete": "CASCADE"` | Integrity enforced in the DB, not in app code |
| `/board` SQL | A real SQL `JOIN` returned straight to JSON |
| `"indexes": [{ "columns": ["list_id"] }]` | Query planning / lookup acceleration |
| 409 CONFLICT | FINN maps SQLite constraint errors to proper HTTP status |

## 8. Q&A cheat sheet

- **SQL injection?** Named-parameter binding only (`:name`), never string concatenation; SQL is curated config, client input is always bound.
- **Why SQLite, not Postgres?** Single file, portable, zero ops. FINN's model is engine-portable in principle.
- **Migrations?** FINN is explicit and non-destructive: rebuild, or point `file` at a new DB. No silent destructive migration.
- **Many-to-many?** Yes — a join table:
  ```json
  "todo_tags": {
    "columns": {
      "todo_id": { "type": "integer", "references": "todos.id", "onDelete": "CASCADE" },
      "tag_id":  { "type": "integer", "references": "tags.id",  "onDelete": "CASCADE" }
    }
  }
  ```
- **Scaling?** SQLite serializes writes; documented limitation. Engine swap is a roadmap item.
- **Auth?** Reserved vocabulary (`authentication`, scopes) validated but out of scope for v1.
- **Hand-edit the generated files?** No — `main.py`/`API.py`/`DB.py` are FINN-owned and regenerated. You edit `finn.json`.

## 9. Fallback plan (short on time / typo anxiety)

Type the **final** config straight from `examples/todo/finn.json` in one go,
narrate the relations inline (skip the two-stage split), then jump to §6. Every
command and body above is pre-verified. Remember the alias:
`alias finn='.venv/bin/python -m cli'` (or the full `.venv/bin/python -m cli`).

## 10. Deliverable map

| File | What it is |
|---|---|
| `examples/todo/finn.json` | Final config: lists + todos, FK, JOIN, static frontend |
| `examples/todo/index.html` | Self-contained todo board served at `/` |
| `examples/todo/presenter-notes.md` | This script |
