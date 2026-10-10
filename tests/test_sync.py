"""`since-cutoff sync`: write the notes from the API diff for the changed APIs the code uses into
AGENTS.md / CLAUDE.md, and keep them in step with the lockfile and the code (plan A.5).

Every scan here uses a fake PyPI (toylib 0.9; 1.0, the release at the cutoff 2025-07-31; 1.1
with 1.0's API; 2.0 and 2.1 with 2.0's; otherlib 1.0 and 1.1, released before the cutoff) and no
network. No model is called."""

from __future__ import annotations

import builtins
import io
import json
import os
import re
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, ClassVar

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from since_cutoff import cli, net
from since_cutoff.cache import DiskCache
from since_cutoff.engine import Engine, ScanResult
from since_cutoff.errors import PackageIndexError
from since_cutoff.notes import (
    BLOCK_END,
    BLOCK_START,
    NOTE_MODEL,
    SCOPE_FAILURES,
    TAG_TYPE_CHECKED,
    Note,
    apply_block,
    block_targets,
    body_hash,
    parse_block,
    render_block,
    render_legacy_block,
)
from since_cutoff.pypi import SourceTree
from since_cutoff.sync import (
    EXIT_EDITED,
    EXIT_OK,
    EXIT_OUT_OF_DATE,
    propose,
    read_target,
)
from tests.conftest import FakePyPI
from tests.test_ci import _read as repo_file
from tests.test_ci import scan_app

SONNET = ["--model", "anthropic:claude-sonnet-4-5", "--cutoff", "2025-07-31"]
MAIN = "from toylib import Client, fetch\nClient().send('hi', temperature=0.2)\nfetch('u')\n"
OTHER = "from toylib import Client\nClient().send('x')\n"
MINE = "# Project rules\n\nUse tabs.\n"


def make_app(tmp_path: Path, pins: tuple[str, ...] = ("toylib==2.0", "otherlib==1.0")) -> Path:
    root = tmp_path / "app"
    (root / "sub").mkdir(parents=True)
    set_pins(root, pins)
    (root / "main.py").write_text(MAIN, encoding="utf-8")
    (root / "sub" / "other.py").write_text(OTHER, encoding="utf-8")
    return root


def set_pins(root: Path, pins: tuple[str, ...]) -> None:
    deps = ", ".join(f'"{p}"' for p in pins)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "app"\nversion = "0"\ndependencies = [{deps}]\n', encoding="utf-8"
    )


@pytest.fixture
def pypi(cache: DiskCache, toylib: tuple[SourceTree, SourceTree]) -> FakePyPI:
    v1, v2 = toylib
    return FakePyPI(
        cache,
        {
            "toylib": [
                ("0.9", "2024-06-01"),
                ("1.0", "2025-01-10"),
                ("1.1", "2025-09-01"),
                ("2.0", "2025-10-01"),
                ("2.1", "2025-11-01"),
            ],
            "otherlib": [("1.0", "2024-01-01"), ("1.1", "2024-02-01")],
        },
        {
            ("toylib", "0.9"): SourceTree("toylib", "0.9", v1.root, ("toylib",)),
            ("toylib", "1.0"): v1,
            ("toylib", "1.1"): SourceTree("toylib", "1.1", v1.root, ("toylib",)),
            ("toylib", "2.0"): v2,
            ("toylib", "2.1"): SourceTree("toylib", "2.1", v2.root, ("toylib",)),
        },
    )


@pytest.fixture
def sc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pypi: FakePyPI,
    capsys: pytest.CaptureFixture[str],
) -> Any:
    """``sc("sync", root, ...)``: the CLI with the fake PyPI, a throwaway cache and no network
    (the model registry falls back to its bundled snapshot); returns (exit code, stdout)."""
    monkeypatch.setenv("SINCE_CUTOFF_CACHE", str(tmp_path / "cli-cache"))
    monkeypatch.setattr(
        cli, "Engine", lambda settings, **kw: Engine(settings, **{**kw, "pypi": pypi})
    )

    def offline(url: str, *args: object, **kwargs: object) -> None:
        raise net.HTTPError(url, None, "no network in tests")

    monkeypatch.setattr(net, "request", offline)

    def run(*argv: object) -> tuple[int, str]:
        code = cli.main([str(a) for a in argv])
        return code, capsys.readouterr().out.replace("\r\n", "\n")

    return run


def write(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))  # LF on every platform


def agents(root: Path) -> str:
    return (root / "AGENTS.md").read_bytes().decode("utf-8")


def first_sync(sc: Any, root: Path) -> str:
    code, out = sc("sync", root, *SONNET, "--yes")
    assert code == EXIT_OK, out
    return agents(root)


# ------------------------------------------------------ first write and no-op
def test_the_first_sync_shows_a_diff_from_dev_null_and_writes_the_scan_block(
    sc, tmp_path, cache, pypi
) -> None:
    root = make_app(tmp_path)
    code, out = sc("sync", root, *SONNET, "--yes")
    assert code == EXIT_OK, out
    assert "--- /dev/null\n+++ AGENTS.md (since-cutoff sync)\n@@ -0,0 +1," in out
    assert "✓ Created AGENTS.md with 2 notes." in out
    assert "Text outside the since-cutoff markers" not in out  # there was no file
    scan = scan_app(root, cache, pypi, date(2025, 7, 31))
    assert agents(root) == scan.notes_block(scan.diff_notes())
    block = parse_block(agents(root))
    assert block is not None and block.meta["scope"] == "used" and block.edited is False
    assert set(block.packages) == {"toylib"} and block.packages["toylib"].version == "2.0"


def test_a_sync_with_nothing_to_change_leaves_the_file_alone(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    write(root / "AGENTS.md", MINE)
    code, out = sc("sync", root, *SONNET, "--yes")
    assert code == EXIT_OK and "✓ Added the notes block to the end of AGENTS.md: 2 notes." in out
    assert "Text outside the since-cutoff markers is unchanged." in out
    before = (root / "AGENTS.md").read_bytes()
    assert before.startswith(MINE.encode())
    old = (root / "AGENTS.md").stat().st_mtime_ns
    os.utime(root / "AGENTS.md", ns=(old - 10**9, old - 10**9))  # an mtime a write would change
    stamp = (root / "AGENTS.md").stat().st_mtime_ns

    code, out = sc("sync", root)
    assert code == EXIT_OK, out
    assert (
        "• Model claude-sonnet-4-5, training cutoff 2025-07-31, comparing from releases up to "
        "2025-07-01 (from the notes in AGENTS.md; --model to change)" in out
    )
    assert (
        "AGENTS.md is up to date: 2 notes for claude-sonnet-4-5 (cutoff 2025-07-31), versions "
        "from pyproject.toml. Nothing written." in out
    )
    assert "---" not in out  # no diff
    assert (root / "AGENTS.md").read_bytes() == before
    assert (root / "AGENTS.md").stat().st_mtime_ns == stamp
    assert sc("sync", root, "--check")[0] == EXIT_OK


def test_the_model_is_sticky(sc, tmp_path, monkeypatch) -> None:
    """Teammates whose agents use other models do not rewrite the block back and forth."""
    root = make_app(tmp_path)
    before = first_sync(sc, root)
    monkeypatch.setenv("SINCE_CUTOFF_MODEL", "openai:gpt-5.4")
    code, out = sc("sync", root, "--check")
    assert code == EXIT_OK and "gpt-5.4" not in out and "(from the notes in AGENTS.md" in out
    assert agents(root) == before


def test_another_since_cutoff_version_alone_does_not_rewrite_the_block(
    sc, tmp_path, cache, pypi
) -> None:
    """A teammate's older or newer since-cutoff with the same notes leaves the block as it is;
    once the notes change, the block names the version that wrote them."""
    root = make_app(tmp_path)
    scan = scan_app(root, cache, pypi, date(2025, 7, 31))
    options: dict[str, Any] = {
        "model": "claude-sonnet-4-5",
        "cutoff": date(2025, 7, 31),
        "version_source": "pyproject.toml",
        "deps": scan.deps_hash(),
    }
    theirs = render_block(scan.diff_notes(), **options, tool="0.4.9")
    write(root / "AGENTS.md", theirs)
    code, out = sc("sync", root, "--yes")
    assert code == EXIT_OK and "AGENTS.md is up to date" in out and agents(root) == theirs
    set_pins(root, ("toylib==2.1", "otherlib==1.0"))
    assert sc("sync", root, "--yes")[0] == EXIT_OK
    block = parse_block(agents(root))
    assert block is not None and block.meta["tool"] == cli.__version__


# --------------------------------------------------- lockfile and code changes
def test_a_bumped_package_is_checked_again(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    write(root / "AGENTS.md", MINE)
    first_sync(sc, root)
    before = agents(root)
    set_pins(root, ("toylib==2.1", "otherlib==1.0"))

    code, out = sc("sync", root, "--check")
    assert code == EXIT_OUT_OF_DATE
    assert (
        "• pyproject.toml changed since the notes in AGENTS.md were written: toylib 2.0 -> 2.1"
        in out
    )
    assert "-**toylib 2.0** (compared from 1.0)\n+**toylib 2.1** (compared from 1.0)" in out
    assert (
        "AGENTS.md is out of date: toylib 2.0 in the notes, 2.1 in pyproject.toml. Run "
        "`since-cutoff sync`." in out
    )
    assert agents(root) == before  # --check writes nothing

    code, out = sc("sync", root, "--yes")
    assert code == EXIT_OK
    assert "✓ Updated AGENTS.md: toylib checked again for 2.1 (2 notes, text unchanged)." in out
    assert agents(root).startswith(MINE + "\n" + BLOCK_START)
    assert "**toylib 2.1** (compared from 1.0)" in agents(root)


def test_a_package_no_longer_a_dependency_is_dropped(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    write(root / "AGENTS.md", MINE)
    first_sync(sc, root)
    set_pins(root, ("otherlib==1.0",))
    code, out = sc("sync", root, "--check")
    assert code == EXIT_OUT_OF_DATE
    assert (
        "• pyproject.toml changed since the notes in AGENTS.md were written: toylib removed" in out
    )
    assert "AGENTS.md is out of date: toylib is no longer a dependency." in out
    code, out = sc("sync", root, "--yes")
    assert code == EXIT_OK
    assert (
        "✓ Removed the notes block from AGENTS.md: toylib dropped (no longer a dependency)." in out
    )
    assert agents(root) == MINE  # as it was before the first sync, byte for byte


def test_a_package_at_or_below_the_cutoff_release_is_dropped(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    first_sync(sc, root)
    set_pins(root, ("toylib==1.0", "otherlib==1.0"))
    code, out = sc("sync", root, "--check")
    assert code == EXIT_OUT_OF_DATE
    assert (
        "AGENTS.md is out of date: toylib 1.0 in pyproject.toml is the latest release at the "
        "cutoff." in out
    )
    code, out = sc("sync", root, "--yes")
    assert "toylib dropped (1.0 is the latest release at the cutoff)" in out
    assert parse_block(agents(root)) is None
    # Older than the release at the cutoff: said so (it said "0.9 is not newer than 1.0").
    set_pins(root, ("toylib==2.0", "otherlib==1.0"))
    first_sync(sc, root)
    set_pins(root, ("toylib==0.9", "otherlib==1.0"))
    code, out = sc("sync", root, "--yes")
    assert "toylib dropped (0.9 is older than 1.0, the latest release at the cutoff)" in out


def test_an_api_the_code_no_longer_uses_loses_its_note(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    first_sync(sc, root)
    (root / "main.py").write_text("from toylib import fetch\nfetch('u')\n", encoding="utf-8")
    (root / "sub" / "other.py").unlink()
    code, out = sc("sync", root, "--check")
    assert code == EXIT_OUT_OF_DATE
    assert "AGENTS.md is out of date: the notes for toylib 2.0 would change (1 dropped)." in out
    assert "-- `Client.send()` no longer accepts `temperature`" in out
    code, out = sc("sync", root, "--yes")
    assert "✓ Updated AGENTS.md: toylib 2.0: 1 dropped." in out
    block = parse_block(agents(root))
    assert block is not None and len(block.packages["toylib"].bullets) == 1

    (root / "main.py").write_text("import json\n", encoding="utf-8")
    code, out = sc("sync", root, "--yes")
    assert "toylib dropped (your code no longer uses an API of it that changed)" in out
    assert agents(root) == ""  # sync created the file with the block alone


def test_a_newly_used_api_gets_its_note(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    (root / "main.py").write_text("from toylib import fetch\nfetch('u')\n", encoding="utf-8")
    (root / "sub" / "other.py").unlink()
    first_sync(sc, root)
    (root / "main.py").write_text(MAIN, encoding="utf-8")
    code, out = sc("sync", root, "--yes")
    assert code == EXIT_OK and "✓ Updated AGENTS.md: toylib 2.0: 1 added." in out


def test_another_model_rebuilds_the_block(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    first_sync(sc, root)
    code, out = sc("sync", root, "--model", "anthropic:claude-haiku-4-5", "--cutoff", "2024-12-31")
    assert code == EXIT_OUT_OF_DATE  # no terminal to ask in
    code, out = sc(
        "sync", root, "--model", "anthropic:claude-haiku-4-5", "--cutoff", "2024-12-31", "--yes"
    )
    assert code == EXIT_OK
    assert (
        "written for claude-haiku-4-5 (cutoff 2024-12-31) (it was for claude-sonnet-4-5 (cutoff "
        "2025-07-31))" in out
    )
    block = parse_block(agents(root))
    assert block is not None and block.meta["model"] == "claude-haiku-4-5"
    assert block.packages["toylib"].cutoff_version == "0.9"
    # From now on the block's own model and cutoff.
    assert sc("sync", root, "--check")[0] == EXIT_OK


def test_several_models_use_the_earliest_cutoff(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    code, out = sc("sync", root, "--model", "openai:gpt-5.4,anthropic:claude-haiku-4-5", "--yes")
    assert code == EXIT_OK, out
    assert "• Models gpt-5.4, claude-haiku-4-5, earliest training cutoff 2025-02-28" in out
    block = parse_block(agents(root))
    assert block is not None
    assert block.meta["model"] == "gpt-5.4, claude-haiku-4-5"
    assert block.meta["cutoff"] == "2025-02-28"  # claude-haiku-4-5's; gpt-5.4's is 2025-08-31
    assert (
        "Changed after the earliest training cutoff of `gpt-5.4` and `claude-haiku-4-5` "
        "(2025-02-28, comparing from releases up to 2025-01-29) and used by this project"
        in agents(root)
    )
    code, out = sc("sync", root, "--check")
    assert code == EXIT_OK and "• Models gpt-5.4, claude-haiku-4-5, earliest" in out
    assert "2 notes for gpt-5.4 and claude-haiku-4-5 (earliest cutoff 2025-02-28)" in out


def test_a_cutoff_alone_is_sticky_too(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    assert sc("sync", root, "--cutoff", "2025-07", "--yes")[0] == EXIT_OK
    block = parse_block(agents(root))
    assert block is not None and block.meta["model"] is None
    code, out = sc("sync", root, "--check")
    assert code == EXIT_OK
    assert (
        "• Custom cutoff 2025-07-31, comparing from releases up to 2025-07-01 (from the notes in "
        "AGENTS.md; --cutoff to change)" in out
    )
    assert "2 notes for the cutoff 2025-07-31" in out


def test_other_dependencies_changing_rewrites_the_meta_line_only(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    before = first_sync(sc, root)
    set_pins(root, ("toylib==2.0", "otherlib==1.1"))  # no notes: released before the cutoff
    code, out = sc("sync", root, "--check")
    assert code == EXIT_OUT_OF_DATE
    assert (
        "• pyproject.toml changed since the notes in AGENTS.md were written: other dependencies"
        in out
    )
    assert (
        "AGENTS.md is out of date: other dependencies changed since the notes were written." in out
    )
    changed = [line for line in out.splitlines() if line.startswith(("-<", "+<"))]
    assert [line[1:].split(" {")[0] for line in changed] == ["<!-- since-cutoff:meta"] * 2
    code, out = sc("sync", root, "--yes")
    assert "✓ Updated AGENTS.md: other dependencies changed since the notes were written." in out
    assert agents(root).split("\n", 2)[2] == before.split("\n", 2)[2]  # the same text


def test_the_file_the_versions_come_from_is_part_of_the_notes(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    first_sync(sc, root)
    set_pins(root, ())
    (root / "requirements.txt").write_text("toylib==2.0\notherlib==1.0\n", encoding="utf-8")
    code, out = sc("sync", root, "--check")
    assert code == EXIT_OUT_OF_DATE
    assert (
        "AGENTS.md is out of date: the versions now come from requirements.txt, not "
        "pyproject.toml." in out
    )


def test_why_a_section_goes(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    first_sync(sc, root)
    set_pins(root, ("toylib==1.1", "otherlib==1.0"))
    code, out = sc("sync", root, "--check")
    assert "AGENTS.md is out of date: toylib's API did not change from 1.0 to 1.1." in out
    set_pins(root, ("toylib==2.0", "otherlib==1.0"))
    # No toylib release on or before the cutoff: its whole API is newer.
    code, out = sc("sync", root, "--cutoff", "2024-01-01", "--yes")
    assert code == EXIT_OK
    assert (
        "✓ Removed the notes block from AGENTS.md: written for the cutoff 2024-01-01 (it was for "
        "claude-sonnet-4-5 (cutoff 2025-07-31)); toylib dropped (first released after the "
        "cutoff)." in out
    )


def test_a_package_the_code_no_longer_imports_leaves_an_imported_block(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    (root / "main.py").write_text("import toylib\n", encoding="utf-8")
    (root / "sub" / "other.py").unlink()
    assert sc("sync", root, *SONNET, "--scope", "imported", "--yes")[0] == EXIT_OK
    (root / "main.py").write_text("import json\n", encoding="utf-8")
    code, out = sc("sync", root, "--yes")
    assert code == EXIT_OK and "toylib dropped (your code no longer imports it)" in out
    _, out = sc("sync", root, *SONNET, "--scope", "imported", "--yes")
    assert (
        "AGENTS.md: no notes to write; none of the packages your code imports changed their API "
        "after claude-sonnet-4-5's training cutoff (2025-07-31). Nothing written." in out
    )


def test_notes_for_a_package_that_is_gone_make_way_for_new_ones(sc, tmp_path, cache, pypi) -> None:
    root = make_app(tmp_path)
    scan = scan_app(root, cache, pypi, date(2025, 7, 31))
    gone = replace(scan.diff_notes()[0].change, package="gonelib", to_version="3.0")
    block = render_block(
        [Note(gone, "`gonelib.x()` was removed.", None, False, "diff")],
        model="claude-sonnet-4-5",
        cutoff=date(2025, 7, 31),
        version_source="pyproject.toml",
    )
    write(root / "AGENTS.md", block)
    code, out = sc("sync", root, "--check")
    assert code == EXIT_OUT_OF_DATE
    assert (
        "AGENTS.md is out of date: gonelib is no longer a dependency; toylib 2.0 has 2 notes to "
        "add." in out
    )
    _, out = sc("sync", root, "--yes")
    assert (
        "✓ Updated AGENTS.md: gonelib dropped (no longer a dependency); toylib 2.0: 2 notes "
        "added." in out
    )


def test_a_new_header_alone_is_said(sc, tmp_path) -> None:
    """A block whose notes, versions and hashes are current but whose header another version
    of since-cutoff worded differently."""
    root = make_app(tmp_path)
    text = first_sync(sc, root)
    meta_line, rest = text.split("\n", 2)[1:]
    rest = rest.replace("No library code was run.", "Nothing was run.")
    body = rest[: rest.index(BLOCK_END)]
    meta = json.loads(meta_line[len("<!-- since-cutoff:meta ") : -len(" -->")])
    meta["body"] = body_hash(body)
    meta_json = json.dumps(meta, separators=(",", ":"))
    write(root / "AGENTS.md", f"{BLOCK_START}\n<!-- since-cutoff:meta {meta_json} -->\n{rest}")
    code, out = sc("sync", root, "--check")
    assert code == EXIT_OUT_OF_DATE
    assert "AGENTS.md is out of date: its header or metadata would change." in out
    code, out = sc("sync", root, "--yes")
    assert (
        f"✓ Updated AGENTS.md: header and metadata written by since-cutoff {cli.__version__}."
        in out
    )


def test_reading_blocks_that_say_little(tmp_path) -> None:
    from since_cutoff.sync import _same_version, after_text, basis_text, sticky_target

    path = tmp_path / "AGENTS.md"
    meta = '<!-- since-cutoff:meta {"v":2,"cutoff":"soon"} -->'
    write(path, f"{BLOCK_START}\n{meta}\n{BLOCK_END}\n")
    target = read_target(tmp_path, path)
    assert target.basis is None and sticky_target(target) is None
    outside = tmp_path.parent / "elsewhere.md"
    assert read_target(tmp_path, outside).name == str(outside)
    assert read_target(tmp_path, outside).text is None
    assert _same_version("1.0", "1.0.0") and _same_version("abc", "abc")
    assert not _same_version("abc", "1.0")
    day = date(2025, 2, 28)
    assert after_text("a, b", day) == "the earliest training cutoff of a and b (2025-02-28)"
    assert basis_text("a, b", day) == "a and b (earliest cutoff 2025-02-28)"


def test_a_package_that_could_not_be_checked_is_never_mistaken_for_unused(
    tmp_path, cache, pypi
) -> None:
    """sync stops before this (nothing written); propose still says why, for the Python API."""
    root = make_app(tmp_path)
    first = scan_app(root, cache, pypi, date(2025, 7, 31))
    block = first.notes_block(first.diff_notes())
    assert block is not None
    write(root / "AGENTS.md", block)
    down = DownPyPI(DiskCache(tmp_path / "cold"), pypi._releases, pypi._trees)
    DownPyPI.down = {"toylib"}
    try:
        scan = scan_app(root, DiskCache(tmp_path / "cold"), down, date(2025, 7, 31))
    finally:
        DownPyPI.down = set()
    proposal = propose(scan, read_target(root, root / "AGENTS.md"))
    [change] = [c for c in proposal.changes if c.package == "toylib"]
    assert change.done.startswith("toylib dropped (could not be checked: could not reach PyPI")


# ----------------------------------------------------- hand edits and --check
def test_a_hand_edit_is_not_overwritten_without_force(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    text = first_sync(sc, root).replace("do not pass it.", "do not pass it, ever.")
    write(root / "AGENTS.md", text)
    for flags in ([], ["--yes"], ["--check"]):
        code, out = sc("sync", root, *flags)
        assert code == EXIT_EDITED, (flags, out)
        assert "-- `Client.send()` no longer accepts `temperature`; do not pass it, ever." in out
        assert (
            "! The since-cutoff block in AGENTS.md was edited by hand; sync would replace it "
            f"(diff above). Move your text below `{BLOCK_END}`, or run `since-cutoff sync "
            "--force`." in out
        )
        assert agents(root) == text
    assert sc("sync", root, "--dry-run")[0] == EXIT_OK and agents(root) == text
    code, out = sc("sync", root, "--force", "--yes")
    assert code == EXIT_OK and "replaced the text edited by hand" in out
    assert "ever" not in agents(root) and parse_block(agents(root)).edited is False  # type: ignore[union-attr]


def test_dry_run_and_check_write_nothing(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    code, out = sc("sync", root, *SONNET, "--dry-run")
    assert code == EXIT_OK and "+++ AGENTS.md (since-cutoff sync)" in out
    assert "Nothing written (--dry-run)." in out and not (root / "AGENTS.md").exists()
    code, out = sc("sync", root, *SONNET, "--check")
    assert code == EXIT_OUT_OF_DATE and not (root / "AGENTS.md").exists()
    assert (
        "AGENTS.md is out of date: it has no since-cutoff notes; 2 notes to write. Run "
        "`since-cutoff sync`." in out
    )


def test_sync_json_written_then_current(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    code, out = sc("sync", root, *SONNET, "--json", "--yes")
    result = json.loads(out)
    assert result["exit_code"] == code == EXIT_OK
    [target] = result["targets"]
    assert target["target"] == "AGENTS.md"
    assert target["action"] == "created"
    assert target["changed"] and target["written"] and not target["edited"]
    assert target["model"] == "claude-sonnet-4-5"
    assert target["cutoff"] == "2025-07-31" and target["scope"] == "used"
    assert target["notes"] == 2 and target["retest"] == {}
    assert target["diff_lines"][0] == "--- /dev/null"
    assert all(set(c) == {"package", "done", "state"} for c in target["changes"])
    before = agents(root)
    code, out = sc("sync", root, "--json", "--check")
    result = json.loads(out)
    assert result["exit_code"] == code == EXIT_OK
    [target] = result["targets"]
    assert target["action"] is None
    assert not target["changed"] and not target["written"] and target["diff_lines"] == []
    assert agents(root) == before


@pytest.mark.parametrize(
    "flags, expected", [(("--check",), 3), (("--dry-run",), 0), (("--check", "--dry-run"), 3)]
)
def test_sync_json_preview_preserves_exit_codes(sc, tmp_path, flags, expected) -> None:
    root = make_app(tmp_path)
    code, out = sc("sync", root, *SONNET, "--json", *flags)
    result = json.loads(out)
    assert result["exit_code"] == code == expected
    [target] = result["targets"]
    assert target["changed"] and not target["written"]
    assert target["diff_lines"] and not (root / "AGENTS.md").exists()


def test_sync_json_edited_block_and_force(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    assert sc("sync", root, *SONNET, "--yes")[0] == EXIT_OK
    text = agents(root).replace("Client", "EditedClient")
    write(root / "AGENTS.md", text)
    code, out = sc("sync", root, "--json", "--yes")
    result = json.loads(out)
    assert result["exit_code"] == code == EXIT_EDITED
    [target] = result["targets"]
    assert target["edited"] and target["changed"] and not target["written"]
    assert agents(root) == text
    code, out = sc("sync", root, "--json", "--yes", "--force")
    assert json.loads(out)["targets"][0]["written"] and code == EXIT_OK


def test_sync_json_requires_noninteractive_mode(sc, tmp_path, capsys, monkeypatch) -> None:
    def no_prompt(*args):
        pytest.fail("JSON mode prompted")

    monkeypatch.setattr(cli, "_confirm", no_prompt)
    # The guard must reject even before reading a nonexistent project.
    code = cli.main(["sync", str(tmp_path / "missing"), "--json"])
    captured = capsys.readouterr()
    assert code == 2 and captured.out == ""
    assert "--yes" in captured.err and "--check" in captured.err and "--dry-run" in captured.err


def test_sync_asks_before_writing(sc, tmp_path, monkeypatch) -> None:
    root = make_app(tmp_path)
    asked: list[str] = []

    def answer(value: bool | None) -> None:
        def confirm(question: str, out: Any) -> bool | None:
            asked.append(question)
            return value

        monkeypatch.setattr(cli, "_confirm", confirm)

    answer(None)  # no terminal: a pipe, CI, a coding agent's shell
    code, out = sc("sync", root, *SONNET)
    assert code == EXIT_OUT_OF_DATE and not (root / "AGENTS.md").exists()
    assert (
        "Not written: there is no terminal to ask in. `since-cutoff sync --yes` writes it; "
        "`since-cutoff sync --check` only checks." in out
    )
    answer(False)
    code, out = sc("sync", root, *SONNET)
    assert code == EXIT_OUT_OF_DATE and "Nothing written." in out
    assert not (root / "AGENTS.md").exists()
    answer(True)
    code, out = sc("sync", root, *SONNET)
    assert code == EXIT_OK and (root / "AGENTS.md").exists()
    assert asked == ["Write this to AGENTS.md?"] * 3
    asked.clear()
    assert sc("sync", root, *SONNET, "--yes")[0] == EXIT_OK and asked == []  # up to date


def test_the_question_needs_a_terminal(monkeypatch) -> None:
    from rich.console import Console

    console = Console(file=io.StringIO())
    monkeypatch.setattr(cli, "_interactive", lambda stream: False)
    assert cli._confirm("Write?", console) is None
    monkeypatch.setattr(cli, "_interactive", lambda stream: True)
    for typed, expected in (("y", True), ("YES ", True), ("", False), ("n", False)):
        monkeypatch.setattr(builtins, "input", lambda typed=typed: typed)
        assert cli._confirm("Write?", console) is expected

    def closed() -> str:
        raise EOFError

    monkeypatch.setattr(builtins, "input", closed)
    assert cli._confirm("Write?", console) is False


# ------------------------------------------------------ what sync keeps
def run_block(root: Path, scan: ScanResult, *, bullet: str = "Call `Client().send(q)`.") -> str:
    """The block `run --apply` writes: a [type-checked] note of the model's, then the notes
    from the diff (the fallback for failures whose example did not type-check)."""
    notes = scan.diff_notes()
    model_note = Note(notes[0].change, bullet, "x = 1", True, NOTE_MODEL, (TAG_TYPE_CHECKED,))
    block = render_block(
        [model_note, *notes],
        model="claude-sonnet-4-5",
        cutoff=date(2025, 7, 31),
        version_source=scan.project.version_source,
        deps=scan.deps_hash(),
        scope=SCOPE_FAILURES,
    )
    apply_block(root / "AGENTS.md", block)
    return block


def type_checked(text: str) -> list[str]:
    block = parse_block(text)
    assert block is not None
    return [
        bullet
        for section in block.packages.values()
        for bullet, tags in section.bullets
        if TAG_TYPE_CHECKED in tags
    ]


def test_type_checked_notes_stay_while_their_version_does(sc, tmp_path, cache, pypi) -> None:
    root = make_app(tmp_path)
    run_block(root, scan_app(root, cache, pypi, date(2025, 7, 31)))
    code, out = sc("sync", root, "--yes")
    assert code == EXIT_OK
    # The [type-checked] note of Client.send takes the place of the note from the diff for it.
    assert (
        "✓ Updated AGENTS.md: now for the changed APIs your code uses (it was for what "
        "`since-cutoff run` found the model getting wrong); toylib 2.0: 1 dropped." in out
    )
    block = parse_block(agents(root))
    assert block is not None and block.meta["scope"] == "used"
    bullets = block.packages["toylib"].bullets
    assert ("Call `Client().send(q)`.", (TAG_TYPE_CHECKED,)) in bullets and len(bullets) == 2
    assert not any("`Client.send()` no longer accepts" in text for text, _ in bullets)

    set_pins(root, ("toylib==2.1", "otherlib==1.0"))
    code, out = sc("sync", root, "--yes")
    # It does not suggest `run --only toylib --apply`, which writes a block of that run's
    # failures alone, in place of the whole block.
    assert (
        "toylib checked again for 2.1 (2 notes, 1 added, 1 dropped); its [type-checked] notes "
        "were for 2.0, so the notes from the diff take their place (`since-cutoff run --only "
        "toylib` tests the model again)" in out
    )
    assert "--apply" not in out
    assert not type_checked(agents(root))


def test_type_checked_notes_go_when_the_model_changes(sc, tmp_path, cache, pypi) -> None:
    root = make_app(tmp_path)
    run_block(root, scan_app(root, cache, pypi, date(2025, 7, 31)))
    code, _ = sc("sync", root, "--cutoff", "2024-12-31", "--yes")
    assert code == EXIT_OK and not type_checked(agents(root))


def test_a_block_0_3_wrote_is_upgraded_with_its_model(sc, tmp_path, cache, pypi) -> None:
    root = make_app(tmp_path)
    scan = scan_app(root, cache, pypi, date(2025, 7, 31))
    legacy = render_legacy_block(
        scan.diff_notes(),
        model="claude-sonnet-4-5",
        cutoff=date(2025, 7, 31),
        version_source="requirements",
    )
    write(root / "AGENTS.md", MINE + "\n" + legacy)
    code, out = sc("sync", root, "--check")
    assert code == EXIT_OUT_OF_DATE
    assert "(from the notes in AGENTS.md; --model to change)" in out
    assert "AGENTS.md is out of date: it is in since-cutoff 0.3's format" in out
    code, out = sc("sync", root, "--yes")
    assert "✓ Updated AGENTS.md: upgraded from since-cutoff 0.3's format" in out
    assert agents(root) == MINE + "\n" + scan.notes_block(scan.diff_notes())


def test_suggestions_are_off_by_default_and_the_block_keeps_the_choice(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    assert sc("sync", root, *SONNET, "--suggestions", "--yes")[0] == EXIT_OK
    block = parse_block(agents(root))
    assert block is not None and block.meta["suggestions"] is True
    assert sc("sync", root, "--check")[0] == EXIT_OK  # kept without the flag
    code, out = sc("sync", root, "--no-suggestions", "--check")
    assert code == EXIT_OUT_OF_DATE and "the notes would be without similar names" in out
    assert sc("sync", root, "--no-suggestions", "--yes")[0] == EXIT_OK
    assert "suggestions" not in parse_block(agents(root)).meta  # type: ignore[union-attr]


def test_scope_imported_notes_the_packages_the_code_imports(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    (root / "main.py").write_text("import toylib\n", encoding="utf-8")
    (root / "sub" / "other.py").unlink()
    code, out = sc("sync", root, *SONNET, "--yes")
    assert code == EXIT_OK and not (root / "AGENTS.md").exists()
    # The message counts what --scope imported would write (0.4.1).
    assert (
        "AGENTS.md: no notes to write; your code uses none of the APIs that changed after "
        "claude-sonnet-4-5's training cutoff (2025-07-31). `since-cutoff sync --scope imported` "
        "writes the 5 changes most likely to matter in the 1 package your code imports (up to 5 "
        "per package; --per-package N for more). Nothing written." in out
    )
    code, out = sc("sync", root, *SONNET, "--scope", "imported", "--yes")
    assert code == EXIT_OK, out
    block = parse_block(agents(root))
    assert block is not None and block.meta["scope"] == "imported"
    assert "in the packages this project imports" in agents(root)
    assert 1 <= len(block.packages["toylib"].bullets) <= 5
    assert sc("sync", root, "--check")[0] == EXIT_OK  # the block keeps its scope


# -------------------------------------------------- what sync cannot check
class DownPyPI(FakePyPI):
    """PyPI unreachable for some packages."""

    down: ClassVar[set[str]] = set()

    def releases(self, name: str) -> Any:
        if name in self.down:
            raise PackageIndexError(f"could not reach PyPI for '{name}'")
        return super().releases(name)


def test_nothing_is_written_when_a_noted_package_cannot_be_checked(
    sc, tmp_path, monkeypatch, pypi
) -> None:
    root = make_app(tmp_path)
    before = first_sync(sc, root)
    down = DownPyPI(pypi.cache, pypi._releases, pypi._trees)
    monkeypatch.setattr(
        cli, "Engine", lambda settings, **kw: Engine(settings, **{**kw, "pypi": down})
    )
    DownPyPI.down = {"toylib"}
    set_pins(root, ("toylib==2.1", "otherlib==1.0"))
    code, out = sc("sync", root, "--yes")
    assert code == 1 and agents(root) == before
    DownPyPI.down = {"otherlib"}  # a package without notes: said, and the rest is synced
    code, out = sc("sync", root, "--yes")
    assert code == EXIT_OK and "! otherlib could not be checked" in out
    DownPyPI.down = {"otherlib", "toylib"}
    assert sc("sync", root, "--yes")[0] == 1


def test_broken_markers_are_an_error(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    write(root / "AGENTS.md", f"{BLOCK_START}\nno end\n")
    code, _ = sc("sync", root, "--yes")
    assert code == 1


# ------------------------------------------------ bytes outside the block
def test_crlf_files_keep_their_line_breaks(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    mine = MINE.replace("\n", "\r\n")
    (root / "AGENTS.md").write_bytes(mine.encode())
    assert sc("sync", root, *SONNET, "--yes")[0] == EXIT_OK
    data = (root / "AGENTS.md").read_bytes()
    assert data.startswith(mine.encode()) and b"\n" not in data.replace(b"\r\n", b"")
    code, out = sc("sync", root, "--check")
    assert code == EXIT_OK, out  # the block's hashes ignore the line breaks
    set_pins(root, ("toylib==2.1", "otherlib==1.0"))
    assert sc("sync", root, "--yes")[0] == EXIT_OK
    data = (root / "AGENTS.md").read_bytes()
    assert data.startswith(mine.encode()) and b"\n" not in data.replace(b"\r\n", b"")


_LINE = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\r\n"), max_size=12
).filter(lambda line: "since-cutoff:" not in line)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=40)
@given(
    before=st.lists(_LINE, max_size=4),
    after=st.lists(_LINE, max_size=4),
    crlf=st.booleans(),
)
def test_text_outside_the_markers_stays_byte_for_byte(
    tmp_path: Path,
    cache: DiskCache,
    pypi: FakePyPI,
    before: list[str],
    after: list[str],
    crlf: bool,
) -> None:
    root = tmp_path / "prop"
    if not root.exists():
        root.mkdir()
        (root / "pyproject.toml").write_text(
            '[project]\nname = "app"\nversion = "0"\ndependencies = ["toylib==2.0"]\n'
        )
        (root / "main.py").write_text(MAIN, encoding="utf-8")
    scan = scan_app(root, cache, pypi, date(2025, 7, 31))
    nl = "\r\n" if crlf else "\n"
    head = "".join(line + nl for line in before)
    tail = "".join(line + nl for line in after)
    stale = scan.notes_block(scan.diff_notes()).replace("**toylib 2.0**", "**toylib 1.9**")
    stale = re.sub(r'"body":"[^"]+"', '"body":"sha256:000000000000"', stale)  # not "edited"
    path = root / "AGENTS.md"
    path.write_bytes((head + stale.replace("\n", nl) + tail).encode("utf-8"))
    target = read_target(root, path)
    proposal = propose(scan, target)
    assert proposal.new_text is not None and proposal.changed
    assert proposal.new_text.startswith(head) and proposal.new_text.endswith(tail)
    if crlf:
        assert "\n" not in proposal.new_text.replace("\r\n", "")


# ------------------------------------------------------------ the target files
def test_targets_are_the_files_that_have_a_block_else_the_first_choice(tmp_path) -> None:
    root = tmp_path
    assert block_targets(root) == [root / "AGENTS.md"]  # neither file: AGENTS.md
    write(root / "CLAUDE.md", "mine\n")
    assert block_targets(root) == [root / "CLAUDE.md"]  # only CLAUDE.md
    write(root / "AGENTS.md", "mine\n")
    # Both, and no block yet: AGENTS.md, as 0.3 chose (issue #13 is this case).
    assert block_targets(root) == [root / "AGENTS.md"]
    write(root / "CLAUDE.md", f"mine\n{BLOCK_START}\nx\n{BLOCK_END}\n")
    assert block_targets(root) == [root / "CLAUDE.md"]  # where the block is
    write(root / "AGENTS.md", f"{BLOCK_START}\nbroken\n")  # its reader reports it
    assert block_targets(root) == [root / "AGENTS.md", root / "CLAUDE.md"]
    assert block_targets(root, "docs/RULES.md") == [root / "docs" / "RULES.md"]  # --target wins


def test_sync_updates_the_block_where_it_is(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    write(root / "AGENTS.md", MINE)
    write(root / "CLAUDE.md", "# Claude\n")
    assert sc("sync", root, *SONNET, "--target", "CLAUDE.md", "--yes")[0] == EXIT_OK
    set_pins(root, ("toylib==2.1", "otherlib==1.0"))
    code, out = sc("sync", root, "--yes")
    assert code == EXIT_OK and "✓ Updated CLAUDE.md: toylib checked again for 2.1" in out
    assert agents(root) == MINE  # not given a second block
    # A block in both files: both are kept current, in one question.
    apply_block(
        root / "AGENTS.md", (root / "CLAUDE.md").read_text(encoding="utf-8").split("\n", 2)[2]
    )
    set_pins(root, ("toylib==2.0", "otherlib==1.0"))
    code, out = sc("sync", root, "--yes")
    assert code == EXIT_OK
    assert "Updated AGENTS.md" in out and "Updated CLAUDE.md" in out


ISSUE_13 = pytest.mark.xfail(
    strict=True,
    reason="issue #13 (good first issue): both AGENTS.md and CLAUDE.md, and no block yet",
)


@ISSUE_13
def test_13_with_an_agents_import_only_agents_md_gets_the_block(tmp_path) -> None:
    write(tmp_path / "AGENTS.md", "mine\n")
    write(tmp_path / "CLAUDE.md", "@AGENTS.md\n")
    assert block_targets(tmp_path) == [tmp_path / "AGENTS.md"]
    write(tmp_path / "CLAUDE.md", "See the rules.\n")
    assert block_targets(tmp_path) == [tmp_path / "AGENTS.md", tmp_path / "CLAUDE.md"]


@ISSUE_13
def test_13_an_import_inside_code_does_not_count(tmp_path) -> None:
    write(tmp_path / "AGENTS.md", "mine\n")
    write(tmp_path / "CLAUDE.md", "Write `@AGENTS.md` to import it.\n")
    assert block_targets(tmp_path) == [tmp_path / "AGENTS.md", tmp_path / "CLAUDE.md"]


# ---------------------------------------------- what scan says about the notes
def test_scan_points_to_sync_for_the_notes(tmp_path, cache, pypi) -> None:
    from since_cutoff.report import scan_lines, to_json

    root = make_app(tmp_path)
    (root / "main.py").write_text("from toylib import fetch\nfetch('u')\n", encoding="utf-8")
    (root / "sub" / "other.py").unlink()
    scan = scan_app(root, cache, pypi, date(2025, 7, 31))
    text = "\n".join(line.plain for line in scan_lines(scan))
    assert (
        "\n1 note ready: `since-cutoff sync` writes it to AGENTS.md and keeps it in step with "
        "pyproject.toml.\n" in text
    )
    # Where sync and run --apply would write: every file that has a block.
    write(root / "AGENTS.md", f"{BLOCK_START}\nx\n{BLOCK_END}\n")
    write(root / "CLAUDE.md", f"{BLOCK_START}\nx\n{BLOCK_END}\n")
    assert to_json(scan)["notes_preview"]["targets"] == ["AGENTS.md", "CLAUDE.md"]
    assert "writes it to AGENTS.md and CLAUDE.md" in "\n".join(
        line.plain for line in scan_lines(scan)
    )


def test_scan_suggests_scope_imported_when_the_code_uses_no_changed_api(
    tmp_path, cache, pypi
) -> None:
    from since_cutoff.report import scan_lines

    root = make_app(tmp_path)
    (root / "main.py").write_text("import toylib\n", encoding="utf-8")
    (root / "sub" / "other.py").unlink()
    scan = scan_app(root, cache, pypi, date(2025, 7, 31))
    lines = [line.plain for line in scan_lines(scan)]
    assert lines[-2:] == [
        "No notes to write. For the changes most likely to matter in the packages your code "
        "imports:",
        "  since-cutoff sync --scope imported",
    ]


def test_the_notes_keep_in_step_with_what_gives_the_versions(tmp_path) -> None:
    from since_cutoff.project import Project

    def word(source: str) -> str:
        return Project(tmp_path, [], source).versions_word

    assert word("uv.lock") == "uv.lock"
    assert word("poetry.lock, requirements.txt for 3 not in it") == "poetry.lock"
    assert word(".venv") == "your virtual environment"
    assert word("latest on PyPI, as nothing is pinned") == "your dependencies"
    assert word("") == "your dependencies"


# -------------------------------------------------------------- run --apply
@pytest.mark.pyright
def test_run_apply_updates_the_block_where_it_is(tmp_path, scripted_cli) -> None:
    from tests.conftest import ScriptedModel

    root = make_app(tmp_path, pins=("toylib==2.0",))
    write(root / "AGENTS.md", MINE)
    write(root / "CLAUDE.md", f"# Claude\n\n{BLOCK_START}\nold notes\n{BLOCK_END}\n")
    argv = ["run", str(root), *SONNET, "--max-probes", "1", "--heldout", "1"]
    scripted_cli.append(ScriptedModel())
    assert cli.main([*argv, "--regression", "0", "--apply"]) == 0
    assert agents(root) == MINE  # no second block
    claude = (root / "CLAUDE.md").read_bytes().decode("utf-8")
    assert claude.startswith("# Claude\n\n" + BLOCK_START) and "old notes" not in claude


# ------------------------------------------------------------ reading the file
def test_a_file_that_is_not_utf8_is_reported(tmp_path) -> None:
    from since_cutoff.notes import NotesFileError, has_markers, read_text

    path = tmp_path / "AGENTS.md"
    path.write_bytes(b"\xff\xfe" + BLOCK_START.encode("utf-16-le"))
    assert has_markers(path) is False and has_markers(tmp_path / "none.md") is False
    with pytest.raises(NotesFileError, match=r"AGENTS\.md is not UTF-8 text"):
        read_text(path)
    assert read_text(tmp_path / "none.md") is None


# --------------------------------------------------- pre-commit, Action, plugin
# Files of the repository, not of the package: a test skips where they are not (an sdist).


def test_pre_commit_offers_sync_and_the_offline_status_check() -> None:
    manifest = repo_file(".pre-commit-hooks.yaml")
    # The manifest's flat "key: value" lines, hook by hook (no YAML library needed).
    hooks: dict[str, dict[str, Any]] = {}
    for item in manifest.split("\n- ")[1:]:
        fields: dict[str, Any] = {
            key: value.strip("'")
            for key, value in re.findall(r"^ *([\w-]+): (.+)$", "  " + item, re.M)
        }
        fields["pass_filenames"] = fields["pass_filenames"] == "true"
        hooks[fields["id"]] = fields
    assert list(hooks) == ["since-cutoff-scan", "since-cutoff-sync", "since-cutoff-status"]
    # sync writes (pre-commit fails a hook that changes files); args: [--check] only checks.
    assert hooks["since-cutoff-sync"]["entry"] == "since-cutoff sync --yes"
    assert hooks["since-cutoff-status"]["entry"] == "since-cutoff status"
    assert "args: [--check]" in manifest
    for name in ("since-cutoff-sync", "since-cutoff-status"):
        hook = hooks[name]
        assert hook["pass_filenames"] is False and hook["language"] == "python"
        for path, hit in (
            ("uv.lock", True),
            ("sub/requirements-dev.txt", True),
            ("AGENTS.md", True),
            ("docs/CLAUDE.md", True),
            ("README.md", False),
            ("app/main.py", False),
        ):
            assert bool(re.search(hook["files"], path)) is hit, (name, path)


def test_the_action_can_check_the_notes() -> None:
    action = repo_file("action.yml")
    assert "\n  check-notes:\n" in action and "\n  notes:\n" in action
    check = re.search(r"\n  check-notes:\n(?:    .*\n)*?    default: \"(\w+)\"", action)
    assert check is not None and check.group(1) == "false"  # opt-in
    scan = action.split("\n    - name: Scan the dependencies\n", 1)[1].split("\n    - name:")[0]
    # Nothing written, a block keeps its model, and the input model serves a first one; so
    # does the input cutoff, as for the scan, but only where there is no block yet.
    assert 'notes_args=(sync "$SC_WORKDIR" --check)' in scan
    assert 'SINCE_CUTOFF_MODEL="$SC_MODEL" uvx "${from[@]}" since-cutoff "${notes_args[@]}"' in scan
    assert (
        """if [ -n "$SC_CUTOFF" ] && ! grep -qs -- '<!-- since-cutoff:start -->' """
        '"$SC_WORKDIR/AGENTS.md" "$SC_WORKDIR/CLAUDE.md"; then\n'
        '            notes_args+=(--model "$SC_MODEL" --cutoff "$SC_CUTOFF")' in scan
    )
    assert 'echo "notes-exit-code=$notes_status" >> "$GITHUB_OUTPUT"' in scan
    for code, word in (("0", "up-to-date"), ("3", "out-of-date"), ("4", "edited-by-hand")):
        assert f"{code}) notes={word} ;;" in scan
    report = action.split("\n    - name: Report the result\n", 1)[1]
    assert "steps.scan.outputs.notes-exit-code != '0'" in report
    assert 'exit "${SC_NOTES_STATUS:-0}"' in report


def test_the_plugin_says_at_session_start_when_the_notes_are_out_of_date() -> None:
    # hooks/hooks.json from the release that has `status` (0.4.0) on; until then it waits in
    # hooks/hooks.json.in (scripts/check_versions.py).
    released = (Path(__file__).resolve().parents[1] / "hooks" / "hooks.json").exists()
    hooks = json.loads(repo_file("hooks/hooks.json" if released else "hooks/hooks.json.in"))
    [entry] = hooks["hooks"]["SessionStart"]
    [hook] = entry["hooks"]
    # Pinned, as the MCP server is: scripts/check_versions.py keeps it at this release.
    version = cli.__version__ if released else "0.4.0"
    assert hook == {
        "type": "command",
        "command": f"uvx since-cutoff=={version} status --hook",
        "timeout": 60,
    }
    assert cli.build_parser().parse_args(["status", "--hook"]).hook is True


def test_per_package_sets_the_imported_budget_and_the_block_keeps_it(sc, tmp_path) -> None:
    """0.4.1: ``--scope imported`` writes up to 5 APIs per package; ``--per-package N`` changes
    that, the block records the choice (as it does --suggestions) and later syncs keep it."""
    root = make_app(tmp_path)
    (root / "main.py").write_text("import toylib\n", encoding="utf-8")
    (root / "sub" / "other.py").unlink()
    code, out = sc("sync", root, *SONNET, "--scope", "imported", "--per-package", "2", "--yes")
    assert code == EXIT_OK, out
    block = parse_block(agents(root))
    assert block is not None and block.meta["per_package"] == 2
    assert len(block.packages["toylib"].bullets) == 2
    assert "toylib 2.0: 2 notes added" in out or "Created AGENTS.md with 2 notes." in out
    # The next sync keeps the choice; --check with the default budget says what would change.
    code, out = sc("sync", root, "--yes")
    assert code == EXIT_OK and "AGENTS.md is up to date: 2 notes" in out
    code, out = sc("sync", root, "--check", "--per-package", "5")
    assert code == EXIT_OUT_OF_DATE
    assert "the notes would be for up to 5 APIs per package (--per-package)" in out
    code, out = sc("sync", root, "--per-package", "5", "--yes")
    assert code == EXIT_OK and "now up to 5 APIs per package (--per-package)" in out
    block = parse_block(agents(root))
    assert block is not None and "per_package" not in block.meta  # the default is not recorded
    assert len(block.packages["toylib"].bullets) == 5


def test_sync_rewrites_a_block_compared_from_another_day(sc, tmp_path) -> None:
    """The margin is the scan's setting, not the block's: a block written with
    ``--cutoff-margin 0`` is out of date for ``sync --check`` and rewritten by ``sync``, which
    says why; the header and the meta line then say the new day."""
    root = make_app(tmp_path)
    assert sc("sync", root, *SONNET, "--yes", "--cutoff-margin", "0")[0] == EXIT_OK
    assert "(2025-07-31) and used by this project" in agents(root)
    code, out = sc("sync", root, "--check")
    assert code == EXIT_OUT_OF_DATE
    assert (
        "AGENTS.md is out of date: the notes compare from the cutoff itself, not 30 days before "
        "the cutoff (--cutoff-margin). Run `since-cutoff sync`."
    ) in out
    code, out = sc("sync", root, "--yes")
    assert code == EXIT_OK
    assert (
        "Updated AGENTS.md: now comparing from 30 days before the cutoff (it was the cutoff "
        "itself; --cutoff-margin)."
    ) in out
    block = parse_block(agents(root))
    assert block is not None and block.meta["margin"] == 30
    assert "(2025-07-31, comparing from releases up to 2025-07-01) and used by this project" in (
        agents(root)
    )
    assert sc("sync", root, "--check")[0] == EXIT_OK
