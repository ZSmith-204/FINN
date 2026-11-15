# FINN Studio GUI Specification

**Status:** Draft v0.1
**Date:** 2026-11-15
**Companions:** [`README.md`](../README.md) (authoritative product spec), [`docs/cli-spec.md`](cli-spec.md) (engine contract — "CLI §n" references below), [`docs/database-layer.md`](database-layer.md) ("DB Layer Guide")

---

## 1. Scope

FINN Studio is a local graphical application for the FINN generator. It provides full
parity with the CLI's three core verbs and adds what a GUI is uniquely good at:
visual authoring of the configuration.

| CLI verb (CLI §6) | Studio equivalent |
|---|---|
| `finn init` (INIT) | New-project wizard (§6.2) |
| `finn build` (BUILD) | Build panel (§6.6) |
| `finn run` (RUN) | Run panel (§6.7) |
| — (GUI-only) | Schema editor (§6.3) and API editor (§6.4) covering the complete `database` (README §4) and `api` (README §5–§6) vocabularies |

The acceptance bar for authoring is the coverage matrix in §9: every config field
defined in README §4.1–§4.7 and §5.1–§5.4 plus §6.2 MUST be creatable and editable in
the GUI, so a user can build the API and DB schemas listed in the README without
hand-writing the config.

**Audience:** GUI implementers and reviewers. **Normative language:** MUST / SHOULD /
MAY (RFC 2119).

## 2. Launcher and host

### 2.1 Launcher

- `finn-studio [PROJECT_DIR]` starts the Studio **host process**, which serves the UI
  and the internal JSON API described below, binds `127.0.0.1` on a random free port,
  and opens the default browser at a URL carrying a one-time token (§2.3).
- Flags: `--port` (pin the host port instead of a random one), `--no-open` (print the
  URL to stdout instead of opening a browser).
- If `PROJECT_DIR` is omitted, the UI opens on the Home screen (§6.1).
- The host tech is non-normative: suggested shape is a Python host (FastAPI) serving a
  static frontend; frontend framework is non-normative (React + TypeScript suggested).
- A desktop wrapper (Electron/Tauri) is explicitly out of scope for v1 (§8, P2).

### 2.2 Host API surface

The host exposes a small internal API used only by its own frontend. Every endpoint
requires the session cookie (§2.3). The endpoint set is non-normative in name but the
behaviors it anchors are normative where stated in later sections:

| Internal endpoint | Anchors |
|---|---|
| `GET /api/state` | project path, dirty flag, CLI handshake result, diagnostics summary |
| `POST /api/save` | canonical serialization + atomic write (§4.2, §4.3) |
| `POST /api/engine` | dispatch `validate` \| `init` \| `build` to the CLI (§3); one engine call at a time |
| `POST /api/run/start`, `POST /api/run/stop` | run lifecycle (§6.7) |
| `POST /api/test-request` | loopback-only request proxy (§6.8) |
| `GET /api/events` | SSE/streaming channel for run events and console output |

The Studio host MAY additionally import `database` read-only for the DDL preview
(§6.3.8); it MUST NOT use that import as a second validator — validation is
CLI-mediated (§3).

### 2.3 Token and session

- Generate at least 256 bits of randomness per launch.
- The token is accepted once. The first browser load exchanges it for an `HttpOnly`,
  `SameSite=Strict` session cookie and invalidates the token; later use of the same
  URL fails with 401.
- Every internal API call requires the session cookie. The host validates `Host` and
  `Origin` against the loopback origin to reduce DNS-rebinding risk.
- The token MAY appear in shell history or browser history; Studio MUST NOT log it.
  `--no-open` prints the URL to stdout so the user can open it manually.

## 3. Engine: CLI subprocess

### 3.1 Command mapping

The CLI is the single source of behavioral truth. Studio MUST NOT implement a parallel
validator, builder, DDL emitter, or server.

| Studio action | CLI invocation | Consumed output |
|---|---|---|
| Handshake | `finn --version --json` | `{"version","configSchema"}` |
| New project | `finn init --json --template <t> [--name <n>] <dir>` | JSON result |
| Validate | `finn validate --json --config <path>` | Diagnostics |
| Build | `finn build --json --config <path>` | Artifacts, DB, diagnostics |
| Run | `finn run --json --config <path> --host 127.0.0.1 --port <port>` | NDJSON events |
| Stop | SIGTERM to the child; SIGKILL after 5 s | Process exit |

### 3.2 Handshake

- Resolve `finn` from `PATH`; v1 does not bundle a CLI.
- Require valid JSON with `configSchema === 1` and a `version` at least the minimum
  declared by the Studio release.
- A missing CLI, unparseable handshake, wrong schema, or too-old version puts Studio
  in degraded mode.
- The handshake runs at startup and before Build/Run if the CLI path changed.

### 3.3 Degraded mode

When the CLI is unavailable or incompatible:

- The editor remains fully usable for viewing, editing, and saving the config.
- `init`, Build, Run, and validation controls are disabled.
- A persistent banner explains the cause and the required CLI version.
- The diagnostics panel shows "engine unavailable" rather than stale or fabricated
  diagnostics.
- Studio MUST NOT claim a config is valid when it cannot run the CLI.

### 3.4 Process rules

- Spawn with argv arrays; never a shell.
- `cwd` is the project directory.
- Capture stdout and stderr separately. Parse stdout as JSON/NDJSON; stream stderr to
  the console/log view.
- Do not pass `FINN_CONFIG`; always pass the resolved `--config` path.
- Do not pass `FINN_DB_PATH` unless the user explicitly configured an override.
- On nonzero exit, surface the CLI diagnostics; do not rewrite their codes, paths, or
  messages.
- Kill the run child when Studio exits, after attempting graceful SIGTERM.

## 4. Source of truth and file safety

### 4.1 Model

- The single config file on disk is the source of truth (README §1.1).
- Studio edits an in-memory JSON tree and tracks a dirty flag.
- On open, Studio resolves the config path once (same precedence as CLI §3) and passes
  that explicit path to every CLI invocation.
- Build and Run require a saved file. When dirty, Build and Run are disabled with a
  "Save first" tooltip; Studio MAY also offer "Save and build".
- Studio never edits `build/` or the database directly. Only `finn build` does that.

### 4.2 Canonical serialization

Save MUST produce:

- UTF-8, LF line endings, 2-space indentation, one trailing newline.
- Top-level key order: `version`, `meta`, `database`, `api`.
- Fixed-shape object key order: `database` = `file`, `tables`, `indexes`, `views`,
  `init`; table = `columns`, `primaryKey`, `indexes`, `withoutRowid`; column =
  `type`, `nullable`, `default`, `primaryKey`, `autoincrement`, `unique`, `check`,
  `references`, `onDelete`, `onUpdate`, `description`; operation = `sql`, `params`,
  `response`, `description`, `authentication`, `rateLimit`; response = `shape`,
  `key`, `columns`, `renames`, `paginate`.
- Author-declared ordering preserved for name-keyed maps: tables, columns, endpoints,
  and methods. New methods append in README §5.1 order (`get`, `post`, `put`, `patch`,
  `delete`).
- Method keys lower-case.
- Type aliases normalized to canonical names (`int` → `integer`, `float`/`double` →
  `real`, `string`/`varchar` → `text`, `bytes` → `blob`). The file changes spelling on
  save; the UI shows a "normalized from …" hint.
- Optional keys that Studio did not touch keep their authored presence or absence.
  When a control is set back to its documented default, Studio omits the key. New
  elements contain only required fields plus non-default values.
- A bare-string operation (`"get": "SELECT …"`) is preserved as shorthand while it has
  only `sql` and is unmodified. Adding any other operation field expands it to the
  full object.

### 4.3 Atomic writes

- Write to a temporary file in the same directory, flush, then rename over the target.
  No partial writes.
- Never write outside the project directory for config content.
- Studio MUST NOT delete user files.

### 4.4 Save gate

- Build, Run, and Restart require a clean tree.
- Attempting a keyboard shortcut for Build while dirty prompts Save, Save and build,
  or Cancel.
- A failed save leaves the old file intact and the tree dirty.

### 4.5 External changes (P1)

- Watch the config's mtime and SHA-256.
- On change, show "File changed on disk" with Reload, Diff, and Overwrite.
- Reload discards in-memory edits after confirmation.
- Overwrite writes the in-memory tree after confirmation.
- Studio MUST NOT silently overwrite an externally changed file.

### 4.6 Crash draft (P1)

- Autosave a recovery draft to `<project>/.finn-studio-draft.json`.
- On next open, offer Restore or Discard.
- The draft is Studio-owned, is never passed to the CLI, and is excluded from
  external-change detection.
- Remove the draft after a successful save or explicit discard.

### 4.7 Destructive edits

Deleting a table, column, endpoint, method, index, view, or `init` statement requires
confirmation naming the element. Deleting the last column of a table is blocked
because README §4.1 requires at least one column.

## 5. Configuration compatibility

### 5.1 Version gate

If `version` is missing or not equal to `1`, open the project in a read-only viewer
with a banner explaining the supported version. Saving, Build, and Run are disabled.
Studio MUST NOT attempt to downgrade or rewrite the file.

### 5.2 Unknown keys and `meta.extraKeys`

- Without `meta.extraKeys: "ignore"`, unknown keys are a CLI structural error; Studio
  shows the diagnostic and offers no "ignore" shortcut.
- With `meta.extraKeys: "ignore"`, Studio renders unknown top-level keys in an
  "Advanced" tree (P1) and preserves them verbatim on save.
- Changing `extraKeys` back to reject while unknown keys exist blocks save with an
  explanation.
- Unknown nested keys remain structural errors; `extraKeys` does not make them
  editable.

### 5.3 Reserved keys

`authentication` and `rateLimit` are displayed read-only with a "reserved for v1+"
tag (README §9, §13). Studio MUST NOT imply they take effect.

## 6. Screens

### 6.1 Home

Purpose: open or create a project. Controls: recent projects (name, path, last
opened), Open folder, New project. Recent entries are local host state, not project
files. States: empty (explanation + both actions), loading (skeleton list), error
(unreadable entry shows inline error and a remove action that does not delete files).
If the CLI is unavailable, New project is disabled and the banner explains why.

### 6.2 New-project wizard

Three steps: template, location, review.

- Templates: the same `example`, `minimal`, and `blank` definitions as `finn init`
  (CLI §6.1), with a one-line description and what each contains.
- Location: target directory (picker + text) and project name. Name maps to
  `meta.name`.
- Review: shows the exact `finn init` command and the resulting path.
- Create invokes `finn init --json`. On success, open the editor at the created path.
- On `FINN-E0061`, show the diagnostic and offer another directory; an explicit
  "Overwrite finn.json" action maps to `--force` after typed confirmation.
- States: idle, creating (indeterminate progress), success, error (diagnostic +
  retry). If the CLI is unavailable, Create is disabled.

### 6.3 Schema editor

Layout: left rail (Tables, Indexes, Views, init, Database), main pane, diagnostics
below.

#### 6.3.1 Tables

- List with add, rename, delete.
- Rename cascades to `references`, table `primaryKey` arrays, and index `columns`. It
  does not rewrite SQL text or `response.columns`; Studio warns that the CLI may
  report unresolved references.
- Delete is blocked while another table's column references the table, with the
  referrers listed. Self-references are allowed and removed with the table.
  A database-level index targeting the table also blocks deletion. Views are opaque
  SQL; Studio warns but cannot detect references.
- New tables start with an `id` column (`integer`, primary key, autoincrement) so the
  ≥1-column rule is satisfied.
- States: empty ("No tables" + Add), error (duplicate/reserved names inline).

#### 6.3.2 Column grid

One row per column; add, delete, reorder by drag. Every `ColumnDef` field has a
control:

| Field | Control | Rules |
|---|---|---|
| name | text | Required; no `.`; no `sqlite_` prefix |
| type | dropdown | Canonical set only: `integer`, `real`, `text`, `blob`, `boolean`, `json`, `datetime`; aliases accepted on load and normalized on save |
| nullable | toggle | Default false; a single-column INTEGER PK is implicitly non-null |
| default | value editor + live SQL preview | A string is a SQL expression, so text literals must be quoted inside the string, e.g. `"'open'"`; preview shows the emitted fragment |
| primaryKey | checkbox | Edits the table-level `primaryKey` array; loads from the array, or synthesizes it from column flags when only flags exist |
| autoincrement | toggle | Enabled only for a sole `integer` primary key and not on `WITHOUT ROWID` tables |
| unique | toggle | — |
| check | text | Free SQL expression; CLI semantic stage validates |
| references | two-step table → column picker | Target columns filtered to `unique` or the sole single-column PK (README §4.3) |
| onDelete | select | `NO ACTION`, `RESTRICT`, `SET NULL`, `SET DEFAULT`, `CASCADE`; enabled only with `references` |
| onUpdate | select | Same values and gating |
| description | textarea | — |

Default preview examples: `"datetime('now')"` → `DEFAULT (datetime('now'))`;
`"'open'"` → `DEFAULT ('open')`; `0` → `DEFAULT 0`; `true` → `DEFAULT 1`; absent →
no clause.

Primary-key rules: the table-level array is authoritative. If legacy per-column flags
are present and agree, Studio preserves them; if the author toggles a checkbox,
Studio updates the array and any legacy flags together. Conflicting forms are
surfaced as an error.

Effective-type indicator: when `references` is set and the author has not explicitly
chosen a type, Studio fills the type from the target column and shows a read-only
"coerced from `<table>.<column>`" tag. Because README §4.2 requires `type`, the
derived value is written to the file. Clearing the reference keeps the last type and
removes the tag.

States: empty is impossible inside a table (last-column delete blocked); loading
skeleton on open; errors inline for duplicate names, invalid type, invalid default,
or dangling reference.

#### 6.3.3 Types and references

- The type dropdown offers exactly the seven canonical types; aliases are accepted
  only when loading an existing file.
- `references` accepts `"table"` (resolved to the target's sole PK) and
  `"table.column"`. The picker writes the explicit `table.column` form. A loaded bare
  reference is resolved for display and preserved unless edited.
- If the target table has a composite PK, a bare reference cannot be resolved; Studio
  shows an error and requires an explicit column.
- Self-references are allowed.
- `onDelete`/`onUpdate` are disabled without `references`; default is `NO ACTION`.

#### 6.3.4 Indexes

Two editors:

- Database-level list (`database.indexes`): fields `table`, `columns[]`, `unique`,
  `name`. `table` is required because the parser needs it to bind the index; this is
  a README §4.4 gap (see §11).
- Per-table list (`TableDef.indexes`): same fields without `table`.

`columns` is an ordered multi-select limited to the target table's columns. `name` is
optional; when empty, Studio shows `ix_<table>_<col>[_<col>…]` as a preview and omits
`name` from the file. Duplicate names across all indexes are flagged. States: empty
list + Add; error for missing columns or duplicate name.

#### 6.3.5 Views

List of view name + SQL editor. A view is a single read-only `SELECT` definition
(README §4.5); Studio flags other statement kinds immediately. Empty state: "No
views" + Add. Error state: empty SQL or non-SELECT advisory. The CLI semantic stage
is authoritative.

#### 6.3.6 `init`

Ordered statement list with add, edit, delete, and drag reorder. Each item is a SQL
string. A persistent warning banner states: "Runs once, on first database creation
only" (README §4.6). Empty state: explanation + Add statement. Error state: empty
statement or CLI diagnostic per index.

#### 6.3.7 `database.file`

Filename field. Validation: non-empty, no `/` or `\`, not `.` or `..`, no NUL.
Helper text: "The database is always emitted inside the build directory"
(README §4.7). Empty means default `finn.db`; Studio omits the key. A read-only note
appears if `FINN_DB_PATH` is set in the host environment.

#### 6.3.8 DDL preview (P0)

Read-only, live-rendered `CREATE TABLE` / `CREATE INDEX` / `CREATE VIEW` statements,
in dependency-safe table order, with a Copy button. The pane is labelled advisory;
`finn build` is authoritative.

Rendering MUST be done by the Studio host process importing `database` —
`parse_database` on the in-memory tree (best-effort), then `schema_ddl` (DB Layer
Guide §4). Studio MUST NOT re-implement DDL generation (DB Layer Guide §10.4). On
`ConfigError`, the pane shows the resulting diagnostics instead of DDL. If the host
cannot import `database`, the pane reports "unavailable"; no other functionality
depends on this import. Validation, build, and run remain CLI-mediated (§3).

Example:

```sql
CREATE TABLE IF NOT EXISTS "orders" (
  "id" INTEGER PRIMARY KEY AUTOINCREMENT,
  "user_id" INTEGER NOT NULL,
  "status" TEXT NOT NULL DEFAULT ('open'),
  FOREIGN KEY ("user_id") REFERENCES "users" ("id") ON DELETE CASCADE ON UPDATE NO ACTION
)
```

States: updating (debounced), ready, diagnostics-only (parse/validation failed),
unavailable (import or malformed in-memory state).

### 6.4 API editor

Layout: endpoint list left, endpoint detail right.

#### 6.4.1 `basePath` and endpoints

- `basePath` text, default `/`; blank means omit and use `/`. Studio warns if a
  non-empty value does not start with `/`.
- Endpoint list keyed by path template. Add, rename, delete. Renaming re-derives path
  parameters and leaves SQL untouched, so diagnostics may appear.
- `{segment}` tokens are auto-derived and shown as read-only path parameters
  (README §5.3).
- Empty state: "No endpoints" + Add. Error: duplicate path, invalid template, or CLI
  diagnostic.

#### 6.4.2 Methods

Tabs for `get`, `post`, `put`, `patch`, `delete`. `head` and `options` render as
fixed "auto (FastAPI)" badges and MUST NOT be addable (README §5.1). A loaded
`head`/`options` key appears as a read-only ignored badge with a Remove action.
Loaded uppercase method keys are normalized to lower-case on save. Deleting a method
requires confirmation.

#### 6.4.3 Operation editor

- SQL textarea with highlighting for `:name`, `@name`, `$name`, ignoring placeholders
  inside string literals and SQL comments.
- Live statement-classification chip: `SELECT`, `INSERT`, `UPDATE`, `DELETE`, `DDL`,
  or `unclassified`. DDL (`CREATE`/`ALTER`/`DROP`/`PRAGMA`) is a config error
  (README §5.5). `WITH` is classified by its top-level statement; if uncertain, show
  "unclassified" and defer to the CLI.
- `multi` toggle: allows multiple `;`-separated statements in one transaction
  (README §5.4). Without it, internal `;` is flagged; one trailing `;` is ignored.
- Positional `?` outside literals is flagged (README §5.3).
- `description`, `authentication`, `rateLimit` fields; the last two are read-only
  reserved keys.
- For multi/mixed statements, the response preview is disabled because README does
  not define which statement supplies the response.

#### 6.4.4 Params

| Column | Rules |
|---|---|
| name | Placeholder name |
| source | `path`, `query`, `body`, `header` |
| required | Boolean; path rows locked true |
| type | Canonical type dropdown |
| default | Value editor; disabled for path rows |

- Path rows are auto-managed from the path template and cannot be deleted or
  re-sourced.
- Header-sourced params REQUIRE an explicit row (README §5.3); a header placeholder
  without one is an error.
- Placeholders without an explicit row are shown with an "inferred" tag and source
  "auto (query/body)"; they are advisory and do not block. Promoting one creates an
  explicit row and writes it to `params`.
- A parameter may come from exactly one source; conflicting rows are flagged.
- Required/default binding semantics are README §5.3 and are not redefined here.

#### 6.4.5 Placeholder resolver

Lists every placeholder with its resolved source: path, query, body, header,
inferred, or unresolved. Unresolved and unbindable placeholders are red; inferred
ones are neutral. Reserved `__pageSize` and `__offset` are flagged if the author
declares or uses them (README §6.4). The panel is advisory; the CLI build enforces
resolution.

#### 6.4.6 Response

- `shape`: `list` (default), `single`, `none`.
- `key`: enabled only for `single`.
- `columns`: ordered projection chips with schema autocomplete; free text allowed for
  SQL aliases.
- `renames`: key → value map.
- `paginate`: enabled only when `shape = list` and the statement classifies as
  `SELECT` (README §6.2/§6.4). Changing shape away from `list` disables the toggle
  and, if `paginate` was true, shows an inline error with a "Clear pagination"
  action; save is blocked until resolved.
- `response.blob` is not defined by README §6.2; Studio preserves it verbatim if
  present and exposes no control.

#### 6.4.7 Reserved keys

`authentication` and `rateLimit` render read-only with a "reserved for v1+" tag.
No effect is implied.

### 6.5 Diagnostics panel

- Debounced `finn validate --json` after edits (300 ms; implementation may tune).
- Because the CLI reads a file, Studio writes the in-memory tree to
  `.finn-studio-validate.json` in the project directory, validates that path, and
  removes the file afterward. One validation runs at a time; stale results are
  discarded by sequence number.
- Grouped by stage: parse, structural, semantic, build, runtime. Each row shows
  severity icon, code, path, message, and hint.
- Clicking a row navigates to the owning element. Paths are mapped using the known
  config structure; endpoint keys are matched longest-first because they may contain
  `/` and `.`.
- Any `severity: "error"` disables Build and Run. Warnings never block.
- States: idle, validating (spinner), OK (with counts), errors, engine unavailable.
  Engine unavailable shows no diagnostics.

### 6.6 Build panel

- Save gate first. Build runs `finn build --json --config <path>` with the resolved
  path.
- Progress is indeterminate because build emits one JSON document; stderr is streamed
  into an expandable log.
- On success: artifact list (name, bytes, created/updated), database path and
  created/reused, duration.
- Actions: Open build folder, Run.
- On `FINN-E0042`, show the diagnostic and guidance that a human must resolve the
  conflict by editing the config or deliberately recreating the database. Studio
  MUST NOT offer a destructive reset or migration.
- On failure: diagnostics plus the log. Keep the previous successful artifact list
  visible but marked stale.

### 6.7 Run panel

Lifecycle states: `idle`, `starting`, `listening`, `stopped`, `crashed`.

- Port field, default 8000, with a host-side availability check. The check is
  advisory; the CLI's `FINN-E0051` is authoritative.
- Start spawns `finn run --json --config <path> --host 127.0.0.1 --port <chosen>`.
- Console streams parsed NDJSON events and raw server logs.
- Health chip polls `/healthz` from the host process (not the browser, to avoid
  CORS), every 2 s while listening: green 200, red failure, gray idle.
- Actions:
  - Start — disabled unless idle/stopped/crashed and the tree is clean.
  - Stop — SIGTERM, then SIGKILL after a 5 s grace; state `stopped`.
  - Restart — stop → save (if dirty) → build → start. This replaces hot-reload;
    Studio MUST NOT offer file-watching auto-restart (README §10.4).
- Links when listening: Open app (`http://127.0.0.1:<port><basePath>`), Open `/docs`,
  Open `/redoc`.
- Errors: `E0051` shows the port hint; `E0041` with a missing build prompts Build.
  A crashed child leaves state `crashed` with the exit code and log tail.

### 6.8 Test-request panel (P1)

- Assembles a request from the selected operation: method, URL, path substitutions,
  query rows, header rows, and a JSON body scaffolded from referenced columns with
  type-appropriate placeholders.
- Inferred params require a test-only placement choice (query or body); default query
  for `GET`/`DELETE` and body for write methods, clearly labelled as a test choice,
  not a config decision.
- The HOST process sends the request, not the browser, because the generated API has
  CORS off by default (README §9). The host restricts targets to
  `127.0.0.1:<active app port>`.
- Displays status, response JSON, and, for error-contract bodies,
  `code`/`message`/`detail` (README §7).
- Studio MUST NOT execute SQL directly against the database anywhere in v1. All
  execution flows through a running server.

## 7. Security

- Bind the Studio host to `127.0.0.1` only; remote and multi-user access are out of
  scope.
- One-time URL token, exchanged for an `HttpOnly`, `SameSite=Strict` cookie;
  invalidate after first load.
- Validate `Host`/`Origin` to mitigate DNS rebinding.
- Spawn subprocesses with argv arrays, never a shell; `cwd` is the project directory.
- No telemetry, analytics, or remote assets.
- No direct database access in v1.
- Config file operations are confined to the project directory. Recent-project and
  draft metadata may live in the OS application-data directory, but config content
  never does.
- Test requests are loopback-only and restricted to the active app port.

## 8. Priority tiers

| Tier | Scope |
|---|---|
| P0 | Schema and API editor coverage, canonical save, diagnostics panel, build panel, run panel, init wizard, version gate |
| P1 | External-change watch, comment preservation, test-request panel, crash-draft recovery, Advanced unknown-keys tree |
| P2 | ERD diagram view (read-only, auto-layout from FK data), sample-data preview via an in-memory copy of the DB (read-only), multi-project workspace, desktop wrapper |

P2 items are not part of v1. The P2 sample-data preview is the only future feature
that may read the database directly; it remains read-only and does not weaken the v1
rule.

## 9. Coverage matrix

Locations: S3-T Tables, S3-C Column grid, S3-I Indexes, S3-V Views, S3-N init, S3-D
Database, S4-E Endpoints, S4-O Operation, S4-P Params, S4-R Response, S5 Diagnostics,
S6 Build, S7 Run.

### 9.1 Database vocabulary (README §4)

| README field | GUI control | Location |
|---|---|---|
| `database.file` (§4.7) | Filename field | S3-D |
| `database.tables` (§4) | Table list add/delete | S3-T |
| `database.indexes` (§4) | Database-level index list | S3-I |
| `database.views` (§4) | View list | S3-V |
| `database.init` (§4) | Ordered SQL list | S3-N |
| `tables.<t>.columns` (§4.1) | Column grid | S3-C |
| `tables.<t>.primaryKey` (§4.1) | PK checkboxes | S3-C |
| `tables.<t>.indexes` (§4.1) | Per-table index list | S3-I |
| `tables.<t>.withoutRowid` (§4.1) | Toggle | S3-T settings |
| `columns.<c>.type` (§4.2) | Canonical dropdown | S3-C |
| `columns.<c>.nullable` (§4.2) | Toggle | S3-C |
| `columns.<c>.default` (§4.2) | Value editor + SQL preview | S3-C |
| `columns.<c>.primaryKey` (§4.2) | Checkbox editing table array | S3-C |
| `columns.<c>.autoincrement` (§4.2) | Gated toggle | S3-C |
| `columns.<c>.unique` (§4.2) | Toggle | S3-C |
| `columns.<c>.check` (§4.2) | Text | S3-C |
| `columns.<c>.references` (§4.2/§4.3) | Two-step picker | S3-C |
| `columns.<c>.onDelete` (§4.2/§4.3) | Select | S3-C |
| `columns.<c>.onUpdate` (§4.2/§4.3) | Select | S3-C |
| `columns.<c>.description` (§4.2) | Textarea | S3-C |
| canonical types `integer`/`real`/`text`/`blob`/`boolean`/`json`/`datetime` (§4.2) | Dropdown options | S3-C |
| aliases `int`/`float`/`double`/`string`/`varchar`/`bytes` (§4.2) | Accepted on load, normalized on save | S3-C |
| bare `references: "table"` (§4.3) | Picker resolves to sole PK; preserved unless edited | S3-C |
| `references: "table.column"` (§4.3) | Picker writes explicit form | S3-C |
| FK actions `NO ACTION`/`RESTRICT`/`SET NULL`/`SET DEFAULT`/`CASCADE` (§4.3) | Select options | S3-C |
| effective type coercion (§4.3) | Read-only "coerced from" indicator | S3-C |
| index `columns` (§4.4) | Ordered multi-select | S3-I |
| index `unique` (§4.4) | Toggle | S3-I |
| index `name` (§4.4) | Text + auto-name preview | S3-I |
| database-level index `table` (parser requirement) | Table selector | S3-I |
| view name (§4.5) | Name field | S3-V |
| view `sql` (§4.5) | SQL editor | S3-V |
| `init` item (§4.6) | Statement editor + drag reorder | S3-N |

### 9.2 API vocabulary (README §5–§6)

| README field | GUI control | Location |
|---|---|---|
| `api.basePath` (§5) | Text, default `/` | API settings |
| `api.endpoints` (§5) | Endpoint list | S4-E |
| endpoint path key (§5.1) | Path field | S4-E |
| `get` (§5.1) | Method tab | S4-E |
| `post` (§5.1) | Method tab | S4-E |
| `put` (§5.1) | Method tab | S4-E |
| `patch` (§5.1) | Method tab | S4-E |
| `delete` (§5.1) | Method tab | S4-E |
| `head` (§5.1) | N/A — auto (FastAPI) | S4-E badge |
| `options` (§5.1) | N/A — auto (FastAPI) | S4-E badge |
| uppercase method normalization (§5.1) | Tabs write lower-case | S4-E |
| operation `sql` (§5.2) | SQL editor | S4-O |
| operation `params` (§5.2) | Params table | S4-P |
| operation `response` (§5.2) | Response panel | S4-R |
| operation `description` (§5.2) | Text | S4-O |
| operation `authentication` (§5.2) | Read-only reserved | S4-O |
| operation `rateLimit` (§5.2) | Read-only reserved | S4-O |
| bare-string shorthand (§5.2) | Preserved while SQL-only | S4-O |
| param `source` path/query/body/header (§5.3) | Source select; path locked | S4-P |
| param `required` (§5.3) | Toggle; path locked true | S4-P |
| param `type` (§5.3) | Canonical type select | S4-P |
| param `default` (§5.3) | Value editor | S4-P |
| `:name` placeholders (§5.3) | Highlighting + resolver | S4-O/S4-P |
| `@name` / `$name` (§5.3) | Same resolution, spelling preserved | S4-O/S4-P |
| positional `?` (§5.3) | Flagged as error | S4-O |
| explicit header declaration (§5.3) | Required header row | S4-P |
| inferred params (§5.3) | "Inferred" tag, promotable | S4-P |
| `multi` (§5.4) | Toggle | S4-O |
| embedded `;` without `multi` (§5.4) | Flagged | S4-O |
| statement classification (§5.5) | Chip | S4-O |
| `response.shape` (§6.2) | Select | S4-R |
| `response.key` (§6.2) | Text, single only | S4-R |
| `response.columns` (§6.2) | Projection chips | S4-R |
| `response.renames` (§6.2) | Key → value map | S4-R |
| `response.paginate` (§6.2/§6.4) | Guarded toggle | S4-R |
| `response.blob` (§6.3) | Not defined by §6.2; preserved verbatim, no control | Advanced tree (P1) |

README references without a §5/§6.2 definition — `api.cors` (§9) and configurable
`/healthz` (§10.5) — get no control; Studio preserves them verbatim if present and
relies on CLI validation.

## 10. Acceptance criteria

1. A project created through the wizard yields a `finn.json` that passes
   `finn validate`.
2. A two-table schema with a foreign key can be authored entirely in the GUI, saved,
   and validated.
3. Endpoints using path, query, body, and header parameters can be authored in the
   GUI and validate; the generated OpenAPI lists them.
4. The pagination-vs-shape guard is enforced: `paginate` cannot remain true with
   `shape` other than `list`, or on a non-`SELECT` operation.
5. Building via the GUI produces `main.py`, `API.py`, `DB.py`, and `finn.db`.
6. Running via the GUI reaches `/healthz` 200; Stop terminates the process within the
   grace period and ends in `stopped`.
7. Every README §4/§5/§6 field is editable per the coverage matrix; a config
   exercising all fields round-trips and validates.
8. Invalid edits cannot be built: severity-error diagnostics disable Build/Run and
   identify the offending path.
9. Any save round-trip leaves valid JSON on disk with canonical formatting, key
   order, and preserved author ordering.
10. `version !== 1` opens read-only; no save/build/run is possible.
11. With the CLI missing or too old, editing and saving still work and
    init/build/run are disabled with a banner.
12. The host binds loopback only, the URL token is single-use, subprocesses use argv
    arrays, and Studio performs no direct database access.

## 11. Known README gaps

| README reference | Gap | Studio behavior |
|---|---|---|
| §4.4 vs parser | Database-level index objects show no `table` field, but the parser requires one | Studio adds a table selector; matrix documents it |
| §4.3 vs §4.2 | Referencing type is "coerced if not explicitly set," but `type` is required | Studio materializes the derived type and labels it |
| §5.3 | Query-vs-body inference for `:name` is undefined | Studio labels inferred params without guessing; CLI is authoritative |
| §5.4/§5.5 | Multi-statement response semantics are undefined | Studio disables response preview for multi/mixed operations |
| §9 | `api.cors` is referenced but undefined in §5 | No control; preserved verbatim if present |
| §6.3 | `response.blob` is mentioned but undefined in §6.2 | No control; preserved verbatim |
| §10.5 | `/healthz` is "configurable" but no key is defined | Fixed `/healthz` link |
| §8.1 | Cites "§9.4" for schema conflicts; §9 has no subsections | Studio follows README §10.3 |

## 12. Non-goals

- Multi-user or remote access.
- Authentication/authorization enforcement UI.
- Migration editing.
- SQL console.
- Hot-reload editing.
- Plugin system.
- Cloud sync.

---

**End of spec (v0.1 Draft).**
