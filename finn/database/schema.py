"""Parsing, validation, and normalization of FINN's ``database`` config block.

This module models only the SQLite schema described by ``database`` in the FINN
config (README §4): tables, columns, relations, indexes, views, and init
statements.  It has no HTTP/API concerns.

Everything is validated strictly and fail-fast: unknown keys, unknown types,
dangling foreign keys, and inconsistent primary keys raise :class:`ConfigError`
carrying the JSON path of the offending element.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Type

__all__ = [
    "ConfigError",
    "SchemaConflictError",
    "Column",
    "Table",
    "Index",
    "View",
    "DatabaseSpec",
    "parse_database",
    "normalize_type",
    "normalize_fk_action",
    "affinity_for_declared_type",
    "TYPE_ALIASES",
    "CANONICAL_TYPES",
    "FK_ACTIONS",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """A config validation failure scoped to a JSON path (README §2.2)."""

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.path}: {self.message}"


class SchemaConflictError(ConfigError):
    """The on-disk schema conflicts with the config (README §10.3).

    Raised during re-boot of an existing database; bootstrap never drops or
    alters existing objects.
    """


# ---------------------------------------------------------------------------
# Canonical types (README §4.2)
# ---------------------------------------------------------------------------

TYPE_ALIASES: Dict[str, str] = {
    "integer": "integer",
    "int": "integer",
    "real": "real",
    "float": "real",
    "double": "real",
    "text": "text",
    "string": "text",
    "varchar": "text",
    "blob": "blob",
    "bytes": "blob",
    "boolean": "boolean",
    "json": "json",
    "datetime": "datetime",
}

# canonical finn type -> declared SQLite affinity + Python type for writes
CANONICAL_TYPES: Dict[str, Dict[str, object]] = {
    "integer": {"affinity": "INTEGER", "py_type": int},
    "real": {"affinity": "REAL", "py_type": float},
    "text": {"affinity": "TEXT", "py_type": str},
    "blob": {"affinity": "BLOB", "py_type": bytes},
    "boolean": {"affinity": "INTEGER", "py_type": bool},
    "json": {"affinity": "TEXT", "py_type": dict},
    "datetime": {"affinity": "TEXT", "py_type": str},
}

FK_ACTIONS: Tuple[str, ...] = (
    "NO ACTION",
    "RESTRICT",
    "SET NULL",
    "SET DEFAULT",
    "CASCADE",
)

DEFAULT_FK_ACTION = "NO ACTION"


def normalize_type(value: object) -> Optional[str]:
    """Return the canonical FINN type for a config ``type`` value, or ``None``."""

    if not isinstance(value, str):
        return None
    return TYPE_ALIASES.get(value.strip().lower())


def normalize_fk_action(value: object, path: str = "database") -> str:
    """Normalize an ``onDelete``/``onUpdate`` action, raising on bad values."""

    if not isinstance(value, str):
        raise ConfigError(path, "must be a string such as 'CASCADE'")
    normalized = " ".join(value.strip().upper().split())
    if normalized not in FK_ACTIONS:
        raise ConfigError(
            path,
            f"invalid referential action {value!r}; "
            f"expected one of {', '.join(FK_ACTIONS)}",
        )
    return normalized


def affinity_for_declared_type(declared_type: str) -> str:
    """Reduce a declared SQLite type to its affinity class (§4.2).

    Used by schema introspection (``PRAGMA table_info``) to compare an existing
    database against the spec without depending on the exact spelling.
    """

    text = (declared_type or "").strip().upper()
    if not text:
        return "BLOB"
    canonical = TYPE_ALIASES.get(text.lower())
    if canonical is not None:
        return str(CANONICAL_TYPES[canonical]["affinity"])
    if "INT" in text:
        return "INTEGER"
    if "CHAR" in text or "CLOB" in text or "TEXT" in text:
        return "TEXT"
    if "BLOB" in text:
        return "BLOB"
    if "REAL" in text or "FLOA" in text or "DOUB" in text:
        return "REAL"
    return "NUMERIC"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Column:
    """One normalized column definition."""

    name: str
    fin_type: str
    affinity: str
    py_type: Type[object]
    nullable: bool = False
    # Raw default value.  When ``default_is_sql`` is True the value is raw SQL
    # text (an expression such as ``datetime('now')`` or a quoted literal such
    # as ``'open'``); otherwise it is a plain JSON literal to be quoted/encoded.
    default: object = None
    default_is_sql: bool = False
    primary_key: bool = False
    autoincrement: bool = False
    unique: bool = False
    check: Optional[str] = None
    references: Optional[Tuple[str, str]] = None
    on_delete: str = DEFAULT_FK_ACTION
    on_update: str = DEFAULT_FK_ACTION
    description: Optional[str] = None


@dataclass
class Index:
    """One index, either table-local or declared at ``database.indexes``."""

    name: str
    table: str
    columns: List[str]
    unique: bool = False


@dataclass
class Table:
    """One normalized table definition."""

    name: str
    columns: Dict[str, Column]
    primary_key: List[str] = field(default_factory=list)
    indexes: List[Index] = field(default_factory=list)
    without_rowid: bool = False


@dataclass
class View:
    """One read-only SQL view definition."""

    name: str
    sql: str


@dataclass
class DatabaseSpec:
    """The fully validated, normalized ``database`` block."""

    file: str = "finn.db"
    tables: Dict[str, Table] = field(default_factory=dict)
    indexes: List[Index] = field(default_factory=list)
    views: Dict[str, View] = field(default_factory=dict)
    init: List[str] = field(default_factory=list)

    def column_types(self, table_name: str) -> Dict[str, str]:
        """Map column name -> canonical FINN type for one table."""

        return {
            name: column.fin_type
            for name, column in self.tables[table_name].columns.items()
        }

    def all_column_types(self) -> Dict[str, str]:
        """Map column name -> canonical FINN type, omitting ambiguous names.

        Column names are not guaranteed unique across tables.  When two tables
        declare the same name with different normalized types, the value cannot
        be serialized correctly from the name alone, so that name is excluded
        from the flat map.  Use :meth:`column_types` (the per-table map) as the
        authoritative source of truth.
        """

        merged: Dict[str, str] = {}
        ambiguous = set()
        for table in self.tables.values():
            for name, fin_type in self.column_types(table.name).items():
                if name in merged and merged[name] != fin_type:
                    ambiguous.add(name)
                else:
                    merged[name] = fin_type
        for name in ambiguous:
            del merged[name]
        return merged


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_DATABASE_KEYS = ("file", "tables", "indexes", "views", "init")
_TABLE_KEYS = ("columns", "primaryKey", "indexes", "withoutRowid")
_COLUMN_KEYS = (
    "type",
    "nullable",
    "default",
    "primaryKey",
    "autoincrement",
    "unique",
    "check",
    "references",
    "onDelete",
    "onUpdate",
    "description",
)
_INDEX_KEYS = ("columns", "unique", "name")
_VIEW_KEYS = ("sql",)


def _check_keys(mapping: dict, allowed: Tuple[str, ...], path: str) -> None:
    unknown = [key for key in mapping if key not in allowed]
    if unknown:
        key = unknown[0]
        raise ConfigError(
            f"{path}.{key}",
            f"unknown key {key!r}; allowed keys: {', '.join(allowed)}",
        )


def _require_object(value: object, path: str, what: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(path, f"{what} must be a JSON object")
    return value


def _require_bool(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(path, f"must be a boolean, got {type(value).__name__}")
    return value


def _require_str(value: object, path: str, *, allow_blank: bool = False) -> str:
    if not isinstance(value, str):
        raise ConfigError(path, f"must be a string, got {type(value).__name__}")
    if not allow_blank and not value.strip():
        raise ConfigError(path, "must not be empty")
    return value


def _require_name(
    value: object, path: str, what: str, *, no_dot: bool = False
) -> str:
    name = _require_str(value, path, allow_blank=False).strip()
    if "\x00" in name:
        raise ConfigError(path, f"{what} must not contain NUL characters")
    if name.startswith("sqlite_"):
        raise ConfigError(path, f"{what} must not use the reserved 'sqlite_' prefix")
    if no_dot and "." in name:
        raise ConfigError(
            path,
            f"{what} must not contain '.' (relations use 'table.column')",
        )
    return name


def _column_path(table_name: str, column_name: str) -> str:
    return f"database.tables.{table_name}.columns.{column_name}"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_database(database_config: dict) -> DatabaseSpec:
    """Validate and normalize the config's ``database`` object (README §4)."""

    if not isinstance(database_config, dict):
        raise ConfigError("database", "must be a JSON object")
    _check_keys(database_config, _DATABASE_KEYS, "database")

    file_name = database_config.get("file", "finn.db")
    if not isinstance(file_name, str) or not file_name.strip():
        raise ConfigError("database.file", "must be a non-empty string")
    file_name = file_name.strip()
    if "\x00" in file_name:
        raise ConfigError(
            "database.file", "must not contain NUL characters"
        )
    if "/" in file_name or "\\" in file_name or file_name in (".", ".."):
        raise ConfigError(
            "database.file",
            "must be a file name only (no path separators); the database is "
            "always emitted inside /build (README §4.7)",
        )

    tables_raw = database_config.get("tables")
    if tables_raw is None:
        raise ConfigError("database.tables", "is required")
    tables_raw = _require_object(tables_raw, "database.tables", "database.tables")

    tables: Dict[str, Table] = {}
    # (table, column) -> raw "references" string, resolved in a second pass so
    # author order never matters.
    pending_refs: Dict[Tuple[str, str], str] = {}
    index_entries: List[Tuple[Index, str]] = []

    for raw_table_name, raw_table in tables_raw.items():
        table_name = _require_name(
            raw_table_name,
            f"database.tables.{raw_table_name}",
            "table name",
            no_dot=True,
        )
        if table_name in tables:
            raise ConfigError(
                f"database.tables.{raw_table_name}",
                f"duplicate table name {table_name!r}",
            )
        table, refs = _parse_table(table_name, raw_table)
        tables[table_name] = table
        pending_refs.update({(table_name, name): raw for name, raw in refs.items()})
        for position, index in enumerate(table.indexes):
            index_entries.append(
                (index, f"database.tables.{table_name}.indexes.{position}")
            )

    # --- views -------------------------------------------------------------
    views_raw = database_config.get("views", {})
    views: Dict[str, View] = {}
    if views_raw is None:
        views_raw = {}
    views_raw = _require_object(views_raw, "database.views", "database.views")
    for raw_view_name, raw_view in views_raw.items():
        view_name = _require_name(
            raw_view_name,
            f"database.views.{raw_view_name}",
            "view name",
        )
        path = f"database.views.{view_name}"
        raw_view = _require_object(raw_view, path, "view definition")
        _check_keys(raw_view, _VIEW_KEYS, path)
        if "sql" not in raw_view:
            raise ConfigError(f"{path}.sql", "is required")
        sql = _require_str(raw_view["sql"], f"{path}.sql")
        views[view_name] = View(name=view_name, sql=sql)

    # --- global indexes ----------------------------------------------------
    indexes_raw = database_config.get("indexes", [])
    if indexes_raw is None:
        indexes_raw = []
    if not isinstance(indexes_raw, list):
        raise ConfigError("database.indexes", "must be an array of index objects")
    global_indexes: List[Index] = []
    for position, raw_index in enumerate(indexes_raw):
        path = f"database.indexes.{position}"
        raw_index = _require_object(raw_index, path, "index definition")
        _check_keys(raw_index, _INDEX_KEYS + ("table",), path)
        if "table" not in raw_index:
            raise ConfigError(
                f"{path}.table",
                "is required for database-level indexes (table-local indexes "
                "live under database.tables.<table>.indexes)",
            )
        if "columns" not in raw_index:
            raise ConfigError(f"{path}.columns", "is required")
        table_name = _require_str(raw_index["table"], f"{path}.table").strip()
        table = tables.get(table_name)
        if table is None:
            raise ConfigError(
                f"{path}.table", f"unknown table {table_name!r}"
            )
        index = _parse_index(
            raw_index,
            path,
            table_name=table_name,
            columns=table.columns,
        )
        global_indexes.append(index)
        index_entries.append((index, path))

    # --- init --------------------------------------------------------------
    init_raw = database_config.get("init", [])
    if init_raw is None:
        init_raw = []
    if not isinstance(init_raw, list):
        raise ConfigError("database.init", "must be an array of SQL strings")
    init: List[str] = []
    for position, statement in enumerate(init_raw):
        path = f"database.init.{position}"
        if not isinstance(statement, str):
            raise ConfigError(path, "must be a SQL string")
        if not statement.strip():
            raise ConfigError(path, "must not be an empty SQL statement")
        init.append(statement)

    # --- resolve relations (README §4.3) -----------------------------------
    for (table_name, column_name), raw_reference in pending_refs.items():
        column = tables[table_name].columns[column_name]
        column.references = _resolve_reference(
            raw_reference,
            tables,
            _column_path(table_name, column_name) + ".references",
        )

    # --- duplicate index names --------------------------------------------
    seen_names: Dict[str, str] = {}
    for index, path in index_entries:
        previous = seen_names.get(index.name)
        if previous is not None:
            raise ConfigError(
                f"{path}.name",
                f"duplicate index name {index.name!r} (already declared at {previous})",
            )
        seen_names[index.name] = path

    return DatabaseSpec(
        file=file_name,
        tables=tables,
        indexes=global_indexes,
        views=views,
        init=init,
    )


def _parse_table(
    table_name: str, raw_table: object
) -> Tuple[Table, Dict[str, str]]:
    """Parse one TableDef; return it plus unresolved ``references`` strings."""

    path = f"database.tables.{table_name}"
    raw_table = _require_object(raw_table, path, "table definition")
    _check_keys(raw_table, _TABLE_KEYS, path)

    columns_raw = raw_table.get("columns")
    if columns_raw is None:
        raise ConfigError(f"{path}.columns", "is required")
    columns_raw = _require_object(columns_raw, f"{path}.columns", "columns")
    if not columns_raw:
        raise ConfigError(f"{path}.columns", "must define at least one column")

    columns: Dict[str, Column] = {}
    pending_refs: Dict[str, str] = {}
    for raw_column_name, raw_column in columns_raw.items():
        column_name = _require_name(
            raw_column_name,
            f"{path}.columns.{raw_column_name}",
            "column name",
            no_dot=True,
        )
        if column_name in columns:
            raise ConfigError(
                f"{path}.columns.{raw_column_name}",
                f"duplicate column name {column_name!r}",
            )
        column, raw_reference = _parse_column(
            table_name, column_name, raw_column
        )
        columns[column_name] = column
        if raw_reference is not None:
            pending_refs[column_name] = raw_reference

    # --- primary key -------------------------------------------------------
    declared_pk: Optional[List[str]] = None
    if "primaryKey" in raw_table and raw_table["primaryKey"] is not None:
        raw_pk = raw_table["primaryKey"]
        if not isinstance(raw_pk, list) or not raw_pk:
            raise ConfigError(
                f"{path}.primaryKey",
                "must be a non-empty array of column names",
            )
        declared_pk = []
        for position, raw_name in enumerate(raw_pk):
            if not isinstance(raw_name, str):
                raise ConfigError(
                    f"{path}.primaryKey.{position}", "must be a column name string"
                )
            pk_column = raw_name.strip()
            if pk_column not in columns:
                raise ConfigError(
                    f"{path}.primaryKey",
                    f"primaryKey references unknown column {pk_column!r}",
                )
            if pk_column in declared_pk:
                raise ConfigError(
                    f"{path}.primaryKey",
                    f"duplicate primaryKey column {pk_column!r}",
                )
            declared_pk.append(pk_column)

    flagged_pk = [name for name, column in columns.items() if column.primary_key]

    if declared_pk is not None and flagged_pk:
        if set(declared_pk) != set(flagged_pk):
            raise ConfigError(
                f"{path}.primaryKey",
                "conflicts with column-level primaryKey flags "
                f"({', '.join(flagged_pk)})",
            )
    primary_key = list(declared_pk if declared_pk is not None else flagged_pk)

    # --- withoutRowid ------------------------------------------------------
    without_rowid = False
    if "withoutRowid" in raw_table and raw_table["withoutRowid"] is not None:
        without_rowid = _require_bool(raw_table["withoutRowid"], f"{path}.withoutRowid")
        if without_rowid and not primary_key:
            raise ConfigError(
                f"{path}.withoutRowid",
                "a WITHOUT ROWID table requires a primary key",
            )

    # --- autoincrement rules (README §4.2) --------------------------------
    for column in columns.values():
        if not column.autoincrement:
            continue
        column_path = f"{path}.columns.{column.name}"
        if column.name not in primary_key:
            raise ConfigError(
                f"{column_path}.autoincrement",
                "autoincrement is only allowed on primary key columns",
            )
        if column.fin_type != "integer":
            raise ConfigError(
                f"{column_path}.autoincrement",
                "autoincrement is only allowed on integer columns",
            )
        if len(primary_key) != 1 or primary_key[0] != column.name:
            raise ConfigError(
                f"{column_path}.autoincrement",
                "autoincrement requires the column to be the sole primary key",
            )
        if without_rowid:
            raise ConfigError(
                f"{path}.withoutRowid",
                "AUTOINCREMENT is not allowed on a WITHOUT ROWID table",
            )

    # --- table-local indexes ----------------------------------------------
    indexes_raw = raw_table.get("indexes", [])
    if indexes_raw is None:
        indexes_raw = []
    if not isinstance(indexes_raw, list):
        raise ConfigError(f"{path}.indexes", "must be an array of index objects")
    indexes: List[Index] = []
    for position, raw_index in enumerate(indexes_raw):
        index_path = f"{path}.indexes.{position}"
        raw_index = _require_object(raw_index, index_path, "index definition")
        _check_keys(raw_index, _INDEX_KEYS, index_path)
        if "columns" not in raw_index:
            raise ConfigError(f"{index_path}.columns", "is required")
        indexes.append(
            _parse_index(
                raw_index,
                index_path,
                table_name=table_name,
                columns=columns,
            )
        )

    table = Table(
        name=table_name,
        columns=columns,
        primary_key=primary_key,
        indexes=indexes,
        without_rowid=without_rowid,
    )
    return table, pending_refs


def _parse_column(
    table_name: str, column_name: str, raw_column: object
) -> Tuple[Column, Optional[str]]:
    """Parse one ColumnDef; the second element is the raw ``references`` text."""

    path = _column_path(table_name, column_name)
    raw_column = _require_object(raw_column, path, "column definition")
    _check_keys(raw_column, _COLUMN_KEYS, path)

    if "type" not in raw_column:
        raise ConfigError(f"{path}.type", "is required")
    raw_type = raw_column["type"]
    if not isinstance(raw_type, str):
        raise ConfigError(f"{path}.type", "must be a string")
    fin_type = normalize_type(raw_type)
    if fin_type is None:
        raise ConfigError(
            f"{path}.type",
            f"unknown column type {raw_type!r}; expected one of "
            "integer, real, text, blob, boolean, json, datetime "
            "(and aliases int, float, double, string, varchar, bytes)",
        )
    meta = CANONICAL_TYPES[fin_type]

    nullable = False
    if "nullable" in raw_column and raw_column["nullable"] is not None:
        nullable = _require_bool(raw_column["nullable"], f"{path}.nullable")

    primary_key = False
    if "primaryKey" in raw_column and raw_column["primaryKey"] is not None:
        primary_key = _require_bool(
            raw_column["primaryKey"], f"{path}.primaryKey"
        )

    autoincrement = False
    if "autoincrement" in raw_column and raw_column["autoincrement"] is not None:
        autoincrement = _require_bool(
            raw_column["autoincrement"], f"{path}.autoincrement"
        )

    unique = False
    if "unique" in raw_column and raw_column["unique"] is not None:
        unique = _require_bool(raw_column["unique"], f"{path}.unique")

    check: Optional[str] = None
    if "check" in raw_column and raw_column["check"] is not None:
        check = _require_str(raw_column["check"], f"{path}.check")

    description: Optional[str] = None
    if "description" in raw_column and raw_column["description"] is not None:
        description = _require_str(
            raw_column["description"], f"{path}.description", allow_blank=True
        )

    # --- default: preserve literal-vs-SQL distinction (README §4.2) -------
    default: object = None
    default_is_sql = False
    if "default" in raw_column and raw_column["default"] is not None:
        default = raw_column["default"]
        if isinstance(default, str):
            if not default.strip():
                raise ConfigError(
                    f"{path}.default",
                    "string default must not be empty or whitespace-only",
                )
            # Per the spec a string default is either a SQL expression
            # ("datetime('now')") or a SQL literal ("'open'"); it is emitted
            # verbatim, never re-quoted.
            default_is_sql = True
        elif isinstance(default, (bool, int, float, dict, list)):
            default_is_sql = False
        else:
            raise ConfigError(
                f"{path}.default",
                "must be a literal, a string SQL expression, or null",
            )

    # --- referential actions ----------------------------------------------
    on_delete = DEFAULT_FK_ACTION
    has_on_delete = "onDelete" in raw_column and raw_column["onDelete"] is not None
    if has_on_delete:
        on_delete = normalize_fk_action(
            raw_column["onDelete"], f"{path}.onDelete"
        )
    on_update = DEFAULT_FK_ACTION
    has_on_update = "onUpdate" in raw_column and raw_column["onUpdate"] is not None
    if has_on_update:
        on_update = normalize_fk_action(
            raw_column["onUpdate"], f"{path}.onUpdate"
        )

    raw_reference: Optional[str] = None
    if "references" in raw_column and raw_column["references"] is not None:
        raw_reference = _require_str(
            raw_column["references"], f"{path}.references"
        ).strip()

    # README §4.3: onDelete/onUpdate are "optional, with references".
    if raw_reference is None:
        if has_on_delete:
            raise ConfigError(
                f"{path}.onDelete",
                "onDelete requires a 'references' declaration",
            )
        if has_on_update:
            raise ConfigError(
                f"{path}.onUpdate",
                "onUpdate requires a 'references' declaration",
            )

    # NOTE: §4.3's "auto-coerce the referencing column's type to the target's
    # type if not explicitly set" is vacuous here: §4.2 makes `type` required.

    column = Column(
        name=column_name,
        fin_type=fin_type,
        affinity=str(meta["affinity"]),
        py_type=meta["py_type"],  # type: ignore[arg-type]
        nullable=nullable,
        default=default,
        default_is_sql=default_is_sql,
        primary_key=primary_key,
        autoincrement=autoincrement,
        unique=unique,
        check=check,
        references=None,
        on_delete=on_delete,
        on_update=on_update,
        description=description,
    )
    return column, raw_reference


def _parse_index(
    raw_index: dict,
    path: str,
    *,
    table_name: str,
    columns: Dict[str, Column],
) -> Index:
    raw_columns = raw_index.get("columns")
    if not isinstance(raw_columns, list) or not raw_columns:
        raise ConfigError(f"{path}.columns", "must be a non-empty array of column names")
    index_columns: List[str] = []
    for position, raw_name in enumerate(raw_columns):
        if not isinstance(raw_name, str):
            raise ConfigError(
                f"{path}.columns.{position}", "must be a column name string"
            )
        column_name = raw_name.strip()
        if column_name not in columns:
            raise ConfigError(
                f"{path}.columns.{position}",
                f"unknown column {column_name!r} on table {table_name!r}",
            )
        if column_name in index_columns:
            raise ConfigError(
                f"{path}.columns.{position}",
                f"duplicate column {column_name!r} in index",
            )
        index_columns.append(column_name)

    unique = False
    if "unique" in raw_index and raw_index["unique"] is not None:
        unique = _require_bool(raw_index["unique"], f"{path}.unique")

    name: Optional[str] = None
    if "name" in raw_index and raw_index["name"] is not None:
        name = _require_name(raw_index["name"], f"{path}.name", "index name")
    if not name:
        name = "ix_" + table_name + "_" + "_".join(index_columns)

    return Index(
        name=name, table=table_name, columns=index_columns, unique=unique
    )


def _resolve_reference(
    raw_reference: str, tables: Dict[str, Table], path: str
) -> Tuple[str, str]:
    """Resolve ``"table"`` / ``"table.column"`` to a validated FK target."""

    value = raw_reference.strip()
    if not value:
        raise ConfigError(
            path, "must be 'table' or 'table.column'"
        )
    if "." in value:
        parts = value.split(".")
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            raise ConfigError(
                path,
                f"invalid reference {raw_reference!r}; expected 'table' or 'table.column'",
            )
        target_table_name = parts[0].strip()
        target_column_name = parts[1].strip()
    else:
        target_table_name = value
        bare = tables.get(target_table_name)
        if bare is None:
            raise ConfigError(
                path, f"references unknown table {target_table_name!r}"
            )
        if len(bare.primary_key) != 1:
            raise ConfigError(
                path,
                f"cannot resolve bare reference {raw_reference!r}: table "
                f"{target_table_name!r} must have a single-column primary key; "
                "use 'table.column' instead",
            )
        target_column_name = bare.primary_key[0]

    target_table = tables.get(target_table_name)
    if target_table is None:
        raise ConfigError(
            path, f"references unknown table {target_table_name!r}"
        )
    target_column = target_table.columns.get(target_column_name)
    if target_column is None:
        raise ConfigError(
            path,
            f"references unknown column {target_table_name}.{target_column_name}",
        )

    is_sole_pk = (
        len(target_table.primary_key) == 1
        and target_table.primary_key[0] == target_column_name
    )
    if not (target_column.unique or is_sole_pk):
        raise ConfigError(
            path,
            f"referenced column {target_table_name}.{target_column_name} must be "
            "declared unique or be the sole primary key",
        )

    return (target_table_name, target_column_name)