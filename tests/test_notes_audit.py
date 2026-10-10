"""What the 0.6 audit of 36 real libraries found wrong with the notes from the diff, and what
they say now.

- a replacement the library states in a sentence that leads up to it ("replaced by newer
  agents based on X", "in favor of the http-based alternatives implemented in [`X`]") is
  named, markup and all; "renamed to X" and a sentence-initial "Use X." count too, and advice
  ("If you want ..., use `x=True`", "see `X` for the options") still does not;
- where no name can be found, the note points at the package's changelog (PyPI's project URLs,
  else the releases page of the GitHub repository they name) instead of saying it found no
  replacement;
- a function deprecated in one call form only (an ``@overload``) is named by the parameters
  that form alone takes, never by a 500-character signature, and a call site is in the old
  form only when it passes one of them; a form that no keyword names falls back to its
  signature;
- a removed parameter of an SDK method points at ``extra_body`` alone, with no "found no
  replacement" after it; bullets that say the same of several APIs merge into one, and sync
  keeps its books per API;
- a package first released after the cutoff gets a bullet from PyPI's metadata, tagged
  ``[metadata]``, which sync writes.
"""

from __future__ import annotations

import ast
import json
import textwrap
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from since_cutoff import net
from since_cutoff.apidiff import DEPRECATED, PARAM_REMOVED, REMOVED, APIChange
from since_cutoff.cache import DiskCache
from since_cutoff.engine import NEW, Engine, ModelTarget, ScanResult, Settings
from since_cutoff.notes import (
    EVIDENCE_LIBRARY,
    NEW_PACKAGE,
    NOTE_DIFF,
    NOTE_MODEL,
    TAG_DIFF,
    TAG_LIBRARY,
    TAG_METADATA,
    TAG_TYPE_CHECKED,
    Note,
    diff_note,
    diff_notes,
    new_package_note,
    parse_block,
    render_block,
    render_legacy_block,
    tag_legend,
)
from since_cutoff.project import load_project, scan_file
from since_cutoff.pypi import PyPI, SourceTree, changelog_url
from since_cutoff.report import (
    changes_text,
    render_markdown,
    render_scan_markdown,
    scan_lines,
    to_json,
)
from since_cutoff.selection import OLD_FORM, USE_CALL, USE_KEYWORD, USES_API, form, uses
from since_cutoff.stats import estimate_tokens
from since_cutoff.sync import propose, read_target
from tests.conftest import FakePyPI, write_tree
from tests.test_notes_provenance import toy_diff

CUTOFF = date(2025, 7, 31)


def change(
    kind: str,
    path: str,
    *,
    owner: str | None = None,
    parameter: str | None = None,
    pkg: str = "pkg",
    old: str = "1.0",
    new: str = "2.0",
    **kw: Any,
) -> APIChange:
    kw.setdefault("library_names", [])
    name = path.rsplit(".", 1)[-1]
    return APIChange(pkg, old, new, kind, path, name, owner=owner, parameter=parameter, **kw)


# ------------------------------------------------- G: the replacement the text names
def test_replaced_by_in_a_sentence_names_the_replacement() -> None:
    """llama-index-core 0.12.49: "FunctionCallingAgent has been rewritten and replaced by
    newer agents based on llama_index.core.agent.workflow.FunctionAgent." The name is not
    right after "replaced by", so the note quoted the sentence and then said it found no
    replacement. It names FunctionAgent: the library's own sentence, tagged [library]."""
    c = change(
        REMOVED,
        "llama_index.core.agent.FunctionCallingAgent",
        pkg="llama-index-core",
        old="0.12.49",
        new="0.14.0",
        hint=(
            "FunctionCallingAgent has been rewritten and replaced by newer agents based on "
            "llama_index.core.agent.workflow.FunctionAgent."
        ),
        hint_file="llama_index/core/agent/__init__.py",
        library_names=["llama_index.core.agent.workflow.FunctionAgent"],
    )
    note = diff_note([c])
    assert note.line == (
        "`llama_index.core.agent.FunctionCallingAgent` was removed; do not use it. "
        'llama-index-core 0.12.49 said: "FunctionCallingAgent has been rewritten and replaced '
        'by newer agents based on llama_index.core.agent.workflow.FunctionAgent." '
        "[diff + library]"
    )
    [r] = note.replacements
    assert (r.text, r.evidence, r.replaces) == (
        "llama_index.core.agent.workflow.FunctionAgent",
        EVIDENCE_LIBRARY,
        "llama_index.core.agent.FunctionCallingAgent",
    )
    assert r.source == "llama-index-core 0.12.49 llama_index/core/agent/__init__.py"
    assert "found no replacement" not in note.bullet


def test_in_favor_of_with_doc_markup_names_hfapi() -> None:
    """huggingface-hub 0.34.3: "['Repository'] is deprecated in favor of the http-based
    alternatives implemented in [`HfApi`]." The brackets are MkDocs cross-references: the
    quote drops them, and HfApi is the replacement."""
    c = change(
        REMOVED,
        "huggingface_hub.Repository",
        pkg="huggingface-hub",
        old="0.34.3",
        new="2.0.0",
        hint=(
            "['Repository'] is deprecated in favor of the http-based alternatives implemented "
            "in [`HfApi`]."
        ),
        library_names=["huggingface_hub.HfApi"],
    )
    note = diff_note([c])
    assert note.line == (
        "`huggingface_hub.Repository` was removed; do not use it. huggingface-hub 0.34.3 said: "
        "\"'Repository' is deprecated in favor of the http-based alternatives implemented in "
        '`HfApi`." [diff + library]'
    )
    assert note.replacements[0].text == "huggingface_hub.HfApi"


def test_renamed_to_is_stated_directly() -> None:
    """A "renamed to X" in the shape of huggingface-hub's login(): a synthetic text, since
    0.34.3's own text for `new_session` names nothing (its real note points at the changelog)."""
    c = change(
        PARAM_REMOVED,
        "huggingface_hub.login",
        parameter="new_session",
        pkg="huggingface-hub",
        old="0.34.3",
        new="2.0.0",
        hint="`new_session` has been renamed to skip_if_logged_in.",
        library_names=["skip_if_logged_in"],
    )
    assert diff_note([c]).line == (
        "`huggingface_hub.login()` no longer accepts `new_session`; do not pass it. Use "
        "`skip_if_logged_in` instead of `new_session`. [diff + library]"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Right after the cue: "Use X instead", as before.
        ("Deprecated. Use `fetch` instead.", "Use `pkg.fetch` instead. [diff + library]"),
        # A sentence-initial "Use X." with nothing but the name states it; "use X" after a
        # condition, or "Use X to ...", is advice (below).
        ("Please use fetch.", "Use `pkg.fetch` instead. [diff + library]"),
        (
            "Superseded by the simpler `fetch` helper.",
            'pkg 1.0 said: "Superseded by the simpler `fetch` helper." [diff + library]',
        ),
        # Sphinx roles are markup, not part of the name: stated directly.
        (
            "Use :func:`~pkg.fetch` instead of this.",
            "Use `pkg.fetch` instead. [diff + library]",
        ),
    ],
)
def test_each_way_of_stating_a_replacement(text: str, expected: str) -> None:
    c = change(REMOVED, "pkg.legacy_fetch", hint=text, library_names=["pkg.fetch"])
    note = diff_note([c])
    assert note.line == f"`pkg.legacy_fetch` was removed; do not use it. {expected}"
    assert note.replacements[0].text == "pkg.fetch" and note.tag_list == (TAG_DIFF, TAG_LIBRARY)


def test_advice_is_still_not_a_replacement() -> None:
    """huggingface-hub's resume_download: "If you want to force a new download, use
    `force_download=True`" mentions a name; nothing states it as the replacement."""
    c = change(
        PARAM_REMOVED,
        "huggingface_hub.hf_hub_download",
        parameter="resume_download",
        pkg="huggingface-hub",
        old="0.34.3",
        new="2.0.0",
        hint=(
            "Downloads always resume when possible. If you want to force a new download, use "
            "`force_download=True`."
        ),
        library_names=["force_download"],
    )
    note = diff_note([c])
    assert note.tag_list == (TAG_DIFF,) and note.replacements == []
    assert note.bullet.endswith(
        "since-cutoff found no replacement in huggingface-hub's deprecation text."
    )


def test_the_strongest_cue_anywhere_in_the_text_wins() -> None:
    """The text says "see `Helper`" first and "Use `new_func` instead" after it: the second
    states the replacement; the note named Helper and quoted the "see" sentence."""
    c = change(
        REMOVED,
        "pkg.old",
        hint="This function is deprecated; see `Helper` for the background. Use `new_func` instead.",
        library_names=["pkg.Helper", "pkg.new_func"],
    )
    note = diff_note([c])
    assert note.line == (
        "`pkg.old` was removed; do not use it. Use `pkg.new_func` instead. [diff + library]"
    )
    assert [r.text for r in note.replacements] == ["pkg.new_func"]


def test_a_pointer_at_a_name_is_quoted_and_is_not_a_replacement() -> None:
    """A "see `X`" and a sentence-initial "Use `X` to ..." mention a name without saying that
    it replaces anything. They were recorded as stated replacements, tagged [library]; they are
    quoted under [diff], as advice is."""
    see = change(
        REMOVED,
        "pkg.old",
        hint="`old()` is deprecated; see `Config` for the available options.",
        library_names=["pkg.Config"],
    )
    note = diff_note([see])
    assert note.line == (
        "`pkg.old` was removed; do not use it. pkg 1.0 said: \"'old()' is deprecated; see "
        "`Config` for the available options.\" since-cutoff found no replacement in pkg's "
        "deprecation text. [diff]"
    )
    assert note.replacements == [] and note.tag_list == (TAG_DIFF,)
    advice = change(
        PARAM_REMOVED,
        "pkg.parse",
        parameter="lenient",
        hint="Use `strict=False` to keep the previous lenient parsing.",
        library_names=["strict"],
    )
    note = diff_note([advice])
    assert note.replacements == [] and note.tag_list == (TAG_DIFF,)
    assert (
        'On `lenient`, pkg 1.0 said: "Use `strict=False` to keep the previous lenient parsing."'
        in note.bullet
    )


def test_a_long_sentence_that_leads_up_to_the_name_falls_back_to_use_instead() -> None:
    """The quote keeps whole sentences within its limit; when the stating sentence is longer,
    the note still names the replacement, in the short form."""
    long = "a word " * 40
    c = change(
        REMOVED,
        "pkg.Legacy",
        hint=f"Legacy is deprecated in favor of {long}the new `Modern` class.",
        library_names=["pkg.Modern"],
    )
    assert diff_note([c]).line == (
        "`pkg.Legacy` was removed; do not use it. Use `pkg.Modern` instead. [diff + library]"
    )


# ------------------------------------------------- G: no name: point at the changelog
def test_no_name_points_at_the_changelog_and_the_pinned_version() -> None:
    gone = change(
        PARAM_REMOVED,
        "huggingface_hub.hf_hub_download",
        parameter="proxies",
        pkg="huggingface-hub",
        old="0.34.3",
        new="2.0.0",
    )
    url = "https://github.com/huggingface/huggingface_hub/releases"
    assert diff_note([gone], changelog=url).line == (
        "`huggingface_hub.hf_hub_download()` no longer accepts `proxies`; do not pass it. See "
        "https://github.com/huggingface/huggingface_hub/releases (2.0.0). [diff]"
    )
    # Without a URL to point at, the note says what was read, as before.
    assert diff_note([gone]).line.endswith(
        "do not pass it. since-cutoff found no replacement in huggingface-hub's deprecation "
        "text. [diff]"
    )
    # Named when the pointer is not about everything before it.
    said = change(
        PARAM_REMOVED,
        "huggingface_hub.hf_hub_download",
        parameter="resume_download",
        pkg="huggingface-hub",
        old="0.34.3",
        new="2.0.0",
        still_handled_text="deprecated without replacement",
    )
    assert diff_note([gone, said], changelog=url).bullet.endswith(
        "huggingface-hub's deprecation text says there is no replacement for "
        "`resume_download`. For `proxies`, see "
        "https://github.com/huggingface/huggingface_hub/releases (2.0.0)."
    )
    # A removed name, with a tag the deprecation text gives no replacement for.
    removed = change(REMOVED, "anthropic.AI_PROMPT", pkg="anthropic", old="0.60.0", new="1.8.0")
    assert diff_note([removed], changelog="https://x.example/CHANGELOG.md").line == (
        "`anthropic.AI_PROMPT` was removed; do not use it. See https://x.example/CHANGELOG.md "
        "(1.8.0). [diff]"
    )


def test_the_url_cannot_end_the_block_or_open_a_code_span() -> None:
    c = change(REMOVED, "pkg.gone")
    line = diff_note([c], changelog="https://x.example/a -->\n<!-- `b").line
    assert "-->" not in line and "<!--" not in line and line.count("`") == 2
    assert "See https://x.example/a (2.0)." in line.replace("b", "")


@pytest.mark.parametrize(
    ("urls", "expected"),
    [
        # What PyPI lists for the audit's packages.
        (
            {"Homepage": "https://github.com/huggingface/huggingface_hub"},
            "https://github.com/huggingface/huggingface_hub/releases",
        ),
        (
            {
                "Homepage": "https://github.com/anthropics/anthropic-sdk-python",
                "Repository": "https://github.com/anthropics/anthropic-sdk-python",
            },
            "https://github.com/anthropics/anthropic-sdk-python/releases",
        ),
        (
            {
                "Changelog": "https://github.com/encode/httpx/blob/master/CHANGELOG.md",
                "Documentation": "https://www.python-httpx.org",
                "Homepage": "https://github.com/encode/httpx",
            },
            "https://github.com/encode/httpx/blob/master/CHANGELOG.md",
        ),
        (
            {
                "Changes": "https://click.palletsprojects.com/page/changes/",
                "Source": "https://github.com/pallets/click/",
            },
            "https://click.palletsprojects.com/page/changes/",
        ),
        (
            {
                "Documentation": "https://py.sdk.modelcontextprotocol.io/",
                "Homepage": "https://modelcontextprotocol.io",
                "Issues": "https://github.com/modelcontextprotocol/python-sdk/issues",
                "Repository": "https://github.com/modelcontextprotocol/python-sdk",
            },
            "https://github.com/modelcontextprotocol/python-sdk/releases",
        ),
        (
            {"Release Notes": "https://x.example/notes", "Source": "https://github.com/a/b.git"},
            "https://x.example/notes",
        ),
        ({"Source": "https://github.com/a/b.git"}, "https://github.com/a/b/releases"),
        # pydantic-core: the first URL is a sponsors page, not a repository; the Source URL
        # names one. The releases page was github.com/sponsors/samuelcolvin/releases (a 404).
        (
            {
                "Funding": "https://github.com/sponsors/samuelcolvin",
                "Homepage": "https://github.com/pydantic",
                "Source": "https://github.com/pydantic/pydantic/tree/main/pydantic-core",
            },
            "https://github.com/pydantic/pydantic/releases",
        ),
        ({"Funding": "https://github.com/sponsors/samuelcolvin"}, None),
        ({"Source": "https://github.com/orgs/o/discussions"}, None),
        # A Homepage before a Funding URL that happens to name a repository; any case.
        (
            {"Funding": "https://github.com/a/funding", "Homepage": "https://github.com/a/b"},
            "https://github.com/a/b/releases",
        ),
        ({"Source": "https://GitHub.com/O/R.git"}, "https://github.com/O/R/releases"),
        ({"Documentation": "https://docs.example"}, None),
        ({"Changelog": "ftp://not.http"}, None),
        ({}, None),
    ],
)
def test_changelog_url_from_the_project_urls(urls: dict[str, str], expected: str | None) -> None:
    assert changelog_url(urls) == expected


def test_pypi_keeps_the_project_urls_and_summary_in_its_cache(monkeypatch, cache) -> None:
    """The slim JSON the cache keeps has ``project_urls`` and ``home_page`` now; a copy cached
    by an earlier version, without them, is fetched again once."""
    data = {
        "info": {
            "name": "httpx",
            "version": "0.28.1",
            "summary": "The next generation HTTP client.",
            "project_urls": {
                "Changelog": "https://github.com/encode/httpx/blob/master/CHANGELOG.md"
            },
            "home_page": None,
            "author": "not kept",
        },
        "releases": {"0.28.1": []},
    }
    calls: list[str] = []

    def get_json(url: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(url)
        return data

    monkeypatch.setattr(net, "get_json", get_json)
    pypi = PyPI(cache)
    cache.set("pypi", "httpx", {"info": {"name": "httpx", "version": "0.28.1"}, "releases": {}})
    assert pypi.project_urls("httpx") == {
        "Changelog": "https://github.com/encode/httpx/blob/master/CHANGELOG.md"
    }
    assert pypi.summary("httpx") == "The next generation HTTP client." and len(calls) == 1
    assert "author" not in cache.get("pypi", "httpx")["info"]
    assert pypi.project_urls("httpx") and len(calls) == 1  # from the cache now
    # ``home_page`` stands in for a missing Homepage URL.
    cache.set(
        "pypi",
        "old-style",
        {"info": {"project_urls": None, "home_page": "https://github.com/o/r"}, "releases": {}},
    )
    assert pypi.project_urls("old-style") == {"Homepage": "https://github.com/o/r"}
    assert pypi.summary("old-style") is None
    # Offline, a copy from before stands in as it is (it is within the TTL): no stale warning.
    cache.set("pypi", "offline", {"info": {"name": "offline"}, "releases": {"1.0": []}})

    def unreachable(url: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise net.HTTPError(url, None)

    monkeypatch.setattr(net, "get_json", unreachable)
    assert pypi.project("offline")["releases"] == {"1.0": []}
    assert pypi.project_urls("offline") == {} and pypi.stale == {}


HUB_OLD = {
    "hub/__init__.py": "from hub.file_download import hf_hub_download\n",
    "hub/file_download.py": """
        def hf_hub_download(repo_id: str, filename: str, proxies: dict | None = None) -> str:
            return filename
    """,
}
HUB_NEW = {
    "hub/__init__.py": "from hub.file_download import hf_hub_download\n",
    "hub/file_download.py": """
        def hf_hub_download(repo_id: str, filename: str) -> str:
            return filename
    """,
}


def _app(tmp_path: Path, requirement: str, code: str) -> Path:
    root = tmp_path / "app"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "app"\nversion = "0"\ndependencies = ["{requirement}"]\n'
    )
    (root / "main.py").write_text(code)
    return root


def _pypi(cache: DiskCache, tmp_path: Path, info: dict[str, Any] | None = None) -> FakePyPI:
    trees = {
        ("hub", v): SourceTree("hub", v, write_tree(tmp_path / f"hub-{v}", files), ("hub",))
        for v, files in (("1.0", HUB_OLD), ("2.0", HUB_NEW))
    }
    return FakePyPI(
        cache,
        {"hub": [("1.0", "2025-01-10"), ("2.0", "2025-10-01")]},
        trees,
        info={"hub": info} if info else None,
    )


def test_the_scan_passes_the_changelog_to_the_notes(tmp_path, cache) -> None:
    root = _app(
        tmp_path, "hub==2.0", "from hub import hf_hub_download\nhf_hub_download('r', 'f')\n"
    )
    urls = {"Homepage": "https://github.com/huggingface/huggingface_hub"}
    pypi = _pypi(cache, tmp_path, {"summary": "Hub client", "project_urls": urls})
    engine = Engine(Settings(), store=cache, llm_cache=cache, pypi=pypi)
    scan = engine.scan(load_project(root), ModelTarget.cutoff_only(CUTOFF))
    [hub] = scan.packages
    assert hub.changelog == "https://github.com/huggingface/huggingface_hub/releases"
    [note] = scan.scope_notes()
    assert note.line == (
        "`hub.hf_hub_download()` no longer accepts `proxies`; do not pass it. See "
        "https://github.com/huggingface/huggingface_hub/releases (2.0). [diff]"
    )
    assert [u.note.line for u in scan.used_apis()] == [note.line]
    # Without project URLs on PyPI, the note says what it read, as before.
    scan = Engine(Settings(), store=cache, llm_cache=cache, pypi=_pypi(cache, tmp_path)).scan(
        load_project(root), ModelTarget.cutoff_only(CUTOFF)
    )
    assert scan.packages[0].changelog is None
    assert scan.scope_notes()[0].bullet.endswith(
        "since-cutoff found no replacement in hub's deprecation text."
    )


# ------------------------------------------------- H: a deprecated call form
SERVER_OLD = {
    "mcp/__init__.py": "from mcp.server import Server\n",
    "mcp/server.py": """
        class Server:
            def __init__(self, name: str, version: str | None = None, lifespan=None) -> None:
                self.name = name
    """,
}
SERVER_NEW = {
    "mcp/__init__.py": "from mcp.server import Server\n",
    "mcp/server.py": """
        from typing import overload
        from typing_extensions import deprecated


        class Server:
            @overload
            def __init__(self, name: str, *, version: str = "") -> None: ...

            @overload
            @deprecated("on_progress and on_roots_list_changed are deprecated as of 2026-07-28. Pass neither.")
            def __init__(
                self,
                name: str,
                *,
                version: str = "",
                lifespan=None,
                on_progress=None,
                on_roots_list_changed=None,
            ) -> None: ...

            def __init__(self, name, *, version="", lifespan=None, on_progress=None, on_roots_list_changed=None):
                self.name = name
    """,
}


def _server_change(tmp_path: Path) -> APIChange:
    [c] = [c for c in toy_diff(tmp_path, "mcp", SERVER_OLD, SERVER_NEW) if c.kind == DEPRECATED]
    assert c.path == "mcp.server.Server.__init__" and c.call_form
    return c


def test_a_deprecated_overload_records_the_parameters_only_it_takes(tmp_path) -> None:
    """``lifespan`` is in the deprecated overload too, but the other overload does not take it
    either: only the keywords that make a call the deprecated form count."""
    c = _server_change(tmp_path)
    assert c.call_form_only == ["lifespan", "on_progress", "on_roots_list_changed"]


def _form_change(tmp_path: Path, deprecated: str, other: str) -> APIChange:
    """The DEPRECATED change of ``pkg.f``, whose ``@overload`` with the parameters
    ``deprecated`` is deprecated while the one with ``other`` is not."""
    old = {"pkg/__init__.py": "def f(*args, **kwargs): ...\n"}
    new = {
        "pkg/__init__.py": f"""
            from typing import overload
            from typing_extensions import deprecated


            @overload
            def f({other}): ...
            @overload
            @deprecated("Deprecated.")
            def f({deprecated}): ...
            def f(*args, **kwargs): ...
        """,
    }
    [c] = [c for c in toy_diff(tmp_path, "pkg", old, new) if c.kind == DEPRECATED]
    return c


def test_a_form_that_no_keyword_names_records_no_parameters(tmp_path) -> None:
    """cachetools' ``cached(cache, key, lock, info, /)``: ``info`` is positional-only, so
    ``info=`` is a TypeError, not the deprecated form, yet the note read "with `info=` passed as
    a keyword argument" and only a call passing ``info=`` was the old form. The same for a form
    that differs by ``**kwargs``. No keyword names such a form: the field is [], the note
    quotes the signature when it is short, and every call is "uses this API"."""
    info = _form_change(tmp_path / "a", "cache, key, lock, info, /", "cache, key=None, lock=None")
    assert (info.call_form, info.call_form_only) == ("f(cache, key, lock, info, /)", [])
    assert diff_note([info]).line == (
        "`pkg.f` called as `f(cache, key, lock, info, /)` is deprecated; avoid it in new code. "
        "[diff]"
    )
    [use] = uses(info, _file("from pkg import f\nf(1, 2, 3, info=4)\n"))
    assert use.form == USES_API
    kwargs = _form_change(tmp_path / "b", "name: str, **kwargs", "name: str")
    assert (kwargs.call_form, kwargs.call_form_only) == ("f(name: str, **kwargs)", [])
    assert diff_note([kwargs]).bullet.startswith(
        "`pkg.f` called as `f(name: str, **kwargs)` is deprecated"
    )
    # A keyword-only parameter of its own next to a positional-only one: the keyword alone
    # would not name the form.
    mixed = _form_change(tmp_path / "c", "a, info, /, *, on_progress", "a")
    assert mixed.call_form_only == []
    # A parameter a call may pass by keyword names the form, positional-or-keyword or not.
    plain = _form_change(tmp_path / "d", "a, b", "a")
    assert plain.call_form_only == ["b"]
    assert diff_note([plain]).line == (
        "`pkg.f(...)` with `b=` passed as a keyword argument is deprecated; avoid it in new "
        "code. [diff]"
    )


def _mcp_2_3_server() -> APIChange:
    """mcp 2.3.0's ``Server.__init__`` as the audit saw it: a 500-character deprecated overload
    whose own parameters are the three the 2.2.0 fixture records too (the other overload took
    ``lifespan``, ``cache_hints`` and ``get_tool_input_schema`` by then)."""
    return change(
        DEPRECATED,
        "mcp.server.lowlevel.server.Server.__init__",
        owner="Server",
        pkg="mcp",
        old="1.28.1",
        new="2.3.0",
        deprecation="Passing any of them emits an MCPDeprecationWarning at runtime.",
        call_form="__init__(self, name: str, *, version: str = '', " + "x: int = 0, " * 60,
        call_form_only=["on_set_logging_level", "on_roots_list_changed", "on_progress"],
    )


def test_the_note_names_the_deprecated_call_form_not_its_signature(tmp_path) -> None:
    c = _server_change(tmp_path)
    assert diff_note([c]).line == (
        "`Server(...)` with `lifespan=`, `on_progress=` or `on_roots_list_changed=` passed as "
        'keyword arguments is deprecated; avoid it in new code. mcp 2.0 says: "Pass neither." '
        "[diff]"
    )
    line = diff_note([_mcp_2_3_server()]).line
    assert line.startswith(
        "`Server(...)` with `on_set_logging_level=`, `on_roots_list_changed=` or `on_progress=` "
        "passed as keyword arguments is deprecated; avoid it in new code."
    )
    assert "x: int" not in line and len(line) < 300
    # A short form is still quoted; a long one with no parameter of its own is only named.
    short = change(
        DEPRECATED, "sdk.with_config", call_form="with_config(*, config)", call_form_only=[]
    )
    assert diff_note([short]).line == (
        "`sdk.with_config` called as `with_config(*, config)` is deprecated; avoid it in new "
        "code. [diff]"
    )
    long = change(
        DEPRECATED, "sdk.cached", call_form="cached(" + "a, " * 60 + ")", call_form_only=[]
    )
    assert diff_note([long]).line == (
        "One call form of `sdk.cached` (an overload) is deprecated; avoid that form in new "
        "code. [diff]"
    )


def test_the_heading_and_the_listing_name_the_call_form_too() -> None:
    """The scan's heading ("Server is deprecated when called as __init__(self, name: str, ...")
    and ``--all``'s listing (APIChange.describe) dumped the 400-character signature next to the
    fixed note. They name the form's keywords, quote a signature only when it is short, and
    otherwise say that one call form is deprecated."""
    real = _mcp_2_3_server()
    assert real.describe() == (
        "`mcp.server.lowlevel.server.Server.__init__` called with `on_set_logging_level=`, "
        "`on_roots_list_changed=`, `on_progress=` is deprecated (mcp 2.3.0): Passing any of "
        "them emits an MCPDeprecationWarning at runtime."
    )
    assert changes_text([real]) == (
        True,
        "is deprecated when called with on_set_logging_level=, on_roots_list_changed= or "
        "on_progress=",
    )
    assert changes_text([real], code=True)[1].startswith(
        "is deprecated when called with `on_set_logging_level=`, "
    )
    short = change(
        DEPRECATED, "sdk.with_config", call_form="with_config(*, config)", call_form_only=[]
    )
    assert short.describe() == (
        "`sdk.with_config` called as `with_config(*, config)` is deprecated (pkg 2.0)"
    )
    assert changes_text([short])[1] == "is deprecated when called as with_config(*, config)"
    long = change(
        DEPRECATED, "sdk.cached", call_form="cached(" + "a, " * 60 + ")", call_form_only=[]
    )
    assert long.describe() == "`sdk.cached`: one call form (an overload) is deprecated (pkg 2.0)"
    assert changes_text([long])[1] == "is deprecated in one call form (an overload)"


def _file(text: str, name: str = "app/main.py"):
    return (scan_file(ast.parse(textwrap.dedent(text)), name),)


def test_a_call_is_in_the_old_form_only_when_it_passes_a_deprecated_forms_keyword(
    tmp_path,
) -> None:
    c = _server_change(tmp_path)
    plain = _file("from mcp import Server\nserver = Server('x')\n")
    [use] = uses(c, plain)
    assert (use.kind, use.form, use.names) == (USE_CALL, USES_API, ("Server",))
    passing = _file("from mcp import Server\nserver = Server('x', on_progress=print)\n")
    [use] = uses(c, passing)
    assert (use.kind, use.form, use.names) == (USE_KEYWORD, OLD_FORM, ("Server", "on_progress"))
    assert form(c, ("Server", "on_progress")) == OLD_FORM and form(c, ("Server",)) == USES_API
    other = _file("from mcp import Server\nserver = Server('x', version='1')\n")
    assert [u.form for u in uses(c, other)] == [USES_API]
    # A deprecated form from an older diff, without the parameters, stays "uses this API".
    old = change(DEPRECATED, "sdk.with_config", call_form="with_config(*, config)")
    assert form(old, ("with_config",)) == USES_API
    [use] = uses(old, _file("from sdk import with_config\nwith_config(config=1)\n"))
    assert use.form == USES_API


# ------------------------------------------------- I: wording, and twin bullets
def _create(parameter: str, **kw: Any) -> APIChange:
    return change(
        PARAM_REMOVED,
        "anthropic.resources.messages.messages.Messages.create",
        owner="Messages",
        parameter=parameter,
        pkg="anthropic",
        old="0.60.0",
        new="1.8.0",
        **kw,
    )


def test_a_removed_body_field_points_at_extra_body_alone() -> None:
    """The audit's anthropic note said "pass them through its `extra_body` or `extra_query`
    argument. since-cutoff found no replacement": a field of the request body goes in
    extra_body only, and the pointer is the replacement."""
    extras = {"request_extras": ["extra_body", "extra_query"]}
    note = diff_note([_create(p, **extras) for p in ("top_p", "temperature", "top_k")])
    assert note.line == (
        "`Messages.create()` no longer accepts `temperature`, `top_k` or `top_p` as keyword "
        "arguments. If the API still needs them, pass them through its `extra_body` argument. "
        "[diff]"
    )
    assert "found no replacement" not in note.bullet
    # Nor a changelog pointer: the bullet says where the field goes.
    assert diff_note([_create("top_k", **extras)], changelog="https://x.example").line == (
        "`Messages.create()` no longer accepts `top_k` as a keyword argument. If the API still "
        "needs it, pass it through its `extra_body` argument. [diff]"
    )
    # A method that takes only extra_query sends a query: that is where the field goes.
    query = change(
        PARAM_REMOVED,
        "sdk.Files.list",
        owner="Files",
        parameter="order",
        request_extras=["extra_query"],
    )
    assert "through its `extra_query` argument. [diff]" in diff_note([query]).line
    assert "extra_body" not in diff_note([query]).line


def test_bullets_that_say_the_same_of_several_apis_merge_into_one() -> None:
    """Issue #5 and the audit: anthropic's legacy completions API went as three names, and the
    notes wrote three bullets that differed only in the name."""
    url = "https://github.com/anthropics/anthropic-sdk-python/releases"
    gone = [
        change(
            REMOVED,
            "anthropic.resources.Anthropic.completions",
            owner="Anthropic",
            pkg="anthropic",
            old="0.60.0",
            new="1.8.0",
        ),
        change(REMOVED, "anthropic.AI_PROMPT", pkg="anthropic", old="0.60.0", new="1.8.0"),
        change(REMOVED, "anthropic.HUMAN_PROMPT", pkg="anthropic", old="0.60.0", new="1.8.0"),
    ]
    separate = diff_notes(gone, changelog=url)
    assert len(separate) == 3  # one per API, as every report lists them
    [merged] = diff_notes(gone, changelog=url, merge=True)
    assert merged.line == (
        "`Anthropic.completions`, `anthropic.AI_PROMPT` and `anthropic.HUMAN_PROMPT` were "
        "removed; do not use them. See "
        "https://github.com/anthropics/anthropic-sdk-python/releases (1.8.0). [diff]"
    )
    assert merged.api == "Anthropic.completions" and merged.covered == gone
    assert merged.source == NOTE_DIFF and merged.tag_list == (TAG_DIFF,)
    # Whatever order the changes come in: the names in alphabetical order.
    assert diff_notes(gone[::-1], changelog=url, merge=True)[0].line == merged.line
    # The block is shorter by the two bullets.
    kw: dict[str, Any] = {"model": "m", "cutoff": CUTOFF, "version_source": "uv.lock"}
    before = render_block(separate, **kw)
    after = render_block([merged], **kw)
    assert estimate_tokens(after) < estimate_tokens(before)
    assert after.count("\n- ") == 1 and before.count("\n- ") == 3


def test_twin_functions_losing_the_same_parameters_merge() -> None:
    """huggingface-hub's hf_hub_download and snapshot_download lost the same parameters, with
    the same deprecation text: one bullet."""
    twins = [
        change(
            PARAM_REMOVED,
            f"huggingface_hub.{f}",
            parameter=p,
            pkg="huggingface-hub",
            old="0.34.3",
            new="2.0.0",
            **kw,
        )
        for f in ("hf_hub_download", "snapshot_download")
        for p, kw in (
            ("proxies", {}),
            ("resume_download", {"still_handled_text": "deprecated without replacement"}),
        )
    ]
    [note] = diff_notes(twins, merge=True)
    assert note.line == (
        "`huggingface_hub.hf_hub_download()` and `huggingface_hub.snapshot_download()` no "
        "longer accept `proxies` or `resume_download`; do not pass them. huggingface-hub's "
        "deprecation text says there is no replacement for `resume_download`. since-cutoff "
        "found no replacement for `proxies` in huggingface-hub's deprecation text. [diff]"
    )
    # Twins whose changes differ keep their own bullets.
    apart = twins[:2] + twins[2:3]  # snapshot_download lost only proxies
    assert [n.api for n in diff_notes(apart, merge=True)] == [
        "huggingface_hub.hf_hub_download",
        "huggingface_hub.snapshot_download",
    ]
    # Other sentences take the plural too.
    required = [change(PARAM_REMOVED, f"pkg.{f}", parameter="x") for f in ("a", "b")] + [
        change("param_required", f"pkg.{f}", parameter="timeout") for f in ("a", "b")
    ]
    [note] = diff_notes(required, merge=True)
    assert note.bullet == (
        "`pkg.a()` and `pkg.b()` no longer accept `x`; do not pass it. since-cutoff found no "
        "replacement in pkg's deprecation text. `pkg.a()` and `pkg.b()` now require `timeout`."
    )
    [note] = diff_notes(required[2:], merge=True)
    assert note.bullet == "`pkg.a()` and `pkg.b()` now require `timeout`."


def test_the_legacy_block_and_the_notes_per_api_do_not_change() -> None:
    """``run --compare``'s baselines render the notes they are given, unmerged, as 0.3 did."""
    gone = [change(REMOVED, f"pkg.{n}") for n in ("A", "B")]
    notes = diff_notes(gone)
    assert [n.bullet for n in notes] == [
        "`pkg.A` was removed; do not use it. since-cutoff found no replacement in pkg's "
        "deprecation text.",
        "`pkg.B` was removed; do not use it. since-cutoff found no replacement in pkg's "
        "deprecation text.",
    ]
    legacy = render_legacy_block(notes, model="m", cutoff=CUTOFF, version_source="uv.lock")
    assert legacy.count("\n- ") == 2 and "`pkg.A` was removed" in legacy


TWINS_OLD = {"tw/__init__.py": "class A: ...\nclass B: ...\n"}
TWINS_NEW = {"tw/__init__.py": "class C: ...\n"}


def _twins(tmp_path: Path, cache: DiskCache) -> tuple[Path, ScanResult]:
    """A project using ``tw.A`` and ``tw.B``, both removed in tw 2.0: one merged bullet."""
    trees = {
        ("tw", v): SourceTree("tw", v, write_tree(tmp_path / f"tw-{v}", files), ("tw",))
        for v, files in (("1.0", TWINS_OLD), ("2.0", TWINS_NEW))
    }
    pypi = FakePyPI(cache, {"tw": [("1.0", "2025-01-10"), ("2.0", "2025-10-01")]}, trees)
    root = _app(tmp_path, "tw==2.0", "from tw import A, B\nA()\nB()\n")
    engine = Engine(Settings(), store=cache, llm_cache=cache, pypi=pypi)
    return root, engine.scan(load_project(root), ModelTarget.cutoff_only(CUTOFF))


def _type_checked(note: Note) -> Note:
    """What ``run --apply`` writes for the API of ``note``: a model's bullet whose example
    type-checked."""
    bullet = f"`{note.api}` is gone; use `tw.C` instead."
    return Note(note.change, bullet, None, True, NOTE_MODEL, (TAG_TYPE_CHECKED,))


def test_sync_keeps_a_type_checked_note_next_to_the_other_apis_bullet(tmp_path, cache) -> None:
    """A block ``run --apply`` wrote holds a [type-checked] bullet for tw.A and the diff's for
    tw.B. sync keyed the merged bullet by tw.A alone: with the [type-checked] bullet for tw.A
    it dropped the merged bullet whole, so tw.B lost its note ("tw 2.0: 1 dropped" while the
    code still uses it); with the [type-checked] bullet for tw.B, it found no note keyed by
    tw.B and dropped the [type-checked] one instead. Each API keeps its own note, and the
    block is up to date."""
    root, scan = _twins(tmp_path, cache)
    [merged] = scan.scope_notes()
    assert merged.apis == ["tw.A", "tw.B"]
    a, b = scan.diff_notes()
    kw: dict[str, Any] = {"model": "", "cutoff": CUTOFF, "version_source": "pyproject.toml"}
    for notes in ([_type_checked(a), b], [a, _type_checked(b)]):
        block = render_block(notes, deps=scan.deps_hash(), **kw)
        (root / "AGENTS.md").write_text(block, encoding="utf-8")
        proposal = propose(scan, read_target(root, root / "AGENTS.md"))
        assert proposal.action is None and proposal.changes == [], proposal.block
        assert proposal.block is not None and proposal.notes == 2
        assert [line for line in proposal.block.splitlines() if line.startswith("- ")] == [
            f"- {n.line}" for n in notes
        ]


def test_bullets_that_merge_count_as_changed_and_name_their_apis(tmp_path, cache) -> None:
    """A block since-cutoff 0.5 wrote has one bullet per API: when they merge, sync said
    "1 changed, 1 dropped" (one API per merged line); both changed. report.md's list of sources
    named the first API only."""
    root, scan = _twins(tmp_path, cache)
    a, b = scan.diff_notes()
    block = render_block(
        [a, b], model="", cutoff=CUTOFF, version_source="pyproject.toml", deps=scan.deps_hash()
    )
    (root / "AGENTS.md").write_text(block, encoding="utf-8")
    proposal = propose(scan, read_target(root, root / "AGENTS.md"))
    assert [c.done for c in proposal.changes] == ["tw 2.0: 2 changed"]
    [merged] = scan.scope_notes()
    assert proposal.apis == {merged.line: ("tw.A", "tw.B")}
    assert "- `tw.A` and `tw.B` [diff]: tw 1.0 -> 2.0; stated from the API diff" in (
        render_markdown(scan)
    )


# ------------------------------------------------- J: a package newer than the cutoff
def test_the_note_for_a_package_first_released_after_the_cutoff() -> None:
    """httpx2 0.0.0 reserved the name on 2026-05-11; the pinned 2.13.1 came out on 2026-09-23.
    The note said "2.13.1 was first released on 2026-05-11": it gives both dates."""
    note = new_package_note(
        "httpx2",
        "2.13.1",
        "2026-05-11",
        "The next generation HTTP client.",
        "https://github.com/pydantic/httpx2/blob/main/src/httpx2/CHANGELOG.md",
        "2026-09-23",
    )
    assert note.line == (
        "httpx2 first appeared on PyPI on 2026-05-11, after the cutoff; the model has no "
        "training data on it. The project pins 2.13.1 (released 2026-09-23). Summary: The next "
        "generation HTTP client. Changelog: "
        "https://github.com/pydantic/httpx2/blob/main/src/httpx2/CHANGELOG.md [metadata]"
    )
    assert estimate_tokens(note.line) <= 75
    assert note.tag_list == (TAG_METADATA,) and note.api == "httpx2"
    assert note.change.kind == NEW_PACKAGE and note.applies_to()["version"] == "2.13.1"
    assert note.checks()["change"].startswith("the release list and metadata of httpx2 on PyPI")
    assert note.checks()["runtime"] == "not checked"
    # Without a date, summary or URL on PyPI: only what is known.
    assert new_package_note("fresh", "1.0", None).line == (
        "fresh first appeared on PyPI after the cutoff; the model has no training data on it. "
        "The project pins 1.0. [metadata]"
    )
    assert tag_legend([TAG_METADATA]) == [
        "[metadata] PyPI metadata: the two releases' Requires-Dist (in their wheels' METADATA), "
        "or a package's release dates, summary and changelog URL"
    ]


def _new_package_pypi(cache: DiskCache) -> FakePyPI:
    return FakePyPI(
        cache,
        {
            "httpx2": [("2.12.0", "2026-05-11"), ("2.13.1", "2026-06-02")],
            "old": [("1.0", "2024-01-01")],
        },
        {},
        info={
            "httpx2": {
                "summary": "The next generation HTTP client.",
                "project_urls": {
                    "Changelog": "https://github.com/pydantic/httpx2/blob/main/src/httpx2/CHANGELOG.md",
                    "Homepage": "https://github.com/pydantic/httpx2",
                },
            }
        },
    )


HTTPX2_NOTE = (
    "- httpx2 first appeared on PyPI on 2026-05-11, after the cutoff; the model has no training "
    "data on it. The project pins 2.13.1 (released 2026-06-02). Summary: The next generation "
    "HTTP client. Changelog: https://github.com/pydantic/httpx2/blob/main/src/httpx2/CHANGELOG.md "
    "[metadata]"
)


def test_sync_writes_the_note_for_a_new_package_the_code_imports(tmp_path, cache) -> None:
    root = tmp_path / "app"
    root.mkdir()
    (root / "requirements.txt").write_text("httpx2==2.13.1\nold==1.0\n")
    (root / "main.py").write_text("import httpx2\nhttpx2.get('u')\n")
    engine = Engine(Settings(), store=cache, llm_cache=cache, pypi=_new_package_pypi(cache))
    scan = engine.scan(load_project(root), ModelTarget.cutoff_only(CUTOFF))
    httpx2 = scan.package("httpx2")
    assert (httpx2.status, httpx2.first_released, httpx2.locked_date, httpx2.summary) == (
        NEW,
        "2026-05-11",
        "2026-06-02",
        "The next generation HTTP client.",
    )
    [note] = scan.scope_notes()
    assert f"- {note.line}" == HTTPX2_NOTE
    assert scan.used_apis() == []  # not a changed API the code uses
    proposal = propose(scan, read_target(root, root / "AGENTS.md"))
    assert proposal.block is not None and proposal.notes == 1
    block = parse_block(proposal.block)
    assert block is not None and block.packages["httpx2"].version == "2.13.1"
    assert block.packages["httpx2"].bullets == [(HTTPX2_NOTE[2:].rsplit(" [", 1)[0], ("metadata",))]
    assert "**httpx2 2.13.1**\n" in proposal.block  # no release at the cutoff to name
    assert "[metadata] PyPI metadata:" in proposal.block  # the legend explains the tag
    # The scan says the note is ready, and the previews show it.
    text = "\n".join(line.plain for line in scan_lines(scan, width=200))
    assert "Your code imports httpx2 2.13.1 (first released 2026-05-11, after the cutoff)" in text
    assert "1 note ready: `since-cutoff sync` writes it to AGENTS.md" in text
    assert HTTPX2_NOTE in render_scan_markdown(scan) and HTTPX2_NOTE in render_markdown(scan)
    data = to_json(scan)
    assert data["notes_preview"]["tokens"] > 0 and HTTPX2_NOTE in data["notes_preview"]["block"]
    assert data["used"]["new_packages_imported"] == ["httpx2"]
    # Once the code no longer imports it, the section goes, and sync says why.
    (root / "AGENTS.md").write_text(proposal.new_text or "", encoding="utf-8")
    (root / "main.py").write_text("import old\n")
    scan = engine.scan(load_project(root), ModelTarget.cutoff_only(CUTOFF))
    assert scan.scope_notes() == []
    proposal = propose(scan, read_target(root, root / "AGENTS.md"))
    assert proposal.action == "removed from"
    assert [c.done for c in proposal.changes] == [
        "httpx2 dropped (first released after the cutoff, and your code no longer imports it)"
    ]


def test_a_new_package_the_code_does_not_import_gets_no_note(tmp_path, cache) -> None:
    root = tmp_path / "app"
    root.mkdir()
    (root / "requirements.txt").write_text("httpx2==2.13.1\n")
    (root / "main.py").write_text("import json\n")
    engine = Engine(Settings(), store=cache, llm_cache=cache, pypi=_new_package_pypi(cache))
    scan = engine.scan(load_project(root), ModelTarget.cutoff_only(CUTOFF))
    assert scan.package("httpx2").status == NEW and scan.scope_notes() == []
    assert scan.notes_block(scan.scope_notes()) is None
    assert json.dumps(to_json(scan)["notes_preview"]) == "null"
