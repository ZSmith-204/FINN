"""Parsing and validation of FINN's ``api`` config block (README §5).

This module models the HTTP surface only: endpoints, operations, parameter
sources, and response shaping.  It is standard-library only and has no
FastAPI/HTTP-runtime dependency; the generated ``API.py`` is produced by
:mod:`api.codegen`.

Validation is strict and fail-fast: unknown methods/keys, DDL statements,
positional ``?`` placeholders, and unresolvable parameters raise
:class:`database.ConfigError` carrying the JSON path of the offending
element, e.g. ``api.endpoints./users.get.sql``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from database import CANONICAL_TYPES, ConfigError, normalize_type

__all__ = [
    "METHODS",
    "ApiSpec",
    "Endpoint",
    "Operation",
    "Param",
    "Response",
    "parse_api",
    "statement_kind",
    "extract_params",
    "split_statements",
]

LOGGER = logging.getLogger("api")

METHODS: Tuple[str, ...] = ("get", "post", "put", "patch", "delete")

# FastAPI derives these from ``get``; configuring them is ignored (README §5.1).
_IGNORED_METHODS = ("head", "options")

_API_KEYS = ("basePath", "endpoints", "cors")
_OPERATION_KEYS = (
    "sql",
    "params",
    "response",
    "description",
    "authentication",
    "rateLimit",
    "multi",
)
_PARAM_KEYS = ("source", "type", "required", "default")
_RESPONSE_KEYS = (
    "shape",
    "key",
    "columns",
    "renames",
    "paginate",
    "blob",
    "blobFormat",
)
_PARAM_SOURCES = ("path", "query", "body", "header")
_SHAPES = ("list", "single", "none")

_KEYWORD_KINDS = {
    "SELECT": "SELECT",
    "WITH": "SELECT",
    "INSERT": "INSERT",
    "REPLACE": "INSERT",
    "UPDATE": "UPDATE",
    "DELETE": "DELETE",
}

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_IDENT_START = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
)
_IDENT_CONT = _IDENT_START | frozenset("0123456789")
_PATH_SEGMENT_RE = re.compile(r"\{([^{}]*)\}")

# Bind parameters reserved for pagination (README §6.4).
_RESERVED_PAGINATION_PARAMS = ("__pageSize", "__offset")

# Names that collide with injected generated-function arguments/locals
# (README §5.3).  Declaring one would shadow or duplicate a generated name.
_RESERVED_NAMES = frozenset(
    ("page", "pageSize", "payload", "params", "result", "_value")
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Param:
    """One normalized operation parameter."""

    name: str
    source: str
    fin_type: Optional[str] = None
    required: bool = False
    default: object = None


@dataclass
class Response:
    """Response shaping for a SELECT operation (README §6.2)."""

    shape: str = "list"
    key: Optional[str] = None
    columns: Optional[List[str]] = None
    renames: Optional[Dict[str, str]] = None
    paginate: bool = False
    blob_format: Optional[str] = None


@dataclass
class Operation:
    """One HTTP method bound to a SQL statement."""

    method: str
    sql: str
    params: Dict[str, Param] = field(default_factory=dict)
    response: Response = field(default_factory=Response)
    multi: bool = False
    description: Optional[str] = None
    authentication: Optional[str] = None
    rate_limit: object = None


@dataclass
class Endpoint:
    """One path and its method-keyed operations."""

    path: str
    operations: Dict[str, Operation] = field(default_factory=dict)


@dataclass
class ApiSpec:
    """The fully validated, normalized ``api`` block."""

    base_path: str = "/"
    endpoints: Dict[str, Endpoint] = field(default_factory=dict)
    cors: object = None


# ---------------------------------------------------------------------------
# SQL scanning (README §5.3 / §5.4)
# ---------------------------------------------------------------------------


def _skip_leading(sql: str) -> int:
    """Return the index of the first token, skipping whitespace/comments."""

    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char.isspace():
            index += 1
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = length if newline == -1 else newline + 1
            continue
        if sql.startswith("/*", index):
            close = sql.find("*/", index + 2)
            index = length if close == -1 else close + 2
            continue
        if char == "(":
            index += 1
            continue
        break
    return index


def statement_kind(sql: str) -> str:
    """Classify a SQL statement by its first keyword (README §5.5).

    ``WITH`` is treated as ``SELECT`` (CTE) and ``REPLACE`` as ``INSERT``.
    Anything other than SELECT/INSERT/UPDATE/DELETE (including DDL) is
    ``"OTHER"``.
    """

    if not isinstance(sql, str):
        return "OTHER"
    index = _skip_leading(sql)
    if index >= len(sql):
        return "OTHER"
    match = _IDENT_RE.match(sql, index)
    if match is None:
        return "OTHER"
    keyword = match.group(0).upper()
    return _KEYWORD_KINDS.get(keyword, "OTHER")


def _scan_sql(sql: str, path: str) -> Tuple[List[str], List[str]]:
    """Scan ``sql`` for named bind params and statements.

    Skips single-quoted strings, double-quoted identifiers, bracket
    identifiers, ``--`` line comments, and ``/* ... */`` block comments.  A
    bare ``?`` outside those contexts is a config error (README §5.3).
    Returns ``(parameter_names, statements)`` with deduped names in order.
    """

    names: List[str] = []
    seen = set()
    statements: List[str] = []
    start = 0
    index = 0
    length = len(sql)

    while index < length:
        char = sql[index]

        if char == "'":
            index += 1
            while index < length:
                if sql[index] == "'":
                    if index + 1 < length and sql[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue

        if char == '"':
            index += 1
            while index < length:
                if sql[index] == '"':
                    if index + 1 < length and sql[index + 1] == '"':
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue

        if char == "[":
            close = sql.find("]", index + 1)
            index = length if close == -1 else close + 1
            continue

        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = length if newline == -1 else newline + 1
            continue

        if sql.startswith("/*", index):
            close = sql.find("*/", index + 2)
            index = length if close == -1 else close + 2
            continue

        if char == "?":
            raise ConfigError(
                path,
                "positional parameter '?' is not allowed; use named "
                "parameters (:name, @name, or $name) per README §5.3",
            )

        if char in ":@$":
            cursor = index + 1
            if cursor < length and sql[cursor] in _IDENT_START:
                cursor += 1
                while cursor < length and sql[cursor] in _IDENT_CONT:
                    cursor += 1
                name = sql[index + 1 : cursor]
                if name not in seen:
                    seen.add(name)
                    names.append(name)
                index = cursor
                continue
            index += 1
            continue

        if char == ";":
            statement = sql[start:index].strip()
            if statement:
                statements.append(statement)
            start = index + 1
            index += 1
            continue

        index += 1

    tail = sql[start:].strip()
    if tail:
        statements.append(tail)
    return names, statements


def extract_params(sql: str) -> List[str]:
    """Return the deduped ``:name``/``@name``/``$name`` names in ``sql``."""

    names, _ = _scan_sql(sql, "api.sql")
    return names


def split_statements(sql: str) -> List[str]:
    """Split ``sql`` on top-level ``;`` (exposed internally for codegen)."""

    _, statements = _scan_sql(sql, "api.sql")
    return statements


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _check_keys(block: dict, allowed: Tuple[str, ...], path: str) -> None:
    for key in block:
        if key not in allowed:
            raise ConfigError(f"{path}.{key}", f"unknown key {key!r}")


def _require_object(value: object, path: str, what: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(path, f"{what} must be a JSON object")
    return value


def _require_bool(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(path, f"must be a boolean, got {type(value).__name__}")
    return value


def _require_string(value: object, path: str, what: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(path, f"{what} must be a string")
    return value


def _path_segments(path: str) -> List[str]:
    """Return ``{segment}`` names in path order (validated as identifiers)."""

    segments: List[str] = []
    for name in _PATH_SEGMENT_RE.findall(path):
        if not _IDENT_RE.fullmatch(name):
            raise ConfigError(
                f"api.endpoints.{path}",
                f"invalid path parameter segment {{{name}}}; expected an "
                "identifier such as {id}",
            )
        if name in _RESERVED_NAMES:
            raise ConfigError(
                f"api.endpoints.{path}",
                f"path parameter segment {{{name}}} is reserved and collides "
                "with a generated name; reserved names are "
                f"{', '.join(sorted(_RESERVED_NAMES))} (README §5.3)",
            )
        if name not in segments:
            segments.append(name)
    return segments


# ---------------------------------------------------------------------------
# Parsing (README §5)
# ---------------------------------------------------------------------------


def _parse_param(name: str, raw_param: object, path: str) -> Param:
    if not _IDENT_RE.fullmatch(name):
        raise ConfigError(path, f"invalid parameter name {name!r}")
    if name in _RESERVED_NAMES:
        raise ConfigError(
            path,
            f"parameter name {name!r} is reserved and collides with a "
            "generated name; reserved names are "
            f"{', '.join(sorted(_RESERVED_NAMES))} (README §5.3)",
        )
    raw = _require_object(raw_param, path, "parameter definition")
    _check_keys(raw, _PARAM_KEYS, path)

    if "source" not in raw:
        raise ConfigError(f"{path}.source", "is required")
    source = raw["source"]
    if not isinstance(source, str):
        raise ConfigError(f"{path}.source", "must be a string")
    source = source.strip().lower()
    if source not in _PARAM_SOURCES:
        raise ConfigError(
            f"{path}.source",
            f"invalid source {source!r}; expected one of "
            f"{', '.join(_PARAM_SOURCES)}",
        )

    required = raw.get("required", False)
    if required is None:
        required = False
    required = _require_bool(required, f"{path}.required")

    fin_type: Optional[str] = None
    if raw.get("type") is not None:
        raw_type = raw["type"]
        if not isinstance(raw_type, str):
            raise ConfigError(f"{path}.type", "must be a string")
        fin_type = normalize_type(raw_type)
        if fin_type is None:
            raise ConfigError(
                f"{path}.type",
                f"unknown type {raw_type!r}; expected one of "
                f"{', '.join(sorted(CANONICAL_TYPES))}",
            )

    return Param(
        name=name,
        source=source,
        fin_type=fin_type,
        required=required,
        default=raw.get("default"),
    )


def _parse_response(raw_response: object, path: str, kind: str) -> Response:
    if raw_response is None:
        return Response()
    raw = _require_object(raw_response, path, "response definition")
    _check_keys(raw, _RESPONSE_KEYS, path)

    shape = raw.get("shape", "list")
    if shape is None:
        shape = "list"
    shape = _require_string(shape, f"{path}.shape", "shape").strip().lower()
    if shape not in _SHAPES:
        raise ConfigError(
            f"{path}.shape",
            f"invalid shape {shape!r}; expected one of {', '.join(_SHAPES)}",
        )

    columns: Optional[List[str]] = None
    if raw.get("columns") is not None:
        raw_columns = raw["columns"]
        if not isinstance(raw_columns, list):
            raise ConfigError(f"{path}.columns", "must be an array of column names")
        columns = []
        for position, column in enumerate(raw_columns):
            if not isinstance(column, str) or not column.strip():
                raise ConfigError(
                    f"{path}.columns.{position}", "must be a non-empty string"
                )
            columns.append(column.strip())

    renames: Optional[Dict[str, str]] = None
    if raw.get("renames") is not None:
        raw_renames = raw["renames"]
        if not isinstance(raw_renames, dict):
            raise ConfigError(f"{path}.renames", "must be an object of name -> name")
        renames = {}
        for source_name, target_name in raw_renames.items():
            if not isinstance(source_name, str) or not source_name.strip():
                raise ConfigError(
                    f"{path}.renames", "keys must be non-empty strings"
                )
            if not isinstance(target_name, str) or not target_name.strip():
                raise ConfigError(
                    f"{path}.renames.{source_name}",
                    "must be a non-empty string",
                )
            renames[source_name.strip()] = target_name.strip()

    paginate = raw.get("paginate", False)
    if paginate is None:
        paginate = False
    paginate = _require_bool(paginate, f"{path}.paginate")
    if paginate:
        if shape != "list":
            raise ConfigError(
                f"{path}.paginate",
                'pagination requires response.shape "list" (README §6.4)',
            )
        if kind != "SELECT":
            raise ConfigError(
                f"{path}.paginate",
                "pagination requires a SELECT statement (README §6.4)",
            )

    key: Optional[str] = None
    if raw.get("key") is not None:
        key = _require_string(raw["key"], f"{path}.key", "key").strip()
        if not key:
            raise ConfigError(f"{path}.key", "must not be empty")
    if shape != "single":
        key = None

    blob_format = raw.get("blob", raw.get("blobFormat"))
    if blob_format is not None and not isinstance(blob_format, str):
        raise ConfigError(f"{path}.blob", "must be a string format marker")

    return Response(
        shape=shape,
        key=key,
        columns=columns,
        renames=renames,
        paginate=paginate,
        blob_format=blob_format,
    )


def _parse_operation(path: str, method: str, raw_operation: object) -> Operation:
    base = f"api.endpoints.{path}.{method}"
    sql_path = f"{base}.sql"

    if isinstance(raw_operation, str):
        operation_def = {"sql": raw_operation}
    else:
        operation_def = _require_object(raw_operation, base, "operation definition")
    _check_keys(operation_def, _OPERATION_KEYS, base)

    if "sql" not in operation_def:
        raise ConfigError(sql_path, "is required")
    sql = operation_def["sql"]
    if not isinstance(sql, str):
        raise ConfigError(sql_path, "must be a string")
    if not sql.strip():
        raise ConfigError(sql_path, "must not be empty")

    multi = operation_def.get("multi", False)
    if multi is None:
        multi = False
    multi = _require_bool(multi, f"{base}.multi")

    names, statements = _scan_sql(sql, sql_path)
    if not statements:
        raise ConfigError(sql_path, "must contain a SQL statement")
    kinds = [statement_kind(statement) for statement in statements]
    if any(statement_kind_value == "OTHER" for statement_kind_value in kinds):
        raise ConfigError(
            sql_path,
            "unsupported SQL statement; only SELECT/INSERT/UPDATE/DELETE are "
            "allowed in endpoints (DDL is a config error, README §5.5)",
        )
    if len(statements) > 1 and not multi:
        raise ConfigError(
            sql_path,
            'multiple ;-separated statements require "multi": true '
            "(README §5.4)",
        )
    kind = kinds[0]

    declared: Dict[str, Param] = {}
    raw_params = operation_def.get("params")
    if raw_params is not None:
        raw_params = _require_object(raw_params, f"{base}.params", "params")
        for raw_name, raw_param in raw_params.items():
            if not isinstance(raw_name, str):
                raise ConfigError(f"{base}.params", "parameter names must be strings")
            declared[raw_name] = _parse_param(
                raw_name, raw_param, f"{base}.params.{raw_name}"
            )

    response = _parse_response(operation_def.get("response"), f"{base}.response", kind)

    path_names = _path_segments(path)
    for name in path_names:
        if name in declared and declared[name].source != "path":
            raise ConfigError(
                f"{base}.params.{name}",
                f"path parameter {{{name}}} is implicit in the endpoint path "
                "and must not be declared with a non-path source (README §5.3)",
            )
    for name, param in declared.items():
        if param.source == "path" and name not in path_names:
            raise ConfigError(
                f"{base}.params.{name}",
                f"declared path parameter {name!r} does not match any "
                "{segment} in the endpoint path (README §5.3)",
            )

    if response.paginate:
        if multi:
            raise ConfigError(
                f"{base}.response.paginate",
                'pagination cannot be combined with "multi": true (README §5.4/§6.4)',
            )
        reserved = set(_RESERVED_PAGINATION_PARAMS)
        if any(name in reserved for name in names) or any(
            name in reserved for name in declared
        ):
            raise ConfigError(
                sql_path,
                "the bind parameters :__pageSize and :__offset are reserved "
                "for pagination and must not be declared (README §6.4)",
            )
        if any(name in ("page", "pageSize") for name in names) or any(
            name in ("page", "pageSize") for name in declared
        ):
            raise ConfigError(
                f"{base}.params",
                "the query parameters 'page' and 'pageSize' are reserved by "
                "pagination (README §6.4)",
            )

    params: Dict[str, Param] = {}
    for name in path_names:
        declared_param = declared.get(name)
        if declared_param is not None:
            params[name] = Param(
                name=name,
                source="path",
                fin_type=declared_param.fin_type,
                required=True,
                default=declared_param.default,
            )
        else:
            params[name] = Param(name=name, source="path", required=True)
    for name in names:
        if name in params:
            continue
        if name in declared:
            params[name] = declared[name]
        else:
            source = "query" if method in ("get", "delete") else "body"
            params[name] = Param(name=name, source=source)
    for name, param in declared.items():
        if name not in params:
            params[name] = param

    for name in params:
        if name in _RESERVED_NAMES:
            raise ConfigError(
                f"{base}.params.{name}",
                f"parameter name {name!r} is reserved and collides with a "
                "generated name; reserved names are "
                f"{', '.join(sorted(_RESERVED_NAMES))} (README §5.3)",
            )

    description = operation_def.get("description")
    if description is not None and not isinstance(description, str):
        raise ConfigError(f"{base}.description", "must be a string")

    return Operation(
        method=method,
        sql=sql,
        params=params,
        response=response,
        multi=multi,
        description=description,
        authentication=operation_def.get("authentication"),
        rate_limit=operation_def.get("rateLimit"),
    )


def _parse_endpoint(path: str, raw_endpoint: object) -> Endpoint:
    base = f"api.endpoints.{path}"
    raw = _require_object(raw_endpoint, base, "endpoint definition")

    operations: Dict[str, Operation] = {}
    for raw_method, raw_operation in raw.items():
        if not isinstance(raw_method, str):
            raise ConfigError(base, "HTTP method keys must be strings")
        method = raw_method.strip().lower()
        if method in _IGNORED_METHODS:
            LOGGER.warning(
                "ignoring %s on %s: HEAD/OPTIONS are derived by FastAPI "
                "(README §5.1)",
                raw_method,
                path,
            )
            continue
        if method not in METHODS:
            raise ConfigError(
                f"{base}.{raw_method}",
                f"unknown HTTP method {raw_method!r}; expected one of "
                f"{', '.join(METHODS)}",
            )
        if method in operations:
            raise ConfigError(
                f"{base}.{method}", f"duplicate method {method!r}"
            )
        operations[method] = _parse_operation(path, method, raw_operation)

    return Endpoint(path=path, operations=operations)


def parse_api(api_config: dict) -> ApiSpec:
    """Validate and normalize the config's ``api`` object (README §5)."""

    if not isinstance(api_config, dict):
        raise ConfigError("api", "must be a JSON object")
    _check_keys(api_config, _API_KEYS, "api")

    base_path = api_config.get("basePath", "/")
    if base_path is None:
        base_path = "/"
    base_path = _require_string(base_path, "api.basePath", "basePath").strip()
    if not base_path:
        base_path = "/"
    if not base_path.startswith("/"):
        base_path = "/" + base_path
    if len(base_path) > 1:
        base_path = base_path.rstrip("/") or "/"

    # ``cors`` is a documented reserved key (README §9) that is accepted and
    # ignored for now; keep it on the spec for introspection.
    cors = api_config.get("cors")

    raw_endpoints = api_config.get("endpoints")
    if raw_endpoints is None:
        raise ConfigError("api.endpoints", "is required")
    raw_endpoints = _require_object(
        raw_endpoints, "api.endpoints", "endpoints"
    )

    endpoints: Dict[str, Endpoint] = {}
    for raw_path, raw_endpoint in raw_endpoints.items():
        if not isinstance(raw_path, str):
            raise ConfigError("api.endpoints", "endpoint keys must be strings")
        path = raw_path.strip()
        if not path:
            raise ConfigError("api.endpoints", "endpoint paths must not be empty")
        if not path.startswith("/"):
            path = "/" + path
        if path in endpoints:
            raise ConfigError(
                f"api.endpoints.{path}",
                f"duplicate endpoint path {path!r}",
            )
        endpoints[path] = _parse_endpoint(path, raw_endpoint)

    return ApiSpec(base_path=base_path, endpoints=endpoints, cors=cors)