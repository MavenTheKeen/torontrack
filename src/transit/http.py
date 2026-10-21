"""Bounded transient retries. Long Retry-After values fail instead of retrying early."""

import json
import math
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

BIKESHARE_HOST = "toronto.publicbikesystem.net"


class FetchError(RuntimeError):
    def __init__(self, message, *, retry_not_before=None):
        super().__init__(message)
        self.retry_not_before = retry_not_before


def retry_delay(header: str | None, attempt: int) -> float:
    backoff = float(2**attempt)
    if header is None:
        return backoff
    try:
        delay = float(header)
    except ValueError:
        try:
            delay = (parsedate_to_datetime(header) - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return backoff
    if not math.isfinite(delay):
        return backoff
    return max(0.0, backoff, delay)


class FeedClient:
    def __init__(self, client=None, sleep=time.sleep, attempts: int = 3):
        if not 1 <= attempts <= 5:
            raise ValueError("attempts must be between 1 and 5")
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(20.0, connect=5.0),
            follow_redirects=False,
            headers={"User-Agent": "Torontrack/0.1 (portfolio project)"},
        )
        self.sleep = sleep
        self.attempts = attempts

    def close(self):
        self.client.close()

    def fetch(self, url: str) -> dict:
        body = self.fetch_bytes(url)
        try:
            payload = json.loads(body)
        except (ValueError, RecursionError) as exc:
            raise FetchError("Source returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise FetchError("Source JSON root must be an object")
        return payload

    def fetch_bytes(
        self,
        url: str,
        *,
        host: str = BIKESHARE_HOST,
        max_bytes: int = 10 * 1024 * 1024,
        sink=None,
        headers: dict | None = None,
    ) -> bytes:
        """GET one official source URL; only `host` over HTTPS is ever contacted. With
        `sink` (a binary file), the body is written there instead of held in memory, and
        b"" is returned."""
        try:
            parsed = urlsplit(url)
            allowed = (
                parsed.scheme == "https"
                and parsed.hostname == host
                and parsed.port in (None, 443)
                and parsed.username is None
                and parsed.password is None
            )
        except ValueError:
            allowed = False
        if not allowed:
            raise FetchError(
                "Feed URL must use the official Toronto HTTPS host without credentials"
            )
        for attempt in range(self.attempts):
            delay = float(2**attempt)
            try:
                with self.client.stream(
                    "GET", url, headers=headers, follow_redirects=False
                ) as response:
                    body = bytearray()
                    size = 0
                    if sink is not None:
                        sink.seek(0)
                        sink.truncate()
                    if response.is_success:
                        for chunk in response.iter_bytes(chunk_size=65536):
                            size += len(chunk)
                            if sink is None:
                                body.extend(chunk)
                            else:
                                sink.write(chunk)
                            if size > max_bytes:
                                raise FetchError(
                                    f"Source response exceeds {max_bytes >> 20} MiB limit"
                                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt + 1 == self.attempts:
                    raise FetchError(
                        f"HTTP transport failed after {self.attempts} attempts"
                    ) from exc
            else:
                if response.status_code not in (408, 429, 500, 502, 503, 504):
                    if not response.is_success:
                        raise FetchError(f"HTTP {response.status_code}: non-retryable response")
                    return bytes(body)
                delay = retry_delay(response.headers.get("Retry-After"), attempt)
                try:
                    retry_at = datetime.now(UTC) + timedelta(seconds=delay)
                except OverflowError:
                    retry_at = datetime.max.replace(tzinfo=UTC)
                if attempt + 1 == self.attempts:
                    raise FetchError(
                        f"HTTP {response.status_code}: retries exhausted", retry_not_before=retry_at
                    )
                if delay > 60:
                    raise FetchError(
                        "Retry-After exceeds 60s budget; no early retry performed",
                        retry_not_before=retry_at,
                    )
            self.sleep(delay)
        raise AssertionError("unreachable")
