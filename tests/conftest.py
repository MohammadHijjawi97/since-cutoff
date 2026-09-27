"""Shared fixtures: a toy library with two versions, a fake PyPI, a scripted model and a local
HTTP server."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import textwrap
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import settings

from since_cutoff import cli, hosts, net
from since_cutoff.cache import DiskCache
from since_cutoff.engine import Engine, Settings
from since_cutoff.providers.base import Completion
from since_cutoff.pypi import PyPI, Release, SourceTree

# ------------------------------------------------------------ property tests
# The property tests (test_properties.py, test_parser_fuzz.py) try a modest number of
# generated examples, with no time limit per example, so a slow machine cannot fail them. In
# CI they try the same examples every run (derandomize): a red build reproduces. Locally each
# run tries new ones; HYPOTHESIS_PROFILE=thorough tries 40 times as many. A failure prints the
# example and how to replay it; no example database is kept, and Hypothesis's own files go to
# the temporary folder, not to a .hypothesis folder in the checkout.
os.environ.setdefault(
    "HYPOTHESIS_STORAGE_DIRECTORY", str(Path(tempfile.gettempdir()) / "since-cutoff-hypothesis")
)
settings.register_profile("dev", max_examples=50, deadline=None, database=None, print_blob=True)
settings.register_profile("ci", settings.get_profile("dev"), derandomize=True)
settings.register_profile("thorough", settings.get_profile("dev"), max_examples=2000)
settings.load_profile(
    os.environ.get("HYPOTHESIS_PROFILE") or ("ci" if os.environ.get("CI") else "dev")
)


# ------------------------------------------------------------ agent settings
@pytest.fixture(autouse=True)
def modern_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render Rich output the same way on every OS.

    On Windows, Rich falls back to legacy box characters (light bars throughout), while Linux
    and macOS draw table headers with heavy bars (U+2503). Tests that read rendered tables then
    pass locally and fail in CI; with this, Windows renders like the CI runners.
    """
    import rich.console

    monkeypatch.setattr(rich.console, "detect_legacy_windows", lambda: False)


@pytest.fixture(autouse=True)
def agent_home(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An empty home directory and none of the model variables, so that no test reads the
    developer's own coding-agent settings (:func:`since_cutoff.hosts.detect_model`)."""
    for name in hosts.ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(hosts, "user_home", lambda: home)
    return home


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


# A base class that becomes ``Base = Impl`` (transformers 5: ``PreTrainedTokenizer = PythonBackend``).
# Only ``legacy_decode`` really goes away.
ALIASLIB_V1 = {
    "aliaslib/__init__.py": "",
    "aliaslib/base.py": """
        class Base:
            \"\"\"Shared tokenizer behaviour.\"\"\"

            @classmethod
            def from_pretrained(cls, name: str) -> "Base":
                return cls()

            def encode(self, text: str) -> list[int]:
                return []

            def legacy_decode(self, ids: list[int]) -> str:
                return ""
    """,
    "aliaslib/tok.py": """
        from aliaslib.base import Base


        class BertTok(Base):
            pass


        class GptTok(Base):
            pass
    """,
}
ALIASLIB_V2 = {
    "aliaslib/__init__.py": "",
    "aliaslib/base.py": """
        class Impl:
            \"\"\"Shared tokenizer behaviour.\"\"\"

            @classmethod
            def from_pretrained(cls, name: str) -> "Impl":
                return cls()

            def encode(self, text: str) -> list[int]:
                return []


        Base = Impl
    """,
    "aliaslib/tok.py": ALIASLIB_V1["aliaslib/tok.py"],
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
def aliaslib(tmp_path: Path) -> tuple[SourceTree, SourceTree]:
    v1 = write_tree(tmp_path / "aliaslib-1.0", ALIASLIB_V1)
    v2 = write_tree(tmp_path / "aliaslib-2.0", ALIASLIB_V2)
    return (
        SourceTree("aliaslib", "1.0", v1, ("aliaslib",)),
        SourceTree("aliaslib", "2.0", v2, ("aliaslib",)),
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
    """A deterministic stand-in for an LLM that knows toylib 1.0 but not 2.0.

    With notes in the system prompt it writes correct code for every topic, unless
    ``learns_from`` maps topics to the text the notes must contain for that topic: then it only
    gets a topic right when its notes contain that text, and never gets an unlisted topic right
    from notes. That lets different notes blocks (``run --compare``) fix different changes.
    """

    def __init__(
        self,
        *,
        knows: set[str] | None = None,
        learns_from_notes: bool = True,
        learns_from: dict[str, str] | None = None,
    ) -> None:
        self.knows = knows or set()
        self.learns_from_notes = learns_from_notes
        self.learns_from = learns_from
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
        good = topic in self.knows or self._learns(topic, system)
        code = (GOOD_CODE if good else STALE_CODE)[topic]
        return Completion(f"```python\n{code}```")

    def _learns(self, topic: str, system: str) -> bool:
        """Do the notes in this solver prompt (if any) teach the model ``topic``?"""
        _, found, notes = system.partition("Project notes")
        if not found or not self.learns_from_notes:
            return False
        if self.learns_from is None:
            return True
        needle = self.learns_from.get(topic)
        return needle is not None and needle in notes

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


@pytest.fixture
def scripted_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_pypi: FakePyPI
) -> list[ScriptedModel]:
    """The CLI with the fake PyPI, a throwaway cache and a scripted model per run.

    Append one ScriptedModel per ``cli.main`` call; each run's engine takes the next.
    """
    monkeypatch.setenv("SINCE_CUTOFF_CACHE", str(tmp_path / "cli-cache"))
    models: list[ScriptedModel] = []

    def engine(settings: Settings, **kwargs: Any) -> Engine:
        model = models.pop(0)
        return Engine(
            settings, **{**kwargs, "pypi": fake_pypi, "provider_factory": lambda s: model}
        )

    monkeypatch.setattr(cli, "Engine", engine)
    return models


# ---------------------------------------------------------- local HTTP server
@dataclass
class Reply:
    """One scripted HTTP response. A dict or list body is sent as JSON. ``cut`` sends only that
    many bytes of the body, although Content-Length announces all of it, then hangs up;
    ``delay`` waits that many seconds before answering."""

    status: int = 200
    body: bytes | str | dict[str, Any] | list[Any] = b""
    headers: dict[str, str] = field(default_factory=dict)
    cut: int | None = None
    delay: float = 0.0

    def data(self) -> bytes:
        if isinstance(self.body, bytes):
            return self.body
        if isinstance(self.body, str):
            return self.body.encode("utf-8")
        return json.dumps(self.body).encode("utf-8")


@dataclass
class Seen:
    """A request the server received."""

    method: str
    path: str
    headers: dict[str, str]  # lower-cased names
    body: bytes


class LocalServer:
    """An HTTP server on 127.0.0.1 for code that talks to PyPI or a model API.

    ``routes`` maps a path to its replies, given in order; the last one repeats. A reply can
    be a callable that returns the Reply, to act at the moment the request arrives. Any other
    path gets a 404. ``seen`` logs every request, and ``errors`` what went wrong in answering
    one (the fixture fails the test on those).
    """

    def __init__(self) -> None:
        self.routes: dict[str, list[Reply | Callable[[], Reply]]] = {}
        self.seen: list[Seen] = []
        self.errors: list[BaseException] = []
        self._lock = threading.Lock()  # requests arrive on several threads
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                server._answer(self)

            do_POST = do_GET

            def log_message(self, *args: Any) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._httpd.block_on_close = False
        self._httpd.handle_error = self._failed
        self.url = f"http://127.0.0.1:{self._httpd.server_port}"
        # A short poll interval: shutdown() waits for it, after every test.
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()

    def _failed(self, request: object, address: object) -> None:
        # A client that gave up (a timeout test) makes the late answer fail: not an error here.
        # Anything else, such as a reply callable that raised, would otherwise only show as a
        # dropped connection, which the code under test retries.
        exc = sys.exc_info()[1]
        if exc is not None and not isinstance(exc, ConnectionError):
            self.errors.append(exc)

    def paths(self, prefix: str = "/") -> list[str]:
        return [s.path for s in self.seen if s.path.startswith(prefix)]

    def _answer(self, handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else b""
        headers = {k.lower(): v for k, v in handler.headers.items()}
        with self._lock:
            self.seen.append(Seen(handler.command, handler.path, headers, body))
            queue = self.routes.get(handler.path) or [Reply(404, b"not found")]
            item = queue.pop(0) if len(queue) > 1 else queue[0]
        reply = item() if callable(item) else item
        if reply.delay:
            time.sleep(reply.delay)
        data = reply.data()
        handler.send_response(reply.status)
        for name, value in reply.headers.items():
            handler.send_header(name, value)
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data if reply.cut is None else data[: reply.cut])
        handler.close_connection = True

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def http_server() -> Iterator[LocalServer]:
    server = LocalServer()
    try:
        yield server
    finally:
        server.close()
    assert not server.errors, f"the local server could not answer: {server.errors!r}"


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """HTTP retries back off without waiting; this lists the delays they asked for."""
    delays: list[float] = []
    monkeypatch.setattr(net, "time", SimpleNamespace(sleep=delays.append))
    return delays
