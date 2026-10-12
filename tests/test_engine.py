"""End-to-end pipeline tests with a fake PyPI and a scripted model (no network, no API keys)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from since_cutoff.apidiff import DIFF_SCHEMA, REMOVED, griffe_version
from since_cutoff.cache import DiskCache, stable_hash
from since_cutoff.engine import (
    CHANGED,
    KNOWN,
    NEW,
    PASS,
    STALE,
    UNCHANGED,
    Engine,
    PackageScan,
    Settings,
    star_imports_from_outside,
)
from since_cutoff.errors import PackageIndexError
from since_cutoff.mcp_server import Target, _render_project
from since_cutoff.notes import BLOCK_END, BLOCK_START, NOTE_DIFF, SCOPE_USED, TAG_DIFF
from since_cutoff.project import load_project
from since_cutoff.pypi import SOURCE_SCHEMA, SourceTree
from since_cutoff.report import headline, render_markdown, scan_lines, summary, to_json
from since_cutoff.sync import _dropped
from tests.conftest import FakePyPI, ScriptedModel, compiled_fastlib, write_tree

pytestmark = pytest.mark.pyright

TOYLIB_CODE = "from toylib import Client\nClient().send('hi')\n"


def make_project(tmp_path: Path, deps: str = '"toylib==2.0"', code: str = TOYLIB_CODE) -> Path:
    root = tmp_path / "app"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "app"\nversion = "0"\ndependencies = [{deps}]\n'
    )
    (root / "main.py").write_text(code)
    return root


def make_engine(cache: DiskCache, fake_pypi, model: ScriptedModel, **overrides) -> Engine:
    settings = Settings(
        model="scripted:scripted-1", cutoff=date(2025, 7, 31), python_version="3.12", jobs=2
    )
    for k, v in overrides.items():
        setattr(settings, k, v)
    return Engine(
        settings,
        store=cache,
        llm_cache=cache,
        pypi=fake_pypi,
        provider_factory=lambda spec: model,
    )


def test_scan_finds_the_changes_since_the_cutoff(tmp_path, cache, fake_pypi, scripted):
    engine = make_engine(cache, fake_pypi, scripted)
    project = load_project(make_project(tmp_path))
    scan = engine.scan(project, engine.resolve_target())
    pkg = scan.package("toylib")
    assert pkg.status == CHANGED
    assert (pkg.cutoff_version, pkg.locked) == ("1.0", "2.0")
    assert {c.name for c in pkg.changes} >= {"send", "fetch", "legacy_fetch", "Session", "close"}
    assert scripted.calls == []  # scanning never calls the model


def test_package_the_model_already_knows(tmp_path, cache, fake_pypi, scripted):
    engine = make_engine(cache, fake_pypi, scripted)
    project = load_project(make_project(tmp_path, '"toylib==1.0"'))
    scan = engine.scan(project, engine.resolve_target())
    assert scan.package("toylib").status == KNOWN
    assert engine.run(scan).probes == []


def test_package_newer_than_the_model(tmp_path, cache, fake_pypi, scripted):
    engine = make_engine(cache, fake_pypi, scripted, cutoff=date(2024, 1, 1))
    project = load_project(make_project(tmp_path))
    scan = engine.scan(project, engine.resolve_target())
    assert scan.package("toylib").status == NEW


def test_the_comparison_release_is_a_margin_before_the_cutoff(tmp_path, cache, toylib, scripted):
    """llama-index-core 0.13.0, which removed ``ReActAgent.from_tools``, was uploaded the day
    before claude-sonnet-4-5's cutoff, and the scan compared from it. A release 1 day before the
    cutoff is skipped with the default margin (30 days) and used with ``--cutoff-margin 0``; a
    package first released within the margin says so; and the two diffs are cached apart."""
    v1, v2 = toylib
    pypi = FakePyPI(
        cache,
        {
            "toylib": [("1.0", "2025-01-10"), ("1.9", "2025-07-30"), ("2.0", "2025-10-01")],
            "newlib": [("1.0", "2025-07-15")],
        },
        {
            ("toylib", "1.0"): v1,
            ("toylib", "1.9"): SourceTree("toylib", "1.9", v2.root, ("toylib",)),
            ("toylib", "2.0"): v2,
        },
    )
    project = load_project(make_project(tmp_path, '"toylib==2.0", "newlib==1.0"'))
    engine = make_engine(cache, pypi, scripted)
    target = engine.resolve_target()
    assert (target.margin, target.compare_date) == (30, date(2025, 7, 1))
    scan = engine.scan(project, target)
    default = scan.package("toylib")
    assert (default.status, default.cutoff_version) == (CHANGED, "1.0")
    assert "send" in {c.name for c in default.changes}
    newlib = scan.package("newlib")
    assert (newlib.status, newlib.first_released) == (NEW, "2025-07-15")
    assert newlib.reason == "first released within 30 days of the cutoff (2025-07-15)"

    engine = make_engine(cache, pypi, scripted, cutoff_margin=0)
    target = engine.resolve_target()
    assert target.compare_date == date(2025, 7, 31)
    scan = engine.scan(project, target)
    zero = scan.package("toylib")
    assert (zero.status, zero.cutoff_version, zero.changes) == (UNCHANGED, "1.9", [])
    assert scan.package("newlib").status == KNOWN
    # The diff cache is keyed per comparison release: neither scan was served the other's diff.
    assert Engine._diff_key(default) != Engine._diff_key(zero)


def test_a_dependency_first_released_within_the_margin_is_not_after_the_cutoff(
    tmp_path, cache, toylib, scripted
):
    """newlib 1.0 was uploaded 16 days before the cutoff, after the day the scan compares from:
    it counts as new, and the scan, the MCP tool and sync say it was first released within the
    margin, not after the cutoff (late 1.0 was); toylib 1.0, which the model knows, is the
    latest release 30 days before the cutoff, not at it."""
    v1, v2 = toylib
    pypi = FakePyPI(
        cache,
        {
            "toylib": [("1.0", "2025-01-10"), ("2.0", "2025-10-01")],
            "newlib": [("1.0", "2025-07-15")],
            "late": [("1.0", "2025-09-01")],
        },
        {("toylib", "1.0"): v1, ("toylib", "2.0"): v2},
    )
    code = TOYLIB_CODE + "import newlib\nimport late\n"
    deps = '"toylib==1.0", "newlib==1.0", "late==1.0"'
    project = load_project(make_project(tmp_path, deps, code))
    engine = make_engine(cache, pypi, scripted)
    target = engine.resolve_target()
    scan = engine.scan(project, target)
    newlib, late = scan.package("newlib"), scan.package("late")
    assert (newlib.status, late.status) == (NEW, NEW)

    text = "\n".join(t.plain for t in [*headline(scan, None), *scan_lines(scan, width=200)])
    assert (
        "Your code imports newlib 1.0 (first released 2025-07-15, within 30 days of the cutoff): "
        "its whole API is newer than the releases the scan compares from." in text
    )
    assert "Your code imports late 1.0 (first released 2025-09-01, after the cutoff)" in text
    assert "2 dependencies did not exist yet 30 days before the cutoff" in text

    mcp = _render_project(scan, Target(target.cutoff, "scripted-1"), [], 40)
    assert (
        "- Your code imports newlib 1.0, first released 2025-07-15: within 30 days before "
        "your reported training cutoff" in mcp
    )
    assert "- Your code imports late 1.0, first released 2025-09-01: released after" in mcp
    assert "## First released after the cutoff or within 30 days before it" in mcp
    assert "- newlib 1.0 (2025-07-15); first released within 30 days of the cutoff" in mcp

    assert _dropped(scan, newlib, "newlib", SCOPE_USED)[0] == (
        "first released 2025-07-15, within 30 days of the cutoff"
    )
    assert _dropped(scan, late, "late", SCOPE_USED)[0] == "first released after the cutoff"
    assert _dropped(scan, scan.package("toylib"), "toylib", SCOPE_USED)[0] == (
        "1.0 is the latest release 30 days before the cutoff"
    )
    assert target.released_within_margin(newlib)
    assert not target.released_within_margin(late)


def test_the_diff_cache_key_has_both_schemas_griffe_and_the_siblings():
    """Review of #83: the diff reads what the source trees record (the compiled modules), so
    its key has SOURCE_SCHEMA next to DIFF_SCHEMA, as _unread_key does; the distributions read
    next to a release (Engine._siblings) are in it too."""
    s = PackageScan("toylib", "2.0", "test", True, cutoff_version="1.0")
    assert Engine._diff_key(s) == stable_hash(
        "diff", DIFF_SCHEMA, SOURCE_SCHEMA, griffe_version(), "toylib", "1.0", "2.0"
    )
    assert Engine._diff_key(s, ("new:toylib-types==2.0",)) != Engine._diff_key(s)


# mcp 2.3.0's ``mcp/types/__init__.py`` is ``from mcp_types import *`` and mcp requires
# mcp-types 2.3.0, which renamed every field to snake_case: ``types.Tool(name=...,
# inputSchema=...)`` breaks, and a diff of mcp's own tree had no entry under ``mcp.types``.
MCPLIKE_OLD = {
    "mcplike/__init__.py": "from mcplike import types\n",
    "mcplike/types.py": "class Tool:\n    name: str\n    inputSchema: dict\n\nclass Gone:\n    pass\n",
}
MCPLIKE_NEW = {
    "mcplike/__init__.py": "from mcplike import types\n",
    "mcplike/types/__init__.py": "from mcplike_types import *\n",
}
MCPLIKE_TYPES = {
    "mcplike_types/__init__.py": "class Tool:\n    name: str\n    input_schema: dict\n"
}
# The code reads the field on an instance of the class: that is what selection matches.
MCPLIKE_CODE = "from mcplike.types import Tool\n\ntool = Tool()\nprint(tool.inputSchema)\n"
SIBLING_LIMIT_ERROR = "mcplike-types==2.0 is above the 80 MB download limit (--max-download-mb)"


def mcplike_pypi(root: Path, cache: DiskCache, *, sibling: bool = True) -> FakePyPI:
    """mcplike 1.0 and 2.0 in the shape of mcp 1.28 and 2.3: 2.0's ``mcplike/types/__init__.py``
    is ``from mcplike_types import *`` and it requires mcplike-types 2.0, whose ``Tool`` spells
    ``inputSchema`` ``input_schema``. Without ``sibling``, mcplike-types cannot be downloaded."""

    class Limited(FakePyPI):
        def source(self, name: str, version: str) -> SourceTree:
            if name == "mcplike-types":
                raise PackageIndexError(SIBLING_LIMIT_ERROR)
            return super().source(name, version)

    old = write_tree(root / "mcplike-1.0", MCPLIKE_OLD)
    new = write_tree(root / "mcplike-2.0", MCPLIKE_NEW)
    types = write_tree(root / "mcplike-types-2.0", MCPLIKE_TYPES)
    make = FakePyPI if sibling else Limited
    return make(
        cache,
        {
            "mcplike": [("1.0", "2025-01-10"), ("2.0", "2025-10-01")],
            "mcplike-types": [("2.0", "2025-10-01")],
        },
        {
            ("mcplike", "1.0"): SourceTree("mcplike", "1.0", old, ("mcplike",)),
            ("mcplike", "2.0"): SourceTree(
                "mcplike", "2.0", new, ("mcplike",), requires=("mcplike-types==2.0",)
            ),
            ("mcplike-types", "2.0"): SourceTree("mcplike-types", "2.0", types, ("mcplike_types",)),
        },
    )


def test_names_a_module_takes_from_another_distribution_are_compared(tmp_path, cache, scripted):
    """Audit item 6: the sibling distribution is read next to the release, at the version its
    requirement resolves to, and its names are compared under the paths that re-export them;
    the diff is cached under a key that names the sibling, and served again from it."""
    pypi = mcplike_pypi(tmp_path, cache)
    project = load_project(make_project(tmp_path, '"mcplike==2.0"', code=MCPLIKE_CODE))
    for attempt in ("diffed", "cached"):
        engine = make_engine(cache, pypi, scripted)
        scan = engine.scan(project, engine.resolve_target())
        pkg = scan.package("mcplike")
        assert {(c.kind, c.path) for c in pkg.changes} == {
            (REMOVED, "mcplike.types.Gone"),
            (REMOVED, "mcplike.types.Tool.inputSchema"),
        }, attempt
        assert (pkg.status, pkg.reexported, scan.warnings) == (CHANGED, {}, []), attempt
        used = [c.path for u in scan.used_apis() for c in u.changes]
        assert "mcplike.types.Tool.inputSchema" in used, attempt
    assert cache.get("diffs", Engine._diff_key(pkg, ("new:mcplike-types==2.0",))) is not None
    assert cache.get("diffs", Engine._diff_key(pkg)) is None


@pytest.mark.parametrize("package", ["toylib", "mcplike"])
def test_a_cached_diff_is_served_from_the_pinned_release_alone(
    tmp_path, cache, fake_pypi, scripted, package
):
    """The key of a cached diff names the distributions each release takes names from, which
    the first scan read from both releases' sources and kept in the store: the next scan serves
    the diff with only the pinned release's sources, as before there were siblings, so a cache
    that lost the release at the cutoff (or the sibling), or no network for them, is no
    failure."""
    if package == "mcplike":
        fake_pypi = mcplike_pypi(tmp_path, cache)
        project = load_project(make_project(tmp_path, '"mcplike==2.0"', code=MCPLIKE_CODE))
        pinned, siblings = ("mcplike", "2.0"), [{**MCPLIKE_SIBLING, "modules": ["mcplike.types"]}]
    else:
        project = load_project(make_project(tmp_path))
        pinned, siblings = ("toylib", "2.0"), []
    engine = make_engine(cache, fake_pypi, scripted)
    first = engine.scan(project, engine.resolve_target()).package(package)
    assert first.status == CHANGED and first.cutoff_version == "1.0"
    assert cache.get("diffs", Engine._siblings_key(package, "1.0")) == []
    assert cache.get("diffs", Engine._siblings_key(package, "2.0")) == siblings

    class Offline(FakePyPI):
        def source(self, name: str, version: str) -> SourceTree:
            if (name, version) != pinned:
                raise PackageIndexError(f"could not download {name}-{version}: network error")
            return super().source(name, version)

    offline = Offline(cache, fake_pypi._releases, fake_pypi._trees)
    engine = make_engine(cache, offline, scripted)
    scan = engine.scan(project, engine.resolve_target())
    again = scan.package(package)
    assert (again.status, again.reason, scan.warnings) == (CHANGED, None, [])
    assert [(c.kind, c.path) for c in again.changes] == [(c.kind, c.path) for c in first.changes]


MCPLIKE_SIBLING = {
    "name": "mcplike-types",
    "package": "mcplike_types",
    "modules": ["mcplike.types"],
    "requirement": "mcplike-types==2.0",
}


def test_star_imports_that_run_make_a_sibling(tmp_path):
    """A star import in a docstring, inside a function, or under ``if TYPE_CHECKING:`` defines
    no name when the module runs: it must not make a distribution a sibling to download. One
    under ``try`` or on the other side of a ``TYPE_CHECKING`` test does, and so does every one
    in a module that does not parse. The standard library and the tree's own modules never."""
    root = write_tree(
        tmp_path / "t",
        {
            "pkg/__init__.py": '"""Usage:\n\n    from numpy import *\n"""\nfrom attrs import *\n',
            "pkg/checked.py": (
                "import typing\nif typing.TYPE_CHECKING:\n    from requests import *\n"
                "else:\n    from yaml import *\nif not typing.TYPE_CHECKING:\n"
                "    from attrs import *\n"
            ),
            "pkg/inner.py": "def f():\n    from scipy import *\n\nclass C:\n    from h5py import *\n",
            "pkg/optional.py": "try:\n    from orjson import *\nexcept ImportError:\n    pass\n",
            "pkg/own.py": "from pkg.optional import *\nfrom os.path import *\n",
            "pkg/old.py": "from legacylib import *\nprint 'python 2'\n",
        },
    )
    tree = SourceTree("pkg", "1.0", root, ("pkg",))
    assert star_imports_from_outside(tree) == {
        "attrs": ["pkg", "pkg.checked"],
        "yaml": ["pkg.checked"],
        "orjson": ["pkg.optional"],
        "legacylib": ["pkg.old"],
    }


def test_a_sibling_distribution_that_cannot_be_downloaded_is_a_warning(tmp_path, cache, scripted):
    """Without mcplike-types, the names of ``mcplike.types`` cannot be compared (as before, when
    no change was reported there): the scan says so, in the report too, and does not cache the
    diff under the key with the sibling, so the next scan tries again."""
    pypi = mcplike_pypi(tmp_path, cache, sibling=False)
    project = load_project(make_project(tmp_path, '"mcplike==2.0"', code=MCPLIKE_CODE))
    why = f"mcplike-types 2.0, which could not be downloaded: {SIBLING_LIMIT_ERROR}"
    for attempt in ("diffed", "cached"):
        engine = make_engine(cache, pypi, scripted)
        scan = engine.scan(project, engine.resolve_target())
        pkg = scan.package("mcplike")
        assert (pkg.status, pkg.changes) == (UNCHANGED, []), attempt
        assert pkg.reexported == {"mcplike.types": why}, attempt
        assert scan.warnings == [
            f"mcplike 2.0: changes to mcplike.types are not reported (re-exported from {why})"
        ], attempt
        assert "re-exported from mcplike-types 2.0" in render_markdown(scan, None), attempt
    assert cache.get("diffs", Engine._diff_key(pkg)) is not None
    assert cache.get("diffs", Engine._diff_key(pkg, ("new:mcplike-types==2.0",))) is None


@pytest.mark.parametrize("module", ["fastlib.fast", "fastlib._core"], ids=["public", "private"])
def test_a_module_that_became_compiled_is_a_warning_not_a_removal(
    tmp_path, cache, scripted, module
):
    """Issue #52: fastlib 2.0 ships ``module`` as an extension module without a stub. It is
    not removed and its API is not compared, and the scan says so, whether the diff is new or
    cached; ``gone``, which ``fastlib`` no longer imports from it, is removed."""
    pypi = compiled_fastlib(tmp_path, cache, module)
    project = load_project(make_project(tmp_path, '"fastlib==2.0"'))
    for attempt in ("diffed", "cached"):
        engine = make_engine(cache, pypi, scripted)
        scan = engine.scan(project, engine.resolve_target())
        pkg = scan.package("fastlib")
        assert {(c.kind, c.path) for c in pkg.changes} == {
            (REMOVED, "fastlib.gone"),
            (REMOVED, "fastlib.slow"),
        }, attempt
        assert pkg.unread == [module], attempt
        assert scan.warnings == [
            f"fastlib 2.0: {module} is a compiled module without a .py source or a .pyi stub, "
            "unlike in 1.0; since-cutoff does not run code, so changes to it and to the names "
            "taken from it are not reported"
        ], attempt
        assert f"{module} is a compiled module" in render_markdown(scan, None)
        # Kept next to the diff, under a key that knows how the source trees were read.
        assert cache.get("diffs", Engine._unread_key(pkg)) == [module]


def test_full_run_measures_fixes_and_verifies(tmp_path, cache, fake_pypi):
    model = ScriptedModel(knows={"session"})
    engine = make_engine(cache, fake_pypi, model, max_probes=10, heldout=2, regression=2)
    project = load_project(make_project(tmp_path))
    scan = engine.scan(project, engine.resolve_target())
    run = engine.run(scan)

    outcomes = {a.change.name: a.outcome for a in run.probes}
    assert outcomes["send"] == STALE
    assert outcomes["legacy_fetch"] == STALE
    assert outcomes["fetch"] == STALE
    assert outcomes["Session"] == PASS  # the model "knows" this one
    assert outcomes["close"] == "deprecated"

    # Every failure gets a verified note, written by the model and grounded in its example.
    assert len(run.notes) == len(run.failing()) == 4
    assert all(n.verified and n.source == "model" for n in run.notes)
    assert (
        run.block and run.block.startswith(BLOCK_START) and run.block.rstrip().endswith(BLOCK_END)
    )
    assert "temperature" in run.block

    heldout = run.pairing("heldout")
    assert heldout.n == 8  # 4 failures x 2 held-out tasks, compared as pairs
    assert (heldout.before, heldout.after, heldout.fixed, heldout.broken) == (0, 8, 8, 0)
    assert (heldout.changes, heldout.changes_fixed, heldout.changes_broken) == (4, 4, 0)
    assert heldout.excluded == 0 and all(c.outcome == "fixed" for c in heldout.per_change)
    regression = run.pairing("regression")
    assert (regression.n, regression.after, regression.broken) == (1, 1, 0)

    s = summary(scan, run)
    assert s["stale_dependencies"] == ["toylib"]
    assert s["probes"]["stale"] == 3
    md = render_markdown(scan, run)
    assert "Held-out test of the notes" in md and "STALE" in md
    assert to_json(scan, run)["attempts"]
    # A run's dependency table has the probe columns after the counts.
    assert (
        "| package | you use | at cutoff | status | breaking | deprecated | probed | stale | wrong |"
        "\n|---|---|---|---|---:|---:|---:|---:|---:|\n"
        "| toylib | 2.0 (2025-10-01) | 1.0 | API changed, imported by your code | 5 | 1 | 5 | 3 |  |"
    ) in md


def test_runs_are_cached(tmp_path, cache, fake_pypi):
    project = load_project(make_project(tmp_path))
    first = ScriptedModel()
    engine = make_engine(cache, fake_pypi, first, max_probes=3, heldout=1, regression=0)
    engine.run(engine.scan(project, engine.resolve_target()))
    assert first.calls

    second = ScriptedModel()
    engine = make_engine(cache, fake_pypi, second, max_probes=3, heldout=1, regression=0)
    run = engine.run(engine.scan(project, engine.resolve_target()))
    assert second.calls == []  # tasks, answers and notes all come from the cache
    assert run.probes


def test_unverifiable_notes_fall_back_to_the_diff(tmp_path, cache, fake_pypi):
    class BadNotes(ScriptedModel):
        def complete(self, system, user):
            if system.startswith("You keep AGENTS.md"):
                from since_cutoff.providers.base import Completion

                return Completion(
                    '{"bullet": "`Client().send(q, temp=0)` works", "example": "from toylib import Client\\nClient().send(\'x\', temp=0)\\n"}'
                )
            return super().complete(system, user)

    engine = make_engine(cache, fake_pypi, BadNotes(), max_probes=2, heldout=1, regression=0)
    project = load_project(make_project(tmp_path))
    run = engine.run(engine.scan(project, engine.resolve_target()))
    # The note from the diff instead, tagged with what it rests on, never [type-checked].
    assert run.notes and all(n.source == NOTE_DIFF and not n.verified for n in run.notes)
    assert all(n.tag_list[0] == TAG_DIFF for n in run.notes)
    # The model's examples were checked and failed: results.json says so, as `verified` did.
    assert all(n.checks()["example_type_checks"] is False for n in run.notes)
    bullets = [line for line in (run.block or "").splitlines() if line.startswith("- ")]
    assert "temp=0" not in (run.block or "")
    assert bullets and not any(line.endswith("[type-checked]") for line in bullets)
    # toylib 1.0 said "Use fetch instead." of legacy_fetch, and 2.0 has it: named, tagged so.
    assert (
        "- `toylib.legacy_fetch` was removed; do not use it. Use `toylib.fetch` instead. "
        "[diff + library]"
    ) in bullets


def test_measure_only_mode_writes_no_notes(tmp_path, cache, fake_pypi, scripted):
    engine = make_engine(cache, fake_pypi, scripted, max_probes=3)
    project = load_project(make_project(tmp_path))
    run = engine.run(engine.scan(project, engine.resolve_target()), fix=False)
    assert run.probes and not run.notes and run.block is None and not run.heldout
