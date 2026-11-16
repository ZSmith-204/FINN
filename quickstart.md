# FINN Quickstart

Get from an empty directory to a running API in minutes, using either the
**command line** (`finn`) or the **desktop GUI** (FINN Studio).

FINN reads a single JSON config (`finn.json`) that describes a database schema
and an API, then generates a self-contained FastAPI + SQLite backend in a
`build/` folder. You never write handler code — you write SQL.

---

## 1. Prerequisites

- Python 3.9+ (the repo includes a `.venv` on 3.13).
- Install dependencies once:

  ```bash
  pip install -r requirements.txt
  ```

  This installs `fastapi` and `uvicorn[standard]` (required to *serve* an app)
  and `PyQt6` (required only for the GUI). `httpx` is dev-only.

The CLI itself is stdlib-only for `init`, `build`, and `validate`; only
`finn run` and the GUI's Run panel need `fastapi`/`uvicorn`.

---

## 2. The CLI

### 2.1 Invoking it

Pick whichever fits your setup (all are equivalent):

```bash
python3 cli.py <command> ...     # from the repo root
python3 -m cli <command> ...     # same, as a module
finn <command> ...               # if you built the PyInstaller binary (dist/finn)
```

Check it works:

```bash
finn --version            # finn 0.1.0
finn --version --json     # {"version": "0.1.0", "configSchema": 1}
finn --help
finn help build
```

### 2.2 The four commands

| Command | What it does |
|---|---|
| `finn init` | Scaffold a new `finn.json` |
| `finn validate` | Check the config; writes nothing |
| `finn build` | Validate + emit the `build/` folder |
| `finn run` | Build if stale, then serve the API |

### 2.3 Create and inspect a project

```bash
mkdir my-api && cd my-api
finn init                  # writes ./finn.json (example template)
```

Templates: `example` (full schemas), `minimal` (one table), `blank` (empty).

```bash
finn init . --template minimal --name "my app"
finn validate              # no errors? good to go
```

`validate` prints diagnostics (each with a stable `FINN-Exxx` code and a JSON
path like `database.tables.users.columns.id`) and exits `0` only when clean.

### 2.4 Build

```bash
finn build
```

Output lands in `build/`:

```
build/
├── main.py          # FastAPI entrypoint (uvicorn build.main:app)
├── API.py           # generated routes
├── DB.py            # generated schema + data layer
├── finn.db          # initialized SQLite database
└── .finn-build.json # build manifest (staleness check)
```

Generated files are FINN-owned and overwritten on every build. A build that
fails validation writes **nothing** (no partial output).

### 2.5 Run

```bash
finn run                # http://127.0.0.1:8000
finn run --port 9000
finn run --no-build     # serve existing build as-is
```

The banner shows the useful URLs:

```
FINN dev server
  App:     http://127.0.0.1:8000
  Docs:    http://127.0.0.1:8000/docs
  Redoc:   http://127.0.0.1:8000/redoc
  Health:  http://127.0.0.1:8000/healthz
```

- `/docs` and `/redoc` — auto-generated OpenAPI UI.
- `/healthz` — health check (`200`).
- Ctrl+C stops the server (exit code `0`).

There is **no hot reload** in v1: edit `finn.json`, stop, then `finn run` again
(it rebuilds automatically when the config changed).

### 2.6 Config discovery and flags

Every file-taking command resolves the config in this order:

1. `--config PATH`
2. `$FINN_CONFIG`
3. `./finn.json`

Useful flags:

```bash
finn build --json --quiet     # machine-readable output, no summary
finn run --json               # NDJSON events (building, listening, stopped, error)
finn init --force             # overwrite an existing finn.json
```

### 2.7 Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Validation error (a diagnostic with `severity: error`) |
| `2` | I/O / runtime failure (bad path, port in use, schema conflict) |
| `3` | Usage error (unknown command/flag) |

---

## 3. The GUI — FINN Studio

> **Note:** `docs/gui-spec.md` describes a browser-hosted app. That is out of
> date — the current implementation is a native **PyQt6 desktop application**,
> `finn_studio.py`. The screens and safety rules are the same; the host is not.

### 3.1 Launching it

```bash
python3 finn_studio.py              # start on an empty state
python3 finn_studio.py proj-test    # open a project directory directly
```

Requires `PyQt6` (from `requirements.txt`), plus a reachable `finn` for the
engine features (see §3.2). A GUI needs a display; run it on your desktop, not
a headless box.

### 3.2 How it works

FINN Studio is a thin wrapper — the CLI is the single source of truth. The GUI
**never** re-implements validation, build, or serving; it drives `finn` as a
subprocess (using `--json`) and edits the `finn.json` in memory. At startup it
runs a handshake (`finn --version --json`).

- If the CLI is missing or too old, Studio enters **degraded mode**: you can
  still open, edit, and save a config, but init/build/run/validate are disabled
  and a banner explains why.

### 3.3 The window

- **Toolbar:** Open…, New project…, Save (`Ctrl+S`), Validate now, Build, Run.
- **Tabs:** Editor · Diagnostics · Build · Run.

#### Editor

- Left: a collapsible **tree** of the config (database tables, columns, API
  endpoints, methods, params, response…). Double-click a value to edit it;
  right-click for Add / Edit / Rename / Delete. Renames cascade to foreign-key
  `references` and index columns.
- Right: the **raw JSON** (a canonical preview). Click **Apply raw JSON** to
  parse your pasted text back into the tree.
- Editing triggers a debounced `finn validate`; results appear in the
  Diagnostics tab.

#### Diagnostics

Shows severity, code, path, and message for every problem the CLI reports.
Any `severity: error` disables Build and Run; warnings never block.

#### Build

- **Build** runs `finn build --json` and lists the artifacts (name, size,
  created/updated), the database path, and duration.
- **Open build folder** and **Run ▶** shortcuts.

#### Run

- Choose a port, then **Start** (runs `finn run --json`).
- While listening, the buttons **Open app**, **Open /docs**, **Open /redoc**,
  and **Open /healthz** become active, and a health chip polls `/healthz`.
- **Stop** sends SIGTERM (SIGKILL after a 5 s grace).

### 3.4 Project lifecycle

1. **New project…** → a wizard asks for a template (`example`/`minimal`/`blank`),
   an optional project name (`meta.name`), and a directory. It runs
   `finn init --json` and opens the result.
2. **Edit** the schema and API in the trees (or paste raw JSON).
3. **Save** (`Ctrl+S`) — write to `finn.json` (atomic, canonical formatting).
4. **Validate / Build / Run** — the buttons gate on a saved, error-free config.

### 3.5 Safety rules worth knowing

- **Save gate** — Build and Run are disabled while there are unsaved edits.
- **Version gate** — a config whose `version` is not `1` opens read-only.
- **No destructive reset** — a database/schema conflict is shown as a
  diagnostic; a human must resolve it. The GUI never deletes `build/` or the DB.
- **Engine mediation** — all validation/build/run go through the CLI, so GUI
  behavior matches the CLI exactly.
