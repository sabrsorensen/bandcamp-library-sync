from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .models import AppPaths


def default_app_paths(config_dir: str | None = None) -> AppPaths:
    root = Path(
        config_dir
        or os.environ.get("BANDCAMP_LIBRARY_SYNC_CONFIG_DIR")
        or Path.home() / ".config" / "bandcamp-library-sync"
    ).expanduser()
    return AppPaths(
        config_dir=root,
        config_file=root / "config.json",
        session_file=root / "session.json",
    )


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
