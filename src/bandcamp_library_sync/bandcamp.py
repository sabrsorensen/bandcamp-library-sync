from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Iterable
from pathlib import Path
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
DEFAULT_TIMEOUT = 30
DEFAULT_REQUEST_DELAY = 2.5
DEFAULT_MAX_RETRIES = 5
DEFAULT_RETRY_WAIT = 10.0
DEFAULT_RETRY_BACKOFF = 2.0


class BandcampError(RuntimeError):
    pass


class BandcampClient:
    def __init__(self, paths: AppPaths, config: dict[str, Any]) -> None:
        self.paths = paths
        self.config = config
        self.request_delay = float(config.get("request_delay", DEFAULT_REQUEST_DELAY))
        self.max_retries = int(config.get("max_retries", DEFAULT_MAX_RETRIES))
        self.retry_wait = float(config.get("retry_wait", DEFAULT_RETRY_WAIT))
        self.retry_backoff = float(config.get("retry_backoff", DEFAULT_RETRY_BACKOFF))
        self._last_request_at = 0.0
        self.session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/json,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }
        )

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

    def _pace_requests(self) -> None:
        if self.request_delay <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        delay = self.request_delay - elapsed
        if delay > 0:
            time.sleep(delay + random.uniform(0.0, min(0.5, self.request_delay / 4)))

    def _compute_retry_delay(self, response: requests.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return max(float(retry_after), self.retry_wait)
                except ValueError:
                    pass
        return self.retry_wait * (self.retry_backoff ** max(0, attempt - 1))

    def _request(
        self,
        method: str,
        url: str,
        *,
        timeout: int | float = DEFAULT_TIMEOUT,
        expected_statuses: set[int] | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        response: requests.Response | None = None
        for attempt in range(1, self.max_retries + 1):
            self._pace_requests()
            try:
                response = self.session.request(method, url, timeout=timeout, **kwargs)
                self._last_request_at = time.monotonic()
            except requests.RequestException as exc:
                if attempt == self.max_retries:
                    raise BandcampError(f"Request failed for {url}: {exc}") from exc
                time.sleep(self._compute_retry_delay(None, attempt))
                continue

            acceptable = expected_statuses or set()
            if response.status_code < 400 or response.status_code in acceptable:
                return response

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < self.max_retries:
                    time.sleep(self._compute_retry_delay(response, attempt))
                    continue

            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                raise BandcampError(
                    f"HTTP {response.status_code} for {url}"
                ) from exc

        assert response is not None
        return response

    def _debug_dump(self, slug: str, html: str) -> Path:
        debug_dir = self.paths.config_dir / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        path = debug_dir / f"{timestamp}-{slug}.html"
        path.write_text(html, encoding="utf-8")
        return path

    def require_fan_url(self) -> str:
        fan_url = self.config.get("fan_url")
        if not fan_url:
            raise BandcampError(
                "No fan collection URL is configured. Run `login` and finish on your collection page."
            )
        return fan_url

    def fetch_collection_context(self) -> dict[str, Any]:
        response = self._request("GET", self.require_fan_url())
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
            response = self._request(
                "POST",
                COLLECTION_ITEMS_URL,
                json=payload,
                headers={"Origin": "https://bandcamp.com", "Referer": self.require_fan_url()},
            )
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
        release_page = self._request("GET", release.item_url)

        direct_link = find_download_link_in_html(release_page.text)
        if direct_link:
            download_page_url = urljoin(release.item_url, direct_link)
        else:
            debug_path = self._debug_dump(f"{release.item_id}-release-page", release_page.text)
            raise BandcampError(
                f"Unable to locate a download page link for {release.artist} / {release.title}. "
                f"Saved debug page to {debug_path}"
            )

        download_page = self._request(
            "GET",
            download_page_url,
            headers={"Referer": release.item_url},
        )

        archive_url = find_archive_url(download_page.text, fmt)
        if archive_url:
            return archive_url

        debug_path = self._debug_dump(f"{release.item_id}-download-page", download_page.text)
        raise BandcampError(
            f"Unable to resolve a {fmt} archive URL for {release.artist} / {release.title}. "
            f"Saved debug page to {debug_path}"
        )

    def download_archive(self, archive_url: str, target_path: str) -> None:
        with self._request(
            "GET",
            archive_url,
            timeout=120,
            stream=True,
            headers={"Referer": "https://bandcamp.com/"},
        ) as response:
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
        rf'"retry_url"\s*:\s*"([^"]*{encoded_fmt}[^"]*)"',
        r'"downloads"\s*:\s*(\{.*?\})',
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
        description = value.get("description")
        if isinstance(url, str) and fmt in url:
            return url
        if isinstance(url, str) and enc == fmt:
            return url
        if isinstance(url, str) and isinstance(description, str) and fmt in description.lower():
            return url
        if fmt in value and isinstance(value[fmt], dict):
            nested = value[fmt]
            nested_url = nested.get("url") or nested.get("download_url") or nested.get("retry_url")
            if isinstance(nested_url, str):
                return nested_url
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
