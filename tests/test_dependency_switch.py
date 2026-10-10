"""0.5: a dependency the pinned release switched is a change of its own (DEPENDENCY_SWITCHED).

openai 3, anthropic 1.8, huggingface-hub 2.0 and mcp 2.2 require ``httpx2`` and no longer
``httpx``, and their public signatures take ``httpx2`` objects. A diff of the package's own API
saw none of it: ``httpx`` only showed up inside other changes' signatures. Code written from
memory passes ``httpx.Client`` / ``httpx.Timeout`` objects to these SDKs, or catches
``httpx.HTTPError`` from huggingface-hub.

The rule was measured on public data (the design pass over 283 release pairs that dropped a
base requirement, and the real pairs below): a switch needs a requirement X the pinned release
dropped (a base one, or one of an extra both releases define: fastmcp-slim 4.0), a requirement
Y it has or a copy of X it ships (typer 0.27's ``typer._click``), places of the public API that
named X types and name Y types at the same site, with a type that kept its name at half of them
or more, and no X left anywhere in the pinned release's public API. The toy trees below reduce
the real cases; the real diffs are fixtures (tests/fixtures/diffs), and the pairs read from PyPI
are marked network.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import textwrap
from datetime import date
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from since_cutoff.apidiff import (
    DEPENDENCY_SWITCHED,
    DIFF_SCHEMA,
    PARAM_REMOVED,
    APIChange,
    diff_sources,
    requirements,
)
from since_cutoff.engine import CHANGED, ModelTarget, PackageScan, ScanResult, probeable
from since_cutoff.notes import (
    DEPENDENCY_NOTE_LIMIT,
    EVIDENCE_METADATA,
    SCOPE_IMPORTED,
    SCOPE_USED,
    TAG_DIFF,
    TAG_METADATA,
    dependency_detail,
    diff_note,
    parse_block,
    render_block,
    runtime_text,
    split_tags,
    tag_legend,
    tag_text,
)
from since_cutoff.project import Dependency, Project, load_project, scan_file
from since_cutoff.selection import (
    OLD_FORM,
    TIER_IMPORT_PATH,
    USE_DEPENDENCY,
    USES_API,
    dependency_names,
    form,
    imported_rank,
    tier,
    usage,
    uses,
)
from tests.conftest import write_tree

FIXTURES = Path(__file__).parent / "fixtures" / "diffs"
OLD_REQUIRES = ["oldhttp<1,>=0.23", "anyio>=4", "typing-extensions>=4.7; python_version < '3.11'"]
NEW_REQUIRES = ["newhttp<3,>=2.12", "anyio>=4"]


def _tree(http: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """A toy SDK whose public API names ``http`` types: a client's constructor and method, a
    function's return, an error's base (and a subclass of it), a re-export."""
    files = {
        "pkg/__init__.py": f"""
            from {http} import Timeout

            from pkg._client import Client, get_session
            from pkg.errors import PkgError, SubError

            __all__ = ["Client", "PkgError", "SubError", "Timeout", "get_session"]
        """,
        "pkg/_client.py": f"""
            import {http}

            class Client:
                def __init__(
                    self,
                    http_client: {http}.Client | None = None,
                    timeout: float | {http}.Timeout = 5.0,
                ) -> None:
                    pass

                def send(self, text: str, timeout: float | {http}.Timeout | None = None) -> str:
                    return text

            def get_session() -> {http}.Client:
                return {http}.Client()
        """,
        "pkg/errors.py": f"""
            import {http}

            class PkgError({http}.HTTPError):
                pass

            class SubError(PkgError):
                pass
        """,
    }
    return {**files, **(extra or {})}


def _diff(
    tmp_path: Path,
    old: dict[str, str],
    new: dict[str, str],
    old_requires: list[str] = OLD_REQUIRES,
    new_requires: list[str] = NEW_REQUIRES,
) -> list[APIChange]:
    a = write_tree(tmp_path / "old", old)
    b = write_tree(tmp_path / "new", new)
    found = diff_sources(
        "pkg", "1", a, "2", b, ["pkg"], old_requires=old_requires, new_requires=new_requires
    )
    return [APIChange.from_dict(c) for c in found]


def _switches(changes: list[APIChange]) -> list[APIChange]:
    return [c for c in changes if c.kind == DEPENDENCY_SWITCHED]


def _switch(tmp_path: Path, **extra: str) -> APIChange:
    [change] = _switches(_diff(tmp_path, _tree("oldhttp"), _tree("newhttp", extra)))
    return change


# ------------------------------------------------------------------ the diff
def test_a_switched_http_library_is_one_change_with_its_evidence(tmp_path) -> None:
    change = _switch(tmp_path)
    assert (change.package, change.path, change.name, change.switched_to) == (
        "pkg",
        "pkg",
        "oldhttp",
        "newhttp",
    )
    assert change.describe() == "pkg 2 requires `newhttp` instead of `oldhttp`"
    assert change.display == "oldhttp -> newhttp"
    d = change.dependency or {}
    assert d["old_requirement"] == "oldhttp<1,>=0.23"
    assert d["new_requirement"] == "newhttp<3,>=2.12"
    assert (d["old_modules"], d["new_modules"], d["old_in_extras"]) == (
        ["oldhttp"],
        ["newhttp"],
        [],
    )
    # Both parameters of the constructor, the method's, the function's return, the error's
    # base and the re-export; not SubError, whose base is PkgError (it says nothing more).
    assert (d["sites"], d["sites_same_name"]) == (6, 6)
    assert d["names"] == ["Client", "HTTPError", "Timeout"]
    assert d["parameters"] == {"timeout": 2, "http_client": 1}
    assert d["parameter_types"] == {
        "http_client": [{"new": "newhttp.Client", "direct": True, "count": 1}],
        "timeout": [{"new": "newhttp.Timeout", "direct": True, "count": 2}],
    }
    # A function first, then the constructor; at the constructor, the parameter that takes
    # nothing but the new library's types (``http_client``) before the one that takes a
    # ``float`` too (``timeout``), although that one is in more signatures.
    assert [(e["display"], e["parameter"], e["context"]) for e in d["examples"]] == [
        ("get_session", None, "return"),
        ("Client", "http_client", "param"),
        ("Client", "timeout", "param"),
    ]
    assert [e.get("exclusive") for e in d["examples"]] == [None, True, False]
    assert d["examples"][2]["path"] == "pkg.Client.__init__"
    assert d["examples"][2]["new"] == "newhttp.Timeout"
    assert [(b["display"], b["old"], b["new"]) for b in d["bases"]] == [
        ("PkgError", "oldhttp.HTTPError", "newhttp.HTTPError")
    ]
    assert [(r["path"], r["new"]) for r in d["reexports"]] == [("pkg.Timeout", "newhttp.Timeout")]
    # With where a call passes each one by position, after ``self``.
    assert ["pkg.Client", "http_client", 0] in d["calls"]
    assert ["pkg.Client", "timeout", 1] in d["calls"]
    assert "pkg.Client" in (change.import_paths or []) and "pkg.get_session" in (
        change.import_paths or []
    )
    assert (d["still_named_at"], d["still_named_count"]) == ([], 0)
    # One API of its own, with an id of its own.
    assert change.api_key == "pkg:dependency:oldhttp->newhttp"
    assert change.id != APIChange.from_dict({**change.to_dict(), "dependency": None}).id


def test_the_note_says_what_the_metadata_and_the_api_show(tmp_path) -> None:
    change = _switch(tmp_path)
    note = diff_note([change])
    assert note.tag_list == (TAG_DIFF, TAG_METADATA)
    # One or two sentences: what it requires instead, three places (two examples, a base
    # class) and what to use there.
    assert note.line == (
        "pkg 2 requires `newhttp` instead of `oldhttp`: `get_session()` returns "
        "`newhttp.Client`, `Client(http_client=...)` takes `newhttp.Client` and `PkgError` "
        "derives from `newhttp.HTTPError` instead of `oldhttp.HTTPError`. Use `newhttp` there, "
        "not `oldhttp`. [diff + metadata]"
    )
    assert len(note.line) <= DEPENDENCY_NOTE_LIMIT
    # The requirements, the counts and the other places are in the detail (scan --all's
    # "Places:", report.md, the MCP tools).
    assert dependency_detail(change) == (
        "its Requires-Dist lists `newhttp<3,>=2.12` and no `oldhttp`; 6 places in its public "
        "API that named `oldhttp` types name the `newhttp` types of the same name: "
        "`get_session()` returns `newhttp.Client`, `Client(http_client=...)` takes "
        "`newhttp.Client` and `Client(timeout=...)` (and 1 other signature) takes "
        "`newhttp.Timeout`; `pkg.Timeout` is now `newhttp.Timeout`; `PkgError` derives from "
        "`newhttp.HTTPError` instead of `oldhttp.HTTPError`"
    )
    assert note.api == "oldhttp -> newhttp"
    [replacement] = note.replacements
    assert (replacement.text, replacement.replaces, replacement.evidence) == (
        "newhttp",
        "oldhttp",
        EVIDENCE_METADATA,
    )
    checks = note.checks()
    assert checks["change"].startswith(
        "Requires-Dist of both releases' wheels (METADATA) and a static comparison of their "
        "public APIs (griffe)"
    )
    assert checks["change"].endswith(f"diff schema {DIFF_SCHEMA}")
    assert checks["runtime"] == "not checked"


def test_where_the_pinned_source_still_names_it_is_a_pointer_not_a_claim(tmp_path) -> None:
    """openai 3.19.2's ``sys.modules.get("httpx")``: its source still names ``httpx``. What it
    does with httpx objects differs between libraries, so only the place is said."""
    compat = {"pkg/_compat.py": 'import sys\n\nLEGACY = sys.modules.get("oldhttp")\n'}
    change = _switch(tmp_path, **compat)
    d = change.dependency or {}
    assert (d["still_named_at"], d["still_named_count"]) == (["pkg/_compat.py:3"], 1)
    note = diff_note([change])
    assert "_compat" not in note.line  # the block's text does not change
    assert note.checks()["runtime"] == (
        "not checked (nothing was run); 2's source still names `oldhttp` (pkg/_compat.py:3); "
        "what it does with `oldhttp` objects was not checked"
    )


def test_the_old_library_left_only_for_an_extra_is_said_so(tmp_path) -> None:
    """huggingface-hub 2.0 lists ``requests`` only for its ``gradio`` extra."""
    new_requires = [*NEW_REQUIRES, "oldhttp>=0.23; extra == 'legacy'"]
    changes = _diff(tmp_path, _tree("oldhttp"), _tree("newhttp"), new_requires=new_requires)
    [change] = _switches(changes)
    assert (change.dependency or {})["old_in_extras"] == ["legacy"]
    # The note stays short; the detail says what the extra still lists.
    line = diff_note([change]).line
    assert line.startswith("pkg 2 requires `newhttp` instead of `oldhttp`: ")
    assert "legacy" not in line
    assert dependency_detail(change).startswith(
        "its Requires-Dist lists `newhttp<3,>=2.12`, and `oldhttp` only for its `legacy` "
        "extra; 6 places in its public API"
    )


def test_a_base_class_through_the_package_s_own_class_counts(tmp_path) -> None:
    """mcp 2.2's ``OAuthClientProvider`` derives from its new ``RedirectAwareAuth``, which
    derives from ``httpx2.Auth``; in 1.28 it derived from ``httpx.Auth``."""
    old = {"pkg/__init__.py": "import oldhttp\n\nclass Provider(oldhttp.Auth):\n    pass\n"}
    new = {
        "pkg/__init__.py": (
            "import newhttp\n\nclass RedirectAware(newhttp.Auth):\n    pass\n\n"
            "class Provider(RedirectAware):\n    pass\n"
        )
    }
    [change] = _switches(_diff(tmp_path, old, new))
    d = change.dependency or {}
    assert [(b["display"], b["new"]) for b in d["bases"]] == [("Provider", "newhttp.Auth")]
    assert "`Provider` derives from `newhttp.Auth` instead of `oldhttp.Auth`" in (
        diff_note([change]).line
    )


def test_an_annotation_that_is_not_the_type_itself_is_not_said_to_take_it(tmp_path) -> None:
    """huggingface-hub's ``set_client_factory(client_factory: Callable[[], httpx.Client])``."""
    code = "from typing import Callable\nimport {h}\n\ndef set_factory(factory: Callable[[], {h}.Client]) -> None:\n    pass\n"
    old = {"pkg/__init__.py": code.format(h="oldhttp")}
    new = {"pkg/__init__.py": code.format(h="newhttp")}
    [change] = _switches(_diff(tmp_path, old, new))
    assert "`set_factory(factory=...)` has `newhttp.Client` in its annotation" in (
        diff_note([change]).line
    )


_TWINS = """
    import {h}

    class Client:
        def __init__(self, http_client: {h}.Client | None = None) -> None:
            pass

        def with_options(self, http_client: {h}.Client | None = None) -> "Client":
            return self

    class AsyncClient:
        def __init__(self, http_client: {h}.AsyncClient | None = None) -> None:
            pass

        def with_options(self, http_client: {h}.AsyncClient | None = None) -> "AsyncClient":
            return self
"""


def _twins(http: str) -> dict[str, str]:
    """openai 3's ``OpenAI`` / ``AsyncOpenAI``: ``http_client`` takes a sync client in one and
    an async client in the other."""
    init = "from pkg._client import AsyncClient, Client\n\n__all__ = ['AsyncClient', 'Client']\n"
    return {"pkg/__init__.py": init, "pkg/_client.py": _TWINS.replace("{h}", http)}


def test_a_sync_and_an_async_twin_are_one_place_and_counted_by_type(tmp_path) -> None:
    [change] = _switches(_diff(tmp_path, _twins("oldhttp"), _twins("newhttp")))
    d = change.dependency or {}
    # Each type its own count: 2 signatures take ``newhttp.Client``, 2 ``newhttp.AsyncClient``.
    assert d["parameter_types"] == {
        "http_client": [
            {"new": "newhttp.AsyncClient", "direct": True, "count": 2},
            {"new": "newhttp.Client", "direct": True, "count": 2},
        ]
    }
    assert [(e["display"], e["new"]) for e in d["examples"]] == [
        ("Client", "newhttp.Client"),
        ("AsyncClient", "newhttp.AsyncClient"),
    ]
    # The note gives the sync example, with its twin's type; the detail both, with their counts.
    line = diff_note([change]).line
    assert line == (
        "pkg 2 requires `newhttp` instead of `oldhttp`: `Client(http_client=...)` takes "
        "`newhttp.Client` (`newhttp.AsyncClient` for `AsyncClient`). Use `newhttp` there, not "
        "`oldhttp`. [diff + metadata]"
    )
    assert dependency_detail(change).endswith(
        "4 places in its public API that named `oldhttp` types name the `newhttp` types of the "
        "same name: `Client(http_client=...)` (and 1 other signature) takes `newhttp.Client` and "
        "`AsyncClient(http_client=...)` (and 1 other signature) takes `newhttp.AsyncClient`"
    )


def test_a_switch_for_some_extras_only_says_which(tmp_path) -> None:
    """fastmcp-slim 4.0 lists ``httpx2`` where 3.4 listed ``httpx``, for its ``client``, ``mcp``
    and ``server`` extras only."""
    changes = _diff(
        tmp_path,
        _tree("oldhttp"),
        _tree("newhttp"),
        old_requires=["anyio>=4", "oldhttp>=0.23; extra == 'client'"],
        new_requires=["anyio>=4", "newhttp>=2.12; extra == 'client'"],
    )
    [change] = _switches(changes)
    d = change.dependency or {}
    assert (d["extras"], d["old_requirement"], d["new_requirement"]) == (
        ["client"],
        "oldhttp>=0.23",
        "newhttp>=2.12",
    )
    assert change.describe() == "pkg 2's `client` extra requires `newhttp` instead of `oldhttp`"
    line = diff_note([change]).line
    assert line.startswith("pkg 2's `client` extra requires `newhttp` instead of `oldhttp`: ")
    assert len(line) <= DEPENDENCY_NOTE_LIMIT
    assert dependency_detail(change).startswith(
        "its Requires-Dist lists `newhttp>=2.12` and no `oldhttp` for it; 6 places"
    )


def _copy_tree() -> dict[str, str]:
    """The toy SDK after it stopped requiring ``oldhttp`` and ships a copy of it as
    ``pkg._oldhttp`` (typer 0.27's ``typer._click``)."""
    copy = """
        class Client:
            pass

        class Timeout:
            pass

        class HTTPError(Exception):
            pass
    """
    return _tree("pkg._oldhttp", {"pkg/_oldhttp/__init__.py": copy})


def test_a_copy_the_package_ships_is_a_switch_to_its_own_names(tmp_path) -> None:
    changes = _diff(tmp_path, _tree("oldhttp"), _copy_tree(), new_requires=["anyio>=4"])
    [change] = _switches(changes)
    d = change.dependency or {}
    assert (change.name, change.switched_to, d["vendored"], d["new_requirement"]) == (
        "oldhttp",
        "pkg._oldhttp",
        True,
        None,
    )
    assert (
        change.describe() == "pkg 2 no longer requires `oldhttp` and ships `pkg._oldhttp` instead"
    )
    # What to use: the package's re-export of the copy's class. Its own ``Client`` only shares
    # a name with ``oldhttp.Client``: it is no replacement for it.
    assert d["public_names"] == ["pkg.Timeout"]
    line = diff_note([change]).line
    assert line.endswith(
        "Use pkg's own names there (`pkg.Timeout`), not `oldhttp`'s. [diff + metadata]"
    )
    assert len(line) <= DEPENDENCY_NOTE_LIMIT
    assert dependency_detail(change).startswith(
        "its Requires-Dist lists no `oldhttp`, and it ships `pkg._oldhttp`; 6 places"
    )
    [replacement] = diff_note([change]).replacements
    assert replacement.source.startswith(
        "pkg 2 Requires-Dist no longer lists `oldhttp<1,>=0.23`, and the package ships "
        "`pkg._oldhttp`; the API diff found 6 places"
    )


def test_a_library_the_old_release_already_required_is_said_so(tmp_path) -> None:
    """gradio 3.45.2 -> 4.28.3 dropped ``requests``, and its API names ``httpx`` types, which
    3.45.2 also required."""
    old_requires = [*OLD_REQUIRES, "newhttp>=2"]
    [change] = _switches(_diff(tmp_path, _tree("oldhttp"), _tree("newhttp"), old_requires))
    assert (change.dependency or {})["new_added"] is False
    assert change.describe() == (
        "pkg 2 no longer requires `oldhttp`; its API names `newhttp` types instead"
    )
    line = diff_note([change]).line
    assert line.startswith(
        "pkg 2 no longer requires `oldhttp`; its API names `newhttp` types instead: "
    )
    assert len(line) <= DEPENDENCY_NOTE_LIMIT
    assert dependency_detail(change).startswith(
        "its Requires-Dist lists `newhttp<3,>=2.12`, which 1 also required, and no `oldhttp`; "
    )


def test_a_constructor_reached_through_a_plain_import_is_recorded_by_its_class(
    tmp_path,
) -> None:
    """A class of a private module that a public module only imports (iter_public_objects'
    last resort, which path_index does not list): code calls ``pkg.server.Client(...)``, so
    that is what the change records, not ``pkg.server.Client.__init__``."""
    impl = "import {h}\n\nclass Client:\n    def __init__(self, http_client: {h}.Client) -> None:\n        pass\n"

    def tree(http: str) -> dict[str, str]:
        return {
            "pkg/__init__.py": "",
            "pkg/_impl.py": impl.format(h=http),
            "pkg/server.py": "from pkg._impl import Client\n\ndef serve() -> None:\n    pass\n",
        }

    [change] = _switches(_diff(tmp_path, tree("oldhttp"), tree("newhttp")))
    d = change.dependency or {}
    assert d["calls"] == [["pkg.server.Client", "http_client", 0]]
    assert "pkg.server.Client" in (change.import_paths or [])
    assert not any(p.endswith("__init__") for p in change.import_paths or [])
    code = "import oldhttp\nfrom pkg.server import Client\n\nClient(http_client=oldhttp.Client())\n"
    names = dependency_names(change, scan_file(ast.parse(code)))
    assert names == ("oldhttp", "oldhttp.Client", "Client(http_client)")
    assert form(change, names) == OLD_FORM


def _factory_tree(http: str, importers: list[str]) -> dict[str, str]:
    """mcp's ``McpHttpClientFactory``: a Protocol of a private module that public modules only
    import, the type of a factory code passes in (``sse_client(httpx_client_factory=...)``)."""
    files = {
        "pkg/__init__.py": "",
        "pkg/_utils.py": f"""
            from typing import Protocol

            import {http}

            class Factory(Protocol):
                def __call__(
                    self, timeout: {http}.Timeout | None = None, auth: {http}.Auth | None = None
                ) -> {http}.AsyncClient: ...
        """,
    }
    for name in importers:
        files[f"pkg/{name}.py"] = (
            "from pkg._utils import Factory\n\ndef connect(factory: Factory) -> None:\n    pass\n"
        )
    return files


@pytest.mark.parametrize("descending", [False, True])
def test_a_protocol_two_modules_import_is_one_place_whatever_the_listing_order(
    tmp_path, monkeypatch, descending
) -> None:
    """mcp 1.28.1 imports ``McpHttpClientFactory`` in ``client.sse`` and
    ``client.streamable_http``, 2.2.0 only in ``client.sse``. Its ``__call__`` is part of the
    switch (code implements it, and now returns ``httpx2.AsyncClient``), at the same public path
    in both releases whatever order the file system lists the modules in: griffe took that
    order, and the fixture's 23 places came out as 20 on another machine."""
    walk = os.walk

    def listed(top: Any, *args: Any, **kwargs: Any) -> Any:
        for root, dirs, files in walk(top, *args, **kwargs):
            dirs.sort(reverse=descending)
            files.sort(reverse=descending)
            yield root, dirs, files

    monkeypatch.setattr(os, "walk", listed)
    old = _factory_tree("oldhttp", ["sse", "streamable_http"])
    [change] = _switches(_diff(tmp_path, old, _factory_tree("newhttp", ["sse"])))
    d = change.dependency or {}
    assert (d["sites"], d["parameters"]) == (3, {"auth": 1, "timeout": 1})
    assert {e["path"] for e in d["examples"]} == {"pkg.sse.Factory.__call__"}
    assert d["calls"] == []  # an instance's method: the file may have it from anywhere


def test_names_a_module_only_exports_are_not_names_of_the_old_library(tmp_path) -> None:
    """huggingface-hub 2.0's ``utils`` answers ``utils.httpx`` with ``httpx2``: the strings in
    ``__all__``, compared in a module-level ``__getattr__`` or returned by ``__dir__`` name
    what the module exports. A method's ``__getattr__`` is code like any other."""
    names = """
        __all__ = ["oldhttp"]
        __all__ += ["oldhttp"]
        __all__.append("oldhttp")
        __all__.extend(["oldhttp"])


        def __getattr__(name):
            if name == "oldhttp":
                import newhttp

                return newhttp
            raise AttributeError(name)


        def __dir__():
            return ["oldhttp"]


        class Lazy:
            def __getattr__(self, name):
                if name == "oldhttp":
                    return None
                raise AttributeError(name)
    """
    change = _switch(tmp_path, **{"pkg/_names.py": names})
    lines = textwrap.dedent(names).lstrip().splitlines()
    method = 1 + max(i for i, line in enumerate(lines) if 'name == "oldhttp"' in line)
    d = change.dependency or {}
    assert (d["still_named_at"], d["still_named_count"]) == ([f"pkg/_names.py:{method}"], 1)


def test_package_names_in_requirement_lists_and_examples_are_not_names_of_it(tmp_path) -> None:
    """fastmcp 4's ``Field(examples=[["fastmcp>=2.0,<3", "httpx", "pandas>=2.0"]])`` lists
    packages to install, not code that handles ``httpx`` objects; nor is a list of
    requirements with versions. A plain list of module names may be imported: it counts."""
    config = """
        import sys

        from pydantic import Field

        DEPENDENCIES = Field(
            description="oldhttp",
            examples=[["pkg>=2.0,<3", "oldhttp", "anyio>=4"]],
        )
        INSTALL = ("oldhttp", "anyio[trio]>=4")
        MODULES = ["oldhttp", "anyio"]
        LEGACY = sys.modules.get("oldhttp")
    """
    change = _switch(tmp_path, **{"pkg/_config.py": config})
    lines = textwrap.dedent(config).lstrip().splitlines()
    at = [f"pkg/_config.py:{1 + lines.index(line)}" for line in lines if "MODULES" in line]
    at += [f"pkg/_config.py:{1 + lines.index(line)}" for line in lines if "LEGACY" in line]
    d = change.dependency or {}
    assert (d["still_named_at"], d["still_named_count"]) == (at, 2)


def test_the_runtime_pointer_lists_five_places_and_how_many_more(tmp_path) -> None:
    lookups = "".join(f'M{i} = sys.modules.get("oldhttp")\n' for i in range(7))
    change = _switch(tmp_path, **{"pkg/_compat.py": f"import sys\n\n{lookups}"})
    d = change.dependency or {}
    assert (len(d["still_named_at"]), d["still_named_count"]) == (5, 7)
    assert runtime_text([change]) == (
        "2's source still names `oldhttp` (pkg/_compat.py:3, 4, 5, 6, 7 and 2 more); what it "
        "does with `oldhttp` objects was not checked"
    )


# ---------------------------------------------------------- what is no switch
def _no_switch(tmp_path: Path, old: dict[str, str], new: dict[str, str], **kw: Any) -> None:
    changes = _diff(tmp_path, old, new, **kw)
    assert _switches(changes) == [], [c.describe() for c in changes]


def test_no_switch_when_the_pinned_api_still_names_the_old_library(tmp_path) -> None:
    """langsmith 0.14 dropped ``httpx`` from its requirements and still names it (optional
    use, compatibility code): anywhere, a default or a command-line module included."""
    in_cli = {"pkg/cli.py": "import oldhttp\n\ndef main(t: oldhttp.Timeout) -> None:\n    pass\n"}
    _no_switch(tmp_path / "a", _tree("oldhttp"), _tree("newhttp", in_cli))
    default = {"pkg/defaults.py": "import oldhttp\n\ndef make(t=oldhttp.Timeout(3)):\n    pass\n"}
    _no_switch(tmp_path / "b", _tree("oldhttp"), _tree("newhttp", default))


def test_no_switch_when_the_library_only_moved_to_an_extra_and_is_still_named(tmp_path) -> None:
    """hishel 1 and pydantic-ai-slim 2.51 moved ``httpx`` to an extra and still name it."""
    new_requires = ["anyio>=4", "oldhttp>=0.23; extra == 'http'"]
    _no_switch(tmp_path, _tree("oldhttp"), _tree("oldhttp"), new_requires=new_requires)


def test_no_switch_for_a_new_distribution_with_the_same_import_name(tmp_path) -> None:
    """mkdocstrings-python 2 requires ``griffelib`` instead of ``griffe``: the module is still
    ``griffe``, so the pinned API still names the old distribution's module."""
    _no_switch(
        tmp_path,
        _tree("oldhttp"),
        _tree("oldhttp"),
        new_requires=["oldhttp-lib>=2", "anyio>=4"],
    )


def test_no_switch_when_the_names_differ(tmp_path) -> None:
    """crewai 0.28 -> 1.15: ``TokenCalcHandler`` derived from langchain's
    ``BaseCallbackHandler`` and derives from pydantic's ``BaseModel``: a rewrite."""
    old = {
        "pkg/__init__.py": "import oldhttp\n\nclass Handler(oldhttp.CallbackHandler):\n    pass\n"
    }
    new = {"pkg/__init__.py": "import newhttp\n\nclass Handler(newhttp.BaseModel):\n    pass\n"}
    _no_switch(tmp_path, old, new)


def test_no_switch_when_the_old_library_is_left_for_type_checkers_only(tmp_path) -> None:
    """mistralai 2 still names ``opentelemetry`` under ``if TYPE_CHECKING:``: a reference that
    only type checkers read is still a reference."""
    typing_only = {
        "pkg/hooks.py": (
            "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    import oldhttp\n\n"
            'def on_response(response: "oldhttp.Response") -> None:\n    pass\n'
        )
    }
    _no_switch(tmp_path, _tree("oldhttp"), _tree("newhttp", typing_only))


def test_no_switch_when_every_old_place_is_gone(tmp_path) -> None:
    """openai 0.28 -> 1.0 dropped ``requests`` with the whole API that named it: the removals
    say so, and no place names ``httpx`` where ``requests`` was."""
    old = {"pkg/__init__.py": "import oldhttp\n\ndef get(url) -> oldhttp.Response:\n    pass\n"}
    new = {
        "pkg/__init__.py": "import newhttp\n\nclass Client:\n    def fetch(self) -> newhttp.Response:\n        pass\n"
    }
    changes = _diff(tmp_path, old, new)
    assert _switches(changes) == [] and {c.path for c in changes} == {"pkg.get"}


def test_no_switch_from_a_typing_helper(tmp_path) -> None:
    """flask-limiter and pytest-asyncio dropped ``typing-extensions`` for ``typing``."""
    code = "import {m}\n\ndef f(x: {m}.Protocol) -> None:\n    pass\n"
    _no_switch(
        tmp_path,
        {"pkg/__init__.py": code.format(m="typing_extensions")},
        {"pkg/__init__.py": code.format(m="newhttp")},
        old_requires=["typing-extensions>=4"],
        new_requires=["newhttp>=2"],
    )


def test_no_switch_in_annotated_metadata_or_command_line_modules(tmp_path) -> None:
    """huggingface-hub 2 dropped ``typer``: its 721 references were ``Annotated[...,
    typer.Option()]`` metadata, and the rest in ``huggingface_hub.cli``."""
    code = (
        "from typing import Annotated\nimport {m}\n\n"
        "def run(name: Annotated[str, {m}.Option()]) -> None:\n    pass\n"
    )
    old = {"pkg/__init__.py": "", "pkg/cli.py": code.format(m="oldhttp")}
    new = {"pkg/__init__.py": "", "pkg/cli.py": code.format(m="newhttp")}
    _no_switch(tmp_path / "a", old, new)
    old = {"pkg/__init__.py": code.format(m="oldhttp")}
    new = {"pkg/__init__.py": code.format(m="newhttp")}
    _no_switch(tmp_path / "b", old, new)
    cli = "import {m}\n\ndef main(t: {m}.Timeout) -> None:\n    pass\n"
    old = {"pkg/__init__.py": "", "pkg/cli.py": cli.format(m="oldhttp")}
    new = {"pkg/__init__.py": "", "pkg/cli.py": cli.format(m="newhttp")}
    _no_switch(tmp_path / "c", old, new)


def test_no_switch_when_the_pinned_release_ships_the_module_itself(tmp_path) -> None:
    """pytest 7.2 dropped the ``py`` requirement and ships a ``py.py`` of its own."""
    _no_switch(tmp_path, _tree("oldhttp"), _tree("newhttp", {"oldhttp.py": "Timeout = float\n"}))


def test_no_switch_to_a_shim_that_imports_the_old_library(tmp_path) -> None:
    """langsmith 0.14 dropped ``httpx`` and has ``_openapi_client._httpx``, which imports
    ``httpx2`` or else ``httpx``: its public ``raise_for_status_with_text(response=...)`` still
    takes ``httpx`` objects through it. A module named after the old library that imports it
    is no copy of it."""
    shim = """
        from typing import TYPE_CHECKING

        if TYPE_CHECKING:
            import oldhttp
        else:
            try:
                import newhttp as oldhttp
            except ModuleNotFoundError:
                import oldhttp
    """
    code = "{i}\n\ndef check(response: oldhttp.Response) -> None:\n    pass\n"
    old = {"pkg/__init__.py": code.format(i="import oldhttp")}
    new = {
        "pkg/__init__.py": code.format(i="from pkg._oldhttp import oldhttp"),
        "pkg/_oldhttp.py": shim,
    }
    _no_switch(tmp_path, old, new, new_requires=["anyio>=4"])


def test_no_switch_when_the_old_library_was_never_public(tmp_path) -> None:
    """transformers 5 dropped ``requests``, which no public signature named."""
    code = "def fetch(url):\n    import {m}\n    return {m}.get(url)\n"
    _no_switch(
        tmp_path,
        {"pkg/__init__.py": code.format(m="oldhttp")},
        {"pkg/__init__.py": code.format(m="newhttp")},
    )


def test_no_switch_without_requirements_to_compare(tmp_path) -> None:
    """An sdist without Requires-Dist says nothing about what it dropped; nor does a diff
    made without the requirements (before 0.5)."""
    _no_switch(tmp_path / "a", _tree("oldhttp"), _tree("newhttp"), new_requires=[])
    _no_switch(tmp_path / "b", _tree("oldhttp"), _tree("newhttp"), old_requires=[], new_requires=[])


def test_requirements_are_base_or_for_an_extra() -> None:
    found = requirements(
        [
            "httpx2<3,>=2.12.0",
            "numpy>=1.22; python_version < '3.11'",
            "numpy>=1.26; python_version >= '3.11'",
            "requests; extra == 'gradio'",
            'aiohttp>=3; extra == "aiohttp" and python_version >= "3.9"',
            "Typing_Extensions>=4",
            "not a requirement ((",
        ]
    )
    assert found.base == {
        "httpx2": "httpx2<3,>=2.12.0",
        "numpy": "numpy>=1.22",
        "typing-extensions": "Typing_Extensions>=4",
    }
    assert found.extras == {"requests": ["gradio"], "aiohttp": ["aiohttp"]}


# ------------------------------------------------------------------ the tag
def test_the_metadata_tag_reads_back_and_is_explained_only_when_used(tmp_path) -> None:
    assert tag_text((TAG_DIFF, TAG_METADATA)) == "[diff + metadata]"
    assert split_tags("pkg 2 requires `b`. [diff + metadata]") == (
        "pkg 2 requires `b`.",
        (TAG_DIFF, TAG_METADATA),
    )
    legend = (
        "[metadata] the two releases' declared requirements (Requires-Dist in their wheels' "
        "METADATA)"
    )
    assert tag_legend([TAG_DIFF, TAG_METADATA])[-1] == legend
    assert legend not in tag_legend([TAG_DIFF])
    change = _switch(tmp_path)
    removed = APIChange("pkg", "1", "2", PARAM_REMOVED, "pkg.Client.send", "send", "Client", "x")
    notes = [diff_note([removed]), diff_note([change])]
    block = render_block(notes, model="m", cutoff=date(2025, 7, 31), version_source="uv.lock")
    assert legend in block
    assert legend not in render_block(
        notes[:1], model="m", cutoff=date(2025, 7, 31), version_source="uv.lock"
    )
    # The switch comes first under its package, whatever the order of the notes.
    bullets = [line for line in block.splitlines() if line.startswith("- ")]
    assert bullets[0].startswith("- pkg 2 requires `newhttp`") and len(bullets) == 2
    parsed = parse_block(block)
    assert parsed is not None and parsed.packages["pkg"].bullets[0][1] == (TAG_DIFF, TAG_METADATA)


# ------------------------------------------------------- the project's code
def _scan(
    tmp_path: Path, change: APIChange, code: str, deps: list[Dependency] | None = None
) -> ScanResult:
    package = PackageScan("pkg", "2", "uv.lock", True, True, CHANGED, cutoff_version="1")
    package.import_names = ["pkg"]
    package.changes = [change]
    files = [scan_file(ast.parse(code), "app/llm.py")] if code else []
    project = Project(
        tmp_path, deps or [], "uv.lock", imported_modules={"pkg", "oldhttp"}, files=files
    )
    return ScanResult(project, ModelTarget.cutoff_only(date(2025, 7, 31)), [package])


OLD_FORM_CODE = """
import oldhttp
from pkg import Client

client = Client(http_client=oldhttp.Client(), timeout=oldhttp.Timeout(3))
"""


def test_code_that_hands_old_objects_to_the_package_is_the_old_form(tmp_path) -> None:
    change = _switch(tmp_path)
    f = scan_file(ast.parse(OLD_FORM_CODE), "app/llm.py")
    names = dependency_names(change, f)
    assert names == (
        "oldhttp",
        "oldhttp.Client",
        "oldhttp.Timeout",
        "Client(http_client)",
        "Client(timeout)",
    )
    assert form(change, names) == OLD_FORM
    [use] = uses(change, [f])
    assert (use.file, use.kind, use.form) == ("app/llm.py", USE_DEPENDENCY, OLD_FORM)
    assert usage(change, [f]) == 2
    # A client made elsewhere and passed in: what ``c`` holds is not known from the file, so
    # it hands nothing over, unless the file says it is the old library's (an annotation).
    code = "import oldhttp\nfrom pkg import Client\n\ndef make(c{}):\n    return Client(http_client=c)\n"
    passed = scan_file(ast.parse(code.format("")))
    assert dependency_names(change, passed) == ("oldhttp",)
    assert form(change, dependency_names(change, passed)) == USES_API
    typed = scan_file(ast.parse(code.format(": oldhttp.Client")))
    assert dependency_names(change, typed) == ("oldhttp", "oldhttp.Client", "Client(http_client)")
    assert form(change, dependency_names(change, typed)) == OLD_FORM


def test_importing_both_is_a_use_and_a_switched_keyword_alone_is_none(tmp_path) -> None:
    change = _switch(tmp_path)
    both = scan_file(ast.parse("import oldhttp\nfrom pkg import Client\n\noldhttp.get('x')\n"))
    assert dependency_names(change, both) == ("oldhttp",)
    assert form(change, dependency_names(change, both)) == USES_API
    assert usage(change, [both]) == 1
    alone = scan_file(ast.parse("from pkg import Client\n\nClient(timeout=30)\n"))
    assert dependency_names(change, alone) == ()
    assert usage(change, [alone]) == 0 and uses(change, [alone]) == []


@pytest.mark.parametrize(
    ("code", "names", "expected", "said"),
    [
        # huggingface-hub 2.0's errors derive from httpx2.HTTPError: an except for httpx's
        # around its call no longer catches them.
        (
            "from pkg import get_session\n\ntry:\n    get_session()\nexcept oldhttp.HTTPError:\n"
            "    pass\n",
            ("oldhttp", "except oldhttp.HTTPError"),
            OLD_FORM,
            "catches oldhttp.HTTPError",
        ),
        # Through an instance the file made, in a tuple of exceptions.
        (
            "from pkg import Client\n\nclient = Client()\ntry:\n    client.send('x')\n"
            "except (oldhttp.HTTPError, ValueError):\n    pass\n",
            ("oldhttp", "except oldhttp.HTTPError"),
            OLD_FORM,
            "catches oldhttp.HTTPError",
        ),
        # Around the old library's own call: the file uses it for itself.
        (
            "import pkg\n\ntry:\n    oldhttp.get('x')\nexcept oldhttp.HTTPError:\n    pass\n",
            ("oldhttp", "oldhttp.HTTPError"),
            USES_API,
            "reads oldhttp.HTTPError",
        ),
    ],
)
def test_catching_a_switched_error_around_the_package_is_the_old_form(
    tmp_path, code: str, names: tuple[str, ...], expected: str, said: str
) -> None:
    from since_cutoff.report import use_text

    change = _switch(tmp_path)
    f = scan_file(ast.parse("import oldhttp\n" + code), "app/llm.py")
    assert dependency_names(change, f) == names
    assert form(change, names) == expected
    assert use_text(uses(change, [f])) == said


def test_a_method_keyword_is_not_handed_to_a_switched_constructor(tmp_path) -> None:
    """``calls`` has the switched constructors and functions only: an old object passed to a
    method's keyword of the same name is read, not counted as handed over."""
    change = _switch(tmp_path)
    code = 'import oldhttp\nfrom pkg import Client\n\nClient().send("hi", timeout=oldhttp.Timeout(3))\n'
    f = scan_file(ast.parse(code))
    assert ("pkg.Client.send", "timeout", "oldhttp") in f.keyword_modules
    assert dependency_names(change, f) == ("oldhttp", "oldhttp.Timeout")
    assert form(change, dependency_names(change, f)) == USES_API
    # Nor by position.
    f = scan_file(ast.parse(code.replace("timeout=", "")))
    assert ("pkg.Client.send", 1, "oldhttp") in f.positional_modules
    assert dependency_names(change, f) == ("oldhttp", "oldhttp.Timeout")


@pytest.mark.parametrize(
    ("code", "names"),
    [
        # The constructor's parameters by position (after ``self``), as by keyword.
        ("Client(oldhttp.Client())\n", ("oldhttp", "oldhttp.Client", "Client(http_client)")),
        ("Client(None, oldhttp.Timeout(3))\n", ("oldhttp", "oldhttp.Timeout", "Client(timeout)")),
        (
            "c = oldhttp.Client()\nClient(c, 5.0)\n",
            ("oldhttp", "oldhttp.Client", "Client(http_client)"),
        ),
        # Through ``super().__init__`` in a subclass.
        (
            "class Mine(Client):\n    def __init__(self):\n        super().__init__(oldhttp.Client())\n",
            ("oldhttp", "oldhttp.Client", "Client(http_client)"),
        ),
        # After ``*args`` the positions are not known.
        ("Client(*[oldhttp.Client()])\n", ("oldhttp", "oldhttp.Client")),
    ],
)
def test_a_value_passed_by_position_is_handed_over_too(
    tmp_path, code: str, names: tuple[str, ...]
) -> None:
    change = _switch(tmp_path)
    f = scan_file(ast.parse("import oldhttp\nfrom pkg import Client\n\n" + code))
    assert dependency_names(change, f) == names
    assert form(change, names) == (OLD_FORM if "(" in names[-1] else USES_API)


def _one_place(http: str) -> dict[str, str]:
    """gradio 4's ``encode_to_base64(r: httpx.Response)``: one switched place, a function
    whose parameter code passes by position (huggingface-hub's own ``hf_raise_for_status(r)``)."""
    code = f"import {http}\n\ndef check(response: {http}.Response, name: str = '') -> None:\n    pass\n"
    return {"pkg/__init__.py": code}


def test_one_switched_place_is_said_in_the_singular(tmp_path) -> None:
    [change] = _switches(_diff(tmp_path, _one_place("oldhttp"), _one_place("newhttp")))
    d = change.dependency or {}
    assert (d["sites"], d["calls"]) == (1, [["pkg.check", "response", 0]])
    [replacement] = diff_note([change]).replacements
    assert replacement.source == (
        "pkg 2 Requires-Dist lists `newhttp<3,>=2.12` and 1's listed `oldhttp<1,>=0.23`; the API "
        "diff found 1 place that named `oldhttp` types naming `newhttp` types"
    )
    assert dependency_detail(change).endswith(
        "; 1 place in its public API that named `oldhttp` types names the `newhttp` types of the "
        "same name: `check(response=...)` takes `newhttp.Response`"
    )


@pytest.mark.parametrize(
    ("code", "names"),
    [
        ("check(oldhttp.get('x'))\n", ("oldhttp", "check(response)")),
        ("check(response=oldhttp.get('x'))\n", ("oldhttp", "check(response)")),
        ("r = oldhttp.get('x')\ncheck(r, 'n')\n", ("oldhttp", "check(response)")),
        ("pkg.check(oldhttp.get('x'))\n", ("oldhttp", "check(response)")),
        # The old library's value at a parameter that did not switch.
        ("check(None, oldhttp.__name__)\n", ("oldhttp",)),
    ],
)
def test_a_function_called_positionally_is_the_old_form(
    tmp_path, code: str, names: tuple[str, ...]
) -> None:
    """``hf_raise_for_status(httpx.get(url))`` hands an ``httpx`` response over as surely as
    ``hf_raise_for_status(response=...)``."""
    [change] = _switches(_diff(tmp_path, _one_place("oldhttp"), _one_place("newhttp")))
    f = scan_file(ast.parse("import oldhttp\nimport pkg\nfrom pkg import check\n\n" + code))
    assert dependency_names(change, f) == names
    assert form(change, names) == (OLD_FORM if len(names) > 1 else USES_API)


_ON_CLASS = """
    import {h}

    class Server:
        @classmethod
        def from_spec(cls, spec: dict, client: {h}.AsyncClient | None = None) -> "Server":
            return cls()

        @staticmethod
        def make_client(client: {h}.Client) -> None:
            pass

        def mount(self, client: {h}.Client) -> None:
            pass
"""


def _on_class(http: str) -> dict[str, str]:
    """fastmcp 4's ``FastMCP.from_openapi(openapi_spec, client=...)``, a classmethod."""
    init = "from pkg._server import Server\n\n__all__ = ['Server']\n"
    return {"pkg/__init__.py": init, "pkg/_server.py": _ON_CLASS.replace("{h}", http)}


@pytest.mark.parametrize(
    ("code", "names"),
    [
        (
            "Server.from_spec({}, client=oldhttp.AsyncClient())\n",
            ("oldhttp", "oldhttp.AsyncClient", "Server.from_spec(client)"),
        ),
        (
            "Server.from_spec({}, oldhttp.AsyncClient())\n",
            ("oldhttp", "oldhttp.AsyncClient", "Server.from_spec(client)"),
        ),
        (
            "Server.make_client(oldhttp.Client())\n",
            ("oldhttp", "oldhttp.Client", "Server.make_client(client)"),
        ),
        # An instance method: the file may have its instance from anywhere.
        ("Server().mount(oldhttp.Client())\n", ("oldhttp", "oldhttp.Client")),
        ("Server().mount(client=oldhttp.Client())\n", ("oldhttp", "oldhttp.Client")),
    ],
)
def test_a_classmethod_or_staticmethod_on_its_class_is_a_switched_call(
    tmp_path, code: str, names: tuple[str, ...]
) -> None:
    from since_cutoff.report import use_text

    [change] = _switches(_diff(tmp_path, _on_class("oldhttp"), _on_class("newhttp")))
    calls = (change.dependency or {})["calls"]
    assert ["pkg.Server.from_spec", "client", 1] in calls
    assert ["pkg.Server.make_client", "client", 0] in calls
    assert not any(path.endswith(".mount") for path, *_ in calls)
    f = scan_file(ast.parse("import oldhttp\nfrom pkg import Server\n\n" + code), "app/a.py")
    assert dependency_names(change, f) == names
    handed = "(" in names[-1]
    assert form(change, names) == (OLD_FORM if handed else USES_API)
    if handed:
        callable_name, _, keyword = names[-1].rstrip(")").partition("(")
        said = use_text(uses(change, [f]))
        assert said.endswith(f"; passes {keyword} to {callable_name}"), said


def test_a_keyword_only_parameter_has_no_position(tmp_path) -> None:
    code = "import {h}\n\ndef connect(*, client: {h}.Client) -> None:\n    pass\n"
    old = {"pkg/__init__.py": code.format(h="oldhttp")}
    new = {"pkg/__init__.py": code.format(h="newhttp")}
    [change] = _switches(_diff(tmp_path, old, new))
    assert (change.dependency or {})["calls"] == [["pkg.connect", "client", None]]
    f = scan_file(
        ast.parse("import oldhttp\nfrom pkg import connect\n\nconnect(oldhttp.Client())\n")
    )
    assert dependency_names(change, f) == ("oldhttp", "oldhttp.Client")


def test_a_diff_without_positions_still_matches_keywords(tmp_path) -> None:
    """A diff made earlier in schema 19 has ``[path, parameter]`` in ``calls``."""
    change = _switch(tmp_path)
    d = change.dependency or {}
    d["calls"] = [c[:2] for c in d["calls"]]
    code = "import oldhttp\nfrom pkg import Client\n\n"
    keyword = scan_file(ast.parse(code + "Client(http_client=oldhttp.Client())\n"))
    assert dependency_names(change, keyword)[-1] == "Client(http_client)"
    positional = scan_file(ast.parse(code + "Client(oldhttp.Client())\n"))
    assert dependency_names(change, positional) == ("oldhttp", "oldhttp.Client")


def test_the_legend_says_what_the_pinned_release_did(tmp_path) -> None:
    from since_cutoff.report import _form_legend

    change = _switch(tmp_path / "diff")
    scan = _scan(tmp_path, change, OLD_FORM_CODE)
    assert _form_legend(scan.used_apis()) == [
        "old form: valid for 1; 2 switched to another library for the types it uses (a static "
        "name match; nothing was run)"
    ]
    removed = APIChange("pkg", "1", "2", PARAM_REMOVED, "pkg.Client.send", "send", "Client", "x")
    scan = _scan(tmp_path, change, OLD_FORM_CODE + "client.send('hi', x=1)\n")
    scan.packages[0].changes = [change, removed]
    assert [u.form for u in scan.used_apis()] == [OLD_FORM, OLD_FORM]
    assert _form_legend(scan.used_apis()) == [
        "old form: valid for 1; 2 removed, moved or deprecated what it uses, or switched to "
        "another library for the types it uses (a static name match; nothing was run)"
    ]
    scan = _scan(tmp_path, change, OLD_FORM_CODE + "client.send('hi', x=1)\n")
    scan.packages[0].changes = [removed]
    assert _form_legend(scan.used_apis()) == [
        "old form: valid for 1; 2 removed, moved or deprecated what it uses (a static name "
        "match; nothing was run)"
    ]


def test_the_switch_leads_the_imported_scope_and_is_never_probed(tmp_path) -> None:
    change = _switch(tmp_path)
    assert tier(change) == TIER_IMPORT_PATH
    moved = APIChange("pkg", "1", "2", "moved", "pkg.Session", "Session", moved_to="pkg.s.Session")
    moved.move_evidence = {"compared": "class", "kept": 3, "of": 3}
    assert imported_rank(change, []) < imported_rank(moved, [])
    scan = _scan(tmp_path, change, "import pkg\n")
    scan.packages[0].changes = [moved, change]
    notes = scan.scope_notes(SCOPE_IMPORTED, per_package=1)
    assert [n.change.kind for n in notes] == [DEPENDENCY_SWITCHED]
    # A run's probes need a callable and a parameter for their tasks: not this kind.
    assert not probeable(change) and probeable(moved)


def test_the_scan_says_where_what_is_installed_and_what_the_source_names(tmp_path) -> None:
    from since_cutoff.report import scan_lines

    compat = {"pkg/_compat.py": 'import sys\n\nLEGACY = sys.modules.get("oldhttp")\n'}
    change = _switch(tmp_path / "diff", **compat)
    pinned = Dependency("oldhttp", "0.28.1", False, "uv.lock")
    scan = _scan(tmp_path, change, OLD_FORM_CODE, [pinned])
    [used] = scan.used_apis()
    assert used.form == OLD_FORM
    assert used.installed == {
        "name": "oldhttp",
        "version": "0.28.1",
        "source": "uv.lock",
        "direct": False,
    }
    text = "\n".join(line.plain for line in scan_lines(scan, width=100))
    assert "oldhttp -> newhttp: pkg 2 requires newhttp instead of oldhttp" in text
    assert "old form" in text
    assert (
        "app/llm.py   reads oldhttp.Client, oldhttp.Timeout; passes http_client and timeout" in text
    )
    assert (
        "Installed: uv.lock also pins oldhttp 0.28.1, so import oldhttp still works; oldhttp "
        "objects are not newhttp objects." in " ".join(text.split())
    )
    assert "Runtime: 2's source still names oldhttp (pkg/_compat.py:3)" in " ".join(text.split())
    assert "[metadata] the two releases' declared requirements" in " ".join(text.split())
    # Not installed: said so, and nothing is suggested about removing it when it is.
    scan = _scan(tmp_path, change, OLD_FORM_CODE)
    lines = " ".join(" ".join(line.plain for line in scan_lines(scan, width=100)).split())
    assert "Installed: uv.lock has no oldhttp: code that imports it needs it installed." in lines


def test_installed_reads_the_whole_virtual_environment(tmp_path) -> None:
    """Without a lockfile the dependencies are the declared ones; the old library is often a
    transitive one (another package still requires it), which the virtual environment has."""
    from since_cutoff.report import installed_text

    site = ".venv/Lib/site-packages"
    root = write_tree(
        tmp_path / "project",
        {
            "requirements.txt": "pkg\n",
            ".venv/pyvenv.cfg": "home = /usr/bin\n",
            f"{site}/pkg-2.dist-info/METADATA": "Metadata-Version: 2.1\nName: pkg\nVersion: 2\n",
            f"{site}/oldhttp-0.28.1.dist-info/METADATA": (
                "Metadata-Version: 2.1\nName: oldhttp\nVersion: 0.28.1\n"
            ),
            "app/llm.py": OLD_FORM_CODE,
        },
    )
    project = load_project(root)
    assert [d.key for d in project.dependencies] == ["pkg"]
    assert project.installed["oldhttp"] == "0.28.1"
    change = _switch(tmp_path / "diff")
    package = PackageScan("pkg", "2", "installed", True, True, CHANGED, cutoff_version="1")
    package.import_names = ["pkg"]
    package.changes = [change]
    scan = ScanResult(project, ModelTarget.cutoff_only(date(2025, 7, 31)), [package])
    [used] = scan.used_apis()
    assert used.form == OLD_FORM
    assert used.installed == {
        "name": "oldhttp",
        "version": "0.28.1",
        "source": "installed",
        "direct": False,
    }
    assert installed_text(scan, used) == (
        "your virtual environment also has oldhttp 0.28.1, so `import oldhttp` still works; "
        "oldhttp objects are not newhttp objects."
    )


def test_the_json_and_markdown_reports_carry_the_evidence(tmp_path) -> None:
    from since_cutoff.report import render_markdown, render_scan_markdown, to_json

    change = _switch(tmp_path / "diff")
    pinned = Dependency("oldhttp", "0.28.1", True, "uv.lock")
    scan = _scan(tmp_path, change, OLD_FORM_CODE, [pinned])
    data = json.loads(json.dumps(to_json(scan), default=str))
    [used] = data["used_apis"]
    assert used["installed"] == {
        "name": "oldhttp",
        "version": "0.28.1",
        "source": "uv.lock",
        "direct": True,
    }
    assert used["note"]["tags"] == ["diff", "metadata"]
    assert used["note"]["tag_text"] == "[diff + metadata]"
    assert used["changes"][0]["kind"] == DEPENDENCY_SWITCHED
    assert used["changes"][0]["runtime"] == {
        "checked": False,
        "still_handled_at": None,
        "still_named_at": [],
    }
    assert used["locations"][0]["names"] == [
        "oldhttp",
        "oldhttp.Client",
        "oldhttp.Timeout",
        "Client(http_client)",
        "Client(timeout)",
    ]
    [scanned] = data["scan"][0]["changes"]
    assert scanned["dependency"]["new"] == "newhttp" and scanned["dependency"]["sites"] == 6
    report = render_markdown(scan)
    assert (
        "- **`oldhttp -> newhttp`: pkg 2 requires `newhttp` instead of `oldhttp`** · old form"
        in report
    )
    assert "  - Installed: uv.lock also pins oldhttp 0.28.1 (a direct dependency)" in report
    assert (
        "- pkg requires `newhttp` instead of `oldhttp` (6 places in its public API that named "
        "`oldhttp` types name `newhttp` types)" in report
    )
    summary = render_scan_markdown(scan)
    assert (
        "| `app/llm.py` | `oldhttp` -> `newhttp` (pkg 1 -> 2 dependency) | pkg 2 requires "
        "`newhttp` instead of `oldhttp` | old form | `newhttp` for `oldhttp` (diff + metadata) |"
    ) in summary


def test_the_mcp_tools_list_the_switch_first_and_match_its_names(tmp_path) -> None:
    from since_cutoff.mcp_server import _counts, _matching, _sections, _used_entry

    change = _switch(tmp_path / "diff")
    removed = APIChange("pkg", "1", "2", PARAM_REMOVED, "pkg.Client.send", "send", "Client", "x")
    lines = _sections([removed, change])
    assert lines[:3] == ["", "## Dependencies switched", ""]
    assert lines[3].startswith(
        "- pkg requires `newhttp` instead of `oldhttp`; its Requires-Dist lists "
        "`newhttp<3,>=2.12` and no `oldhttp`; 6 places in its public API that named `oldhttp` "
        "types name the `newhttp` types of the same name: `get_session()` returns"
    )
    assert _counts([removed, change]).startswith("2 breaking changes, 0 new deprecations (")
    assert "dependencies switched 1" in _counts([removed, change])
    for symbol in ("oldhttp", "http_client", "Timeout", "get_session", "newhttp.AsyncClient"):
        assert _matching([removed, change], symbol)[0] == [change], symbol
    # ``Client`` names the class whose ``send`` lost ``x`` too.
    assert _matching([removed, change], "newhttp.Client")[0] == [removed, change]
    assert change not in _matching([removed, change], "client.messages.create")[0]
    scan = _scan(
        tmp_path, change, OLD_FORM_CODE, [Dependency("oldhttp", "0.28.1", False, "uv.lock")]
    )
    [used] = scan.used_apis()
    entry = _used_entry(scan, used)
    assert entry[1].startswith("- `oldhttp -> newhttp` (pkg 1 -> 2): pkg 2 requires `newhttp`")
    assert entry[1].endswith("[old form]")
    assert "  - installed: uv.lock also pins oldhttp 0.28.1, so `import oldhttp` still works" in (
        "\n".join(entry)
    )
    marks = "\n".join(_sections([change], scan.uses(scan.packages[0])))
    assert "## Touching names your code uses" in marks
    assert "[your code uses `oldhttp.Client`, `oldhttp.Timeout`, `Client(http_client)` and" in marks


def test_the_sync_block_puts_the_switch_under_its_package(tmp_path) -> None:
    change = _switch(tmp_path / "diff")
    scan = _scan(tmp_path, change, OLD_FORM_CODE)
    block = scan.notes_block(scan.scope_notes(SCOPE_USED)) or ""
    assert "\n**pkg 2** (1 at the cutoff)\n- pkg 2 requires `newhttp` instead of `oldhttp`" in block
    assert block.count("[diff + metadata]") == 1


# ------------------------------------------------------------ the real diffs
def _fixture(name: str) -> tuple[PackageScan, dict[str, Any]]:
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    package = PackageScan(
        data["package"],
        data["to_version"],
        "uv.lock",
        True,
        True,
        CHANGED,
        cutoff_version=data["from_version"],
    )
    package.import_names = list(data["import_names"])
    package.changes = [APIChange.from_dict(c) for c in data["changes"]]
    return package, data


def test_the_openai_fixture_is_current() -> None:
    """(test_ranking.py checks the mcp one.)"""
    _, data = _fixture("openai-2.44.0-3.19.2-switch")
    assert data["schema"] == DIFF_SCHEMA, "re-record it: python scripts/record_diff_fixtures.py"


def _recorder() -> Any:
    path = Path(__file__).parents[1] / "scripts" / "record_diff_fixtures.py"
    if not path.exists():
        pytest.skip("scripts/record_diff_fixtures.py is not part of this checkout")
    spec = importlib.util.spec_from_file_location("record_diff_fixtures", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_re_recorded_fixture_keeps_its_shape_and_its_kinds(toylib) -> None:
    trees = {tree.version: tree for tree in toylib}
    pypi = SimpleNamespace(source=lambda name, version: trees[version])
    fixture = {"package": "toylib", "from_version": "1.0", "to_version": "2.0"}
    recorder = _recorder()
    data = recorder.record(pypi, {**fixture, "kinds": [PARAM_REMOVED]})
    assert list(data) == [
        "schema",
        "package",
        "from_version",
        "to_version",
        "import_names",
        "kinds",
        "changes",
    ]
    assert (data["schema"], data["import_names"]) == (DIFF_SCHEMA, ["toylib"])
    assert [c["kind"] for c in data["changes"]] == [PARAM_REMOVED]
    everything = recorder.record(pypi, fixture)
    assert "kinds" not in everything and len(everything["changes"]) > 1
    assert recorder.text(data).endswith("}\n")


@pytest.mark.network
def test_every_diff_fixture_is_what_the_releases_on_pypi_give() -> None:
    """``python scripts/record_diff_fixtures.py --check``, on the machine that runs it: the
    mcp fixture's 23 places came out as 20 where griffe listed mcp's modules in another order."""
    from since_cutoff.cache import DiskCache
    from since_cutoff.pypi import PyPI

    recorder = _recorder()
    pypi = PyPI(DiskCache())
    for path in sorted(FIXTURES.glob("*.json")):
        recorded = path.read_text(encoding="utf-8")
        assert recorder.text(recorder.record(pypi, json.loads(recorded))) == recorded, path.name


MCP_NOTE = (
    "mcp 2.2.0 requires `httpx2` instead of `httpx`: `sse_client(auth=...)` takes "
    "`httpx2.Auth`, `create_mcp_http_client(timeout=...)` takes `httpx2.Timeout` and "
    "`OAuthClientProvider` derives from `httpx2.Auth` instead of `httpx.Auth`. Use `httpx2` "
    "there, not `httpx`. [diff + metadata]"
)
OPENAI_NOTE = (
    "openai 3.19.2 requires `httpx2` instead of `httpx`: `OpenAI(http_client=...)` takes "
    "`httpx2.Client` (`httpx2.AsyncClient` for `AsyncOpenAI`), `OpenAI(timeout=...)` takes "
    "`httpx2.Timeout` and `OpenAI(base_url=...)` takes `httpx2.URL`. Use `httpx2` there, not "
    "`httpx`. [diff + metadata]"
)


def test_mcp_2_2_leads_with_the_httpx2_switch(tmp_path) -> None:
    package, _ = _fixture("mcp-1.28.1-2.2.0")
    [change] = [c for c in package.changes if c.kind == DEPENDENCY_SWITCHED]
    assert diff_note([change]).line == MCP_NOTE
    assert len(MCP_NOTE) <= DEPENDENCY_NOTE_LIMIT
    assert dependency_detail(change).startswith(
        "its Requires-Dist lists `httpx2>=2.5.0` and no `httpx`; 23 places in its public API "
        "that named `httpx` types name the `httpx2` types of the same name: "
        "`sse_client(auth=...)` (and 2 other signatures) takes `httpx2.Auth`, "
        "`create_mcp_http_client(timeout=...)` (and 1 other signature) takes `httpx2.Timeout`, "
        "`streamable_http_client(http_client=...)` takes `httpx2.AsyncClient`, "
    )
    scan = _scan(tmp_path, change, "")
    scan.packages[0] = package
    scan = ScanResult(scan.project, scan.target, [package])
    assert scan.ranked(package, scope=SCOPE_IMPORTED)[0] is not None
    assert scan.ranked(package, scope=SCOPE_IMPORTED)[0].kind == DEPENDENCY_SWITCHED
    assert (change.dependency or {})["still_named_at"] == []


def test_openai_3_takes_httpx2_objects(tmp_path) -> None:
    package, data = _fixture("openai-2.44.0-3.19.2-switch")
    assert data["kinds"] == [DEPENDENCY_SWITCHED]  # the rest of this diff is not recorded
    [change] = package.changes
    assert diff_note([change]).line == OPENAI_NOTE
    assert len(OPENAI_NOTE) <= DEPENDENCY_NOTE_LIMIT
    assert (change.dependency or {})["still_named_at"] == ["openai/_httpx2.py:27"]
    # Counted by the type each signature names: ``http_client`` is an ``httpx2.Client`` in 8
    # signatures and an ``httpx2.AsyncClient`` in 6, not 14 of either.
    assert (change.dependency or {})["parameter_types"]["http_client"] == [
        {"new": "httpx2.Client", "direct": True, "count": 8},
        {"new": "httpx2.AsyncClient", "direct": True, "count": 6},
    ]
    detail = dependency_detail(change)
    assert detail.startswith(
        "its Requires-Dist lists `httpx2<3,>=2.12.0` and no `httpx`; 655 places in its public "
        "API that named `httpx` types name the `httpx2` types of the same name: "
        "`OpenAI(http_client=...)` (and 7 other signatures) takes `httpx2.Client`, "
        "`OpenAI(timeout=...)` (and 579 other signatures) takes `httpx2.Timeout`, "
    )
    assert "`AsyncOpenAI(http_client=...)` (and 5 other signatures) takes `httpx2.AsyncClient`" in (
        detail
    )
    code = (
        "import httpx\nfrom openai import OpenAI\n\n"
        "client = OpenAI(http_client=httpx.Client(timeout=httpx.Timeout(30.0)))\n"
    )
    f = scan_file(ast.parse(code), "app/llm.py")
    assert dependency_names(change, f) == (
        "httpx",
        "httpx.Client",
        "httpx.Timeout",
        "OpenAI(http_client)",
    )
    plain = scan_file(ast.parse("from openai import OpenAI\n\nclient = OpenAI(timeout=30)\n"))
    assert dependency_names(change, plain) == ()


_HEAD = "import httpx\nfrom openai import OpenAI\n\n"


@pytest.mark.parametrize(
    ("code", "names", "expected"),
    [
        # httpx used for something else in the same file: nothing is handed to OpenAI.
        (
            "client = OpenAI(timeout=30.0)\nhttpx.get('https://example.com')\n",
            ("httpx",),
            USES_API,
        ),
        (
            "session = httpx.Client()\nclient = OpenAI(timeout=30.0)\n",
            ("httpx", "httpx.Client"),
            USES_API,
        ),
        # An httpx client handed to it, made in the call or before it.
        (
            "client = OpenAI(http_client=httpx.Client())\n",
            ("httpx", "httpx.Client", "OpenAI(http_client)"),
            OLD_FORM,
        ),
        (
            "c = httpx.Client()\nclient = OpenAI(http_client=c)\n",
            ("httpx", "httpx.Client", "OpenAI(http_client)"),
            OLD_FORM,
        ),
    ],
)
def test_openai_3_old_form_only_where_httpx_objects_are_handed_over(
    code: str, names: tuple[str, ...], expected: str
) -> None:
    package, _ = _fixture("openai-2.44.0-3.19.2-switch")
    [change] = package.changes
    found = dependency_names(change, scan_file(ast.parse(_HEAD + code), "app/llm.py"))
    assert (found, form(change, found)) == (names, expected)


# --------------------------------------------------- real pairs from PyPI
@cache
def _real(package: str, old: str, new: str) -> tuple[APIChange, ...]:
    from since_cutoff.cache import DiskCache
    from since_cutoff.pypi import PyPI

    pypi = PyPI(DiskCache())
    a, b = pypi.source(package, old), pypi.source(package, new)
    names = sorted(set(a.import_names) | set(b.import_names))
    found = diff_sources(
        package, old, a.root, new, b.root, names, old_requires=a.requires, new_requires=b.requires
    )
    return tuple(c for c in map(APIChange.from_dict, found) if c.kind == DEPENDENCY_SWITCHED)


@pytest.mark.network
@pytest.mark.parametrize(
    ("package", "old", "new", "at_least"),
    [
        ("openai", "2.44.0", "3.19.2", 600),
        ("anthropic", "0.115.0", "1.8.0", 300),
        ("huggingface-hub", "1.21.0", "2.0.0", 10),
        ("mcp", "1.28.1", "2.2.0", 20),
    ],
)
def test_the_sdks_that_moved_to_httpx2_report_it(package, old, new, at_least) -> None:
    [change] = _real(package, old, new)
    assert (change.name, change.switched_to) == ("httpx", "httpx2")
    assert (change.dependency or {})["sites"] >= at_least
    line = diff_note([change]).line
    assert len(line) <= DEPENDENCY_NOTE_LIMIT
    if package == "openai":
        assert line == OPENAI_NOTE
    if package == "mcp":
        assert line == MCP_NOTE


FASTMCP_NOTE = (
    "fastmcp-slim 4.0.10's `client`, `mcp` and `server` extras require `httpx2` instead of "
    "`httpx`: `Client(auth=...)` takes `httpx2.Auth` and `FastMCP.from_openapi(client=...)` "
    "takes `httpx2.AsyncClient`. Use `httpx2` there, not `httpx`. [diff + metadata]"
)
TYPER_NOTE = (
    "typer 0.27.2 no longer requires `click` and ships `typer._click` instead: "
    "`Option(click_type=...)` takes `typer._click.types.ParamType` and "
    "`show_callback(ctx=...)` takes `typer._click.Context`. Use typer's own names there "
    "(`typer.BadParameter`, `typer.Context`), not `click`'s. [diff + metadata]"
)


@pytest.mark.network
def test_fastmcp_slim_4_switched_httpx_for_three_extras() -> None:
    """fastmcp-slim lists ``httpx`` / ``httpx2`` only for extras: ``client``, ``mcp`` and
    ``server``, of which the ``fastmcp`` distribution installs ``client`` and ``server``."""
    [change] = _real("fastmcp-slim", "3.4.2", "4.0.10")
    d = change.dependency or {}
    assert (change.name, change.switched_to, d["extras"]) == (
        "httpx",
        "httpx2",
        ["client", "mcp", "server"],
    )
    assert diff_note([change]).line == FASTMCP_NOTE
    assert len(FASTMCP_NOTE) <= DEPENDENCY_NOTE_LIMIT
    # Where it still accepts legacy httpx objects; not the package list of a ``Field``'s
    # ``examples`` (fastmcp/utilities/mcp_server_config/v1/environments/uv.py).
    assert d["still_named_at"] == [
        "fastmcp/server/providers/openapi/provider.py:56",
        "fastmcp/utilities/exceptions.py:14",
    ]
    # The note's second place is a classmethod, called on its class: the old form there.
    assert ["fastmcp.FastMCP.from_openapi", "client", 1] in d["calls"]
    head = "import httpx\nfrom fastmcp import FastMCP\n\n"
    for call in ("client=httpx.AsyncClient()", "httpx.AsyncClient()"):
        f = scan_file(ast.parse(f"{head}FastMCP.from_openapi(spec, {call})\n"))
        names = dependency_names(change, f)
        assert names == ("httpx", "httpx.AsyncClient", "FastMCP.from_openapi(client)"), call
        assert form(change, names) == OLD_FORM


@pytest.mark.network
def test_gradio_4_no_longer_requires_requests_and_names_httpx() -> None:
    [change] = _real("gradio", "3.45.2", "4.28.3")
    assert (change.name, change.switched_to) == ("requests", "httpx")
    assert (change.dependency or {})["new_added"] is False
    note = diff_note([change])
    assert note.line == (
        "gradio 4.28.3 no longer requires `requests`; its API names `httpx` types instead: "
        "`encode_to_base64(r=...)` takes `httpx.Response`. Use `httpx` there, not `requests`. "
        "[diff + metadata]"
    )
    assert len(note.line) <= DEPENDENCY_NOTE_LIMIT
    assert note.replacements[0].source.endswith(
        "the API diff found 1 place that named `requests` types naming `httpx` types"
    )
    # Its only place, by keyword or by position.
    head = "import requests\nfrom gradio.external_utils import encode_to_base64\n\n"
    for call in ("r=requests.get('x')", "requests.get('x')"):
        names = dependency_names(change, scan_file(ast.parse(f"{head}encode_to_base64({call})\n")))
        assert names == ("requests", "encode_to_base64(r)"), call
        assert form(change, names) == OLD_FORM


@pytest.mark.network
def test_huggingface_hub_2_old_form_by_keyword_or_by_position() -> None:
    """huggingface-hub's own code calls ``hf_raise_for_status(r)``, positionally."""
    [change] = _real("huggingface-hub", "1.21.0", "2.0.0")
    assert ["huggingface_hub.hf_raise_for_status", "response", 0] in (change.dependency or {})[
        "calls"
    ]
    head = "import httpx\nfrom huggingface_hub import hf_raise_for_status\n\n"
    for call in ("response=httpx.get(url)", "httpx.get(url)"):
        f = scan_file(ast.parse(f"{head}hf_raise_for_status({call})\n"))
        names = dependency_names(change, f)
        assert names == ("httpx", "hf_raise_for_status(response)"), call
        assert form(change, names) == OLD_FORM


@pytest.mark.network
def test_typer_0_27_ships_its_own_copy_of_click() -> None:
    [change] = _real("typer", "0.15.1", "0.27.2")
    d = change.dependency or {}
    assert (change.name, change.switched_to, d["vendored"]) == ("click", "typer._click", True)
    assert d["public_names"] == ["typer.BadParameter", "typer.Context"]
    assert d["still_named_at"] == []
    assert diff_note([change]).line == TYPER_NOTE
    assert len(TYPER_NOTE) <= DEPENDENCY_NOTE_LIMIT


@pytest.mark.network
@pytest.mark.parametrize(
    ("package", "old", "new"),
    [
        ("pandas", "2.3.2", "3.0.6"),  # pytz went to an extra; never public
        # httpx dropped and still named, through a shim (_openapi_client._httpx) that is no copy
        ("langsmith", "0.7.1", "0.14.1"),
        ("mkdocstrings-python", "1.16.12", "2.0.3"),  # griffe -> griffelib, same module
        ("crewai", "0.28.8", "1.15.22"),  # langchain -> pydantic: other names
        ("hishel", "0.1.1", "1.1.8"),  # httpx moved to an extra, still named
    ],
)
def test_other_real_pairs_report_no_switch(package, old, new) -> None:
    assert _real(package, old, new) == ()
