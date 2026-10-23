"""Crash-safe JSON persistence for HeatWatch's runtime state.

An open emergency must survive a restart. State is written atomically (temp file +
rename) and datetimes/dates are tagged so they round-trip exactly, including timezone.
Camera snapshots are never written to disk.
"""

from __future__ import annotations

import json
import os
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


def encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"__dt__": value.isoformat()}
    if isinstance(value, date):
        return {"__date__": value.isoformat()}
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: encode(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [encode(v) for v in value]
    return value


def decode(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"__dt__"}:
            return datetime.fromisoformat(value["__dt__"])
        if set(value) == {"__date__"}:
            return date.fromisoformat(value["__date__"])
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


def build(cls: type, data: dict[str, Any]):
    """Instantiate a dataclass from decoded data, ignoring unknown keys."""
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(encode(payload)), encoding="utf-8")
    os.replace(tmp, path)


def read(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return decode(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
