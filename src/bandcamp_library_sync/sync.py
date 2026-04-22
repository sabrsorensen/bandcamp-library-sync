from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

from .bandcamp import BandcampClient, BandcampError
from .models import Release, SyncResult


def sanitize_path_component(value: str) -> str:
    cleaned = re.sub(r"[<>:\"/\\\\|?*\x00-\x1f]", "_", value).strip()
    return cleaned or "unknown"


def marker_paths(root: Path) -> list[Path]:
    return [
        *root.rglob("bandcamp_item_id.txt"),
        *root.rglob(".bandcamp-release.json"),
    ]


def iter_release_manifests(root: Path) -> Iterator[tuple[Path, dict[str, object]]]:
    if not root.exists():
        return

    for marker in root.rglob(".bandcamp-release.json"):
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        yield marker.parent, payload


def discover_existing_item_ids(root: Path) -> set[int]:
    existing: set[int] = set()
    if not root.exists():
        return existing

    for marker in marker_paths(root):
        try:
            if marker.name == "bandcamp_item_id.txt":
                existing.add(int(marker.read_text(encoding="utf-8").strip()))
                continue
            payload = json.loads(marker.read_text(encoding="utf-8"))
            if "item_id" in payload:
                existing.add(int(payload["item_id"]))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return existing


def target_release_dir(root: Path, release: Release) -> Path:
    return root / sanitize_path_component(release.artist) / sanitize_path_component(release.title)


def write_markers(target_dir: Path, release: Release) -> None:
    (target_dir / "bandcamp_item_id.txt").write_text(f"{release.item_id}\n", encoding="utf-8")
    manifest = {
        "item_id": release.item_id,
        "artist": release.artist,
        "title": release.title,
        "item_url": release.item_url,
    }
    (target_dir / ".bandcamp-release.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def extract_archive(archive_path: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(target_dir)


def sync_collection(
    client: BandcampClient,
    output_dir: Path,
    fmt: str = "flac",
    limit: int | None = None,
    dry_run: bool = False,
) -> list[SyncResult]:
    output_dir = output_dir.expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    existing_ids = discover_existing_item_ids(output_dir)
    releases = client.fetch_collection_releases()
    results: list[SyncResult] = []

    pending = [release for release in releases if release.item_id not in existing_ids]
    if limit is not None:
        pending = pending[:limit]

    for release in pending:
        target_dir = target_release_dir(output_dir, release)
        if dry_run:
            results.append(
                SyncResult(
                    release=release,
                    downloaded=False,
                    target_dir=target_dir,
                    reason="dry-run",
                )
            )
            continue

        with tempfile.TemporaryDirectory(prefix="bandcamp-sync-") as tmpdir:
            archive_path = Path(tmpdir) / f"{release.item_id}.zip"
            archive_url = client.resolve_download_archive_url(release, fmt)
            client.download_archive(archive_url, str(archive_path))

            if target_dir.exists():
                shutil.rmtree(target_dir)
            extract_archive(archive_path, target_dir)
            write_markers(target_dir, release)

        results.append(SyncResult(release=release, downloaded=True, target_dir=target_dir))

    already_present = [release for release in releases if release.item_id in existing_ids]
    for release in already_present:
        results.append(
            SyncResult(release=release, downloaded=False, reason="already-present")
        )

    return results


def summarize_results(results: list[SyncResult]) -> str:
    downloaded = sum(1 for result in results if result.downloaded)
    skipped = sum(1 for result in results if result.reason == "already-present")
    dry_run = sum(1 for result in results if result.reason == "dry-run")
    errors = sum(1 for result in results if result.reason and result.reason.startswith("error:"))
    return (
        f"downloaded={downloaded} skipped={skipped} dry_run={dry_run} errors={errors}"
    )


def export_release_manifest(output_dir: Path, manifest_path: Path | None = None) -> tuple[Path, int]:
    output_dir = output_dir.expanduser()
    manifest_path = manifest_path or (output_dir / ".bandcamp-demlo-manifest.json")

    releases: list[dict[str, object]] = []
    for release_dir, payload in iter_release_manifests(output_dir):
        files = sorted(
            str(path.relative_to(output_dir))
            for path in release_dir.rglob("*")
            if path.is_file() and path.name not in {"bandcamp_item_id.txt", ".bandcamp-release.json"}
        )
        releases.append(
            {
                "item_id": payload.get("item_id"),
                "artist": payload.get("artist"),
                "title": payload.get("title"),
                "item_url": payload.get("item_url"),
                "release_dir": str(release_dir.relative_to(output_dir)),
                "files": files,
            }
        )

    manifest_path.write_text(
        json.dumps({"library_root": str(output_dir), "releases": releases}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest_path, len(releases)


def sync_with_error_capture(
    client: BandcampClient,
    output_dir: Path,
    fmt: str = "flac",
    limit: int | None = None,
    dry_run: bool = False,
) -> list[SyncResult]:
    output_dir = output_dir.expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    existing_ids = discover_existing_item_ids(output_dir)
    releases = client.fetch_collection_releases()
    results: list[SyncResult] = []

    pending = [release for release in releases if release.item_id not in existing_ids]
    if limit is not None:
        pending = pending[:limit]

    for release in pending:
        target_dir = target_release_dir(output_dir, release)
        if dry_run:
            results.append(
                SyncResult(release=release, downloaded=False, target_dir=target_dir, reason="dry-run")
            )
            continue

        try:
            with tempfile.TemporaryDirectory(prefix="bandcamp-sync-") as tmpdir:
                archive_path = Path(tmpdir) / f"{release.item_id}.zip"
                archive_url = client.resolve_download_archive_url(release, fmt)
                client.download_archive(archive_url, str(archive_path))

                if target_dir.exists():
                    shutil.rmtree(target_dir)
                extract_archive(archive_path, target_dir)
                write_markers(target_dir, release)

            results.append(SyncResult(release=release, downloaded=True, target_dir=target_dir))
        except (BandcampError, OSError, zipfile.BadZipFile) as exc:
            results.append(
                SyncResult(
                    release=release,
                    downloaded=False,
                    target_dir=target_dir,
                    reason=f"error: {exc}",
                )
            )

    for release in releases:
        if release.item_id in existing_ids:
            results.append(
                SyncResult(release=release, downloaded=False, reason="already-present")
            )

    return results
