"""Unit tests for FINN Studio (the PyQt6 GUI wrapper around the CLI).

Covers the non-Qt logic — canonical serialization, CLI command builders,
handshake parsing, and field classification — plus an opt-in offscreen GUI
smoke test (FINN_RUN_GUI_TESTS=1).
"""

import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import finn_studio as fs


class TestCanonicalSerialization(unittest.TestCase):
    def assert_keys(self, obj, expected, msg=None):
        self.assertEqual(list(obj.keys()), list(expected), msg)

    def test_top_level_order(self):
        model = {
            "api": {"endpoints": {}},
            "database": {"tables": {}},
            "meta": {"name": "x"},
            "version": 1,
        }
        canon = json.loads(fs.canonical_json(model))
        self.assert_keys(canon, ["version", "meta", "database", "api"])

    def test_database_and_table_order(self):
        model = {
            "version": 1,
            "database": {
                "init": ["PRAGMA x"],
                "tables": {"t": {"columns": {
                    "c": {"type": "text", "description": "d", "nullable": False}
                }, "withoutRowid": False}},
                "file": "finn.db",
            },
            "api": {"endpoints": {}},
        }
        db = json.loads(fs.canonical_json(model))["database"]
        # only keys present, in canonical order
        self.assert_keys(db, ["file", "tables", "init"])
        col = db["tables"]["t"]["columns"]["c"]
        self.assert_keys(col, ["type", "nullable", "description"])

    def test_type_alias_normalization(self):
        model = {
            "version": 1,
            "database": {"tables": {"t": {"columns": {
                "a": {"type": "int"},
                "b": {"type": "float"},
                "c": {"type": "double"},
                "d": {"type": "string"},
                "e": {"type": "varchar"},
                "f": {"type": "bytes"},
                "g": {"type": "INTEGER"},
            }}}},
            "api": {"endpoints": {}},
        }
        cols = json.loads(fs.canonical_json(model))["database"]["tables"]["t"]["columns"]
        self.assertEqual(cols["a"]["type"], "integer")
        self.assertEqual(cols["b"]["type"], "real")
        self.assertEqual(cols["c"]["type"], "real")
        self.assertEqual(cols["d"]["type"], "text")
        self.assertEqual(cols["e"]["type"], "text")
        self.assertEqual(cols["f"]["type"], "blob")
        self.assertEqual(cols["g"]["type"], "integer")

    def test_method_reorder_and_lowercase(self):
        model = {
            "version": 1,
            "database": {"tables": {}},
            "api": {"endpoints": {"/u": {
                "DELETE": "DELETE FROM u",
                "get": "SELECT * FROM u",
                "POST": {"sql": "INSERT INTO u"},
            }}},
        }
        ep = json.loads(fs.canonical_json(model))["api"]["endpoints"]["/u"]
        self.assert_keys(ep, ["get", "post", "delete"])

    def test_bare_string_shorthand_preserved(self):
        model = {
            "version": 1,
            "database": {"tables": {}},
            "api": {"endpoints": {"/u": {"get": "SELECT 1"}}},
        }
        text = fs.canonical_json(model)
        self.assertIn('"get": "SELECT 1"', text)

    def test_unknown_keys_preserved(self):
        model = {
            "version": 1,
            "database": {"tables": {}},
            "api": {"endpoints": {}},
            "custom_top": {"keep": True},
        }
        canon = json.loads(fs.canonical_json(model))
        self.assertEqual(canon["custom_top"], {"keep": True})

    def test_trailing_newline_and_indent(self):
        model = {"version": 1, "database": {"tables": {}}, "api": {"endpoints": {}}}
        text = fs.canonical_json(model)
        self.assertTrue(text.endswith("\n"))
        self.assertIn('  "database": {', text)   # top-level key at 2-space indent
        self.assertIn('    "tables": {}', text)  # nested key at 4-space indent

    def test_fixture_roundtrip(self):
        # The fixture is a database-only object (consumed by parse_database);
        # canonical serialization must be a fixed point (idempotent) so saving
        # a loaded file never drifts across repeated saves.
        fix = pathlib.Path(__file__).resolve().parent / "fixtures" / "finn.json"
        model = json.loads(fix.read_text(encoding="utf-8-sig"))
        once = fs.canonical_json(model)
        self.assertEqual(fs.canonical_json(json.loads(once)), once)


class TestCliHelpers(unittest.TestCase):
    def test_handshake_args(self):
        self.assertEqual(fs.cli_args_handshake(), ["--version", "--json"])

    def test_validate_args(self):
        self.assertEqual(
            fs.cli_args_validate("/x.json"),
            ["validate", "--json", "--config", "/x.json"])

    def test_build_args(self):
        self.assertEqual(
            fs.cli_args_build("/x.json"),
            ["build", "--json", "--config", "/x.json"])

    def test_init_args(self):
        self.assertEqual(
            fs.cli_args_init("/d", "blank"),
            ["init", "--json", "--template", "blank", "/d"])
        self.assertEqual(
            fs.cli_args_init("/d", "minimal", "app", True),
            ["init", "--json", "--template", "minimal", "--name", "app",
             "--force", "/d"])

    def test_run_args(self):
        self.assertEqual(
            fs.cli_args_run("/x.json", 9000),
            ["run", "--json", "--config", "/x.json", "--host", "127.0.0.1",
             "--port", "9000"])


class TestHandshake(unittest.TestCase):
    def test_ok(self):
        self.assertTrue(fs.parse_handshake(
            json.dumps({"version": "0.1.0", "configSchema": 1}))["ok"])

    def test_too_old(self):
        self.assertFalse(fs.parse_handshake(
            json.dumps({"version": "0.0.1", "configSchema": 1}))["ok"])

    def test_wrong_schema(self):
        self.assertFalse(fs.parse_handshake(
            json.dumps({"version": "0.5.0", "configSchema": 2}))["ok"])

    def test_unparseable(self):
        self.assertFalse(fs.parse_handshake("not json")["ok"])

    def test_newer_ok(self):
        self.assertTrue(fs.parse_handshake(
            json.dumps({"version": "0.9.0", "configSchema": 1}))["ok"])


class TestClassification(unittest.TestCase):
    def test_enum_type(self):
        self.assertEqual(fs.classify_field("type", "text")[0], "enum")

    def test_enum_shape(self):
        self.assertEqual(fs.classify_field("shape", "list")[0], "enum")

    def test_bool(self):
        self.assertEqual(fs.classify_field("nullable", False)[0], "bool")

    def test_int(self):
        self.assertEqual(fs.classify_field("version", 1)[0], "int")

    def test_default(self):
        self.assertEqual(fs.classify_field("default", 0)[0], "default")

    def test_has_errors(self):
        self.assertTrue(fs.has_errors([{"severity": "error"}]))
        self.assertFalse(fs.has_errors([{"severity": "warning"}]))


class TestProtectedPaths(unittest.TestCase):
    def test_top_level_required(self):
        self.assertTrue(fs.JsonTreeWidget._is_protected(("version",)))
        self.assertTrue(fs.JsonTreeWidget._is_protected(("database",)))
        self.assertTrue(fs.JsonTreeWidget._is_protected(("api",)))

    def test_table_columns_container(self):
        self.assertTrue(
            fs.JsonTreeWidget._is_protected(
                ("database", "tables", "users", "columns")))

    def test_not_protected(self):
        self.assertFalse(fs.JsonTreeWidget._is_protected(("meta",)))
        self.assertFalse(
            fs.JsonTreeWidget._is_protected(("database", "tables", "users")))
        self.assertFalse(
            fs.JsonTreeWidget._is_protected(
                ("database", "tables", "users", "columns", "id")))


class TestAtomicWrite(unittest.TestCase):
    def test_roundtrip_and_no_leftover(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "finn.json"
            fs.atomic_write_text(target, '{"version": 1}\n')
            self.assertEqual(target.read_text(), '{"version": 1}\n')
            leftovers = [
                n for n in os.listdir(tmp) if n.endswith(".tmp")
            ]
            self.assertEqual(leftovers, [])

    def test_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "finn.json"
            target.write_text("old")
            fs.atomic_write_text(target, "new")
            self.assertEqual(target.read_text(), "new")


@unittest.skipUnless(
    os.environ.get("FINN_RUN_GUI_TESTS") == "1",
    "set FINN_RUN_GUI_TESTS=1 to run the offscreen GUI smoke test")
class TestGuiOffscreen(unittest.TestCase):
    def test_construct_and_save_roundtrip(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtTest import QTest
        app = QApplication.instance() or QApplication([])

        win = fs.MainWindow()
        win.show()
        for _ in range(150):
            QTest.qWait(20)
            if win.handshake_complete:
                break
        self.assertIsNone(win.degraded_reason)

        with tempfile.TemporaryDirectory() as tmp:
            cfg = pathlib.Path(tmp) / "finn.json"
            cfg.write_text(json.dumps({
                "version": 1,
                "database": {"tables": {"t": {"columns": {
                    "id": {"type": "int", "primaryKey": True},
                }}}},
                "api": {"endpoints": {}},
            }))
            win._open_project_path(str(cfg))
            self.assertFalse(win.version_readonly)
            win._on_save()
            canon = json.loads(cfg.read_text())
            self.assertEqual(
                canon["database"]["tables"]["t"]["columns"]["id"]["type"],
                "integer")
            v2 = pathlib.Path(tmp) / "v2.json"
            v2.write_text(json.dumps({"version": 2, "database": {}, "api": {}}))
            win._open_project_path(str(v2))
            self.assertTrue(win.version_readonly)
            self.assertFalse(win.save_action.isEnabled())

    def test_version_gate_recompute_on_edit(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        win = fs.MainWindow()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = pathlib.Path(tmp) / "finn.json"
            cfg.write_text(json.dumps({
                "version": 1,
                "database": {"tables": {}},
                "api": {"endpoints": {}},
            }))
            win._open_project_path(str(cfg))
            self.assertFalse(win.version_readonly)
            # mutating version in the model must flip the gate (no bypass)
            win.model["version"] = 2
            win._on_edited()
            self.assertTrue(win.version_readonly)
            self.assertFalse(win.save_action.isEnabled())


if __name__ == "__main__":
    unittest.main()
