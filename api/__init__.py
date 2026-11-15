"""FINN HTTP layer: ``api`` parsing, FastAPI codegen, and build emission.

Mirrors the :mod:`database` layout: a validated spec dataclass
(:mod:`.schema`), standalone source rendering (:mod:`.codegen`), and a safe
build wrapper (:mod:`.build`).  This package is standard-library only; the
*generated* ``API.py``/``main.py`` import FastAPI, never the reverse.
"""

from __future__ import annotations

from database import ConfigError, ValidationError, normalize_type

from .build import (
    API_INIT_CONFIG,
    API_INIT_FILE,
    API_INIT_UNKNOWN,
    api_init,
    generate,
)
from .codegen import render_api_module, render_main_module
from .schema import (
    METHODS,
    ApiSpec,
    Endpoint,
    Operation,
    Param,
    Response,
    extract_params,
    parse_api,
    split_statements,
    statement_kind,
)

__all__ = [
    "ConfigError",
    "ValidationError",
    "normalize_type",
    "METHODS",
    "ApiSpec",
    "Endpoint",
    "Operation",
    "Param",
    "Response",
    "parse_api",
    "statement_kind",
    "split_statements",
    "extract_params",
    "render_api_module",
    "render_main_module",
    "generate",
    "api_init",
    "API_INIT_CONFIG",
    "API_INIT_FILE",
    "API_INIT_UNKNOWN",
]