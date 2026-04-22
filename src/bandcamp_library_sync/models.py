from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class AppPaths:
    config_dir: Path
    config_file: Path
    session_file: Path


@dataclass(slots=True)
class Release:
    item_id: int
    item_url: str
    artist: str
    title: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SyncResult:
    release: Release
    downloaded: bool
    target_dir: Path | None = None
    reason: str | None = None
