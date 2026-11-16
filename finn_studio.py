#!/usr/bin/env python3
"""FINN Studio - a local PyQt6 GUI that wraps the FINN CLI.

The CLI is the single source of behavioral truth: this app never re-implements
validation, build, DDL, or serving.  It drives the CLI as a subprocess using
``--json`` and edits the single JSON config in memory, writing it back
canonically (docs/gui-layer.md).  This implementation is a desktop host rather
than a browser host.

Run:  python finn_studio.py [PROJECT_DIR]
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, QProcess, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QAction, QDesktopServices, QFont
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QRadioButton, QSpinBox,
    QSplitter, QTabWidget, QTextEdit, QToolBar, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

APP_NAME = "FINN Studio"

# ---------------------------------------------------------------------------
# Vocabulary (mirrors the generator packages; kept local so the GUI has no
# runtime dependency on them).
# ---------------------------------------------------------------------------

CANONICAL_TYPES = ("integer", "real", "text", "blob", "boolean", "json", "datetime")
TYPE_ALIASES = {
    "integer": "integer", "int": "integer",
    "real": "real", "float": "real", "double": "real",
    "text": "text", "string": "text", "varchar": "text",
    "blob": "blob", "bytes": "blob",
    "boolean": "boolean", "json": "json", "datetime": "datetime",
}
FK_ACTIONS = ("NO ACTION", "RESTRICT", "SET NULL", "SET DEFAULT", "CASCADE")
METHODS = ("get", "post", "put", "patch", "delete")
PARAM_SOURCES = ("path", "query", "body", "header")
RESPONSE_SHAPES = ("list", "single", "none")
MIN_CLI_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Canonical serialization (docs/gui-layer.md)
# ---------------------------------------------------------------------------

_KEY_ORDER = {
    "root": ("version", "meta", "database", "api"),
    "database": ("file", "tables", "indexes", "views", "init"),
    "table": ("columns", "primaryKey", "indexes", "withoutRowid"),
    "column": ("type", "nullable", "default", "primaryKey", "autoincrement",
               "unique", "check", "references", "onDelete", "onUpdate",
               "description"),
    "operation": ("sql", "multi", "params", "response", "description",
                  "authentication", "rateLimit"),
    "response": ("shape", "key", "columns", "renames", "paginate", "blob"),
    "param": ("source", "type", "required", "default"),
    "index": ("table", "columns", "unique", "name"),
    "view": ("sql",),
    "api": ("basePath", "static", "endpoints"),
}

_CHILD_CTX = {
    "root": {"meta": "meta", "database": "database", "api": "api"},
    "database": {"tables": "tables", "indexes": "indexes", "views": "views"},
    "table": {"columns": "columns", "indexes": "indexes"},
    "api": {"endpoints": "endpoints"},
    "operation": {"params": "params", "response": "response"},
}

_MAP_ELEM_CTX = {
    "tables": "table", "columns": "column", "views": "view",
    "endpoints": "endpoint", "params": "param", "renames": "generic",
    "meta": "generic",
}


def normalize_type_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return TYPE_ALIASES.get(value.strip().lower(), value)


def _canonicalize_endpoint(endpoint: Dict[str, Any]) -> Dict[str, Any]:
    lowered = {str(k).lower(): k for k in endpoint}
    result: Dict[str, Any] = {}
    seen = set()
    for method in METHODS:
        if method in lowered:
            orig = lowered[method]
            value = endpoint[orig]
            ctx = "operation" if isinstance(value, dict) else "generic"
            result[method] = _canonicalize(value, ctx)
            seen.add(orig)
    for key, value in endpoint.items():
        if key in seen:
            continue
        lkey = str(key).lower()
        if lkey in ("head", "options"):
            result[lkey] = _canonicalize(value, "generic")
        else:
            result[key] = _canonicalize(value, "generic")
    return result


def _canonicalize(node: Any, ctx: str) -> Any:
    if isinstance(node, list):
        elem_ctx = "index" if ctx == "indexes" else "generic"
        return [_canonicalize(item, elem_ctx) for item in node]
    if not isinstance(node, dict):
        return node

    if ctx in _MAP_ELEM_CTX:
        if ctx == "meta":
            return dict(node)
        elem_ctx = _MAP_ELEM_CTX[ctx]
        return {k: _canonicalize(v, elem_ctx) for k, v in node.items()}

    if ctx == "endpoint":
        return _canonicalize_endpoint(node)

    order = _KEY_ORDER.get(ctx)
    child_ctx = _CHILD_CTX.get(ctx, {})
    result: Dict[str, Any] = {}

    if order:
        for key in order:
            if key not in node:
                continue
            value = node[key]
            if key == "type" and ctx in ("column", "param"):
                value = normalize_type_value(value)
            result[key] = _canonicalize(value, child_ctx.get(key, "generic"))

    for key, value in node.items():
        if key in result:
            continue
        result[key] = _canonicalize(value, "generic")

    return result


def canonical_json(model: Any) -> str:
    indirect = json.dumps(_canonicalize(model, "root"), indent=2, ensure_ascii=False)
    return indirect + "\n"


# ---------------------------------------------------------------------------
# CLI resolution + command builders (pure, unit-testable)
# ---------------------------------------------------------------------------

def _parse_version(text: str) -> Tuple[int, ...]:
    parts: List[int] = []
    for chunk in str(text).strip().split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            break
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts) if parts else (0,)


def _at_least(version: str, minimum: str) -> bool:
    return _parse_version(version) >= _parse_version(minimum)


def cli_script_path() -> Path:
    return Path(__file__).resolve().parent / "finn_cli.py"


def default_cli_command() -> List[str]:
    finn = shutil.which("finn")
    if finn:
        return [finn]
    if getattr(sys, "frozen", False):
        # Frozen GUI must drive the `finn` CLI on PATH; returning the bare
        # name lets QProcess fail cleanly (degraded mode) if it is absent
        # instead of re-spawning this binary as if it were Python.
        return ["finn"]
    return [sys.executable, str(cli_script_path())]


def can_serve(python: str) -> bool:
    try:
        result = subprocess.run(
            [python, "-c", "import fastapi, uvicorn"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except OSError:
        return False


def run_command() -> List[str]:
    command = default_cli_command()
    script = str(cli_script_path())
    if len(command) == 2 and command[1] == script:
        repo = cli_script_path().parent
        venv_py = repo / ".venv" / "bin" / "python"
        if venv_py.exists() and can_serve(str(venv_py)):
            return [str(venv_py), script]
        if can_serve(sys.executable):
            return [sys.executable, script]
    return command


def cli_args_handshake() -> List[str]:
    return ["--version", "--json"]


def cli_args_validate(path: str) -> List[str]:
    return ["validate", "--json", "--config", path]


def cli_args_build(path: str) -> List[str]:
    return ["build", "--json", "--config", path]


def cli_args_init(target: str, template: str, name: Optional[str] = None,
                  force: bool = False) -> List[str]:
    args = ["init", "--json", "--template", template]
    if name:
        args += ["--name", name]
    if force:
        args.append("--force")
    args.append(target)
    return args


def cli_args_run(path: str, port: int) -> List[str]:
    return ["run", "--json", "--config", path, "--host", "127.0.0.1",
            "--port", str(port)]


def parse_handshake(stdout: str) -> Dict[str, Any]:
    try:
        doc = json.loads(stdout)
    except ValueError as exc:
        return {"ok": False, "reason": "unparseable handshake: {}".format(exc)}
    if not isinstance(doc, dict):
        return {"ok": False, "reason": "handshake is not a JSON object"}
    schema = doc.get("configSchema")
    version = doc.get("version")
    if schema != 1:
        return {"ok": False,
                "reason": "unsupported configSchema {!r}".format(schema)}
    if not isinstance(version, str) or not _at_least(version, MIN_CLI_VERSION):
        return {"ok": False,
                "reason": "CLI version {!r} is too old (need {})".format(
                    version, MIN_CLI_VERSION)}
    return {"ok": True, "version": version, "schema": schema}


def has_errors(diagnostics: List[Dict[str, Any]]) -> bool:
    return any(d.get("severity") == "error" for d in diagnostics)


def diagnostic_text(diag: Dict[str, Any]) -> str:
    return "[{}] {} {}{}".format(
        diag.get("severity", ""), diag.get("code", ""),
        (diag.get("path") or "") + ": " if diag.get("path") else "",
        diag.get("message", ""),
    )


# ---------------------------------------------------------------------------
# Field classification for schema-aware controls
# ---------------------------------------------------------------------------

_ENUM_FIELDS = {
    "type": CANONICAL_TYPES,
    "source": PARAM_SOURCES,
    "shape": RESPONSE_SHAPES,
    "onDelete": FK_ACTIONS,
    "onUpdate": FK_ACTIONS,
}
_BOOL_FIELDS = {
    "nullable", "primaryKey", "autoincrement", "unique", "withoutRowid",
    "multi", "paginate", "required",
}
_INT_FIELDS = {"version", "port", "pageSize", "page"}


def runtime_kind(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if value is None:
        return "null"
    if isinstance(value, (list, dict)):
        return "container"
    return "str"


def classify_field(key: str, value: Any) -> Tuple[str, Optional[Tuple[str, ...]]]:
    if isinstance(value, str) and key in _ENUM_FIELDS:
        return "enum", _ENUM_FIELDS[key]
    if isinstance(value, bool) and key in _BOOL_FIELDS:
        return "bool", None
    if isinstance(value, int) and key in _INT_FIELDS:
        return "int", None
    if key == "default":
        return "default", None
    kind = runtime_kind(value)
    return (kind, None) if kind != "container" else ("container", None)


# ---------------------------------------------------------------------------
# Generic path helpers (paths are tuples of str keys / int indexes)
# ---------------------------------------------------------------------------

def get_by_path(model: Any, path: Tuple[Any, ...]) -> Any:
    node = model
    for part in path:
        node = node[part]
    return node


def set_by_path(model: Any, path: Tuple[Any, ...], value: Any) -> None:
    container = model
    for part in path[:-1]:
        container = container[part]
    container[path[-1]] = value


def del_by_path(model: Any, path: Tuple[Any, ...]) -> None:
    container = model
    for part in path[:-1]:
        container = container[part]
    if isinstance(container, list):
        container.pop(path[-1])
    else:
        del container[path[-1]]


def _insert_default_for(target: Dict[Any, Any], kind: str) -> Any:
    """Kind is the context name for a new element's expected shape."""
    defaults = {
        "table": {"columns": {
            "id": {"type": "integer", "primaryKey": True, "autoincrement": True}}},
        "column": {"type": "text"},
        "view": {"sql": ""},
        "db-index": {"table": "", "columns": []},
        "index": {"columns": []},
        "endpoint": {"get": {"sql": ""}},
        "operation": {"sql": ""},
        "param": {"source": "query", "type": "text"},
        "rename": "",
    }
    return json.loads(json.dumps(defaults.get(kind, "")))


def guess_child_kind(parent_path: Tuple[Any, ...], new_key: Any) -> str:
    path_str = [str(p) for p in parent_path]
    # returns the kind of the new element created under parent_path/new_key
    if path_str and path_str[-1] == "tables":
        return "table"
    if path_str and path_str[-1] == "columns":
        return "column"
    if path_str and path_str[-1] == "views":
        return "view"
    if path_str and path_str[-1] == "indexes":
        return "db-index"
    if path_str and path_str[-1] == "endpoints":
        return "endpoint"
    if path_str and path_str[-1] == "params":
        return "param"
    if path_str and path_str[-1] == "renames":
        return "rename"
    if len(path_str) >= 1 and path_str[-1] == "api":
        return "endpoint"
    if path_str and path_str[-1] in METHODS:
        return "operation"
    return "generic"


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------

def atomic_write_text(path: Path, text: str) -> None:
    import tempfile
    path = Path(path)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# CLI engine (QProcess wrapper)
# ---------------------------------------------------------------------------

class CliEngine(QObject):
    """Runs the CLI as a subprocess (argv arrays, never a shell)."""

    handshakeDone = pyqtSignal(dict)
    opFinished = pyqtSignal(str, int, str, str)  # op, exitCode, stdout, stderr
    runEvent = pyqtSignal(dict)
    runLog = pyqtSignal(str)
    runExited = pyqtSignal(int)

    def __init__(self, command: List[str], run_command: List[str],
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self.command = command
        self.run_command = run_command
        self._handshake_proc: Optional[QProcess] = None
        self._run_proc: Optional[QProcess] = None
        self._run_stdout = b""
        self._run_stderr = b""

    def _decode(self, data) -> str:
        return bytes(data).decode("utf-8", errors="replace")

    def handshake(self) -> None:
        if self._handshake_proc is not None:
            return
        proc = QProcess(self)
        proc.setProgram(self.command[0])
        proc.setArguments(self.command[1:] + cli_args_handshake())
        proc.finished.connect(self._on_handshake_finished)
        proc.errorOccurred.connect(self._on_handshake_error)
        self._handshake_proc = proc
        proc.start()

    def _on_handshake_error(self, error):
        proc = self._handshake_proc
        if proc is not None and error == QProcess.ProcessError.FailedToStart:
            self._handshake_proc = None
            self.handshakeDone.emit(
                {"ok": False,
                 "reason": "cannot start CLI: {}".format(proc.errorString())})

    def _on_handshake_finished(self, exit_code, status):
        proc = self._handshake_proc
        self._handshake_proc = None
        stdout = self._decode(proc.readAllStandardOutput())
        if proc.exitStatus() != QProcess.ExitStatus.NormalExit or exit_code != 0:
            self.handshakeDone.emit(
                {"ok": False, "reason": "CLI exited with code {}".format(exit_code)})
            return
        self.handshakeDone.emit(parse_handshake(stdout))

    def run_op(self, op: str, args: List[str], cwd: Optional[str] = None) -> None:
        proc = QProcess(self)
        proc.setProperty("op", op)
        proc.setProperty("args", list(args))
        proc.setProperty("cwd", cwd or "")
        proc.setProgram(self.command[0])
        proc.setArguments(self.command[1:] + args)
        if cwd:
            proc.setWorkingDirectory(cwd)
        proc.finished.connect(self._on_op_finished)
        proc.errorOccurred.connect(self._on_op_error)
        proc.start()

    def _on_op_error(self, error):
        if error != QProcess.ProcessError.FailedToStart:
            return
        proc = self.sender()
        op = str(proc.property("op"))
        self.opFinished.emit(
            op, 1, "", "cannot start CLI: {}".format(proc.errorString()))

    def _on_op_finished(self, exit_code, status):
        proc = self.sender()
        op = str(proc.property("op"))
        stdout = self._decode(proc.readAllStandardOutput())
        stderr = self._decode(proc.readAllStandardError())
        self.opFinished.emit(op, exit_code, stdout, stderr)

    def start_run(self, config_path: str, port: int) -> None:
        if self._run_proc is not None:
            return
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        proc.setProgram(self.run_command[0])
        proc.setArguments(self.run_command[1:] + cli_args_run(config_path, port))
        proc.setWorkingDirectory(str(Path(config_path).resolve().parent))
        proc.readyReadStandardOutput.connect(self._on_run_stdout)
        proc.readyReadStandardError.connect(self._on_run_stderr)
        proc.finished.connect(self._on_run_finished)
        proc.errorOccurred.connect(self._on_run_error)
        self._run_proc = proc
        self._run_stdout = b""
        self._run_stderr = b""
        proc.start()

    def _on_run_error(self, error):
        proc = self._run_proc
        if proc is not None and error == QProcess.ProcessError.FailedToStart:
            self._run_proc = None
            self.runLog.emit("cannot start server: {}".format(proc.errorString()))
            self.runExited.emit(1)

    def _on_run_stdout(self):
        proc = self._run_proc
        if proc is None:
            return
        data = bytes(proc.readAllStandardOutput())
        if not data:
            return
        self._run_stdout += data
        while b"\n" in self._run_stdout:
            line, self._run_stdout = self._run_stdout.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(event, dict):
                self.runEvent.emit(event)

    def _on_run_stderr(self):
        proc = self._run_proc
        if proc is None:
            return
        data = bytes(proc.readAllStandardError())
        if data:
            self.runLog.emit(self._decode(data).rstrip("\n"))

    def _on_run_finished(self, exit_code, status):
        proc = self._run_proc
        self._run_proc = None
        # flush any trailing buffered output
        if proc is not None:
            leftover = bytes(proc.readAllStandardOutput())
            if leftover:
                self._run_stdout += leftover
                for line in self._run_stdout.split(b"\n"):
                    line = line.strip()
                    if line:
                        try:
                            event = json.loads(line.decode("utf-8", "replace"))
                        except ValueError:
                            continue
                        if isinstance(event, dict):
                            self.runEvent.emit(event)
            tail = bytes(proc.readAllStandardError())
            if tail:
                self.runLog.emit(self._decode(tail).rstrip("\n"))
        self.runExited.emit(exit_code)

    def stop_run(self) -> None:
        if self._run_proc is not None and self._run_proc.state() != QProcess.ProcessState.NotRunning:
            self._run_proc.terminate()
            QTimer.singleShot(5000, self._kill_run)

    def _kill_run(self):
        if self._run_proc is not None and self._run_proc.state() != QProcess.ProcessState.NotRunning:
            self._run_proc.kill()

    def run_is_active(self) -> bool:
        return self._run_proc is not None and self._run_proc.state() != QProcess.ProcessState.NotRunning


# ---------------------------------------------------------------------------
# JSON tree editor widget
# ---------------------------------------------------------------------------

class JsonTreeWidget(QTreeWidget):
    edited = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.model: Any = None
        self.setColumnCount(3)
        self.setHeaderLabels(["Key", "Type", "Value"])
        self.setColumnWidth(0, 260)
        self.setColumnWidth(1, 90)
        self.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

    def populate(self, model: Any) -> None:
        self.model = model
        self.blockSignals(True)
        self.clear()
        if isinstance(model, dict):
            for key in model:
                self._add_item(self.invisibleRootItem(), key, model[key], (key,))
        self.expandToDepth(1)
        self.blockSignals(False)

    def _add_item(self, parent, key, value, path):
        display_key = str(key)
        kind, _ = classify_field(display_key, value) if not isinstance(value, (dict, list)) else ("container", None)
        if isinstance(value, dict):
            item = QTreeWidgetItem([display_key, "object", "{} keys".format(len(value))])
            item.setData(0, Qt.ItemDataRole.UserRole, list(path))
            parent.addChild(item)
            for k, v in value.items():
                self._add_item(item, k, v, path + (k,))
        elif isinstance(value, list):
            item = QTreeWidgetItem([display_key, "array", "[{} items]".format(len(value))])
            item.setData(0, Qt.ItemDataRole.UserRole, list(path))
            parent.addChild(item)
            for i, v in enumerate(value):
                self._add_item(item, i, v, path + (i,))
        else:
            type_label = runtime_kind(value) if runtime_kind(value) != "container" else "str"
            if value is None:
                type_label = "null"
            elif isinstance(value, bool):
                type_label = "bool"
            item = QTreeWidgetItem([display_key, type_label, self._display_value(value)])
            item.setData(0, Qt.ItemDataRole.UserRole, list(path))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            parent.addChild(item)

    def _display_value(self, value) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _is_protected(path) -> bool:
        if len(path) == 1 and path[0] in ("version", "database", "api"):
            return True
        if path and path[-1] == "columns" and len(path) >= 3 and path[-3] == "tables":
            return True
        return False

    def _path_of(self, item) -> Tuple[Any, ...]:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        return tuple(data) if data else ()

    def _find_item(self, path) -> Optional[QTreeWidgetItem]:
        def walk(item):
            if self._path_of(item) == path:
                return item
            for i in range(item.childCount()):
                found = walk(item.child(i))
                if found:
                    return found
            return None
        for i in range(self.topLevelItemCount()):
            found = walk(self.topLevelItem(i))
            if found:
                return found
        return None

    def _on_item_double_clicked(self, item, column):
        del column
        path = self._path_of(item)
        value = get_by_path(self.model, path)
        if isinstance(value, (dict, list)):
            return
        self.edit_value(path)

    def edit_value(self, path) -> None:
        value = get_by_path(self.model, path)
        key = str(path[-1])
        kind, options = classify_field(key, value)
        if kind in ("enum", "bool"):
            choices = list(options) if options else (["true", "false"] if kind == "bool" else [])
            if kind == "bool":
                choices = ["true", "false"]
                current = "true" if value else "false"
            else:
                current = str(value)
            chosen, ok = QInputDialog.getItem(
                self, "Edit value", str(path[-1]), choices,
                choices.index(current) if current in choices else 0, False)
            if ok:
                new_value = True if chosen == "true" else False if chosen == "false" else chosen
                self._commit(path, new_value)
            return
        if kind == "null":
            choices = ["null", "0", "\"\"", "{}", "[]"]
            chosen, ok = QInputDialog.getItem(
                self, "Edit value", str(path[-1]), choices, 0, False)
            if ok:
                self._commit(path, json.loads(chosen))
            return
        text, ok = QInputDialog.getText(self, "Edit value", str(path[-1]), text=self._display_value(value))
        if not ok:
            return
        self._commit_text(path, kind, text)

    def _commit_text(self, path, kind, text) -> None:
        try:
            if kind == "int":
                value = int(text.strip())
            elif kind == "float":
                value = float(text.strip())
            elif kind == "bool":
                value = text.strip().lower() in ("true", "1", "yes")
            elif kind == "default":
                try:
                    value = json.loads(text.strip())
                except ValueError:
                    value = text
            elif kind == "str":
                value = text
            else:
                value = json.loads(text.strip())
        except (ValueError, TypeError):
            QMessageBox.warning(self, APP_NAME, "Invalid value; change not applied.")
            return
        self._commit(path, value)

    def _commit(self, path, value) -> None:
        set_by_path(self.model, path, value)
        self.populate(self.model)
        self.edited.emit()

    def add_child(self, item: QTreeWidgetItem) -> None:
        path = self._path_of(item)
        value = get_by_path(self.model, path)
        if isinstance(value, dict):
            key, ok = QInputDialog.getText(self, "Add key", "New key name:")
            if not ok or not key.strip():
                return
            key = key.strip()
            if key in value:
                QMessageBox.warning(self, APP_NAME, "Key already exists.")
                return
            value[key] = _insert_default_for({}, guess_child_kind(path, key))
            self.populate(self.model)
            self.edited.emit()
        elif isinstance(value, list):
            value.append(_insert_default_for({}, guess_child_kind(path, None)))
            self.populate(self.model)
            self.edited.emit()

    def delete_item(self, item: QTreeWidgetItem) -> None:
        path = self._path_of(item)
        if not path:
            return
        if self._is_protected(path):
            QMessageBox.warning(
                self, APP_NAME, "This key is required and cannot be deleted.")
            return
        name = str(path[-1])
        # block deleting the last column of a table (reference.md §4.1)
        if len(path) >= 2 and isinstance(path[-2], str) and path[-2] == "columns":
            table_columns = get_by_path(self.model, path[:-1])
            if len(table_columns) <= 1:
                QMessageBox.warning(
                    self, APP_NAME,
                    "This is the last column of the table; a table requires at "
                    "least one column.")
                return
        confirm = QMessageBox.question(
            self, "Delete", 'Delete "{}"?'.format(name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if confirm != QMessageBox.StandardButton.Yes:
            return
        del_by_path(self.model, path)
        self.populate(self.model)
        self.edited.emit()

    def rename_item(self, item: QTreeWidgetItem) -> None:
        path = self._path_of(item)
        if not path:
            return
        if self._is_protected(path):
            QMessageBox.warning(
                self, APP_NAME, "This key cannot be renamed.")
            return
        key, ok = QInputDialog.getText(self, "Rename", "New name:", text=str(path[-1]))
        if not ok or not key.strip():
            return
        new_key = key.strip()
        if new_key == str(path[-1]):
            return
        parent_map = get_by_path(self.model, path[:-1])
        if not isinstance(parent_map, dict):
            QMessageBox.warning(
                self, APP_NAME, "List entries cannot be renamed here.")
            return
        if new_key in parent_map:
            QMessageBox.warning(self, APP_NAME, "Name already in use.")
            return
        self._rename(path, new_key)
        self.populate(self.model)
        self.edited.emit()

    def _rename(self, path, new_key) -> None:
        ref_key = str(path[-1])
        # reinsert under the new key preserving order
        parent_map = get_by_path(self.model, path[:-1])
        value = parent_map[ref_key]
        new_map = {}
        for k, v in parent_map.items():
            if k == ref_key:
                new_map[new_key] = value
            else:
                new_map[k] = v
        set_by_path(self.model, path[:-1], new_map)
        # cascade: rename references for tables / columns
        is_table = len(path) >= 2 and path[-2] == "tables"
        is_column = len(path) >= 2 and path[-2] == "columns"
        if is_table:
            self._cascade_table_rename(ref_key, new_key)
        elif is_column:
            table_name = str(path[-3]) if len(path) >= 3 else None
            self._cascade_column_rename(table_name, ref_key, new_key)

    def _cascade_table_rename(self, old, new) -> None:
        root = self.model
        tables = root.get("database", {}).get("tables", {})
        for tname, table in tables.items():
            for cname, col in table.get("columns", {}).items():
                ref = col.get("references")
                if isinstance(ref, str):
                    if ref == old:
                        col["references"] = new
                    elif ref.startswith(old + "."):
                        col["references"] = new + ref[len(old):]
        for idx in root.get("database", {}).get("indexes", []):
            if isinstance(idx, dict) and idx.get("table") == old:
                idx["table"] = new

    def _cascade_column_rename(self, table_name, old, new) -> None:
        if not table_name:
            return
        root = self.model
        tables = root.get("database", {}).get("tables", {})
        for tname, t in tables.items():
            for cname, col in t.get("columns", {}).items():
                ref = col.get("references")
                if isinstance(ref, str) and ref == table_name + "." + old:
                    col["references"] = table_name + "." + new
        table = tables.get(table_name)
        if table:
            pk = table.get("primaryKey")
            if isinstance(pk, list):
                table["primaryKey"] = [new if x == old else x for x in pk]
            for idx in table.get("indexes", []):
                if isinstance(idx, dict) and isinstance(idx.get("columns"), list):
                    idx["columns"] = [new if x == old else x for x in idx["columns"]]
        for idx in root.get("database", {}).get("indexes", []):
            if isinstance(idx, dict) and idx.get("table") == table_name and isinstance(idx.get("columns"), list):
                idx["columns"] = [new if x == old else x for x in idx["columns"]]

    def _selected_item(self) -> Optional[QTreeWidgetItem]:
        items = self.selectedItems()
        return items[0] if items else None

    def _on_context_menu(self, pos):
        item = self.itemAt(pos)
        menu = self._build_menu(item)
        if menu is not None:
            menu.exec(self.viewport().mapToGlobal(pos))

    def _build_menu(self, item):
        from PyQt6.QtWidgets import QMenu
        if item is None:
            return None
        menu = QMenu(self)
        path = self._path_of(item)
        value = get_by_path(self.model, path)
        if isinstance(value, (dict, list)):
            menu.addAction("Add…", lambda: self.add_child(item))
        else:
            menu.addAction("Edit…", lambda: self.edit_value(path))
        menu.addAction("Rename…", lambda: self.rename_item(item))
        menu.addAction("Delete", lambda: self.delete_item(item))
        return menu


# ---------------------------------------------------------------------------
# New-project wizard
# ---------------------------------------------------------------------------

class NewProjectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New FINN project")
        form = QFormLayout()

        self.template = QComboBox()
        for t in ("example", "minimal", "blank"):
            self.template.addItem(t)
        form.addRow("Template", self.template)

        self.name = QLineEdit()
        self.name.setPlaceholderText("optional project name (meta.name)")
        form.addRow("Project name", self.name)

        self.location = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.location)
        row.addWidget(browse)
        form.addRow("Location", row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.setLayout(form)

    def _browse(self):
        directory = QFileDialog.getExistingDirectory(self, "Choose directory")
        if directory:
            self.location.setText(directory)

    def values(self):
        return dict(
            template=self.template.currentText(),
            name=self.name.text().strip() or None,
            location=self.location.text().strip(),
        )


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, project_dir: Optional[str] = None):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1180, 760)

        self.model: Any = None
        self.config_path: Optional[Path] = None
        self.dirty = False
        self.version_readonly = False
        self.degraded_reason: Optional[str] = None
        self._validate_seq = 0
        self._last_diagnostics: List[Dict[str, Any]] = []
        self._has_errors = False
        self.run_state = "idle"  # idle|starting|listening|stopped|crashed
        self.run_port = 8000
        self.run_pid: Optional[int] = None
        self._stop_requested = False
        self.handshake_complete = False
        self._disk_sha: Optional[str] = None
        self._validating = False
        self._revalidate_pending = False
        self._current_probe: Optional[Path] = None
        self._pending_init_dir: Optional[str] = None

        self.engine = CliEngine(default_cli_command(), run_command(), self)
        self.engine.handshakeDone.connect(self._on_handshake)
        self.engine.opFinished.connect(self._on_op_finished)
        self.engine.runEvent.connect(self._on_run_event)
        self.engine.runLog.connect(self._on_run_log)
        self.engine.runExited.connect(self._on_run_exited)

        self._build_ui()
        self._build_toolbar()
        self._connect_signals()

        self._validate_timer = QTimer(self)
        self._validate_timer.setSingleShot(True)
        self._validate_timer.setInterval(400)
        self._validate_timer.timeout.connect(self._run_validate)

        self._health_timer = QTimer(self)
        self._health_timer.setInterval(2000)
        self._health_timer.timeout.connect(self._poll_health)

        self.engine.handshake()
        self._refresh_actions()

        if project_dir:
            self._open_project_path(project_dir)

    # -- UI construction -------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        layout = QVBoxLayout(central)

        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.hide()
        layout.addWidget(self.banner)

        self.tabs = QTabWidget()

        # --- Editor tab ---
        editor_tab = QWidget()
        el = QVBoxLayout(editor_tab)
        split = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.tree = JsonTreeWidget()
        self.tree.edited.connect(self._on_edited)
        ll.addWidget(self.tree)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(QLabel("Raw JSON (canonical preview — Apply parses it back):"))
        self.raw = QPlainTextEdit()
        self.raw.setFont(QFont("Monospace"))
        self.apply_raw = QPushButton("Apply raw JSON")
        self.apply_raw.clicked.connect(self._apply_raw)
        rl.addWidget(self.raw)
        rl.addWidget(self.apply_raw)
        split.addWidget(right)
        split.setSizes([560, 560])
        el.addWidget(split)
        self.tabs.addTab(editor_tab, "Editor")

        # --- Diagnostics tab ---
        diag_tab = QWidget()
        dl = QVBoxLayout(diag_tab)
        self.diag_summary = QLabel("Diagnostics")
        dl.addWidget(self.diag_summary)
        self.diag_tree = QTreeWidget()
        self.diag_tree.setColumnCount(4)
        self.diag_tree.setHeaderLabels(["Severity", "Code", "Path", "Message"])
        self.diag_tree.setRootIsDecorated(False)
        dl.addWidget(self.diag_tree)
        self.tabs.addTab(diag_tab, "Diagnostics")

        # --- Build tab ---
        build_tab = QWidget()
        bl = QVBoxLayout(build_tab)
        brow = QHBoxLayout()
        self.build_button = QPushButton("Build")
        self.build_button.clicked.connect(self._on_build)
        self.open_build_button = QPushButton("Open build folder")
        self.open_build_button.clicked.connect(self._open_build_folder)
        self.build_torun = QPushButton("Run ▶")
        self.build_torun.clicked.connect(self._goto_run)
        brow.addWidget(self.build_button)
        brow.addWidget(self.open_build_button)
        brow.addWidget(self.build_torun)
        brow.addStretch(1)
        bl.addLayout(brow)
        self.build_status = QLabel("No build yet.")
        self.build_status.setWordWrap(True)
        bl.addWidget(self.build_status)
        self.build_log = QPlainTextEdit()
        self.build_log.setFont(QFont("Monospace"))
        self.build_log.setReadOnly(True)
        bl.addWidget(self.build_log)
        self.tabs.addTab(build_tab, "Build")

        # --- Run tab ---
        run_tab = QWidget()
        rl2 = QVBoxLayout(run_tab)
        rrow = QHBoxLayout()
        rrow.addWidget(QLabel("Port:"))
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(8000)
        rrow.addWidget(self.port_spin)
        self.run_button = QPushButton("Start")
        self.run_button.clicked.connect(self._on_run_toggle)
        rrow.addWidget(self.run_button)
        rrow.addStretch(1)
        rl2.addLayout(rrow)
        links = QHBoxLayout()
        self.link_labels: List[Tuple[str, QPushButton]] = []
        for name in ("Open app", "Open /docs", "Open /redoc", "Open /healthz"):
            btn = QPushButton(name)
            btn.setEnabled(False)
            links.addWidget(btn)
            self.link_labels.append((name, btn))
        links.addStretch(1)
        rl2.addLayout(links)
        self.health_chip = QLabel("idle")
        rl2.addWidget(self.health_chip)
        self.run_console = QPlainTextEdit()
        self.run_console.setFont(QFont("Monospace"))
        self.run_console.setReadOnly(True)
        rl2.addWidget(self.run_console)
        self.tabs.addTab(run_tab, "Run")

        layout.addWidget(self.tabs)

        self.status = self.statusBar()

        self.setCentralWidget(central)
        self._wire_links()

    def _build_toolbar(self):
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self.open_action = QAction("Open…", self)
        self.open_action.triggered.connect(self._on_open)
        toolbar.addAction(self.open_action)

        self.new_action = QAction("New project…", self)
        self.new_action.triggered.connect(self._on_new_project)
        toolbar.addAction(self.new_action)

        toolbar.addSeparator()

        self.save_action = QAction("Save", self)
        self.save_action.setShortcut("Ctrl+S")
        self.save_action.triggered.connect(self._on_save)
        toolbar.addAction(self.save_action)

        self.validate_action = QAction("Validate now", self)
        self.validate_action.triggered.connect(self._run_validate)
        toolbar.addAction(self.validate_action)

        self.build_action = QAction("Build", self)
        self.build_action.triggered.connect(self._on_build)
        toolbar.addAction(self.build_action)

        self.run_action = QAction("Run", self)
        self.run_action.triggered.connect(self._on_run_toggle)
        toolbar.addAction(self.run_action)

    def _wire_links(self):
        for name, btn in self.link_labels:
            if name == "Open app":
                btn.clicked.connect(
                    lambda: self._open_url(self._app_url()))
            elif name == "Open /docs":
                btn.clicked.connect(lambda: self._open_url(self._base_url() + "/docs"))
            elif name == "Open /redoc":
                btn.clicked.connect(lambda: self._open_url(self._base_url() + "/redoc"))
            elif name == "Open /healthz":
                btn.clicked.connect(lambda: self._open_url(self._base_url() + "/healthz"))

    def _base_url(self) -> str:
        return "http://127.0.0.1:{}".format(self.run_port)

    def _app_url(self) -> str:
        base_path = "/"
        if isinstance(self.model, dict):
            api = self.model.get("api")
            if isinstance(api, dict) and isinstance(api.get("basePath"), str):
                bp = api["basePath"].strip()
                if bp and bp != "/":
                    base_path = bp if bp.startswith("/") else "/" + bp
        return self._base_url() + base_path

    def _open_url(self, url):
        import webbrowser
        webbrowser.open(url)

    # -- signal wiring ----------------------------------------------------

    def _connect_signals(self):
        pass

    # -- engine callbacks -------------------------------------------------

    def _on_handshake(self, status: dict):
        self.handshake_complete = True
        if status.get("ok"):
            self.degraded_reason = None
        else:
            self.degraded_reason = status.get("reason") or "CLI unavailable"
        self._update_banner()
        self._refresh_actions()

    def _on_op_finished(self, op, exit_code, stdout, stderr):
        if op == "validate":
            self._handle_validate_result(stdout, stderr)
        elif op == "build":
            self._handle_build_result(exit_code, stdout, stderr)
        elif op == "init":
            self._handle_init_result(exit_code, stdout, stderr)

    def _handle_validate_result(self, stdout, stderr):
        self._validating = False
        if self._current_probe is not None:
            try:
                if self._current_probe.exists():
                    self._current_probe.unlink()
            except OSError:
                pass
            self._current_probe = None
        try:
            doc = json.loads(stdout)
        except ValueError:
            self._set_diagnostics(
                [], ["validation output unparseable: " + stdout[:200]])
            return
        diagnostics = doc.get("diagnostics", []) if isinstance(doc, dict) else []
        self._set_diagnostics(diagnostics)
        if self._revalidate_pending:
            self._revalidate_pending = False
            self._run_validate()

    def _set_diagnostics(self, diagnostics, extra: Optional[List[str]] = None):
        self._last_diagnostics = list(diagnostics)
        self._has_errors = has_errors(diagnostics)
        self.diag_tree.setUpdatesEnabled(False)
        self.diag_tree.clear()
        for d in diagnostics:
            row = [
                str(d.get("severity", "")),
                str(d.get("code", "")),
                str(d.get("path") or ""),
                diagnostic_text(d),
            ]
            QTreeWidgetItem(self.diag_tree, row)
        for i in (0, 1, 2, 3):
            self.diag_tree.resizeColumnToContents(i)
        self.diag_tree.setUpdatesEnabled(True)
        errors = sum(1 for d in diagnostics if d.get("severity") == "error")
        warnings = sum(1 for d in diagnostics if d.get("severity") == "warning")
        if self.degraded_reason:
            self.diag_summary.setText("engine unavailable — no diagnostics")
        elif diagnostics:
            self.diag_summary.setText(
                "{} error(s), {} warning(s)".format(errors, warnings))
        else:
            self.diag_summary.setText("OK — no diagnostics")
        if extra:
            self.diag_summary.setText(self.diag_summary.text() + "  " + "; ".join(extra))
        self._refresh_actions()

    def _handle_build_result(self, exit_code, stdout, stderr):
        self.build_log.setPlainText(stderr)
        try:
            doc = json.loads(stdout)
        except ValueError:
            doc = None
        if exit_code == 0 and isinstance(doc, dict) and doc.get("ok"):
            artifacts = doc.get("artifacts", [])
            db = doc.get("db", {})
            dur = doc.get("durationMs", 0)
            lines = ["Build succeeded in {:.1f}s".format(dur / 1000.0)]
            for a in artifacts:
                lines.append("  {:<20} {:>9}  {}".format(
                    a.get("path", ""), a.get("bytes", 0), a.get("action", "")))
            lines.append("  database: {} ({})".format(
                db.get("path", ""), "created" if db.get("created") else "reused"))
            self.build_status.setText("\n".join(lines))
            self.build_status.setStyleSheet("")
        else:
            self.build_status.setText("Build failed.")
            self.build_status.setStyleSheet("color: #b00;")
            if isinstance(doc, dict):
                diags = doc.get("diagnostics", [])
                self._set_diagnostics(diags)
        self._refresh_actions()

    def _handle_init_result(self, exit_code, stdout, stderr):
        try:
            doc = json.loads(stdout)
        except ValueError:
            doc = None
        if exit_code == 0 and isinstance(doc, dict) and doc.get("ok"):
            created_path = self._pending_init_dir
            self._open_project_path(str(Path(created_path) / "finn.json"))
        else:
            reason = ""
            if isinstance(doc, dict):
                reason = "; ".join(diagnostic_text(d) for d in doc.get("diagnostics", []))
            if not reason:
                reason = stderr.strip() or "init failed (exit {})".format(exit_code)
            QMessageBox.warning(self, APP_NAME, "New project failed: {}".format(reason))

    # -- project loading / saving ----------------------------------------

    def _open_project_path(self, path: str):
        p = Path(path)
        if p.is_dir():
            cand = p / "finn.json"
            if not cand.exists():
                QMessageBox.warning(self, APP_NAME,
                                    "No finn.json in {}".format(p))
                return
            p = cand
        try:
            text = p.read_text(encoding="utf-8-sig")
            model = json.loads(text)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, APP_NAME,
                                "Could not open {}: {}".format(p, exc))
            return
        if not isinstance(model, dict):
            QMessageBox.warning(self, APP_NAME,
                                "config must be a JSON object")
            return
        if self.dirty and not self._confirm_discard():
            return
        self.config_path = p
        self.model = model
        self.dirty = False
        self._record_disk_state()
        self._sync_version_gate()
        self._on_model_loaded()
        self._update_banner()
        self._refresh_actions()

    def _on_model_loaded(self):
        self.tree.populate(self.model)
        self._refresh_raw()
        self._show_status()
        self._schedule_validate()

    def _refresh_raw(self):
        self.raw.setPlainText(canonical_json(self.model))

    def _apply_raw(self):
        if self.version_readonly:
            return
        text = self.raw.toPlainText()
        try:
            model = json.loads(text)
        except ValueError as exc:
            QMessageBox.warning(self, APP_NAME, "Invalid JSON: {}".format(exc))
            return
        if not isinstance(model, dict):
            QMessageBox.warning(self, APP_NAME, "Top level must be a JSON object")
            return
        self.model = model
        self._sync_version_gate()
        self.dirty = True
        self.tree.populate(self.model)
        self._update_banner()
        self._refresh_actions()
        self._schedule_validate()

    def _on_edited(self):
        self.dirty = True
        self._sync_version_gate()
        self._refresh_raw()
        self._update_banner()
        self._refresh_actions()
        self._schedule_validate()

    def _on_save(self) -> bool:
        if self.model is None or self.config_path is None:
            return False
        if self.version_readonly:
            QMessageBox.warning(
                self, APP_NAME,
                "This config uses a version other than 1 and is read-only.")
            return False
        if not self._confirm_external_overwrite():
            return False
        try:
            atomic_write_text(self.config_path, canonical_json(self.model))
        except OSError as exc:
            QMessageBox.warning(self, APP_NAME, "Save failed: {}".format(exc))
            return False
        self.dirty = False
        self._record_disk_state()
        self._refresh_actions()
        self._show_status()
        return True

    def _show_status(self):
        if self.config_path is None:
            self.status.showMessage("No project open")
            return
        parts = str(self.config_path)
        if self.dirty:
            parts += "  •"
        if self.version_readonly:
            parts += "  (read-only)"
        self.status.showMessage(parts)

    def _file_sha(self, path: Path) -> Optional[str]:
        try:
            with open(path, "rb") as handle:
                return hashlib.sha256(handle.read()).hexdigest()
        except OSError:
            return None

    def _record_disk_state(self) -> None:
        if self.config_path is not None:
            self._disk_sha = self._file_sha(self.config_path)

    def _sync_version_gate(self) -> None:
        self.version_readonly = (
            not isinstance(self.model, dict) or self.model.get("version") != 1)

    def _confirm_discard(self) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("Unsaved changes")
        box.setText("The current config has unsaved changes.")
        save = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked == save:
            return self._on_save()
        return clicked == discard

    def _confirm_external_overwrite(self) -> bool:
        if self.config_path is None:
            return True
        current = self._file_sha(self.config_path)
        if self._disk_sha is None or current == self._disk_sha:
            return True
        box = QMessageBox(self)
        box.setWindowTitle("File changed on disk")
        box.setText("The config file was modified outside FINN Studio.")
        overwrite = box.addButton("Overwrite", QMessageBox.ButtonRole.DestructiveRole)
        reload = box.addButton("Reload", QMessageBox.ButtonRole.ActionRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked == overwrite:
            return True
        if clicked == reload:
            self._reload_from_disk()
            return False
        return False

    def _reload_from_disk(self) -> None:
        if self.config_path is None:
            return
        try:
            text = self.config_path.read_text(encoding="utf-8-sig")
            model = json.loads(text)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, APP_NAME, "Could not reload: {}".format(exc))
            return
        if not isinstance(model, dict):
            QMessageBox.warning(self, APP_NAME, "config must be a JSON object")
            return
        self.model = model
        self.dirty = False
        self._record_disk_state()
        self._sync_version_gate()
        self._on_model_loaded()
        self._update_banner()
        self._refresh_actions()

    # -- validation -------------------------------------------------------

    def _schedule_validate(self):
        self._validate_timer.start()

    def _run_validate(self):
        if self.model is None or self.config_path is None:
            return
        if self.degraded_reason:
            self._set_diagnostics([])
            return
        if self._validating:
            self._revalidate_pending = True
            return
        self._validating = True
        self._revalidate_pending = False
        probe_path = self.config_path.with_name(
            ".finn-studio-validate-{}.json".format(os.getpid()))
        try:
            atomic_write_text(probe_path, canonical_json(self.model))
        except OSError as exc:
            self._validating = False
            self._set_diagnostics(
                [], ["cannot write validation probe: {}".format(exc)])
            return
        self._current_probe = probe_path
        self.engine.run_op("validate", cli_args_validate(str(probe_path)))

    # -- build ------------------------------------------------------------

    def _on_build(self):
        if self.model is None or self.config_path is None:
            return
        if self.degraded_reason:
            QMessageBox.warning(self, APP_NAME,
                                "Build requires the CLI: {}".format(self.degraded_reason))
            return
        if self.dirty:
            self._prompt_save_for(lambda: self._do_build())
        else:
            self._do_build()

    def _do_build(self):
        if self.config_path is None:
            return
        self.build_status.setText("Building…")
        self.build_status.setStyleSheet("")
        self.engine.run_op("build", cli_args_build(str(self.config_path)),
                           cwd=str(self.config_path.parent))

    def _open_build_folder(self):
        if self.config_path is None:
            return
        build_dir = self.config_path.parent / "build"
        if not build_dir.exists():
            QMessageBox.information(
                self, APP_NAME, "No build folder yet — build first.")
            return
        import webbrowser
        webbrowser.open("file://" + str(build_dir))

    def _goto_run(self):
        self.tabs.setCurrentIndex(3)

    # -- run --------------------------------------------------------------

    def _on_run_toggle(self):
        if self.engine.run_is_active() or self.run_state == "starting":
            self._stop_run()
        elif self.run_state == "listening":
            self._stop_run()
        else:
            self._start_run()

    def _start_run(self):
        if self.config_path is None:
            return
        if self.degraded_reason:
            QMessageBox.warning(self, APP_NAME,
                                "Run requires the CLI: {}".format(self.degraded_reason))
            return
        if self.dirty:
            self._prompt_save_for(self._do_start_run)
        else:
            self._do_start_run()

    def _do_start_run(self):
        if self.config_path is None:
            return
        self.run_port = self.port_spin.value()
        self.run_state = "starting"
        self.run_console.clear()
        self._append_console("Starting on 127.0.0.1:{} …".format(self.run_port))
        self.health_chip.setText("starting…")
        self._stop_requested = False
        self.engine.start_run(str(self.config_path), self.run_port)
        self._refresh_actions()

    def _stop_run(self):
        self._stop_requested = True
        self._append_console("Stopping (SIGTERM, 5s grace)…")
        self.engine.stop_run()

    def _on_run_event(self, event):
        name = event.get("event")
        if name == "building":
            self._append_console("Building…")
        elif name == "build_ok":
            self._append_console("Build OK")
        elif name == "listening":
            self.run_pid = event.get("pid")
            self.run_port = int(event.get("port", self.run_port))
            self.run_state = "listening"
            self.health_chip.setText("listening — checking health…")
            self._append_console("Listening on 127.0.0.1:{} (pid {})".format(
                self.run_port, self.run_pid))
            self._health_timer.start()
            self._refresh_links()
        elif name == "error":
            diag = event.get("diagnostic", {})
            self._append_console("ERROR: " + diagnostic_text(diag) if diag else "error")
            if self.run_state != "listening":
                self.run_state = "crashed"
                self._health_timer.stop()
                self.health_chip.setText("crashed")
        elif name == "stopped":
            self._append_console("Stopped (exit code {})".format(
                event.get("exitCode")))
        self._refresh_actions()

    def _on_run_log(self, text):
        self._append_console(text)

    def _on_run_exited(self, exit_code):
        self._health_timer.stop()
        self.run_pid = None
        if self._stop_requested:
            self.run_state = "stopped"
        elif exit_code != 0:
            self.run_state = "crashed"
        else:
            self.run_state = "stopped"
        if self.run_state == "crashed":
            self.health_chip.setText("crashed (exit {})".format(exit_code))
        else:
            self.health_chip.setText("stopped")
        self._append_console("Process exited (code {})".format(exit_code))
        self._refresh_links()
        self._refresh_actions()

    def _append_console(self, text):
        self.run_console.appendPlainText(text)

    def _refresh_links(self):
        listening = self.run_state == "listening"
        for _, btn in self.link_labels:
            btn.setEnabled(listening)

    def _poll_health(self):
        if self.run_state != "listening":
            return
        url = self._base_url() + "/healthz"
        try:
            with urllib.request.urlopen(url, timeout=0.6) as resp:
                ok = resp.status == 200
        except Exception:
            ok = False
        if ok:
            self.health_chip.setText("● healthy (200)")
            self.health_chip.setStyleSheet("color: #080;")
        else:
            self.health_chip.setText("● health check failed")
            self.health_chip.setStyleSheet("color: #b00;")

    # -- misc -------------------------------------------------------------

    def _on_open(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open config", "", "FINN config (finn.json);;JSON (*.json)")
        if path:
            self._open_project_path(path)

    def _on_new_project(self):
        if self.degraded_reason:
            QMessageBox.warning(self, APP_NAME,
                                "New project requires the CLI: {}".format(self.degraded_reason))
            return
        dlg = NewProjectDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dlg.values()
        if not vals["location"]:
            QMessageBox.warning(self, APP_NAME, "Choose a location")
            return
        target = Path(vals["location"]).expanduser()
        self._pending_init_dir = str(target)
        self.engine.run_op(
            "init",
            cli_args_init(str(target), vals["template"], vals["name"]))

    def _prompt_save_for(self, callback):
        box = QMessageBox(self)
        box.setWindowTitle("Unsaved changes")
        box.setText("The config has unsaved changes.")
        save = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
        save_and_go = box.addButton("Save and continue", QMessageBox.ButtonRole.ActionRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked == save:
            self._on_save()
        elif clicked == save_and_go:
            if self._on_save():
                callback()
        elif clicked == cancel:
            return

    def _update_banner(self):
        messages = []
        if self.degraded_reason:
            messages.append(
                "Engine unavailable: {}.  Editing and saving still work; "
                "init/build/run/validate are disabled.".format(self.degraded_reason))
        if self.version_readonly:
            messages.append(
                "This config uses a version other than 1; opened read-only.")
        if messages:
            self.banner.setText("  ".join(messages))
            self.banner.setStyleSheet(
                "background:#fff3cd; color:#664d03; padding:6px; border-radius:4px;")
            self.banner.show()
        else:
            self.banner.hide()

    def _refresh_actions(self):
        has_project = self.model is not None and self.config_path is not None
        can_engine = self.degraded_reason is None
        can_save = has_project and not self.version_readonly
        can_build = can_engine and has_project and not self.version_readonly and (not self._has_errors)
        can_run = can_build

        self.save_action.setEnabled(can_save)
        self.new_action.setEnabled(can_engine)
        self.validate_action.setEnabled(can_engine and has_project)
        self.build_action.setEnabled(can_engine and has_project and not self.version_readonly and not self.dirty and not self._has_errors)
        self.run_action.setEnabled(can_engine and has_project and not self.version_readonly and not self.dirty and not self._has_errors)

        self.build_button.setEnabled(can_build)
        self.open_build_button.setEnabled(has_project)
        self.build_torun.setEnabled(can_build)

        if self.run_state == "starting":
            self.run_button.setText("Stop")
            self.run_button.setEnabled(True)
        elif self.run_state == "listening":
            self.run_button.setText("Stop")
            self.run_button.setEnabled(True)
        else:
            self.run_button.setText("Start")
            self.run_button.setEnabled(can_engine and has_project and not self.version_readonly and not self.dirty and not self._has_errors)
        self.port_spin.setEnabled(self.run_state in ("idle", "stopped", "crashed"))

        self.tree.setEnabled(has_project and not self.version_readonly)
        self.raw.setEnabled(has_project and not self.version_readonly)
        self.apply_raw.setEnabled(has_project and not self.version_readonly)

        self._show_status()

    def closeEvent(self, event):
        if self.engine.run_is_active():
            self.engine.stop_run()
        if self.dirty:
            box = QMessageBox(self)
            box.setWindowTitle("Unsaved changes")
            box.setText("Discard unsaved changes?")
            save = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
            discard = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
            cancel = box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            clicked = box.clickedButton()
            if clicked == save:
                if not self._on_save():
                    event.ignore()
                    return
            elif clicked == cancel:
                event.ignore()
                return
        event.accept()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    project_dir = argv[0] if argv else None

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    window = MainWindow(project_dir)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
