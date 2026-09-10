"""Shared HTTP machinery for providers: retries, backoff, and a small TTL cache.

Cost control (spec section 22) lives here. Every provider goes through
:meth:`ProviderHTTPClient.get_json`, which memoises responses per
``(method, url, params)`` for ``PROVIDER_CACHE_TTL_SECONDS``, so re-running a
collection cycle inside the TTL - or two routes needing the same airport-wide
departure board - costs one upstream call, not several.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging_config import get_logger
from app.providers.base import (
    ProviderAuthError,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderUnavailable,
)

log = get_logger(__name__)


class _TTLCache:
    """Tiny in-process cache. Bounded so a long-running worker cannot grow forever."""

    def __init__(self, ttl_seconds: int, max_entries: int = 512) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        if self._ttl <= 0:
            return
        if len(self._data) >= self._max:
            # Drop the entry closest to expiry; cheap and good enough at this size.
            oldest = min(self._data, key=lambda k: self._data[k][0])
            self._data.pop(oldest, None)
        self._data[key] = (time.monotonic() + self._ttl, value)

    def clear(self) -> None:
        self._data.clear()


def _cache_key(url: str, params: dict[str, Any] | None, headers_fingerprint: str) -> str:
    payload = json.dumps(
        {"url": url, "params": params or {}, "h": headers_fingerprint},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class ProviderHTTPClient:
    """An ``httpx.AsyncClient`` wrapper that turns transport problems into
    :class:`~app.providers.base.ProviderError` subclasses and retries the ones
    worth retrying."""

    def __init__(
        self,
        provider: str,
        base_url: str,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        cache_ttl: int | None = None,
    ) -> None:
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self._headers = headers or {}
        self._timeout = timeout or settings.collection_timeout_seconds
        self._client: httpx.AsyncClient | None = None
        self._cache = _TTLCache(
            cache_ttl if cache_ttl is not None else settings.provider_cache_ttl_seconds
        )
        self._headers_fp = hashlib.sha256(
            json.dumps(sorted(self._headers.keys())).encode()
        ).hexdigest()[:12]
        #: Counters the collector folds into its run summary.
        self.api_calls = 0
        self.cache_hits = 0

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=self._headers,
                timeout=httpx.Timeout(self._timeout),
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    def reset_counters(self) -> None:
        self.api_calls = 0
        self.cache_hits = 0

    async def get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        use_cache: bool = True,
        allow_404_as_empty: bool = True,
    ) -> Any:
        """GET ``path`` and return decoded JSON.

        Args:
            allow_404_as_empty: A 404 from a departure-board endpoint usually
                means "no flights in this window", not an error, so it maps to
                ``None`` rather than raising.

        Raises:
            ProviderAuthError: 401/403.
            ProviderRateLimited: 429, with ``Retry-After`` honoured when present.
            ProviderUnavailable: 5xx or a transport failure, after retries.
            ProviderResponseError: body was not valid JSON.
        """
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        key = _cache_key(f"{self.base_url}{path}", clean_params, self._headers_fp)

        if use_cache:
            cached = self._cache.get(key)
            if cached is not None:
                self.cache_hits += 1
                log.debug("provider.cache_hit", provider=self.provider, path=path)
                return cached

        attempts = max(1, settings.collection_max_retries)
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                self.api_calls += 1
                started = time.monotonic()
                response = await self.client.get(path, params=clean_params)
                latency_ms = (time.monotonic() - started) * 1000

                log.debug(
                    "provider.response",
                    provider=self.provider,
                    path=path,
                    status_code=response.status_code,
                    latency_ms=round(latency_ms, 1),
                    attempt=attempt,
                )

                if response.status_code in (401, 403):
                    raise ProviderAuthError(
                        f"{self.provider} rejected credentials (HTTP {response.status_code})",
                        provider=self.provider,
                    )
                if response.status_code == 404 and allow_404_as_empty:
                    self._cache.set(key, None) if use_cache else None
                    return None
                if response.status_code == 429:
                    retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                    raise ProviderRateLimited(
                        f"{self.provider} rate limited",
                        provider=self.provider,
                        retry_after_seconds=retry_after,
                    )
                if response.status_code >= 500:
                    raise ProviderUnavailable(
                        f"{self.provider} returned HTTP {response.status_code}",
                        provider=self.provider,
                    )
                if response.status_code >= 400:
                    raise ProviderResponseError(
                        f"{self.provider} returned HTTP {response.status_code}: "
                        f"{response.text[:200]}",
                        provider=self.provider,
                    )

                try:
                    payload = response.json()
                except ValueError as exc:
                    raise ProviderResponseError(
                        f"{self.provider} returned non-JSON body", provider=self.provider
                    ) from exc

                if use_cache:
                    self._cache.set(key, payload)
                return payload

            except (ProviderAuthError, ProviderResponseError):
                raise  # not worth retrying
            except ProviderRateLimited as exc:
                last_error = exc
                if attempt >= attempts:
                    raise
                delay = exc.retry_after_seconds or _backoff_delay(attempt)
                log.warning(
                    "provider.rate_limited",
                    provider=self.provider,
                    attempt=attempt,
                    sleeping=round(delay, 2),
                )
                await asyncio.sleep(min(delay, settings.collection_backoff_max_seconds))
            except (httpx.TimeoutException, httpx.TransportError, ProviderUnavailable) as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                delay = _backoff_delay(attempt)
                log.warning(
                    "provider.retry",
                    provider=self.provider,
                    attempt=attempt,
                    error=str(exc),
                    sleeping=round(delay, 2),
                )
                await asyncio.sleep(delay)

        raise ProviderUnavailable(
            f"{self.provider} unavailable after {attempts} attempts: {last_error}",
            provider=self.provider,
        )


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with full jitter, capped by configuration."""
    base = settings.collection_backoff_base_seconds
    ceiling = min(base * (2 ** (attempt - 1)), settings.collection_backoff_max_seconds)
    return random.uniform(base * 0.5, ceiling)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
