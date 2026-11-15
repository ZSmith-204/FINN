"""Database-layer tests for FINN (stdlib ``unittest``, not pytest).

Covers README §4 (schema parsing/relations), §4.6 (init on first creation),
§6.3 (serialization), §7 (SQLite error mapping), and §8.3 (generated DB.py).

Run from the repository root:

    python3 -m unittest discover -s tests -v
"""

import ast
import base64
import copy
import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from finn.database import (  # noqa: E402
    DB_INIT_CONFLICT,
    DB_INIT_CONFIG,
    DB_INIT_DB,
    DB_INIT_FILE,
    DB_INIT_UNKNOWN,
    ConfigError,
    SchemaConflictError,
    ValidationError,
    bootstrap,
    coerce,
    coerce_value,
    connect,
    db_init,
    describe_schema,
    index_ddl,
    map_sqlite_error,
    parse_database,
    quote_ident,
    render_db_module,
    schema_ddl,
    serialize_row,
    serialize_rows,
    table_ddl,
    topological_table_order,
)

# Runtime-generated, uncommitted artifacts (see DummyDatabaseTests).
FIXTURES_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures"
)

# The exact `database` block from README §12.
EXAMPLE_DATABASE = {
    "file": "finn.db",
    "tables": {
        "users": {
            "columns": {
                "id": {"type": "integer", "primaryKey": True, "autoincrement": True},
                "name": {"type": "text"},
                "email": {"type": "text", "unique": True},
                "created_at": {"type": "datetime", "default": "datetime('now')"},
            }
        },
        "orders": {
            "columns": {
                "id": {"type": "integer", "primaryKey": True, "autoincrement": True},
                "user_id": {
                    "type": "integer",
                    "references": "users.id",
                    "onDelete": "CASCADE",
                },
                "total": {"type": "real"},
                "status": {"type": "text", "default": "'open'"},
            },
            "indexes": [{"columns": ["user_id"]}],
        },
    },
}

EXAMPLE_WITH_INIT = copy.deepcopy(EXAMPLE_DATABASE)
EXAMPLE_WITH_INIT["init"] = [
    "INSERT INTO users (name, email) VALUES ('Ada', 'ada@example.com')",
    "INSERT INTO users (name, email) VALUES ('Grace', 'grace@example.com')",
    "INSERT INTO orders (user_id, total, status) VALUES (1, 10.0, 'open')",
]


# A persistent, realistic dummy database for API/GUI/CLI developers to poke at.
# It exercises every canonical type, two FK relations, and rich seed data; the
# `database` block only (no other config sections).
DUMMY_CONFIG = {
    "file": "dummy.db",
    "tables": {
        "users": {
            "columns": {
                "id": {"type": "integer", "primaryKey": True, "autoincrement": True},
                "name": {"type": "text"},
                "email": {"type": "text", "unique": True},
                "is_active": {"type": "boolean", "default": True},
                "profile": {"type": "json", "nullable": True},
                "created_at": {"type": "datetime", "default": "datetime('now')"},
            }
        },
        "posts": {
            "columns": {
                "id": {"type": "integer", "primaryKey": True, "autoincrement": True},
                "user_id": {
                    "type": "integer",
                    "references": "users.id",
                    "onDelete": "CASCADE",
                },
                "title": {"type": "text"},
                "body": {"type": "text"},
                "tags": {"type": "json", "nullable": True},
                "published": {"type": "boolean", "default": False},
                "published_at": {"type": "datetime", "nullable": True},
            }
        },
        "attachments": {
            "columns": {
                "id": {"type": "integer", "primaryKey": True, "autoincrement": True},
                "post_id": {
                    "type": "integer",
                    "references": "posts.id",
                    "onDelete": "CASCADE",
                },
                "label": {"type": "text"},
                "data": {"type": "blob", "nullable": True},
            }
        },
    },
    "init": [
        "INSERT INTO users (name, email, is_active, profile, created_at) "
        "VALUES ('Ada Lovelace', 'ada@example.com', 1, "
        "'{\"role\": \"admin\", \"age\": 36}', '2026-01-15T09:00:00Z')",
        "INSERT INTO users (name, email, is_active, profile, created_at) "
        "VALUES ('Grace Hopper', 'grace@example.com', 1, "
        "'{\"role\": \"author\", \"age\": 85}', '2026-02-03T12:30:00Z')",
        "INSERT INTO users (name, email, is_active) "
        "VALUES ('Alan Turing', 'alan@example.com', 0)",
        "INSERT INTO posts (user_id, title, body, tags, published, published_at) "
        "VALUES (1, 'Hello world', 'First post', '[\"intro\",\"meta\"]', 1, "
        "'2026-01-16T08:00:00Z')",
        "INSERT INTO posts (user_id, title, body, published) "
        "VALUES (2, 'Draft notes', 'Still writing', 0)",
        "INSERT INTO posts (user_id, title, body, tags, published) "
        "VALUES (1, 'Second post', 'Another body', '[\"followup\"]', 1)",
        "INSERT INTO attachments (post_id, label, data) VALUES "
        "(1, 'cover.txt', X'68656C6C6F'), (1, 'notes.txt', NULL)",
    ],
}


def example():
    return copy.deepcopy(EXAMPLE_DATABASE)


class ParseDatabaseTests(unittest.TestCase):
    def test_readme_example_schema(self):
        spec = parse_database(example())

        self.assertEqual(spec.file, "finn.db")
        self.assertEqual(list(spec.tables), ["users", "orders"])
        self.assertEqual(spec.indexes, [])
        self.assertEqual(spec.views, {})
        self.assertEqual(spec.init, [])

        users = spec.tables["users"]
        self.assertEqual(users.primary_key, ["id"])
        self.assertEqual(list(users.columns), ["id", "name", "email", "created_at"])

        user_id = users.columns["id"]
        self.assertEqual(user_id.fin_type, "integer")
        self.assertEqual(user_id.affinity, "INTEGER")
        self.assertIs(user_id.py_type, int)
        self.assertTrue(user_id.primary_key)
        self.assertTrue(user_id.autoincrement)
        self.assertFalse(user_id.nullable)

        email = users.columns["email"]
        self.assertTrue(email.unique)
        self.assertFalse(email.nullable)
        self.assertEqual(email.affinity, "TEXT")

        created_at = users.columns["created_at"]
        self.assertEqual(created_at.fin_type, "datetime")
        self.assertEqual(created_at.affinity, "TEXT")
        self.assertEqual(created_at.default, "datetime('now')")
        self.assertTrue(created_at.default_is_sql)

        orders = spec.tables["orders"]
        self.assertEqual(orders.primary_key, ["id"])
        user_fk = orders.columns["user_id"]
        self.assertEqual(user_fk.references, ("users", "id"))
        self.assertEqual(user_fk.on_delete, "CASCADE")
        self.assertEqual(user_fk.on_update, "NO ACTION")
        self.assertEqual(user_fk.affinity, "INTEGER")

        total = orders.columns["total"]
        self.assertEqual(total.fin_type, "real")
        self.assertEqual(total.affinity, "REAL")
        self.assertIs(total.py_type, float)
        self.assertFalse(total.nullable)

        status = orders.columns["status"]
        self.assertEqual(status.default, "'open'")
        self.assertTrue(status.default_is_sql)

        self.assertEqual(len(orders.indexes), 1)
        index = orders.indexes[0]
        self.assertEqual(index.name, "ix_orders_user_id")
        self.assertEqual(index.table, "orders")
        self.assertEqual(index.columns, ["user_id"])
        self.assertFalse(index.unique)

    def test_type_aliases_and_affinities(self):
        spec = parse_database(
            {
                "tables": {
                    "things": {
                        "columns": {
                            "id": {"type": "INT", "primaryKey": True},
                            "ratio": {"type": "double"},
                            "label": {"type": "varchar"},
                            "raw": {"type": "bytes"},
                            "active": {"type": "boolean"},
                            "payload": {"type": "json"},
                            "when": {"type": "datetime"},
                        }
                    }
                }
            }
        )
        columns = spec.tables["things"].columns
        self.assertEqual(columns["id"].fin_type, "integer")
        self.assertEqual(columns["ratio"].fin_type, "real")
        self.assertEqual(columns["label"].fin_type, "text")
        self.assertEqual(columns["raw"].fin_type, "blob")
        self.assertEqual(columns["active"].fin_type, "boolean")
        self.assertEqual(columns["active"].affinity, "INTEGER")
        self.assertIs(columns["active"].py_type, bool)
        self.assertEqual(columns["payload"].fin_type, "json")
        self.assertEqual(columns["payload"].affinity, "TEXT")
        self.assertEqual(columns["when"].fin_type, "datetime")
        self.assertEqual(columns["when"].affinity, "TEXT")

    def test_unknown_type_rejected_with_path(self):
        config = example()
        config["tables"]["users"]["columns"]["name"]["type"] = "uuid"
        with self.assertRaises(ConfigError) as caught:
            parse_database(config)
        self.assertEqual(
            caught.exception.path, "database.tables.users.columns.name.type"
        )
        self.assertIn("uuid", caught.exception.message)

    def test_missing_type_rejected_with_path(self):
        config = example()
        del config["tables"]["users"]["columns"]["name"]["type"]
        with self.assertRaises(ConfigError) as caught:
            parse_database(config)
        self.assertEqual(
            caught.exception.path, "database.tables.users.columns.name.type"
        )

    def test_bad_foreign_key_target_table(self):
        config = example()
        config["tables"]["orders"]["columns"]["user_id"]["references"] = "ghosts.id"
        with self.assertRaises(ConfigError) as caught:
            parse_database(config)
        self.assertEqual(
            caught.exception.path,
            "database.tables.orders.columns.user_id.references",
        )
        self.assertIn("ghosts", caught.exception.message)

    def test_foreign_key_to_non_unique_column(self):
        config = example()
        # users.name is neither unique nor the sole primary key.
        config["tables"]["orders"]["columns"]["user_id"]["references"] = "users.name"
        with self.assertRaises(ConfigError) as caught:
            parse_database(config)
        self.assertEqual(
            caught.exception.path,
            "database.tables.orders.columns.user_id.references",
        )

    def test_foreign_key_to_unique_column_allowed(self):
        config = example()
        config["tables"]["orders"]["columns"]["user_id"]["references"] = "users.email"
        spec = parse_database(config)
        self.assertEqual(
            spec.tables["orders"].columns["user_id"].references,
            ("users", "email"),
        )

    def test_bare_reference_resolves_to_primary_key(self):
        config = example()
        config["tables"]["orders"]["columns"]["user_id"]["references"] = "users"
        spec = parse_database(config)
        self.assertEqual(
            spec.tables["orders"].columns["user_id"].references,
            ("users", "id"),
        )

    def test_bare_reference_requires_single_column_pk(self):
        config = {
            "tables": {
                "composite": {
                    "columns": {
                        "a": {"type": "integer"},
                        "b": {"type": "integer"},
                    },
                    "primaryKey": ["a", "b"],
                },
                "child": {
                    "columns": {
                        "id": {"type": "integer", "primaryKey": True},
                        "ref": {"type": "integer", "references": "composite"},
                    }
                },
            }
        }
        with self.assertRaises(ConfigError) as caught:
            parse_database(config)
        self.assertEqual(
            caught.exception.path,
            "database.tables.child.columns.ref.references",
        )

    def test_missing_primary_key_column(self):
        config = example()
        config["tables"]["users"]["primaryKey"] = ["uid"]
        with self.assertRaises(ConfigError) as caught:
            parse_database(config)
        self.assertEqual(
            caught.exception.path, "database.tables.users.primaryKey"
        )

    def test_autoincrement_on_non_primary_key_column_rejected(self):
        config = example()
        config["tables"]["users"]["columns"]["name"]["autoincrement"] = True
        with self.assertRaises(ConfigError) as caught:
            parse_database(config)
        self.assertEqual(
            caught.exception.path,
            "database.tables.users.columns.name.autoincrement",
        )
        self.assertIn("primary key", caught.exception.message)

    def test_table_level_primary_key_declaration(self):
        spec = parse_database(
            {
                "tables": {
                    "t": {
                        "columns": {
                            "id": {"type": "integer", "autoincrement": True},
                            "name": {"type": "text"},
                        },
                        "primaryKey": ["id"],
                    }
                }
            }
        )
        self.assertEqual(spec.tables["t"].primary_key, ["id"])
        self.assertTrue(spec.tables["t"].columns["id"].autoincrement)
        self.assertIn("PRIMARY KEY AUTOINCREMENT", table_ddl(spec)[0])

    def test_unknown_keys_rejected(self):
        config = example()
        config["tables"]["users"]["columnz"] = {}
        with self.assertRaises(ConfigError) as caught:
            parse_database(config)
        self.assertEqual(caught.exception.path, "database.tables.users.columnz")

    def test_empty_columns_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            parse_database({"tables": {"t": {"columns": {}}}})
        self.assertEqual(caught.exception.path, "database.tables.t.columns")

    def test_referential_action_normalization(self):
        config = example()
        column = config["tables"]["orders"]["columns"]["user_id"]
        column["onDelete"] = "  set   null "
        column["onUpdate"] = "restrict"
        spec = parse_database(config)
        resolved = spec.tables["orders"].columns["user_id"]
        self.assertEqual(resolved.on_delete, "SET NULL")
        self.assertEqual(resolved.on_update, "RESTRICT")

        column["onDelete"] = "EXPLODE"
        with self.assertRaises(ConfigError) as caught:
            parse_database(config)
        self.assertEqual(
            caught.exception.path,
            "database.tables.orders.columns.user_id.onDelete",
        )

    def test_index_columns_must_exist(self):
        config = example()
        config["tables"]["orders"]["indexes"] = [{"columns": ["nope"]}]
        with self.assertRaises(ConfigError) as caught:
            parse_database(config)
        self.assertEqual(
            caught.exception.path,
            "database.tables.orders.indexes.0.columns.0",
        )

    def test_global_index_requires_table_and_valid_columns(self):
        config = example()
        config["indexes"] = [{"table": "orders", "columns": ["status"], "name": "idx_status"}]
        spec = parse_database(config)
        self.assertEqual(len(spec.indexes), 1)
        self.assertEqual(spec.indexes[0].name, "idx_status")
        self.assertEqual(spec.indexes[0].table, "orders")

        config["indexes"] = [{"columns": ["status"]}]
        with self.assertRaises(ConfigError) as caught:
            parse_database(config)
        self.assertEqual(caught.exception.path, "database.indexes.0.table")

    def test_init_and_views_recorded(self):
        config = example()
        config["views"] = {"open_orders": {"sql": "SELECT * FROM orders"}}
        config["init"] = ["INSERT INTO users (name, email) VALUES ('A', 'a@x')"]
        spec = parse_database(config)
        self.assertEqual(spec.views["open_orders"].sql, "SELECT * FROM orders")
        self.assertEqual(len(spec.init), 1)


class DdlTests(unittest.TestCase):
    def test_schema_ddl_orders_tables_topologically(self):
        # Declare `orders` before its parent `users`: DDL must still be safe.
        config = example()
        config["tables"] = {
            "orders": config["tables"]["orders"],
            "users": config["tables"]["users"],
        }
        spec = parse_database(config)
        statements = table_ddl(spec)
        self.assertEqual(len(statements), 2)
        self.assertIn('CREATE TABLE IF NOT EXISTS "users"', statements[0])
        self.assertIn('CREATE TABLE IF NOT EXISTS "orders"', statements[1])

    def test_schema_ddl_contains_constraints(self):
        spec = parse_database(example())
        all_sql = "\n".join(schema_ddl(spec))
        self.assertIn("FOREIGN KEY (\"user_id\") REFERENCES \"users\" (\"id\")", all_sql)
        self.assertIn("ON DELETE CASCADE", all_sql)
        self.assertIn("ON UPDATE NO ACTION", all_sql)
        self.assertIn("PRIMARY KEY AUTOINCREMENT", all_sql)
        self.assertIn("UNIQUE", all_sql)
        self.assertIn("DEFAULT (datetime('now'))", all_sql)
        self.assertIn("DEFAULT ('open')", all_sql)
        self.assertIn('CREATE INDEX IF NOT EXISTS "ix_orders_user_id"', all_sql)

    def test_views_emitted_after_indexes(self):
        config = example()
        config["views"] = {"open_orders": {"sql": "SELECT * FROM orders;"}}
        spec = parse_database(config)
        statements = schema_ddl(spec)
        self.assertTrue(statements[0].startswith("CREATE TABLE"))
        self.assertTrue(statements[-1].startswith("CREATE VIEW"))
        self.assertIn(" SELECT * FROM orders", statements[-1])


class BootstrapTests(unittest.TestCase):
    def test_foreign_keys_unique_and_tables(self):
        spec = parse_database(example())
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "finn.db")
            self.assertTrue(bootstrap(path, spec))

            connection = connect(path)
            try:
                tables = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertIn("users", tables)
                self.assertIn("orders", tables)

                indexes = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )
                }
                self.assertIn("ix_orders_user_id", indexes)

                foreign_keys = connection.execute(
                    "PRAGMA foreign_key_list(orders)"
                ).fetchall()
                self.assertEqual(len(foreign_keys), 1)
                foreign_key = foreign_keys[0]
                self.assertEqual(foreign_key["table"], "users")
                self.assertEqual(foreign_key["from"], "user_id")
                self.assertEqual(foreign_key["to"], "id")
                self.assertEqual(foreign_key["on_delete"], "CASCADE")

                # FK enforcement (PRAGMA foreign_keys=ON).
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO orders (user_id, total, status) "
                        "VALUES (999, 10.0, 'open')"
                    )
                connection.rollback()

                connection.execute(
                    "INSERT INTO users (name, email) VALUES ('Ada', 'ada@example.com')"
                )
                # UNIQUE email constraint.
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO users (name, email) "
                        "VALUES ('Imposter', 'ada@example.com')"
                    )
                connection.rollback()

                # A valid order now succeeds.
                cursor = connection.execute(
                    "INSERT INTO users (name, email) VALUES ('Bob', 'bob@example.com')"
                )
                bob_id = cursor.lastrowid
                connection.execute(
                    "INSERT INTO orders (user_id, total, status) VALUES (?, 4.5, 'open')",
                    (bob_id,),
                )
                connection.commit()
                count = connection.execute(
                    "SELECT COUNT(*) FROM orders"
                ).fetchone()[0]
                self.assertEqual(count, 1)
            finally:
                connection.close()

    def test_init_runs_only_on_first_creation(self):
        spec = parse_database(EXAMPLE_WITH_INIT)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "finn.db")

            self.assertTrue(bootstrap(path, spec))

            connection = connect(path)
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                    2,
                )
            finally:
                connection.close()

            # Second bootstrap: existing DB, init must NOT run again.
            self.assertFalse(bootstrap(path, spec))
            connection = connect(path)
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                    2,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
                    1,
                )
            finally:
                connection.close()

    def test_views_and_global_indexes_created(self):
        config = example()
        config["indexes"] = [
            {"table": "orders", "columns": ["status"], "name": "idx_orders_status"}
        ]
        config["views"] = {
            "open_orders": {"sql": "SELECT id, user_id FROM orders WHERE status='open'"}
        }
        spec = parse_database(config)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "finn.db")
            bootstrap(path, spec)
            connection = connect(path)
            try:
                objects = {
                    (row["type"], row["name"])
                    for row in connection.execute(
                        "SELECT type, name FROM sqlite_master"
                    )
                }
                self.assertIn(("index", "idx_orders_status"), objects)
                self.assertIn(("view", "open_orders"), objects)
                rows = connection.execute(
                    "SELECT COUNT(*) FROM open_orders"
                ).fetchone()[0]
                self.assertEqual(rows, 0)
            finally:
                connection.close()

    def test_connect_create_false_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "absent.db")
            with self.assertRaises(FileNotFoundError):
                connect(missing, create=False)


class DbInitTests(unittest.TestCase):
    def test_fresh_database_returns_success(self):
        spec = parse_database(example())
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "finn.db")
            self.assertEqual(db_init(path, spec), (True, None))

            connection = connect(path, create=False)
            try:
                tables = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertIn("users", tables)
                self.assertIn("orders", tables)
            finally:
                connection.close()

    def test_reinit_compatible_database_returns_success(self):
        spec = parse_database(example())
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "finn.db")
            self.assertEqual(db_init(path, spec), (True, None))
            self.assertEqual(db_init(path, spec), (True, None))

    def test_schema_conflict_returns_code_without_raising(self):
        spec = parse_database(example())
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "finn.db")
            self.assertTrue(bootstrap(path, spec))

            config = example()
            config["tables"]["users"]["columns"]["name"]["type"] = "integer"
            changed = parse_database(config)

            # Must not raise: db_init swallows SchemaConflictError.
            result = db_init(path, changed)
            self.assertEqual(result, (False, DB_INIT_CONFLICT))
            self.assertEqual(result[1], "SCHEMA_CONFLICT")

    def test_directory_path_returns_db_error_without_raising(self):
        spec = parse_database(example())
        directory = tempfile.mkdtemp()
        try:
            result = db_init(directory, spec)
            self.assertEqual(result, (False, DB_INIT_DB))
            self.assertEqual(result[1], "DB_ERROR")
        finally:
            os.rmdir(directory)


class SerializationTests(unittest.TestCase):
    def setUp(self):
        self.column_types = {
            "id": "integer",
            "active": "boolean",
            "payload": "json",
            "note": "text",
            "data": "blob",
            "ratio": "real",
            "created_at": "datetime",
        }
        self.columns = list(self.column_types) + ["surprise"]
        self.rows = [
            (
                1,
                1,
                '{"a": [1, 2]}',
                None,
                b"\x00\xff",
                2.5,
                "2024-01-02T03:04:05Z",
                "raw-value",
            ),
            (
                2,
                0,
                '"plain"',
                "hello",
                b"abc",
                0.0,
                "2024-01-03T00:00:00Z",
                "other",
            ),
        ]

    def test_serialize_rows(self):
        serialized = serialize_rows(
            self.rows, self.column_types, columns=self.columns
        )
        self.assertEqual(len(serialized), 2)

        first = serialized[0]
        self.assertEqual(first["id"], 1)
        self.assertIs(first["active"], True)
        self.assertEqual(first["payload"], {"a": [1, 2]})
        self.assertIsNone(first["note"])
        self.assertEqual(
            first["data"],
            "$base64:" + base64.b64encode(b"\x00\xff").decode("ascii"),
        )
        self.assertEqual(first["ratio"], 2.5)
        self.assertEqual(first["created_at"], "2024-01-02T03:04:05Z")
        # Unknown column type -> stored value passes through.
        self.assertEqual(first["surprise"], "raw-value")

        second = serialized[1]
        self.assertIs(second["active"], False)
        self.assertEqual(second["payload"], "plain")
        self.assertEqual(second["note"], "hello")

    def test_custom_blob_format(self):
        serialized = serialize_rows(
            [(b"abc",)],
            {"data": "blob"},
            columns=["data"],
            blob_format="base64:{}",
        )
        self.assertEqual(
            serialized[0]["data"],
            "base64:" + base64.b64encode(b"abc").decode("ascii"),
        )

    def test_sqlite_row_serialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "rows.db")
            connection = connect(path)
            try:
                connection.execute(
                    "CREATE TABLE t (id INTEGER, active BOOLEAN, payload TEXT)"
                )
                connection.execute(
                    "INSERT INTO t (id, active, payload) VALUES (7, 0, '{\"x\": 1}')"
                )
                connection.commit()
                rows = connection.execute("SELECT * FROM t").fetchall()
            finally:
                connection.close()

        serialized = serialize_rows(
            rows, {"id": "integer", "active": "boolean", "payload": "json"}
        )
        self.assertEqual(
            serialized, [{"id": 7, "active": False, "payload": {"x": 1}}]
        )


class ErrorMappingTests(unittest.TestCase):
    def test_integrity_error_maps_to_conflict(self):
        exc = sqlite3.IntegrityError("UNIQUE constraint failed: users.email")
        mapped = map_sqlite_error(exc)
        self.assertIsNotNone(mapped)
        self.assertEqual(mapped["code"], "CONFLICT")
        self.assertEqual(mapped["http_status"], 409)
        self.assertEqual(mapped["detail"], "UNIQUE constraint failed: users.email")
        self.assertIn("unique", mapped["message"].lower())

    def test_foreign_key_and_check_mapping(self):
        fk = map_sqlite_error(
            sqlite3.IntegrityError("FOREIGN KEY constraint failed")
        )
        self.assertEqual(fk["code"], "CONFLICT")
        self.assertIn("foreign key", fk["message"].lower())

        check = map_sqlite_error(
            sqlite3.IntegrityError("CHECK constraint failed: length(name) > 0")
        )
        self.assertEqual(check["code"], "CONFLICT")
        self.assertIn("check", check["message"].lower())

    def test_other_sqlite_errors_return_none(self):
        self.assertIsNone(map_sqlite_error(sqlite3.OperationalError("no such table: x")))
        self.assertIsNone(map_sqlite_error(sqlite3.DatabaseError("disk I/O error")))


class CodegenTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("DB", None)

    def test_generated_module_imports_and_queries(self):
        spec = parse_database(EXAMPLE_WITH_INIT)
        source = render_db_module(spec)

        # Compiles as Python and stays inside the data layer.
        ast.parse(source)
        self.assertNotIn("import finn", source)
        self.assertNotIn("from finn", source)
        lowered = source.lower()
        for forbidden in ("fastapi", "starlette", "httpx", "uvicorn"):
            self.assertNotIn(forbidden, lowered)

        with tempfile.TemporaryDirectory() as tmp:
            import pathlib

            db_path = os.path.join(tmp, "finn.db")
            self.assertTrue(bootstrap(db_path, spec))

            module_path = os.path.join(tmp, "DB.py")
            with open(module_path, "w", encoding="utf-8") as handle:
                handle.write(source)

            sys.modules.pop("DB", None)
            sys.path.insert(0, tmp)
            try:
                module = importlib.import_module("DB")
            finally:
                sys.path.remove(tmp)

            try:
                self.assertEqual(
                    module.DB_PATH, pathlib.Path(tmp).resolve() / "finn.db"
                )

                result = module.query(
                    "SELECT id, name FROM users ORDER BY id"
                )
                self.assertEqual(
                    result["rows"],
                    [
                        {"id": 1, "name": "Ada"},
                        {"id": 2, "name": "Grace"},
                    ],
                )
                self.assertEqual(result["affected"], 0)
                self.assertIsNone(result["rowid"])

                insertion = module.query(
                    "INSERT INTO users (name, email) VALUES (:name, :email)",
                    {"name": "Lin", "email": "lin@example.com"},
                )
                self.assertEqual(insertion["rows"], [])
                self.assertEqual(insertion["affected"], 1)
                self.assertEqual(insertion["rowid"], 3)

                # Row-returning mutations are committed, not discarded.
                returning = module.query(
                    "INSERT INTO users (name, email) VALUES (:name, :email) "
                    "RETURNING id",
                    {"name": "Kai", "email": "kai@example.com"},
                )
                self.assertEqual(returning["rows"], [{"id": 4}])
                persisted = module.query("SELECT COUNT(*) AS n FROM users")
                self.assertEqual(persisted["rows"], [{"n": 4}])

                # Generated serialization helpers behave like the package ones.
                self.assertEqual(
                    module.serialize_rows(
                        [(0, '{"x": 1}', b"abc")],
                        {"active": "boolean", "payload": "json", "data": "blob"},
                        columns=["active", "payload", "data"],
                    ),
                    [
                        {
                            "active": False,
                            "payload": {"x": 1},
                            "data": "$base64:" + base64.b64encode(b"abc").decode("ascii"),
                        }
                    ],
                )

                mapped = module.map_sqlite_error(
                    sqlite3.IntegrityError("UNIQUE constraint failed: users.email")
                )
                self.assertEqual(mapped["http_status"], 409)

                # FK enforcement through the generated connection factory.
                with self.assertRaises(sqlite3.IntegrityError):
                    module.query(
                        "INSERT INTO orders (user_id, total, status) "
                        "VALUES (:user_id, :total, :status)",
                        {"user_id": 999, "total": 1.0, "status": "open"},
                    )
            finally:
                sys.modules.pop("DB", None)


class NotNullConstraintTests(unittest.TestCase):
    def test_text_primary_key_is_not_null(self):
        spec = parse_database(
            {
                "tables": {
                    "t": {
                        "columns": {
                            "id": {"type": "text", "primaryKey": True},
                        }
                    }
                }
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "text_pk.db")
            self.assertTrue(bootstrap(path, spec))
            connection = connect(path)
            try:
                info = {
                    row["name"]: row
                    for row in connection.execute('PRAGMA table_info("t")')
                }
            finally:
                connection.close()

        self.assertEqual(info["id"]["notnull"], 1)
        self.assertEqual(info["id"]["pk"], 1)

    def test_integer_primary_key_rowid_alias_bootstraps(self):
        # INTEGER PRIMARY KEY is the rowid alias; SQLite keeps its implicit
        # nullability, so this test only asserts a clean bootstrap.
        spec = parse_database(
            {
                "tables": {
                    "t": {
                        "columns": {
                            "id": {"type": "integer", "primaryKey": True},
                        }
                    }
                }
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(bootstrap(os.path.join(tmp, "rowid.db"), spec))

    def test_plain_column_emits_not_null(self):
        spec = parse_database(
            {
                "tables": {
                    "t": {
                        "columns": {
                            "id": {"type": "integer", "primaryKey": True},
                            "label": {"type": "text"},
                        }
                    }
                }
            }
        )
        self.assertIn('"label" TEXT NOT NULL', table_ddl(spec)[0])


class IdentifierSafetyTests(unittest.TestCase):
    @staticmethod
    def _table_names(path):
        connection = connect(path)
        try:
            return {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            connection.close()

    def test_reserved_word_table_bootstraps(self):
        spec = parse_database(
            {
                "tables": {
                    "order": {
                        "columns": {"id": {"type": "integer", "primaryKey": True}}
                    }
                }
            }
        )
        self.assertIn(
            'CREATE TABLE IF NOT EXISTS "order"', table_ddl(spec)[0]
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "reserved.db")
            self.assertTrue(bootstrap(path, spec))
            self.assertIn("order", self._table_names(path))

    def test_column_name_with_embedded_double_quote_bootstraps(self):
        spec = parse_database(
            {
                "tables": {
                    "t": {"columns": {'say "hi"': {"type": "text"}}}
                }
            }
        )
        self.assertIn('"say ""hi""" TEXT NOT NULL', table_ddl(spec)[0])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "embedded_quote.db")
            self.assertTrue(bootstrap(path, spec))
            connection = connect(path)
            try:
                names = [
                    row["name"]
                    for row in connection.execute('PRAGMA table_info("t")')
                ]
            finally:
                connection.close()
        self.assertEqual(names, ['say "hi"'])

    def test_injection_shaped_table_name_is_quoted(self):
        injected = 'x"); DROP TABLE users; --'
        spec = parse_database(
            {
                "tables": {
                    injected: {
                        "columns": {"id": {"type": "integer", "primaryKey": True}}
                    },
                    "users": {
                        "columns": {"id": {"type": "integer", "primaryKey": True}}
                    },
                }
            }
        )
        ddl = "\n".join(table_ddl(spec))
        self.assertIn(
            'CREATE TABLE IF NOT EXISTS "x""); DROP TABLE users; --"', ddl
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "injection.db")
            self.assertTrue(bootstrap(path, spec))
            names = self._table_names(path)
        self.assertIn(injected, names)
        self.assertIn("users", names)

    def test_quote_ident_handles_spaces_quotes_and_reserved_words(self):
        self.assertEqual(quote_ident("first name"), '"first name"')
        self.assertEqual(quote_ident('say "hi"'), '"say ""hi"""')
        self.assertEqual(quote_ident("order"), '"order"')
        self.assertEqual(quote_ident('a"b"c'), '"a""b""c"')


class WithoutRowidTests(unittest.TestCase):
    def test_without_rowid_bootstraps(self):
        spec = parse_database(
            {
                "tables": {
                    "t": {
                        "columns": {
                            "id": {"type": "integer", "primaryKey": True},
                        },
                        "withoutRowid": True,
                    }
                }
            }
        )
        self.assertIn("WITHOUT ROWID", table_ddl(spec)[0])
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(bootstrap(os.path.join(tmp, "without_rowid.db"), spec))

    def test_without_rowid_requires_primary_key(self):
        with self.assertRaises(ConfigError) as caught:
            parse_database(
                {
                    "tables": {
                        "t": {
                            "columns": {"id": {"type": "integer"}},
                            "withoutRowid": True,
                        }
                    }
                }
            )
        self.assertEqual(
            caught.exception.path, "database.tables.t.withoutRowid"
        )
        self.assertIn("primary key", caught.exception.message)


class AutoincrementGuardTests(unittest.TestCase):
    def test_text_primary_key_autoincrement_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            parse_database(
                {
                    "tables": {
                        "t": {
                            "columns": {
                                "id": {
                                    "type": "text",
                                    "primaryKey": True,
                                    "autoincrement": True,
                                }
                            }
                        }
                    }
                }
            )
        self.assertEqual(
            caught.exception.path, "database.tables.t.columns.id.autoincrement"
        )
        self.assertIn("integer", caught.exception.message)

    def test_composite_primary_key_autoincrement_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            parse_database(
                {
                    "tables": {
                        "t": {
                            "columns": {
                                "a": {"type": "integer", "autoincrement": True},
                                "b": {"type": "integer"},
                            },
                            "primaryKey": ["a", "b"],
                        }
                    }
                }
            )
        self.assertEqual(
            caught.exception.path, "database.tables.t.columns.a.autoincrement"
        )
        self.assertIn("sole primary key", caught.exception.message)


class CompositePrimaryKeyTests(unittest.TestCase):
    def test_composite_primary_key_emits_table_constraint(self):
        spec = parse_database(
            {
                "tables": {
                    "t": {
                        "columns": {
                            "a": {"type": "integer"},
                            "b": {"type": "integer"},
                        },
                        "primaryKey": ["a", "b"],
                    }
                }
            }
        )
        ddl = table_ddl(spec)[0]
        self.assertIn('PRIMARY KEY ("a", "b")', ddl)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(bootstrap(os.path.join(tmp, "composite.db"), spec))


class IndexTests(unittest.TestCase):
    def test_unique_index_ddl(self):
        spec = parse_database(
            {
                "tables": {
                    "t": {
                        "columns": {
                            "a": {"type": "text"},
                            "b": {"type": "text"},
                        },
                        "indexes": [{"columns": ["a"], "unique": True}],
                    }
                }
            }
        )
        statement = index_ddl(spec)[0]
        self.assertIn("CREATE UNIQUE INDEX", statement)
        self.assertIn('"ix_t_a"', statement)

    def test_two_column_index_auto_name(self):
        spec = parse_database(
            {
                "tables": {
                    "t": {
                        "columns": {
                            "a": {"type": "text"},
                            "b": {"type": "text"},
                        },
                        "indexes": [{"columns": ["a", "b"]}],
                    }
                }
            }
        )
        self.assertEqual(spec.tables["t"].indexes[0].name, "ix_t_a_b")
        self.assertIn(
            'CREATE INDEX IF NOT EXISTS "ix_t_a_b" ON "t" ("a", "b")',
            index_ddl(spec)[0],
        )

    def test_duplicate_explicit_index_name_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            parse_database(
                {
                    "tables": {
                        "t": {
                            "columns": {
                                "a": {"type": "text"},
                                "b": {"type": "text"},
                            },
                            "indexes": [
                                {"columns": ["a"], "name": "idx_dup"},
                                {"columns": ["b"], "name": "idx_dup"},
                            ],
                        }
                    }
                }
            )
        self.assertEqual(
            caught.exception.path, "database.tables.t.indexes.1.name"
        )
        self.assertIn("duplicate index name", caught.exception.message)


class TopologicalOrderTests(unittest.TestCase):
    def test_reverse_declared_chain_bootstraps(self):
        spec = parse_database(
            {
                "tables": {
                    "c": {
                        "columns": {
                            "id": {"type": "integer", "primaryKey": True},
                            "b_id": {"type": "integer", "references": "b.id"},
                        }
                    },
                    "b": {
                        "columns": {
                            "id": {"type": "integer", "primaryKey": True},
                            "a_id": {"type": "integer", "references": "a.id"},
                        }
                    },
                    "a": {
                        "columns": {
                            "id": {"type": "integer", "primaryKey": True},
                        }
                    },
                }
            }
        )
        self.assertEqual(topological_table_order(spec), ["a", "b", "c"])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "chain.db")
            self.assertTrue(bootstrap(path, spec))
            connection = connect(path)
            try:
                tables = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            finally:
                connection.close()
        self.assertEqual(tables, {"a", "b", "c"})

    def test_self_referencing_table_bootstraps(self):
        spec = parse_database(
            {
                "tables": {
                    "node": {
                        "columns": {
                            "id": {"type": "integer", "primaryKey": True},
                            "parent_id": {
                                "type": "integer",
                                "nullable": True,
                                "references": "node.id",
                            },
                        }
                    }
                }
            }
        )
        self.assertEqual(topological_table_order(spec), ["node"])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "self_ref.db")
            self.assertTrue(bootstrap(path, spec))
            connection = connect(path)
            try:
                foreign_keys = connection.execute(
                    'PRAGMA foreign_key_list("node")'
                ).fetchall()
            finally:
                connection.close()
        self.assertEqual(len(foreign_keys), 1)
        self.assertEqual(foreign_keys[0]["table"], "node")


class DefaultValueTests(unittest.TestCase):
    def test_non_string_default_rendered_as_literal(self):
        spec = parse_database(
            {
                "tables": {
                    "t": {"columns": {"n": {"type": "integer", "default": 5}}}
                }
            }
        )
        self.assertIn("DEFAULT 5", table_ddl(spec)[0])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "default_literal.db")
            self.assertTrue(bootstrap(path, spec))
            connection = connect(path)
            try:
                connection.execute("INSERT INTO t DEFAULT VALUES")
                stored_default = connection.execute(
                    'PRAGMA table_info("t")'
                ).fetchone()["dflt_value"]
                value = connection.execute("SELECT n FROM t").fetchone()[0]
            finally:
                connection.close()
        self.assertEqual(stored_default, "5")
        self.assertEqual(value, 5)

    def test_empty_string_default_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            parse_database(
                {
                    "tables": {
                        "t": {
                            "columns": {
                                "id": {"type": "integer"},
                                "note": {"type": "text", "default": ""},
                            }
                        }
                    }
                }
            )
        self.assertEqual(
            caught.exception.path, "database.tables.t.columns.note.default"
        )
        self.assertIn("empty", caught.exception.message)

    def test_whitespace_string_default_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            parse_database(
                {
                    "tables": {
                        "t": {
                            "columns": {
                                "id": {"type": "integer"},
                                "note": {"type": "text", "default": "   "},
                            }
                        }
                    }
                }
            )
        self.assertEqual(
            caught.exception.path, "database.tables.t.columns.note.default"
        )
        self.assertIn("empty", caught.exception.message)


class DatabaseFileValidationTests(unittest.TestCase):
    @staticmethod
    def _config(file_name):
        return {
            "file": file_name,
            "tables": {"t": {"columns": {"id": {"type": "integer"}}}},
        }

    def test_path_separator_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            parse_database(self._config("x/y.db"))
        self.assertEqual(caught.exception.path, "database.file")
        self.assertIn("path separators", caught.exception.message)

    def test_parent_directory_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            parse_database(self._config(".."))
        self.assertEqual(caught.exception.path, "database.file")

    def test_nul_byte_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            parse_database(self._config("bad\x00name.db"))
        self.assertEqual(caught.exception.path, "database.file")
        self.assertIn("NUL", caught.exception.message)


class SchemaCompatibilityTests(unittest.TestCase):
    @staticmethod
    def _index_conflict_base():
        return {
            "tables": {
                "t": {
                    "columns": {
                        "id": {
                            "type": "integer",
                            "primaryKey": True,
                            "autoincrement": True,
                        },
                        "a": {"type": "text", "nullable": True},
                    }
                }
            }
        }

    @staticmethod
    def _index_conflict_changed():
        config = SchemaCompatibilityTests._index_conflict_base()
        # ``b`` exists in the config but not on disk; the index over it makes
        # SQLite raise OperationalError if DDL runs before verification.
        config["tables"]["t"]["columns"]["b"] = {
            "type": "text",
            "nullable": True,
        }
        config["indexes"] = [
            {"table": "t", "columns": ["b"], "name": "idx_t_b"}
        ]
        return config

    @staticmethod
    def _master_snapshot(path):
        connection = connect(path)
        try:
            return [
                (row["type"], row["name"], row["tbl_name"], row["sql"])
                for row in connection.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master "
                    "ORDER BY type, name"
                )
            ]
        finally:
            connection.close()

    def test_rebuild_with_identical_spec_returns_false(self):
        spec = parse_database(example())
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "finn.db")
            self.assertTrue(bootstrap(path, spec))
            self.assertFalse(bootstrap(path, spec))

    def test_changed_column_type_raises_schema_conflict(self):
        spec = parse_database(example())
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "finn.db")
            self.assertTrue(bootstrap(path, spec))

            config = example()
            config["tables"]["users"]["columns"]["name"]["type"] = "integer"
            changed = parse_database(config)
            with self.assertRaises(SchemaConflictError) as caught:
                bootstrap(path, changed)

        self.assertIsInstance(caught.exception, ConfigError)
        self.assertEqual(
            caught.exception.path, "database.tables.users.columns.name"
        )
        self.assertIn("column type changed", caught.exception.message)

    def test_added_config_column_raises_schema_conflict(self):
        # §10.3 is non-destructive and additive-never: bootstrap cannot ALTER an
        # existing table, so a spec column absent on disk is a conflict.
        spec = parse_database(example())
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "finn.db")
            self.assertTrue(bootstrap(path, spec))

            config = example()
            config["tables"]["users"]["columns"]["nickname"] = {
                "type": "text",
                "nullable": True,
            }
            added = parse_database(config)
            with self.assertRaises(SchemaConflictError) as caught:
                bootstrap(path, added)

            # The failed re-boot must not have altered the table.
            connection = connect(path)
            try:
                disk_columns = {
                    row["name"]
                    for row in connection.execute(
                        'PRAGMA table_info("users")'
                    )
                }
            finally:
                connection.close()

        self.assertEqual(
            caught.exception.path,
            "database.tables.users.columns.nickname",
        )
        self.assertIn("missing from existing table", caught.exception.message)
        self.assertNotIn("nickname", disk_columns)

    def test_added_index_over_missing_column_raises_schema_conflict(self):
        # Compatibility is verified before any DDL runs, so an index over a
        # column absent on disk surfaces as SchemaConflictError (never as the
        # raw sqlite3.OperationalError that CREATE INDEX would raise).
        base = parse_database(self._index_conflict_base())
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "index_conflict.db")
            self.assertTrue(bootstrap(path, base))

            changed = parse_database(self._index_conflict_changed())
            with self.assertRaises(SchemaConflictError) as caught:
                bootstrap(path, changed)
            self.assertNotIsInstance(caught.exception, sqlite3.OperationalError)

        self.assertEqual(caught.exception.path, "database.tables.t.columns.b")
        self.assertIn("missing from existing table", caught.exception.message)

    def test_failed_rebuild_leaves_schema_unchanged(self):
        base = parse_database(self._index_conflict_base())
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "index_conflict.db")
            self.assertTrue(bootstrap(path, base))
            before = self._master_snapshot(path)

            changed = parse_database(self._index_conflict_changed())
            with self.assertRaises(SchemaConflictError):
                bootstrap(path, changed)

            after = self._master_snapshot(path)
            connection = connect(path)
            try:
                disk_columns = {
                    row["name"]
                    for row in connection.execute('PRAGMA table_info("t")')
                }
            finally:
                connection.close()

        # No partial schema: the conflicting index was never created and every
        # sqlite_master object is exactly as it was.
        self.assertEqual(before, after)
        self.assertNotIn("idx_t_b", {name for _, name, _, _ in after})
        self.assertNotIn("b", disk_columns)

    def test_composite_unique_index_does_not_satisfy_column_unique(self):
        # A column that is only one member of a composite UNIQUE index does not
        # satisfy a single-column UNIQUE declaration.
        base = parse_database(
            {
                "tables": {
                    "t": {
                        "columns": {
                            "id": {"type": "integer"},
                            "a": {"type": "text"},
                            "b": {"type": "text"},
                        }
                    }
                },
                "indexes": [
                    {
                        "table": "t",
                        "columns": ["a", "b"],
                        "unique": True,
                        "name": "uq_t_a_b",
                    }
                ],
            }
        )
        config = {
            "tables": {
                "t": {
                    "columns": {
                        "id": {"type": "integer"},
                        "a": {"type": "text", "unique": True},
                        "b": {"type": "text"},
                    }
                }
            },
            "indexes": [
                {
                    "table": "t",
                    "columns": ["a", "b"],
                    "unique": True,
                    "name": "uq_t_a_b",
                }
            ],
        }
        changed = parse_database(config)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "composite_unique.db")
            self.assertTrue(bootstrap(path, base))
            with self.assertRaises(SchemaConflictError) as caught:
                bootstrap(path, changed)

        self.assertEqual(caught.exception.path, "database.tables.t.columns.a")
        self.assertIn("column uniqueness changed", caught.exception.message)


class AmbiguousColumnTypeTests(unittest.TestCase):
    def test_ambiguous_name_omitted_from_flat_map(self):
        spec = parse_database(
            {
                "tables": {
                    "alpha": {"columns": {"value": {"type": "text"}}},
                    "beta": {"columns": {"value": {"type": "integer"}}},
                }
            }
        )
        self.assertNotIn("value", spec.all_column_types())
        self.assertEqual(spec.column_types("alpha"), {"value": "text"})
        self.assertEqual(spec.column_types("beta"), {"value": "integer"})


class CoerceValueTests(unittest.TestCase):
    def test_boolean_coerces_to_int(self):
        self.assertIs(type(coerce_value("boolean", True)), int)
        self.assertIs(type(coerce_value("boolean", False)), int)
        self.assertEqual(coerce_value("boolean", True), 1)
        self.assertEqual(coerce_value("boolean", False), 0)

    def test_dict_coerces_to_json_text(self):
        self.assertEqual(
            coerce_value("json", {"a": [1, 2]}),
            json.dumps({"a": [1, 2]}),
        )

    def test_invalid_json_string_rejected(self):
        with self.assertRaises(ValidationError):
            coerce_value("json", "{not json")

    def test_iso_datetime_accepted_with_and_without_z(self):
        for value in ("2026-11-15T12:00:00Z", "2026-11-15T12:00:00"):
            with self.subTest(value=value):
                self.assertEqual(coerce_value("datetime", value), value)

    def test_non_date_datetime_rejected(self):
        with self.assertRaises(ValidationError):
            coerce_value("datetime", "not-a-date")

    def test_bytes_json_value_raises_validation_error(self):
        # bytes are not a JSON document; they must never surface as a raw
        # UnicodeDecodeError from the utf-8 decode.
        with self.assertRaises(ValidationError):
            coerce("json", b"not utf8 or whatever")
        with self.assertRaises(ValidationError) as caught:
            coerce("json", b"\xff\xfe not utf8")
        self.assertIn("UTF-8", str(caught.exception))


class DescribeSchemaTests(unittest.TestCase):
    def test_describe_schema_includes_table_and_view_columns(self):
        spec = parse_database(
            {
                "tables": {
                    "users": {
                        "columns": {
                            "id": {
                                "type": "integer",
                                "primaryKey": True,
                                "autoincrement": True,
                            },
                            "name": {"type": "text"},
                        }
                    }
                },
                "views": {
                    "v_users": {"sql": "SELECT id, name FROM users"}
                },
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "describe.db")
            self.assertTrue(bootstrap(path, spec))
            connection = connect(path)
            try:
                described = describe_schema(connection)
            finally:
                connection.close()

        self.assertIn("users", described)
        self.assertIn("v_users", described)
        for object_name in ("users", "v_users"):
            self.assertEqual(set(described[object_name]), {"id", "name"})
            for column in described[object_name].values():
                self.assertIn("type", column)
                self.assertIn("notnull", column)
                self.assertIn("pk", column)

        self.assertEqual(described["users"]["id"]["type"], "INTEGER")
        self.assertTrue(described["users"]["id"]["pk"])
        self.assertFalse(described["users"]["id"]["notnull"])
        self.assertEqual(described["users"]["name"]["type"], "TEXT")
        self.assertTrue(described["users"]["name"]["notnull"])
        self.assertFalse(described["users"]["name"]["pk"])
        self.assertEqual(described["v_users"]["id"]["type"], "INTEGER")


class ReferentialActionGuardTests(unittest.TestCase):
    def test_on_delete_without_references_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            parse_database(
                {
                    "tables": {
                        "t": {
                            "columns": {
                                "id": {"type": "integer", "primaryKey": True},
                                "note": {"type": "text", "onDelete": "CASCADE"},
                            }
                        }
                    }
                }
            )
        self.assertEqual(
            caught.exception.path, "database.tables.t.columns.note.onDelete"
        )
        self.assertIn("requires a 'references'", caught.exception.message)

    def test_on_update_without_references_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            parse_database(
                {
                    "tables": {
                        "t": {
                            "columns": {
                                "id": {"type": "integer", "primaryKey": True},
                                "note": {"type": "text", "onUpdate": "CASCADE"},
                            }
                        }
                    }
                }
            )
        self.assertEqual(
            caught.exception.path, "database.tables.t.columns.note.onUpdate"
        )
        self.assertIn("requires a 'references'", caught.exception.message)


class DottedNameValidationTests(unittest.TestCase):
    def test_table_name_with_dot_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            parse_database(
                {
                    "tables": {
                        "a.b": {"columns": {"id": {"type": "integer"}}}
                    }
                }
            )
        self.assertEqual(caught.exception.path, "database.tables.a.b")
        self.assertIn("must not contain '.'", caught.exception.message)

    def test_column_name_with_dot_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            parse_database(
                {"tables": {"t": {"columns": {"a.b": {"type": "text"}}}}}
            )
        self.assertEqual(
            caught.exception.path, "database.tables.t.columns.a.b"
        )
        self.assertIn("must not contain '.'", caught.exception.message)


class DummyDatabaseTests(unittest.TestCase):
    """Generates a durable, richly seeded ``tests/fixtures/dummy.db``.

    Unlike every other test here, the artifact is deliberately *not* thrown
    away: it stays on disk so API/GUI/CLI developers have a realistic database
    to inspect.  The ``finn.jsonc`` next to it records the exact seed config.
    Both files are generated at run time and are not committed.
    """

    def test_generate_dummy_database(self):
        db_path = os.path.join(FIXTURES_DIR, "dummy.db")
        config_path = os.path.join(FIXTURES_DIR, "finn.jsonc")

        # Deterministic regeneration: drop any previous DB (and its sidecars)
        # so the seed data is always fresh.
        os.makedirs(FIXTURES_DIR, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            stale = db_path + suffix
            if os.path.exists(stale):
                os.remove(stale)

        spec = parse_database(DUMMY_CONFIG)
        self.assertTrue(bootstrap(db_path, spec))
        self.assertTrue(os.path.isfile(db_path))

        connection = connect(db_path)
        try:
            expected_counts = {"users": 3, "posts": 3, "attachments": 2}
            for table, expected in expected_counts.items():
                with self.subTest(table=table):
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM " + table
                        ).fetchone()[0],
                        expected,
                    )

            posts_fk = connection.execute(
                "PRAGMA foreign_key_list(posts)"
            ).fetchall()
            self.assertEqual(len(posts_fk), 1)
            self.assertEqual(posts_fk[0]["from"], "user_id")
            self.assertEqual(posts_fk[0]["table"], "users")
            self.assertEqual(posts_fk[0]["to"], "id")
            self.assertEqual(posts_fk[0]["on_delete"], "CASCADE")

            attachments_fk = connection.execute(
                "PRAGMA foreign_key_list(attachments)"
            ).fetchall()
            self.assertEqual(len(attachments_fk), 1)
            self.assertEqual(attachments_fk[0]["from"], "post_id")
            self.assertEqual(attachments_fk[0]["table"], "posts")
            self.assertEqual(attachments_fk[0]["to"], "id")

            # Rich canonical-type round trip: booleans, embedded JSON,
            # datetimes, plain NULLs, and base64 blobs.
            ada = serialize_row(
                connection.execute(
                    "SELECT * FROM users WHERE email = 'ada@example.com'"
                ).fetchone(),
                spec.column_types("users"),
            )
            self.assertIs(ada["is_active"], True)
            self.assertIsInstance(ada["profile"], dict)
            self.assertEqual(ada["profile"]["role"], "admin")
            self.assertEqual(ada["profile"]["age"], 36)
            self.assertIsInstance(ada["created_at"], str)
            self.assertTrue(ada["created_at"].startswith("2026-01-15T09:00:00"))

            alan = serialize_row(
                connection.execute(
                    "SELECT * FROM users WHERE email = 'alan@example.com'"
                ).fetchone(),
                spec.column_types("users"),
            )
            self.assertIs(alan["is_active"], False)
            self.assertIsNone(alan["profile"])

            draft = serialize_row(
                connection.execute(
                    "SELECT * FROM posts WHERE title = 'Draft notes'"
                ).fetchone(),
                spec.column_types("posts"),
            )
            self.assertIs(draft["published"], False)
            self.assertIsNone(draft["published_at"])

            published = serialize_row(
                connection.execute(
                    "SELECT * FROM posts WHERE title = 'Hello world'"
                ).fetchone(),
                spec.column_types("posts"),
            )
            self.assertIsInstance(published["tags"], list)
            self.assertEqual(published["tags"], ["intro", "meta"])

            cover = serialize_row(
                connection.execute(
                    "SELECT * FROM attachments WHERE label = 'cover.txt'"
                ).fetchone(),
                spec.column_types("attachments"),
            )
            self.assertEqual(cover["data"], "$base64:aGVsbG8=")

            notes = serialize_row(
                connection.execute(
                    "SELECT * FROM attachments WHERE label = 'notes.txt'"
                ).fetchone(),
                spec.column_types("attachments"),
            )
            self.assertIsNone(notes["data"])
        finally:
            connection.close()

        # Keep the generating config alongside the DB for reproducibility.
        with open(config_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(DUMMY_CONFIG, indent=2))
        self.assertTrue(os.path.isfile(config_path))


if __name__ == "__main__":
    unittest.main()