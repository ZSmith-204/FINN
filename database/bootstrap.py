"""SQLite connection handling, schema bootstrap, and data helpers (§4.6, §10.2).

Public surface (relied on by the API/build layers):

* :func:`connect` -- open a connection with the mandated pragmas.
* :func:`bootstrap` -- create the schema once; run ``init`` only on first creation.
* :func:`serialize_row` / :func:`serialize_rows` -- reference.md §6.3 value serialization.
* :func:`map_sqlite_error` -- reference.md §7 SQLite error mapping.
"""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .ddl import quote_ident, schema_ddl
from .schema import (
    ConfigError,
    DatabaseSpec,
    SchemaConflictError,
    affinity_for_declared_type,
)

__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "DEFAULT_BLOB_FORMAT",
    "DB_INIT_CONFLICT",
    "DB_INIT_CONFIG",
    "DB_INIT_DB",
    "DB_INIT_FILE",
    "DB_INIT_UNKNOWN",
    "ValidationError",
    "connect",
    "bootstrap",
    "db_init",
    "describe_schema",
    "coerce_value",
    "coerce",
    "serialize_value",
    "serialize_row",
    "serialize_rows",
    "map_sqlite_error",
]

LOGGER = logging.getLogger("database")

DEFAULT_BUSY_TIMEOUT_MS = 5000
DEFAULT_BLOB_FORMAT = "$base64:{}"

DB_INIT_CONFLICT = "SCHEMA_CONFLICT"
DB_INIT_CONFIG = "CONFIG_ERROR"
DB_INIT_DB = "DB_ERROR"
DB_INIT_FILE = "FILE_ERROR"
DB_INIT_UNKNOWN = "UNKNOWN"

_MEMORY_PATHS = {":memory:", ""}


class ValidationError(ValueError):
    """A Python value is not valid for its declared FINN column type (§4.2)."""


def connect(
    path,
    *,
    create: bool = True,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """Open a SQLite connection and apply the connection pragmas (§10.2).

    ``create=True`` opens or creates the database file (and its parent
    directory).  ``create=False`` refuses to create a missing file and raises
    :class:`FileNotFoundError` instead.
    """

    target = str(path)
    if target not in _MEMORY_PATHS:
        file_path = Path(target)
        if not create and not file_path.exists():
            raise FileNotFoundError(
                f"database file does not exist: {file_path}"
            )
        file_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(target)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def _schema_object_count(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type IN ('table', 'index', 'view') AND name NOT LIKE 'sqlite_%'"
    ).fetchone()
    return int(row[0])


def describe_schema(connection: sqlite3.Connection) -> Dict[str, Dict[str, Dict]]:
    """Return column metadata for every table and view (§4.5, §10.1 step 4).

    Shape::

        {table_or_view: {column: {"type": str, "notnull": bool,
                                  "pk": bool, "default": str | None}}}

    ``PRAGMA table_info`` works for views as well as tables; hidden/internal
    ``sqlite_%`` objects are skipped.
    """

    names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    ]
    described: Dict[str, Dict[str, Dict]] = {}
    for name in names:
        columns: Dict[str, Dict] = {}
        for row in connection.execute(f"PRAGMA table_info({quote_ident(name)})"):
            # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk
            columns[row[1]] = {
                "type": row[2] or "",
                "notnull": bool(row[3]),
                "pk": bool(row[5]),
                "default": row[4],
            }
        described[name] = columns
    return described


def _normalized_affinity(declared_type: str) -> str:
    """Reduce a declared SQLite type to its affinity class (§4.2)."""

    return affinity_for_declared_type(declared_type)


def _verify_compatible(connection: sqlite3.Connection, spec: DatabaseSpec) -> None:
    """Fail clearly when an existing database conflicts with ``spec`` (§10.3).

    Read-only: never drops or alters anything.  Raises ``SchemaConflictError``
    (a ``ConfigError``) with a path-scoped message on the first conflict.
    """

    actual = describe_schema(connection)
    for table_name, table in spec.tables.items():
        if table_name not in actual:
            raise SchemaConflictError(
                f"database.tables.{table_name}",
                f"table '{table_name}' is missing from the existing database",
            )
        actual_columns = actual[table_name]
        for column in table.columns.values():
            path = f"database.tables.{table_name}.columns.{column.name}"
            if column.name not in actual_columns:
                raise SchemaConflictError(
                    path,
                    f"column '{column.name}' is missing from existing table "
                    f"'{table_name}'",
                )
            existing = actual_columns[column.name]
            actual_affinity = _normalized_affinity(existing["type"])
            if actual_affinity != column.affinity:
                raise SchemaConflictError(
                    path,
                    f"column type changed: spec declares {column.fin_type!r} "
                    f"({column.affinity} affinity) but the existing database "
                    f"declares {existing['type']!r} ({actual_affinity} affinity)",
                )
            is_single_pk = (
                len(table.primary_key) == 1
                and table.primary_key[0] == column.name
            )
            expected_notnull = (not column.nullable) and not (
                is_single_pk and column.affinity == "INTEGER"
            )
            if bool(existing["notnull"]) != expected_notnull:
                raise SchemaConflictError(
                    path,
                    "column nullability changed: spec expects "
                    f"{'NOT NULL' if expected_notnull else 'NULL'} but the "
                    "existing database disagrees"
                    " (the database was created by an older schema; "
                    "recreate it)",
                )
            expected_pk = column.name in table.primary_key
            if bool(existing["pk"]) != expected_pk:
                raise SchemaConflictError(
                    path,
                    "column primary-key membership changed",
                )
            if column.unique and not _has_unique_constraint(
                connection, table_name, column.name
            ):
                raise SchemaConflictError(
                    path,
                    "column uniqueness changed: spec declares UNIQUE but the "
                    "existing database does not",
                )


def _has_unique_constraint(
    connection: sqlite3.Connection, table_name: str, column_name: str
) -> bool:
    """True when a single-column UNIQUE index/constraint covers ``column_name``.

    A column that is merely one member of a composite UNIQUE index does not
    satisfy a single-column UNIQUE declaration, and partial indexes do not
    cover every row, so neither counts (§10.3).
    """

    for row in connection.execute(f"PRAGMA index_list({quote_ident(table_name)})"):
        # PRAGMA index_list: seq, name, unique, origin, partial
        if not row["unique"]:
            continue
        if "partial" in row.keys() and row["partial"]:
            continue
        index_name = row["name"]
        index_columns = [
            info["name"]
            for info in connection.execute(
                f"PRAGMA index_info({quote_ident(index_name)})"
            )
        ]
        if index_columns == [column_name]:
            return True
    return False


def _ddl_failure(
    statement: str, exc: sqlite3.DatabaseError
) -> SchemaConflictError:
    """Wrap a raw SQLite failure raised while applying DDL (§10.3).

    The message names the failing statement so a raw ``sqlite3.OperationalError``
    (for example ``CREATE INDEX`` over a missing column) never escapes
    :func:`bootstrap` unwrapped.
    """

    summary = " ".join(statement.split())
    if len(summary) > 160:
        summary = summary[:157] + "..."
    return SchemaConflictError(
        "database",
        f"schema statement could not be applied: {summary!r} ({exc})",
    )


def bootstrap(path, spec: DatabaseSpec) -> bool:
    """Create the schema from ``spec``; return True if the DB was new (§4.6).

    Tables, indexes, and views are created in dependency-safe order.  ``init``
    statements run inside the same transaction **only** when the database had
    no schema objects yet.  When the database already exists, bootstrap is
    non-destructive (§10.3): compatibility is verified *before* any DDL runs,
    and it never drops or alters anything.  It introspects ``sqlite_master`` /
    ``PRAGMA table_info`` and raises ``SchemaConflictError`` (a
    ``ConfigError``) if the spec conflicts with the on-disk schema (missing
    table/column, changed type, nullability, PK, or uniqueness).  A compatible
    existing database is returned as ``False``.
    """

    target = str(path)
    if target not in _MEMORY_PATHS:
        Path(target).parent.mkdir(parents=True, exist_ok=True)

    connection = connect(target)
    try:
        newly_created = _schema_object_count(connection) == 0

        # Verify first: a conflicting spec must not execute DDL (and thereby
        # commit a partial schema) before the conflict is reported.
        if not newly_created:
            _verify_compatible(connection, spec)

        statements = schema_ddl(spec)
        if newly_created:
            statements.extend(spec.init)

        connection.execute("BEGIN")
        try:
            for statement in statements:
                try:
                    connection.execute(statement)
                except sqlite3.DatabaseError as exc:
                    raise _ddl_failure(statement, exc) from exc
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

        return newly_created
    finally:
        connection.close()


def db_init(path, spec: DatabaseSpec) -> Tuple[bool, Optional[str]]:
    """Non-raising wrapper around :func:`bootstrap` (§4.6).

    Returns ``(True, None)`` on success, or ``(False, error_code)`` on failure.
    The exception detail is logged and never re-raised, so callers that only
    need a success flag plus a machine-readable code can branch on the code.
    """
    try:
        bootstrap(path, spec)
    except SchemaConflictError as exc:
        LOGGER.error("db_init schema conflict for %s: %s", path, exc)
        return False, DB_INIT_CONFLICT
    except ConfigError as exc:
        LOGGER.error("db_init config error for %s: %s", path, exc)
        return False, DB_INIT_CONFIG
    except sqlite3.Error as exc:
        LOGGER.error("db_init sqlite error for %s: %s", path, exc)
        return False, DB_INIT_DB
    except OSError as exc:
        LOGGER.error("db_init file error for %s: %s", path, exc)
        return False, DB_INIT_FILE
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.error("db_init unexpected error for %s: %s", path, exc)
        return False, DB_INIT_UNKNOWN
    return True, None


# ---------------------------------------------------------------------------
# Serialization (reference.md §6.3)
# ---------------------------------------------------------------------------


def coerce_value(column, value: object) -> object:
    """Coerce a Python value to its SQLite storage form (§4.2 write rules).

    ``column`` is a :class:`database.schema.Column` or a canonical FINN
    type string.  ``None`` always passes through (NULL); the DDL/NOT NULL
    constraint decides whether NULL is acceptable.

    * ``boolean`` -> ``0``/``1`` (accepts bool, 0/1, or "true"/"false").
    * ``json`` -> JSON text (``json.dumps``); string input is validated with
      ``json.loads`` first and stored unchanged.
    * ``datetime`` -> the ISO-8601 string, validated (``Z`` accepted as UTC).
    * ``integer``/``real``/``text``/``blob`` -> light type checks, passthrough.

    Invalid values raise :class:`ValidationError` (a ``ValueError``).
    """

    fin_type = column if isinstance(column, str) else getattr(column, "fin_type", None)
    if value is None:
        return None

    if fin_type == "boolean":
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int) and value in (0, 1):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "1"):
                return 1
            if lowered in ("false", "0"):
                return 0
        raise ValidationError(
            f"boolean value must be true/false or 0/1, got {value!r}"
        )

    if fin_type == "json":
        if isinstance(value, (bytes, bytearray)):
            try:
                value = bytes(value).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValidationError(
                    "json value must be UTF-8 text, got bytes that do not "
                    f"decode as UTF-8: {exc}"
                ) from exc
        if isinstance(value, str):
            try:
                json.loads(value)
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"invalid JSON value: {exc}") from exc
            return value
        if isinstance(value, (dict, list, tuple)):
            try:
                return json.dumps(value)
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"value is not JSON-serializable: {exc}") from exc
        raise ValidationError(
            f"json value must be a JSON string, object, or array, got "
            f"{type(value).__name__}"
        )

    if fin_type == "datetime":
        if not isinstance(value, str):
            raise ValidationError(
                f"datetime value must be an ISO-8601 string, got "
                f"{type(value).__name__}"
            )
        candidate = value.strip()
        if candidate.endswith(("Z", "z")):
            candidate = candidate[:-1] + "+00:00"
        try:
            datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise ValidationError(
                f"invalid ISO-8601 datetime {value!r}: {exc}"
            ) from exc
        return value

    if fin_type == "integer":
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        raise ValidationError(
            f"integer value must be an int, got {type(value).__name__}"
        )

    if fin_type == "real":
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        raise ValidationError(
            f"real value must be a number, got {type(value).__name__}"
        )

    if fin_type == "text":
        if isinstance(value, str):
            return value
        raise ValidationError(
            f"text value must be a str, got {type(value).__name__}"
        )

    if fin_type == "blob":
        if isinstance(value, memoryview):
            return value.tobytes()
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        raise ValidationError(
            f"blob value must be bytes, got {type(value).__name__}"
        )

    return value


def coerce(fin_type: str, value: object) -> object:
    """Type-first form of :func:`coerce_value`."""

    return coerce_value(fin_type, value)


def serialize_value(
    value: object,
    fin_type: Optional[str],
    *,
    blob_format: str = DEFAULT_BLOB_FORMAT,
) -> object:
    """Convert one stored SQLite value into its JSON-ready representation.

    Unknown column types pass the raw value through unchanged.
    """

    if value is None:
        return None

    if fin_type == "boolean":
        if isinstance(value, str):
            return value.strip().lower() not in ("", "0", "false", "no", "null")
        return bool(value)

    if fin_type == "json":
        if isinstance(value, (str, bytes, bytearray)):
            try:
                return json.loads(value)
            except (TypeError, ValueError):
                return value
        return value

    if fin_type == "blob":
        if isinstance(value, memoryview):
            value = value.tobytes()
        elif isinstance(value, bytearray):
            value = bytes(value)
        if isinstance(value, str):
            value = value.encode("utf-8")
        if not isinstance(value, bytes):
            value = bytes(value)  # type: ignore[arg-type]
        return blob_format.format(base64.b64encode(value).decode("ascii"))

    # integer / real / text / datetime -> numbers, strings, ISO text unchanged.
    return value


def serialize_rows(
    rows: Iterable,
    column_types: Optional[Dict[str, str]] = None,
    *,
    columns: Optional[List[str]] = None,
    blob_format: str = DEFAULT_BLOB_FORMAT,
) -> List[Dict[str, object]]:
    """Serialize a sequence of result rows into JSON-ready dicts (§6.3).

    ``column_types`` maps column name -> normalized FINN type; columns absent
    from the mapping (or with an unknown type) are passed through as stored.
    ``sqlite3.Row`` and ``dict`` rows resolve column names themselves; plain
    tuples need ``columns`` (defaulting to the ``column_types`` key order).
    """

    types = column_types or {}
    result_rows = list(rows)
    if not result_rows:
        return []

    first = result_rows[0]

    if isinstance(first, sqlite3.Row):
        names = list(first.keys())
        return [
            {
                name: serialize_value(
                    row[name], types.get(name), blob_format=blob_format
                )
                for name in names
            }
            for row in result_rows
        ]

    if isinstance(first, dict):
        return [
            {
                name: serialize_value(
                    value, types.get(name), blob_format=blob_format
                )
                for name, value in row.items()
            }
            for row in result_rows
        ]

    names = list(columns) if columns is not None else list(types)
    if not names and result_rows:
        raise ValueError(
            "column names are required to serialize tuple rows; pass the "
            "columns=... argument"
        )
    serialized: List[Dict[str, object]] = []
    for row in result_rows:
        values = list(row)
        if len(names) != len(values):
            raise ValueError(
                "cannot map row values to column names: "
                f"{len(names)} names for {len(values)} values"
            )
        serialized.append(
            {
                name: serialize_value(
                    value, types.get(name), blob_format=blob_format
                )
                for name, value in zip(names, values)
            }
        )
    return serialized


def serialize_row(
    row,
    column_types: Optional[Dict[str, str]] = None,
    *,
    columns: Optional[List[str]] = None,
    blob_format: str = DEFAULT_BLOB_FORMAT,
) -> Dict[str, object]:
    """Serialize a single result row; see :func:`serialize_rows`."""

    if isinstance(row, sqlite3.Row):
        return serialize_rows(
            [row], column_types, blob_format=blob_format
        )[0]
    if isinstance(row, dict):
        return serialize_rows(
            [row], column_types, blob_format=blob_format
        )[0]
    return serialize_rows(
        [row], column_types, columns=columns, blob_format=blob_format
    )[0]


# ---------------------------------------------------------------------------
# Error mapping (reference.md §7)
# ---------------------------------------------------------------------------


def map_sqlite_error(exc: sqlite3.Error) -> Optional[dict]:
    """Map a SQLite error to the FINN error contract (§7).

    Integrity constraint failures (UNIQUE / FOREIGN KEY / CHECK) become
    ``409 CONFLICT``; anything else returns ``None`` so the caller can fall
    back to a generic internal error.
    """

    if not isinstance(exc, sqlite3.IntegrityError):
        return None

    detail = str(exc)
    if detail.startswith("UNIQUE constraint failed"):
        message = "A unique constraint was violated."
    elif detail.startswith("FOREIGN KEY constraint failed"):
        message = "A foreign key constraint was violated."
    elif detail.startswith("CHECK constraint failed"):
        message = "A check constraint was violated."
    elif detail.startswith("NOT NULL constraint failed"):
        message = "A NOT NULL constraint was violated."
    else:
        message = "An integrity constraint was violated."

    return {
        "code": "CONFLICT",
        "http_status": 409,
        "message": message,
        "detail": detail,
    }