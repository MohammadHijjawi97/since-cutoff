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
SCRIPT = """
import json, sys
import since_cutoff.cli as cli
try:
    code = cli.main(sys.argv[1:])
except SystemExit as exc:
    code = exc.code
print("MODULES", json.dumps({"code": code, "modules": sorted(sys.modules)}))
"""


def run(args: list[str], cache: Path) -> tuple[int, str, list[str]]:
    """Exit code, stdout and the modules imported after ``since-cutoff <args>`` in a fresh
    interpreter."""
    env = {**os.environ, "SINCE_CUTOFF_CACHE": str(cache), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-c", SCRIPT, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=True,
        timeout=120,
    )
    out, marker = proc.stdout.rsplit("MODULES ", 1)
    result = json.loads(marker)
    return result["code"], out, result["modules"]


@pytest.mark.parametrize("args", [["--help"], ["--version"], ["cache", "path"], ["cache", "info"]])
def test_help_version_and_cache_import_neither_the_engine_nor_rich(tmp_path, args) -> None:
    code, out, modules = run(args, tmp_path / "cache")
    assert code == 0, out
    assert [m for m in HEAVY if m in modules] == []
    assert "since_cutoff.cli" in modules


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
    assert [m for m in NOT_FOR_STATUS if m in modules] == []


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
