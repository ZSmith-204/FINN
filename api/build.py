"""Build-time emission of the generated ``API.py`` / ``main.py`` files.

Mirrors :func:`database.db_init`-style layering: :func:`generate` raises on
failure, while :func:`api_init` wraps it for callers that only need a
``(success, error_code)`` pair (README §8.1/§8.3).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Optional, Tuple

from database import ConfigError

from .codegen import render_api_module, render_main_module
from .schema import ApiSpec

__all__ = [
    "API_INIT_CONFIG",
    "API_INIT_FILE",
    "API_INIT_UNKNOWN",
    "generate",
    "api_init",
]

LOGGER = logging.getLogger("api")

API_INIT_CONFIG = "CONFIG_ERROR"
API_INIT_FILE = "FILE_ERROR"
API_INIT_UNKNOWN = "UNKNOWN"


def generate(output_dir, spec: ApiSpec) -> List[str]:
    """Write ``API.py`` and ``main.py`` into ``output_dir``; return their paths."""

    target = Path(output_dir)
    os.makedirs(target, exist_ok=True)

    api_source = render_api_module(spec)
    main_source = render_main_module(spec)

    api_path = target / "API.py"
    main_path = target / "main.py"
    api_path.write_text(api_source, encoding="utf-8")
    main_path.write_text(main_source, encoding="utf-8")
    return [str(api_path.resolve()), str(main_path.resolve())]


def api_init(output_dir, spec: ApiSpec) -> Tuple[bool, Optional[str]]:
    """Non-raising wrapper around :func:`generate`.

    Returns ``(True, None)`` on success, or ``(False, error_code)`` on failure.
    The exception detail is logged through the ``api`` logger.
    """

    try:
        generate(output_dir, spec)
    except ConfigError as exc:
        LOGGER.error("api_init config error for %s: %s", output_dir, exc)
        return False, API_INIT_CONFIG
    except OSError as exc:
        LOGGER.error("api_init file error for %s: %s", output_dir, exc)
        return False, API_INIT_FILE
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.error("api_init unexpected error for %s: %s", output_dir, exc)
        return False, API_INIT_UNKNOWN
    return True, None