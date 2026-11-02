"""Polite HTTP client for free scholarly APIs (Crossref, OpenAlex, ...).

- identifies itself with CONTACT_EMAIL (User-Agent + `mailto`) for the polite pools,
- rate-limits requests per host,
- retries on 429 / 5xx / transport errors with exponential backoff,
- caches JSON responses on disk (default `data/cache/`) so re-runs don't re-query.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from rigorlight import __version__

DEFAULT_CACHE_DIR = Path(os.environ.get("RIGORLIGHT_CACHE_DIR", "data/cache"))
MIN_INTERVAL_S = 0.2          # at most ~5 requests/second per host
MAX_RETRIES = 3
BACKOFF_BASE_S = 1.0          # 1s, 2s, 4s
TIMEOUT_S = 20.0
RETRY_STATUSES = {429, 500, 502, 503, 504}

_MISSING = object()


class LookupFailed(RuntimeError):
    """A service could not be reached or kept failing. Not the same as 'not found'."""


def contact_email() -> str:
    return os.environ.get("CONTACT_EMAIL", "").strip()


def user_agent() -> str:
    email = contact_email()
    ua = f"Rigorlight/{__version__} (https://github.com/chreissel/rigorlight"
    return ua + (f"; mailto:{email})" if email else ")")


class Http:
    def __init__(
        self,
        cache_dir: Path | str | None = DEFAULT_CACHE_DIR,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        min_interval: float = MIN_INTERVAL_S,
    ):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.client = client or httpx.Client(
            headers={"User-Agent": user_agent()}, timeout=TIMEOUT_S, follow_redirects=True
        )
        self.sleep = sleep
        self.min_interval = min_interval
        self._last: dict[str, float] = {}

    # -- internals -----------------------------------------------------------------
    def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc
        wait = self._last.get(host, 0.0) + self.min_interval - time.monotonic()
        if wait > 0:
            self.sleep(wait)
        self._last[host] = time.monotonic()

    def _cache_path(self, key: str) -> Path | None:
        if not self.cache_dir:
            return None
        h = hashlib.sha256(key.encode()).hexdigest()
        return self.cache_dir / h[:2] / f"{h}.json"

    def _cache_get(self, key: str) -> Any:
        p = self._cache_path(key)
        if p and p.exists():
            try:
                return json.loads(p.read_text())["value"]
            except (ValueError, KeyError):
                pass
        return _MISSING

    def _cache_put(self, key: str, value: Any) -> None:
        p = self._cache_path(key)
        if p:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"key": key, "value": value}))

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Send a request with throttling and retries. Raises LookupFailed if it keeps failing."""
        last_err: str = ""
        for attempt in range(MAX_RETRIES + 1):
            self._throttle(url)
            try:
                resp = self.client.request(method, url, **kwargs)
            except httpx.HTTPError as e:
                last_err = f"{type(e).__name__}: {e}"
            else:
                if resp.status_code not in RETRY_STATUSES:
                    return resp
                last_err = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("Retry-After", "")
                if attempt < MAX_RETRIES and retry_after.isdigit():
                    self.sleep(min(int(retry_after), 60))
                    continue
            if attempt < MAX_RETRIES:
                self.sleep(BACKOFF_BASE_S * 2**attempt)
        raise LookupFailed(f"{method} {url} failed after {MAX_RETRIES + 1} attempts ({last_err})")

    # -- public API ----------------------------------------------------------------
    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None,
                 polite: bool = True) -> Any | None:
        """GET a JSON document. Returns None on 404. Successful results and 404s are cached."""
        params = dict(params or {})
        if polite and contact_email():
            params.setdefault("mailto", contact_email())
        key = "GET " + str(httpx.URL(url, params=params))
        cached = self._cache_get(key)
        if cached is not _MISSING:
            return cached
        resp = self.request("GET", url, params=params, headers=headers)
        if resp.status_code == 404:
            value = None
        elif resp.status_code >= 400:
            raise LookupFailed(f"GET {url}: HTTP {resp.status_code}")
        else:
            try:
                value = resp.json()
            except ValueError as e:
                raise LookupFailed(f"GET {url}: invalid JSON") from e
        self._cache_put(key, value)
        return value

    def cached(self, key: str, compute: Callable[[], Any]) -> Any:
        """Generic disk cache for JSON-serialisable values (e.g. URL liveness)."""
        value = self._cache_get(key)
        if value is _MISSING:
            value = compute()
            self._cache_put(key, value)
        return value
