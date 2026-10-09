"""What the project actually uses: "your code uses" marks, imported packages, their order in
every report, and the versions of dependencies that nothing pins."""

from __future__ import annotations

import ast
import re
import textwrap
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from since_cutoff.apidiff import (
    DEPRECATED,
    HINT_PARAM_DOC,
    HINT_WARNING,
    KIND_CHANGED,
    PARAM_REMOVED,
    REMOVED,
    APIChange,
    diff_sources,
)
from since_cutoff.cache import DiskCache
from since_cutoff.engine import CHANGED, KNOWN, Engine, ModelTarget, ScanResult, Settings
from since_cutoff.mcp_server import Tools, _matching, _sections, change_line
from since_cutoff.models import ModelRegistry
from since_cutoff.project import FileUse, load_project, scan_file
from since_cutoff.pypi import PyPI, Release, SourceTree
from since_cutoff.report import (
    render_console,
    render_markdown,
    render_scan_changes,
    render_scan_markdown,
    to_json,
)
from since_cutoff.selection import (
    NAME_MATCH,
    NAME_ONLY,
    OLD_FORM,
    PATH_MATCH,
    USE_CALL,
    USE_KEYWORD,
    USES_API,
    collapse,
    form,
    match,
    used_names,
    uses,
    uses_text,
)
from tests.conftest import TOYLIB_V1, TOYLIB_V2, FakePyPI, write_tree

CUTOFF = date(2025, 7, 31)


def code(text: str) -> tuple[FileUse, ...]:
    """What one file with this code reaches through its imports."""
    return (scan_file(ast.parse(textwrap.dedent(text))),)


def change(path: str, owner: str | None = None, parameter: str | None = None) -> APIChange:
    kind = PARAM_REMOVED if parameter else REMOVED
    package = path.split(".")[0]
    return APIChange(package, "1", "2", kind, path, path.rsplit(".", 1)[-1], owner, parameter)


# ------------------------------------------------------------ "your code uses"
def test_a_module_level_name_counts_only_under_its_own_path() -> None:
    files = code(
        """
        import copier
        import io
        from dataclasses import asdict
        from typing import Callable
        asdict(settings)
        """
    )
    # `asdict` from dataclasses, `Callable` from typing and the stdlib `io` are not the
    # package's names, although a file imports the package.
    assert used_names(change("copier.asdict"), files) == ()
    assert used_names(change("typing_extensions.Callable"), files) == ()
    assert used_names(change("torchaudio.io"), files) == ()

    files = code(
        """
        import numpy as np
        import torch
        from typing_extensions import Callable
        np.linalg.norm(torch.ones(3))
        """
    )
    assert used_names(change("typing_extensions.Callable"), files) == ("Callable",)
    assert used_names(change("numpy.linalg.norm"), files) == ("norm",)  # through `np`
    assert used_names(change("numpy.ones"), files) == ()  # torch.ones is not numpy's
    # Another path to the same object counts too (APIChange.also).
    moved = change("numpy.core.norm")
    moved.also = ["numpy.linalg.norm"]
    assert used_names(moved, files) == ("norm",)

    star = code("from typing_extensions import *\nhandler: Callable[[int], int]\n")
    assert used_names(change("typing_extensions.Callable"), star) == ("Callable",)


def test_a_method_needs_its_class_named_in_the_same_file() -> None:
    files = code(
        """
        import os
        from fake_useragent import UserAgent
        headers = {"User-Agent": UserAgent().random}
        cookies.update(extra)
        session.verify = os.path.exists(bundle)
        """
    )
    # utils.update is another module's function: `cookies.update` is a dict's.
    assert used_names(change("fake_useragent.utils.update", None, "path"), files) == ()
    assert used_names(change("fake_useragent.UserAgent.random", "UserAgent"), files) == ("random",)
    # A common method name on a class the file never names.
    assert used_names(change("dulwich.objects.Commit.verify", "Commit"), files) == ()


def test_an_attribute_counts_only_on_the_class_or_an_instance_of_it() -> None:
    # python-sdk: mcp's own Server keeps `self.middleware`, in a file that imports Starlette,
    # which marked starlette's removed `Starlette.middleware` as used.
    middleware = change("starlette.applications.Starlette.middleware", "Starlette")
    own = code(
        """
        from starlette.applications import Starlette
        class Server:
            def __init__(self):
                self.middleware = []
            def app(self):
                return Starlette(routes=[])
        """
    )
    assert used_names(middleware, own) == ()
    for text in (
        "app = Starlette(routes=[])\napp.middleware.append(m)\n",
        "def build(app: Starlette | None) -> None:\n    app.middleware.append(m)\n",
        "class App(Starlette):\n    def add(self, m):\n        self.middleware.append(m)\n",
        "Starlette(routes=[]).middleware\n",
        "with Starlette() as app:\n    app.middleware\n",
        "print(Starlette.middleware)\n",
    ):
        files = code(f"from starlette.applications import Starlette\n{text}")
        assert used_names(middleware, files) == ("middleware",), text


def test_a_keyword_counts_only_for_its_own_callable() -> None:
    files = code(
        """
        import subprocess
        from vcs import GitClient, Version

        def fetch(client: GitClient, model) -> None:
            Version("1.0")
            subprocess.run(["git"], text=True)
            model.save(update_fields=["name"])
        """
    )
    # `text=True` goes to subprocess.run, not to Version().
    assert used_names(change("vcs.Version.__init__", "Version", "text"), files) == ("Version",)
    # GitClient is only an annotation: its constructor is never called.
    assert used_names(change("vcs.GitClient.__init__", "GitClient", "kwargs"), files) == ()
    # `*args` cannot be passed as `args=`, and the code passes no such keyword.
    save = change("vcs.Model.save", "Model", "args")
    assert used_names(save, code("from vcs import Model\nModel().save(force=True)\n")) == ("save",)


def test_no_marks_for_names_the_package_does_not_provide(tmp_path: Path, cache, fake_pypi) -> None:
    # toylib 2.0 removed legacy_fetch and Session moved, fetch() needs `timeout` and
    # Client.close is deprecated. The code imports toylib, but these names are other packages'.
    root = write_tree(
        tmp_path / "app",
        {
            "requirements.txt": "toylib==2.0\n",
            "main.py": """
                import requests
                from mylib import fetch, legacy_fetch
                from toylib import Client
                Client()
                fetch(url, timeout=5)
                legacy_fetch(url)
                requests.Session()
            """,
            "other.py": "import toylib\nhandle.close()\n",  # a file handle, not a Client
        },
    )
    md = render_scan_markdown(_scan(root, fake_pypi))
    assert "your code uses" not in md and "<details open>" not in md
    tools = Tools(cache, registry=ModelRegistry(cache, offline=True), pypi=fake_pypi)
    out = tools.project_changes(str(root), cutoff="2025-07")
    assert "Your code imports it." in out
    assert "Touching names your code uses" not in out and "[your code uses" not in out


def test_names_count_only_in_the_files_that_import_the_package(tmp_path: Path) -> None:
    root = write_tree(
        tmp_path / "app",
        {
            "requirements.txt": "anthropic\nopenai\n",
            "claude.py": "import anthropic\nclient.messages.create(model=m, max_tokens=5)\n",
            "gpt.py": "import openai\nprint(openai.__name__)\n",
        },
    )
    project = load_project(root)
    [gpt] = project.code_use(["openai"])
    assert "openai" in gpt.paths and "messages.create" not in gpt.pairs
    # client.messages.create in claude.py is Anthropic's, not openai's Messages.create.
    create = change("openai.resources.Messages.create", "Messages", "max_tokens")
    assert used_names(create, project.code_use(["openai"])) == ()
    anthropic = change("anthropic.resources.Messages.create", "Messages", "max_tokens")
    assert used_names(anthropic, project.code_use(["anthropic"])) == ("create", "max_tokens")


# ----------------------------------------------------------- imported and order
def _lib(
    name: str, v1: dict[str, str], v2: dict[str, str], tmp_path: Path
) -> dict[str, SourceTree]:
    return {
        version: SourceTree(
            name, version, write_tree(tmp_path / f"{name}-{version}", files), (name,)
        )
        for version, files in (("1.0", v1), ("2.0", v2))
    }


def _functions(names: list[str], deprecated: tuple[str, ...] | list[str] = ()) -> str:
    lines = ["from typing_extensions import deprecated", ""]
    for n in names:
        if n in deprecated:
            lines.append('@deprecated("Use h() instead.")')
        lines += [f"def {n}() -> None:", "    pass", ""]
    return "\n".join(lines)


def _pypi(tmp_path: Path, cache: DiskCache) -> FakePyPI:
    """toylib (5 breaking, 1 deprecated), breaklib (7 breaking), deplib (2 breaking, 7
    deprecated), quietlib (1 deprecated) and oldlib (released before the cutoff)."""
    f = [f"f{i}" for i in range(9)]
    libs = {
        "toylib": _lib("toylib", TOYLIB_V1, TOYLIB_V2, tmp_path),
        "breaklib": _lib(
            "breaklib",
            {"breaklib/__init__.py": _functions([f"g{i}" for i in range(7)])},
            {"breaklib/__init__.py": _functions(["h"])},
            tmp_path,
        ),
        "deplib": _lib(
            "deplib",
            {"deplib/__init__.py": _functions([*f, "h"])},
            {"deplib/__init__.py": _functions([*f[2:], "h"], deprecated=f[2:])},
            tmp_path,
        ),
        "quietlib": _lib(
            "quietlib",
            {"quietlib/__init__.py": _functions(["f", "h"])},
            {"quietlib/__init__.py": _functions(["f", "h"], deprecated=["f"])},
            tmp_path,
        ),
    }
    releases = {name: [("1.0", "2025-01-10"), ("2.0", "2025-10-01")] for name in libs}
    releases["oldlib"] = [("1.0", "2024-01-10")]
    trees = {(name, v): tree for name, lib in libs.items() for v, tree in lib.items()}
    return FakePyPI(cache, releases, trees)


def _scan(root: Path, pypi: PyPI) -> ScanResult:
    engine = Engine(Settings(cutoff=CUTOFF), store=pypi.cache, llm_cache=pypi.cache, pypi=pypi)
    return engine.scan(load_project(root), ModelTarget.cutoff_only(CUTOFF))


def _app(tmp_path: Path, deps: list[str], main: str) -> Path:
    listed = ", ".join(f'"{d}"' for d in deps)
    return write_tree(
        tmp_path / "app",
        {
            "pyproject.toml": f'[project]\nname = "app"\nversion = "0"\ndependencies = [{listed}]\n',
            "main.py": main,
        },
    )


def _table_rows(text: str, names: list[str]) -> list[str]:
    """The package names in the order a report's table lists them."""
    found = []
    for line in text.splitlines():
        cells = line.replace("│", "|").strip("| ").split("|")
        name = cells[0].strip() if cells else ""
        if name in names and name not in found:
            found.append(name)
    return found


def test_imported_packages_come_first_in_every_report(tmp_path: Path, cache: DiskCache) -> None:
    pypi = _pypi(tmp_path, cache)
    root = _app(
        tmp_path,
        ["toylib==2.0", "breaklib==2.0", "oldlib==1.0"],
        "from toylib import Client\nClient().send('hi', temperature=0.2)\n",
    )
    scan = _scan(root, pypi)
    names = ["toylib", "breaklib", "oldlib"]

    # The field is set once, from the import names; None where they were never read.
    data = to_json(scan)
    assert [(p["name"], p["imported"]) for p in data["scan"]] == [
        ("toylib", True),
        ("breaklib", False),
        ("oldlib", None),
    ]
    assert [p["imported"] for p in data["packages"]] == [True, False, None]

    # toylib (5 breaking) comes before breaklib (7): the code imports it.
    console = Console(width=200, record=True)
    render_console(console, scan, verbose=True)
    render_scan_changes(console, scan)
    text = console.export_text()
    assert _table_rows(text, names) == names
    assert "API changed, imported by your code" in text
    assert text.index("toylib 1.0 -> 2.0") < text.index("breaklib 1.0 -> 2.0")

    report = render_markdown(scan)
    assert _table_rows(report.split("## Dependencies")[1], names) == names
    assert report.index("### toylib 1.0 -> 2.0") < report.index("### breaklib 1.0 -> 2.0")
    summary = render_scan_markdown(scan)
    assert _table_rows(summary, names) == ["toylib", "breaklib"]

    tools = Tools(cache, registry=ModelRegistry(cache, offline=True), pypi=pypi)
    out = tools.project_changes(str(root), cutoff="2025-07")
    assert out.index("## toylib ") < out.index("## breaklib ")
    assert "5 breaking, 1 deprecated. Your code imports it." in out
    assert "7 breaking, 0 deprecated.\n" in out


def test_packages_are_ordered_by_the_breaking_changes_the_table_shows(
    tmp_path: Path, cache: DiskCache
) -> None:
    pypi = _pypi(tmp_path, cache)
    root = _app(tmp_path, ["deplib==2.0", "breaklib==2.0", "quietlib==2.0"], "print('hi')\n")
    scan = _scan(root, pypi)
    assert [(p.name, p.counts) for p in scan.packages] == [
        ("breaklib", (7, 0)),
        ("deplib", (2, 7)),  # 9 changes in all, but fewer breaking ones
        ("quietlib", (0, 1)),
    ]
    assert all(p.status == CHANGED for p in scan.packages)

    console = Console(width=200, record=True)
    render_console(console, scan)
    lines = console.export_text().splitlines()
    header = next(line for line in lines if line.startswith("package"))
    # Rich draws the header with heavy bars (┃) and rows with light ones (│); legacy Windows
    # consoles get light bars throughout.
    cells = re.compile("[│┃]").split
    assert cells(header)[4:] == [" breaking ", " deprecated"]
    rows = {cells(line)[0].strip(): cells(line)[4:] for line in lines if "│" in line}
    assert [c.strip() for c in rows["deplib"]] == ["2", "7"]
    assert [c.strip() for c in rows["quietlib"]] == ["0", "1"]  # never a blank cell

    report = render_markdown(scan)
    assert "| package | you use | at cutoff | status | breaking | deprecated |\n" in report
    assert "| quietlib | 2.0 (2025-10-01) | 1.0 | API changed | 0 | 1 |" in report
    assert "probed" not in report  # a scan has no probes


# --------------------------------------------------------- unpinned versions
def test_an_unpinned_dependency_stays_in_its_declared_range(tmp_path: Path, cache, fake_pypi):
    root = write_tree(
        tmp_path / "app",
        {"pyproject.toml": '[project]\nname = "app"\nversion = "0"\ndependencies = ["toylib<2"]\n'},
    )
    toylib = _scan(root, fake_pypi).package("toylib")
    # 1.0, the newest release below 2, not 2.0: known to a model with a July 2025 cutoff.
    assert (toylib.locked, toylib.status) == ("1.0", KNOWN)
    assert toylib.version_source == "latest on PyPI matching <2"
    [dep] = load_project(root).dependencies
    assert (dep.version, dep.specifier) == (None, "<2")


def test_environment_markers_pick_the_declaration_for_the_python(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "requirements.txt": 'toylib==2.0 ; python_version >= "3.10"\n'
            'toylib==1.0 ; python_version < "3.10"\n'
            'rich<14 ; python_version < "3.10"\n'
            'rich>=13 ; python_version >= "3.10"\n'
            'colorama ; sys_platform == "win32"\n',
        },
    )

    def declared(python: str | None) -> dict[str, tuple[str | None, str]]:
        deps = load_project(tmp_path, python=python).dependencies
        return {d.name: (d.version, d.specifier) for d in deps}

    # Without a Python version, the newest Python is assumed (as for uv.lock forks).
    assert declared(None) == {"toylib": ("2.0", ""), "rich": (None, ">=13"), "colorama": (None, "")}
    assert declared("3.9") == {"toylib": ("1.0", ""), "rich": (None, "<14"), "colorama": (None, "")}
    assert declared("3.12")["toylib"] == ("2.0", "")


def test_a_direct_url_in_one_extra_does_not_hide_the_pypi_declarations(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "app"\nversion = "0"\n'
            'dependencies = ["torch==2.10.0", "triton @ https://example.org/triton.whl"]\n'
            "[project.optional-dependencies]\n"
            'cuda = ["torch @ https://download.pytorch.org/whl/torch-2.10.0.whl"]\n',
        },
    )
    deps = {d.name: d for d in load_project(tmp_path).dependencies}
    assert (deps["torch"].version, deps["torch"].non_pypi) == ("2.10.0", None)
    assert deps["triton"].non_pypi == "https://example.org/triton.whl"  # declared by URL only


def test_pipfile_and_poetry_constraints_are_respected(tmp_path: Path) -> None:
    write_tree(
        tmp_path / "pipenv",
        {"Pipfile": '[packages]\ntoylib = "<2"\nrequests = "==2.28.2"\nrich = "*"\n'},
    )
    project = load_project(tmp_path / "pipenv")
    deps = {d.name: (d.version, d.specifier) for d in project.dependencies}
    assert deps == {"toylib": (None, "<2"), "requests": ("2.28.2", ""), "rich": (None, "")}
    assert project.version_source == "Pipfile, latest on PyPI for 2 unpinned"

    write_tree(
        tmp_path / "poetry",
        {
            "pyproject.toml": "[tool.poetry.dependencies]\n"
            'python = "^3.11"\ntoylib = ">=1,<2"\nrich = "^13.0"\n'
        },
    )
    deps = {d.name: d.specifier for d in load_project(tmp_path / "poetry").dependencies}
    assert deps == {"toylib": "<2,>=1", "rich": ""}  # Poetry's ^ is not a PEP 440 specifier


class _PythonAwarePyPI(FakePyPI):
    """Releases whose files require a minimum Python, as on PyPI."""

    def releases(self, name: str) -> list[Release]:
        needs = {"1.0": ">=3.9", "2.0": ">=3.12"}
        return [
            Release(
                r.version,
                r.uploaded,
                r.yanked,
                ({**r.files[0], "requires_python": needs[r.version]},),
            )
            for r in super().releases(name)
        ]


def test_unpinned_versions_install_on_the_projects_python(tmp_path: Path, cache) -> None:
    pypi = _PythonAwarePyPI(cache, {"toylib": [("1.0", "2025-01-10"), ("2.0", "2025-10-01")]}, {})
    root = write_tree(
        tmp_path / "app", {"requirements.txt": "toylib\n", ".python-version": "3.11\n"}
    )
    toylib = _scan(root, pypi).package("toylib")
    # toylib 2.0 needs Python 3.12: pip on 3.11 installs 1.0.
    assert (toylib.locked, toylib.version_source) == ("1.0", "latest on PyPI for Python 3.11")
    assert pypi.best_match("toylib", "", python="3.12").version == "2.0"  # type: ignore[union-attr]
    assert pypi.best_match("toylib", "").version == "2.0"  # type: ignore[union-attr]


def test_deprecated_changes_can_be_marked_too() -> None:
    files = code("import toylib\nclient = toylib.Client()\nclient.close()\n")
    close = APIChange("toylib", "1", "2", DEPRECATED, "toylib.Client.close", "close", "Client")
    assert used_names(close, files) == ("close",)


# ------------------------------------------------ re-exports and every path (0.4)
# ``tp/__init__.py`` re-exports what ``tp/dl.py`` defines; 2.0 drops ``fetch(resume=)`` and
# ``Client.send(retries=)``. The diff records them at tp.dl, where they are defined.
TP_INIT = 'from .dl import Client, fetch\n\n__all__ = ["Client", "fetch"]\n'
TP_V1 = "def fetch(url, resume=False):\n    pass\n\n\nclass Client:\n"
TP_V1 += "    def send(self, x, retries=0):\n        pass\n"
TP_V2 = "def fetch(url):\n    pass\n\n\nclass Client:\n    def send(self, x):\n        pass\n"


def _tp_changes(tmp_path: Path) -> dict[str, APIChange]:
    old = write_tree(tmp_path / "tp-1", {"tp/__init__.py": TP_INIT, "tp/dl.py": TP_V1})
    new = write_tree(tmp_path / "tp-2", {"tp/__init__.py": TP_INIT, "tp/dl.py": TP_V2})
    raw = diff_sources("tp", "1", old, "2", new, ["tp"])
    changes = {c.name: c for c in map(APIChange.from_dict, raw)}
    assert {(c.path, c.parameter) for c in changes.values()} == {
        ("tp.dl.fetch", "resume"),
        ("tp.dl.Client.send", "retries"),
    }
    return changes


@pytest.mark.parametrize(
    ("text", "name", "used"),
    [
        ("from tp import fetch\nfetch('u', resume=True)\n", "fetch", ("fetch", "resume")),
        ("import tp\ntp.fetch('u', resume=True)\n", "fetch", ("fetch", "resume")),
        ("from tp.dl import fetch\nfetch('u', resume=True)\n", "fetch", ("fetch", "resume")),
        ("from tp import Client\nClient().send(1, retries=2)\n", "send", ("send", "retries")),
        ("from tp.dl import Client\nClient().send(1, retries=2)\n", "send", ("send", "retries")),
        ("import tp\nc = tp.Client()\nc.send(1)\n", "send", ("send",)),
    ],
)
def test_names_a_package_re_exports_are_used_names(tmp_path: Path, text, name, used) -> None:
    # 0.3.1 found only the last two forms: the change's own path, not the re-export.
    changes = _tp_changes(tmp_path)
    assert used_names(changes[name], code(text)) == used
    assert match(changes[name], code(text)) == PATH_MATCH
    other = changes["send" if name == "fetch" else "fetch"]
    assert used_names(other, code(text)) == ()


def test_a_collapsed_change_is_matched_under_every_path(tmp_path: Path) -> None:
    # One removed Thing under 8 paths: collapse keeps 5 of them to show, and matching the
    # project's code still sees all 8 (0.3.1 missed `pkg.m7.Thing`).
    old = {"pkg/__init__.py": ""}
    old |= {f"pkg/m{i}.py": "class Thing:\n    pass\n" for i in range(8)}
    new = {"pkg/__init__.py": ""} | {f"pkg/m{i}.py": "" for i in range(8)}
    raw = diff_sources(
        "pkg", "1", write_tree(tmp_path / "a", old), "2", write_tree(tmp_path / "b", new), ["pkg"]
    )
    changes = [APIChange.from_dict(c) for c in raw]
    (thing,) = collapse(changes)
    assert (thing.path, thing.occurrences, len(thing.also)) == ("pkg.m0.Thing", 8, 5)
    assert "pkg.m7.Thing" not in thing.also
    files = code("from pkg.m7 import Thing\nThing()\n")
    assert used_names(thing, files) == ("Thing",)
    assert uses_text(thing, files) == "`Thing`"
    # Changes without import paths (made by hand): each change collapsed is still matched.
    legacy = [change(f"pkg.m{i}.Thing") for i in range(8)]
    (thing,) = collapse(legacy)
    assert thing.import_paths is None and "pkg.m7.Thing" not in thing.also
    assert (used_names(thing, files), match(thing, files)) == (("Thing",), PATH_MATCH)
    assert "_merged" not in thing.to_dict()


def test_a_collapsed_change_is_matched_as_each_change_it_merged() -> None:
    # The same parameter removed from `Messages.create` and its async mirror in another
    # module: one concept, shown once under the async one (its path sorts first), and
    # `client.messages.create(temperature=...)` is the sync one's use.
    mirror = change("anthropic.a.AsyncMessages.create", "AsyncMessages", "temperature")
    sync = change("anthropic.b.Messages.create", "Messages", "temperature")
    (create,) = collapse([sync, mirror])
    assert (create.owner, create.also) == ("AsyncMessages", ["anthropic.b.Messages.create"])
    files = code("import anthropic\nclient.messages.create(model=m, temperature=0.2)\n")
    assert used_names(create, files) == ("create", "temperature")
    assert used_names(mirror, files) == ()  # 0.3.1 matched the collapsed change as this one


def test_a_constructor_counts_under_each_name_of_its_class() -> None:
    # A change merged with its async twin's (apidiff groups them) and a class re-exported
    # under another name: the call names the class as the file imported it.
    init = change("toylib.Client.__init__", "Client", "timeout")
    init.also = ["toylib.AsyncClient.__init__"]
    init.import_paths = [
        "toylib.AsyncClient.__init__",
        "toylib.Client.__init__",
        "toylib.HubClient.__init__",
    ]
    for cls in ("Client", "AsyncClient", "HubClient"):
        files = code(f"from toylib import {cls}\n{cls}(timeout=5)\n")
        assert used_names(init, files) == (cls, "timeout"), cls
    # The class not imported, named but not called, or another class called: no use of it.
    assert used_names(init, code("import toylib\nClient(timeout=5)\n")) == ()
    assert used_names(init, code("from toylib import Client\nc: Client = make()\n")) == ()
    assert used_names(init, code("from toylib import Client\nfrom x import Other\nOther()\n")) == ()


def test_without_import_paths_the_package_and_name_count(tmp_path: Path) -> None:
    # A change from a diff cached before DIFF_SCHEMA 12 (no import paths): the same import
    # root and the same last name count too, and the mark says it matched by name only.
    fetch = change("tp.dl.fetch", parameter="resume")
    assert fetch.import_paths is None
    files = code("from tp import fetch\nfetch('u', resume=True)\n")
    assert used_names(fetch, files) == ("fetch", "resume")
    assert match(fetch, files) == NAME_MATCH
    assert uses_text(fetch, files) == "`fetch` and `resume`, matched by name"
    send = change("tp.dl.Client.send", "Client", "retries")
    assert used_names(send, code("from tp import Client\nClient().send(1, retries=2)\n")) == (
        "send",
        "retries",
    )
    # Another package's name is never taken for it: `asdict` of dataclasses is not copier's.
    asdict = code("import copier\nfrom dataclasses import asdict\nasdict(settings)\n")
    assert used_names(change("copier.asdict"), asdict) == ()
    assert used_names(change("copier.utils.asdict"), asdict) == ()
    # With import paths from the diff, a path must match: the name alone is not enough.
    fetch.import_paths = ["tp.dl.fetch"]
    assert (used_names(fetch, files), match(fetch, files), uses_text(fetch, files)) == (
        (),
        None,
        None,
    )


def test_a_lazy_init_like_huggingface_hubs_is_resolved(tmp_path: Path) -> None:
    # `from huggingface_hub import hf_hub_download` with resume_download=: hub's __init__
    # lists the name in __all__, imports it under `if TYPE_CHECKING:` and serves it from a
    # module __getattr__. The change is recorded at file_download, where it is defined.
    init = (
        "from typing import TYPE_CHECKING\n\n"
        '__all__ = ["hf_hub_download"]\n\n\n'
        "def __getattr__(name):\n"
        "    from . import file_download\n\n"
        "    return getattr(file_download, name)\n\n\n"
        "if TYPE_CHECKING:  # pragma: no cover\n"
        "    from .file_download import hf_hub_download\n"
    )
    old = {
        "hub/__init__.py": init,
        "hub/file_download.py": "def hf_hub_download(repo_id, filename, resume_download=None):\n"
        "    pass\n",
    }
    new = {
        "hub/__init__.py": init,
        "hub/file_download.py": "def hf_hub_download(repo_id, filename):\n    pass\n",
    }
    a, b = write_tree(tmp_path / "a", old), write_tree(tmp_path / "b", new)
    (removed,) = [APIChange.from_dict(c) for c in diff_sources("hub", "1", a, "2", b, ["hub"])]
    assert removed.path == "hub.file_download.hf_hub_download"
    files = code(
        "from hub import hf_hub_download\n"
        "hf_hub_download('gpt2', 'config.json', resume_download=True)\n"
    )
    assert used_names(removed, files) == ("hf_hub_download", "resume_download")
    assert match(removed, files) == PATH_MATCH


def test_a_name_match_is_labelled_in_every_report(tmp_path: Path) -> None:
    legacy = change("toylib.helpers.fetch", parameter="retries")
    files = code("from toylib import fetch\nfetch(url, retries=2)\n")
    assert "[your code uses `fetch` and `retries`, matched by name]" in "\n".join(
        _sections([legacy], files)
    )


def test_mcp_finds_a_change_under_any_of_its_paths() -> None:
    # api_changes(symbol=...) looks in import_paths too, so a class past the five that
    # `also` keeps is found (a method removed from a base class that eight classes inherit).
    legacy = change("pkg.base.Base.legacy", "Base")
    legacy.also = [f"pkg.models.M{i}.legacy" for i in range(5)]
    assert _matching([legacy], "M7.legacy") == ([legacy], True)  # by `legacy` alone: loose
    legacy.import_paths = [legacy.path, *(f"pkg.models.M{i}.legacy" for i in range(8))]
    assert _matching([legacy], "M7.legacy") == ([legacy], False)


def test_mcp_says_where_a_hint_comes_from() -> None:
    removed = change("pkg.fetch", parameter="resume")
    removed.hint = "`resume` is deprecated. Use `force=True` instead."
    removed.hint_source = HINT_WARNING
    assert "the old version warned: `resume` is deprecated" in change_line(removed)
    removed.hint_source = HINT_PARAM_DOC
    assert "the old docs said: `resume` is deprecated" in change_line(removed)


# ------------------------------------------------ receivers typed from the API (0.6)
# ``frames`` 1.0 -> 2.0, a package whose own annotations say what its values are (pandas'
# shape). 2.0 removes ``Frame.applymap``, ``NDFrame.swapaxes`` (``Frame`` inherits it),
# ``Grouped.count``, ``Frame.groupby(axis=)`` and the ``melt`` of both ``Frame`` and ``Lazy``, and
# turns the class ``option_context`` into a function.
FRAMES_V1 = {
    "frames/__init__.py": """
        from frames.core import Frame, Grouped, Lazy, NDFrame, option_context, read_csv

        __all__ = ["Frame", "Grouped", "Lazy", "NDFrame", "option_context", "read_csv"]
    """,
    "frames/core.py": """
        from __future__ import annotations

        from typing_extensions import Self


        class Grouped:
            def count(self) -> Frame:
                return Frame()

            def sum(self) -> Frame:
                return Frame()


        class NDFrame:
            def head(self) -> Self:
                return self

            def swapaxes(self, a: int, b: int) -> Self:
                return self


        class Frame(NDFrame):
            def applymap(self, func: object) -> Frame:
                return self

            def groupby(self, key: str, axis: int = 0) -> Grouped:
                return Grouped()

            def melt(self, id_vars: str) -> Frame:
                return self

            @property
            def T(self) -> Frame:
                return self


        class Lazy:
            def melt(self, id_vars: str) -> Lazy:
                return self

            def collect(self) -> Frame:
                return Frame()


        def read_csv(path: str) -> Frame:
            return Frame()


        class option_context:
            def __init__(self, *args: object) -> None:
                pass
    """,
}
FRAMES_V2 = {
    "frames/__init__.py": FRAMES_V1["frames/__init__.py"],
    "frames/core.py": """
        from __future__ import annotations

        from typing_extensions import Self


        class Grouped:
            def sum(self) -> Frame:
                return Frame()


        class NDFrame:
            def head(self) -> Self:
                return self


        class Frame(NDFrame):
            def groupby(self, key: str) -> Grouped:
                return Grouped()

            @property
            def T(self) -> Frame:
                return self


        class Lazy:
            def collect(self) -> Frame:
                return Frame()


        def read_csv(path: str) -> Frame:
            return Frame()


        def option_context(*args: object) -> None:
            pass
    """,
}
# ``core`` 1.0 -> 2.0 loses ``predict`` on ``BaseLanguage`` and on ``BaseChat``, which overrides
# it (langchain-core 1's shape); ``plugins``, another package, derives ``ChatX`` from ``BaseChat``
# (langchain-openai's ``ChatOpenAI``) and did not change.
CORE_V1 = {
    "core/__init__.py": "",
    "core/models.py": """
        class BaseLanguage:
            def predict(self, text: str) -> str:
                return text

            def invoke(self, text: str) -> str:
                return text


        class BaseChat(BaseLanguage):
            def predict(self, text: str) -> str:
                return text
    """,
}
CORE_V2 = {
    "core/__init__.py": "",
    "core/models.py": """
        class BaseLanguage:
            def invoke(self, text: str) -> str:
                return text


        class BaseChat(BaseLanguage):
            pass
    """,
}
PLUGINS = {
    "plugins/__init__.py": 'from plugins.chat import ChatX\n\n__all__ = ["ChatX"]\n',
    "plugins/chat.py": """
        from core.models import BaseChat


        class ChatX(BaseChat):
            pass
    """,
}


def _typed_scan(
    tmp_path: Path,
    cache: DiskCache,
    libs: dict[str, tuple[dict[str, str], dict[str, str]]],
    files: dict[str, str],
) -> ScanResult:
    """A scan of an app with these files, pinning 2.0 of each lib (1.0 at the cutoff)."""
    trees: dict[tuple[str, str], SourceTree] = {}
    for name, (v1, v2) in libs.items():
        trees.update({(name, v): t for v, t in _lib(name, v1, v2, tmp_path).items()})
    releases = {name: [("1.0", "2025-01-10"), ("2.0", "2025-10-01")] for name in libs}
    pins = "".join(f"{name}==2.0\n" for name in libs)
    root = write_tree(tmp_path / "app", {"requirements.txt": pins, **files})
    return _scan(root, FakePyPI(cache, releases, trees))


def _found(scan: ScanResult) -> dict[str, tuple[str, str | None, list[tuple[Any, ...]]]]:
    """Each used API: its form, how it matched, and its uses as (file, kind, names, how)."""
    return {
        u.note.api: (u.form, u.match, [(x.file, x.kind, x.names, x.how) for x in u.uses])
        for u in scan.used_apis()
    }


def test_a_value_from_a_package_call_is_typed_from_the_packages_annotations(tmp_path, cache):
    # The audit's biggest under-report: ``df = pd.read_csv(...)`` and ``df.applymap(str)``
    # were not flagged, as the file never names ``DataFrame``.
    scan = _typed_scan(
        tmp_path,
        cache,
        {"frames": (FRAMES_V1, FRAMES_V2)},
        {
            "main.py": """
                import json
                import frames as fr

                def load(path):
                    df = fr.read_csv(path)
                    df.applymap(str)
                    df.swapaxes(0, 1)
                    df.groupby("k", axis=0).count()
                    wide = fr.Frame()
                    wide.melt(id_vars="a")
                    lazy = fr.Lazy()
                    lazy.collect().T.applymap(str)
                    with fr.option_context("mode", True):
                        pass
            """,
            "other.py": """
                import json
                import frames

                def shape(path):
                    data = json.loads(path)
                    data.applymap(str)
                    data.swapaxes(0, 1)
                    data.melt(id_vars="a")
                    data.groupby("k", axis=0).count()
            """,
        },
    )
    main = "main.py"
    assert _found(scan) == {
        # Assigned from a call of a package function: its return annotation's class.
        "Frame.applymap": (OLD_FORM, PATH_MATCH, [(main, USE_CALL, ("applymap",), PATH_MATCH)]),
        # Inherited: the diff lists ``Frame.swapaxes`` among the paths of ``NDFrame.swapaxes``.
        "NDFrame.swapaxes": (OLD_FORM, PATH_MATCH, [(main, USE_CALL, ("swapaxes",), PATH_MATCH)]),
        # A chain: ``groupby`` returns a ``Grouped``; the keyword goes to ``Frame.groupby``.
        "Frame.groupby": (
            OLD_FORM,
            PATH_MATCH,
            [(main, USE_KEYWORD, ("groupby", "axis"), PATH_MATCH)],
        ),
        "Grouped.count": (OLD_FORM, PATH_MATCH, [(main, USE_CALL, ("count",), PATH_MATCH)]),
        # A constructor, through the module (``fr.Frame()``), and a property (``.T``).
        "Frame.melt": (OLD_FORM, PATH_MATCH, [(main, USE_CALL, ("melt",), PATH_MATCH)]),
        # A class that became a function, called the same way: not the old form (item F).
        "frames.option_context": (
            USES_API,
            PATH_MATCH,
            [(main, USE_CALL, ("option_context",), PATH_MATCH)],
        ),
    }
    # other.py imports frames, and its ``data`` is json's: none of its calls counts.
    assert all(u.file == main for api in scan.used_apis() for u in api.uses)
    assert len(scan.diff_notes()) == 6  # every one of them goes into the notes


def test_an_annotation_types_a_parameter_and_self_types_a_subclass(tmp_path, cache):
    scan = _typed_scan(
        tmp_path,
        cache,
        {"frames": (FRAMES_V1, FRAMES_V2)},
        {
            "main.py": """
                from frames import Frame, Lazy

                def tidy(df: "Frame | None", lazy: Lazy):
                    df.applymap(str)
                    lazy.collect().swapaxes(0, 1)

                class Mine(Frame):
                    def go(self):
                        self.groupby("k", axis=1)
            """,
        },
    )
    main = "main.py"
    assert _found(scan) == {
        # A string annotation (0.5 resolved only an annotation written as code).
        "Frame.applymap": (OLD_FORM, PATH_MATCH, [(main, USE_CALL, ("applymap",), PATH_MATCH)]),
        "NDFrame.swapaxes": (OLD_FORM, PATH_MATCH, [(main, USE_CALL, ("swapaxes",), PATH_MATCH)]),
        "Frame.groupby": (
            OLD_FORM,
            PATH_MATCH,
            [(main, USE_KEYWORD, ("groupby", "axis"), PATH_MATCH)],
        ),
    }


def test_a_class_is_read_with_its_bases_across_packages(tmp_path, cache):
    # langchain's shape: ``llm = ChatOpenAI(...)`` (langchain-openai) and ``llm.predict(q)``,
    # where langchain-core removed ``BaseChatModel.predict``; the file never imports
    # langchain-core. The base's own ``BaseLanguage.predict`` is not a second use: the diff
    # reports it on its own, and ``BaseChat`` is the nearest class that lost ``predict``.
    scan = _typed_scan(
        tmp_path,
        cache,
        {"core": (CORE_V1, CORE_V2), "plugins": (PLUGINS, PLUGINS)},
        {
            "main.py": """
                from plugins import ChatX

                def ask(q):
                    llm = ChatX()
                    return llm.predict(q)

                class Mine(ChatX):
                    def go(self, q):
                        return self.predict(q)
            """,
            "other.py": "from plugins import ChatX\n\nmodel = {}\nmodel.predict(1)\n",
        },
    )
    assert _found(scan) == {
        "BaseChat.predict": (
            OLD_FORM,
            PATH_MATCH,
            [("main.py", USE_CALL, ("predict",), PATH_MATCH)],
        )
    }
    core = scan.package("core")
    assert core.imported is False  # no file imports it; main.py still uses its API
    [main] = scan.uses(core)
    assert (main.file, "core" in main.reaches, "core" in main.paths) == ("main.py", True, False)


def test_a_name_alone_counts_when_it_is_one_changed_apis_and_not_common(tmp_path, cache):
    scan = _typed_scan(
        tmp_path,
        cache,
        {"frames": (FRAMES_V1, FRAMES_V2)},
        {
            "main.py": """
                import frames

                def tidy(df, rows, long):
                    df.applymap(str)
                    rows.count()
                    long.melt(id_vars="a")
            """,
        },
    )
    # ``applymap``: one changed API's, and not a common name: a name match, tagged. ``count``
    # (``Grouped.count``) is too common a name; ``melt`` is two changed APIs' (``Frame`` and
    # ``Lazy``): neither counts.
    assert _found(scan) == {
        "Frame.applymap": (
            OLD_FORM,
            NAME_ONLY,
            [("main.py", USE_CALL, ("applymap",), NAME_ONLY)],
        )
    }
    frames = scan.package("frames")
    applymap = next(c for c in frames.distinct if c.name == "applymap")
    assert uses_text(applymap, scan.uses(frames)) == "`applymap` [name match]"
    # Out of the notes (and the block) by default; in with --include-name-matches.
    assert scan.diff_notes() == [] and scan.used_changes(frames) == []
    assert scan.notes_block(scan.diff_notes()) is None
    assert [n.api for n in scan.diff_notes(name_matches=True)] == ["Frame.applymap"]
    scan.name_matches = True
    assert [n.api for n in scan.diff_notes()] == ["Frame.applymap"]


def test_a_class_that_became_a_function_is_the_old_form_only_with_a_refused_keyword(tmp_path):
    # Item F: ``with pd.option_context("mode.copy_on_write", True):`` is the same code for
    # pandas 2's class and pandas 3's function; the note still says to check the signature.
    old = write_tree(tmp_path / "a", FRAMES_V1)
    new = write_tree(tmp_path / "b", FRAMES_V2)
    raw = diff_sources("frames", "1", old, "2", new, ["frames"])
    (kind,) = [c for c in map(APIChange.from_dict, raw) if c.kind == KIND_CHANGED]
    assert (kind.name, kind.old_kind, kind.new_kind) == ("option_context", "class", "function")
    same = code("import frames as fr\nwith fr.option_context('mode', True):\n    pass\n")
    [use] = uses(kind, same)
    assert (use.kind, use.names, use.form) == (USE_CALL, ("option_context",), USES_API)
    assert form(kind, ("option_context",)) == USES_API
    # A keyword the function does not take (``option_context(*args)``) is the old form.
    refused = code("from frames import option_context\noption_context(mode=True)\n")
    [use] = uses(kind, refused)
    assert (use.kind, use.names, use.form) == (USE_KEYWORD, ("option_context", "mode"), OLD_FORM)
    # Any other kind change stays the old form.
    kind.new_kind = "attribute"
    [use] = uses(kind, same)
    assert use.form == OLD_FORM
