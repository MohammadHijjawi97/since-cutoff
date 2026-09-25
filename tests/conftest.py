"""Shared fixtures: a toy library with two versions, a fake PyPI and a scripted model."""

from __future__ import annotations

import json
import re
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from since_cutoff.cache import DiskCache
from since_cutoff.providers.base import Completion
from since_cutoff.pypi import PyPI, Release, SourceTree

# --------------------------------------------------------------- toy library
TOYLIB_V1 = {
    "toylib/__init__.py": """
        from toylib._client import AsyncClient, Client
        from toylib.helpers import Session, fetch, legacy_fetch

        __all__ = ["AsyncClient", "Client", "Session", "fetch", "legacy_fetch"]
    """,
    "toylib/_client.py": """
        class Client:
            \"\"\"A client for the Toy service.\"\"\"

            def __init__(self, api_key: str | None = None, timeout: float = 10.0) -> None:
                self.api_key = api_key

            def send(self, message: str, temperature: float = 1.0, stream: bool = False) -> str:
                \"\"\"Send a message and return the reply. `temperature` controls randomness.\"\"\"
                return message

            def close(self) -> None:
                \"\"\"Close the client.\"\"\"


        class AsyncClient:
            async def send(self, message: str, temperature: float = 1.0, stream: bool = False) -> str:
                return message
    """,
    "toylib/helpers.py": """
        def legacy_fetch(url: str) -> bytes:
            \"\"\"Fetch a URL.

            .. deprecated:: 1.0
                Use fetch instead.
            \"\"\"
            return b""


        def fetch(url: str, *, retries: int = 3) -> bytes:
            \"\"\"Fetch a URL with retries.\"\"\"
            return b""


        class Session:
            \"\"\"A reusable HTTP session.\"\"\"

            def get(self, url: str) -> bytes:
                return b""
    """,
}

TOYLIB_V2 = {
    "toylib/__init__.py": """
        from toylib._client import AsyncClient, Client
        from toylib.helpers import fetch
        from toylib.sessions import Session

        __all__ = ["AsyncClient", "Client", "Session", "fetch"]
    """,
    "toylib/_client.py": """
        from typing_extensions import deprecated


        class Client:
            \"\"\"A client for the Toy service.\"\"\"

            def __init__(self, api_key: str | None = None, timeout: float = 10.0) -> None:
                self.api_key = api_key

            def send(self, message: str, *, stream: bool = False) -> str:
                \"\"\"Send a message and return the reply.\"\"\"
                return message

            @deprecated("Use Client.shutdown() instead.")
            def close(self) -> None:
                \"\"\"Close the client.\"\"\"

            def shutdown(self) -> None:
                \"\"\"Shut the client down.\"\"\"


        class AsyncClient:
            async def send(self, message: str, *, stream: bool = False) -> str:
                return message
    """,
    "toylib/helpers.py": """
        def fetch(url: str, *, timeout: float, retries: int = 3) -> bytes:
            \"\"\"Fetch a URL with retries.\"\"\"
            return b""
    """,
    "toylib/sessions.py": """
        class Session:
            \"\"\"A reusable HTTP session.\"\"\"

            def get(self, url: str) -> bytes:
                return b""
    """,
}


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return root


@pytest.fixture
def toylib(tmp_path: Path) -> tuple[SourceTree, SourceTree]:
    v1 = write_tree(tmp_path / "toylib-1.0", TOYLIB_V1)
    v2 = write_tree(tmp_path / "toylib-2.0", TOYLIB_V2)
    return (
        SourceTree("toylib", "1.0", v1, ("toylib",)),
        SourceTree("toylib", "2.0", v2, ("toylib",)),
    )


@pytest.fixture
def cache(tmp_path: Path) -> DiskCache:
    return DiskCache(tmp_path / "cache")


# ------------------------------------------------------------------ fake PyPI
def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


class FakePyPI(PyPI):
    """PyPI with in-memory release dates and local source trees."""

    def __init__(
        self,
        cache: DiskCache,
        releases: dict[str, list[tuple[str, str]]],
        sources: dict[tuple[str, str], SourceTree],
    ):
        super().__init__(cache)
        self._releases = releases
        self._trees = sources

    def releases(self, name: str) -> list[Release]:
        out = [
            Release(v, _dt(d), False, ({"filename": f"{name}-{v}.whl"},))
            for v, d in self._releases.get(name, [])
        ]
        out.sort(key=lambda r: r.parsed)
        return out

    def source(self, name: str, version: str) -> SourceTree:
        return self._trees[(name, version)]


@pytest.fixture
def fake_pypi(cache: DiskCache, toylib: tuple[SourceTree, SourceTree]) -> FakePyPI:
    v1, v2 = toylib
    return FakePyPI(
        cache,
        {"toylib": [("0.9", "2024-06-01"), ("1.0", "2025-01-10"), ("2.0", "2025-10-01")]},
        {("toylib", "1.0"): v1, ("toylib", "2.0"): v2},
    )


# -------------------------------------------------------------- fake model
STALE_CODE = {
    "send": "from toylib import Client\n\ndef ask(q: str) -> str:\n    return Client().send(q, temperature=0.2)\n",
    "fetch": "from toylib import fetch\n\ndef grab(u: str) -> bytes:\n    return fetch(u, retries=5)\n",
    "legacy": "from toylib import legacy_fetch\n\ndef grab(u: str) -> bytes:\n    return legacy_fetch(u)\n",
    "session": "from toylib.helpers import Session\n\ndef grab(u: str) -> bytes:\n    return Session().get(u)\n",
    "close": "from toylib import Client\n\ndef done(c: Client) -> None:\n    c.close()\n",
}
GOOD_CODE = {
    "send": "from toylib import Client\n\ndef ask(q: str) -> str:\n    return Client().send(q)\n",
    "fetch": "from toylib import fetch\n\ndef grab(u: str) -> bytes:\n    return fetch(u, timeout=5.0, retries=5)\n",
    "legacy": "from toylib import fetch\n\ndef grab(u: str) -> bytes:\n    return fetch(u, timeout=5.0)\n",
    "session": "from toylib import Session\n\ndef grab(u: str) -> bytes:\n    return Session().get(u)\n",
    "close": "from toylib import Client\n\ndef done(c: Client) -> None:\n    c.shutdown()\n",
}
NOTE_BULLETS = {
    "send": (
        "`Client().send(q, temperature=...)`: `temperature` was removed. Call `Client().send(q)`.",
        GOOD_CODE["send"],
    ),
    "fetch": (
        "`fetch(url)` now needs `timeout`: call `fetch(url, timeout=5.0)`.",
        GOOD_CODE["fetch"],
    ),
    "legacy": ("`legacy_fetch(url)` is gone: use `fetch(url, timeout=5.0)`.", GOOD_CODE["legacy"]),
    "session": ("Import `Session` with `from toylib import Session`.", GOOD_CODE["session"]),
    "close": ("`client.close()` is deprecated: call `client.shutdown()`.", GOOD_CODE["close"]),
}


def topic_of(text: str) -> str | None:
    """Map a prompt (or a task) about the toy library to one of the scripted topics."""
    low = text.lower()
    for topic, needles in (
        ("send", ("temperature", "randomness", "reply")),
        ("legacy", ("legacy_fetch", "download the page", "old download")),
        ("fetch", ("fetch(", "download", "retries")),
        ("session", ("session", "reusable")),
        ("close", ("close", "shut")),
    ):
        if any(n in low for n in needles):
            return topic
    return None


class ScriptedModel:
    """A deterministic stand-in for an LLM that knows toylib 1.0 but not 2.0."""

    def __init__(self, *, knows: set[str] | None = None, learns_from_notes: bool = True) -> None:
        self.knows = knows or set()
        self.learns_from_notes = learns_from_notes
        self.calls: list[tuple[str, str]] = []

    provider_name = "scripted"
    model_name = "scripted-1"

    @property
    def key(self) -> str:
        return "scripted:scripted-1"

    def complete(self, system: str, user: str) -> Completion:
        self.calls.append((system, user))
        if system.startswith("You write evaluation tasks"):
            return Completion(json.dumps({"tasks": self._tasks(user), "skip_reason": None}))
        if system.startswith("You keep AGENTS.md"):
            topic = topic_of(re.search(r"Change: (.*)", user).group(1))  # type: ignore[union-attr]
            bullet, example = NOTE_BULLETS[topic or "send"]
            return Completion(json.dumps({"bullet": bullet, "example": example}))
        topic = topic_of(user) or "send"
        notes = "Project notes" in system
        good = topic in self.knows or (notes and self.learns_from_notes)
        code = (GOOD_CODE if good else STALE_CODE)[topic]
        return Completion(f"```python\n{code}```")

    @staticmethod
    def _tasks(prompt: str) -> list[str]:
        change = re.search(r"Change: (.*)", prompt).group(1)  # type: ignore[union-attr]
        topic = topic_of(change)
        base = {
            "send": "Write ask(q) that sends q with toylib and returns the reply with low randomness",
            "fetch": "Write grab(u) that uses toylib to download u with 5 retries",
            "legacy": "Write grab(u) that uses toylib to download the page u the simple way",
            "session": "Write grab(u) that uses a reusable toylib session to get u",
            "close": "Write done(c) that closes a toylib client c when you are finished",
        }.get(topic or "", "")
        if not base:
            return []
        return [f"{base} (variant {i})." for i in range(1, 4)]


@pytest.fixture
def scripted() -> ScriptedModel:
    return ScriptedModel()
