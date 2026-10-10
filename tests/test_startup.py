"""What the CLI imports at start.

``--help``, ``--version`` and ``cache`` should answer at once, and ``status --hook`` runs at
every Claude Code session start: none of them may pay for the engine, the API diff, the
reports and rich, which make up most of the CLI's import time (``since-cutoff --help`` took
400 ms before 0.6, 150 after). Each command runs in a fresh interpreter, since the test
process has everything imported already."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from since_cutoff import cli, notes

# What --help, --version and cache must not import: see the import-time profile in the
# changelog of 0.6 (engine 160 ms, baselines 90, apidiff 85, rich.console 60, ...).
HEAVY = (
    "since_cutoff.engine",
    "since_cutoff.apidiff",
    "since_cutoff.baselines",
    "since_cutoff.report",
    "since_cutoff.notes",
    "since_cutoff.checker",
    "since_cutoff.pypi",
    "since_cutoff.providers",
    "since_cutoff.progress",
    "since_cutoff.mcp_server",
    "since_cutoff.hosts",
    "since_cutoff.models",
    "rich",
    "rich.console",
    "rich.progress",
    "concurrent.futures.process",
    "multiprocessing",
    "griffe",
)
# status --hook reads the notes block (notes, apidiff) and the deps hash (engine, through
# sync), but prints without rich and never renders a report.
NOT_FOR_STATUS = (
    "since_cutoff.report",
    "since_cutoff.progress",
    "since_cutoff.mcp_server",
    "rich",
    "rich.console",
    "rich.progress",
)
# Records, for each module, the module (and line) whose import statement loaded it first, so
# that a failure names who imports a heavy module: the import machinery's frames are skipped.
WHO = """
import json, sys
importers = {}
class Who:
    def find_spec(self, name, path=None, target=None):
        frame = sys._getframe(1)
        while frame and frame.f_globals.get("__name__", "").startswith(
            ("importlib", "_frozen_importlib")
        ):
            frame = frame.f_back
        if frame and name not in importers:
            importers[name] = f"{frame.f_globals.get('__name__')} line {frame.f_lineno}"
        return None
sys.meta_path.insert(0, Who())
"""
SCRIPT = (
    WHO
    + """
import since_cutoff.cli as cli
try:
    code = cli.main(sys.argv[1:])
except SystemExit as exc:
    code = exc.code
"""
)
REPORT = """
print("MODULES", json.dumps({"code": code, "modules": sorted(sys.modules), "by": importers}))
"""


def run(args: list[str], cache: Path, script: str = SCRIPT) -> tuple[int, str, dict[str, str]]:
    """Exit code, stdout and the modules imported after ``since-cutoff <args>`` (or
    ``script``) in a fresh interpreter, each with the module and line that imported it."""
    env = {**os.environ, "SINCE_CUTOFF_CACHE": str(cache), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-c", script + REPORT, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=True,
        timeout=120,
    )
    out, marker = proc.stdout.rsplit("MODULES ", 1)
    result = json.loads(marker)
    return result["code"], out, {m: result["by"].get(m, "?") for m in result["modules"]}


def loaded(forbidden: tuple[str, ...], modules: dict[str, str]) -> list[str]:
    """The forbidden modules that were imported, each with who imported it."""
    return [f"{m}, imported by {modules[m]}" for m in forbidden if m in modules]


@pytest.mark.parametrize("args", [["--help"], ["--version"], ["cache", "path"], ["cache", "info"]])
def test_help_version_and_cache_import_neither_the_engine_nor_rich(tmp_path, args) -> None:
    code, out, modules = run(args, tmp_path / "cache")
    assert code == 0, out
    assert loaded(HEAVY, modules) == []
    assert "since_cutoff.cli" in modules


def test_a_heavy_import_names_the_module_that_made_it(tmp_path) -> None:
    """When a command imports what it must not, the failure says which module imported it
    (sync imports the engine at its top)."""
    _, _, modules = run([], tmp_path / "cache", WHO + "import since_cutoff.sync\ncode = 0\n")
    [engine] = loaded(("since_cutoff.engine",), modules)
    assert engine.startswith("since_cutoff.engine, imported by since_cutoff.sync line ")
    assert modules["since_cutoff.sync"].startswith("__main__ line ")


def test_help_is_the_full_help(tmp_path) -> None:
    code, out, _ = run(["--help"], tmp_path / "cache")
    assert code == 0
    assert out.startswith("usage: since-cutoff")
    assert "since-cutoff cache clear --sources" in out


def test_status_hook_imports_neither_rich_nor_the_reports(tmp_path) -> None:
    root = tmp_path / "app"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0"\ndependencies = ["toylib==2.0"]\n'
    )
    (root / "main.py").write_text("import toylib\n")
    code, out, modules = run(["status", "--hook", str(root)], tmp_path / "cache")
    assert (code, out) == (0, "")  # no block: nothing to say
    assert loaded(NOT_FOR_STATUS, modules) == []


def test_the_per_package_default_in_the_help_is_the_one_sync_uses() -> None:
    """``cli.IMPORTED_APIS`` is a copy of ``notes.IMPORTED_APIS``, so that --help does not
    import notes (and the API diff with it)."""
    assert cli.IMPORTED_APIS == notes.IMPORTED_APIS
    parser = cli.build_parser()
    [sub] = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    assert f"(default: {notes.IMPORTED_APIS}," in sub.choices["sync"].format_help()


def test_the_lazy_names_are_the_real_ones_once_called() -> None:
    """``cli.Engine``, ``cli.detect_model``, ``cli.write_tasks`` and ``cli.RichReporter`` are
    proxies that import on the first call, so that the tests can still replace them on cli."""
    from rich.console import Console

    from since_cutoff import hosts
    from since_cutoff.progress import RichReporter

    assert isinstance(cli.Engine, cli._LazyImport)
    assert isinstance(cli.RichReporter(Console()), RichReporter)
    assert cli.detect_model(Path()) == hosts.detect_model(Path())
