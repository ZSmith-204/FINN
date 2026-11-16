"""Generation of the standalone ``API.py`` and ``main.py`` modules (§8.1/§8.3).

The generated ``API.py`` imports only the Python standard library, FastAPI,
and the generated ``DB.py`` data layer; it never imports ``finn``.  This
generator itself is standard-library only and never imports FastAPI.

Route behavior mirrors the spec:

* parameters are bound from path/query/header/JSON body sources (§5.3),
* SELECT/INSERT/UPDATE/DELETE shape their responses per §5.5,
* ``paginate`` emits the count/page query pair and envelope of §6.4,
* errors use the uniform ``{"error": {...}}`` body of §7.
"""

from __future__ import annotations

import mimetypes
import os
import re
from typing import List, Optional

from database import ConfigError

from .schema import (
    ApiSpec,
    Operation,
    Param,
    StaticTarget,
    _RESERVED_NAMES,
    _scan_sql,
    statement_kind,
)

__all__ = ["render_api_module", "render_main_module"]

_PY_TYPES = {
    "integer": "int",
    "real": "float",
    "boolean": "bool",
    "text": "str",
    "datetime": "str",
    "blob": "str",
    "json": "Any",
}

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PATH_SEGMENT_RE = re.compile(r"\{([^{}]*)\}")
_RESERVED_PAGINATION_PARAMS = ("__pageSize", "__offset")

_MODULE_HEADER = '''"""FINN-generated HTTP layer (reference.md §5, §6, §7, §8.3).

One route per configured endpoint/operation.  SQL is parameterized; this
module imports the generated data layer (``DB.py``) and no FINN runtime.
"""

from __future__ import annotations

import math
import sqlite3
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Header, Path, Query
from fastapi.responses import JSONResponse, Response

from DB import (
    DB_PATH,
    ValidationError,
    coerce,
    connect,
    map_sqlite_error,
    query,
    serialize_rows,
)

router = APIRouter()


def _fail(status: int, code: str, message: str, detail: object = None) -> JSONResponse:
    """Build the uniform FINN error body (reference.md §7)."""
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "detail": detail}},
    )


def _coerce(name, fin_type, value):
    """Coerce ``value`` with the data layer; return a 400 response on failure."""
    if fin_type is None or value is None:
        return value
    try:
        return coerce(fin_type, value)
    except ValidationError as exc:
        return _fail(
            400,
            "VALIDATION_ERROR",
            "Invalid value for parameter %r." % (name,),
            str(exc),
        )'''

_MAIN_TEMPLATE = '''"""FINN-generated FastAPI entrypoint (reference.md §8.1)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
__STATIC_IMPORTS__
from API import router

__STATIC_ROOT__
app = FastAPI()
app.include_router(router__PREFIX__)


@app.exception_handler(RequestValidationError)
async def _validation_handler(request, exc):
    """Return the FINN error contract for request validation (reference.md §7)."""
    first = exc.errors()[0] if exc.errors() else {}
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed.",
                "detail": exc.errors(),
            }
        },
    )


@app.get("/healthz")
def healthz():
    """Basic liveness probe (reference.md §10.4)."""
    return {"status": "ok"}
__STATIC__
__all__ = ["app"]
'''


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _py_type(fin_type: Optional[str]) -> str:
    return _PY_TYPES.get(fin_type or "", "str")


def _route_name(method: str, path: str, used: set) -> str:
    slug = re.sub(r"[^0-9A-Za-z]+", "_", path).strip("_")
    name = f"{method}_{slug}" if slug else f"{method}_root"
    if not name[0].isalpha() and name[0] != "_":
        name = "op_" + name
    base = name
    counter = 1
    while name in used:
        counter += 1
        name = f"{base}_{counter}"
    used.add(name)
    return name


def _path_segments(path: str) -> List[str]:
    return _PATH_SEGMENT_RE.findall(path)


def _bind_lines(
    param: Param, expression: str, *, indent: str = "    "
) -> List[str]:
    """Emit lines binding ``expression`` into ``params[param.name]``."""

    name = param.name
    if param.fin_type is None:
        return [f"{indent}params[{name!r}] = {expression}"]
    return [
        f"{indent}_value = _coerce({name!r}, {param.fin_type!r}, {expression})",
        f"{indent}if isinstance(_value, JSONResponse):",
        f"{indent}    return _value",
        f"{indent}params[{name!r}] = _value",
    ]


def _validate_operation(path: str, method: str, operation: Operation) -> str:
    """Re-validate a (possibly hand-built) operation at render time.

    Returns the statement kind.  Parse-time validation already covers config
    input; this guards direct ``ApiSpec`` construction and keeps ``generate``
    failures path-scoped as :class:`ConfigError`.
    """

    sql_path = f"api.endpoints.{path}.{method}.sql"
    if not isinstance(operation.sql, str) or not operation.sql.strip():
        raise ConfigError(sql_path, "must be a non-empty string")
    names, statements = _scan_sql(operation.sql, sql_path)
    if not statements:
        raise ConfigError(sql_path, "must contain a SQL statement")
    kinds = [statement_kind(statement) for statement in statements]
    if any(kind == "OTHER" for kind in kinds):
        raise ConfigError(
            sql_path,
            "unsupported SQL statement; only SELECT/INSERT/UPDATE/DELETE are "
            "allowed in endpoints (reference.md §5.5)",
        )
    if len(statements) > 1 and not operation.multi:
        raise ConfigError(
            sql_path,
            'multiple ;-separated statements require "multi": true '
            "(reference.md §5.4)",
        )
    kind = kinds[0]

    if not isinstance(operation.response.shape, str) or operation.response.shape not in (
        "list",
        "single",
        "none",
    ):
        raise ConfigError(
            f"api.endpoints.{path}.{method}.response.shape",
            "must be one of list, single, none",
        )
    if operation.response.paginate:
        if operation.multi:
            raise ConfigError(
                f"api.endpoints.{path}.{method}.response.paginate",
                "pagination cannot be combined with multi-statement "
                "operations (reference.md §5.4/§6.4)",
            )
        if operation.response.shape != "list":
            raise ConfigError(
                f"api.endpoints.{path}.{method}.response.paginate",
                'pagination requires response.shape "list" (reference.md §6.4)',
            )
        if kind != "SELECT":
            raise ConfigError(
                f"api.endpoints.{path}.{method}.response.paginate",
                "pagination requires a SELECT statement (reference.md §6.4)",
            )
        reserved = set(_RESERVED_PAGINATION_PARAMS)
        if any(name in reserved for name in names) or any(
            name in reserved for name in operation.params
        ):
            raise ConfigError(
                sql_path,
                "the bind parameters :__pageSize and :__offset are reserved "
                "for pagination (reference.md §6.4)",
            )
        if any(name in ("page", "pageSize") for name in operation.params):
            raise ConfigError(
                f"api.endpoints.{path}.{method}.params",
                "the query parameters 'page' and 'pageSize' are reserved by "
                "pagination (reference.md §6.4)",
            )

    for param in operation.params.values():
        if not _IDENT_RE.fullmatch(param.name):
            raise ConfigError(
                f"api.endpoints.{path}.{method}.params",
                f"invalid parameter name {param.name!r}",
            )
        if param.name in _RESERVED_NAMES:
            raise ConfigError(
                f"api.endpoints.{path}.{method}.params.{param.name}",
                f"parameter name {param.name!r} is reserved and collides "
                "with a generated name (reference.md §5.3)",
            )
        if param.source not in ("path", "query", "body", "header"):
            raise ConfigError(
                f"api.endpoints.{path}.{method}.params.{param.name}",
                f"invalid parameter source {param.source!r}",
            )
        if param.source == "path" and param.name not in _path_segments(path):
            raise ConfigError(
                f"api.endpoints.{path}.{method}.params.{param.name}",
                "path parameters must match a {segment} in the endpoint path",
            )
    for segment in _path_segments(path):
        if segment in _RESERVED_NAMES:
            raise ConfigError(
                f"api.endpoints.{path}.{method}",
                f"path parameter segment {{{segment}}} is reserved and "
                "collides with a generated name (reference.md §5.3)",
            )
    return kind


# ---------------------------------------------------------------------------
# Route rendering
# ---------------------------------------------------------------------------


def _render_route(
    name: str, path: str, method: str, operation: Operation
) -> str:
    kind = _validate_operation(path, method, operation)
    response = operation.response
    segments = _path_segments(path)

    path_params = [p for p in operation.params.values() if p.source == "path"]
    query_params = [p for p in operation.params.values() if p.source == "query"]
    body_params = [p for p in operation.params.values() if p.source == "body"]
    header_params = [p for p in operation.params.values() if p.source == "header"]

    # --- signature ---------------------------------------------------------
    signature: List[str] = []
    seen_path = set()
    for segment in segments:
        if segment in seen_path:
            continue
        seen_path.add(segment)
        declared = operation.params.get(segment)
        fin_type = declared.fin_type if declared is not None else None
        signature.append(f"{segment}: {_py_type(fin_type)}")

    for param in query_params:
        py_type = _py_type(param.fin_type)
        if param.required:
            signature.append(f"{param.name}: {py_type} = Query(...)")
        elif param.default is not None:
            signature.append(
                f"{param.name}: Optional[{py_type}] = Query({param.default!r})"
            )
        else:
            signature.append(f"{param.name}: Optional[{py_type}] = None")

    if response.paginate:
        signature.append("page: int = Query(1, ge=1)")
        signature.append("pageSize: int = Query(50, ge=1, le=1000)")

    for param in header_params:
        py_type = _py_type(param.fin_type)
        alias = param.name.replace("_", "-")
        signature.append(
            f"{param.name}: Optional[{py_type}] = Header(None, alias={alias!r})"
        )

    if body_params:
        signature.append("payload: Dict[str, Any] = Body(None)")

    # --- body --------------------------------------------------------------
    decorator = f"@router.{method}({path!r}"
    if operation.description:
        decorator += f", description={operation.description!r}"
    decorator += ")"
    lines: List[str] = [decorator]
    lines.append(f"def {name}({', '.join(signature)}):")
    lines.append('    """Generated route (reference.md §5)."""')
    lines.append("    params = {}")

    # Path parameters are always present (FastAPI guarantees it, §5.3).
    for segment in segments:
        param = operation.params.get(segment)
        if param is None:
            continue
        lines.extend(_bind_lines(param, segment))

    for param in query_params:
        if param.required:
            # ``Query(...)`` guarantees presence; bind directly.
            lines.extend(_bind_lines(param, param.name))
        else:
            lines.append(f"    if {param.name} is not None:")
            lines.extend(_bind_lines(param, param.name, indent="        "))
            lines.append("    else:")
            lines.append(f"        params[{param.name!r}] = None")

    for param in header_params:
        if param.required:
            lines.append(f"    if {param.name} is None:")
            lines.append(
                "        return _fail(400, \"VALIDATION_ERROR\", "
                f"'Missing required header parameter {param.name}.'"
                ")"
            )
        lines.append(f"    if {param.name} is not None:")
        lines.extend(_bind_lines(param, param.name, indent="        "))
        lines.append("    else:")
        lines.append(f"        params[{param.name!r}] = None")

    if body_params:
        lines.append("    payload = payload or {}")
        for param in body_params:
            lines.append(f"    if {param.name!r} in payload:")
            lines.extend(
                _bind_lines(param, f"payload[{param.name!r}]", indent="        ")
            )
            if param.required:
                lines.append("    else:")
                lines.append(
                    "        return _fail(400, \"VALIDATION_ERROR\", "
                    f"'Missing required body field {param.name}.'"
                    ")"
                )
            elif param.default is not None:
                lines.append("    else:")
                lines.append(f"        params[{param.name!r}] = {param.default!r}")
            else:
                lines.append("    else:")
                lines.append(f"        params[{param.name!r}] = None")

    # --- execution ---------------------------------------------------------
    statements = _scan_sql(
        operation.sql, f"api.endpoints.{path}.{method}.sql"
    )[1]

    if operation.multi:
        lines.append("    conn = connect()")
        lines.append("    try:")
        lines.append("        _total = 0")
        lines.append("        _last_rowid = None")
        lines.append("        _rows = []")
        lines.append("        for _s in [")
        for statement in statements:
            lines.append(f"            {statement!r},")
        lines.append("        ]:")
        lines.append("            _cur = conn.execute(_s, params)")
        lines.append("            if _cur.description is not None:")
        lines.append(
            "                _rows = [dict(zip([c[0] for c in "
            "_cur.description], r)) for r in _cur.fetchall()]"
        )
        lines.append("            else:")
        lines.append("                _total += (_cur.rowcount or 0)")
        lines.append("                if _cur.rowcount == 1:")
        lines.append("                    _last_rowid = _cur.lastrowid")
        lines.append("        conn.commit()")
        lines.append("    except sqlite3.Error as e:")
        lines.append("        conn.rollback()")
        lines.append("        mapped = map_sqlite_error(e)")
        lines.append("        if mapped is not None:")
        lines.append(
            "            return _fail(mapped[\"http_status\"], mapped[\"code\"], "
            "mapped[\"message\"], mapped[\"detail\"])"
        )
        lines.append(
            '        return _fail(500, "INTERNAL_ERROR", "Internal server error.")'
        )
        lines.append("    finally:")
        lines.append("        conn.close()")
        if kind == "SELECT":
            lines.append("    return serialize_rows(_rows)")
        else:
            lines.append('    return {"affected": _total, "rowid": _last_rowid}')
        return "\n".join(lines)

    lines.append("    try:")
    if response.paginate:
        lines.append(
            "        _count_sql = \"SELECT COUNT(*) AS __c FROM (\" + "
            f"{operation.sql!r} + \") AS _c\""
        )
        lines.append('        _total_row = query(_count_sql, params)["rows"]')
        lines.append('        total = _total_row[0]["__c"] if _total_row else 0')
        lines.append("        _page_params = dict(params)")
        lines.append('        _page_params["__pageSize"] = pageSize')
        lines.append('        _page_params["__offset"] = (page - 1) * pageSize')
        lines.append(
            "        _page_sql = \"SELECT * FROM (\" + "
            f"{operation.sql!r} + \") AS _p LIMIT :__pageSize OFFSET :__offset\""
        )
        lines.append("        result = query(_page_sql, _page_params)")
    else:
        lines.append(f"        result = query({operation.sql!r}, params)")
    lines.append("    except sqlite3.Error as exc:")
    lines.append("        _mapped = map_sqlite_error(exc)")
    lines.append("        if _mapped is not None:")
    lines.append(
        "            return _fail(_mapped[\"http_status\"], _mapped[\"code\"], "
        "_mapped[\"message\"], _mapped[\"detail\"])"
    )
    lines.append(
        '        return _fail(500, "INTERNAL_ERROR", "Internal server error.")'
    )

    # --- response shaping (reference.md §5.5 / §6.2) -----------------------------
    if kind == "SELECT":
        lines.append('    rows = result["rows"]')
        if response.columns is not None:
            lines.append(
                "    rows = [{{_name: _row.get(_name) for _name in {columns!r}}}"
                " for _row in rows]".format(columns=response.columns)
            )
        if response.renames:
            lines.append(
                "    rows = [{{{renames!r}.get(_name, _name): _value "
                "for _name, _value in _row.items()}} for _row in rows]".format(
                    renames=response.renames
                )
            )

        if response.shape == "none":
            lines.append("    return Response(status_code=204)")
        elif response.shape == "single":
            lines.append("    if not rows:")
            lines.append(
                '        return _fail(404, "NOT_FOUND", "No matching record was found.")'
            )
            if response.key:
                lines.append(f"    return {{{response.key!r}: rows[0]}}")
            else:
                lines.append("    return rows[0]")
        elif response.paginate:
            lines.append(
                "    return {"
                '"items": rows, "page": page, "pageSize": pageSize, '
                '"total": total, "totalPages": math.ceil(total / pageSize)'
                "}"
            )
        else:
            if method in ("post", "put"):
                lines.append("    if not rows:")
                lines.append(
                    '        return _fail(404, "NOT_FOUND", '
                    '"No matching record was found.")'
                )
            lines.append("    return rows")
    elif kind == "INSERT":
        lines.append(
            '    return {"affected": result["affected"], "rowid": result["rowid"]}'
        )
    else:  # UPDATE / DELETE
        lines.append('    affected = result["affected"]')
        lines.append("    if affected == 0:")
        lines.append(
            '        return _fail(404, "NOT_FOUND", "No matching record was found.")'
        )
        lines.append('    return {"affected": affected}')

    return "\n".join(lines)


def render_api_module(spec: ApiSpec) -> str:
    """Render the complete ``API.py`` source for ``spec`` (reference.md §8.3)."""

    parts: List[str] = [_MODULE_HEADER]
    used: set = set()
    for path, endpoint in spec.endpoints.items():
        for method, operation in endpoint.operations.items():
            name = _route_name(method, path, used)
            parts.append(_render_route(name, path, method, operation))
    return "\n\n".join(parts).rstrip() + "\n"


def _static_imports(spec: ApiSpec) -> str:
    """Return the extra imports/helpers needed when static targets exist."""

    if not spec.static:
        return ""
    return "\n".join(
        (
            "import os",
            "from pathlib import Path",
            "",
            "from fastapi.responses import FileResponse",
            "from fastapi.staticfiles import StaticFiles",
        )
    )


def _static_root(spec: ApiSpec) -> str:
    """Return the ``_STATIC_ROOT`` module-level assignment, if needed."""

    if not spec.static:
        return ""
    return '_STATIC_ROOT = Path(os.path.dirname(__file__)) / "_static"\n'


def _static_media_type(target: StaticTarget) -> str:
    """Return the configured or guessed MIME type for a file target."""

    if target.content_type:
        return target.content_type
    guessed = mimetypes.guess_type(target.target)[0]
    return guessed or "application/octet-stream"


def _render_static_routes(spec: ApiSpec) -> str:
    """Render static routes so file handlers are registered before mounts.

    ``<N>`` stays the target's position in ``spec.static.values()`` (the build
    copy uses the same index), but emission order is independent: file targets
    first, then directory mounts from most-specific to least-specific so a
    broad mount cannot shadow a narrower one.
    """

    if not spec.static:
        return ""

    entries = list(enumerate(spec.static.values()))
    files = [(index, target) for index, target in entries if not target.is_dir]
    directories = [
        (index, target) for index, target in entries if target.is_dir
    ]
    directories.sort(key=lambda item: len(item[1].path), reverse=True)

    blocks: List[str] = []
    for index, target in files:
        basename = os.path.basename(target.target)
        blocks.append(
            "@app.get(%r)\n"
            "def _static_%d():\n"
            '    """Serve the configured static file."""\n'
            "    return FileResponse(str(_STATIC_ROOT / %r / %r), media_type=%r)"
            % (
                target.path,
                index,
                "s%d" % index,
                basename,
                _static_media_type(target),
            )
        )
    for index, target in directories:
        blocks.append(
            "app.mount(%r, StaticFiles(directory=str(_STATIC_ROOT / %r), "
            'html=True), name="static_s%d")'
            % (target.path, "s%d" % index, index)
        )
    return "\n\n".join(blocks)


def render_main_module(spec: ApiSpec) -> str:
    """Render the ``main.py`` entrypoint (reference.md §8.1)."""

    prefix = spec.base_path or "/"
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    if len(prefix) > 1:
        prefix = prefix.rstrip("/") or "/"
    prefix_arg = "" if prefix == "/" else ", prefix=" + repr(prefix)
    static = _render_static_routes(spec)
    if static:
        static = "\n" + static + "\n"
    return (
        _MAIN_TEMPLATE.replace("__PREFIX__", prefix_arg)
        .replace("__STATIC_IMPORTS__", _static_imports(spec))
        .replace("__STATIC_ROOT__", _static_root(spec))
        .replace("__STATIC__", static)
    )