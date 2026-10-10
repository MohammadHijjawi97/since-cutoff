"""`sync`, `status` and the block in the file, as the 0.4 review found them (plan A.5).

- the block does not change when the code passes other parameters, and a changed bullet is
  "changed";
- `status` agrees with `sync --check`: no block, or a teammate's model, is not a failure; a
  0.3 block or a `run --apply` block is;
- a `[type-checked]` note stays only while the code uses its API;
- files without a final line break, with a byte order mark, in a folder that is not there;
- the header and `status` name every file the versions come from;
- with AGENTS.md and a CLAUDE.md that does not import it, the block in both, and a tip (#13);
- smaller wording: the hint without a model setting, `--fail-on` repeated, warnings once.

The fake PyPI and the offline CLI are test_sync.py's.
"""

from __future__ import annotations

import ast
import json
import os
import re
import stat
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from rich.text import Text

from since_cutoff import cli, net
from since_cutoff.apidiff import REMOVED, APIChange
from since_cutoff.cache import DiskCache
from since_cutoff.engine import CHANGED, Engine, ModelTarget, PackageScan, ScanResult
from since_cutoff.hosts import NOT_FOUND_HINT, NOT_FOUND_HINT_CUTOFF
from since_cutoff.notes import (
    AGENTS_IMPORT_TIP,
    BLOCK_END,
    BLOCK_START,
    NOTE_MODEL,
    TAG_TYPE_CHECKED,
    TWO_COPIES_TIP,
    Note,
    agents_import_tip,
    block_targets,
    imports_agents,
    parse_block,
    remove_block,
    render_block,
    render_legacy_block,
)
from since_cutoff.project import Project, scan_file
from since_cutoff.pypi import SourceTree
from since_cutoff.report import scan_lines
from since_cutoff.sync import EXIT_OK, EXIT_OUT_OF_DATE, _counts, versions_phrase
from tests import test_sync
from tests.conftest import FakePyPI, write_tree
from tests.test_ci import scan_app
from tests.test_notes_provenance import _user_facing_strings
from tests.test_sync import MINE, SONNET, agents, first_sync, make_app, set_pins, write

pypi = test_sync.pypi
sc = test_sync.sc
ROOT = Path(__file__).resolve().parents[1]


def cli_with(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any, pypi: FakePyPI) -> Any:
    """The CLI with ``pypi``, a throwaway cache and no network: ``run(*argv) -> (code, out)``."""
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


# ------------------------------------------- the same block, whatever the code passes
SENDLIB_OLD = {
    "sendlib/__init__.py": """
        class Client:
            def send(self, message: str, temperature: float = 1.0, top_p: float = 1.0, top_k: int = 0) -> str:
                return message


        def fetch(url: str, retries: int = 3) -> bytes:
            return b""
    """
}
SENDLIB_NEW = {
    "sendlib/__init__.py": """
        class Client:
            def send(self, message: str) -> str:
                return message


        def fetch(url: str) -> bytes:
            return b""
    """
}


@pytest.fixture
def sendlib(cache: DiskCache, tmp_path: Path) -> FakePyPI:
    trees = {
        ("sendlib", v): SourceTree(
            "sendlib", v, write_tree(tmp_path / f"sendlib-{v}", files), ("sendlib",)
        )
        for v, files in (("1.0", SENDLIB_OLD), ("2.0", SENDLIB_NEW))
    }
    return FakePyPI(cache, {"sendlib": [("1.0", "2025-01-10"), ("2.0", "2025-10-01")]}, trees)


def test_passing_other_parameters_leaves_the_block_as_it_is(
    tmp_path, monkeypatch, capsys, sendlib
) -> None:
    """``sync --check`` (and the pre-commit hook, and the Action) failed with "the notes for
    huggingface-hub 2.0.0 would change (1 added, 1 dropped)" when a commit started passing
    ``proxies``: the note listed the parameters the code passes first."""
    run = cli_with(monkeypatch, tmp_path, capsys, sendlib)
    root = tmp_path / "app"
    root.mkdir()
    set_pins(root, ("sendlib==2.0",))
    main = root / "main.py"
    main.write_text("from sendlib import Client, fetch\nClient().send('q', top_k=1)\nfetch('u')\n")
    assert run("sync", root, *SONNET, "--yes")[0] == EXIT_OK
    before = agents(root)
    assert (
        "`Client.send()` no longer accepts `temperature`, `top_p` or `top_k`" in before
    )  # the old signature's order, not the code's
    main.write_text(
        "from sendlib import Client, fetch\nfetch('u', retries=2)\n"
        "Client().send('q', temperature=1)\n"
    )
    code, out = run("sync", root, "--check")
    assert code == EXIT_OK, out
    assert "AGENTS.md is up to date: 2 notes" in out and agents(root) == before


def test_a_bullet_whose_api_keeps_a_note_is_counted_as_changed() -> None:
    old = "`Messages.create()` no longer accepts `temperature`; do not pass it. [diff]"
    new = "`Messages.create()` no longer accepts `temperature` or `top_p`; do not pass them. [diff]"
    other = "`toylib.fetch()` now requires `timeout`. [diff]"
    apis = {new: "Messages.create", other: "toylib.fetch"}
    assert _counts([new], [old], apis) == "1 changed"
    assert _counts([new, other], [old], apis) == "1 added, 1 changed"
    assert _counts([other], [old], apis) == "1 added, 1 dropped"
    assert _counts([], [], apis) == "text unchanged"


def test_force_after_a_hand_edit_says_what_it_replaced_once(sc, tmp_path) -> None:
    """``sync --force`` said "replaced the text edited by hand; toylib 2.0: 1 added, 1
    dropped": the same hand edit, counted again as a note added and one dropped."""
    root = make_app(tmp_path)
    text = first_sync(sc, root).replace("do not pass it.", "do not pass it, ever.")
    write(root / "AGENTS.md", text)
    code, out = sc("sync", root, "--force", "--yes")
    assert code == EXIT_OK
    assert "✓ Updated AGENTS.md: replaced the text edited by hand." in out
    assert "added" not in out and "dropped" not in out


# ------------------------------------------------ status agrees with sync --check
@pytest.fixture
def status(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], sc: Any) -> Any:
    def run(*argv: object) -> tuple[int, str]:
        code = cli.main(["status", *[str(a) for a in argv]])
        return code, capsys.readouterr().out.replace("\r\n", "\n")

    return run


def test_nothing_to_note_is_not_a_failure(sc, status, tmp_path) -> None:
    """A project whose code uses no changed API: sync writes nothing, and ``status`` said "No
    notes yet: run `since-cutoff sync` (exit code 3)" for ever (the since-cutoff-status
    pre-commit hook could never pass)."""
    root = make_app(tmp_path)
    (root / "main.py").write_text("import json\n", encoding="utf-8")
    (root / "sub" / "other.py").unlink()
    code, out = sc("sync", root, *SONNET, "--yes")
    assert code == EXIT_OK and "Nothing written" in out and not (root / "AGENTS.md").exists()
    assert sc("sync", root, *SONNET, "--check")[0] == EXIT_OK
    code, out = status(root)
    assert code == EXIT_OK and "run `since-cutoff sync`" not in out
    assert status(root, "--hook") == (EXIT_OK, "")


def test_a_0_3_block_is_out_of_date_for_status_as_for_sync_check(
    sc, status, tmp_path, cache, pypi
) -> None:
    root = make_app(tmp_path)
    scan = scan_app(root, cache, pypi, date(2025, 7, 31))
    legacy = render_legacy_block(
        scan.diff_notes(),
        model="claude-sonnet-4-5",
        cutoff=date(2025, 7, 31),
        version_source="pyproject.toml",
    )
    write(root / "AGENTS.md", MINE + "\n" + legacy)
    assert sc("sync", root, "--check")[0] == EXIT_OUT_OF_DATE
    code, out = status(root)
    assert code == EXIT_OUT_OF_DATE
    assert "(written by since-cutoff 0.3: `since-cutoff sync` upgrades the block)" in out


def test_a_run_apply_block_is_out_of_date_for_status_as_for_sync_check(
    sc, status, tmp_path, cache, pypi
) -> None:
    """``run --apply`` writes the notes for what the model got wrong (scope "failures");
    ``sync --check`` rewrites that for the used APIs, and ``status`` said "Current"."""
    root = make_app(tmp_path)
    test_sync.run_block(root, scan_app(root, cache, pypi, date(2025, 7, 31)))
    assert sc("sync", root, "--check")[0] == EXIT_OUT_OF_DATE
    code, out = status(root)
    assert code == EXIT_OUT_OF_DATE
    assert "written by `since-cutoff run` for what it found the model getting wrong" in out
    assert sc("sync", root, "--yes")[0] == EXIT_OK
    assert status(root)[0] == EXIT_OK and sc("sync", root, "--check")[0] == EXIT_OK


# ------------------------------------------------ [type-checked] notes, per API
def test_a_type_checked_note_goes_when_the_code_no_longer_uses_its_api(
    sc, tmp_path, cache, pypi
) -> None:
    root = make_app(tmp_path)
    scan = scan_app(root, cache, pypi, date(2025, 7, 31))
    notes = scan.diff_notes()
    send = next(n for n in notes if n.api == "Client.send")
    checked = Note(
        send.change, "Call `Client().send(q)`.", "x = 1", True, NOTE_MODEL, (TAG_TYPE_CHECKED,)
    )
    block = render_block(
        [checked, *(n for n in notes if n is not send)],
        model="claude-sonnet-4-5",
        cutoff=date(2025, 7, 31),
        version_source=scan.project.version_source,
        deps=scan.deps_hash(),
    )
    assert parse_block(block).meta["checked"]  # type: ignore[union-attr]
    write(root / "AGENTS.md", block)
    assert sc("sync", root, "--check")[0] == EXIT_OK  # kept: the code still calls send
    # The code stops using Client.send: its [type-checked] note goes too.
    (root / "main.py").write_text("from toylib import fetch\nfetch('u')\n", encoding="utf-8")
    (root / "sub" / "other.py").unlink()
    code, out = sc("sync", root, "--check")
    assert code == EXIT_OUT_OF_DATE, out
    assert sc("sync", root, "--yes")[0] == EXIT_OK
    assert "Client().send(q)" not in agents(root)
    assert "`toylib.fetch()` now requires `timeout`" in agents(root)


# ------------------------------------------------------- the file around the block
def test_a_file_that_ends_at_the_end_marker_is_up_to_date(sc, status, tmp_path) -> None:
    """An editor that strips the final line break made ``sync --check`` fail for ever with
    "its header or metadata would change", while ``status`` said "Current"."""
    root = make_app(tmp_path)
    write(root / "AGENTS.md", MINE)
    text = first_sync(sc, root).rstrip("\n")
    assert text.endswith(BLOCK_END)
    write(root / "AGENTS.md", text)
    code, out = sc("sync", root, "--check")
    assert code == EXIT_OK, out
    assert status(root)[0] == EXIT_OK
    assert sc("sync", root, "--yes")[0] == EXIT_OK and agents(root) == text
    # Changed notes are written, and the file still ends where it did.
    set_pins(root, ("toylib==2.1", "otherlib==1.0"))
    assert sc("sync", root, "--yes")[0] == EXIT_OK
    assert agents(root).endswith(BLOCK_END) and "**toylib 2.1**" in agents(root)


def test_the_block_is_appended_after_a_blank_line_and_removed_byte_for_byte(sc, tmp_path) -> None:
    """A CLAUDE.md whose last line (a list item) had no line break got the start marker on the
    very next line; AGENTS.md, which had one, got a blank line."""
    root = make_app(tmp_path)
    mine = "# Rules\n\n- Uses BigQuery's public PyPI dataset"
    write(root / "AGENTS.md", mine)
    assert sc("sync", root, *SONNET, "--yes")[0] == EXIT_OK
    text = agents(root)
    assert text.startswith(mine + "\n\n" + BLOCK_START + "\n")
    assert sc("sync", root, "--check")[0] == EXIT_OK  # what it records about the file is kept
    set_pins(root, ("toylib==2.1", "otherlib==1.0"))
    assert sc("sync", root, "--yes")[0] == EXIT_OK
    assert agents(root).startswith(mine + "\n\n" + BLOCK_START + "\n")
    assert sc("unapply", root)[0] == EXIT_OK
    assert agents(root) == mine  # as it was, with no line break at the end
    crlf = "# Rules\r\n\r\n- item"
    (root / "AGENTS.md").write_bytes(crlf.encode())
    assert sc("sync", root, *SONNET, "--yes")[0] == EXIT_OK
    assert (root / "AGENTS.md").read_bytes().startswith((crlf + "\r\n\r\n" + BLOCK_START).encode())
    assert remove_block(root / "AGENTS.md") and (root / "AGENTS.md").read_bytes() == crlf.encode()


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_a_byte_order_mark_before_the_block_is_read_and_kept(sc, status, tmp_path, newline) -> None:
    """Windows editors (and PowerShell 5.1's ``Set-Content -Encoding utf8``) save a BOM first:
    the start marker was not found, and sync, status and unapply failed with "unbalanced or
    repeated since-cutoff markers"; ``status --hook`` said nothing."""
    root = make_app(tmp_path)
    text = first_sync(sc, root).replace("\n", newline)
    (root / "AGENTS.md").write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    assert sc("sync", root, "--check")[0] == EXIT_OK
    assert status(root)[0] == EXIT_OK
    set_pins(root, ("toylib==2.1", "otherlib==1.0"))
    assert status(root, "--hook")[1].startswith("since-cutoff: the library notes in AGENTS.md")
    assert sc("sync", root, "--yes")[0] == EXIT_OK
    data = (root / "AGENTS.md").read_bytes()
    assert data.startswith(b"\xef\xbb\xbf" + BLOCK_START.encode()) and b"toylib 2.1" in data
    if newline == "\r\n":
        assert b"\n" not in data.replace(b"\r\n", b"")
    assert sc("unapply", root)[0] == EXIT_OK
    assert (root / "AGENTS.md").read_bytes() == b"\xef\xbb\xbf"


def test_a_target_in_a_folder_that_is_not_there_is_created(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    code, out = sc("sync", root, *SONNET, "--target", "docs/RULES.md", "--yes")
    assert code == EXIT_OK, out
    assert parse_block((root / "docs" / "RULES.md").read_text(encoding="utf-8")) is not None


@pytest.mark.skipif(os.name != "nt" and os.geteuid() == 0, reason="root may write read-only files")
def test_a_file_that_cannot_be_written_is_an_error_not_a_traceback(sc, tmp_path, capsys) -> None:
    root = make_app(tmp_path)
    write(root / "AGENTS.md", MINE)
    (root / "AGENTS.md").chmod(stat.S_IREAD)
    try:
        code = cli.main(["sync", str(root), *SONNET, "--yes"])
    finally:
        (root / "AGENTS.md").chmod(stat.S_IREAD | stat.S_IWRITE)
    assert code == 1
    assert "error: cannot write" in capsys.readouterr().err
    assert agents(root) == MINE


# ---------------------------------------------- where the versions come from
def test_the_header_puts_only_file_names_in_code() -> None:
    change = APIChange("toylib", "1.0", "2.0", REMOVED, "toylib.old", "old", library_names=[])
    from since_cutoff.notes import diff_note

    note = diff_note([change])
    header = {
        source: render_block([note], model="m", cutoff=date(2025, 7, 31), version_source=source)
        for source in (
            "pyproject.toml, latest on PyPI for 3 unpinned",
            "requirements-dev.txt, requirements.txt",
            "poetry.lock, requirements.txt for 3 not in it",
        )
    }
    assert (
        "at the versions in `pyproject.toml`, latest on PyPI for 3 unpinned, according to"
        in header["pyproject.toml, latest on PyPI for 3 unpinned"]
    )
    assert (
        "at the versions in `requirements-dev.txt`, `requirements.txt`, according to"
        in header["requirements-dev.txt, requirements.txt"]
    )
    assert (
        "at the versions in `poetry.lock`, `requirements.txt` for 3 not in it, according to"
        in header["poetry.lock, requirements.txt for 3 not in it"]
    )


def test_status_names_every_file_the_versions_come_from(sc, status, tmp_path) -> None:
    root = make_app(tmp_path)
    set_pins(root, ())
    (root / "requirements.txt").write_text("toylib==2.0\n", encoding="utf-8")
    (root / "requirements-dev.txt").write_text("otherlib==1.0\n", encoding="utf-8")
    assert sc("sync", root, *SONNET, "--yes")[0] == EXIT_OK
    code, out = status(root)
    assert code == EXIT_OK
    assert (
        out.splitlines()[-1]
        == "Current: the notes match the versions in requirements-dev.txt, requirements.txt."
    )
    assert versions_phrase(".venv, requirements.txt for 2 not in it") == (
        "the versions in your virtual environment, requirements.txt for 2 not in it"
    )
    assert versions_phrase("latest on PyPI, as nothing is pinned") == "your dependencies"


# --------------------------------------------- AGENTS.md and CLAUDE.md (issue #13)
def _said(lines: list[Text]) -> str:
    return " ".join(" ".join(line.plain for line in lines).split())


def test_both_files_get_the_block_when_claude_md_does_not_import_agents_md(
    sc, status, tmp_path, cache, pypi
) -> None:
    """Issue #13: both files, and CLAUDE.md without ``@AGENTS.md``. Claude Code reads only
    CLAUDE.md, Codex, Cursor and Copilot only AGENTS.md, so the block goes to both, and scan,
    sync in each of its modes (stdout is not a terminal here, as in a pipe) and status say how
    to keep one copy, until CLAUDE.md imports AGENTS.md."""
    root = make_app(tmp_path)
    write(root / "AGENTS.md", MINE)
    write(root / "CLAUDE.md", "# Claude\n\nUse tabs.\n")
    tip = f"! {TWO_COPIES_TIP}."
    code, out = sc("sync", root, *SONNET, "--dry-run")
    assert code == EXIT_OK and tip in " ".join(out.split()) and "Nothing written" in out
    assert BLOCK_START not in agents(root) + (root / "CLAUDE.md").read_text()
    code, out = sc("sync", root, *SONNET, "--yes")
    assert code == EXIT_OK and tip in " ".join(out.split())
    assert BLOCK_START in agents(root) and BLOCK_START in (root / "CLAUDE.md").read_text()
    assert (root / "CLAUDE.md").read_text().startswith("# Claude\n\nUse tabs.\n")
    code, out = sc("sync", root, "--yes")  # up to date: said again
    assert code == EXIT_OK and tip in " ".join(out.split())
    code, out = status(root)
    assert code == EXIT_OK and f"  ({TWO_COPIES_TIP})" in out.splitlines()
    code, out = status(root, "--json")
    targets = {t["file"]: t for t in json.loads(out)["targets"]}
    assert code == EXIT_OK and sorted(targets) == ["AGENTS.md", "CLAUDE.md"]
    assert TWO_COPIES_TIP in targets["AGENTS.md"]["notes"]
    assert {t["state"] for t in targets.values()} == {"current"}
    assert status(root, "--hook") == (EXIT_OK, "")  # a session start is not nagged
    scan = scan_app(root, cache, pypi, date(2025, 7, 31))
    assert "writes them to AGENTS.md and CLAUDE.md" in _said(scan_lines(scan))
    assert f"{TWO_COPIES_TIP}." in _said(scan_lines(scan))
    # What the tip says: the import, and the copy in CLAUDE.md removed.
    write(root / "CLAUDE.md", (root / "CLAUDE.md").read_text() + "\n@AGENTS.md\n")
    assert sc("unapply", root, "--target", "CLAUDE.md")[0] == EXIT_OK
    code, out = sc("sync", root, "--yes")
    assert code == EXIT_OK and "does not import" not in out
    assert BLOCK_START in agents(root) and BLOCK_START not in (root / "CLAUDE.md").read_text()
    assert "does not import" not in _said(scan_lines(scan))
    assert "does not import" not in status(root)[1]


def test_with_the_import_only_agents_md_gets_the_block(sc, tmp_path, cache, pypi) -> None:
    """Issue #13: CLAUDE.md imports AGENTS.md, so Claude Code reads AGENTS.md: one copy. An
    ``@AGENTS.md`` in a code span or a fenced block is not an import, as Claude Code reads it."""
    root = make_app(tmp_path)
    write(root / "AGENTS.md", MINE)
    write(root / "CLAUDE.md", "# Claude\n\nSee @AGENTS.md for the rules.\n")
    code, out = sc("sync", root, *SONNET, "--yes")
    assert code == EXIT_OK and "does not import" not in out
    assert BLOCK_START in agents(root) and BLOCK_START not in (root / "CLAUDE.md").read_text()
    assert "writes them to AGENTS.md and keeps them" in _said(
        scan_lines(scan_app(root, cache, pypi, date(2025, 7, 31)))
    )
    for code_only in ("Write `@AGENTS.md` to import it.\n", "```\n@AGENTS.md\n```\n"):
        write(root / "CLAUDE.md", code_only)
        assert block_targets(root) == [root / "AGENTS.md"]  # the block stays where it is
        assert not imports_agents(root / "CLAUDE.md")


def test_a_tip_when_a_block_in_agents_md_alone_is_not_imported(sc, status, tmp_path) -> None:
    """A block written to AGENTS.md alone (before #13, or with ``--target AGENTS.md``) stays
    the only one; when CLAUDE.md does not import AGENTS.md, Claude Code does not read it."""
    root = make_app(tmp_path)
    write(root / "AGENTS.md", MINE)
    assert sc("sync", root, *SONNET, "--yes")[0] == EXIT_OK
    write(root / "CLAUDE.md", "# Claude\n\nUse tabs.\n")
    code, out = sc("sync", root, "--yes")
    assert code == EXIT_OK and f"! {AGENTS_IMPORT_TIP}." in " ".join(out.split())
    assert BLOCK_START not in (root / "CLAUDE.md").read_text()
    assert f"  ({AGENTS_IMPORT_TIP})" in status(root)[1].splitlines()
    code, out = sc("sync", root, *SONNET, "--target", "CLAUDE.md", "--dry-run")
    assert code == EXIT_OK and "does not import" not in out


def test_the_import_after_a_fenced_block_in_a_crlf_claude_md(sc, tmp_path) -> None:
    """A CLAUDE.md with Windows line breaks: the fence closes on its ``\\r\\n`` line, so the
    ``@AGENTS.md`` after it is an import and only AGENTS.md gets the block."""
    root = make_app(tmp_path)
    write(root / "AGENTS.md", MINE)
    crlf = "# C\r\n\r\n```\r\nmake test\r\n```\r\n\r\n@AGENTS.md\r\n"
    (root / "CLAUDE.md").write_bytes(crlf.encode())
    assert imports_agents(root / "CLAUDE.md")
    assert block_targets(root) == [root / "AGENTS.md"]
    code, out = sc("sync", root, *SONNET, "--yes")
    assert code == EXIT_OK and "does not import" not in out
    assert (root / "CLAUDE.md").read_bytes() == crlf.encode()


@pytest.mark.parametrize("link", ["hard", "symbolic"])
def test_a_claude_md_that_is_a_link_to_agents_md_is_one_file(sc, status, tmp_path, link) -> None:
    """``ln AGENTS.md CLAUDE.md`` (or ``ln -s``): one file, which Claude Code reads, so one
    target, AGENTS.md, and no tip to import it into itself."""
    root = make_app(tmp_path)
    write(root / "AGENTS.md", MINE)
    try:
        if link == "hard":
            os.link(root / "AGENTS.md", root / "CLAUDE.md")
        else:
            (root / "CLAUDE.md").symlink_to("AGENTS.md")
    except OSError as exc:  # a symbolic link needs a privilege on Windows
        pytest.skip(f"cannot make a {link} link here: {exc}")
    agents_md = root / "AGENTS.md"
    assert block_targets(root) == [agents_md]
    assert agents_import_tip(root, [agents_md]) is None
    code, out = sc("sync", root, *SONNET, "--yes")
    assert code == EXIT_OK and "does not import" not in out and "CLAUDE.md" not in out
    assert agents(root).count(BLOCK_START) == 1
    assert block_targets(root) == [agents_md]  # the block is under both names
    code, out = status(root)
    assert code == EXIT_OK and "CLAUDE.md" not in out


def test_a_claude_md_that_is_not_utf8_gets_no_block(sc, status, tmp_path) -> None:
    """A CLAUDE.md that cannot be read: whether it imports AGENTS.md cannot be told, so the
    notes go to AGENTS.md alone, as before #13, and sync and status still work."""
    root = make_app(tmp_path)
    write(root / "AGENTS.md", MINE)
    latin1 = b"# Caf\xe9 rules\n\n@AGENTS.md\n"
    (root / "CLAUDE.md").write_bytes(latin1)
    assert block_targets(root) == [root / "AGENTS.md"]
    assert agents_import_tip(root, [root / "AGENTS.md"]) is None
    code, out = sc("sync", root, *SONNET, "--yes")
    assert code == EXIT_OK and "does not import" not in out
    assert BLOCK_START in agents(root) and (root / "CLAUDE.md").read_bytes() == latin1
    assert status(root)[0] == EXIT_OK


# ---------------------------------------------------------------- wording
def test_sync_without_a_model_setting_does_not_say_it_tests_the_model(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    code, out = sc("sync", root, "--dry-run")
    out = " ".join(out.split())
    assert code == EXIT_OK and NOT_FOUND_HINT_CUTOFF in out and NOT_FOUND_HINT not in out


def test_fail_on_may_be_given_twice(sc, tmp_path, capsys) -> None:
    """``--fail-on used --fail-on old-form`` kept only the last: a used API not in the old form
    passed the job."""
    root = make_app(tmp_path)
    (root / "main.py").write_text("from toylib import fetch\nfetch('u')\n", encoding="utf-8")
    (root / "sub" / "other.py").unlink()
    argv = ("scan", root, *SONNET)
    assert sc(*argv, "--fail-on", "old-form")[0] == EXIT_OK
    code, out = sc(*argv, "--fail-on", "used", "--fail-on", "old-form")
    assert code == 3 and "--fail-on used: your code uses 1 API" in out


def test_a_scan_warning_is_printed_once(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    set_pins(root, ())
    (root / "requirements.txt").write_text("toylib==2.0\n", encoding="utf-8")
    (root / "poetry.lock").write_text('[[package]]\nname = "toylib"\nversion = "1.0"\n')
    for extra in ([], ["--all"]):
        code, out = sc("scan", root, *SONNET, *extra)
        assert code == EXIT_OK and out.count("! ignored poetry.lock") == 1, (extra, out)


def test_the_line_about_the_other_changes_wraps_with_the_legend(tmp_path) -> None:
    packages = []
    for i in range(5):
        p = PackageScan(
            f"package-number-{i}", "2.0", "uv.lock", True, True, CHANGED, cutoff_version="1.0"
        )
        p.import_names = [f"pkg{i}"]
        p.changes = [
            APIChange(
                p.name,
                "1.0",
                "2.0",
                REMOVED,
                f"pkg{i}.gone{j}",
                f"gone{j}",
                import_paths=[f"pkg{i}.gone{j}"],
            )
            for j in range(30)
        ]
        packages.append(p)
    code = "from pkg0 import gone0\n"
    project = Project(
        tmp_path,
        [],
        "uv.lock",
        imported_modules={"pkg0"},
        files=[scan_file(ast.parse(code), "a.py")],
    )
    scan = ScanResult(project, ModelTarget.cutoff_only(date(2025, 7, 31)), packages)
    lines = [line.plain for line in scan_lines(scan, width=100, page=160)]
    other = [i for i, line in enumerate(lines) if line.startswith("Also changed")]
    assert other and all(len(line) <= 100 for line in lines[other[0] :])
    assert isinstance(scan_lines(scan)[0], Text)


def test_the_plugin_and_skill_descriptions_say_what_is_checked() -> None:
    """Plan C.6: no "verified" in what users and models read about the plugin and the skill."""
    verified = re.compile(r"\bverif(?:y|ied|ies|ying|ication)\b", re.IGNORECASE)
    c6 = (
        "Find which dependency APIs your code uses changed after your coding model's training "
        "cutoff, and write short AGENTS.md notes from the API diff, each with its source."
    )
    for name in (
        "plugin.json",
        ".claude-plugin/plugin.json",
        ".claude-plugin/marketplace.json",
        ".codex-plugin/plugin.json",
        "gemini-extension.json",
        "server.json",
        "skills/since-cutoff/SKILL.md",
    ):
        path = ROOT / name
        if not path.exists():
            continue  # an sdist
        text = path.read_text(encoding="utf-8")
        assert not verified.search(text), name
        if name.endswith(".json") and name not in ("gemini-extension.json", "server.json"):
            data = json.loads(text)
            entries = data.get("plugins", [data])
            assert all(e["description"] == c6 for e in entries), name
    # And the source's user-facing strings, as test_notes_provenance checks them.
    assert all(
        not verified.search(s) or re.fullmatch(r"[a-z_]+", s)
        for path in (ROOT / "src" / "since_cutoff").glob("*.py")
        for _, s in _user_facing_strings(path)
    )
