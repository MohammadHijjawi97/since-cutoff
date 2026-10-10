"""Output robustness and messages: redirected and closed streams, narrow and piped tables, and
the wording of the reports (reasons, labels, counts)."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

import pytest
from rich.console import Console

from since_cutoff import cli, mcp_server, report
from since_cutoff import engine as engine_module
from since_cutoff import pypi as pypi_module
from since_cutoff.apidiff import PARAM_REMOVED, REMOVED, APIChange
from since_cutoff.cache import DiskCache
from since_cutoff.engine import (
    CHANGED,
    KNOWN,
    NEW,
    SKIPPED,
    UNCHANGED,
    Engine,
    ModelTarget,
    PackageScan,
    ScanResult,
    Settings,
)
from since_cutoff.errors import (
    ModelLookupError,
    NoCodeError,
    PackageIndexError,
    ProjectError,
    ProviderError,
)
from since_cutoff.mcp_server import Target, Tools
from since_cutoff.models import ModelRegistry
from since_cutoff.project import Project, load_project
from since_cutoff.providers import check_spec
from since_cutoff.pypi import PyPI, Release, SourceTree
from since_cutoff.report import headline, render_console
from tests.conftest import (
    ALIASLIB_V1,
    ALIASLIB_V2,
    TOYLIB_V1,
    TOYLIB_V2,
    FakePyPI,
    write_tree,
)
from tests.test_ci import make_app, scan_app

ROOT = Path(__file__).resolve().parents[1]
LATEST = "latest on PyPI, as nothing is pinned"
CUTOFF = ["--cutoff", "2025-07-31"]

# The CLI in a child process, with the toy library on a fake PyPI and a throwaway cache.
DRIVER = """
import sys
from pathlib import Path

sys.path.insert(0, {root!r})
from tests.conftest import TOYLIB_V1, TOYLIB_V2, FakePyPI, write_tree
from since_cutoff import cli
from since_cutoff.cache import DiskCache
from since_cutoff.engine import Engine
from since_cutoff.pypi import SourceTree

work = Path({work!r})
v1 = write_tree(work / "toylib-1.0", TOYLIB_V1)
v2 = write_tree(work / "toylib-2.0", TOYLIB_V2)
pypi = FakePyPI(
    DiskCache(work / "cache"),
    {{"toylib": [("0.9", "2024-06-01"), ("1.0", "2025-01-10"), ("2.0", "2025-10-01")]}},
    {{
        ("toylib", "1.0"): SourceTree("toylib", "1.0", v1, ("toylib",)),
        ("toylib", "2.0"): SourceTree("toylib", "2.0", v2, ("toylib",)),
    }},
)
cli.Engine = lambda settings, **kwargs: Engine(settings, **{{**kwargs, "pypi": pypi}})
raise SystemExit(cli.main(sys.argv[1:]))
"""


@pytest.fixture
def driver(tmp_path: Path) -> list[str]:
    script = tmp_path / "driver.py"
    script.write_text(DRIVER.format(root=str(ROOT), work=str(tmp_path / "work")), encoding="utf-8")
    return [sys.executable, str(script)]


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("PYTHONIOENCODING", "PYTHONUTF8", "PYTHONUNBUFFERED")
    }
    env.update(SINCE_CUTOFF_CACHE=str(tmp_path / "cli-cache"), **extra)
    env.pop("COLUMNS", None)
    return env


def _clean(stderr: str) -> None:
    for sign in ("Traceback", "Exception ignored", "UnicodeEncodeError", "lost sys.stderr"):
        assert sign not in stderr, stderr


# ---------------------------------------------------------- redirected output
def test_output_to_a_file_is_complete_and_not_cut_at_80_columns(tmp_path, driver) -> None:
    app = make_app(tmp_path).rename(
        tmp_path / "a-project-with-a-long-directory-name-for-the-subtitle"
    )
    out, err = tmp_path / "out.txt", tmp_path / "err.txt"
    with out.open("wb") as stdout, err.open("wb") as stderr:
        code = subprocess.run(
            [*driver, "scan", str(app), *CUTOFF, "--fail-on-changes", "--all"],
            stdout=stdout,
            stderr=stderr,
            env=_env(tmp_path, PYTHONIOENCODING="ascii"),
            timeout=120,
        ).returncode
    _clean(err.read_text(encoding="utf-8"))
    assert code == 3  # --fail-on-changes still works
    text = out.read_text(encoding="utf-8")  # UTF-8, whatever the code page says
    assert "• Checking 1 dependency on PyPI" in text
    # The subtitle is 99 characters: at rich's default of 80 columns it was cut.
    assert (
        "a-project-with-a-long-directory-name-for-the-subtitle · 1 dependency · "
        "versions from pyproject.toml" in text
    )


@pytest.mark.skipif(sys.platform != "win32", reason="the NUL device is Windows'")
def test_output_to_nul_does_not_crash(tmp_path, driver) -> None:
    """Windows' NUL claims to be a TTY: the progress spinner went to it and could not be
    encoded (exit 1, "lost sys.stderr"), which broke --fail-on-changes in CI."""
    app = make_app(tmp_path)
    for extra, expected in (([], 0), (["--fail-on-changes"], 3)):
        with Path(os.devnull).open("w") as null:
            done = subprocess.run(
                [*driver, "scan", str(app), *CUTOFF, *extra],
                stdout=null,
                stderr=subprocess.PIPE,
                env=_env(tmp_path, PYTHONIOENCODING="ascii"),
                timeout=120,
            )
        _clean(done.stderr.decode("utf-8", "replace"))
        assert done.returncode == expected


@pytest.mark.parametrize("unbuffered", [False, True], ids=["buffered", "unbuffered"])
@pytest.mark.parametrize(
    "args",
    [
        ["scan", "APP", *CUTOFF],
        ["scan", "APP", *CUTOFF, "--json"],
        ["--help"],
        ["scan", "--help"],
        ["--version"],
    ],
    ids=["scan", "json", "help", "scan-help", "version"],
)
def test_a_closed_pipe_ends_the_run_quietly(tmp_path, driver, args, unbuffered) -> None:
    """``since-cutoff scan | head``: the reader goes away. No traceback and no exit code 120
    from Python's last flush; the shell's code for a writer stopped by SIGPIPE instead.

    With PYTHONUNBUFFERED (set in many containers) argparse's own write of the help or the
    version hit the closed pipe: Python 3.11 and later ignored it and exited with 0, 3.10
    printed a BrokenPipeError traceback and exited with 1."""
    app = make_app(tmp_path)
    env = _env(tmp_path, **({"PYTHONUNBUFFERED": "1"} if unbuffered else {}))
    proc = subprocess.Popen(
        [*driver, *(str(app) if a == "APP" else a for a in args)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert proc.stdout is not None and proc.stderr is not None
    proc.stdout.close()  # before the child writes anything
    stderr = proc.stderr.read().decode("utf-8", "replace")
    assert proc.wait(timeout=120) == cli.OUTPUT_CLOSED
    _clean(stderr)


def test_a_long_diff_says_how_far_it_got_in_a_log() -> None:
    """A cold scan of private-gpt printed only "Diffing the API of 71 packages" to a file or
    pipe, then nothing for minutes."""
    out = io.StringIO()
    reporter = cli.RichReporter(Console(file=out, width=160))
    reporter.stage("Checking 3 dependencies on PyPI", 3)
    for _ in range(3):
        reporter.advance()  # quick steps with no package to name print nothing
    reporter.stage("Diffing the API of 71 packages released after their cutoff version", 71)
    for i in range(71):
        reporter.advance(label="transformers" if i == 39 else f"pkg{i}")
    reporter.done()
    lines = out.getvalue().splitlines()
    progress = [line.strip() for line in lines if line.strip().startswith("diffed")]
    assert lines[:2] == [
        "• Checking 3 dependencies on PyPI",
        "• Diffing the API of 71 packages released after their cutoff version",
    ]
    assert progress[0] == "diffed 7/71 (pkg6)" and progress[-1] == "diffed 71/71 (pkg70)"
    assert "diffed 42/71 (pkg41)" in progress and 8 <= len(progress) <= 12


def test_a_terminal_that_is_no_console_gets_utf8(monkeypatch) -> None:
    """A stream that claims to be a TTY but cannot encode the output (NUL on Windows, a
    terminal with an ASCII locale) must not crash on the bullets and the spinner."""

    class Claims(io.TextIOWrapper):
        def isatty(self) -> bool:
            return True

    stream = Claims(io.BytesIO(), encoding="ascii")
    monkeypatch.setattr(sys, "stdout", stream)
    cli._utf8_streams()
    stream.write("• ⠋ done\n")
    stream.flush()


def test_the_markdown_on_stdout_keeps_the_report_off_stderr(tmp_path, capsys, monkeypatch):
    _fake_cli(monkeypatch, tmp_path)
    app = make_app(tmp_path)
    assert cli.main(["scan", str(app), *CUTOFF, "--markdown", "-"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("## since-cutoff scan")
    # As with --json: progress and the report's path, not the whole report a second time.
    assert "Checking 1 dependency on PyPI" in captured.err and "Full report" in captured.err
    assert "since-cutoff · " not in captured.err and "toylib 1.0 -> 2.0" not in captured.err


# ----------------------------------------------------------------- the table
def _long_names_scan(tmp_path: Path) -> ScanResult:
    """A scan with a 39-character package name, released before the cutoff (shown with -v)."""
    name = "pytest-github-actions-annotate-failures"
    root = tmp_path / "long"
    root.mkdir()
    (root / "requirements.txt").write_text(f"{name}==0.1.8\ntoylib==2.0\n", encoding="utf-8")
    pypi = FakePyPI(
        DiskCache(tmp_path / "long-cache"),
        {name: [("0.1.8", "2021-01-01"), ("0.3.0", "2025-01-01")], "toylib": []},
        {},
    )
    engine = Engine(Settings(), store=pypi.cache, llm_cache=pypi.cache, pypi=pypi)
    scan = engine.scan(load_project(root), ModelTarget.cutoff_only(date(2025, 7, 31)))
    toylib = scan.package("toylib")
    toylib.reason = "toylib==2.0 is 192 MB, above the 80 MB download limit (--max-download-mb)"
    return scan


@pytest.mark.parametrize("width", [60, 80])
def test_a_long_package_name_leaves_room_for_status_and_changes(tmp_path, width) -> None:
    scan = _long_names_scan(tmp_path)
    console = Console(width=width, soft_wrap=True, record=True)
    render_console(console, scan, verbose=True)
    lines = console.export_text().splitlines()
    assert all(len(line) <= width for line in lines)
    header = next(line for line in lines if line.startswith("package"))
    assert "status" in header and "breaking" in header  # used to shrink to nothing
    table = " ".join(" ".join(line.split("│")) for line in lines if "│" in line)
    words = table.split()
    # The status of the long-named row is all there, whole words or folded pieces.
    body = [line.split("│") for line in lines[lines.index(header) + 2 :] if "│" in line]
    long_row = body[: next(i for i, cells in enumerate(body) if cells[0].startswith("toylib"))]
    assert "".join("".join(cells[3].split()) for cells in long_row) == "releasedbeforecutoff"
    # The flag in the skip reason is folded, never cut: its pieces add up to all of it.
    assert "--max-download-mb" in "".join(words)


def test_an_80_column_table_keeps_rows_short_and_names_whole(tmp_path) -> None:
    """python-sdk at 80 columns: every changed row took three lines ("API changed, / imported
    by / your code") and names broke as "opentelemetr" / "y-sdk"."""
    packages = []
    for name, locked, cutoff in (
        ("cryptography", "50.0.0", "44.0.1"),
        ("opentelemetry-sdk", "1.39.1", "1.30.0"),
        ("pydantic-settings", "2.10.1", "2.8.1"),
    ):
        p = PackageScan(name, locked, "uv.lock", True, True, CHANGED, cutoff_version=cutoff)
        p.changes = [APIChange(name, cutoff, locked, REMOVED, "x.y", "y")]
        packages.append(p)
    packages.append(PackageScan("zensical", "0.0.50", "uv.lock", True, status=NEW))
    scan = ScanResult(
        Project(tmp_path, [], "uv.lock"), ModelTarget.cutoff_only(date(2025, 2, 28)), packages
    )
    console = Console(width=80, record=True)
    render_console(console, scan)
    lines = console.export_text().splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("package"))
    rows = [[c.strip() for c in line.split("│")] for line in lines[header + 2 :] if "│" in line]
    assert all(len(line) <= 80 for line in lines)
    assert [r[0] for r in rows] == [
        "cryptography",
        "opentelemetry-",
        "sdk",
        "pydantic-",
        "settings",
        "zensical",
    ]
    assert [r[3] for r in rows] == ["changed, imported", *["changed, imported", ""] * 2, "new"]
    # One column narrower, the "-" goes to the next line rather than alone on its own.
    assert report._fold_at_hyphens("opentelemetry-sdk", 13) == "opentelemetry\n-sdk"
    # Wide terminals keep the full labels.
    wide = Console(width=160, record=True)
    render_console(wide, scan)
    assert "API changed, imported by your code" in wide.export_text()


def test_a_skip_reason_is_cut_at_a_word_and_says_so() -> None:
    reason = "x==1 is 201 MB, above the 80 MB download limit " + "(--max-download-mb) " * 6
    assert report.clip(reason, 200) == " ".join(reason.split())
    short = report.clip(reason, 80)
    assert len(short) <= 80 and short.endswith("...")
    assert short[:-3].split()[-1] in reason.split()  # a whole word before the "..."


# ------------------------------------------------------------------ wording
def test_other_paths_of_a_change_do_not_read_like_replacements() -> None:
    """ "12 similar, e.g. `setuptools.command.alias.alias.ensure_string_list`" read like a
    replacement, next to "similar names now", but named another path that lost the name."""
    gone = APIChange(
        "setuptools",
        "75.8.2",
        "80.9.0",
        REMOVED,
        "setuptools.Command.ensure_string_list",
        "ensure_string_list",
        "Command",
        suggestions=["ensure_string"],
        occurrences=13,
        also=["setuptools.command.alias.alias.ensure_string_list"],
    )
    assert mcp_server.change_line(gone) == (
        "`setuptools.Command.ensure_string_list` was removed; similar names in 80.9.0, not "
        "confirmed as replacements: `ensure_string`; also removed under 12 other paths, e.g. "
        "`setuptools.command.alias.alias.ensure_string_list`"
    )
    assert report._md_change(gone, ()) == (
        "- `setuptools.Command.ensure_string_list` was removed (similar names in 80.9.0, not "
        "confirmed as replacements: `ensure_string`) (also removed under 12 other paths)"
    )


def test_a_renamed_parameter_shows_its_new_name_in_every_report(tmp_path) -> None:
    """markdown-it-py 4: parseLinkTitle's `pos` became `start`; only MCP said so. It is a
    parameter renamed in place (the diff's ``renamed``), so a probable rename, and says so."""
    renamed = APIChange(
        "markdown-it-py",
        "3.0.0",
        "4.0.0",
        PARAM_REMOVED,
        "markdown_it.helpers.parseLinkTitle",
        "parseLinkTitle",
        parameter="pos",
        suggestions=["start"],
        renamed=True,
    )
    scan = ScanResult(
        Project(tmp_path, [], "uv.lock"),
        ModelTarget.cutoff_only(date(2025, 2, 28)),
        [
            PackageScan(
                "markdown-it-py",
                "4.0.0",
                "uv.lock",
                True,
                status=CHANGED,
                cutoff_version="3.0.0",
                changes=[renamed],
            )
        ],
    )
    console = Console(width=200, record=True)
    report.render_scan_changes(console, scan)
    text = "parameter pos was removed (probably renamed to start (same position and type))"
    assert text in console.export_text()
    new_name = "parameter `pos` was removed (probably renamed to `start` (same position and type))"
    assert new_name in report.render_scan_markdown(scan)
    assert new_name in report.render_markdown(scan)


def test_counts_of_one_are_singular(tmp_path, cache, fake_pypi) -> None:
    scan = scan_app(make_app(tmp_path, pin="toylib==1.0"), cache, fake_pypi, date(2025, 7, 31))
    console = Console(width=200, record=True)
    render_console(console, scan)
    text = console.export_text()
    assert "0 of 1 dependency changed its API after the cutoff" in text
    assert "app · 1 dependency · " in text
    # Nothing is listed above it, so it is not "1 more".
    assert "1 dependency was not flagged (released before the cutoff" in text

    new = scan_app(make_app(tmp_path / "new"), cache, fake_pypi, date(2024, 1, 1))
    assert "1 dependency did not exist yet at the cutoff" in [t.plain for t in headline(new, None)]


def test_one_unchecked_dependency_is_not_an_example(tmp_path, cache, fake_pypi) -> None:
    scan = scan_app(make_app(tmp_path), cache, fake_pypi, date(2025, 7, 31))
    scan.packages[0].status, scan.packages[0].reason = SKIPPED, "installed from git, not from PyPI"
    lines = [t.plain for t in headline(scan, None)]
    assert "1 of 1 dependency could not be checked: installed from git, not from PyPI" in lines


def test_a_git_reference_is_named_in_the_skip_reason(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0"\ndependencies = [\n'
        '  "dacvae @ git+https://user:secret@github.com/facebookresearch/dacvae.git@v1",\n'
        '  "local-lib @ file:///srv/local-lib",\n]\n'
        '[tool.uv.sources]\nshared = { path = "../shared" }\n',
        encoding="utf-8",
    )
    deps = {d.name: d for d in load_project(tmp_path).dependencies}
    # Nested parentheses and "direct URL" before; the credentials never show.
    assert deps["dacvae"].non_pypi_reason == (
        "installed from git+https://github.com/facebookresearch/dacvae.git@v1, not from PyPI"
    )
    assert (
        deps["local-lib"].non_pypi_reason == "installed from file:///srv/local-lib, not from PyPI"
    )
    engine = Engine(Settings(), store=DiskCache(tmp_path / "c"), llm_cache=DiskCache(tmp_path))
    scan = engine._scan_versions(
        deps["dacvae"], ModelTarget.cutoff_only(date(2025, 1, 1), margin=0), load_project(tmp_path)
    )
    assert scan.status == SKIPPED and "secret" not in (scan.reason or "")


def test_a_locked_git_source_is_named_and_not_looked_up(tmp_path, cache, fake_pypi) -> None:
    """python-sdk locks strict-no-cover from git: it said "installed from git" without the
    URL, and "Checking 42 dependencies on PyPI" counted it."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0"\ndependencies = ["toylib", "strict-no-cover"]\n'
    )
    (tmp_path / "uv.lock").write_text(
        'version = 1\n[[package]]\nname = "app"\nversion = "0"\nsource = { virtual = "." }\n'
        'dependencies = [{ name = "toylib" }, { name = "strict-no-cover" }]\n'
        '[[package]]\nname = "toylib"\nversion = "2.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        '[[package]]\nname = "strict-no-cover"\nversion = "0.1.1"\nsource = { git = '
        '"https://github.com/pydantic/strict-no-cover#7fc59da2c4dff919db2095a0f0e47101b657131d" }\n'
    )
    project = load_project(tmp_path)
    deps = {d.name: d for d in project.dependencies}
    assert deps["strict-no-cover"].non_pypi_reason == (
        "installed from git+https://github.com/pydantic/strict-no-cover@7fc59da2c4df, not from PyPI"
    )
    stages: list[str] = []

    class Stages(engine_module.Reporter):
        def stage(self, title: str, total: int | None = None) -> None:
            stages.append(title)

    engine = Engine(Settings(), store=cache, llm_cache=cache, pypi=fake_pypi, reporter=Stages())
    scan = engine.scan(project, ModelTarget.cutoff_only(date(2025, 7, 31)))
    assert stages[0] == "Checking 1 dependency on PyPI"
    assert [p.name for p in scan.packages] == ["toylib", "strict-no-cover"]

    poetry = tmp_path / "poetry"
    poetry.mkdir()
    (poetry / "pyproject.toml").write_text('[tool.poetry.dependencies]\ntool = "*"\nrepo = "*"\n')
    (poetry / "poetry.lock").write_text(
        '[[package]]\nname = "tool"\nversion = "1.0"\n[package.source]\ntype = "git"\n'
        'url = "https://user:token@example.org/tool.git"\nreference = "main"\n'
        'resolved_reference = "0123456789abcdef0123"\n'
        '[[package]]\nname = "repo"\nversion = "2.0"\n[package.source]\ntype = "legacy"\n'
        'url = "https://pypi.acme.corp/simple"\nreference = "acme"\n'
    )
    deps = {d.name: d for d in load_project(poetry).dependencies}
    assert deps["tool"].non_pypi == "git+https://example.org/tool.git@0123456789ab"
    assert deps["repo"].non_pypi == "private index"


def test_a_transitive_dependency_the_code_imports_is_checked(tmp_path, cache, fake_pypi) -> None:
    """pypistats.org imports alembic and sqlalchemy, which pip-compile pins "via
    flask-migrate": without --all-deps they would not be checked at all."""
    (tmp_path / "requirements.txt").write_text(
        "flask-migrate==4.1.0\n    # via -r requirements.in\n"
        "toylib==2.0\n    # via flask-migrate\nkombu==5.5.4\n    # via flask-migrate\n"
    )
    (tmp_path / "app.py").write_text("import toylib\n")
    engine = Engine(Settings(), store=cache, llm_cache=cache, pypi=fake_pypi)
    scan = engine.scan(load_project(tmp_path), ModelTarget.cutoff_only(date(2025, 7, 31)))
    assert sorted(p.name for p in scan.packages) == ["flask-migrate", "toylib"]  # not kombu
    assert scan.package("toylib").status == CHANGED


@pytest.mark.parametrize(
    ("files", "label"),
    [
        ({"pyproject.toml": ["requests", "rich"]}, LATEST),
        ({"Pipfile": ["requests", "rich"]}, LATEST),
        ({"requirements.txt": ["requests==2.32.3", "rich==13.9.4"]}, "requirements.txt"),
        (
            {"pyproject.toml": ["requests==2.32.3", "rich"]},
            "pyproject.toml, latest on PyPI for 1 unpinned",
        ),
        (
            {"pyproject.toml": ["requests"], "requirements/prod.txt": ["requests==2.32.3"]},
            "requirements/prod.txt",
        ),
    ],
)
def test_the_version_source_says_where_the_versions_come_from(tmp_path, files, label) -> None:
    """A project that pins nothing is checked at the latest releases on PyPI: its label used
    to say "requirements" even without a requirements file."""
    for name, deps in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name == "pyproject.toml":
            listed = ", ".join(f'"{d}"' for d in deps)
            path.write_text(f'[project]\nname = "app"\ndependencies = [{listed}]\n')
        elif name == "Pipfile":
            path.write_text("[packages]\n" + "".join(f'{d} = "*"\n' for d in deps))
        else:
            path.write_text("\n".join(deps) + "\n")
    assert load_project(tmp_path).version_source == label


def test_a_setup_py_project_is_told_what_is_read(tmp_path) -> None:
    (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup(name='x')\n")
    with pytest.raises(ProjectError) as info:
        load_project(tmp_path)
    message = str(info.value)
    assert "It does not read setup.py" in message and "Pipfile" in message


def test_all_deps_without_a_lockfile_says_it_cannot_add_anything(tmp_path, capsys, monkeypatch):
    _fake_cli(monkeypatch, tmp_path)
    app = make_app(tmp_path)
    assert cli.main(["scan", str(app), *CUTOFF, "--all-deps"]) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "--all-deps: without a lockfile (uv.lock, poetry.lock, ...) or a virtual" in out
    md = (app / ".since-cutoff" / "report.md").read_text(encoding="utf-8")
    assert "- Warning: --all-deps: without a lockfile" in md

    (app / "uv.lock").write_text(
        'version = 1\n[[package]]\nname = "app"\nversion = "0"\nsource = { virtual = "." }\n'
        'dependencies = [{ name = "toylib" }]\n[[package]]\nname = "toylib"\nversion = "2.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )
    assert cli.main(["scan", str(app), *CUTOFF, "--all-deps"]) == 0
    assert "--all-deps:" not in capsys.readouterr().out


# ---------------------------------------------------------------------- PyPI
class _OneFile(PyPI):
    """PyPI with a single release, of a single file."""

    def __init__(self, cache: DiskCache, version: str, file: dict[str, Any], **kw: Any) -> None:
        super().__init__(cache, **kw)
        self.version, self.file = version, file

    def releases(self, name: str) -> list[Release]:
        when = datetime(2025, 1, 1, tzinfo=timezone.utc)
        return [Release(self.version, when, False, (self.file,))]


@pytest.mark.parametrize(("limit", "shown"), [(80.0, "80 MB"), (80.5, "80.5 MB")])
def test_the_download_limit_is_shown_as_given(tmp_path, limit, shown) -> None:
    wheel = {"filename": "big-2.0-py3-none-any.whl", "size": 90 * 1024 * 1024, "url": "x"}
    pypi = _OneFile(DiskCache(tmp_path), "2.0", wheel, max_download_mb=limit)
    with pytest.raises(PackageIndexError) as info:
        pypi.source("big", "2.0")
    # MiB against MB: the limit read 84 MB and the file 94 MB.
    assert str(info.value) == (
        f"big==2.0 is 90 MB, above the {shown} download limit (--max-download-mb)"
    )


def test_a_metapackage_says_where_its_code_is(tmp_path, monkeypatch) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "docling-2.129.0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: docling\nVersion: 2.129.0\n"
            "Requires-Dist: docling-slim[standard]==2.129.0\n"
            'Requires-Dist: docling-extra; extra == "all"\n\n',
        )
        zf.writestr("docling-2.129.0.dist-info/RECORD", "")
    monkeypatch.setattr(pypi_module.net, "request", lambda *a, **k: buf.getvalue())
    wheel = {"filename": "docling-2.129.0-py3-none-any.whl", "size": 5229, "url": "x"}
    with pytest.raises(PackageIndexError) as info:
        _OneFile(DiskCache(tmp_path), "2.129.0", wheel).source("docling", "2.129.0")
    assert str(info.value) == (
        "docling 2.129.0 is a metapackage without code of its own: it installs "
        "docling-slim==2.129.0; check that package instead"
    )


def _wheel(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


@pytest.mark.parametrize(
    "top_level",
    [
        "adodbapi\npythonwin\\pywin\nwin32\\lib\\win32con\nwin32\\lib\\pywintypes\nwin32\\win32api\n",
        "adodbapi\npythonwin\nwin32\\lib\\win32con\nwin32\\lib\\pywintypes\nwin32api\n",  # 311
    ],
)
def test_modules_a_pth_file_puts_on_the_path_get_their_import_names(
    tmp_path, monkeypatch, top_level
):
    """pywin32's top_level.txt lists ``win32\\lib\\win32con`` and ``pythonwin\\pywin`` (or just
    ``pythonwin``); its .pth file puts those directories on sys.path, so the code imports
    win32con and pywin."""
    blob = _wheel(
        {
            "pywin32.pth": "# .pth file for the PyWin32 extensions\r\nwin32\r\nwin32\\lib\r\n"
            "Pythonwin\r\nimport pywin32_bootstrap\r\n",
            "pywin32-311.dist-info/top_level.txt": top_level,
            "adodbapi/__init__.py": "def connect(dsn):\n    pass\n",
            "pythonwin/pywin/__init__.py": "",
            "pythonwin/pywin/scintilla/__init__.py": "",
            "pythonwin/pywin/scintilla/scintillacon.py": "SCI_START = 2000\n",
            "win32/lib/win32con.py": "WM_USER = 1024\n",
            "win32/lib/pywintypes.py": "def __import_pywin32_system_module__(name, globs):\n"
            "    pass\n",
            "win32/win32api.pyd": "",
        }
    )
    monkeypatch.setattr(pypi_module.net, "request", lambda *a, **k: blob)
    wheel = {"filename": "pywin32-311-cp313-cp313-win_amd64.whl", "size": len(blob), "url": "x"}
    tree = _OneFile(DiskCache(tmp_path), "311", wheel).source("pywin32", "311")
    assert tree.import_names == ("adodbapi", "pywin", "pywintypes", "win32con")
    assert (tree.root / "pywin" / "scintilla" / "scintillacon.py").is_file()
    # Compiled modules are named from the .pth directories too: win32/win32api.pyd is win32api.
    assert tree.compiled == ("win32api",)
    # The diff finds the modules where they are imported from.
    old = write_tree(tmp_path / "old", {"win32con.py": "WM_USER = 1024\nWM_OLD = 1\n"})
    changes = _diff_trees(old, tree.root, ["win32con"])
    assert [c["path"] for c in changes] == ["win32con.WM_OLD"]


def test_a_stub_only_distribution_is_imported_under_the_stubbed_name(tmp_path, monkeypatch):
    """types-cachetools ships cachetools-stubs/: code that imports cachetools uses it."""
    blob = _wheel(
        {
            "types_cachetools-6.2.0.dist-info/top_level.txt": "cachetools-stubs\n",
            "cachetools-stubs/__init__.pyi": "def cached(cache: object) -> object: ...\n",
            "cachetools-stubs/keys.pyi": "def hashkey(*args: object) -> tuple: ...\n",
        }
    )
    monkeypatch.setattr(pypi_module.net, "request", lambda *a, **k: blob)
    wheel = {"filename": "types_cachetools-6.2.0-py3-none-any.whl", "size": 99, "url": "x"}
    tree = _OneFile(DiskCache(tmp_path), "6.2.0", wheel).source("types-cachetools", "6.2.0")
    assert tree.import_names == ("cachetools",)
    root = tmp_path / "app"
    root.mkdir()
    (root / "requirements.txt").write_text("types-cachetools\n")
    (root / "app.py").write_text("from cachetools import cached\n")
    assert load_project(root).imports(tree.import_names)
    # The stubs are still what the diff reads.
    old = write_tree(
        tmp_path / "old",
        {
            "cachetools-stubs/__init__.pyi": "def cached(cache: object) -> object: ...\n"
            "def cachedmethod(cache: object) -> object: ...\n"
        },
    )
    changes = _diff_trees(old, tree.root, ["cachetools"])
    assert [c["path"] for c in changes] == ["cachetools.cachedmethod"]


def _diff_trees(old: Path, new: Path, names: list[str]) -> list[dict[str, Any]]:
    from since_cutoff.apidiff import diff_sources

    return diff_sources("pkg", "1", old, "2", new, names)


def test_a_package_with_only_prereleases_existed_at_the_cutoff(cache) -> None:
    """sqlgpt-parser (0.0.1a0-a5, September 2023) was reported as first released after a
    2025 cutoff, because only final releases counted."""
    pypi = FakePyPI(
        cache,
        {
            "sqlgpt-parser": [("0.0.1a0", "2023-09-01"), ("0.0.1a5", "2023-09-20")],
            "beta-only": [
                ("0.50b0", "2024-12-01"),
                ("0.51b0", "2025-02-01"),
                ("0.62b1", "2026-01-01"),
            ],
            "mixed": [("1.0", "2024-01-01"), ("1.1rc1", "2025-01-01")],
        },
        {},
    )
    cutoff = date(2025, 2, 28)
    assert pypi.version_at("sqlgpt-parser", cutoff).version == "0.0.1a5"
    assert pypi.version_at("beta-only", cutoff).version == "0.51b0"
    assert pypi.version_at("mixed", cutoff).version == "1.0"  # a final release still wins
    assert pypi.version_at("sqlgpt-parser", date(2023, 1, 1)) is None
    # Unpinned, such a package gets its newest pre-release, as pip and uv install it.
    assert pypi.latest("beta-only").version == "0.62b1"
    assert pypi.latest("mixed").version == "1.0"

    root = cache.root.parent / "pre"
    root.mkdir()
    (root / "requirements.txt").write_text("sqlgpt-parser==0.0.1a5\nbeta-only\n")
    project = load_project(root)
    deps = {d.name: d for d in project.dependencies}
    engine = Engine(Settings(), store=cache, llm_cache=cache, pypi=pypi)
    at_cutoff = ModelTarget.cutoff_only(cutoff, margin=0)
    parser = engine._scan_versions(deps["sqlgpt-parser"], at_cutoff, project)
    assert parser.status == KNOWN  # not "first released after the cutoff"
    beta = engine._scan_versions(deps["beta-only"], at_cutoff, project)  # not "no final releases"
    assert (beta.locked, beta.cutoff_version, beta.status) == ("0.62b1", "0.51b0", CHANGED)


class _PurePyPI(FakePyPI):
    """FakePyPI whose releases are small pure-Python wheels, except those in ``compiled``."""

    compiled: ClassVar[set[tuple[str, str]]] = set()

    def releases(self, name: str) -> list[Release]:
        out = []
        for r in super().releases(name):
            tag = "cp313-win_amd64" if (name, r.version) in self.compiled else "py3-none-any"
            wheel = {"filename": f"{name}-{r.version}-{tag}.whl", "size": 947}
            out.append(Release(r.version, r.uploaded, r.yanked, (wheel,)))
        return out

    def source(self, name: str, version: str) -> SourceTree:
        tree = self._trees[(name, version)]
        if not tree.import_names:  # nvidia-cuda-runtime 0.0.1.dev5: a 7.9 KB sdist, no code
            raise NoCodeError(f"could not find importable modules in {name}-{version}.tar.gz")
        return tree


def test_a_placeholder_release_at_the_cutoff_means_the_api_is_new(tmp_path, cache) -> None:
    """nvidia-cuda-runtime reserved its name with 0.0.1.dev5 in 2021 and zensical with an
    empty 0.0.0 in 2024; both were compared with those, so the first was "not checked" and
    the second had "no breaking changes"."""
    real = write_tree(tmp_path / "real", {"zensical/__init__.py": "def build(config):\n    pass\n"})
    empty = write_tree(tmp_path / "empty", {"zensical/__init__.py": '"""Coming soon."""\n'})
    pypi = _PurePyPI(
        cache,
        {
            "nvidia-cuda-runtime": [("0.0.1.dev5", "2021-04-23"), ("13.0.96", "2025-08-01")],
            "reserved": [("0.0.1", "2021-04-23"), ("2.0", "2025-08-01")],
            "zensical": [("0.0.0", "2024-11-25"), ("0.0.50", "2026-07-09")],
            "compiled": [("1.0", "2024-11-25"), ("2.0", "2026-07-09")],
        },
        {
            ("nvidia-cuda-runtime", "13.0.96"): SourceTree("x", "13.0.96", real, ("zensical",)),
            ("reserved", "0.0.1"): SourceTree("reserved", "0.0.1", empty, ()),
            ("reserved", "2.0"): SourceTree("reserved", "2.0", real, ("zensical",)),
            ("zensical", "0.0.0"): SourceTree("zensical", "0.0.0", empty, ("zensical",)),
            ("zensical", "0.0.50"): SourceTree("zensical", "0.0.50", real, ("zensical",)),
            # An empty __init__.py next to a compiled extension is not a placeholder.
            ("compiled", "1.0"): SourceTree("compiled", "1.0", empty, ("zensical",)),
            ("compiled", "2.0"): SourceTree("compiled", "2.0", empty, ("zensical",)),
        },
    )
    pypi.compiled = {("compiled", "1.0"), ("compiled", "2.0")}
    cutoff = date(2025, 2, 28)
    assert pypi.version_at("nvidia-cuda-runtime", cutoff) is None  # a dev release is no baseline

    root = tmp_path / "app"
    root.mkdir()
    (root / "requirements.txt").write_text(
        "nvidia-cuda-runtime==13.0.96\nreserved==2.0\nzensical==0.0.50\ncompiled==2.0\n"
    )
    engine = Engine(Settings(), store=cache, llm_cache=cache, pypi=pypi)
    scan = engine.scan(load_project(root), ModelTarget.cutoff_only(cutoff))
    got = {p.name: (p.status, p.cutoff_version, p.reason) for p in scan.packages}
    assert got == {
        "nvidia-cuda-runtime": (NEW, None, "first released after the cutoff"),
        "reserved": (NEW, "0.0.1", "0.0.1 at the cutoff was an empty placeholder"),
        "zensical": (NEW, "0.0.0", "0.0.0 at the cutoff was an empty placeholder"),
        "compiled": (UNCHANGED, "1.0", None),
    }
    tools = Tools(cache, registry=ModelRegistry(cache, offline=True), pypi=pypi)
    out = tools.api_changes("zensical==0.0.50", cutoff="2025-02-28")
    assert "0.0.0 was an empty placeholder" in out and "breaking" not in out


# -------------------------------------------------------------------- models
def test_scan_takes_a_bare_model_id(tmp_path) -> None:
    cache = DiskCache(tmp_path)
    registry = ModelRegistry(cache, offline=True)

    def target(model: str, *, calls: bool = False) -> ModelTarget:
        engine = Engine(Settings(model=model), store=cache, llm_cache=cache, registry=registry)
        return engine.resolve_target(allow_calls=calls)

    found = target("claude-haiku-4-5")  # scan only needs the cutoff: no provider is called
    assert (found.model_id, found.cutoff, found.effort) == (
        "claude-haiku-4-5",
        date(2025, 2, 28),
        None,
    )
    assert target("sonnet").model_id.startswith("claude-sonnet-")
    with pytest.raises(
        ModelLookupError,
        match=r"unknown model 'claude-haiku-9'\. Close matches: .*claude-haiku-4-5",
    ):
        target("claude-haiku-9")
    with pytest.raises(ProviderError, match="'anthropic:claude-haiku-4-5' \\(the API\\)"):
        target("claude-haiku-4-5", calls=True)  # a run must know how to call it
    with pytest.raises(ModelLookupError, match=r"Close matches: .*claude-haiku-4-5"):
        registry.require("claude-haiku-9", "anthropic")


def test_the_run_hint_names_the_api_too() -> None:
    with pytest.raises(ProviderError) as info:
        check_spec("claude-haiku-4-5")
    assert "'claude-code:claude-haiku-4-5' (the Claude Code CLI)" in str(info.value)
    assert "'anthropic:claude-haiku-4-5' (the API)" in str(info.value)


# ------------------------------------------------------------------------ MCP
def _big_scan(tmp_path: Path, packages: int = 60, changes: int = 30) -> ScanResult:
    project = Project(tmp_path / "big", [], "uv.lock")
    scans = [
        PackageScan(f"pkg{i:02d}", "2.0", "uv.lock", True, status=CHANGED, cutoff_version="1.0")
        for i in range(packages)
    ]
    for s in scans:
        s.cutoff_version_date, s.locked_date = "2025-01-01", "2025-10-01"
        s.changes = [
            APIChange(
                s.name, "1.0", "2.0", REMOVED, f"{s.name}.module.function_{j}", f"function_{j}"
            )
            for j in range(changes)
        ]
    skipped = PackageScan("huge", "2.0", "uv.lock", True, reason="too big")
    return ScanResult(project, ModelTarget.cutoff_only(date(2025, 7, 31)), [*scans, skipped])


def test_project_changes_stays_short_for_a_big_project(tmp_path) -> None:
    """56 KB for 155 dependencies before: now the budget, and one line per package past it."""
    scan = _big_scan(tmp_path)
    text = mcp_server._render_project(scan, Target(date(2025, 7, 31), "given"), [], 10)
    assert len(text) <= mcp_server.PROJECT_BUDGET
    assert text.startswith("# big: 60 of 61 dependencies checked (versions from uv.lock)")
    detailed = text.count("\n## pkg")
    assert 0 < detailed < 60
    left = 60 - detailed
    assert f"## {left} more with API changes (left out to keep this answer short)" in text
    assert "- pkg59 1.0 -> 2.0: 30 breaking, 0 deprecated" in text
    assert 'project_changes(..., only=["pkg' in text

    small = _big_scan(tmp_path, packages=2)
    text = mcp_server._render_project(small, Target(date(2025, 7, 31), "given"), [], 10)
    assert "more with API changes" not in text and text.count("\n## pkg") == 2


@pytest.mark.parametrize(("packages", "changes"), [(45, 30), (60, 10), (80, 30)])
def test_the_one_line_list_counts_towards_the_budget(tmp_path, packages, changes) -> None:
    """private-gpt got 25,135 characters: the "22 more with API changes" list came on top."""
    scan = _big_scan(tmp_path, packages=packages, changes=changes)
    text = mcp_server._render_project(scan, Target(date(2025, 7, 31), "given"), [], 10)
    assert len(text) <= mcp_server.PROJECT_BUDGET
    assert "more with API changes" in text


def test_project_changes_needs_no_main_guard(tmp_path, cache, toylib, monkeypatch) -> None:
    """A script calling Tools().project_changes at top level: spawned workers import it again
    (RuntimeError on Windows). Tools diffs in-process unless told otherwise."""

    def no_pool(*args: object, **kwargs: object) -> None:
        raise AssertionError("Tools() must not start worker processes by default")

    monkeypatch.setattr(engine_module, "ProcessPoolExecutor", no_pool)
    root, pypi = _two_packages(tmp_path, cache, toylib)
    text = Tools(cache, registry=ModelRegistry(cache, offline=True), pypi=pypi).project_changes(
        str(root), cutoff="2025-07-31"
    )
    assert "## toylib 1.0" in text and "## aliaslib 1.0" in text


SCRIPT = """
import multiprocessing, sys
from pathlib import Path

sys.path.insert(0, {root!r})
multiprocessing.set_start_method("spawn", force=True)  # as on Windows and macOS
from tests.conftest import ALIASLIB_V1, ALIASLIB_V2, TOYLIB_V1, TOYLIB_V2, FakePyPI, write_tree
from since_cutoff.cache import DiskCache
from since_cutoff.mcp_server import Tools
from since_cutoff.models import ModelRegistry
from since_cutoff.pypi import SourceTree

work = Path({work!r})
trees = {{}}
for name, old, new in (("toylib", TOYLIB_V1, TOYLIB_V2), ("aliaslib", ALIASLIB_V1, ALIASLIB_V2)):
    for version, files in (("1.0", old), ("2.0", new)):
        path = write_tree(work / f"{{name}}-{{version}}", files)
        trees[(name, version)] = SourceTree(name, version, path, (name,))
dates = [("1.0", "2025-01-10"), ("2.0", "2025-10-01")]
cache = DiskCache(work / "cache")
pypi = FakePyPI(cache, {{"toylib": dates, "aliaslib": dates}}, trees)
tools = Tools(cache, registry=ModelRegistry(cache, offline=True), pypi=pypi)
text = tools.project_changes({app!r}, cutoff="2025-07-31")
print("RESULT", "## toylib 1.0" in text, "## aliaslib 1.0" in text)
"""


def test_a_script_without_a_main_guard_gets_its_answer_once(tmp_path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "requirements.txt").write_text("toylib==2.0\naliaslib==2.0\n")
    script = tmp_path / "unguarded.py"
    script.write_text(
        SCRIPT.format(root=str(ROOT), work=str(tmp_path / "work"), app=str(app)), encoding="utf-8"
    )
    done = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=180, check=False
    )
    assert "RuntimeError" not in done.stderr, done.stderr
    assert done.returncode == 0
    assert done.stdout.count("RESULT") == 1  # the script did not run again in workers
    assert "RESULT True True" in done.stdout


# ------------------------------------------------------------------- helpers
def _fake_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The CLI with the toy library on a fake PyPI and a throwaway cache."""
    v1 = write_tree(tmp_path / "t-1.0", TOYLIB_V1)
    v2 = write_tree(tmp_path / "t-2.0", TOYLIB_V2)
    pypi = FakePyPI(
        DiskCache(tmp_path / "fake-cache"),
        {"toylib": [("0.9", "2024-06-01"), ("1.0", "2025-01-10"), ("2.0", "2025-10-01")]},
        {
            ("toylib", "1.0"): SourceTree("toylib", "1.0", v1, ("toylib",)),
            ("toylib", "2.0"): SourceTree("toylib", "2.0", v2, ("toylib",)),
        },
    )
    monkeypatch.setenv("SINCE_CUTOFF_CACHE", str(tmp_path / "cli-cache"))
    monkeypatch.setattr(
        cli, "Engine", lambda settings, **kwargs: Engine(settings, **{**kwargs, "pypi": pypi})
    )


def _two_packages(
    tmp_path: Path, cache: DiskCache, toylib: tuple[SourceTree, SourceTree]
) -> tuple[Path, FakePyPI]:
    a1 = write_tree(tmp_path / "aliaslib-1.0", ALIASLIB_V1)
    a2 = write_tree(tmp_path / "aliaslib-2.0", ALIASLIB_V2)
    dates = [("1.0", "2025-01-10"), ("2.0", "2025-10-01")]
    pypi = FakePyPI(
        cache,
        {"toylib": dates, "aliaslib": dates},
        {
            ("toylib", "1.0"): toylib[0],
            ("toylib", "2.0"): toylib[1],
            ("aliaslib", "1.0"): SourceTree("aliaslib", "1.0", a1, ("aliaslib",)),
            ("aliaslib", "2.0"): SourceTree("aliaslib", "2.0", a2, ("aliaslib",)),
        },
    )
    root = tmp_path / "two"
    root.mkdir()
    (root / "requirements.txt").write_text("toylib==2.0\naliaslib==2.0\n")
    return root, pypi
