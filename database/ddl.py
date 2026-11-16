"""DDL emission for a validated :class:`database.schema.DatabaseSpec`.

The emitted SQL is SQLite dialect (reference.md §4).  Tables are generated in a
dependency-safe (topological) order so foreign keys always resolve regardless
of the author's declaration order; indexes and views follow (reference.md §10.1).

Every identifier is quoted with :func:`quote_ident`; no client/user input ever
reaches this module.
"""

from __future__ import annotations

import json
from typing import Dict, List

from .schema import Column, DatabaseSpec, Index, Table

__all__ = [
    "quote_ident",
    "table_ddl",
    "index_ddl",
    "view_ddl",
    "schema_ddl",
    "topological_table_order",
]


def quote_ident(name: str) -> str:
    """Quote a SQLite identifier safely (double quotes, internal quotes doubled)."""

    if not isinstance(name, str):
        raise TypeError(f"identifier must be a string, got {type(name).__name__}")
    if not name:
        raise ValueError("identifier must not be empty")
    if "\x00" in name:
        raise ValueError("identifier must not contain NUL characters")
    return '"' + name.replace('"', '""') + '"'


def _quote_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _literal(value: object) -> str:
    """Render a JSON literal as SQL (numbers, booleans, JSON text, strings)."""

    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (dict, list)):
        return _quote_string(json.dumps(value))
    return _quote_string(str(value))


def _default_clause(column: Column) -> str | None:
    """Render the DEFAULT clause, preserving expression-vs-literal semantics."""

    if column.default is None:
        return None
    if column.default_is_sql:
        # Raw SQL: parenthesized so expressions such as datetime('now') are
        # accepted by SQLite's DEFAULT grammar.
        return f"DEFAULT ({column.default})"
    return f"DEFAULT {_literal(column.default)}"


def _column_ddl(column: Column, primary_key: List[str]) -> str:
    parts = [quote_ident(column.name), column.affinity]

    is_single_pk = len(primary_key) == 1 and primary_key[0] == column.name
    is_rowid_alias = is_single_pk and column.affinity == "INTEGER"
    if is_single_pk:
        parts.append("PRIMARY KEY")
        if column.autoincrement:
            parts.append("AUTOINCREMENT")

    # A single-column INTEGER PRIMARY KEY is the rowid alias: NOT NULL is
    # implicit.  Every other non-nullable column (including TEXT PKs) gets an
    # explicit NOT NULL so NULLs cannot be stored.
    if not column.nullable and not is_rowid_alias:
        parts.append("NOT NULL")

    if column.unique:
        parts.append("UNIQUE")

    if column.check:
        parts.append(f"CHECK ({column.check})")

    default_clause = _default_clause(column)
    if default_clause:
        parts.append(default_clause)

    return "  " + " ".join(parts)


def _foreign_key_ddl(column: Column) -> str:
    assert column.references is not None
    target_table, target_column = column.references
    return (
        "  FOREIGN KEY ({child}) REFERENCES {parent} ({parent_column}) "
        "ON DELETE {on_delete} ON UPDATE {on_update}".format(
            child=quote_ident(column.name),
            parent=quote_ident(target_table),
            parent_column=quote_ident(target_column),
            on_delete=column.on_delete,
            on_update=column.on_update,
        )
    )


def topological_table_order(spec: DatabaseSpec) -> List[str]:
    """Return table names ordered so every FK target precedes its dependents.

    Stable with respect to author order.  Self-references do not create an
    ordering constraint; if a genuine cycle exists the remaining tables are
    appended in author order (SQLite tolerates forward FK references).
    """

    author_order: Dict[str, int] = {
        name: position for position, name in enumerate(spec.tables)
    }
    dependencies: Dict[str, set] = {}
    for name, table in spec.tables.items():
        targets = set()
        for column in table.columns.values():
            if column.references is None:
                continue
            target = column.references[0]
            if target != name and target in spec.tables:
                targets.add(target)
        dependencies[name] = targets

    ordered: List[str] = []
    remaining = dict(dependencies)
    while remaining:
        pending_names = set(remaining)
        ready = [
            name
            for name, targets in remaining.items()
            if not (targets & pending_names)
        ]
        if not ready:
            # Cycle: preserve author order for whatever is left.
            ordered.extend(
                name for name in spec.tables if name in remaining
            )
            break
        ready.sort(key=lambda name: author_order[name])
        for name in ready:
            ordered.append(name)
            del remaining[name]
    return ordered


def _table_sql(table: Table) -> str:
    primary_key = table.primary_key
    lines = [_column_ddl(column, primary_key) for column in table.columns.values()]

    if len(primary_key) > 1:
        lines.append(
            "  PRIMARY KEY ("
            + ", ".join(quote_ident(name) for name in primary_key)
            + ")"
        )

    for column in table.columns.values():
        if column.references is not None:
            lines.append(_foreign_key_ddl(column))

    sql = (
        f"CREATE TABLE IF NOT EXISTS {quote_ident(table.name)} (\n"
        + ",\n".join(lines)
        + "\n)"
    )
    if table.without_rowid:
        sql += " WITHOUT ROWID"
    return sql


def table_ddl(spec: DatabaseSpec) -> List[str]:
    """`CREATE TABLE` statements in dependency-safe order."""

    return [
        _table_sql(spec.tables[name]) for name in topological_table_order(spec)
    ]


def _all_indexes(spec: DatabaseSpec) -> List[Index]:
    indexes = list(spec.indexes)
    for table in spec.tables.values():
        indexes.extend(table.indexes)
    return indexes


def index_ddl(spec: DatabaseSpec) -> List[str]:
    """`CREATE INDEX` statements (global indexes first, then table-local)."""

    statements: List[str] = []
    for index in _all_indexes(spec):
        unique = "UNIQUE " if index.unique else ""
        columns = ", ".join(quote_ident(name) for name in index.columns)
        statements.append(
            f"CREATE {unique}INDEX IF NOT EXISTS {quote_ident(index.name)} "
            f"ON {quote_ident(index.table)} ({columns})"
        )
    return statements


def view_ddl(spec: DatabaseSpec) -> List[str]:
    """`CREATE VIEW` statements for author-defined read-only views (§4.5)."""

    statements: List[str] = []
    for view in spec.views.values():
        body = view.sql.strip()
        if body.endswith(";"):
            body = body[:-1].rstrip()
        statements.append(
            f"CREATE VIEW IF NOT EXISTS {quote_ident(view.name)} AS {body}"
        )
    return statements


def schema_ddl(spec: DatabaseSpec) -> List[str]:
    """The complete, ordered schema: tables, then indexes, then views (§10.1)."""

    return table_ddl(spec) + index_ddl(spec) + view_ddl(spec)