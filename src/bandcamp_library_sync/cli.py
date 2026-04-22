from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .bandcamp import BandcampClient, BandcampError
from .config import default_app_paths, load_json, save_json
from .sync import summarize_results, sync_with_error_capture


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bandcamp-library-sync")
    parser.add_argument("--config-dir", help="Override the config directory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    login = subparsers.add_parser("login", help="Open a browser and save a Bandcamp session")
    login.add_argument("--fan-url", help="Optional Bandcamp collection URL to persist")
    login.add_argument("--headless", action="store_true", help="Use headless mode")

    list_cmd = subparsers.add_parser("list", help="List discovered purchased releases")
    list_cmd.add_argument("--limit", type=int, help="Only print the first N releases")

    sync_cmd = subparsers.add_parser("sync", help="Download missing releases")
    sync_cmd.add_argument("--output", required=True, help="Directory to sync into")
    sync_cmd.add_argument("--format", default="flac", help="Bandcamp download format")
    sync_cmd.add_argument("--limit", type=int, help="Only attempt the first N missing releases")
    sync_cmd.add_argument("--dry-run", action="store_true", help="Do not download anything")

    export_cmd = subparsers.add_parser(
        "export-manifest",
        help="Write a flat manifest describing the staged releases for downstream tools",
    )
    export_cmd.add_argument("--output", required=True, help="Directory to scan")
    export_cmd.add_argument(
        "--manifest",
        help="Destination JSON path, defaults to <output>/.bandcamp-demlo-manifest.json",
    )

    return parser


def load_config(config_dir: str | None) -> tuple[Any, dict[str, Any]]:
    paths = default_app_paths(config_dir)
    config = load_json(paths.config_file, {})
    return paths, config


def save_config(paths: Any, config: dict[str, Any]) -> None:
    save_json(paths.config_file, config)


def cmd_login(args: argparse.Namespace) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BandcampError(
            "Playwright is not installed. Install project dependencies and run `playwright install chromium`."
        ) from exc

    paths, config = load_config(args.config_dir)
    target = args.fan_url or config.get("fan_url") or "https://bandcamp.com/login"

    paths.config_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=args.headless)
        context_kwargs: dict[str, Any] = {}
        if paths.session_file.exists():
            context_kwargs["storage_state"] = str(paths.session_file)
        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        page.goto(target, wait_until="domcontentloaded")

        print("Complete the Bandcamp login in the browser.")
        print("Finish on your collection page, then press Enter here to save the session.")
        input()

        current_url = page.url
        fan_url = args.fan_url or infer_collection_url(current_url)
        if not fan_url:
            raise BandcampError(
                "Could not infer your collection URL. Re-run `login --fan-url https://bandcamp.com/<user>`."
            )

        context.storage_state(path=str(paths.session_file))
        context.close()
        browser.close()

    config["fan_url"] = fan_url
    save_config(paths, config)
    print(f"Saved session to {paths.session_file}")
    print(f"Saved fan URL: {fan_url}")
    return 0


def infer_collection_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.netloc != "bandcamp.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    return f"https://bandcamp.com/{parts[0]}"


def cmd_list(args: argparse.Namespace) -> int:
    paths, config = load_config(args.config_dir)
    client = BandcampClient(paths, config)
    releases = client.fetch_collection_releases()
    limit = args.limit or len(releases)
    for release in releases[:limit]:
        print(f"{release.item_id}\t{release.artist}\t{release.title}\t{release.item_url}")
    print(f"total={len(releases)}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    paths, config = load_config(args.config_dir)
    client = BandcampClient(paths, config)
    results = sync_with_error_capture(
        client=client,
        output_dir=Path(args.output),
        fmt=args.format,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    for result in results:
        status = "downloaded" if result.downloaded else (result.reason or "skipped")
        print(f"{result.release.item_id}\t{status}\t{result.release.artist} - {result.release.title}")

    print(summarize_results(results))
    return 0 if not any((result.reason or "").startswith("error:") for result in results) else 1


def cmd_export_manifest(args: argparse.Namespace) -> int:
    from .sync import export_release_manifest

    output_dir = Path(args.output).expanduser()
    manifest_path = Path(args.manifest).expanduser() if args.manifest else None
    written_path, count = export_release_manifest(output_dir, manifest_path)
    print(f"manifest={written_path}")
    print(f"releases={count}")
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "login":
            return cmd_login(args)
        if args.command == "list":
            return cmd_list(args)
        if args.command == "sync":
            return cmd_sync(args)
        if args.command == "export-manifest":
            return cmd_export_manifest(args)
        parser.error(f"unknown command: {args.command}")
    except BandcampError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
