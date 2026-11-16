![FINN -- FINN Is Not NGINX](images/logo.png)

# FINN

**FINN Is Not NGINX.** FINN reads **one** JSON config file and generates a
complete, self-contained **FastAPI + SQLite** backend. You never write handler
code — you describe a schema and endpoints with SQL, and FINN produces a
runnable `/build` directory: generated Python plus an initialized database.

> **In a hurry?** [`quickstart.md`](quickstart.md) is the fast-read version of
> this page. Use it to go from zero to a running API in a couple of minutes,
> then come back here for full installation options and deeper usage.

---

## Contents

- [What FINN does](#what-finn-does)
- [Installation](#installation)
- [Using the CLI](#using-the-cli)
- [Using the GUI (FINN Studio)](#using-the-gui-finn-studio)
- [Writing configs](#writing-configs)
- [Documentation](#documentation)

---

## What FINN does

FINN has one input and one output. The input is a single file, `finn.json`,
which describes two things:

1. **`database`** — your schema: tables, columns, types, keys, indexes, and
   relations.
2. **`api`** — your surface: endpoints, where each HTTP method maps to a SQL
   statement that FINN executes and returns as JSON.

From that one file, `finn build` emits a self-contained `/build` folder:

```
build/
├── main.py   # FastAPI entrypoint
├── API.py    # generated routes
├── DB.py     # generated schema + data helpers
├── _static/  # static files (only when api.static is configured)
└── finn.db   # initialized SQLite database
```

The build is runnable on its own — `uvicorn build.main:app` — with no FINN
runtime required to *serve* it. FINN also ships a CLI (`finn`) and a desktop GUI
(FINN Studio) that wrap the whole workflow: init → validate → build → run.

FINN is opinionated and declarative: **you write SQL, not handler code.** FINN
owns the plumbing — parameter binding, connection management, validation, and
error mapping.

---

## Installation

### Prerequisites

- **Python 3.9+** (FINN is developed and tested against 3.13).
- **`pip`** to install dependencies.

There are three ways to run FINN. Pick one.

### Option A — prebuilt binaries (recommended)

FINN ships PyInstaller single-file executables. Build and install them with
`make` (requires the build venv; see the steps in *Option C*):

```bash
make install         # build + install both the CLI (`finn`) and GUI (`finn-studio`)
make install-cli     # build + install only the CLI
make install-studio  # build + install only the GUI
```

Binaries land in `~/.local/bin` (override with `BINDIR=/some/path make install`).
`make ensure-path` adds `~/.local/bin` to your `PATH` in `~/.bashrc` if it is not
already there. The frozen GUI shells out to the `finn` CLI on `PATH`, so install
the CLI too if you want build/run from the GUI.

Verify:

```bash
finn --version          # finn 0.1.0
finn-studio             # opens the desktop GUI
```

### Option B — run from source

Clone the repository, then install the runtime dependencies once:

```bash
git clone <repo-url> finn && cd finn
pip install -r requirements.txt
```

`requirements.txt` installs `fastapi`, `uvicorn[standard]`, `PyQt6` (the GUI),
and `httpx` (dev/test only).

Run the CLI straight from the repo root (the CLI is stdlib-only for
`init`/`build`/`validate`; only `run` needs `fastapi`/`uvicorn`):

```bash
python3 finn_cli.py --version      # finn 0.1.0
python3 -m finn_cli --version      # same, as a module
python3 finn_cli.py build
```

Run the GUI (needs a display and `PyQt6`):

```bash
python3 finn_studio.py
```

### Option C — build the binaries yourself

Create a virtual environment and install PyInstaller, then build:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-build.txt
make build           # -> dist/finn and dist/finn-studio
make install         # -> ~/.local/bin/finn and ~/.local/bin/finn-studio
```

Note: PyInstaller does **not** cross-compile — build `finn`/`finn-studio` on
Linux and on Windows separately. The `finn` binary bundles `fastapi`/`uvicorn`
so `finn run` works with no third-party installs; `finn-studio` needs the `finn`
CLI on `PATH`.

---

## Using the CLI

FINN has four commands:

| Command | What it does | Writes anything? |
|---|---|---|
| `finn init [DIR]` | Scaffold a new `finn.json` | yes (the config) |
| `finn validate [CONFIG]` | Check the config, print diagnostics | no |
| `finn build [CONFIG]` | Validate then emit the `build/` folder | yes |
| `finn run [CONFIG]` | Build if stale, then serve the API | yes |

### A full session, end to end

```bash
mkdir my-api && cd my-api

finn init                    # writes ./finn.json (the "example" template)
#   Templates: example (full), minimal (one table), blank (empty)
#   finn init . --template minimal --name "my app"   # target dir + name + template

finn validate                # prints any diagnostics; exits 0 only when clean
finn build                   # emits the build/ folder
finn run                     # serves at http://127.0.0.1:8000
```

While `finn run` is up:

- **App** — `http://127.0.0.1:8000` (or your `--port`).
- **Docs** — `/docs` and `/redoc` (auto-generated OpenAPI UI).
- **Health** — `/healthz` (returns `200` when the DB is reachable).

Press `Ctrl+C` to stop (exits `0`). There is **no hot reload** in v1: edit
`finn.json`, stop, and `finn run` again (it auto-rebuilds when the config
changed).

### Config discovery

Every file-taking command resolves the config in this order:

1. `--config PATH`
2. `$FINN_CONFIG`
3. `./finn.json`

### Useful flags

```bash
finn build --out dist          # emit to a different directory
finn build --json --quiet      # machine-readable output, no summary
finn run  --port 9000          # a different port
finn run  --no-build           # serve the existing build as-is
finn run  --json               # NDJSON events on stdout (building, listening, …)
finn init --force              # overwrite an existing finn.json
finn init --json               # machine-readable init output
```

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Validation error (a diagnostic with `severity: error`) |
| `2` | I/O / runtime failure (bad path, port in use, schema conflict, init target exists) |
| `3` | Usage error (unknown command/flag) |
| `130` | Interrupted (SIGINT/SIGTERM mid-command) |

Diagnostics are printed to stderr with a stable `FINN-Exxx` code and a JSON path
(like `database.tables.orders.columns.user_id`) pointing at the offending
element.

---

## Using the GUI (FINN Studio)

FINN Studio is a native **PyQt6** desktop app that wraps the CLI. The CLI is the
single source of truth: Studio never re-implements validation, build, DDL, or
serving — it drives `finn` as a subprocess and edits `finn.json` in memory.

```bash
python3 finn_studio.py              # open on an empty state
python3 finn_studio.py my-api       # open a project directory directly
```

What you get:

- **Project wizard** — new project from a template (`example`/`minimal`/`blank`).
- **Editor** — a collapsible tree (tables, columns, endpoints, params,
  response…) plus a raw-JSON view; edits trigger a debounced `finn validate`.
- **Diagnostics** — every problem the CLI reports, with code/path/message; any
  error disables Build and Run.
- **Build panel** — run `finn build` and list the artifacts.
- **Run panel** — pick a port, start/stop the server, open the app, `/docs`,
  `/redoc`, or `/healthz`.

Studio needs `PyQt6` (from `requirements.txt`) and a reachable `finn` CLI. If the
CLI is missing or too old it enters **degraded mode**: you can still open, edit,
and save a config, but init/build/run/validate are disabled with a banner that
explains why. Configs whose `version` is not `1` open read-only.

Safety rules worth knowing: Build/Run are gated on a saved, error-free config;
a database/schema conflict is shown as a diagnostic (a human must resolve it —
Studio never deletes the build or the database); saves are atomic and use
canonical formatting.

---

## Writing configs

A config is a single JSON object with four top-level keys:

```json
{
  "version": 1,
  "meta": {},
  "database": { "file": "finn.db", "tables": { "…": { } } },
  "api": { "basePath": "/api", "endpoints": { "…": { } } }
}
```

The `database` block defines tables (typed columns, primary keys, foreign keys,
indexes, views, seed `init` SQL). The `api` block defines endpoints, where each
HTTP method (`get`/`post`/`put`/`patch`/`delete`) maps to a SQL statement using
named `:parameters` bound from the path, query string, request body, or headers.

For the complete, authoritative format — every field, the canonical type set,
parameter-binding rules, response shaping, pagination, the error contract, and a
full worked example — see [`docs/reference.md`](docs/reference.md).

---

## Documentation

| Document | Audience | Location |
|---|---|---|
| **README** (this page) | Users — install + usage | root |
| **[`quickstart.md`](quickstart.md)** | Users — the fast read | root |
| **[`docs/reference.md`](docs/reference.md)** | Config format + runtime semantics (system reference) | `docs/` |
| **[`docs/cli-layer.md`](docs/cli-layer.md)** | CLI layer guide | `docs/` |
| **[`docs/gui-layer.md`](docs/gui-layer.md)** | GUI layer guide | `docs/` |
| **[`docs/database-layer.md`](docs/database-layer.md)** | `database` package integration guide | `docs/` |
| **[`docs/api-layer.md`](docs/api-layer.md)** | `api` package integration guide | `docs/` |
