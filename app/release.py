"""Release identity and packaged-build provenance."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any


APP_VERSION = "0.2.1-rc1"
PROVENANCE_FILENAME = "build_provenance.json"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def provenance_path() -> Path | None:
    override = os.environ.get("HOSO_BUILD_PROVENANCE")
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)) / PROVENANCE_FILENAME
    return None


def read_provenance(path: Path | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"version": APP_VERSION, "build_sha": "unpackaged", "build_timestamp_utc": None}
    candidate = path if path is not None else provenance_path()
    if candidate is None:
        return result
    try:
        raw = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return result
    if not isinstance(raw, dict) or raw.get("version") != APP_VERSION:
        return result
    build_sha = str(raw.get("build_sha", "")).lower()
    if not _SHA_RE.fullmatch(build_sha):
        return result
    result["build_sha"] = build_sha
    if isinstance(raw.get("build_timestamp_utc"), str):
        result["build_timestamp_utc"] = raw["build_timestamp_utc"]
    return result


def health_release_fields() -> dict[str, Any]:
    return read_provenance()
