"""PyPI over HTTP, against a local index: metadata, downloads, and safe extraction of wheels
and sdists, including hostile archives."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import time
import tracemalloc
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from since_cutoff import pypi as pypi_module
from since_cutoff.cache import DiskCache
from since_cutoff.engine import NEW, Engine, Settings, stale_warning
from since_cutoff.errors import NoCodeError, PackageIndexError
from since_cutoff.project import load_project
from since_cutoff.pypi import (
    METADATA_TTL,
    SOURCE_SCHEMA,
    PyPI,
    SourceTree,
    _safe_target,
    is_placeholder,
)
from tests.conftest import LocalServer, Reply, ScriptedModel, write_tree

METADATA = "Metadata-Version: 2.1\nName: toy\nVersion: 1.0\n"


# ----------------------------------------------------------------- archives
def make_zip(files: dict[str, str | bytes], *, comment: bytes = b"") -> bytes:
    """A zip archive; member names are stored exactly as given (backslashes too)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            info = zipfile.ZipInfo("placeholder")
            info.filename = name  # ZipInfo() would turn "\" into "/" on Windows
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
        zf.comment = comment
    return buf.getvalue()


def make_tar(
    files: dict[str, str | bytes], links: dict[str, tuple[bytes, str]] | None = None
) -> bytes:
    """A .tar.gz; ``links`` maps member names to (tar type, link target)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, text in files.items():
            data = text.encode() if isinstance(text, str) else text
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        for name, (kind, target) in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type, info.linkname = kind, target
            tf.addfile(info)
    return buf.getvalue()


def wheel(files: dict[str, str | bytes], *, requires: tuple[str, ...] = ()) -> bytes:
    meta = METADATA + "".join(f"Requires-Dist: {r}\n" for r in requires) + "\nThe README.\n"
    return make_zip({**files, "toy-1.0.dist-info/METADATA": meta})


# ------------------------------------------------------------------- index
class Index:
    """A PyPI on the local server: project JSON at /pypi/<name>/json, files under /files/."""

    def __init__(self, server: LocalServer) -> None:
        self.server = server
        self.projects: dict[str, dict[str, list[dict[str, Any]]]] = {}

    def add(
        self,
        name: str,
        version: str,
        filename: str | None = None,
        blob: bytes = b"",
        *,
        uploaded: str = "2025-01-10T12:00:00.000000Z",
        **fields: Any,
    ) -> None:
        releases = self.projects.setdefault(name, {})
        files = releases.setdefault(version, [])
        if filename is not None:
            files.append(
                {
                    "filename": filename,
                    "url": f"{self.server.url}/files/{filename}",
                    "size": len(blob),
                    "upload_time_iso_8601": uploaded,
                    "yanked": False,
                    **fields,
                }
            )
            self.server.routes[f"/files/{filename}"] = [Reply(body=blob)]
        doc = {"info": {"name": name, "version": version, "summary": "Toy", "description": "x"}}
        self.server.routes[f"/pypi/{name}/json"] = [Reply(body={**doc, "releases": releases})]

    @property
    def downloads(self) -> list[str]:
        return self.server.paths("/files/")


@pytest.fixture
def index(http_server: LocalServer, monkeypatch: pytest.MonkeyPatch) -> Index:
    monkeypatch.setattr(pypi_module, "PYPI_JSON", http_server.url + "/pypi/{name}/json")
    return Index(http_server)


def files_in(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def leftovers(cache: DiskCache) -> list[str]:
    """Temporary extraction directories left in the cache."""
    return [p.name for p in (cache.root / "sources").glob("*.tmp-*")]


# ---------------------------------------------------------------- metadata
def test_metadata_is_fetched_once_kept_slim_and_refreshed_after_its_ttl(index, cache) -> None:
    index.add("toy-lib", "1.0", "toy_lib-1.0-py3-none-any.whl", b"x")
    assert PyPI(cache).project("Toy_Lib")["info"]["name"] == "toy-lib"
    assert PyPI(cache).project("toy.lib")["releases"].keys() == {"1.0"}  # from the cache
    assert index.server.paths() == ["/pypi/toy-lib/json"]
    assert cache.get("pypi", "toy-lib")["info"] == {  # no long description in the cache
        "name": "toy-lib",
        "version": "1.0",
        "summary": "Toy",
        "project_urls": None,
        "home_page": None,
    }
    old = time.time() - METADATA_TTL - 60
    os.utime(cache.path("pypi", "toy-lib"), (old, old))
    PyPI(cache).project("toy-lib")
    assert index.server.paths() == ["/pypi/toy-lib/json"] * 2


# Issue #54: when PyPI cannot be reached, a release list cached days ago beats none at all.
FETCHED = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
UNREACHABLE = {
    "503": Reply(503, "Service Unavailable"),
    "429": Reply(429, "Too Many Requests"),
    "cut-off": Reply(body={"info": {}, "releases": {}}, cut=5),  # no status: a network error
}


def _cached_then(index: Index, cache: DiskCache, reply: Reply) -> None:
    """toy 1.0's release list in the cache, fetched on FETCHED, and PyPI answering ``reply``."""
    index.add("toy", "1.0", "toy-1.0-py3-none-any.whl", wheel({"toy/__init__.py": ""}))
    PyPI(cache).project("toy")
    os.utime(cache.path("pypi", "toy"), (FETCHED.timestamp(), FETCHED.timestamp()))
    index.server.routes["/pypi/toy/json"] = [reply]


@pytest.mark.parametrize("reply", UNREACHABLE.values(), ids=UNREACHABLE.keys())
def test_an_older_cached_release_list_is_used_when_pypi_cannot_be_reached(
    index, cache, slept, reply
) -> None:
    _cached_then(index, cache, reply)
    pypi = PyPI(cache)
    assert [r.version for r in pypi.releases("toy")] == ["1.0"]
    assert pypi.stale == {"toy": date(2026, 9, 20)}
    assert len(slept) == 2  # it tried as hard as for any other request first
    # The rest of the run uses that copy without asking PyPI again.
    asked = len(index.server.paths("/pypi/"))
    assert pypi.release("Toy", "1.0").version == "1.0"
    assert pypi.version_at("toy", date(2026, 1, 1)).version == "1.0"
    assert len(index.server.paths("/pypi/")) == asked
    # A version the copy does not list may be newer than the copy, not missing from PyPI.
    with pytest.raises(PackageIndexError) as err:
        pypi.release("toy", "2.0")
    assert str(err.value) == (
        "toy==2.0 is not in the cached PyPI metadata from 2026-09-20; PyPI could not be reached"
    )


def test_a_long_running_process_asks_pypi_again(index, cache, slept, monkeypatch) -> None:
    """The MCP server keeps one PyPI for its whole life: an older copy is reused for
    STALE_REUSE seconds, then PyPI is asked again, and its answer replaces the copy."""
    _cached_then(index, cache, Reply(503, "Service Unavailable"))
    pypi = PyPI(cache)
    pypi.project("toy")
    asked = len(index.server.paths("/pypi/"))
    pypi.project("toy")
    assert len(index.server.paths("/pypi/")) == asked  # within STALE_REUSE: the copy
    monkeypatch.setattr(pypi_module, "STALE_REUSE", 0)
    index.add("toy", "2.0", "toy-2.0-py3-none-any.whl", wheel({"toy/__init__.py": ""}))
    assert pypi.release("toy", "2.0").version == "2.0"
    assert len(index.server.paths("/pypi/")) == asked + 1
    assert pypi.stale == {}


def test_a_copy_another_process_refreshed_is_no_longer_stale(index, cache, slept) -> None:
    _cached_then(index, cache, Reply(503, "Service Unavailable"))
    pypi = PyPI(cache)
    pypi.project("toy")
    assert pypi.stale == {"toy": date(2026, 9, 20)}
    os.utime(cache.path("pypi", "toy"))  # written just now, by another since-cutoff
    pypi.project("toy")
    assert pypi.stale == {}


def test_without_a_cached_release_list_an_unreachable_pypi_is_an_error(index, cache, slept):
    index.server.routes["/pypi/toy/json"] = [Reply(503, "Service Unavailable")]
    pypi = PyPI(cache)
    with pytest.raises(PackageIndexError, match="could not reach PyPI for 'toy': HTTP 503"):
        pypi.project("toy")
    assert pypi.stale == {}


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (Reply(404, "Not Found"), "'toy' is not on PyPI"),
        # A proxy that refuses the request is an answer, not a network that went away.
        (Reply(403, "Forbidden"), "could not reach PyPI for 'toy': HTTP 403: Forbidden"),
    ],
    ids=["404", "403"],
)
def test_an_answer_from_pypi_is_not_covered_by_an_older_copy(
    index, cache, slept, reply, message
) -> None:
    _cached_then(index, cache, reply)
    pypi = PyPI(cache)
    with pytest.raises(PackageIndexError) as err:
        pypi.project("toy")
    assert str(err.value) == message
    assert pypi.stale == {}


def test_a_scan_says_which_release_lists_are_older_copies(index, cache, slept, tmp_path):
    _cached_then(index, cache, Reply(503, "Service Unavailable"))
    app = write_tree(
        tmp_path / "app", {"requirements.txt": "toy==1.0\n", "main.py": "import toy\n"}
    )
    settings = Settings(model="scripted:scripted-1", cutoff=date(2025, 7, 31), jobs=2)
    engine = Engine(
        settings,
        store=cache,
        llm_cache=cache,
        pypi=PyPI(cache),
        provider_factory=lambda spec: ScriptedModel(),
    )
    scan = engine.scan(load_project(app), engine.resolve_target())
    assert scan.package("toy").locked == "1.0"
    assert scan.warnings == [
        "PyPI could not be reached: the release list of toy (cached 2026-09-20) is an older "
        "copy from the cache, so releases published after that day are unknown to this scan"
    ]
    # The same PyPI (as in the MCP server) scanning a project without toy says nothing of it.
    index.add("other", "1.0", "other-1.0-py3-none-any.whl", wheel({"other/__init__.py": ""}))
    other = write_tree(tmp_path / "other-app", {"requirements.txt": "other==1.0\n"})
    assert engine.scan(load_project(other), engine.resolve_target()).warnings == []


def test_offline_a_copy_cached_by_0_5_is_asked_for_once_per_scan(
    cache, slept, tmp_path, monkeypatch
) -> None:
    """A copy within METADATA_TTL from before the metadata kept the URLs, PyPI unreachable:
    each read of the scan (release, project_urls, version_at, releases, summary) asked PyPI
    again, 3 attempts each, 15 for one package. The copy is kept for STALE_REUSE as an older
    one is, and since it is fresh by date the scan does not call it stale."""
    attempts: list[str] = []

    def refused(req: urllib.request.Request, *args: Any, **kwargs: Any) -> Any:
        attempts.append(req.full_url)
        raise urllib.error.URLError(ConnectionRefusedError("connection refused"))

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    wheel_file = {
        "filename": "toy-1.0-py3-none-any.whl",
        "url": "https://files.example/toy-1.0-py3-none-any.whl",
        "size": 1,
        "upload_time_iso_8601": "2025-01-10T12:00:00.000000Z",
        "yanked": False,
    }
    old_shape = {"info": {"name": "toy", "version": "1.0", "summary": "Toy"}}
    cache.set("pypi", "toy", {**old_shape, "releases": {"1.0": [wheel_file]}})
    app = write_tree(
        tmp_path / "app", {"requirements.txt": "toy==1.0\n", "main.py": "import toy\n"}
    )
    settings = Settings(model="scripted:scripted-1", cutoff=date(2024, 12, 31), jobs=2)
    pypi = PyPI(cache)
    engine = Engine(
        settings,
        store=cache,
        llm_cache=cache,
        pypi=pypi,
        provider_factory=lambda spec: ScriptedModel(),
    )
    scan = engine.scan(load_project(app), engine.resolve_target())
    toy = scan.package("toy")
    assert (toy.status, toy.summary, toy.changelog) == (NEW, "Toy", None)
    assert len(attempts) <= 3
    assert scan.warnings == [] and scan.stale == {} and pypi.stale == {}


def test_the_warning_names_every_older_copy() -> None:
    assert stale_warning({"anthropic": date(2026, 9, 20), "openai": date(2026, 9, 21)}) == (
        "PyPI could not be reached: the release lists of anthropic (cached 2026-09-20) and "
        "openai (cached 2026-09-21) are older copies from the cache, so releases published "
        "after those days are unknown to this scan"
    )


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (None, "'toy' is not on PyPI"),
        (Reply(500, "oops"), "could not reach PyPI for 'toy': HTTP 500: oops"),
        (Reply(body="<html>maintenance</html>"), "PyPI returned an unexpected response for 'toy'"),
        (Reply(body={"releases": {}}), "PyPI returned an unexpected response for 'toy'"),
        (Reply(body=[1, 2]), "PyPI returned an unexpected response for 'toy'"),
    ],
    ids=["404", "500", "not-json", "no-info", "not-an-object"],
)
def test_index_failures_are_package_errors_and_not_cached(
    http_server, index, cache, slept, reply, message
) -> None:
    if reply is not None:
        http_server.routes["/pypi/toy/json"] = [reply]
    with pytest.raises(PackageIndexError) as info:
        PyPI(cache).project("toy")
    assert str(info.value) == message
    assert cache.get("pypi", "toy") is None


def test_releases_leave_out_what_cannot_be_dated_or_installed(index, cache) -> None:
    index.add("toy", "1.0", "toy-1.0.tar.gz", uploaded="2024-03-01T00:00:00Z")
    index.add("toy", "1.1")  # a release without files
    index.add("toy", "banana", "toy-banana.tar.gz")  # not a PEP 440 version
    index.add("toy", "1.2", "toy-1.2.tar.gz", upload_time_iso_8601="not a time")
    index.add("toy", "9.0", "toy-9.0.tar.gz", uploaded="2025-02-01T00:00:00Z")
    index.add("toy", "9.0", "toy-9.0-py3-none-any.whl", uploaded="2025-01-15T09:30:00Z")
    index.add("toy", "10.0", "toy-10.0.tar.gz", uploaded="2025-06-01T00:00:00Z", yanked=True)
    index.add("toy", "10.1", "toy-10.1.tar.gz", yanked=True)
    index.add("toy", "10.1", "toy-10.1-py3-none-any.whl")
    got = [(r.version, r.uploaded.date(), r.yanked) for r in PyPI(cache).releases("toy")]
    assert got == [
        ("1.0", date(2024, 3, 1), False),
        ("9.0", date(2025, 1, 15), False),  # the first upload dates a release
        ("10.0", date(2025, 6, 1), True),
        ("10.1", date(2025, 1, 10), False),  # yanked only when every file is
    ]


def test_release_lookup_follows_pep_440(index, cache) -> None:
    for version in ("1.0", "2.0", "3.0rc1"):
        index.add("toy", version, f"toy-{version}.tar.gz")
    pypi = PyPI(cache)
    assert pypi.release("toy", "1.0.0").version == "1.0"
    assert pypi.release("toy", "2.0+cpu").version == "2.0"
    with pytest.raises(PackageIndexError, match=r"^toy==latest is not a valid \(PEP 440\)"):
        pypi.release("toy", "latest")
    with pytest.raises(PackageIndexError, match=r"^toy==2\.1 is not on PyPI$"):
        pypi.release("toy", "2.1")
    with pytest.raises(PackageIndexError, match=r"^toy==4\.0\+cpu is not on PyPI$"):
        pypi.release("toy", "4.0+cpu")


def test_latest_skips_yanked_releases(index, cache) -> None:
    index.add("toy", "1.0", "toy-1.0.tar.gz")
    index.add("toy", "2.0", "toy-2.0.tar.gz", yanked=True)
    index.add("gone", "1.0", "gone-1.0.tar.gz", yanked=True)
    pypi = PyPI(cache)
    assert pypi.latest("toy").version == "1.0"
    with pytest.raises(PackageIndexError, match="'gone' has no final releases on PyPI"):
        pypi.latest("gone")


def test_best_match_honours_requires_python_and_ignores_broken_specifiers(index, cache) -> None:
    index.add("toy", "1.0", "toy-1.0.tar.gz", requires_python=">=3.8")
    index.add("toy", "2.0", "toy-2.0.tar.gz", requires_python=">=3.11")
    index.add("toy", "2.1", "toy-2.1.tar.gz", requires_python=">=3.11")
    index.add("toy", "2.1", "toy-2.1-py3-none-any.whl", requires_python=">=3.12")
    pypi = PyPI(cache)
    assert pypi.best_match("toy", "", python="3.10").version == "1.0"
    assert pypi.best_match("toy", "", python="3.11").version == "2.1"  # one file installs
    assert pypi.best_match("toy", "<2.1", python="3.13").version == "2.0"
    assert pypi.best_match("toy", "", python="three").version == "2.1"  # not a version: no filter
    assert pypi.best_match("toy", "~=banana").version == "2.1"  # not a specifier: any version
    assert pypi.best_match("toy", ">=5") is None


def test_an_invalid_requires_python_does_not_hide_a_release(index, cache) -> None:
    index.add("toy", "1.0", "toy-1.0.tar.gz", requires_python=">=3.8")
    index.add("toy", "2.0", "toy-2.0.tar.gz", requires_python=">=3.6.*")  # as pip: installable
    assert PyPI(cache).best_match("toy", "", python="3.10").version == "2.0"


# ----------------------------------------------------------------- sources
def test_a_wheel_is_downloaded_once_and_only_its_sources_kept(index, cache) -> None:
    blob = wheel(
        {
            "toy/__init__.py": "from toy.core import run\n",
            "toy/core.py": "def run() -> None: ...\n",
            "toy/core.pyi": "def run() -> None: ...\n",
            "toy/py.typed": "",
            "toy/_speedups.cp313-win_amd64.pyd": b"MZ\x90\x00",
            "toy/data/logo.png": b"\x89PNG",
            "toy-1.0.dist-info/top_level.txt": "toy\n",
            "toy-1.0.dist-info/RECORD": "",
        },
        requires=("httpx>=0.27", 'rich; extra == "cli"'),
    )
    index.add("toy", "1.0", "toy-1.0-py3-none-any.whl", blob)
    tree = PyPI(cache).source("toy", "1.0")
    assert (tree.name, tree.version, tree.import_names) == ("toy", "1.0", ("toy",))
    assert tree.requires == ("httpx>=0.27", 'rich; extra == "cli"')
    assert tree.root == cache.root / "sources" / "toy-1.0"
    assert files_in(tree.root) == {
        ".since-cutoff.json",
        "toy/__init__.py",
        "toy/core.py",
        "toy/core.pyi",
        "toy/py.typed",
    }
    # Another process (a new PyPI) finds the tree without asking PyPI anything.
    requests = len(index.server.seen)
    assert tree.compiled == ("toy._speedups",)  # no _speedups.py or .pyi next to it
    assert PyPI(cache).source("Toy", "1.0") == SourceTree(
        "Toy", "1.0", tree.root, ("toy",), tree.requires, ("toy._speedups",)
    )
    assert len(index.server.seen) == requests
    assert index.downloads == ["/files/toy-1.0-py3-none-any.whl"] and leftovers(cache) == []


SDIST = {
    "toy-1.0/PKG-INFO": METADATA + "Requires-Dist: attrs>=23\n\nThe README.\n",
    "toy-1.0/setup.py": "from setuptools import setup\nsetup()\n",
    "toy-1.0/README.md": "# toy\n",
    "toy-1.0/src/toy/__init__.py": "",
    "toy-1.0/src/toy/core.py": "def run() -> None: ...\n",
    "toy-1.0/tests/test_core.py": "def test_run(): ...\n",
}


@pytest.mark.parametrize(
    ("filename", "archive"),
    [("toy-1.0.tar.gz", make_tar), ("toy-1.0.zip", make_zip)],
    ids=["tar.gz", "zip"],
)
def test_an_sdist_is_used_when_there_is_no_wheel(index, cache, filename, archive) -> None:
    index.add("toy", "1.0", filename, archive(SDIST))
    tree = PyPI(cache).source("toy", "1.0")
    assert (tree.import_names, tree.requires) == (("toy",), ("attrs>=23",))
    assert files_in(tree.root) == {".since-cutoff.json", "toy/__init__.py", "toy/core.py"}
    assert leftovers(cache) == []


def test_a_flat_sdist_names_its_modules_but_not_its_build_scripts(index, cache) -> None:
    sdist = {  # and no PKG-INFO: no requirements
        "toy-1.0/setup.py": "",
        "toy-1.0/noxfile.py": "",
        "toy-1.0/toy_mod.py": "def run() -> None: ...\n",
        "toy-1.0/_private.py": "",
        "toy-1.0/toy_stubs/__init__.pyi": "",
    }
    index.add("toy", "1.0", "toy-1.0.tar.gz", make_tar(sdist))
    tree = PyPI(cache).source("toy", "1.0")
    assert (tree.import_names, tree.requires) == (("toy_mod", "toy_stubs"), ())


def test_a_wheel_records_its_compiled_modules_and_still_extracts_only_sources(index, cache) -> None:
    """Issue #52: a module that became an extension module, or lost its stub, looked
    removed. The tree names the compiled modules a static reading cannot see."""
    elf, pe = b"\x7fELF", b"MZ\x90\x00"
    blob = wheel(
        {
            "toy/__init__.py": "",
            "toy/fast.cpython-312-x86_64-linux-gnu.so": elf,
            "toy/limited.abi3.so": elf,
            "toy/win.cp313-win_amd64.pyd": pe,
            "toy/plain.pyd": pe,
            "toy/sub/__init__.py": "",
            "toy/sub/inner.cpython-312-darwin.so": elf,
            "toy/compiled_pkg/__init__.cpython-312-x86_64-linux-gnu.so": elf,
            # Readable: a stub, or the Python source next to a compiled copy (mypyc, black).
            "toy/stubbed.cpython-312-x86_64-linux-gnu.so": elf,
            "toy/stubbed.pyi": "def f() -> None: ...\n",
            "toy/both.py": "def f() -> None: ...\n",
            "toy/both.cpython-312-x86_64-linux-gnu.so": elf,
            # Not modules: a vendored shared library, mypyc's hashed helper (its hash may start
            # with a letter or a digit).
            "toy.libs/libgfortran-040039e1.so.5.0.0": elf,
            "toy.libs/libz.so": elf,
            "a3f2b9e1d0c4__mypyc.cpython-312-x86_64-linux-gnu.so": elf,
            "30fcd23745efe32ce681__mypyc.cpython-312-x86_64-linux-gnu.so": elf,
        }
    )
    index.add("toy", "1.0", "toy-1.0-cp312-cp312-manylinux_2_17_x86_64.whl", blob)
    tree = PyPI(cache).source("toy", "1.0")
    assert tree.compiled == (
        "toy.compiled_pkg.__init__",  # its submodules have files of their own
        "toy.fast",
        "toy.limited",
        "toy.plain",
        "toy.sub.inner",
        "toy.win",
    )
    assert files_in(tree.root) == {
        ".since-cutoff.json",
        "toy/__init__.py",
        "toy/both.py",
        "toy/stubbed.pyi",
        "toy/sub/__init__.py",
    }
    assert PyPI(cache).source("toy", "1.0").compiled == tree.compiled  # from the marker


@pytest.mark.parametrize(
    ("filename", "archive"),
    [("toy-1.0.tar.gz", make_tar), ("toy-1.0.zip", make_zip)],
    ids=["tar.gz", "zip"],
)
def test_an_sdist_records_cython_modules_without_a_python_source(
    index, cache, filename, archive
) -> None:
    sdist = {
        **SDIST,
        "toy-1.0/src/toy/fast.pyx": "def speedy(int x): return x\n",
        "toy-1.0/src/toy/fast.pxd": "cdef int helper(int x)\n",
        "toy-1.0/src/toy/fallback.pyx": "def f(): pass\n",
        "toy-1.0/src/toy/fallback.py": "def f() -> None: ...\n",
        "toy-1.0/benchmarks/bench.pyx": "",  # outside the package root
    }
    index.add("toy", "1.0", filename, archive(sdist))
    tree = PyPI(cache).source("toy", "1.0")
    assert tree.compiled == ("toy.fast",)
    assert "toy/fast.pyx" not in files_in(tree.root)


TOKEN = "sc7f3a"  # in the name of every file a hostile archive tries to write


def escaped(tmp_path: Path) -> list[str]:
    places = [*tmp_path.rglob(f"*{TOKEN}*"), *Path(tmp_path.anchor).glob(f"*{TOKEN}*")]
    return sorted(str(p) for p in places)


@pytest.fixture
def deep_cache(tmp_path: Path) -> DiskCache:
    """A cache deep enough below tmp_path that a "../" escape still lands inside tmp_path."""
    return DiskCache(tmp_path / "a" / "b" / "c" / "cache")


def test_wheel_members_cannot_escape_the_extraction_directory(index, deep_cache, tmp_path) -> None:
    hostile = {
        f"../../escape_{TOKEN}.py": "boom",
        f"toy/../../../../../up_{TOKEN}.py": "boom",
        f"/abs_{TOKEN}.py": "boom",
        f"C:/drive_{TOKEN}.py": "boom",
        f"C:drive_relative_{TOKEN}.py": "boom",
        f"toy/C:stream_{TOKEN}.py": "boom",
        f"\\\\host\\share\\unc_{TOKEN}.py": "boom",
        f"toy\\..\\..\\..\\back_{TOKEN}.py": "boom",
    }
    blob = wheel({"toy/__init__.py": "", **hostile, "toy-1.0.dist-info/top_level.txt": "toy\n"})
    index.add("toy", "1.0", "toy-1.0-py3-none-any.whl", blob)
    tree = PyPI(deep_cache).source("toy", "1.0")
    assert tree.import_names == ("toy",)
    assert files_in(tree.root) == {".since-cutoff.json", "toy/__init__.py"}
    assert escaped(tmp_path) == []


def test_members_windows_would_rename_or_could_not_delete_are_left_out(index, cache) -> None:
    # Windows writes "toy./x.py" as "toy/x.py", over the real one, and makes a "..." or
    # ".. " folder that nothing can delete afterwards. No module is named like that.
    odd = {"toy./x.py": "boom", ".../y.py": "boom", "toy/sub. /z.py": "boom", "toy/v..py": "ok"}
    blob = wheel({"toy/__init__.py": "", "toy/x.py": "x = 1\n", **odd})
    index.add("toy", "1.0", "toy-1.0-py3-none-any.whl", blob)
    tree = PyPI(cache).source("toy", "1.0")
    assert files_in(tree.root) == {".since-cutoff.json", "toy/__init__.py", "toy/x.py", "toy/v..py"}
    assert (tree.root / "toy" / "x.py").read_text() == "x = 1\n"
    assert leftovers(cache) == []


def test_sdist_members_cannot_escape_or_link_out(index, deep_cache, tmp_path) -> None:
    outside = tmp_path / f"secret_{TOKEN}.txt"
    outside.write_text("not for the package")
    files = {
        "toy-1.0/PKG-INFO": METADATA,
        "toy-1.0/toy/__init__.py": "",
        f"toy-1.0/../../../escape_{TOKEN}.py": "boom",
        f"toy-1.0/toy/../../../../../../up_{TOKEN}.py": "boom",
        f"toy-1.0/C:\\drive_{TOKEN}.py": "boom",
        f"no_top_directory_{TOKEN}.py": "boom",
    }
    links = {
        f"toy-1.0/toy/symlink_{TOKEN}.py": (tarfile.SYMTYPE, str(outside)),
        f"toy-1.0/toy/uplink_{TOKEN}.py": (tarfile.SYMTYPE, "../../../../../../../etc/passwd"),
        f"toy-1.0/toy/hardlink_{TOKEN}.py": (tarfile.LNKTYPE, "toy-1.0/toy/__init__.py"),
        f"toy-1.0/toy/fifo_{TOKEN}.py": (tarfile.FIFOTYPE, ""),
    }
    index.add("toy", "1.0", "toy-1.0.tar.gz", make_tar(files, links))
    tree = PyPI(deep_cache).source("toy", "1.0")
    assert files_in(tree.root) == {".since-cutoff.json", "toy/__init__.py"}
    assert escaped(tmp_path) == [str(outside)]
    assert outside.read_text() == "not for the package"


def link_directory(link: Path, target: Path) -> None:
    """A link to a directory: a symlink, or on Windows without that privilege a junction."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True
        )


def test_a_path_through_a_linked_directory_is_refused(tmp_path) -> None:
    """The last check on a member's path: where it resolves to. Names alone cannot tell that
    ``pkg/out/x.py`` leaves the directory when ``pkg/out`` is a link."""
    outside, dest = tmp_path / "outside", tmp_path / "dest"
    outside.mkdir()
    (dest / "pkg").mkdir(parents=True)
    link_directory(dest / "pkg" / "out", outside)
    assert _safe_target(dest, "pkg/out/x.py") is None
    assert _safe_target(dest, "pkg/out") is not None  # the link itself is inside
    assert _safe_target(dest, "pkg/x.py") == dest / "pkg" / "x.py"


@pytest.mark.parametrize(
    ("filename", "archive"),
    [("toy-1.0-py3-none-any.whl", make_zip), ("toy-1.0.tar.gz", make_tar)],
    ids=["wheel", "sdist"],
)
def test_oversized_members_are_left_out(index, cache, monkeypatch, filename, archive) -> None:
    monkeypatch.setattr(pypi_module, "_MAX_MEMBER_BYTES", 64)
    top = "" if filename.endswith(".whl") else "toy-1.0/"
    members = {
        "PKG-INFO": METADATA,
        "toy/__init__.py": "x = 1\n",
        "toy/generated.py": "#" * 65,  # one byte over
        "toy/edge.py": "#" * 64,
    }
    index.add("toy", "1.0", filename, archive({top + k: v for k, v in members.items()}))
    tree = PyPI(cache).source("toy", "1.0")
    assert files_in(tree.root) == {".since-cutoff.json", "toy/__init__.py", "toy/edge.py"}


@pytest.mark.parametrize("limit", ["_MAX_TOTAL_BYTES", "_MAX_MEMBERS"])
def test_an_archive_that_expands_too_far_is_refused(index, cache, monkeypatch, limit) -> None:
    monkeypatch.setattr(pypi_module, limit, 3)
    blob = wheel({f"toy/m{i}.py": "x = 1\n" for i in range(5)})
    index.add("toy", "1.0", "toy-1.0-py3-none-any.whl", blob)
    with pytest.raises(PackageIndexError) as info:
        PyPI(cache).source("toy", "1.0")
    assert str(info.value) == (
        "could not extract toy-1.0-py3-none-any.whl: archive expands beyond the extraction limits"
    )
    assert not (cache.root / "sources" / "toy-1.0").exists() and leftovers(cache) == []


def test_huge_metadata_files_are_read_only_in_part(index, cache) -> None:
    """METADATA, top_level.txt and .pth files were read whole: a small wheel whose metadata
    expands to gigabytes filled the memory. Sources were always capped (_MAX_MEMBER_BYTES)."""
    heads = {
        "toy-1.0.dist-info/METADATA": METADATA + "Requires-Dist: attrs\n\n",
        "toy-1.0.dist-info/top_level.txt": "toy\n",
        "toy.pth": "# nothing to add\n",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("toy/__init__.py", "")
        padding = b" " * (1 << 20)
        for name, head in heads.items():
            with zf.open(name, "w") as fh:  # 32 MB each, about 32 KB compressed
                fh.write(head.encode())
                for _ in range(32):
                    fh.write(padding)
    index.add("toy", "1.0", "toy-1.0-py3-none-any.whl", buf.getvalue())
    tracemalloc.start()
    try:
        tree = PyPI(cache).source("toy", "1.0")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert (tree.import_names, tree.requires) == (("toy",), ("attrs",))
    assert peak < 24 << 20, f"peak {peak >> 20} MB"  # about 10 MB; 77 MB when read whole


@pytest.mark.parametrize("filename", ["toy-1.0-py3-none-any.whl", "toy-1.0.tar.gz", "toy-1.0.zip"])
def test_a_corrupt_download_is_a_package_error(index, cache, filename) -> None:
    index.add("toy", "1.0", filename, b"<html>Bad gateway</html>")
    with pytest.raises(PackageIndexError, match=rf"^could not extract {filename}: "):
        PyPI(cache).source("toy", "1.0")
    assert leftovers(cache) == []


def test_a_download_matching_its_pypi_sha256_extracts(index, cache) -> None:
    blob = wheel({"toy/__init__.py": "x = 1\n"})
    digest = hashlib.sha256(blob).hexdigest()
    index.add("toy", "1.0", "toy-1.0-py3-none-any.whl", blob, digests={"sha256": digest})
    tree = PyPI(cache).source("toy", "1.0")
    assert files_in(tree.root) == {".since-cutoff.json", "toy/__init__.py"}
    assert leftovers(cache) == []


def test_a_download_with_a_mismatched_sha256_is_refused(index, cache) -> None:
    blob = wheel({"toy/__init__.py": "x = 1\n"})
    actual_hash = hashlib.sha256(blob).hexdigest()
    wrong_hash = "a" * 64
    index.add("toy", "1.0", "toy-1.0-py3-none-any.whl", blob, digests={"sha256": wrong_hash})
    with pytest.raises(PackageIndexError) as info:
        PyPI(cache).source("toy", "1.0")
    msg = str(info.value)
    assert "toy-1.0-py3-none-any.whl" in msg
    assert wrong_hash in msg
    assert actual_hash in msg
    assert not (cache.root / "sources" / "toy-1.0").exists()
    assert leftovers(cache) == []


def test_a_download_without_a_sha256_still_extracts(index, cache) -> None:
    blob = wheel({"toy/__init__.py": "x = 1\n"})
    index.add("toy", "1.0", "toy-1.0-py3-none-any.whl", blob)
    tree = PyPI(cache).source("toy", "1.0")
    assert files_in(tree.root) == {".since-cutoff.json", "toy/__init__.py"}
    assert leftovers(cache) == []


def test_a_failed_download_names_the_file(index, cache, slept) -> None:
    index.add("toy", "1.0", "toy-1.0-py3-none-any.whl", wheel({"toy/__init__.py": ""}))
    index.server.routes["/files/toy-1.0-py3-none-any.whl"] = [Reply(403, "Forbidden")]
    with pytest.raises(PackageIndexError) as info:
        PyPI(cache).source("toy", "1.0")
    assert str(info.value) == "could not download toy-1.0-py3-none-any.whl: HTTP 403: Forbidden"


def test_the_download_limit_is_checked_before_and_while_downloading(index, cache) -> None:
    limit_mb = 0.002  # int(0.002 MB) = 2097 bytes
    limit = int(limit_mb * 1024 * 1024)
    small = wheel({"toy/__init__.py": ""})
    padding = limit - len(make_zip({"toy/__init__.py": ""}))
    exact = make_zip({"toy/__init__.py": ""}, comment=b"#" * padding)
    assert len(exact) == limit
    index.add("toy", "1.0", "toy-1.0-py3-none-any.whl", exact)
    index.add("toy", "2.0", "toy-2.0-py3-none-any.whl", exact + b"#")
    index.add("toy", "3.0", "toy-3.0-py3-none-any.whl", exact + b"#", size=len(small))
    pypi = PyPI(cache, max_download_mb=limit_mb)
    assert pypi.source("toy", "1.0").import_names == ("toy",)  # the limit itself is allowed
    with pytest.raises(PackageIndexError, match=r"above the 0\.002 MB download limit"):
        pypi.source("toy", "2.0")  # PyPI's size: refused without a download
    assert index.downloads == ["/files/toy-1.0-py3-none-any.whl"]
    # A size that understates the file: the download stops at the limit. This said "could not
    # download ...: network error (response larger than 2098 bytes)".
    with pytest.raises(PackageIndexError) as info:
        pypi.source("toy", "3.0")
    assert str(info.value) == "toy==3.0 is above the 0.002 MB download limit (--max-download-mb)"


def test_a_release_without_modules_is_no_code_or_a_metapackage(index, cache) -> None:
    index.add("empty", "1.0", "empty-1.0-py3-none-any.whl", wheel({"README.txt": "soon"}))
    with pytest.raises(NoCodeError, match=r"could not find importable modules in empty-1\.0-py3"):
        PyPI(cache).source("empty", "1.0")
    requires = ("a>=1", "b", 'c; extra == "all"', "not a requirement!", "d<2", "e")
    index.add("meta", "1.0", "meta-1.0-py3-none-any.whl", wheel({}, requires=requires))
    with pytest.raises(PackageIndexError) as info:
        PyPI(cache).source("meta", "1.0")
    assert not isinstance(info.value, NoCodeError)
    assert str(info.value) == (
        "meta 1.0 is a metapackage without code of its own: it installs a>=1, b, d<2, ...; "
        "check those packages instead"
    )


def test_a_version_without_files_or_on_pypi_is_an_error(index, cache) -> None:
    index.add("toy", "1.0", "toy-1.0.exe", b"MZ")
    with pytest.raises(PackageIndexError, match=r"^toy==1\.0 has no wheel or sdist on PyPI$"):
        PyPI(cache).source("toy", "1.0")
    with pytest.raises(PackageIndexError, match=r"^toy==9\.9 is not on PyPI$"):
        PyPI(cache).source("toy", "9.9")


def test_a_yanked_version_is_still_extracted_when_pinned(index, cache) -> None:
    index.add("toy", "1.0", "toy-1.0-py3-none-any.whl", wheel({"toy/__init__.py": ""}), yanked=True)
    assert PyPI(cache).source("toy", "1.0").import_names == ("toy",)


# ------------------------------------------------ the cache of extracted trees
def test_a_broken_tree_in_the_cache_is_replaced(index, cache) -> None:
    broken = cache.root / "sources" / "toy-1.0"
    write_tree(broken, {"toy/half_written.py": "def f(\n"})  # no marker: a crash mid-way
    index.add("toy", "1.0", "toy-1.0-py3-none-any.whl", wheel({"toy/__init__.py": ""}))
    tree = PyPI(cache).source("toy", "1.0")
    assert tree.root == broken
    assert files_in(broken) == {".since-cutoff.json", "toy/__init__.py"}


@pytest.mark.parametrize(
    "marker",
    [
        {"schema": 1, "import_names": ["toy"], "requires": []},
        # Before SOURCE_SCHEMA 3 the compiled modules were not recorded (issue #52).
        {"schema": 2, "import_names": ["toy"], "requires": []},
        {"schema": SOURCE_SCHEMA, "import_names": ["win32\\lib\\toy"], "requires": []},
        "not json",
    ],
    ids=["schema-1", "schema-2", "backslash-names", "unreadable"],
)
def test_a_tree_from_an_earlier_version_is_extracted_again(index, cache, marker) -> None:
    old = cache.root / "sources" / "toy-1.0"
    write_tree(old, {"toy/__init__.py": ""})
    text = marker if isinstance(marker, str) else json.dumps(marker)
    (old / ".since-cutoff.json").write_text(text, encoding="utf-8")
    index.add("toy", "1.0", "toy-1.0-py3-none-any.whl", wheel({"toy/__init__.py": ""}))
    assert PyPI(cache).source("toy", "1.0").import_names == ("toy",)
    assert index.downloads == ["/files/toy-1.0-py3-none-any.whl"]


def _publish_as_another_process(root: Path) -> None:
    write_tree(root, {"toy/__init__.py": ""})
    marker = {
        "schema": SOURCE_SCHEMA,
        "import_names": ["toy"],
        "requires": ["from-the-other-process"],
    }
    (root / ".since-cutoff.json").write_text(json.dumps(marker), encoding="utf-8")


def test_a_tree_another_process_published_meanwhile_is_used(index, cache) -> None:
    root = cache.root / "sources" / "toy-1.0"
    blob = wheel({"toy/__init__.py": ""})
    index.add("toy", "1.0", "toy-1.0-py3-none-any.whl", blob)

    def download() -> Reply:  # while this process downloads, another one finishes
        _publish_as_another_process(root)
        return Reply(body=blob)

    index.server.routes["/files/toy-1.0-py3-none-any.whl"] = [download]
    tree = PyPI(cache).source("toy", "1.0")
    assert tree.requires == ("from-the-other-process",)
    assert leftovers(cache) == []


def test_a_tree_that_cannot_be_moved_into_place_is_a_package_error(
    index, cache, monkeypatch
) -> None:
    """On Windows a virus scanner can hold a file that was just written: the rename that
    publishes the tree fails. That ended the scan with a PermissionError traceback."""
    index.add("toy", "1.0", "toy-1.0-py3-none-any.whl", wheel({"toy/__init__.py": ""}))

    def locked(self: Path, target: Path) -> Path:
        raise PermissionError(13, "Access is denied", str(self))

    monkeypatch.setattr(Path, "rename", locked)
    with pytest.raises(PackageIndexError, match=r"^could not publish the sources of toy==1\.0$"):
        PyPI(cache).source("toy", "1.0")
    assert leftovers(cache) == []


def test_two_processes_replacing_a_broken_tree_do_not_crash(index, cache, monkeypatch) -> None:
    """Both find a broken tree; the other one replaces it between this one's removal of it and
    its rename. That rename then failed with an OSError that ended the scan."""
    root = cache.root / "sources" / "toy-1.0"
    write_tree(root, {"toy/half_written.py": ""})
    index.add("toy", "1.0", "toy-1.0-py3-none-any.whl", wheel({"toy/__init__.py": ""}))
    rmtree = pypi_module.shutil.rmtree

    def rmtree_then_the_other_process_publishes(path: Any, *args: Any, **kwargs: Any) -> None:
        rmtree(path, *args, **kwargs)
        if Path(path) == root:
            _publish_as_another_process(root)

    monkeypatch.setattr(pypi_module.shutil, "rmtree", rmtree_then_the_other_process_publishes)
    tree = PyPI(cache).source("toy", "1.0")
    assert tree.requires == ("from-the-other-process",)
    assert leftovers(cache) == []


# ------------------------------------------------------------ placeholders
@pytest.mark.parametrize(
    ("files", "names", "expected"),
    [
        ({"zen/__init__.py": '"""Coming soon."""\n__version__ = "0.0.0"\n'}, ("zen",), True),
        ({"zen/__init__.py": "__all__: list[str] = []\npass\n"}, ("zen",), True),
        ({"zen.py": '"""Reserved."""\n'}, ("zen",), True),
        ({"zen.pyi": "__version__: str\n"}, ("zen",), True),
        ({"zen/__init__.py": "", "zen/core.py": "def build(): ...\n"}, ("zen",), False),
        ({"zen/__init__.py": "import os\n"}, ("zen",), False),
        ({"zen/__init__.py": "VERSION = '1'\n"}, ("zen",), False),
        ({"zen/__init__.py": "x, __y__ = 1, 2\n"}, ("zen",), False),
        ({"zen/__init__.py": "def broken(:\n"}, ("zen",), False),
        # Generated code too deep for the parser of Python 3.13 (5000 strings joined with "+").
        ({"zen/__init__.py": "X = " + " + ".join(['"x"'] * 5000) + "\n"}, ("zen",), False),
        # Too deep for the parser of every Python (a MemoryError: "too complex to parse").
        ({"zen/__init__.py": "X = " + "-" * 10_000 + "1\n"}, ("zen",), False),
        ({"zen/__init__.py": ""}, ("elsewhere",), False),  # nothing to look at
    ],
    ids=[
        "docstring-and-version",
        "annotated-dunder",
        "module-docstring",
        "stub",
        "a-function",
        "an-import",
        "a-public-constant",
        "tuple-target",
        "syntax-error",
        "too-deep-for-3.13",
        "too-deep-for-any-python",
        "no-files",
    ],
)
def test_a_placeholder_release_defines_nothing(tmp_path, files, names, expected) -> None:
    tree = SourceTree("zen", "0.0.0", write_tree(tmp_path, files), names)
    assert is_placeholder(tree) is expected


def test_a_release_with_many_files_is_not_a_placeholder(tmp_path) -> None:
    files = {f"zen/m{i}.py": '"""Empty."""\n' for i in range(4)}
    tree = SourceTree("zen", "0.0.0", write_tree(tmp_path, files), ("zen",))
    assert is_placeholder(tree, max_files=4) is True
    assert is_placeholder(tree, max_files=3) is False
