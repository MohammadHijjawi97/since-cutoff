"""What the project actually uses: "your code uses" marks, imported packages, their order in
every report, and the versions of dependencies that nothing pins."""

from __future__ import annotations

import ast
import re
import textwrap
from datetime import date
from pathlib import Path

from rich.console import Console

from since_cutoff.apidiff import DEPRECATED, PARAM_REMOVED, REMOVED, APIChange
from since_cutoff.cache import DiskCache
from since_cutoff.engine import CHANGED, KNOWN, Engine, ModelTarget, ScanResult, Settings
from since_cutoff.mcp_server import Tools
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
from since_cutoff.selection import used_names
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
