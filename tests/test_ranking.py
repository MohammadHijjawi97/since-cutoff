"""0.4.1: what ``sync --scope imported`` and ``scan --all`` put first, and how the diff and the
notes say that a subpackage moved whole and that a class or function came back under another
name.

Measured with 0.4.0 on real projects: for mcp 1.28.1 -> 2.2.0, the five notes ``--scope
imported`` wrote were parameter removals on ``ClientSession`` and members nobody imports, while
the changes that break most real code (the whole ``mcp.server.fastmcp`` package moved to
``mcp.server.mcpserver``, ``FastMCP`` renamed ``MCPServer``, ``streamablehttp_client`` removed with
the library naming its replacement) were reported as seven module moves, a plain removal and a
note further down the list. The real diff is a fixture here (tests/fixtures/diffs); the rules
are tested on toy trees, with no network.
"""

from __future__ import annotations

import ast
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from since_cutoff.apidiff import (
    DEPRECATED,
    DIFF_SCHEMA,
    MOVED,
    PARAM_REMOVED,
    REMOVED,
    APIChange,
    diff_sources,
)
from since_cutoff.engine import CHANGED, ModelTarget, PackageScan, ScanResult
from since_cutoff.notes import (
    EVIDENCE_LIBRARY,
    EVIDENCE_MOVE,
    IMPORTED_APIS,
    SCOPE_IMPORTED,
    SCOPE_USED,
    diff_note,
    library_replacement,
)
from since_cutoff.project import Project, scan_file
from since_cutoff.report import changes_text
from since_cutoff.selection import (
    TIER_IMPORT_PATH,
    TIER_MEMBER,
    TIER_REMOVED,
    TIER_REPLACEMENT,
    TIER_REST,
    collapse,
    dropped_reexport,
    imported_rank,
    is_value,
    package_moves,
    project_rank,
    tier,
    used_names,
    within_tier,
)
from tests.conftest import write_tree

FIXTURES = Path(__file__).parent / "fixtures" / "diffs"


def _pkg(tmp_path: Path, old: dict[str, str], new: dict[str, str]) -> list[APIChange]:
    a = write_tree(tmp_path / "old", old)
    b = write_tree(tmp_path / "new", new)
    return [APIChange.from_dict(c) for c in diff_sources("pkg", "1", a, "2", b, ["pkg"])]


def _by_name(changes: list[APIChange], name: str) -> APIChange:
    hits = [c for c in changes if c.name == name and not c.owner]
    assert len(hits) == 1, [c.describe() for c in changes]
    return hits[0]


def _scan(tmp_path: Path, package: PackageScan, code: str, file: str = "app/main.py") -> ScanResult:
    files = [scan_file(ast.parse(code), file)] if code else []
    project = Project(
        tmp_path, [], "uv.lock", imported_modules=set(package.import_names), files=files
    )
    return ScanResult(project, ModelTarget.cutoff_only(date(2025, 7, 31)), [package])


def _package(name: str, changes: list[APIChange], import_name: str | None = None) -> PackageScan:
    package = PackageScan(name, "2", "uv.lock", True, True, CHANGED, cutoff_version="1")
    package.import_names = [import_name or name]
    package.changes = changes
    return package


METHODS = "".join(
    f"    def {m}(self):\n        pass\n\n"
    for m in ("run", "add_tool", "tool", "resource", "prompt", "list_tools")
)


# ------------------------------------------------------------ 2. renames in the diff
def test_a_class_that_came_back_under_another_name_is_a_checked_move(tmp_path) -> None:
    """mcp 2: ``FastMCP`` is gone and ``MCPServer``, new, has its members."""
    old = {"pkg/__init__.py": "", "pkg/server.py": f"class FastThing:\n{METHODS}"}
    new = {
        "pkg/__init__.py": "",
        "pkg/server.py": f"class Thing:\n{METHODS}    def remove_tool(self, name):\n        pass\n",
    }
    changes = _pkg(tmp_path, old, new)
    moved = _by_name(changes, "FastThing")
    assert (moved.kind, moved.moved_to) == (MOVED, "pkg.server.Thing")
    assert moved.move_evidence == {"compared": "class", "kept": 6, "of": 6, "renamed": True}
    assert moved.renamed_to == "Thing"
    assert not [c for c in changes if c.kind == REMOVED]
    assert moved.describe() == (
        "`pkg.server.FastThing` moved to `pkg.server.Thing` (`FastThing` is now `Thing`) (pkg 2)"
    )
    note = diff_note([moved])
    assert note.line == (
        "`FastThing` is now `Thing`: `pkg.server.FastThing` moved to `pkg.server.Thing`; import "
        "it with `from pkg.server import Thing`. [diff + move checked]"
    )
    [r] = note.replacements
    assert (r.evidence, r.text, r.replaces) == (EVIDENCE_MOVE, "pkg.server.Thing", moved.path)
    assert r.source == (
        "pkg 2 API diff: the object at `pkg.server.Thing` keeps 6 of 6 public names of the old "
        "one, and no `Thing` existed in 1"
    )
    assert changes_text([moved], code=True) == (
        True,
        "moved to `pkg.server.Thing` (`FastThing` is now `Thing`)",
    )
    # A diff made before DIFF_SCHEMA 15 has no "renamed" in its evidence: a plain move.
    plain = APIChange.from_dict({**moved.to_dict(), "move_evidence": {"compared": "class"}})
    assert plain.renamed_to is None
    assert changes_text([plain], code=True) == (True, "moved to `pkg.server.Thing`")


def test_renames_are_conservative(tmp_path) -> None:
    """Never a tiny class, a type of fields alone under an unrelated name, a tie between two
    candidates, an object that existed before, or when the library's own text already names
    the replacement; yes to a name that differs in case alone, to a type of fields under a
    related name, and to a function whose parameters and name carry over."""
    fields = "    host: str\n    port: int\n    user: str\n    retries: int\n    debug: bool\n"
    other_fields = (
        "    path: str\n    mode: int\n    level: str\n    verbose: bool\n    color: str\n"
    )
    five = "".join(f"    def {m}(self):\n        pass\n\n" for m in "pqrst")
    old = {
        "pkg/__init__.py": "",
        "pkg/a.py": (
            "class Small:\n    def one(self):\n        pass\n\n    def two(self):\n        pass\n\n\n"
            f"class Options:\n{fields}\n\n"
            f"class Config:\n{other_fields}\n\n"
            "class Big:\n"
            + "".join(f"    def {m}(self):\n        pass\n\n" for m in "abcde")
            + "\n"
            f"class Gone:\n{five}\n\n"
            "class McpError(Exception):\n    error: int\n\n"
            "    def __init__(self, error):\n        self.error = error\n\n\n"
            "def old_fn(a, b, c):\n    '''Deprecated. Use `new_fn` instead.'''\n\n\n"
            "def fetch_thing(a, b, c):\n    pass\n"
        ),
        "pkg/b.py": f"class Other:\n{five}",
    }
    new = {
        "pkg/__init__.py": "",
        "pkg/a.py": (
            "class Tiny:\n    def one(self):\n        pass\n\n    def two(self):\n        pass\n\n\n"
            f"class Settings:\n{fields}\n\n"
            f"class ServerConfig:\n{other_fields}\n\n"
            "class Big1:\n"
            + "".join(f"    def {m}(self):\n        pass\n\n" for m in "abcde")
            + "\n"
            "class Big2:\n"
            + "".join(f"    def {m}(self):\n        pass\n\n" for m in "abcde")
            + "\n"
            f"class Other:\n{five}\n\n"
            "class MCPError(Exception):\n    error: int\n\n"
            "    def __init__(self, code, message):\n        self.error = code\n\n\n"
            "def new_fn(a, b, c):\n    pass\n\n\ndef fetch_things(a, b, c):\n    pass\n"
        ),
        "pkg/b.py": "",
    }
    changes = _pkg(tmp_path, old, new)
    kinds = {c.name: (c.kind, c.moved_to) for c in changes if not c.owner}
    assert kinds["Small"] == (REMOVED, None)  # 2 members: too few to tell
    assert kinds["Options"] == (REMOVED, None)  # fields alone, and Settings is another name
    assert kinds["Config"] == (MOVED, "pkg.a.ServerConfig")  # fields alone, but the name says
    assert kinds["Big"] == (REMOVED, None)  # Big1 and Big2 both qualify: no way to tell
    assert kinds["Gone"] == (REMOVED, None)  # Other has its members, but existed before
    assert kinds["Other"] == (MOVED, "pkg.a.Other")  # a move under its own name, as before
    assert kinds["McpError"] == (MOVED, "pkg.a.MCPError")
    assert kinds["old_fn"] == (REMOVED, None)  # the library's text names the replacement
    assert kinds["fetch_thing"] == (MOVED, "pkg.a.fetch_things")
    assert _by_name(changes, "McpError").move_evidence == {
        "compared": "class",
        "kept": 1,
        "of": 1,
        "renamed": True,
    }
    assert _by_name(changes, "fetch_thing").move_evidence == {
        "compared": "function",
        "kept": 3,
        "of": 3,
        "renamed": True,
    }
    assert _by_name(changes, "Other").move_evidence == {"compared": "class", "kept": 5, "of": 5}
    old_fn = _by_name(changes, "old_fn")
    assert old_fn.library_names == ["pkg.a.new_fn"]
    assert library_replacement(old_fn) is not None
    assert library_replacement(old_fn).evidence == EVIDENCE_LIBRARY  # type: ignore[union-attr]
    assert library_replacement(_by_name(changes, "Small")) is None


HANDLER = ("handle", "setup", "teardown", "validate", "render")


def _class(name: str, methods: tuple[str, ...], base: str = "") -> str:
    bases = f"({base})" if base else ""
    return f"class {name}{bases}:\n" + "".join(
        f"    def {m}(self):\n        pass\n\n" for m in methods
    )


def test_a_sibling_subclass_is_not_the_rename(tmp_path) -> None:
    """One module holding subclasses of a shared base, one dropped and one added in the same
    release (an SDK's handlers, resources, tools): ``FooHandler`` overrode five of
    ``BaseHandler``'s methods, and the new ``WebhookReceiver`` inherits them. With 0.4.1's
    first cut that was "`FooHandler` is now `WebhookReceiver`" (5 of 5, checked). What a class
    inherits from a base the old one also had says nothing about it; what it defines itself
    does, and three generic names (``run``, ``close``, ``start``) are too few for an unrelated
    name (DIFF_SCHEMA 16)."""
    base = _class("BaseHandler", HANDLER)
    imports = "from pkg.base import BaseHandler\n\n"
    old = {
        "pkg/__init__.py": "",
        "pkg/base.py": base,
        "pkg/handlers.py": imports + _class("FooHandler", HANDLER, "BaseHandler"),
        "pkg/dispatch.py": imports + _class("Sender", (*HANDLER, "send"), "BaseHandler"),
        "pkg/io.py": _class("Task", ("run", "close", "start")),
    }
    new = {
        "pkg/__init__.py": "",
        "pkg/base.py": base,
        "pkg/handlers.py": imports + _class("WebhookReceiver", ("parse",), "BaseHandler"),
        "pkg/dispatch.py": imports + _class("Dispatcher", (*HANDLER, "send"), "BaseHandler"),
        "pkg/io.py": _class("Worker", ("run", "close", "start", "join")),
    }
    changes = _pkg(tmp_path, old, new)
    kinds = {c.name: (c.kind, c.moved_to) for c in changes if not c.owner}
    assert kinds["FooHandler"] == (REMOVED, None)
    assert kinds["Task"] == (REMOVED, None)
    # A class that defines the old one's members itself is its rename, whatever its bases.
    assert kinds["Sender"] == (MOVED, "pkg.dispatch.Dispatcher")
    assert _by_name(changes, "Sender").move_evidence == {
        "compared": "class",
        "kept": 6,
        "of": 6,
        "renamed": True,
    }
    for c in changes:
        if c.kind == MOVED:
            assert "WebhookReceiver" not in (c.moved_to or "") and "Worker" not in (
                c.moved_to or ""
            )


# ----------------------------------------------------- 1. a package moved whole
# As mcp 1: ``mcp/server/__init__.py`` re-exported ``FastMCP`` from the ``fastmcp`` package (mcp
# 2's re-exports ``MCPServer`` from ``mcpserver``). griffe reports a removed module without its
# members, so a class of a moved module is a change of its own only where a surviving module
# re-exported it.
FAST = {
    "pkg/__init__.py": "from pkg.fast import FastThing\n\n__all__ = ['FastThing']\n",
    "pkg/fast/__init__.py": (
        "from pkg.fast.server import Context, FastThing\n\n__all__ = ['FastThing', 'Context']\n"
    ),
    "pkg/fast/server.py": (
        f"class FastThing:\n{METHODS}\n"
        "class Context:\n    def info(self):\n        pass\n\n    def debug(self):\n"
        "        pass\n\n    def error(self):\n        pass\n"
    ),
    "pkg/fast/tools.py": "def add(a, b):\n    pass\n\n\ndef remove(name):\n    pass\n",
    "pkg/fast/prompts.py": "def render(p):\n    pass\n",
    "pkg/fast/resources.py": "def read(uri):\n    pass\n",
}
NEWER = {
    "pkg/__init__.py": "from pkg.newer import Thing\n\n__all__ = ['Thing']\n",
    # As mcp 2 left ``mcp/server/fastmcp.py``: a module with no API, so that importing the old
    # package says where it went. griffe then reports each module of it on its own.
    "pkg/fast/__init__.py": '"""Moved to pkg.newer."""\n',
    "pkg/newer/__init__.py": (
        "from pkg.newer.server import Context, Thing\n\n__all__ = ['Thing', 'Context']\n"
    ),
    "pkg/newer/server.py": FAST["pkg/fast/server.py"]
    .replace("FastThing", "Thing")
    .replace(
        "\nclass Context", "    def remove_tool(self, name):\n        pass\n\n\nclass Context"
    ),
    "pkg/newer/tools.py": FAST["pkg/fast/tools.py"],
    "pkg/newer/prompts.py": FAST["pkg/fast/prompts.py"],
    "pkg/newer/resources.py": FAST["pkg/fast/resources.py"],
}


def test_a_subpackage_whose_modules_moved_is_one_change_in_the_notes(tmp_path) -> None:
    changes = _pkg(tmp_path, FAST, NEWER)
    # The diff reports each module's move (with evidence), and the rename in the moved module.
    module_moves = {
        c.path: c.moved_to
        for c in changes
        if c.kind == MOVED and (c.old_signature or "").startswith("module ")
    }
    assert module_moves == {
        f"pkg.fast.{m}": f"pkg.newer.{m}" for m in ("server", "tools", "prompts", "resources")
    }
    # Each module's move records that the old package has nothing public left (the shim).
    assert {
        (c.move_evidence or {}).get("left_behind") for c in changes if c.path in module_moves
    } == {0}
    renamed = next(c for c in changes if c.name == "FastThing" and c.kind == MOVED)
    assert (renamed.path, renamed.moved_to, renamed.renamed_to) == (
        "pkg.FastThing",
        "pkg.Thing",
        "Thing",
    )
    assert set(renamed.import_paths or []) == {
        "pkg.FastThing",
        "pkg.fast.FastThing",
        "pkg.fast.server.FastThing",
    }
    # Under the shim it is only gone (no candidates there): a removal the move collapses over.
    assert [
        c.kind for c in changes if c.name == "FastThing" and c.path == "pkg.fast.FastThing"
    ] == [REMOVED]
    # collapse shows the package's move instead, with the individual moves behind it.
    distinct = collapse(changes)
    [whole] = [c for c in distinct if c.is_package_move]
    assert (whole.path, whole.moved_to, whole.kind) == ("pkg.fast", "pkg.newer", MOVED)
    assert whole.move_evidence == {"compared": "package", "kept": 5, "of": 5, "modules": 4}
    assert not [
        c
        for c in distinct
        if c.path.startswith("pkg.fast.")
        and c.kind == MOVED
        and c is not whole
        and c.name != "FastThing"
    ]
    assert "pkg.fast" in (whole.import_paths or []) and "pkg.fast.tools" in (
        whole.import_paths or []
    )
    # The subpackage's own removal is part of it, not a second entry.
    assert not [c for c in distinct if c.path == "pkg.fast" and c is not whole]
    assert (
        whole.describe()
        == "the package `pkg.fast` moved to `pkg.newer` (5 modules and classes) (pkg 2)"
    )
    note = diff_note([whole])
    assert (
        note.line
        == "The package `pkg.fast` moved to `pkg.newer`; import from there. [diff + move checked]"
    )
    [r] = note.replacements
    assert r.evidence == EVIDENCE_MOVE and r.text == "pkg.newer"
    assert r.source == (
        "pkg 2 API diff: the 4 modules and 1 other objects of `pkg.fast` are at `pkg.newer`, "
        "each keeping the old one's public names or parameters"
    )
    # The rename collapses over the plain removal of the same name under the gone package.
    [thing] = [c for c in distinct if c.name == "FastThing"]
    assert (thing.kind, thing.path, thing.renamed_to) == (MOVED, "pkg.FastThing", "Thing")
    assert thing.occurrences == 2 and thing.also == ["pkg.fast.FastThing"]
    # Code that imports from the old package uses both: the package's move and the rename.
    package = _package("pkg", changes)
    scan = _scan(tmp_path, package, "from pkg.fast import FastThing\n\napp = FastThing()\n")
    used = {c.path: used_names(c, scan.uses(package)) for c in scan.used_changes(package)}
    assert used == {"pkg.fast": ("fast",), thing.path: ("FastThing",)}
    lines = sorted(n.line for n in scan.scope_notes(SCOPE_USED))
    assert lines == [
        "The package `pkg.fast` moved to `pkg.newer`; import from there. [diff + move checked]",
        "`FastThing` is now `Thing`: `pkg.FastThing` moved to `pkg.Thing`; import it with "
        "`from pkg import Thing`. [diff + move checked]",
    ]
    assert changes_text([whole], code=True) == (True, "moved to `pkg.newer`, the whole package")
    # results.json keeps every move: the package's is the notes' and the scan's view of them.
    assert len([c for c in package.changes if c.kind == MOVED]) >= 6
    assert len([c for c in package.distinct if c.kind == MOVED]) == 2


def _move(
    path: str,
    to: str,
    *,
    module: bool = False,
    package: str = "pkg",
    left_behind: int | None = 0,
) -> APIChange:
    evidence: dict[str, Any] = {"compared": "module" if module else "class", "kept": 2, "of": 2}
    if module and left_behind is not None:
        evidence["left_behind"] = left_behind
    return APIChange(
        package,
        "1",
        "2",
        MOVED,
        path,
        path.rsplit(".", 1)[-1],
        moved_to=to,
        old_signature=f"module {path}" if module else f"class {path.rsplit('.', 1)[-1]}",
        move_evidence=evidence,
        import_paths=[path],
    )


def test_when_a_subpackage_counts_as_moved_whole() -> None:
    three = [_move(f"pkg.a.{m}", f"pkg.b.{m}", module=True) for m in ("x", "y", "z")]
    [(whole, members)] = package_moves(three)
    assert (whole.path, whole.moved_to, len(members)) == ("pkg.a", "pkg.b", 3)
    # Only when the diff saw nothing public left in the old package: a module that stayed
    # (counted on each move as ``left_behind``) keeps ``from pkg.a import w`` working, and a
    # diff made before DIFF_SCHEMA 16 did not count, so neither is a package's move.
    stayed = [_move(f"pkg.a.{m}", f"pkg.b.{m}", module=True, left_behind=1) for m in "xyz"]
    assert package_moves(stayed) == []
    assert package_moves([*three[:2], stayed[2]]) == []
    older = [_move(f"pkg.a.{m}", f"pkg.b.{m}", module=True, left_behind=None) for m in "xyz"]
    assert package_moves(older) == []
    # Two modules: only when nothing else of the subpackage went elsewhere or was removed.
    two = three[:2]
    assert len(package_moves(two)) == 1
    gone = APIChange("pkg", "1", "2", REMOVED, "pkg.a.w", "w", old_signature="module pkg.a.w")
    assert package_moves([*two, gone]) == []
    elsewhere = _move("pkg.a.w", "pkg.c.w", module=True)
    assert package_moves([*two, elsewhere]) == []
    # The subpackage's own removal (or move), when the diff reports one, is part of the group.
    itself = APIChange("pkg", "1", "2", REMOVED, "pkg.a", "a", old_signature="module pkg.a")
    [(whole, members)] = package_moves([*three, itself])
    assert itself in members and (whole.move_evidence or {})["of"] == 3
    assert "pkg.a" in (whole.import_paths or [])
    # Classes moving along count as members, not as modules; classes alone are no package move.
    classes = [_move(f"pkg.a.{n}", f"pkg.b.{n}") for n in ("One", "Two", "Three")]
    assert package_moves(classes) == []
    [(whole, members)] = package_moves([*three, *classes])
    assert whole.move_evidence == {"compared": "package", "kept": 6, "of": 6, "modules": 3}
    # A module renamed on the way (a different last name) is not part of the group.
    renamed = _move("pkg.a.q", "pkg.b.r", module=True)
    [(whole, members)] = package_moves([*three, renamed])
    assert renamed not in members
    # Members of a class, and the top-level package itself, never make one.
    assert package_moves([_move("pkg.x", "other.x", module=True)]) == []


def test_a_subpackage_with_a_module_left_behind_is_not_moved_whole(tmp_path) -> None:
    """``pkg.a`` has x, y, z and w; x, y and z move to ``pkg.b`` and w stays. The diff sees
    only the three moves (a module that stays makes no change), so 0.4.1's first cut made them
    "The package `pkg.a` moved to `pkg.b`" and gave that note, in the used scope, to code that
    imports ``pkg.a.w`` (still valid). Now each move records what the package has left, the
    three are listed each on its own, and that code uses no change."""
    module = "def f(a):\n    pass\n"
    old = {"pkg/__init__.py": "", "pkg/a/__init__.py": ""}
    old |= {f"pkg/a/{m}.py": module for m in "xyzw"}
    new = {"pkg/__init__.py": "", "pkg/a/__init__.py": "", "pkg/a/w.py": module}
    new |= {"pkg/b/__init__.py": ""} | {f"pkg/b/{m}.py": module for m in "xyz"}
    changes = _pkg(tmp_path, old, new)
    moves = {c.path: c for c in changes if c.kind == MOVED}
    assert set(moves) == {"pkg.a.x", "pkg.a.y", "pkg.a.z"}
    assert {str(c.move_evidence) for c in moves.values()} == {
        "{'compared': 'module', 'kept': 1, 'of': 1, 'left_behind': 1}"
    }
    assert package_moves(changes) == []
    distinct = collapse(changes)
    assert not [c for c in distinct if c.is_package_move]
    assert sorted(c.path for c in distinct) == ["pkg.a.x", "pkg.a.y", "pkg.a.z"]
    package = _package("pkg", changes)
    for code in ("from pkg.a import w\nw.f(1)\n", "from pkg.a.w import f\n", "import pkg.a.w\n"):
        scan = _scan(tmp_path, package, code)
        assert scan.used_changes(package) == [], code
        assert scan.scope_notes(SCOPE_USED) == [], code
    scan = _scan(tmp_path, package, "from pkg.a.x import f\n")
    assert [c.path for c in scan.used_changes(package)] == ["pkg.a.x"]
    assert [n.line for n in scan.scope_notes(SCOPE_USED)] == [
        "`pkg.a.x` moved to `pkg.b.x`: import it with `from pkg.b import x`. [diff + move checked]"
    ]


# ------------------------------------------- a module-level name bound to a method
def test_a_name_bound_to_a_method_and_the_method_are_one_api(tmp_path) -> None:
    """huggingface_hub 1's ``hf_api.py`` ends with ``api = HfApi()`` and ``duplicate_space =
    api.duplicate_space``; 2.0 removed the method (its text: "Use `duplicate_repo` instead")
    and the name with it. With 0.4.1's first cut, ``--scope imported`` wrote "`huggingface_hub
    .duplicate_space` was removed ... found no replacement" (tier 0) and cut the method's note,
    which named the replacement, at the budget. The diff now says which method the name was
    bound to (``alias_of``) and gives it the method's text; the two are one API in the notes
    and the scan, and code using either form uses it."""
    dup = (
        "    def duplicate_space(self, a, b):\n"
        "        '''Dup.\n\n        Deprecated. Use `duplicate_repo` instead.\n        '''\n\n"
    )
    old = {
        "pkg/__init__.py": (
            "from pkg.hf import HfApi, duplicate_space, other, upload\n\n"
            "__all__ = ['HfApi', 'duplicate_space', 'other', 'upload']\n"
        ),
        "pkg/hf.py": (
            f"class HfApi:\n{dup}"
            "    def duplicate_repo(self, a, b):\n        pass\n\n"
            "    def other(self, a):\n        pass\n\n"
            "    def upload(self, a):\n        pass\n\n\n"
            "api = HfApi()\nduplicate_space = api.duplicate_space\nother = api.other\n"
            "upload = api.upload\n"
        ),
    }
    new = {
        "pkg/__init__.py": "from pkg.hf import HfApi\n\n__all__ = ['HfApi']\n",
        "pkg/hf.py": (
            "class HfApi:\n    def duplicate_repo(self, a, b):\n        pass\n\n"
            "    def other(self, a):\n        pass\n\n\napi = HfApi()\n"
        ),
    }
    changes = _pkg(tmp_path, old, new)
    by_path = {c.path: c for c in changes}
    for path in ("pkg.duplicate_space", "pkg.hf.duplicate_space"):
        c = by_path[path]
        assert (c.kind, c.owner, c.alias_of) == (REMOVED, None, "pkg.hf.HfApi.duplicate_space")
        assert (c.hint or "").startswith("Deprecated. Use `duplicate_repo` instead.")
        assert c.library_names == ["pkg.hf.HfApi.duplicate_repo"]
        assert c.hint_file == "pkg/hf.py"
    # A name bound to a method the library says nothing about: the method, no text.
    assert (by_path["pkg.other"].alias_of, by_path["pkg.other"].hint) == (
        "pkg.hf.HfApi.other",
        None,
    )
    assert by_path["pkg.hf.HfApi.duplicate_space"].alias_of is None
    distinct = collapse(changes)
    [entry] = [c for c in distinct if c.name == "duplicate_space"]
    assert (entry.path, entry.kind, entry.occurrences) == ("pkg.duplicate_space", REMOVED, 3)
    assert entry.also == ["pkg.hf.duplicate_space", "pkg.hf.HfApi.duplicate_space"]
    assert "pkg.HfApi.duplicate_space" in (entry.import_paths or [])
    assert (tier(entry), within_tier(entry), is_value(entry)) == (TIER_IMPORT_PATH, 0, False)
    note = diff_note([entry])
    assert note.line == (
        "`pkg.duplicate_space` was removed; do not use it. Use `HfApi.duplicate_repo` instead. "
        "[diff + library]"
    )
    assert library_replacement(entry) is not None
    assert library_replacement(entry).evidence == EVIDENCE_LIBRARY  # type: ignore[union-attr]
    # The method still there (``other``) leaves the name's removal on its own; a method removed
    # without text (``upload``) is folded the same way, so that the two are not two notes.
    assert [c.occurrences for c in distinct if c.name == "other"] == [2]
    [upload] = [c for c in distinct if c.name == "upload"]
    assert upload.occurrences == 3 and upload.also == ["pkg.hf.upload", "pkg.hf.HfApi.upload"]
    assert sorted(c.name for c in distinct) == ["duplicate_space", "other", "upload"]
    package = _package("pkg", changes)
    for code in (
        "from pkg import duplicate_space\n\nduplicate_space(1, 2)\n",
        "from pkg import HfApi\n\napi = HfApi()\napi.duplicate_space(1, 2)\n",
    ):
        scan = _scan(tmp_path, package, code)
        assert [c.path for c in scan.used_changes(package)] == ["pkg.duplicate_space"], code
        assert used_names(entry, scan.uses(package)) == ("duplicate_space",)
        assert [n.line for n in scan.scope_notes(SCOPE_USED)] == [note.line]
    scan = _scan(tmp_path, package, "import pkg\n")
    notes = scan.scope_notes(SCOPE_IMPORTED)
    assert [n.api for n in notes] == ["pkg.duplicate_space", "pkg.upload", "pkg.other"]
    assert notes[0].line == note.line


# ----------------------------------------------------------- 3. the ranking
def _change(kind: str, path: str, **kw: Any) -> APIChange:
    owner = kw.pop("owner", None)
    return APIChange("sdk", "1", "2", kind, path, path.rsplit(".", 1)[-1], owner, **kw)


def test_tiers_put_import_paths_and_known_replacements_before_parameters() -> None:
    top_removed = _change(REMOVED, "sdk.Thing", import_paths=["sdk.Thing", "sdk.things.Thing"])
    module_moved = _move("sdk.utils.old", "sdk.tools.old", module=True, package="sdk")
    replaced = _change(
        REMOVED,
        "sdk.utils.old_fn",
        hint="Deprecated. Use `new_fn` instead.",
        library_names=["sdk.utils.new_fn"],
        import_paths=["sdk.utils.old_fn"],
    )
    class_moved = _move("sdk.utils.Session", "sdk.sessions.Session", package="sdk")
    removed = _change(REMOVED, "sdk.utils.Helper", import_paths=["sdk.utils.Helper"])
    removed_module = _change(REMOVED, "sdk.utils.legacy", old_signature="module sdk.utils.legacy")
    param = _change(
        PARAM_REMOVED,
        "sdk.Client.__init__",
        owner="Client",
        parameter="retries",
        old_signature="Client(self, retries: int = 3)",
    )
    member = _change(REMOVED, "sdk.Client.ping", owner="Client")
    deprecated = _change(DEPRECATED, "sdk.utils.x", deprecation="deprecated")
    assert tier(top_removed) == TIER_IMPORT_PATH
    assert tier(module_moved) == TIER_IMPORT_PATH
    assert tier(replaced) == TIER_REPLACEMENT
    assert tier(class_moved) == TIER_REPLACEMENT
    assert tier(removed) == TIER_REMOVED
    assert tier(removed_module) == TIER_REMOVED
    assert tier(param) == TIER_MEMBER
    assert tier(member) == TIER_MEMBER
    assert tier(deprecated) == TIER_REST
    # A deprecation with a replacement the library names is a known replacement too.
    advised = _change(
        DEPRECATED,
        "sdk.Client.close",
        owner="Client",
        deprecation="Use Client.shutdown() instead.",
        library_names=["sdk.Client.shutdown"],
    )
    assert tier(advised) == TIER_REPLACEMENT
    changes = [param, member, deprecated, removed, class_moved, replaced, module_moved, top_removed]
    files = ()
    ordered = sorted(changes, key=lambda c: imported_rank(c, files))
    assert [c.path for c in ordered][:3] == ["sdk.Thing", "sdk.utils.old", "sdk.utils.old_fn"]
    assert [tier(c) for c in ordered] == sorted(tier(c) for c in changes)
    # The used scope keeps its ranking: what the code uses, then hard breaks, then the score.
    assert sorted(changes, key=lambda c: project_rank(c, files)) != ordered


def test_dropped_reexports_and_values_come_after_classes_and_functions() -> None:
    """langgraph 1 stopped re-exporting its ``pregel.main`` helpers from ``langgraph.pregel``:
    the diff finds each at ``langgraph.pregel.main.x``, a checked move, and 0.4.1's first cut
    listed eleven of them (tier 1, "a known replacement") before ``langgraph.pregel
    .InvalidUpdateError``, removed outright. anthropic 1.8 dropped the top-level ``AI_PROMPT``,
    ``HUMAN_PROMPT``, ``ProxiesTypes`` and ``Transport``: constants and type aliases, which
    filled tier 0 ahead of the modules that moved out of ``anthropic.types.beta``."""
    files = ()
    reexport = _move("sdk.core.helper", "sdk.core.impl.helper", package="sdk")
    promoted = _move("sdk.types.beta.Thing", "sdk.types.Thing", package="sdk")
    removed = _change(REMOVED, "sdk.core.Error", old_signature="class Error", import_paths=[])
    assert dropped_reexport(reexport) and tier(reexport) == TIER_REMOVED
    assert not dropped_reexport(promoted) and tier(promoted) == TIER_REPLACEMENT
    assert [c.path for c in sorted([reexport, removed], key=lambda c: imported_rank(c, files))] == [
        "sdk.core.Error",
        "sdk.core.helper",
    ]
    constant = _change(REMOVED, "sdk.HUMAN_PROMPT", old_signature="HUMAN_PROMPT")
    typed = _change(REMOVED, "sdk.Transport", old_signature="Transport: TypeAlias")
    module_moved = _move("sdk.utils.old", "sdk.tools.old", module=True, package="sdk")
    top_class = _change(REMOVED, "sdk.Thing", old_signature="class Thing(self, x)")
    assert is_value(constant) and is_value(typed)
    assert not is_value(top_class) and not is_value(module_moved) and not is_value(reexport)
    assert {tier(constant), tier(typed), tier(top_class), tier(module_moved)} == {TIER_IMPORT_PATH}
    assert (within_tier(constant), within_tier(reexport), within_tier(top_class)) == (2, 1, 0)
    ordered = sorted(
        [constant, typed, module_moved, top_class, removed, reexport],
        key=lambda c: imported_rank(c, files),
    )
    assert [c.path for c in ordered] == [
        "sdk.Thing",
        "sdk.utils.old",
        "sdk.HUMAN_PROMPT",
        "sdk.Transport",
        "sdk.core.Error",
        "sdk.core.helper",
    ]
    # A package's move to a sibling, a module moved deeper (``import sdk.a.x`` no longer
    # works: a move, not a re-export dropped) and a name bound to a method are neither.
    whole = _move("sdk.fast", "sdk.newer", module=True, package="sdk")
    whole.move_evidence = {"compared": "package", "kept": 3, "of": 3, "modules": 3}
    assert not dropped_reexport(whole) and within_tier(whole) == 0
    deeper = _move("sdk.a.x", "sdk.a.sub.x", module=True, package="sdk")
    assert not dropped_reexport(deeper) and (tier(deeper), within_tier(deeper)) == (0, 0)
    bound = _change(REMOVED, "sdk.upload", old_signature="upload", alias_of="sdk.api.Api.upload")
    assert not is_value(bound) and within_tier(bound) == 0


def test_collapse_prefers_the_move_even_under_the_longer_path() -> None:
    """``pkg.X`` is only gone; under ``pkg.sub`` the diff found it renamed ``Y``. The move is
    the entry (it says where the object went), in the removal's place, with the removal's
    path among its other paths, whichever comes first."""
    removed = APIChange("pkg", "1", "2", REMOVED, "pkg.X", "X", old_signature="class X")
    removed.import_paths = ["pkg.X"]
    moved = APIChange(
        "pkg",
        "1",
        "2",
        MOVED,
        "pkg.sub.X",
        "X",
        moved_to="pkg.sub.Y",
        old_signature="class X",
        move_evidence={"compared": "class", "kept": 5, "of": 5, "renamed": True},
        import_paths=["pkg.sub.X"],
    )
    for changes in ([removed, moved], [moved, removed]):
        [entry] = collapse(changes)
        assert (entry.kind, entry.path, entry.moved_to) == (MOVED, "pkg.sub.X", "pkg.sub.Y")
        assert (entry.occurrences, entry.also) == (2, ["pkg.X"])
        assert entry.import_paths == ["pkg.X", "pkg.sub.X"]
        assert entry.renamed_to == "Y"


def test_scope_imported_writes_the_top_tiers_and_per_package_counts_a_package_move_once(
    tmp_path,
) -> None:
    changes = _pkg(tmp_path, FAST, NEWER)
    changes += [
        APIChange(
            "pkg",
            "1",
            "2",
            PARAM_REMOVED,
            "pkg.Client.__init__",
            "__init__",
            "Client",
            "retries",
            old_signature="Client(self, retries: int = 3)",
            import_paths=["pkg.Client.__init__"],
        ),
        APIChange(
            "pkg",
            "1",
            "2",
            REMOVED,
            "pkg.Client.ping",
            "ping",
            "Client",
            import_paths=["pkg.Client.ping"],
        ),
    ]
    package = _package("pkg", changes)
    scan = _scan(tmp_path, package, "import pkg\n")
    ranked = scan.ranked(package, scope=SCOPE_IMPORTED)
    # The rename is exported at the top level: an import path, before the package's move.
    assert [c.path for c in ranked][:2] == ["pkg.FastThing", "pkg.fast"]
    assert ranked[-1].owner == "Client"  # the members come last
    notes = scan.scope_notes(SCOPE_IMPORTED, per_package=2)
    assert [n.api for n in notes] == ["pkg.FastThing", "pkg.fast"]
    assert len(scan.scope_notes(SCOPE_IMPORTED, per_package=1)) == 1
    assert len(scan.scope_notes(SCOPE_IMPORTED)) == min(IMPORTED_APIS, len(package.distinct))
    # The default budget is IMPORTED_APIS; what the code uses always gets its note.
    scan = _scan(tmp_path, package, "from pkg import Client\nClient(retries=1).ping()\n")
    assert {n.api for n in scan.scope_notes(SCOPE_IMPORTED, per_package=1)} >= {
        "Client",
        "Client.ping",
    }


# -------------------------------------------------------- the real mcp diff
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


def test_the_real_mcp_diff_fixture_is_current() -> None:
    _, data = _fixture("mcp-1.28.1-2.2.0")
    assert data["schema"] == DIFF_SCHEMA, "re-record it: python scripts/record_diff_fixtures.py"


def test_mcp_2_leads_with_the_package_move_and_the_renames(tmp_path) -> None:
    """mcp 1.28.1 -> 2.2.0 (the real diff): with 0.4.0 the five notes were parameter removals
    on ClientSession, add_response_router, McpError and mcp.client.experimental. Since 0.5
    the switch from ``httpx`` to ``httpx2`` comes first (test_dependency_switch.py)."""
    package, _ = _fixture("mcp-1.28.1-2.2.0")
    distinct = package.distinct
    [whole] = [c for c in distinct if c.is_package_move]
    assert (whole.path, whole.moved_to) == ("mcp.server.fastmcp", "mcp.server.mcpserver")
    assert whole.move_evidence == {"compared": "package", "kept": 9, "of": 9, "modules": 6}
    # The seven module moves (and the classes moved alongside) are in the diff, not the list.
    raw_moves = [
        c for c in package.changes if c.kind == MOVED and c.path.startswith("mcp.server.fastmcp.")
    ]
    assert len(raw_moves) == 9
    assert not [c for c in distinct if c.kind == MOVED and c.path.startswith("mcp.server.fastmcp.")]
    fastmcp = next(c for c in distinct if c.name == "FastMCP")
    assert (fastmcp.kind, fastmcp.moved_to, fastmcp.renamed_to) == (
        MOVED,
        "mcp.server.MCPServer",
        "MCPServer",
    )
    assert fastmcp.move_evidence == {"compared": "class", "kept": 29, "of": 30, "renamed": True}
    error = next(c for c in distinct if c.name == "McpError")
    assert (error.kind, error.moved_to) == (MOVED, "mcp.MCPError")
    scan = _scan(tmp_path, package, "import mcp\n")
    lines = [n.line for n in scan.scope_notes(SCOPE_IMPORTED)]
    assert lines[0].startswith("mcp 2.2.0 requires `httpx2` instead of `httpx`")
    assert lines[1:] == [
        "`McpError` is now `MCPError`: `mcp.McpError` moved to `mcp.MCPError`; import it with "
        "`from mcp import MCPError`. [diff + move checked]",
        "The package `mcp.server.fastmcp` moved to `mcp.server.mcpserver`; import from there. "
        "[diff + move checked]",
        "`FastMCP` is now `MCPServer`: `mcp.server.FastMCP` moved to `mcp.server.MCPServer`; "
        "import it with `from mcp.server import MCPServer`. [diff + move checked]",
        "`mcp.client.streamable_http.streamablehttp_client` was removed; do not use it. Use "
        "`mcp.client.streamable_http.streamable_http_client` instead. [diff + library]",
    ]
    # With a bigger budget the next ones follow; the package's move still counts once.
    more = scan.scope_notes(SCOPE_IMPORTED, per_package=8)
    assert more[:5] == scan.scope_notes(SCOPE_IMPORTED) and len(more) == 8
    assert more[5].line == (
        "`ClientSession.list_prompts()` no longer accepts `cursor`; do not pass it. Use `params` "
        "instead of `cursor`. [diff + library]"
    )
    assert sum(n.change.is_package_move for n in more) == 1
    # A server written for mcp 1 uses the package's move and the rename, in the old form.
    code = "from mcp.server.fastmcp import FastMCP\n\nmcp = FastMCP('demo')\n"
    scan = _scan(tmp_path, package, code)
    used = {n.api: n.line for n in scan.scope_notes(SCOPE_USED)}
    assert set(used) == {"mcp.server.fastmcp", "mcp.server.FastMCP"}
    assert all("[diff + move checked]" in line for line in used.values())


def test_scan_all_lists_the_changes_in_the_imported_order(tmp_path) -> None:
    from rich.console import Console

    from since_cutoff.report import render_scan_changes

    package, _ = _fixture("mcp-1.28.1-2.2.0")
    scan = _scan(tmp_path, package, "import mcp\n")
    console = Console(record=True, width=200, force_terminal=False, color_system=None)
    render_scan_changes(console, scan, limit=3)
    text = console.export_text()
    assert "mcp 1.28.1 -> 2.2.0: 73 breaking, 9 deprecated (+25 internal)" in text
    lines = [line.strip() for line in text.splitlines() if line.strip().startswith("- ")]
    assert lines == [
        "- mcp requires httpx2 instead of httpx (23 places in its public API that named httpx "
        "types name httpx2 types)",
        "- mcp.McpError moved to mcp.MCPError (McpError is now MCPError)",
        "- the package mcp.server.fastmcp moved to mcp.server.mcpserver (9 modules and classes)",
    ]


def _bullets(text: str, start: str) -> list[str]:
    """The list items after the line that starts with ``start``, without their "also moved
    under n other paths" tail."""
    lines = text.splitlines()
    begin = next(i for i, line in enumerate(lines) if line.startswith(start))
    items = [line.strip()[2:] for line in lines[begin + 1 :] if line.strip().startswith("- ")]
    return [item.split("; also ")[0].split(" (also ")[0] for item in items]


def test_project_changes_lists_a_package_in_the_imported_order(tmp_path) -> None:
    """The MCP tool's per-package sections follow the ``--scope imported`` order too (with
    0.4.0's, the mcp section led with ClientSession's parameters)."""
    from since_cutoff.mcp_server import _package_section

    package, _ = _fixture("mcp-1.28.1-2.2.0")
    scan = _scan(tmp_path, package, "import mcp\n")
    text = "\n".join(_package_section(scan, package, 4))
    assert (
        "## mcp 1.28.1" in text
        and "73 breaking, 9 deprecated (+25 internal). Your code imports it." in text
    )
    assert _bullets(text, "### Dependencies switched")[0].startswith(
        "mcp requires `httpx2` instead of `httpx`; its Requires-Dist lists `httpx2>=2.5.0` and "
        "no `httpx`; 23 places in its public API"
    )
    assert _bullets(text, "### Removed or moved") == [
        "`mcp.McpError` moved to `mcp.MCPError` (`McpError` is now `MCPError`); import it with "
        "`from mcp import MCPError`",
        "the package `mcp.server.fastmcp` moved to `mcp.server.mcpserver` (9 modules and "
        "classes); import it with `from mcp.server import mcpserver`",
        "`mcp.server.FastMCP` moved to `mcp.server.MCPServer` (`FastMCP` is now `MCPServer`); "
        "import it with `from mcp.server import MCPServer`",
    ]


def test_the_markdown_reports_list_a_package_in_the_imported_order(tmp_path) -> None:
    """``report.md``'s "All changes found" and the Markdown summary's folded list per package
    (the CI comment) follow the ``--scope imported`` order."""
    from since_cutoff.report import render_markdown, render_scan_markdown

    package, _ = _fixture("mcp-1.28.1-2.2.0")
    scan = _scan(tmp_path, package, "import mcp\n")
    full = render_markdown(scan)
    items = _bullets(full, "### mcp 1.28.1 -> 2.2.0: 73 breaking, 9 deprecated (+25 internal)")
    # The switch's counts and places are an item of their own under it ("Places:").
    places = (
        "Places: its Requires-Dist lists `httpx2>=2.5.0` and no `httpx`; 23 places in its "
        "public API that named `httpx` types name the `httpx2` types of the same name: "
    )
    assert len(items) == 108 and items[1].startswith(places)
    assert [i.split(" (")[0] for i in [items[0], *items[2:5]]] == [
        "mcp requires `httpx2` instead of `httpx`",
        "`mcp.McpError` moved to `mcp.MCPError`",
        "the package `mcp.server.fastmcp` moved to `mcp.server.mcpserver`",
        "`mcp.server.FastMCP` moved to `mcp.server.MCPServer`",
    ]
    summary = render_scan_markdown(scan, limit=4)
    items = _bullets(summary, "<details><summary><b>mcp</b> 1.28.1 -> 2.2.0")
    assert items[1].startswith(places)
    assert [i.split(" (")[0] for i in [items[0], *items[2:5]]] == [
        "mcp requires `httpx2` instead of `httpx`",
        "`mcp.McpError` moved to `mcp.MCPError`",
        "the package `mcp.server.fastmcp` moved to `mcp.server.mcpserver`",
        "`mcp.server.FastMCP` moved to `mcp.server.MCPServer`",
    ]
    assert items[5] == "... and 78 more in the full report"


@pytest.mark.parametrize("scope", [SCOPE_USED, SCOPE_IMPORTED])
def test_ranked_keeps_what_the_code_uses_first_in_both_scopes(tmp_path, scope) -> None:
    package, _ = _fixture("mcp-1.28.1-2.2.0")
    code = "from mcp.client.session import ClientSession\n\nasync def f(s: ClientSession):\n    await s.list_tools(cursor=None)\n"
    scan = _scan(tmp_path, package, code)
    first = scan.ranked(package, scope=scope)[0]
    assert (first.owner, first.name, first.parameter) == ("ClientSession", "list_tools", "cursor")
