"""`scan` leads with the project: the changed APIs its code uses, where, in what form, and the
note for each; then one line for the rest (``--all`` lists it). The same in the Markdown summary,
report.md, JSON and the MCP answer; ``--fail-on`` and ``--annotate github`` for CI.

Where the code uses an API is file-level here (``app/main.py``): the lines are issue #8's, and
the "Used in" output is ready for them (``Use.line``)."""

from __future__ import annotations

import ast
import io
import json
import textwrap
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from since_cutoff import cli, net, report
from since_cutoff.apidiff import (
    DEPRECATED,
    KIND_CHANGED,
    MOVED,
    PARAM_KEYWORD_ONLY,
    PARAM_POSITIONAL_ONLY,
    PARAM_REMOVED,
    PARAM_REQUIRED,
    REMOVED,
    APIChange,
)
from since_cutoff.cache import DiskCache
from since_cutoff.engine import (
    CHANGED,
    NEW,
    Engine,
    ModelTarget,
    PackageScan,
    Reporter,
    ScanResult,
    Settings,
    UsedAPI,
)
from since_cutoff.mcp_server import PROJECT_BUDGET, Target, Tools, _render_project
from since_cutoff.models import ModelRegistry
from since_cutoff.notes import diff_note
from since_cutoff.project import Dependency, FileUse, Project, load_project, scan_file, scan_sources
from since_cutoff.pypi import PyPI, SourceTree
from since_cutoff.report import (
    api_head,
    changes_text,
    github_annotations,
    render_console,
    render_markdown,
    render_scan,
    render_scan_changes,
    render_scan_markdown,
    scan_lines,
    to_json,
    versions_text,
)
from since_cutoff.selection import (
    NAME_MATCH,
    OLD_FORM,
    USE_CALL,
    USE_KEYWORD,
    USE_MEMBER,
    USE_REFERENCE,
    USES_API,
    Use,
    form,
    used_names,
    uses,
)
from tests.conftest import TOYLIB_V1, TOYLIB_V2, FakePyPI, write_tree
from tests.test_ci import scan_app

CUTOFF = ["--model", "anthropic:claude-sonnet-4-5", "--cutoff", "2025-07-31"]
MAIN = "from toylib import Client, fetch\nClient().send('hi', temperature=0.2)\nfetch('u')\n"
OTHER = "from toylib import Client\nClient().send('x')\n"


def make_app(tmp_path: Path, main: str = MAIN, other: str | None = OTHER, pin: str = "toylib==2.0"):
    root = tmp_path / "app"
    (root / "sub").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "app"\nversion = "0"\ndependencies = ["{pin}"]\n'
    )
    (root / "main.py").write_text(main)
    if other is not None:
        (root / "sub" / "other.py").write_text(other)
    return root


@pytest.fixture
def app(tmp_path: Path) -> Path:
    return make_app(tmp_path)


@pytest.fixture
def scan(app: Path, cache: DiskCache, fake_pypi: FakePyPI) -> ScanResult:
    return scan_app(app, cache, fake_pypi, date(2025, 7, 31))


@pytest.fixture
def fake_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_pypi: Any) -> None:
    monkeypatch.setenv("SINCE_CUTOFF_CACHE", str(tmp_path / "cli-cache"))
    for name in ("GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_WORKSPACE", "COLUMNS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        cli, "Engine", lambda settings, **kw: Engine(settings, **{**kw, "pypi": fake_pypi})
    )


def text_of(lines: list[Any]) -> str:
    return "\n".join(line.plain for line in lines)


# ------------------------------------------------------------------ the terminal
GOLDEN = """\
Your code uses 2 APIs that changed after claude-sonnet-4-5's training cutoff (2025-07-31)

toylib 1.0 -> 2.0 (1.0 was the latest release 30 days before the cutoff; pyproject.toml pins 2.0)
  Client.send: temperature was removed; stream is now keyword-only                          old form
    main.py        passes temperature to send
    sub/other.py   calls send
    Note: `Client.send()` no longer accepts `temperature`; do not pass it. since-cutoff found no
          replacement in toylib's deprecation text. Pass `stream` to `Client.send()` by keyword.
          [diff]
  fetch: now requires timeout                                                          uses this API
    main.py   calls fetch
    Note: `toylib.fetch()` now requires `timeout`. [diff]

2 notes ready: `since-cutoff sync` writes them to AGENTS.md and keeps them in step with
  pyproject.toml.

old form: valid for 1.0; 2.0 removed, moved or deprecated what it uses (a static name match; nothing
  was run)
uses this API: not in the old form, but an assistant editing this code may write the old form
[diff] a static comparison of the two releases' public APIs
Also changed, not used by your code: 3 changes in toylib. `--all` lists them."""


def test_scan_leads_with_the_apis_the_code_uses(scan: ScanResult) -> None:
    assert text_of(scan_lines(scan)) == GOLDEN


def test_the_first_section_fits_a_narrow_terminal(scan: ScanResult) -> None:
    console = Console(width=60, record=True, soft_wrap=True)
    render_scan(console, scan)
    lines = console.export_text().splitlines()
    assert all(len(line) <= 60 for line in lines)
    # The label stays at the end of its API's last line, and the note wraps under "Note: ".
    assert "  is now keyword-only                               old form" in lines
    assert "    Note: `Client.send()` no longer accepts `temperature`;" in lines
    assert "          do not pass it. since-cutoff found no replacement" in lines


def test_the_cli_prints_the_project_first_and_all_adds_0_3_layout(
    app: Path, capsys: pytest.CaptureFixture[str], fake_cli: None, cache: DiskCache, fake_pypi: Any
) -> None:
    assert cli.main(["scan", str(app), *CUTOFF]) == 0
    out = capsys.readouterr().out
    first = out.index("Your code uses 2 APIs that changed after claude-sonnet-4-5's")
    assert out.index("• Diffing the API of 1 package") < first < out.index("Full report:")
    assert GOLDEN in out.replace("\r\n", "\n")
    # The rest is one line: no panel, table or per-package list without --all.
    assert "since-cutoff · claude-sonnet-4-5" not in out and "5 breaking, 1 deprecated" not in out

    assert cli.main(["scan", str(app), *CUTOFF, "--all", "--limit", "3"]) == 0
    full = capsys.readouterr().out
    assert GOLDEN.rsplit("\n", 1)[0] in full  # the same first section, less "Also changed"
    assert "`--all` lists them" not in full
    # Then 0.3's layout exactly as render_console and render_scan_changes print it.
    expected = io.StringIO()
    console = Console(file=expected, width=cli.LOG_WIDTH, highlight=False, soft_wrap=True)
    same = scan_app(app, cache, fake_pypi, date(2025, 7, 31))
    render_console(console, same)
    render_scan_changes(console, same, limit=3)
    assert expected.getvalue() in full
    assert full.index("Your code uses 2 APIs") < full.index("since-cutoff · claude-sonnet-4-5")


def test_verbose_lists_every_file_and_the_default_three(tmp_path, cache, fake_pypi) -> None:
    root = make_app(tmp_path, main="from toylib import Client\nClient().send('x')\n", other=None)
    for i in range(4):
        (root / "sub" / f"m{i}.py").write_text("from toylib import Client\nClient().send('y')\n")
    scan = scan_app(root, cache, fake_pypi, date(2025, 7, 31))
    short = text_of(scan_lines(scan))
    shown = [
        "    main.py     calls send",
        "    sub/m0.py   calls send",
        "    sub/m1.py   calls send",
    ]
    assert "\n".join(shown) in short
    assert "sub/m2.py" not in short and "    and 2 more (-v lists them)" in short
    full = text_of(scan_lines(scan, verbose=True))
    assert "    sub/m3.py   calls send" in full and "more (-v" not in full
    assert "`sub/m1.py`<br>and 2 more | `Client.send`" in render_scan_markdown(scan)


def test_nothing_used(tmp_path, cache, fake_pypi) -> None:
    root = make_app(tmp_path, main="import json\n", other=None)
    scan = scan_app(root, cache, fake_pypi, date(2025, 7, 31))
    assert text_of(scan_lines(scan)) == (
        "Your code uses none of the APIs that changed after claude-sonnet-4-5's training cutoff "
        "(2025-07-31).\n"
        "Changed in your dependencies, not seen in your code: 6 changes in toylib. `--all` "
        "lists them.\n"
        "No notes to write."
    )
    data = to_json(scan)
    assert data["used"]["apis"] == 0 and data["used_apis"] == []
    md = render_scan_markdown(scan)
    assert "**Your code uses none of the APIs that changed after the cutoff**" in md
    assert "| where | API |" not in md and "notes ready" not in md
    assert "Your code uses none of the APIs that changed after" in render_markdown(scan)


def test_no_dependency_changed(tmp_path, cache, fake_pypi) -> None:
    root = make_app(tmp_path, pin="toylib==1.0")
    scan = scan_app(root, cache, fake_pypi, date(2025, 7, 31))
    assert text_of(scan_lines(scan)) == (
        "1 dependency checked; none changed its API after claude-sonnet-4-5's training cutoff "
        "(2025-07-31)."
    )
    assert "## Used by your code" not in render_markdown(scan)


def test_a_package_first_released_after_the_cutoff_that_the_code_imports(
    tmp_path, cache, fake_pypi
) -> None:
    root = tmp_path / "app"
    root.mkdir()
    (root / "requirements.txt").write_text("brand-new==1.2\nunused-new==3.0\n")
    (root / "main.py").write_text("import brand_new\nbrand_new.go()\n")
    pypi = FakePyPI(
        cache,
        {
            "brand-new": [("1.0", "2025-11-03"), ("1.2", "2026-01-10")],
            "unused-new": [("3.0", "2025-12-01")],
        },
        {},
    )
    engine = Engine(Settings(), store=cache, llm_cache=cache, pypi=pypi)
    scan = engine.scan(load_project(root), ModelTarget.cutoff_only(date(2025, 7, 31)))
    assert [p.status for p in scan.packages] == [NEW, NEW]
    lines = text_of(scan_lines(scan, width=200))
    assert (
        "Your code imports brand-new 1.2 (first released 2025-11-03, after the cutoff): its whole "
        "API is newer than the cutoff." in lines
    )
    assert "unused-new" not in lines
    data = to_json(scan)
    assert data["used"]["new_packages_imported"] == ["brand-new"]
    assert {p["name"]: p["first_released"] for p in data["scan"]} == {
        "brand-new": "2025-11-03",
        "unused-new": "2025-12-01",
    }
    assert "Your code imports brand-new 1.2 (first released 2025-11-03" in render_scan_markdown(
        scan
    )
    mcp = _render_project(scan, Target(date(2025, 7, 31), "given"), [], 10)
    assert (
        "- Your code imports brand-new 1.2, first released 2025-11-03: released after your "
        "reported training cutoff, so its whole API may be missing from your training data."
    ) in mcp


def test_the_versions_line_names_where_the_version_comes_from(tmp_path) -> None:
    def line(source: str, pinned_in: str | None = None) -> str:
        dep = Dependency("toylib", "2.0", True, source, pinned_in=pinned_in)
        project = Project(tmp_path, [dep], source)
        p = PackageScan("toylib", "2.0", source, True, status=CHANGED, cutoff_version="1.0")
        scan = ScanResult(project, ModelTarget.cutoff_only(date(2025, 7, 31)), [p])
        return versions_text(scan, p).split("; ", 1)[1]

    assert line("uv.lock") == "uv.lock pins 2.0)"
    assert line("pylock.web.toml") == "pylock.web.toml pins 2.0)"
    assert line("pinned", "requirements/prod.txt") == "requirements/prod.txt pins 2.0)"
    assert line("pinned") == "the project pins 2.0)"
    assert line("installed") == "2.0 is installed)"
    assert line("latest on PyPI for Python 3.12") == "2.0 is the latest on PyPI for Python 3.12)"
    assert line("requested") == "requested: 2.0)"


def test_the_rest_of_the_first_section(tmp_path) -> None:
    """Names that merely look similar, the runtime caveat, a place with no file, old forms in
    two packages, a placeholder release at the cutoff, a dependency not checked, warnings."""
    gone = APIChange(
        "hub",
        "0.3",
        "2.0",
        PARAM_REMOVED,
        "hub.download",
        "download",
        None,
        "resume",
        suggestions=["resumable"],
        still_handled_at="hub/_validators.py:187",
    )
    removed = APIChange("sdk", "1", "2", REMOVED, "sdk.old", "old")
    hub = PackageScan("hub", "2.0", "uv.lock", True, status=CHANGED, cutoff_version="0.3")
    sdk = PackageScan("sdk", "2", "uv.lock", True, status=CHANGED, cutoff_version="1")
    hub.changes, sdk.changes = [gone], [removed]
    placeholder = PackageScan("zen", "0.5", "uv.lock", True, True, NEW, cutoff_version="0.0.0")
    placeholder.reason = "0.0.0 at the cutoff was an empty placeholder"
    unknown_day = PackageScan("fresh", "1.0", "uv.lock", True, status=NEW)
    skipped = PackageScan("big", "9", "uv.lock", True, reason="big==9 is 200 MB, above the limit")
    project = Project(tmp_path, [], "uv.lock", imported_modules={"fresh"})
    scan = ScanResult(
        project,
        ModelTarget.cutoff_only(date(2025, 7, 31)),
        [hub, sdk, placeholder, unknown_day, skipped],
        ["ignored poetry.lock"],
    )
    keyword = Use("", None, None, USE_KEYWORD, ("download", "resume"), form=OLD_FORM)
    used = [
        UsedAPI(hub, diff_note([gone]), [keyword], {gone.id: OLD_FORM}, "path"),
        UsedAPI(sdk, diff_note([removed]), [], {removed.id: OLD_FORM}, "path"),
    ]
    scan.__dict__["_used"] = (scan._used_key(), used)
    text = text_of(scan_lines(scan, width=200))
    assert "  download: resume is no longer in its signature" in text
    assert "    a file of your code   passes resume to download" in text
    assert "    Runtime: 2.0's source still handles resume (hub/_validators.py:187)" in text
    assert "    Similar names in 2.0, not confirmed as replacements: resumable" in text
    assert (
        "old form: valid for the release at the cutoff; the pinned release removed, moved or "
        "deprecated what it uses" in text
    )
    assert "uses this API:" not in text
    assert (
        "Your code imports zen 0.5 (0.0.0 at the cutoff was an empty placeholder): its whole API "
        "is newer than the cutoff." in text
    )
    assert "Your code imports fresh 1.0 (first released after the cutoff)" in text
    assert "1 of 5 dependencies could not be checked: big==9 is 200 MB, above the limit" in text
    assert text.endswith("! ignored poetry.lock")
    assert "Also changed" not in text  # every change of both packages is used
    assert github_annotations(scan) == []  # no file to annotate
    md = render_markdown(scan)
    assert "  - Runtime: 2.0's source still handles `resume` (hub/_validators.py:187)" in md
    assert "  - Similar names in 2.0, not confirmed as replacements: `resumable`" in md
    mcp = _render_project(scan, Target(date(2025, 7, 31), "given"), [], 10)
    assert "  - runtime: 2.0's source still handles `resume`" in mcp
    assert "  - similar names in 2.0, not confirmed as replacements: `resumable`" in mcp
    assert mcp.index("## Your code uses these changed APIs") < mcp.index("Your code imports zen")
    # Unused changes in several packages: the three with the most, by name.
    for name in ("hub", "sdk"):
        extra = [APIChange(name, "1", "2", REMOVED, f"{name}.x{i}", f"x{i}") for i in range(3)]
        scan.package(name).changes = [*scan.package(name).changes, *extra]
    scan.__dict__["_used"] = (scan._used_key(), used)
    assert "Also changed, not used by your code: 6 changes in 2 packages (hub 3, sdk 3)." in (
        text_of(scan_lines(scan, width=200))
    )
    assert report.use_text([Use("a.py", None, None, USE_MEMBER, ("routes",))]) == "reads routes"
    # A line the file no longer has (edited since the scan), or a file that is gone: no code.
    (tmp_path / "a.py").write_text("x = 1\n")
    assert report._snippet(tmp_path, Use("a.py", 9, 0, USE_CALL, ("f",)), {}) is None
    assert report._snippet(tmp_path, Use("gone.py", 1, 0, USE_CALL, ("f",)), {}) is None


def test_load_project_records_the_file_that_pins_a_dependency(tmp_path) -> None:
    (tmp_path / "requirements").mkdir()
    (tmp_path / "requirements" / "prod.txt").write_text("toylib==2.0\nother>=1\n")
    deps = {d.key: d for d in load_project(tmp_path).dependencies}
    assert (deps["toylib"].source, deps["toylib"].pinned_in) == ("pinned", "requirements/prod.txt")
    assert deps["other"].pinned_in is None


# ------------------------------------------------------------ where and how used
def code(text: str, file: str = "app/main.py") -> tuple[FileUse, ...]:
    return (scan_file(ast.parse(textwrap.dedent(text)), file),)


def change(kind: str, path: str, owner: str | None = None, parameter: str | None = None, **kw):
    name = path.rsplit(".", 1)[-1]
    return APIChange("sdk", "1", "2", kind, path, name, owner, parameter, **kw)


@pytest.mark.parametrize(
    ("the_change", "source", "kind", "the_form"),
    [
        (change(REMOVED, "sdk.old"), "from sdk import old\nold()\n", USE_CALL, OLD_FORM),
        (change(REMOVED, "sdk.OLD"), "import sdk\nx = sdk.OLD\n", USE_REFERENCE, OLD_FORM),
        (
            change(MOVED, "sdk.helpers.Thing", moved_to="sdk.things.Thing"),
            "from sdk.helpers import Thing\n",
            USE_REFERENCE,
            OLD_FORM,
        ),
        (change(KIND_CHANGED, "sdk.value"), "from sdk import value\n", USE_REFERENCE, OLD_FORM),
        (
            change(DEPRECATED, "sdk.legacy"),
            "from sdk import legacy\nlegacy()\n",
            USE_CALL,
            OLD_FORM,
        ),
        (
            change(DEPRECATED, "sdk.with_config", call_form="with_config(*, config)"),
            "from sdk import with_config\nwith_config(config=1)\n",
            USE_CALL,
            USES_API,
        ),
        (
            change(PARAM_REMOVED, "sdk.fetch", parameter="resume"),
            "from sdk import fetch\nfetch('u', resume=True)\n",
            USE_KEYWORD,
            OLD_FORM,
        ),
        (
            change(PARAM_REMOVED, "sdk.fetch", parameter="resume"),
            "from sdk import fetch\nfetch('u')\n",
            USE_CALL,
            USES_API,
        ),
        (
            change(DEPRECATED, "sdk.fetch", parameter="proxies"),
            "from sdk import fetch\nfetch('u', proxies={})\n",
            USE_KEYWORD,
            OLD_FORM,
        ),
        (
            change(PARAM_REQUIRED, "sdk.fetch", parameter="timeout"),
            "from sdk import fetch\nfetch('u', timeout=1)\n",
            USE_KEYWORD,
            USES_API,
        ),
        (
            change(PARAM_KEYWORD_ONLY, "sdk.fetch", parameter="stream"),
            "from sdk import fetch\nfetch('u', stream=True)\n",
            USE_KEYWORD,
            USES_API,
        ),
        (
            change(PARAM_POSITIONAL_ONLY, "sdk.fetch", parameter="url"),
            "from sdk import fetch\nfetch(url='u')\n",
            USE_KEYWORD,
            USES_API,
        ),
        (
            change(REMOVED, "sdk.Client.routes", owner="Client"),
            "from sdk import Client\napp = Client()\nprint(app.routes)\n",
            USE_MEMBER,
            OLD_FORM,
        ),
        (
            change(PARAM_REMOVED, "sdk.Client.__init__", owner="Client", parameter="timeout"),
            "from sdk import Client\nClient(timeout=3)\n",
            USE_KEYWORD,
            OLD_FORM,
        ),
    ],
)
def test_each_kind_of_change_has_its_use_and_form(the_change, source, kind, the_form) -> None:
    files = code(source)
    [use] = uses(the_change, files)
    assert (use.file, use.line, use.column, use.kind, use.form) == (
        "app/main.py",
        None,
        None,
        kind,
        the_form,
    )
    assert use.names == used_names(the_change, files)
    assert form(the_change, use.names) == the_form and use.where == "app/main.py"


def test_uses_are_most_specific_first_and_by_file() -> None:
    passed = code("from sdk import fetch\nfetch('u', resume=True)\n", "b.py")
    called = code("from sdk import fetch\nfetch('u')\n", "a.py")
    imported = code("from sdk import fetch\n", "0.py")
    found = uses(change(PARAM_REMOVED, "sdk.fetch", parameter="resume"), imported + called + passed)
    assert [(u.file, u.kind) for u in found] == [
        ("b.py", USE_KEYWORD),
        ("a.py", USE_CALL),
        ("0.py", USE_REFERENCE),
    ]
    assert (
        uses(change(REMOVED, "sdk.other"), passed) == []
        and form(change(REMOVED, "x"), ()) == USES_API
    )


def test_a_use_matched_by_name_says_so() -> None:
    # No import paths: a diff from before DIFF_SCHEMA 12 (the name alone matched).
    old = change(REMOVED, "sdk.deep.module.old")
    [use] = uses(old, code("from sdk.other import old\nold()\n"))
    assert use.how == NAME_MATCH
    assert report.use_text([use]) == "calls old (matched by name)"


def test_scan_sources_records_each_file_relative_to_the_project(tmp_path) -> None:
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "sub" / "mod.py").write_text("import toylib\n")
    (tmp_path / "top.py").write_text("import toylib\n")
    files = scan_sources(tmp_path).files
    assert sorted(f.file for f in files) == ["pkg/sub/mod.py", "top.py"]  # "/" on Windows too
    assert scan_file(ast.parse("import toylib\n")).file == ""
    assert len({*files, *files}) == 2 and hash(files[0])  # still hashable


def test_the_used_in_output_is_ready_for_lines(scan: ScanResult, app: Path, monkeypatch) -> None:
    """Once the scan records lines (issue #8), a place reads ``main.py:2`` with its code, the
    JSON has line, column and code, and an annotation has ``line`` and ``col``."""
    [send, _] = scan.used_apis()
    lined = [u._replace(line=2, column=0) if u.file == "main.py" else u for u in send.uses]
    monkeypatch.setattr(ScanResult, "used_apis", lambda self: [replace(send, uses=lined)])
    lines = text_of(scan_lines(scan))
    assert "    main.py:2      Client().send('hi', temperature=0.2)" in lines
    [entry] = to_json(scan)["used_apis"]
    assert entry["locations"][0] == {
        "file": "main.py",
        "line": 2,
        "column": 0,
        "kind": USE_KEYWORD,
        "names": ["send", "temperature"],
        "form": OLD_FORM,
        "match": "path",
        "code": "Client().send('hi', temperature=0.2)",
    }
    assert github_annotations(scan)[0].startswith("::warning file=main.py,line=2,col=1,title=")


# ------------------------------------------------------------------- the words
def test_changes_are_said_in_words() -> None:
    def say(*changes: APIChange) -> tuple[bool, str]:
        return changes_text(list(changes))

    assert say(change(REMOVED, "sdk.x")) == (True, "was removed")
    assert say(change(MOVED, "sdk.x", moved_to="sdk.y.x")) == (True, "moved to sdk.y.x")
    assert changes_text([change(MOVED, "sdk.x", moved_to="sdk.y.x")], code=True)[1] == (
        "moved to `sdk.y.x`"
    )
    assert say(change(KIND_CHANGED, "sdk.x", old_kind="class", new_kind="function")) == (
        True,
        "changed from class to function",
    )
    assert say(change(KIND_CHANGED, "sdk.x")) == (True, "changed kind")
    assert say(change(DEPRECATED, "sdk.x")) == (True, "is deprecated")
    assert say(change(DEPRECATED, "sdk.x", call_form="x(*,  a)")) == (
        True,
        "is deprecated when called as x(*, a)",
    )
    assert say(
        change(DEPRECATED, "sdk.f", parameter="a"), change(DEPRECATED, "sdk.f", parameter="b")
    ) == (False, "a and b are deprecated")
    assert say(
        change(PARAM_REMOVED, "sdk.f", parameter="a"),
        change(PARAM_REQUIRED, "sdk.f", parameter="t"),
        change(PARAM_POSITIONAL_ONLY, "sdk.f", parameter="u"),
    ) == (False, "a was removed; now requires t; u is now positional-only")
    shim = change(PARAM_REMOVED, "sdk.f", parameter="a", still_handled_at="sdk/v.py:3")
    assert say(shim, change(PARAM_REMOVED, "sdk.f", parameter="b")) == (
        False,
        "a and b are no longer in its signature",
    )


def test_an_api_is_named_as_the_code_knows_it(tmp_path) -> None:
    def head(*changes: APIChange) -> str:
        note = diff_note(list(changes))
        p = PackageScan("sdk", "2", "uv.lock", True)
        return api_head(UsedAPI(p, note, [], {}, None))

    assert head(change(REMOVED, "sdk.old")) == "old was removed"  # top-level: under its package
    assert head(change(REMOVED, "sdk.mod.old")) == "sdk.mod.old was removed"
    assert head(change(PARAM_REMOVED, "sdk.C.send", "C", "t")) == "C.send: t was removed"
    assert head(change(PARAM_REMOVED, "sdk.C.__init__", "C", "t")) == "C: t was removed"


# ------------------------------------------------------------------------- JSON
def test_json_lists_the_used_apis_with_where_and_how(scan: ScanResult) -> None:
    data = json.loads(json.dumps(to_json(scan), default=str))
    assert next(iter(data)) == "report_schema" and data["report_schema"] == 3
    assert data["settings"]["griffe_version"]
    assert data["used"] == {
        "apis": 2,
        "changes": 3,
        "old_form": 1,
        "files": 2,
        "packages": ["toylib"],
        "new_packages_imported": [],
    }
    send, fetch = data["used_apis"]
    assert (send["display"], send["form"], send["match"]) == ("Client.send", OLD_FORM, "path")
    assert send["versions_from"] == "pyproject.toml"
    assert [(c["parameter"], c["form"]) for c in send["changes"]] == [
        ("temperature", OLD_FORM),
        ("stream", USES_API),
    ]
    assert send["used_in"] == ["main.py", "sub/other.py"]
    assert send["locations_total"] == 2
    assert [(loc["file"], loc["kind"], loc["form"], loc["line"]) for loc in send["locations"]] == [
        ("main.py", USE_KEYWORD, OLD_FORM, None),
        ("sub/other.py", USE_CALL, USES_API, None),
    ]
    assert send["note"]["tags"] == ["diff"] and send["note"]["applies_to"]["version"] == "2.0"
    assert (fetch["form"], fetch["used_in"]) == (USES_API, ["main.py"])
    # 0.3's keys are all still there.
    assert {"tool", "packages", "scan", "breaking_changes", "deprecations"} <= set(data)


# --------------------------------------------------------------------- Markdown
def test_the_markdown_summary_leads_with_a_table_and_links(scan: ScanResult, app: Path) -> None:
    env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_SHA": "abc123", "GITHUB_WORKSPACE": str(app.parent)}
    md = render_scan_markdown(scan, env=env)
    head, rest = md.split("| where | API | change | form | replacement |", 1)
    assert head.endswith("**Your code uses 2 APIs that changed after the cutoff**\n\n")
    rows = [line for line in rest.splitlines() if line.startswith("| [")]
    assert rows == [
        "| [main.py](https://github.com/o/r/blob/abc123/app/main.py)<br>"
        "[sub/other.py](https://github.com/o/r/blob/abc123/app/sub/other.py) "
        "| `Client.send` (toylib 1.0 -> 2.0) | `temperature` was removed; `stream` is now "
        "keyword-only | old form | none named |",
        "| [main.py](https://github.com/o/r/blob/abc123/app/main.py) | `fetch` (toylib 1.0 -> "
        "2.0) | now requires `timeout` | uses this API | none named |",
    ]
    # The folded notes say what writes them (#16 of the 0.4 review).
    assert (
        "<details><summary>2 notes ready for AGENTS.md (`since-cutoff sync` writes them), about "
        in md
    )
    assert md.index("<!-- since-cutoff:start -->") < md.index("<b>toylib</b>")
    # Outside Actions: paths, no links. At the checkout's root: no folder before the path.
    plain = render_scan_markdown(scan)
    assert "| `main.py`<br>`sub/other.py` | `Client.send`" in plain and "/blob/" not in plain
    root = {**env, "GITHUB_WORKSPACE": str(app), "GITHUB_SERVER_URL": "https://ghe.example/"}
    assert "(https://ghe.example/o/r/blob/abc123/main.py)" in render_scan_markdown(scan, env=root)
    outside = {**env, "GITHUB_WORKSPACE": str(app / "elsewhere")}
    assert "(https://github.com/o/r/blob/abc123/main.py)" in render_scan_markdown(scan, env=outside)


def test_a_link_quotes_the_path(scan: ScanResult, monkeypatch) -> None:
    [send, _] = scan.used_apis()
    spaced = [u._replace(file="my app/main.py") for u in send.uses]
    monkeypatch.setattr(ScanResult, "used_apis", lambda self: [replace(send, uses=spaced)])
    env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_SHA": "s"}
    assert "(https://github.com/o/r/blob/s/my%20app/main.py)" in render_scan_markdown(scan, env=env)


def test_report_md_starts_with_what_the_code_uses(scan: ScanResult) -> None:
    md = render_markdown(scan)
    assert md.index("## Used by your code") < md.index("## Summary")
    assert (
        "Your code uses 2 APIs that changed after claude-sonnet-4-5's training cutoff "
        "(2025-07-31), 1 of them in the old form. Old form: valid for 1.0;" in md
    )
    assert (
        "### toylib 1.0 -> 2.0 (1.0 was the latest release 30 days before the cutoff; "
        "pyproject.toml"
    ) in md
    assert (
        "- **`Client.send`: `temperature` was removed; `stream` is now keyword-only** · old form"
        in md
    )
    assert (
        "  - Used in: `main.py` (passes `temperature` to `send`), `sub/other.py` (calls `send`)"
        in md
    )
    full = md.split("## All changes found")[1]
    assert "**your code uses `send` and `temperature`** · used in `main.py`, `sub/other.py`" in full


# ------------------------------------------------------------------ CI: exit codes
@pytest.mark.parametrize(
    ("main", "flags", "code", "why"),
    [
        (MAIN, ["--fail-on", "old-form"], 3, "--fail-on old-form: your code uses 1 changed API"),
        (MAIN, ["--fail-on", "used"], 3, "--fail-on used: your code uses 2 APIs"),
        (MAIN, ["--fail-on", "changes"], 3, "--fail-on changes: 1 dependency changed its API"),
        (MAIN, ["--fail-on-changes"], 3, "--fail-on changes: 1 dependency"),
        (OTHER, ["--fail-on", "old-form"], 0, None),  # calls send, passes nothing removed
        (OTHER, ["--fail-on", "used"], 3, "--fail-on used: your code uses 1 API"),
        ("import json\n", ["--fail-on", "used"], 0, None),
        ("import json\n", ["--fail-on", "changes"], 3, "--fail-on changes"),
        (MAIN, [], 0, None),
    ],
)
def test_fail_on(tmp_path, capsys, fake_cli, main, flags, code, why) -> None:
    root = make_app(tmp_path, main=main, other=None)
    assert cli.main(["scan", str(root), *CUTOFF, *flags]) == code
    out = capsys.readouterr().out
    if why is None:
        assert "Exit code 3" not in out
    else:
        assert f"Exit code 3: {why}" in out


def test_fail_on_takes_known_conditions_only(capsys) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["scan", "--fail-on", "stale"])
    assert "invalid choice: 'stale'" in capsys.readouterr().err
    assert "3 stale API use found with --fail-on-stale, or with scan --fail-on" in cli.EXAMPLES


# -------------------------------------------------------------- CI: annotations
def test_annotations_escape_as_the_actions_toolkit_does() -> None:
    assert report._escape_data("50% done\r\nnext: a, b") == "50%25 done%0D%0Anext: a, b"
    assert report._escape_property("a:b,c%\n") == "a%3Ab%2Cc%25%0A"


def test_annotations_warn_on_the_old_form_and_note_the_rest(scan: ScanResult, app: Path) -> None:
    lines = github_annotations(scan, env={"GITHUB_WORKSPACE": str(app.parent)})
    assert [line.split("::")[1] for line in lines] == [
        "warning file=app/main.py,title=since-cutoff%3A toylib 2.0",
        "notice file=app/sub/other.py,title=since-cutoff%3A toylib 2.0",
        "notice file=app/main.py,title=since-cutoff%3A toylib 2.0",
    ]
    assert lines[0].split("::", 2)[2] == (
        "Client.send: temperature was removed; stream is now keyword-only. toylib 1.0 was the "
        "latest release at claude-sonnet-4-5's training cutoff (2025-07-31); 2.0 is pinned. "
        "Note: `Client.send()` no longer accepts `temperature`; do not pass it. since-cutoff "
        "found no replacement in toylib's deprecation text. Pass `stream` to `Client.send()` by "
        "keyword. [diff] Static match; nothing was run."
    )
    assert all("\n" not in line for line in lines)


def test_annotate_prints_to_stdout_and_needs_it_to_itself(app, capsys, fake_cli) -> None:
    assert cli.main(["scan", str(app), *CUTOFF, "--annotate", "github"]) == 0
    out = capsys.readouterr().out.splitlines()
    commands = [line for line in out if line.startswith("::")]
    assert [c.split(" ", 1)[0] for c in commands] == ["::warning", "::notice", "::notice"]
    assert out[-3:] == commands  # after the report, each on a line of its own
    for other in (["--json"], ["--markdown", "-"]):
        assert cli.main(["scan", str(app), *CUTOFF, "--annotate", "github", *other]) == 2
        assert "cannot both write to stdout" in capsys.readouterr().err


# -------------------------------------------------------------------------- MCP
def test_project_changes_starts_with_the_used_apis(tmp_path, cache, fake_pypi) -> None:
    root = make_app(tmp_path, main=MAIN, other=OTHER)
    for i in range(3):
        (root / "sub" / f"m{i}.py").write_text("from toylib import Client\nClient().send('y')\n")
    tools = Tools(cache, registry=ModelRegistry(cache, offline=True), pypi=fake_pypi)
    out = tools.project_changes(str(root), model="claude-sonnet-4-5")
    assert (
        "- Your code uses 2 APIs that changed after the cutoff, 1 in the old form (as the release "
        "at the cutoff allowed; a static name match)" in out
    )
    section = out.split("## Your code uses these changed APIs\n")[1].split("\n## ")[0]
    assert section.lstrip().startswith(
        "- `Client.send` (toylib 1.0 -> 2.0): `temperature` was removed; `stream` is now "
        "keyword-only [old form]\n"
        "  - used in main.py (passes `temperature` to `send`); sub/m0.py (calls `send`); "
        "sub/m1.py (calls `send`); and 2 more\n"
        "  - note: `Client.send()` no longer accepts `temperature`; do not pass it."
    )
    assert "- `fetch` (toylib 1.0 -> 2.0): now requires `timeout` [uses this API]" in section
    assert out.index("## Your code uses these changed APIs") < out.index("## toylib 1.0")

    quiet_app = make_app(tmp_path / "q", main="import json\n", other=None)
    quiet = tools.project_changes(str(quiet_app), cutoff="2025-07")
    assert "- Your code uses none of the APIs that changed after the cutoff" in quiet
    assert "## Your code uses these changed APIs" not in quiet


def test_project_changes_keeps_the_used_apis_within_the_budget(tmp_path) -> None:
    """Every one of 60 x 30 removed functions used: the used section takes at most half the
    budget and says how many it left out; the answer stays within PROJECT_BUDGET."""
    names = [f"pkg{i:02d}" for i in range(60)]
    paths = {
        p
        for n in names
        for p in (n, f"{n}.module", *(f"{n}.module.function_{j}" for j in range(30)))
    }
    files = [FileUse(paths=frozenset(paths), calls=frozenset(), file="app/main.py")]
    project = Project(tmp_path / "big", [], "uv.lock", files=files)
    scans = []
    for n in names:
        p = PackageScan(n, "2.0", "uv.lock", True, status=CHANGED, cutoff_version="1.0")
        p.import_names = [n]
        p.cutoff_version_date, p.locked_date = "2025-01-01", "2025-10-01"
        p.changes = [
            APIChange(n, "1.0", "2.0", REMOVED, f"{n}.module.function_{j}", f"function_{j}")
            for j in range(30)
        ]
        scans.append(p)
    scan = ScanResult(project, ModelTarget.cutoff_only(date(2025, 7, 31)), scans)
    assert len(scan.used_apis()) == 1800
    text = _render_project(scan, Target(date(2025, 7, 31), "given"), [], 10)
    assert len(text) <= PROJECT_BUDGET
    section = text.split("## Your code uses these changed APIs")[1]
    assert "more: pass `only` to see them" in section
    assert section.count("\n- `") < 1800


# ------------------------------------------------------------------- the network
def test_the_scan_makes_no_request_0_3_did_not(tmp_path, monkeypatch, capsys) -> None:
    """One metadata request per dependency, as in 0.3.1, whatever the flags: the first-release
    date of a new package comes from the same answer, and nothing else is fetched."""
    work = tmp_path / "work"
    trees = {
        ("toylib", "1.0"): SourceTree(
            "toylib", "1.0", write_tree(work / "v1", TOYLIB_V1), ("toylib",)
        ),
        ("toylib", "2.0"): SourceTree(
            "toylib", "2.0", write_tree(work / "v2", TOYLIB_V2), ("toylib",)
        ),
    }

    def files(day: str) -> list[dict[str, Any]]:
        return [{"filename": "x.whl", "upload_time_iso_8601": f"{day}T00:00:00Z", "yanked": False}]

    answers = {
        "toylib": {"1.0": files("2025-01-10"), "2.0": files("2025-10-01")},
        "brand-new": {"1.0": files("2025-11-03")},
    }
    asked: list[str] = []

    def get_json(url: str, **kwargs: Any) -> dict[str, Any]:
        asked.append(url)
        name = url.rstrip("/").split("/")[-2]
        return {"info": {"name": name, "version": "x", "summary": ""}, "releases": answers[name]}

    def no_download(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("no download: the sources come from the local trees")

    class LocalSources(PyPI):
        def source(self, name: str, version: str) -> SourceTree:
            return trees[(name, version)]

    monkeypatch.setattr(net, "get_json", get_json)
    monkeypatch.setattr(net, "get", no_download, raising=False)
    monkeypatch.setenv("SINCE_CUTOFF_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(
        cli,
        "Engine",
        lambda s, **kw: Engine(s, **{**kw, "pypi": LocalSources(kw["store"])}),
    )
    root = make_app(tmp_path, main=MAIN + "import brand_new\n", pin="toylib==2.0")
    (root / "requirements.txt").write_text("toylib==2.0\nbrand-new==1.0\n")
    argv = ["scan", str(root), "--cutoff", "2025-07-31", "--all", "--annotate", "github"]
    assert cli.main([*argv, "--fail-on", "used", "--markdown", str(tmp_path / "s.md")]) == 3
    out = capsys.readouterr().out
    assert "Your code imports brand-new 1.0 (first released 2025-11-03" in out
    assert sorted(asked) == [
        "https://pypi.org/pypi/brand-new/json",
        "https://pypi.org/pypi/toylib/json",
    ]


# ------------------------------------------------------------------------- logs
class _Labels(Reporter):
    def __init__(self) -> None:
        self.labels: list[str | None] = []

    def advance(self, n: int = 1, *, label: str | None = None) -> None:
        self.labels.append(label)


def test_a_cached_diff_adds_no_progress_line_to_a_log(app, cache, fake_pypi) -> None:
    """A warm scan's log goes from "Diffing the API of ..." straight to the results: a line
    such as "diffed 3/8 (openai)" is for a diff that takes time."""
    labels = []
    for _ in range(2):
        reporter = _Labels()
        engine = Engine(
            Settings(cutoff=date(2025, 7, 31)),
            store=cache,
            llm_cache=cache,
            pypi=fake_pypi,
            reporter=reporter,
        )
        engine.scan(load_project(app), ModelTarget.cutoff_only(date(2025, 7, 31)))
        labels.append(reporter.labels)
    computed, cached = labels
    assert "toylib" in computed and "toylib" not in cached and cached.count(None) == len(cached)
    out = io.StringIO()
    rich = cli.RichReporter(Console(file=out, width=160))
    rich.stage("Diffing the API of 1 package released after its cutoff version", 1)
    rich.advance(label=None)
    rich.done()
    assert "diffed" not in out.getvalue()


def test_a_diff_another_griffe_made_is_made_again(app, cache, fake_pypi, monkeypatch) -> None:
    """griffe reads the sources, and it is a range dependency: after an upgrade, the diffs the
    old one made are not served again; with the same griffe, the cached diff is."""

    def diffed(griffe: str) -> bool:
        monkeypatch.setattr("since_cutoff.engine.griffe_version", lambda: griffe)
        reporter = _Labels()
        engine = Engine(
            Settings(cutoff=date(2025, 7, 31)),
            store=cache,
            llm_cache=cache,
            pypi=fake_pypi,
            reporter=reporter,
        )
        engine.scan(load_project(app), ModelTarget.cutoff_only(date(2025, 7, 31)))
        return "toylib" in reporter.labels

    assert diffed("2.3.0")
    assert not diffed("2.3.0")
    assert diffed("2.4.0")
