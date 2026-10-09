"""`since-cutoff status`: is the notes block current for the lockfile? Offline (plan A.5, #18).

Each block here is written by `since-cutoff sync` against the fake PyPI of test_sync.py; then the
network is cut off entirely, and status must still answer."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from since_cutoff import cli, net
from since_cutoff.cache import DiskCache
from since_cutoff.engine import Engine
from since_cutoff.notes import BLOCK_START, parse_block
from since_cutoff.pypi import PyPI
from since_cutoff.sync import EXIT_OK, EXIT_OUT_OF_DATE
from tests import test_sync
from tests.test_sync import SONNET, agents, make_app, set_pins, write

# The fixtures of test_sync.py: the fake PyPI, and the CLI without network (``sc``).
pypi = test_sync.pypi
sc = test_sync.sc


@pytest.fixture
def synced(sc: Any, tmp_path: Path) -> Path:
    """A project whose AGENTS.md sync wrote for claude-sonnet-4-5 (toylib 2.0)."""
    root = make_app(tmp_path)
    code, out = sc("sync", root, *SONNET, "--yes")
    assert code == EXIT_OK, out
    return root


@pytest.fixture
def status(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], sc: Any) -> Any:
    """``status(root, ...)``: ``since-cutoff status``, where any use of the network or of PyPI
    fails the test."""

    def offline(*args: object, **kwargs: object) -> None:
        raise AssertionError("status must not touch the network")

    def engine(settings: Any, **kw: Any) -> Engine:
        return Engine(settings, **{**kw, "pypi": NoPyPI(DiskCache(kw["store"].root))})

    def run(*argv: object) -> tuple[int, str]:
        with monkeypatch.context() as m:
            m.setattr(net, "request", offline)
            m.setattr(urllib.request, "urlopen", offline)
            m.setattr(cli, "Engine", engine)
            code = cli.main(["status", *[str(a) for a in argv]])
        return code, capsys.readouterr().out.replace("\r\n", "\n")

    return run


class NoPyPI(PyPI):
    def project(self, name: str) -> dict[str, Any]:
        raise AssertionError("status must not ask PyPI")

    def source(self, name: str, version: str) -> Any:
        raise AssertionError("status must not download")


def test_current(status, synced) -> None:
    code, out = status(synced)
    assert code == EXIT_OK
    assert out.splitlines() == [
        f"AGENTS.md: notes for claude-sonnet-4-5 (cutoff 2025-07-31), written by since-cutoff "
        f"{cli.__version__}",
        "  toylib    notes for 2.0    pyproject.toml: 2.0    current",
        "Current: the notes match the versions in pyproject.toml.",
    ]


def test_a_bumped_package_is_out_of_date(status, synced) -> None:
    set_pins(synced, ("toylib==2.1", "otherlib==1.0"))
    code, out = status(synced)
    assert code == EXIT_OUT_OF_DATE
    assert "  toylib    notes for 2.0    pyproject.toml: 2.1    out of date" in out
    assert out.splitlines()[-1] == "Out of date: run `since-cutoff sync` (exit code 3)."


def test_notes_for_a_package_no_longer_a_dependency_are_orphaned(status, synced) -> None:
    set_pins(synced, ("otherlib==1.0",))
    code, out = status(synced)
    assert code == EXIT_OUT_OF_DATE and "no longer a dependency" in out
    code, out = status(synced, "--json")
    data = json.loads(out)
    assert data["targets"][0]["packages"][0]["state"] == "orphan"
    assert data["targets"][0]["problems"] == [
        {"kind": "orphan", "text": "toylib is no longer a dependency"}
    ]


def test_other_dependencies_changed(status, synced) -> None:
    set_pins(synced, ("toylib==2.0", "otherlib==1.0", "newlib==3.0"))
    code, out = status(synced)
    assert code == EXIT_OUT_OF_DATE
    assert (
        "  (other dependencies changed since the notes were written; `since-cutoff sync` checks "
        "them)" in out
    )


def test_never_run(status, sc, tmp_path) -> None:
    """No block is not out of date: a project whose code uses no changed API has none, and
    sync writes none, so status must not fail it (the pre-commit status hook could never
    pass) or tell the user to run sync; ``sync --check`` says whether notes are needed."""
    root = make_app(tmp_path)
    code, out = status(root)
    assert code == EXIT_OK
    assert out.splitlines() == [
        "AGENTS.md: no since-cutoff notes. `since-cutoff sync` writes them if your code uses an "
        "API that changed after the cutoff (`since-cutoff sync --check` says whether it does).",
        "No notes to check.",
    ]
    code, out = status(root, "--json")
    assert code == EXIT_OK and out.count('"state": "never_run"') == 2  # overall and the file
    assert json.loads(out)["exit_code"] == EXIT_OK


def test_another_cutoff_for_the_coding_agents_model(status, synced, monkeypatch) -> None:
    """sync keeps the block's model (``sync --check`` says it is up to date), so another
    model is said, with the command that writes the notes for it, and is not a reason to fail:
    a teammate's agent must not fail the status gate or be told at every session start to run
    a sync that changes nothing."""
    # claude-haiku-4-5's cutoff (2025-02-28, from the bundled snapshot: no network) is earlier
    # than the notes' 2025-07-31: they may miss changes between the two.
    monkeypatch.setenv("SINCE_CUTOFF_MODEL", "anthropic:claude-haiku-4-5")
    code, out = status(synced)
    assert code == EXIT_OK
    assert (
        "  (the notes are for claude-sonnet-4-5 (cutoff 2025-07-31); your coding agent is set up "
        "with claude-haiku-4-5 (cutoff 2025-02-28) (from SINCE_CUTOFF_MODEL), which may also miss "
        "changes from before 2025-07-31: `since-cutoff sync --model anthropic:claude-haiku-4-5` "
        "writes the notes for it)" in out
    )
    assert out.splitlines()[-1] == "Current: the notes match the versions in pyproject.toml."
    assert status(synced, "--hook") == (EXIT_OK, "")
    assert json.loads(status(synced, "--json")[1])["targets"][0]["problems"] == []
    # A model with a later cutoff: the notes hold changes it may know already; nothing to say.
    monkeypatch.setenv("SINCE_CUTOFF_MODEL", "openai:gpt-5.4")
    code, out = status(synced)
    assert code == EXIT_OK and "gpt-5.4" not in out
    monkeypatch.setenv("SINCE_CUTOFF_MODEL", "anthropic:claude-haiku-4-5")
    data = json.loads(status(synced, "--json")[1])
    assert data["coding_agent_model"] == {
        "model": "claude-haiku-4-5",
        "cutoff": "2025-02-28",
        "spec": "anthropic:claude-haiku-4-5",
        "source": "SINCE_CUTOFF_MODEL",
    }
    # Another model with the same cutoff writes the same notes: nothing to do.
    monkeypatch.setenv("SINCE_CUTOFF_MODEL", "anthropic:claude-sonnet-4-5-20250929")
    assert status(synced)[0] == EXIT_OK
    # A model that cannot be resolved offline is not held against the notes.
    monkeypatch.setenv("SINCE_CUTOFF_MODEL", "anthropic:no-such-model")
    assert status(synced)[0] == EXIT_OK


def test_a_block_for_a_cutoff_alone_is_not_compared_with_a_model(
    status, sc, tmp_path, monkeypatch
) -> None:
    root = make_app(tmp_path)
    assert sc("sync", root, "--cutoff", "2025-07-31", "--yes")[0] == EXIT_OK
    monkeypatch.setenv("SINCE_CUTOFF_MODEL", "anthropic:claude-haiku-4-5")
    code, out = status(root)
    assert code == EXIT_OK and "AGENTS.md: notes for the cutoff 2025-07-31" in out


def test_hook_prints_one_line_only_when_out_of_date(status, synced, sc, tmp_path) -> None:
    assert status(synced, "--hook") == (EXIT_OK, "")
    set_pins(synced, ("toylib==2.1", "otherlib==1.0"))
    code, out = status(synced, "--hook")
    assert code == EXIT_OK
    assert out == (
        "since-cutoff: the library notes in AGENTS.md are out of date: toylib 2.0 in the notes, "
        "2.1 in pyproject.toml. `since-cutoff sync` updates them.\n"
    )
    never = make_app(tmp_path / "never")
    assert status(never, "--hook") == (EXIT_OK, "")  # no nagging about notes nobody asked for
    write(synced / "AGENTS.md", f"{BLOCK_START}\nbroken\n")
    assert status(synced, "--hook") == (EXIT_OK, "")  # whatever goes wrong, the session starts
    assert status(tmp_path / "missing", "--hook") == (EXIT_OK, "")


def test_broken_markers_are_an_error(status, synced, capsys) -> None:
    write(synced / "AGENTS.md", f"{BLOCK_START}\nbroken\n")
    code, _ = status(synced)
    assert code == 1


def test_json(status, synced) -> None:
    set_pins(synced, ("toylib==2.1", "otherlib==1.0"))
    code, out = status(synced, "--json")
    assert code == EXIT_OUT_OF_DATE
    data = json.loads(out)
    assert data["state"] == "out_of_date" and data["exit_code"] == EXIT_OUT_OF_DATE
    assert data["versions_from"] == "pyproject.toml" and data["coding_agent_model"] is None
    [target] = data["targets"]
    assert target["file"] == "AGENTS.md" and target["format"] == 2 and target["edited"] is False
    assert (target["model"], target["cutoff"], target["scope"]) == (
        "claude-sonnet-4-5",
        "2025-07-31",
        "used",
    )
    assert target["packages"] == [
        {
            "package": "toylib",
            "notes_for": "2.0",
            "locked": "2.1",
            "versions_from": "pyproject.toml",
            "state": "out_of_date",
        }
    ]
    assert target["problems"] == [
        {"kind": "stale", "text": "toylib 2.0 in the notes, 2.1 in pyproject.toml"}
    ]


def test_a_hand_edit_and_0_3_blocks_are_said_but_not_out_of_date(status, synced) -> None:
    text = agents(synced).replace("do not pass it.", "do not pass it, ever.")
    write(synced / "AGENTS.md", text)
    code, out = status(synced)
    assert code == EXIT_OK
    assert (
        "(edited by hand since since-cutoff wrote it: `since-cutoff sync` asks for --force "
        "before replacing it)" in out
    )


def test_an_unpinned_package_is_left_to_sync(status, sc, tmp_path) -> None:
    root = make_app(tmp_path, pins=("toylib", "otherlib==1.0"))
    assert sc("sync", root, *SONNET, "--yes")[0] == EXIT_OK
    block = parse_block(agents(root))
    assert block is not None and block.packages["toylib"].version == "2.1"  # PyPI's latest
    code, out = status(root)
    assert code == EXIT_OK
    assert "not pinned (sync checks it)" in out
    assert (
        "(toylib is not pinned, so its notes are checked against PyPI by `since-cutoff sync` only)"
        in out
    )


def test_status_reads_the_block_where_it_is(status, sc, tmp_path) -> None:
    root = make_app(tmp_path)
    write(root / "AGENTS.md", "mine\n")
    assert sc("sync", root, *SONNET, "--target", "CLAUDE.md", "--yes")[0] == EXIT_OK
    code, out = status(root)
    assert code == EXIT_OK and out.startswith("CLAUDE.md: notes for claude-sonnet-4-5")
    code, out = status(root, "--target", "AGENTS.md")
    assert code == EXIT_OK and out.startswith("AGENTS.md: no since-cutoff notes.")


def test_a_block_compared_from_another_day_is_out_of_date(status, sc, tmp_path) -> None:
    """A block written with ``--cutoff-margin 0``, or by a since-cutoff from before there was a
    margin (no ``margin`` in its meta line), compared from the cutoff itself; the scan now
    compares from 30 days before it, so the notes may miss changes: out of date, unless status
    is told the same margin."""
    root = make_app(tmp_path)
    assert sc("sync", root, *SONNET, "--yes", "--cutoff-margin", "0")[0] == EXIT_OK
    block = parse_block(agents(root))
    assert block is not None and block.meta["margin"] == 0
    code, out = status(root)
    assert code == EXIT_OUT_OF_DATE
    assert (
        "the notes compare from the cutoff itself; the scan now compares from 30 days before "
        "the cutoff (--cutoff-margin 30)"
    ) in out
    code, out = status(root, "--cutoff-margin", "0")
    assert code == EXIT_OK and "compare from" not in out
    assert json.loads(status(root, "--json")[1])["targets"][0]["margin"] == 0
    # The same block as an earlier since-cutoff wrote it: no `margin` in the meta line.
    write(root / "AGENTS.md", agents(root).replace(',"margin":0', ""))
    code, out = status(root)
    assert code == EXIT_OUT_OF_DATE and "the notes compare from the cutoff itself" in out
