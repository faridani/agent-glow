"""Shared pieces every light backend uses: errors and snapshot persistence.

A backend (Hue bridge, WiZ bulbs, ...) implements the controller surface the
daemon consumes: ``connect``, ``close``, steady ``apply_state`` (including
completion-green), transient ``blink_green``, ``restore``,
``restore_from_file``, ``has_snapshot_file``, and ``target_summary``. The
``CompositeController`` fans out across backends and treats
``BackendUnavailableError`` as "this backend cannot help right now".
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from ..config import ensure_private_dir, ensure_private_file, open_private_fd

#: Brightness drift (percent) beyond which we assume the user took over.
OVERRIDE_BRIGHTNESS_TOLERANCE = 12.0


class BackendUnavailableError(Exception):
    """Backend not configured or not reachable right now."""


# -- snapshot persistence (one file per backend) --------------------------------


def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    fd = open_private_fd(tmp)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    tmp.replace(path)
    ensure_private_file(path)


def save_snapshot_data(
    path: Path, snapshot: dict[str, Any], controlled: set[str]
) -> None:
    """Persist per-light snapshots (objects with a ``to_dict()``)."""
    atomic_write_json(
        path,
        {
            "saved_at": time.time(),
            "controlled": sorted(controlled),
            "lights": {lid: snap.to_dict() for lid, snap in snapshot.items()},
        },
    )


def load_snapshot_data(
    path: Path, from_dict: Callable[[dict], Any]
) -> tuple[dict[str, Any], set[str]] | None:
    if not path.exists():
        return None
    try:
        ensure_private_dir(path.parent)
        ensure_private_file(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        lights = {lid: from_dict(item) for lid, item in data.get("lights", {}).items()}
        if not lights:
            return None
        # Corrupt/stale controlled ids must never make a backend believe it
        # owns a lamp for which it cannot restore the original state.
        controlled = set(data.get("controlled", [])) & set(lights)
        return lights, controlled
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return None


def clear_snapshot_data(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
