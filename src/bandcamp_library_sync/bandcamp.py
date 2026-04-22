from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .models import AppPaths, Release

COLLECTION_ITEMS_URL = "https://bandcamp.com/api/fancollection/1/collection_items"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)


class BandcampError(RuntimeError):
    pass


class BandcampClient:
    def __init__(self, paths: AppPaths, config: dict[str, Any]) -> None:
        self.paths = paths
        self.config = config
        self.session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})

        if not self.paths.session_file.exists():
            raise BandcampError(
                f"No saved session found at {self.paths.session_file}. Run `login` first."
            )

        storage = json.loads(self.paths.session_file.read_text(encoding="utf-8"))
        cookies = storage.get("cookies", [])
        for cookie in cookies:
            session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain"),
                path=cookie.get("path", "/"),
            )
        return session

    def require_fan_url(self) -> str:
        fan_url = self.config.get("fan_url")
        if not fan_url:
            raise BandcampError(
                "No fan collection URL is configured. Run `login` and finish on your collection page."
            )
        return fan_url

    def fetch_collection_context(self) -> dict[str, Any]:
        response = self.session.get(self.require_fan_url(), timeout=30)
        response.raise_for_status()
        blobs = extract_data_blobs(response.text)
        context = find_collection_blob(blobs)
        if context is None:
            raise BandcampError("Unable to locate Bandcamp collection metadata in the fan page.")
        return context

    def fetch_collection_releases(self) -> list[Release]:
        context = self.fetch_collection_context()
        fan_data = context.get("fan_data") or {}
        collection_data = context.get("collection_data") or {}
        fan_id = fan_data.get("fan_id")
        token = collection_data.get("last_token")
        if not fan_id:
            raise BandcampError("Bandcamp page did not expose a fan_id.")

        items: list[dict[str, Any]] = []
        seen_tokens: set[str] = set()

        while True:
            payload = {"fan_id": fan_id, "count": 100}
            if token:
                payload["older_than_token"] = token
            response = self.session.post(COLLECTION_ITEMS_URL, json=payload, timeout=30)
            response.raise_for_status()
            page = response.json()
            page_items = page.get("items") or []
            if not page_items:
                break
            items.extend(page_items)

            next_token = page.get("last_token") or page.get("older_than_token")
            more_available = bool(page.get("more_available"))
            if not more_available or not next_token or next_token in seen_tokens:
                break
            seen_tokens.add(next_token)
            token = next_token

        return [self._release_from_item(item) for item in items if self._is_owned(item)]

    def _is_owned(self, item: dict[str, Any]) -> bool:
        flags = [
            item.get("is_purchased"),
            item.get("purchased"),
            item.get("sale_item_id"),
            item.get("tralbum_id"),
        ]
        return any(bool(flag) for flag in flags)

    def _release_from_item(self, item: dict[str, Any]) -> Release:
        item_id = int(item.get("item_id") or item.get("tralbum_id") or item.get("sale_item_id"))
        item_url = item.get("item_url") or item.get("tralbum_url")
        if item_url and item_url.startswith("/"):
            item_url = urljoin("https://bandcamp.com", item_url)
        artist = (
            item.get("band_name")
            or item.get("artist")
            or item.get("item_art_id")
            or "Unknown Artist"
        )
        title = (
            item.get("item_title")
            or item.get("album_title")
            or item.get("tralbum_title")
            or "Unknown Release"
        )
        if not item_url:
            raise BandcampError(f"Collection item {item_id} did not include an item URL.")
        return Release(
            item_id=item_id,
            item_url=item_url,
            artist=str(artist),
            title=str(title),
            raw=item,
        )

    def resolve_download_archive_url(self, release: Release, fmt: str) -> str:
        release_page = self.session.get(release.item_url, timeout=30)
        release_page.raise_for_status()

        direct_link = find_download_link_in_html(release_page.text)
        if direct_link:
            download_page_url = urljoin(release.item_url, direct_link)
        else:
            raise BandcampError(
                f"Unable to locate a download page link for {release.artist} / {release.title}"
            )

        download_page = self.session.get(download_page_url, timeout=30)
        download_page.raise_for_status()

        archive_url = find_archive_url(download_page.text, fmt)
        if archive_url:
            return archive_url

        raise BandcampError(
            f"Unable to resolve a {fmt} archive URL for {release.artist} / {release.title}"
        )

    def download_archive(self, archive_url: str, target_path: str) -> None:
        with self.session.get(archive_url, timeout=120, stream=True) as response:
            response.raise_for_status()
            with open(target_path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)


def extract_data_blobs(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    blobs: list[dict[str, Any]] = []

    for tag in soup.find_all(attrs={"data-blob": True}):
        raw_blob = tag.get("data-blob")
        if not raw_blob:
            continue
        try:
            blobs.append(json.loads(raw_blob))
        except json.JSONDecodeError:
            continue
    return blobs


def find_collection_blob(blobs: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    for blob in blobs:
        if "fan_data" in blob and "collection_data" in blob:
            return blob
    return None


def find_download_link_in_html(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")

    for link in soup.find_all("a", href=True):
        href = link["href"]
        text = link.get_text(" ", strip=True).lower()
        if "/download" in href and ("download" in text or "you own this" in html.lower()):
            return href

    match = re.search(r'https?://[^"\']+/download[^"\']+', html)
    if match:
        return match.group(0)

    match = re.search(r'(/download[^"\']+)', html)
    if match:
        return match.group(1)

    return None


def find_archive_url(html: str, fmt: str) -> str | None:
    blobs = extract_data_blobs(html)
    for blob in blobs:
        match = search_for_format_url(blob, fmt)
        if match:
            return match

    encoded_fmt = re.escape(fmt)
    regexes = [
        rf'https?://[^"\']+{encoded_fmt}[^"\']+',
        rf'"url"\s*:\s*"([^"]*{encoded_fmt}[^"]*)"',
    ]
    for pattern in regexes:
        match = re.search(pattern, html)
        if not match:
            continue
        if match.lastindex:
            return match.group(1).replace("\\u0026", "&").replace("\\/", "/")
        return match.group(0).replace("\\u0026", "&").replace("\\/", "/")

    return None


def search_for_format_url(value: Any, fmt: str) -> str | None:
    if isinstance(value, dict):
        url = value.get("url") or value.get("download_url") or value.get("retry_url")
        enc = value.get("encoding_name") or value.get("encoding")
        if isinstance(url, str) and fmt in url:
            return url
        if isinstance(url, str) and enc == fmt:
            return url
        for nested in value.values():
            match = search_for_format_url(nested, fmt)
            if match:
                return match
        return None

    if isinstance(value, list):
        for nested in value:
            match = search_for_format_url(nested, fmt)
            if match:
                return match
        return None

    return None
