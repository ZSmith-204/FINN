"""Build-time emission of the generated ``API.py`` / ``main.py`` files.

Mirrors :func:`database.db_init`-style layering: :func:`generate` raises on
failure, while :func:`api_init` wraps it for callers that only need a
``(success, error_code)`` pair (README §8.1/§8.3).
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from database import ConfigError

from .codegen import render_api_module, render_main_module
from .schema import ApiSpec, StaticTarget

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


def _plan_static(
    output_dir: Path, spec: ApiSpec, base_dir
) -> List[Tuple[StaticTarget, Path, Path]]:
    """Validate static targets and return ``(target, source, destination)``.

    All checks run before any build file is written, so a bad static target
    cannot leave a partially generated build behind.
    """

    static_root = output_dir / "_static"
    base_root = Path(base_dir).resolve()
    actions: List[Tuple[StaticTarget, Path, Path]] = []
    for index, target in enumerate(spec.static.values()):
        raw_target = target.target
        target_path = Path(raw_target)
        if (
            not raw_target
            or os.path.isabs(raw_target)
            or target_path.is_absolute()
            or ".." in target_path.parts
        ):
            raise ConfigError(
                f"api.static.{target.path}",
                f"unsafe static target {raw_target!r}; expected a relative "
                "path without '..'",
            )

        source = Path(base_dir) / raw_target
        if source.is_symlink():
            raise ConfigError(
                f"api.static.{target.path}",
                "symlinks are not allowed in static targets",
            )
        if target.is_dir:
            if not source.is_dir():
                raise ConfigError(
                    f"api.static.{target.path}",
                    f"static directory {raw_target!r} not found at {source}",
                )
            for dirpath, dirnames, filenames in os.walk(
                source, followlinks=False
            ):
                for name in dirnames + filenames:
                    if (Path(dirpath) / name).is_symlink():
                        raise ConfigError(
                            f"api.static.{target.path}",
                            "symlinks are not allowed in static directory "
                            "trees",
                        )
        else:
            if not source.is_file():
                raise ConfigError(
                    f"api.static.{target.path}",
                    f"static file {raw_target!r} not found at {source}",
                )

        resolved_source = source.resolve()
        if not resolved_source.is_relative_to(base_root):
            raise ConfigError(
                f"api.static.{target.path}",
                f"static target {raw_target!r} resolves outside the base "
                "directory",
            )

        destination = static_root / ("s%d" % index)
        if target.is_dir:
            resolved_destination = destination.resolve()
            if resolved_destination == resolved_source or (
                resolved_destination.is_relative_to(resolved_source)
            ):
                raise ConfigError(
                    f"api.static.{target.path}",
                    "static directory would copy into itself",
                )
        actions.append((target, source, destination))
    return actions


def _copy_static(
    actions: List[Tuple[StaticTarget, Path, Path]]
) -> None:
    """Copy validated static actions into their ``s<N>`` destinations."""

    for target, source, destination in actions:
        os.makedirs(destination, exist_ok=True)
        if target.is_dir:
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(
                source, destination / os.path.basename(target.target)
            )


def generate(output_dir, spec: ApiSpec, base_dir=None) -> List[str]:
    """Write ``API.py`` and ``main.py`` into ``output_dir``; return their paths.

    ``base_dir`` is the config file's directory; it is required when
    ``spec.static`` is non-empty so static sources can be copied into
    ``output_dir/_static``. Static targets are validated before any module is
    written, and ``_static`` is rebuilt from scratch on every successful run.
    """

    if spec.static and base_dir is None:
        raise ConfigError(
            "api.static",
            "static files require a base_dir (the config file's directory)",
        )

    target = Path(output_dir)
    actions = _plan_static(target, spec, base_dir) if spec.static else []

    api_source = render_api_module(spec)
    main_source = render_main_module(spec)

    os.makedirs(target, exist_ok=True)
    api_path = target / "API.py"
    main_path = target / "main.py"
    static_root = target / "_static"

    staging = None
    try:
        if actions:
            staging = Path(
                tempfile.mkdtemp(prefix="_static_tmp_", dir=target)
            )
            _copy_static(
                [
                    (static_target, source, staging / destination.name)
                    for static_target, source, destination in actions
                ]
            )
        api_path.write_text(api_source, encoding="utf-8")
        main_path.write_text(main_source, encoding="utf-8")

        if static_root.exists():
            shutil.rmtree(static_root)
        if staging is not None:
            os.replace(staging, static_root)
            staging = None
    except Exception:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise

    return [str(api_path.resolve()), str(main_path.resolve())]


def api_init(output_dir, spec: ApiSpec, base_dir=None) -> Tuple[bool, Optional[str]]:
    """Non-raising wrapper around :func:`generate`.

    Returns ``(True, None)`` on success, or ``(False, error_code)`` on failure.
    The exception detail is logged through the ``api`` logger.
    """

    try:
        generate(output_dir, spec, base_dir)
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