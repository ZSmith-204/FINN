# FINN Studio GUI Layer — Integration Guide

**Audience:** integrators and maintainers of the GUI.
**Companion spec:** [`reference.md`](reference.md) (config format), [`cli-layer.md`](cli-layer.md) (engine contract).
**Sibling layers:** [`database-layer.md`](database-layer.md) (data, below), [`api-layer.md`](api-layer.md) (HTTP, below), [`cli-layer.md`](cli-layer.md) (CLI, below).

FINN Studio (`finn_studio.py`) is a local **PyQt6** desktop application. It is a
**thin wrapper over the CLI layer**: it never re-implements validation, build,
DDL, or serving. It drives `finn` as a subprocess (`QProcess`, argv arrays,
never a shell) and edits the single JSON config in memory, writing it back
canonically.

---

## 1. Running it

```bash
python finn_studio.py [PROJECT_DIR]   # from source (needs PyQt6 + a display)
finn-studio [PROJECT_DIR]             # the frozen binary (windowed one-file)
```

If `PROJECT_DIR` is omitted the app opens on an empty state; otherwise it opens
that directory's `finn.json` directly.

## 2. Public surface

- `main(argv=None) -> int` — the entry point; `finn_studio.py` is also runnable
  as a module/script.
- `finn-studio` — the frozen executable (`finn-studio.spec`; `console=False`).
- `MIN_CLI_VERSION` — the minimum `finn` version the GUI accepts (`0.1.0`).

CLI resolution (`default_cli_command()`):

1. `finn` on `PATH` (preferred).
2. Frozen GUI without `finn` on `PATH` → returns `["finn"]` so `QProcess`
   fails cleanly into degraded mode (never re-spawns its own binary as Python).
3. Source GUI → drives the current interpreter against `finn_cli.py` in the
   repo root.

For `run`, `run_command()` prefers an interpreter that can `import fastapi,
uvicorn` (the repo `.venv/bin/python` if it serves, else the current
interpreter), so serving works even though the CLI itself is stdlib-only.

## 3. Dependencies

- `PyQt6` (from `requirements.txt`) — all UI.
- the `finn` CLI on `PATH` (or a source-mode interpreter) — all engine work.
- stdlib `urllib.request` — the `/healthz` poll.

## 4. Engine — CLI subprocess mapping (`CliEngine`)

| Studio action | CLI invocation | Consumed output |
|---|---|---|
| Handshake | `finn --version --json` | `{"version","configSchema"}` |
| New project | `finn init --json --template <t> [--name <n>] <dir>` | JSON result |
| Validate | `finn validate --json --config <path>` | Diagnostics |
| Build | `finn build --json --config <path>` | Artifacts, DB, diagnostics |
| Run | `finn run --json --config <path> --host 127.0.0.1 --port <port>` | NDJSON events |
| Stop | `QProcess.terminate()` (SIGTERM), then `kill()` after 5 s | Process exit |

`CliEngine` (a `QObject`) exposes signals: `handshakeDone`, `opFinished`,
`runEvent` (parsed NDJSON), `runLog` (stderr), `runExited`. It captures stdout
and stderr separately and always passes the resolved `--config` path (never
`FINN_CONFIG`).

**Handshake.** Runs `finn --version --json` once, at startup. Requires
`configSchema === 1` and a `version` ≥
`MIN_CLI_VERSION`; a missing CLI, unparseable output, wrong schema, or too-old
version enters **degraded mode** (editing/saving still work; init/build/run/
validate are disabled; a banner explains why).

**Validation probe.** The editor validates unsaved edits without touching the
user's `finn.json`: it writes the in-memory tree to a temporary
`.finn-studio-validate-<pid>.json` probe in the project directory, runs
`finn validate --json --config <probe>`, then deletes the probe afterwards.

## 5. Features

- **Toolbar:** Open…, New project…, | Save (`Ctrl+S`), Validate now, Build,
  Run.
- **Editor** (tree + raw JSON): a collapsible 3-column tree (`Key`, `Type`,
  `Value`) next to a raw-JSON view; "Apply raw JSON" parses pasted text back
  into the tree. Edits trigger a debounced (≈400 ms) `finn validate`.
- **Diagnostics:** severity/code/path/message for every problem the CLI
  reports; any `severity: error` disables Build and Run.
- **Build:** runs `finn build --json`, lists artifacts, and offers "Open build
  folder" and "Run ▶".
- **Run:** port field (default 8000), Start/Stop; while listening, "Open app",
  "Open /docs", "Open /redoc", and "Open /healthz" become active and a health
  chip polls `/healthz` every 2 s.
- **New-project dialog:** template (`example`/`minimal`/`blank`), project name
  (`meta.name`), and target directory.

## 6. Canonical serialization & atomic save

Save (`canonical_json()`) writes UTF-8, LF line endings, 2-space indentation,
one trailing newline; top-level key order `version`, `meta`, `database`, `api`;
fixed per-shape key order for tables/columns/operations/responses; lower-case
method keys; type aliases normalized (`int`→`integer`, etc.); author ordering
preserved for name-keyed maps; and bare-string operations preserved as
shorthand while SQL-only.

Saves are atomic (`atomic_write_text()`: `mkstemp` → flush + `fsync` →
`os.replace`). The app records the file's SHA-256 and, before overwriting an
externally changed file, shows a "File changed on disk" confirmation rather
than silently overwriting.

## 7. Safety rules

- **Save gate** — Build/Run are disabled with unsaved edits.
- **Version gate** — a config whose `version` is not `1` opens read-only.
- **Degraded mode** — no engine claims made when the CLI is unavailable.
- **No destructive reset** — a schema conflict is a diagnostic; a human must
  resolve it; Studio never deletes `build/` or the database.
- **Engine mediation** — all validation/build/run go through the CLI, so GUI
  behavior matches the CLI exactly.
- **No shell, loopback only, no direct DB access.**

## 8. Gotchas / known limitations

- The `/healthz` poll runs synchronously on the UI thread (documented
  limitation; it only blocks while the server is unreachable).
- The editor is schema-aware JSON editing (a tree + raw JSON) with enum/
  boolean/integer pickers for known fields (types, FK actions, param sources,
  response shapes); SQL and cross-field correctness are enforced at validate
  time by the CLI.
- Source-mode CLI resolution expects `finn_cli.py` in the repo root.
- `api.cors` is a reserved, accepted-but-ignored key (reference §9/§12) with
  no dedicated control, and `/healthz` is fixed (reference §10.4).

## 9. End-to-end example

```bash
python finn_studio.py /tmp/demo    # open an existing project
# New project… → template: minimal → creates and opens finn.json
# Editor: add a table/column → Save (Ctrl+S) → Build → Run → open /healthz → Stop
```

The contract is pinned by `tests/test_studio.py` (stdlib `unittest`).
