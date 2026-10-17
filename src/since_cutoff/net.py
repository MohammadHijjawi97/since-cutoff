"""Minimal HTTP helpers on top of urllib (no third-party HTTP client needed)."""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from typing import Any

from since_cutoff import __version__

USER_AGENT = f"since-cutoff/{__version__} (+https://github.com/MohammadHijjawi97/since-cutoff)"
_TRANSIENT_STATUS = (429, 500, 502, 503, 504, 529)


class HTTPError(Exception):
    def __init__(self, url: str, status: int | None, body: str = "") -> None:
        self.url = url
        self.status = status
        self.body = body
        super().__init__(f"{url}: {self.reason}")

    @property
    def transient(self) -> bool:
        """A failure that retrying later may fix (no response, 429, 5xx): what
        :func:`request` retries, as opposed to an answer such as 404."""
        return self.status is None or self.status in _TRANSIENT_STATUS

    @property
    def reason(self) -> str:
        """A short, human-readable description (no raw urllib reprs)."""
        if self.status is None:
            text = self.body.strip()
            if text.startswith("<urlopen error") and text.endswith(">"):
                text = text[len("<urlopen error") : -1].strip()
            return f"network error ({text or 'no response'})"
        snippet = " ".join(self.body.split())[:200]
        return f"HTTP {self.status}" + (f": {snippet}" if snippet else "")


class ResponseTooLarge(HTTPError):
    """The response went past ``max_bytes`` (see :func:`request`)."""

    @property
    def transient(self) -> bool:
        return False

    @property
    def reason(self) -> str:
        return self.body


def request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 60.0,
    retries: int = 3,
    max_bytes: int | None = None,
) -> bytes:
    """Perform an HTTP request with retries on transient failures (429, 5xx, network)."""
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    last: HTTPError | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data: bytes = resp.read() if max_bytes is None else resp.read(max_bytes)
                if max_bytes is not None and len(data) >= max_bytes:
                    raise ResponseTooLarge(url, None, f"response of {max_bytes} bytes or more")
                return data
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            last = HTTPError(url, exc.code, text)
            if exc.code in _TRANSIENT_STATUS and attempt < retries - 1:
                retry_after = exc.headers.get("retry-after") if exc.headers else None
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit() else 2.0**attempt
                )
                time.sleep(min(delay, 30.0))
                continue
            raise last from exc
        except (OSError, http.client.HTTPException) as exc:
            # URLError, timeouts, resets, and TLS errors while the body is read (ssl.SSLError,
            # which urllib does not wrap in a URLError once the response has begun).
            reason = getattr(exc, "reason", None) or exc
            last = HTTPError(url, None, str(reason))
            if attempt < retries - 1:
                time.sleep(2.0**attempt)
                continue
            raise last from exc
    raise last or HTTPError(url, None, "request failed")


def get_json(url: str, *, timeout: float = 60.0) -> Any:
    return json.loads(request(url, timeout=timeout).decode("utf-8"))


def post_json(url: str, payload: Any, *, headers: dict[str, str], timeout: float = 300.0) -> Any:
    data = json.dumps(payload).encode("utf-8")
    raw = request(
        url,
        method="POST",
        headers={"Content-Type": "application/json", **headers},
        body=data,
        timeout=timeout,
    )
    return json.loads(raw.decode("utf-8"))
