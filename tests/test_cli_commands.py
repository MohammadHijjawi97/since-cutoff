"""The CLI: a scan that downloads from a local index, the cache, models, unapply and mcp
commands, the checks on its options, and its exit codes."""

from __future__ import annotations

import errno
import functools
import io
import json
import os
import runpy
import shutil
import sys
import textwrap
import threading
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from since_cutoff import __version__, cli
from since_cutoff import pypi as pypi_module
from since_cutoff.cache import DiskCache
from since_cutoff.engine import Engine
from since_cutoff.errors import ProviderError
from since_cutoff.models import slim_models_dev
from since_cutoff.providers.base import Completion
from tests.conftest import TOYLIB_V1, TOYLIB_V2, LocalServer, ScriptedModel
from tests.test_engine import make_project
from tests.test_pypi_download import Index, make_zip

CUTOFF = ["--cutoff", "2025-07-31"]


# -------------------------------------------------- a scan against a local index
def toylib_wheel(files: dict[str, str]) -> bytes:
    return make_zip({name: textwrap.dedent(text).lstrip() for name, text in files.items()})


@pytest.fixture
def local_index(http_server: LocalServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Index:
    """The real PyPI client, a throwaway cache, and toylib 1.0 and 2.0 on a local index."""
    monkeypatch.setenv("SINCE_CUTOFF_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(pypi_module, "PYPI_JSON", http_server.url + "/pypi/{name}/json")
    index = Index(http_server)
    for version, files, uploaded in (
        ("1.0", TOYLIB_V1, "2025-01-10"),
        ("2.0", TOYLIB_V2, "2025-10-01"),
    ):
        wheel = f"toylib-{version}-py3-none-any.whl"
        index.add("toylib", version, wheel, toylib_wheel(files), uploaded=f"{uploaded}T08:00:00Z")
    return index


def test_a_scan_downloads_each_version_once(local_index, tmp_path, capsys) -> None:
    root = make_project(tmp_path)
    assert cli.main(["scan", str(root), *CUTOFF]) == 0
    out = capsys.readouterr().out
    assert "toylib" in out and "1.0 -> 2.0" in out, out
    assert sorted(local_index.downloads) == [
        "/files/toylib-1.0-py3-none-any.whl",
        "/files/toylib-2.0-py3-none-any.whl",
    ]
    # Again, as JSON for a script: nothing is asked of PyPI, and stdout is only the JSON.
    asked = len(local_index.server.seen)
    assert cli.main(["scan", str(root), *CUTOFF, "--json"]) == 0
    captured = capsys.readouterr()
    assert len(local_index.server.seen) == asked
    result = json.loads(captured.out)
    toylib = next(p for p in result["scan"] if p["name"] == "toylib")
    versions = (toylib["locked"], toylib["cutoff_version"])
    assert toylib["status"] == "changed" and versions == ("2.0", "1.0")
    changes = {(c["path"], c["kind"]) for c in toylib["changes"]}  # diffed from the wheels
    assert {("toylib.legacy_fetch", "removed"), ("toylib.Client.send", "param_removed")} <= changes
    assert "Full report" in captured.err


def test_cache_clear_removes_everything_a_scan_cached(local_index, tmp_path, capsys) -> None:
    assert cli.main(["scan", str(make_project(tmp_path)), *CUTOFF]) == 0
    cache = tmp_path / "cache"
    assert {p.name for p in cache.iterdir()} >= {"pypi", "sources", "diffs"}
    capsys.readouterr()
    assert cli.main(["cache", "clear"]) == 0
    assert capsys.readouterr().out.startswith("cleared pypi, sources, diffs")
    assert not cache.exists()


def test_a_scan_that_can_check_nothing_fails_and_says_why(local_index, tmp_path, capsys) -> None:
    root = make_project(tmp_path)
    assert cli.main(["scan", str(root), *CUTOFF, "--max-download-mb", "0.0001"]) == 1
    out = " ".join(capsys.readouterr().out.split())  # undo the table's line breaks
    assert "above the 0.0001 MB download limit" in out
    assert "No dependency could be checked" in out
    assert local_index.downloads == []


# ------------------------------------------------------------------ cache
def test_cache_path_prints_where_the_cache_is(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("SINCE_CUTOFF_CACHE", str(tmp_path / "c"))
    assert cli.main(["cache", "path"]) == 0
    assert capsys.readouterr().out == f"{tmp_path / 'c'}\n"


def test_cache_clear_says_what_it_removed(tmp_path, monkeypatch, capsys) -> None:
    root = tmp_path / "c"
    monkeypatch.setenv("SINCE_CUTOFF_CACHE", str(root))
    for namespace in ("answers", "sources", "pypi"):
        (root / namespace / "entry").mkdir(parents=True)
    assert cli.main(["cache", "clear"]) == 0
    assert capsys.readouterr().out == f"cleared pypi, sources, answers in {root}\n"
    assert not root.exists()  # it held nothing else
    assert cli.main(["cache", "clear"]) == 0
    assert capsys.readouterr().out == f"cleared nothing in {root}\n"


def test_cache_clear_can_remove_one_kind_and_keep_the_model_output(
    tmp_path, monkeypatch, capsys
) -> None:
    root = tmp_path / "c"
    monkeypatch.setenv("SINCE_CUTOFF_CACHE", str(root))
    for namespace in ("pypi", "sources", "sources-meta", "diffs", "answers", "tasks"):
        (root / namespace / "entry").mkdir(parents=True)
    assert cli.main(["cache", "clear", "--sources"]) == 0
    assert capsys.readouterr().out == f"cleared sources, sources-meta in {root}\n"
    assert sorted(p.name for p in root.iterdir()) == ["answers", "diffs", "pypi", "tasks"]
    assert cli.main(["cache", "clear", "--pypi", "--diffs"]) == 0
    assert capsys.readouterr().out == f"cleared pypi, diffs in {root}\n"
    assert sorted(p.name for p in root.iterdir()) == ["answers", "tasks"]  # what run paid for
    assert cli.main(["cache", "clear", "--sources"]) == 0
    assert capsys.readouterr().out == f"cleared nothing in {root}\n"


def test_cache_kind_flags_go_with_clear_only(capsys) -> None:
    assert cli.main(["cache", "info", "--sources", "--pypi"]) == 2
    err = capsys.readouterr().err
    assert "cache info takes no --sources, --pypi: the kinds go with clear" in err


def test_cache_info_lists_each_kind_with_its_size(tmp_path, monkeypatch, capsys) -> None:
    root = tmp_path / "c"
    monkeypatch.setenv("SINCE_CUTOFF_CACHE", str(root))
    monkeypatch.delenv("SINCE_CUTOFF_CACHE_MAX_MB", raising=False)
    (root / "pypi").mkdir(parents=True)
    (root / "pypi" / "toy.json").write_bytes(b"x" * (3 * 1024 * 1024 // 2))
    (root / "answers").mkdir()
    (root / "answers" / "k.json").write_text("{}")
    tree = root / "sources" / "toy-1.0"
    (tree / "toy").mkdir(parents=True)
    (tree / "toy" / "__init__.py").write_bytes(b"y" * 1024)
    (tree / ".since-cutoff.json").write_text("{}")
    assert cli.main(["cache", "info"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == str(root)
    assert lines[1].split() == ["kind", "size", "entries", "files", "oldest"]
    rows = {line.split()[0]: line.split() for line in lines[2:-1]}
    assert list(rows) == [*cli_cache_kinds(), "total"]
    assert rows["pypi"][1:6] == ["1.5", "MB", "1", "1", date.today().isoformat()]
    assert rows["sources"][1:6] == ["0.0", "MB", "1", "2", date.today().isoformat()]
    assert rows["diffs"][1:5] == ["0.0", "MB", "0", "0"] and len(rows["diffs"]) == 5
    assert rows["answers"][-7:] == ["model", "output:", "costs", "money", "to", "make", "again"]
    assert rows["total"][1:6] == ["1.5", "MB", "3", "4", date.today().isoformat()]
    assert lines[-1].startswith("sources cap: 2,048 MB (SINCE_CUTOFF_CACHE_MAX_MB)")
    for off in ("0", "-5"):  # the value as it is set, not a 0 nobody wrote
        monkeypatch.setenv("SINCE_CUTOFF_CACHE_MAX_MB", off)
        assert cli.main(["cache", "info"]) == 0
        assert (
            capsys.readouterr().out.splitlines()[-1]
            == f"sources cap: none (SINCE_CUTOFF_CACHE_MAX_MB={off})"
        )
    monkeypatch.setenv("SINCE_CUTOFF_CACHE_MAX_MB", "inf")  # was an OverflowError
    assert cli.main(["cache", "info"]) == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith("sources cap: 2,048 MB")


def cli_cache_kinds() -> list[str]:
    from since_cutoff.cache import NAMESPACES

    return list(NAMESPACES)


def test_cache_info_works_on_a_cache_that_does_not_exist(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("SINCE_CUTOFF_CACHE", str(tmp_path / "none"))
    assert cli.main(["cache", "info"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == str(tmp_path / "none")
    assert lines[-2].split() == ["total", "0.0", "MB", "0", "0"]


# ------------------------------------------------------------------ models
MODELS = {
    "zeta": {"models": {"z-1": {"knowledge": "2024-01", "release_date": "2024-05-01"}}},
    "openai": {"models": {"gpt-x": {"knowledge": "2024-10", "release_date": "2025-01-01"}}},
    "anthropic": {
        "models": {
            "claude-new": {"knowledge": "2025-07-31", "release_date": "2025-09-29"},
            "claude-old": {"knowledge": "2024-04", "release_date": "2024-06-20"},
            "claude-no-cutoff": {"release_date": "2025-01-01"},
        }
    },
    "llama": {
        "models": {
            "llama-3.3-70b-versatile": {"knowledge": "2024-12", "release_date": "2024-12-06"}
        }
    },
    "groq": {
        "models": {
            "llama-3.3-70b-versatile": {"knowledge": "2024-12", "release_date": "2024-12-06"}
        }
    },
}


@pytest.fixture
def models_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SINCE_CUTOFF_CACHE", str(tmp_path))
    DiskCache(tmp_path).set("models", "models-dev", slim_models_dev(MODELS))


def test_models_prints_one_line_per_model_for_scripts(models_cache, capsys) -> None:
    """Anthropic first, then OpenAI, then the rest by name; each oldest first. A model with no
    training cutoff is left out."""
    assert cli.main(["models", "--offline"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "anthropic\tclaude-old\t2024-04\t2024-06-20",
        "anthropic\tclaude-new\t2025-07-31\t2025-09-29",
        "openai\tgpt-x\t2024-10\t2025-01-01",
        "llama\tllama-3.3-70b-versatile\t2024-12\t2024-12-06",
        "groq\tllama-3.3-70b-versatile\t2024-12\t2024-12-06",
        "zeta\tz-1\t2024-01\t2024-05-01",
    ]
    assert cli.main(["models", "CLAUDE-N", "--offline"]) == 0
    assert capsys.readouterr().out == "anthropic\tclaude-new\t2025-07-31\t2025-09-29\n"
    assert cli.main(["models", "llama", "--offline"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "llama\tllama-3.3-70b-versatile\t2024-12\t2024-12-06",
        "groq\tllama-3.3-70b-versatile\t2024-12\t2024-12-06",
    ]


def test_models_prints_a_table_in_a_terminal(models_cache, monkeypatch) -> None:
    screen = io.StringIO()
    terminal = Console(file=screen, force_terminal=True, color_system=None, width=100)
    monkeypatch.setattr(cli, "_console", lambda output: terminal)
    assert cli.main(["models", "claude", "--offline"]) == 0
    rows = [
        line.replace("│", " ").replace("┃", " ").split() for line in screen.getvalue().splitlines()
    ]
    assert rows[0] == ["provider", "model", "training", "cutoff", "released"]
    assert ["anthropic", "claude-new", "2025-07-31", "2025-09-29"] in rows
    assert " ".join(rows[-1]) == "2 models (source: models.dev (cached))"


# ----------------------------------------------------------------- unapply
BLOCK = "<!-- since-cutoff:start -->\nx\n<!-- since-cutoff:end -->\n"


def test_unapply_cleans_a_target_relative_to_the_project(tmp_path, capsys) -> None:
    rules = tmp_path / "docs" / "RULES.md"
    rules.parent.mkdir()
    rules.write_text(f"keep\n\n{BLOCK}", encoding="utf-8")
    assert cli.main(["unapply", str(tmp_path), "--target", "docs/RULES.md"]) == 0
    assert capsys.readouterr().out == "removed the since-cutoff block from RULES.md\n"
    assert rules.read_text(encoding="utf-8") == "keep\n"


def test_unapply_says_when_there_is_nothing_to_remove(tmp_path, capsys) -> None:
    (tmp_path / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
    assert cli.main(["unapply", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "no since-cutoff block found\n"
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "# Rules\n"


def test_unapply_needs_a_project_directory(tmp_path, capsys) -> None:
    (tmp_path / "AGENTS.md").write_text(BLOCK, encoding="utf-8")
    assert cli.main(["unapply", str(tmp_path / "AGENTS.md")]) == 1
    assert "is not a directory" in capsys.readouterr().err
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == BLOCK


# --------------------------------------------------------------------- mcp
def test_mcp_serves_with_the_download_limit_given(monkeypatch) -> None:
    from since_cutoff import mcp_server

    served: list[dict[str, Any]] = []
    monkeypatch.setattr(mcp_server, "serve", lambda **kwargs: served.append(kwargs))
    assert cli.main(["mcp", "--max-download-mb", "12.5"]) == 0
    assert served == [{"max_download_mb": 12.5, "debug": False}]


# ----------------------------------------------------------------- options
@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["run", "--jobs", "-1"], "argument --jobs: must be 0 or more"),
        (["run", "--max-probes", "many"], "argument --max-probes: not a number: many"),
        (["scan", "--limit", "2.5"], "argument --limit: not a number: 2.5"),
        # 0 would silently mean the default (5): not a budget.
        (["sync", "--per-package", "0"], "argument --per-package: must be 1 or more"),
    ],
)
def test_counts_must_be_whole_numbers(capsys, argv, message) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(argv)
    assert exit_info.value.code == 2
    assert message in capsys.readouterr().err


def test_quick_caps_the_run_but_keeps_smaller_choices() -> None:
    def settings(*argv: str) -> Any:
        return cli._settings(cli.build_parser().parse_args(["run", *argv]))

    s = settings("--quick")
    assert (s.max_probes, s.heldout, s.regression) == (12, 1, 3)
    s = settings("--quick", "--max-probes", "5", "--regression", "0")
    assert (s.max_probes, s.heldout, s.regression) == (5, 1, 0)
    s = settings("--effort", "default", "--jobs", "0")
    assert (s.effort, s.jobs) == (None, 1)  # the CLI's own effort; at least one job


def test_a_project_path_alone_is_run(tmp_path) -> None:
    assert cli._argv_with_default([str(tmp_path), "--quick"]) == ["run", str(tmp_path), "--quick"]
    # A path that does not exist yet is still a path: the run then says it is missing.
    assert cli._argv_with_default(["../app"]) == ["run", "../app"]


# -------------------------------------------------------------- exit codes
def run_argv(root: Path, *extra: str) -> list[str]:
    return ["run", str(root), "--model", "anthropic:claude-sonnet-4-5", *CUTOFF, *extra]


@pytest.mark.pyright
def test_fail_on_stale_exits_3_when_the_model_writes_stale_code(
    tmp_path, capsys, scripted_cli
) -> None:
    root = make_project(tmp_path)
    scripted_cli.append(ScriptedModel())  # knows toylib 1.0 only
    argv = run_argv(root, "--no-fix", "--max-probes", "1", "--regression", "0")
    assert cli.main([*argv, "--fail-on-stale"]) == 3
    assert "stale" in capsys.readouterr().out.lower()


class _SolverDown(ScriptedModel):
    """The task writer works; every answer fails (a rate limit)."""

    def complete(self, system: str, user: str) -> Completion:
        if system.startswith("You write evaluation tasks"):
            return super().complete(system, user)
        raise ProviderError("claude call failed: rate limited")


def test_run_without_basedpyright_stops_before_the_scan_and_names_the_extra(
    tmp_path, capsys, scripted_cli, monkeypatch
) -> None:
    """basedpyright is the ``since-cutoff[run]`` extra: without it, ``run`` says so at once,
    before PyPI or the model is asked anything; ``scan`` does not need it."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setitem(sys.modules, "basedpyright", None)  # makes the import fail
    root = make_project(tmp_path)
    scripted_cli.append(ScriptedModel())
    assert cli.main(run_argv(root, "--no-fix")) == 1
    err = capsys.readouterr().err
    assert "run checks the model's answers with the basedpyright type checker" in err
    assert 'pip install "since-cutoff[run]"' in err
    assert "uvx --with basedpyright since-cutoff run" in err
    assert len(scripted_cli) == 1  # no engine was built, so no model was taken
    assert not (root / ".since-cutoff").exists()  # and nothing was scanned
    assert cli.main(["scan", str(root), *CUTOFF]) == 0


def test_a_run_where_every_model_call_failed_is_an_error(tmp_path, capsys, scripted_cli) -> None:
    scripted_cli.append(_SolverDown())
    root = make_project(tmp_path)
    assert cli.main(run_argv(root, "--no-fix", "--max-probes", "2")) == 1
    assert "Every model call failed: claude call failed: rate limited" in capsys.readouterr().out


def test_a_markdown_summary_that_cannot_be_written_is_an_error(
    tmp_path, capsys, fake_pypi, monkeypatch
) -> None:
    monkeypatch.setenv("SINCE_CUTOFF_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(cli, "Engine", functools.partial(Engine, pypi=fake_pypi))
    (tmp_path / "taken").write_text("a file, not a folder")
    root = make_project(tmp_path)
    argv = ["scan", str(root), *CUTOFF, "--markdown", str(tmp_path / "taken" / "summary.md")]
    assert cli.main(argv) == 1
    assert "cannot write the Markdown summary to" in capsys.readouterr().err


def test_ctrl_c_exits_130_without_a_traceback(monkeypatch, capsys) -> None:
    def interrupted(*args: Any) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_cmd_models", interrupted)
    assert cli.main(["models"]) == 130
    assert capsys.readouterr().err.strip() == "interrupted"


def test_a_run_with_nothing_changed_since_the_cutoff_asks_no_model(
    tmp_path, capsys, scripted_cli
) -> None:
    model = ScriptedModel()
    scripted_cli.append(model)
    root = make_project(tmp_path, deps='"toylib==1.0"')  # the version at the cutoff
    assert cli.main(run_argv(root, "--fail-on-stale")) == 0
    assert "Nothing to probe" in capsys.readouterr().out
    assert model.calls == []


class _TaskWriterFlaky(ScriptedModel):
    """The task writer answers once, then hits a rate limit."""

    def __init__(self) -> None:
        super().__init__()
        self.written = 0
        self.lock = threading.Lock()

    def complete(self, system: str, user: str) -> Completion:
        if system.startswith("You write evaluation tasks"):
            with self.lock:
                self.written += 1
                if self.written > 1:
                    raise ProviderError("claude call failed: You've hit your limit")
        return super().complete(system, user)


@pytest.mark.pyright
def test_a_run_whose_task_writer_mostly_failed_is_an_error(tmp_path, capsys, scripted_cli) -> None:
    """A run that could write tasks for one API change in five measured too little to trust,
    whatever that one probe found."""
    scripted_cli.append(_TaskWriterFlaky())
    root = make_project(tmp_path)
    argv = run_argv(root, "--no-fix", "--max-probes", "2", "--regression", "0")
    assert cli.main(argv) == 1
    out = " ".join(capsys.readouterr().out.split())
    assert "The task writer failed for 4 of 5 API changes" in out and "hit your limit" in out


# ------------------------------------------------------------------ output
@pytest.mark.parametrize("code", [errno.EPIPE, errno.EINVAL], ids=["posix", "windows"])
def test_a_closed_stdout_ends_the_command_quietly(monkeypatch, code) -> None:
    class Closed(io.StringIO):
        def write(self, text: str) -> int:
            raise OSError(code, os.strerror(code))

    monkeypatch.setattr(sys, "stdout", Closed())
    assert cli.main(["cache", "path"]) == cli.OUTPUT_CLOSED


def test_a_full_disk_is_not_taken_for_a_closed_pipe(monkeypatch) -> None:
    class Full(io.StringIO):
        def write(self, text: str) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(sys, "stdout", Full())
    with pytest.raises(OSError, match="No space left on device"):
        cli.main(["cache", "path"])


def test_a_terminal_gets_a_live_progress_bar_instead_of_log_lines(monkeypatch) -> None:
    screen = io.StringIO()
    monkeypatch.setattr(cli, "_interactive", lambda stream: True)
    reporter = cli.RichReporter(Console(file=screen, force_terminal=True, width=80))
    reporter.stage("Diffing the API of 3 packages", 3)
    assert reporter.progress is not None
    for name in ("a", "b", "c"):
        reporter.advance(label=name)
    reporter.done()
    assert reporter.progress is None
    assert "Diffing the API of 3 packages" in screen.getvalue()
    assert "diffed" not in screen.getvalue()


def test_python_m_since_cutoff_is_the_cli(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["since-cutoff", "--version"])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("since_cutoff", run_name="__main__")
    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"since-cutoff {__version__}\n"
