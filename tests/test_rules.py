"""Path-scoped rule files (issue #16): ``sync --target claude-rules`` (``cursor-rules``,
``copilot-instructions``) writes one file per changed package, whose front matter names the
files that use it (a file at the root as itself, the others by their folder), with the same
block, markers and hash as AGENTS.md's; ``status``, ``sync --check`` and ``unapply`` handle
them, and a file since-cutoff did not write, or one outside the project, is left alone.

The toy project and the fake PyPI are test_sync.py's: toylib 2.0 changed after the cutoff
2025-07-31, and main.py and sub/other.py use it. No network, and no model is called."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from since_cutoff import cli
from since_cutoff import rules as rules_module
from since_cutoff.notes import BLOCK_END, BLOCK_START, NotesFileError, _write, parse_block
from since_cutoff.rules import (
    FORMATS,
    MAX_GLOBS,
    front_edited,
    front_hash,
    glob_of,
    globs_for,
    split_front,
    with_rule,
    without_rule,
)
from since_cutoff.sync import EXIT_EDITED, EXIT_OK, EXIT_OUT_OF_DATE
from tests import test_sync
from tests.test_sync import MINE, SONNET, make_app, set_pins, write

# The fixtures of test_sync.py: the fake PyPI, and the CLI without network (``sc``).
pypi = test_sync.pypi
sc = test_sync.sc

CLAUDE = ".claude/rules/since-cutoff-toylib.md"
CURSOR = ".cursor/rules/since-cutoff-toylib.mdc"
COPILOT = ".github/instructions/since-cutoff-toylib.instructions.md"
FILES = {"claude-rules": CLAUDE, "cursor-rules": CURSOR, "copilot-instructions": COPILOT}
USES = "from toylib import fetch\nfetch('u')\n"
CLAUDE_FRONT = '---\npaths:\n  - "main.py"\n  - "sub/**/*.py"\n---\n'
git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def read(root: Path, name: str) -> str:
    return (root / name).read_bytes().decode("utf-8")


def put(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write(path, text)


def flat(text: str) -> str:
    return " ".join(text.split())


def errors(capsys: pytest.CaptureFixture[str], *argv: object) -> tuple[int, str]:
    """The CLI's exit code and what it printed to stderr, in one line (with ``sc``'s fakes)."""
    code = cli.main([str(a) for a in argv])
    return code, flat(capsys.readouterr().err)


def rules(sc: Any, root: Path, target: str = "claude-rules", *flags: str) -> str:
    code, out = sc("sync", root, *SONNET, "--yes", "--target", target, *flags)
    assert code == EXIT_OK, out
    return read(root, FILES[target])


def status_files(sc: Any, root: Path) -> list[str]:
    _, out = sc("status", root, "--json")
    return [t["file"] for t in json.loads(out)["targets"] if t["state"] != "never_run"]


def link_folder(link: Path, target: Path) -> None:
    """``link`` as a link to the folder ``target`` (a junction on Windows: no admin needed)."""
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform == "win32":
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"cannot link a folder here: {exc}")


def git_init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)


# ------------------------------------------------------------------ writing
def test_claude_rules_name_the_files_that_use_the_package(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    code, out = sc("sync", root, *SONNET, "--yes", "--target", "claude-rules")
    assert code == EXIT_OK, out
    assert f"+++ {CLAUDE} (since-cutoff sync)" in out
    assert f"✓ Created {CLAUDE} with 2 notes." in out
    text = read(root, CLAUDE)
    front, rest = split_front(text)
    # main.py at the root as itself; sub/other.py by its folder, so that a file added there
    # later loads the notes too.
    assert front == CLAUDE_FRONT
    assert rest.startswith(BLOCK_START + "\n")
    block = parse_block(text)
    assert block is not None and block.edited is False
    assert set(block.packages) == {"toylib"} and block.packages["toylib"].version == "2.0"
    # The meta line has the front matter's hash, not the files again (an HTML comment the
    # agents may load with the notes).
    assert "paths" not in block.meta and block.meta["front"] == front_hash(front)
    # The notes are where the agent loads them only for those files: AGENTS.md is not written.
    assert not (root / "AGENTS.md").exists() and not (root / "CLAUDE.md").exists()


def test_cursor_and_copilot_front_matter_as_their_docs_give_it(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    cursor = rules(sc, root, "cursor-rules")
    # Cursor's auto-attached rule: globs and alwaysApply: false, no description.
    assert split_front(cursor)[0] == ("---\nglobs: main.py, sub/**/*.py\nalwaysApply: false\n---\n")
    copilot = rules(sc, root, "copilot-instructions")
    assert split_front(copilot)[0] == (
        "---\n"
        'description: "toylib 2.0: API changes after the model\'s training cutoff"\n'
        'applyTo: "main.py,sub/**/*.py"\n'
        "---\n"
    )
    # The same notes in each: the block after the front matter differs only in its meta line.
    for text in (cursor, copilot):
        block = parse_block(text)
        assert block is not None and len(block.packages["toylib"].bullets) == 2


def test_the_rule_formats_are_the_ones_the_cli_names() -> None:
    """``cli.RULE_TARGETS`` is a copy, so that --help does not import notes."""
    assert tuple(FORMATS) == cli.RULE_TARGETS


def test_imported_scope_rules_apply_to_every_file_that_imports_the_package(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    (root / "plain.py").write_text("import toylib\n", encoding="utf-8")
    text = rules(sc, root, "claude-rules", "--scope", "imported")
    block = parse_block(text)
    assert block is not None and block.meta["scope"] == "imported"
    assert FORMATS["claude-rules"].globs_in(split_front(text)[0]) == [
        "main.py",
        "plain.py",
        "sub/**/*.py",
    ]
    # Used scope: only the files that use a changed API.
    cursor = rules(sc, root, "cursor-rules")
    assert "globs: main.py, sub/**/*.py\n" in cursor


def test_globs_match_each_file_and_nothing_breaks_the_front_matter() -> None:
    assert glob_of("app/main.py") == "app/main.py"
    assert glob_of("a [draft]/b,c.py") == "a??draft?/b?c.py"
    assert glob_of('we"ird#{x}*.py') == "we?ird??x??.py"
    assert glob_of("@scope/x.py") == "?scope/x.py"
    assert glob_of("café/ü.py") == "café/ü.py"
    assert glob_of("a-->b/x.py") == "a--?b/x.py"  # nothing in it can end an HTML comment
    # An emoji: the JavaScript engines' ``?`` matches half of one, so ``*`` (not first).
    assert glob_of("emoji/\U0001f600.py") == "emoji/*.py"
    assert glob_of("\U0001f600/x.py") == "?/x.py"
    claude = FORMATS["claude-rules"].front_matter(['x "y".py'], "toylib", "2.0")
    assert claude == '---\npaths:\n  - "x??y?.py"\n---\n'
    # No file known: a rule that applies everywhere.
    assert FORMATS["claude-rules"].front_matter([], "toylib", "2.0") == ""
    assert FORMATS["cursor-rules"].front_matter([], "toylib", "2.0") == (
        "---\nalwaysApply: true\n---\n"
    )
    assert (
        FORMATS["copilot-instructions"]
        .front_matter([], "toylib", "2.0")
        .endswith('applyTo: "**"\n---\n')
    )
    for f in FORMATS.values():
        assert f.globs_in(f.front_matter(["a.py", "b/c.py"], "toylib", "2.0")) == [
            "a.py",
            "b/**/*.py",
        ]
        assert f.globs_in(f.front_matter([], "toylib", "2.0")) == []


def test_globs_name_folders_and_stay_few() -> None:
    assert globs_for(["main.py", "app/a.py", "app/api/b.py", "lib/x/y.py"]) == [
        "app/**/*.py",
        "lib/x/**/*.py",
        "main.py",
    ]
    # More folders than MAX_GLOBS: by their top-level folder.
    many = [f"pkg/m{i:02}/f.py" for i in range(MAX_GLOBS + 5)]
    assert globs_for([*many, "main.py"]) == ["main.py", "pkg/**/*.py"]
    # Still more: the rule applies always.
    assert globs_for([f"top{i:02}/f.py" for i in range(MAX_GLOBS + 5)]) == []


def test_a_package_used_in_many_files_gets_a_short_front_matter(sc, tmp_path) -> None:
    """602 using files made a Copilot applyTo of 13,840 characters, which VS Code puts in
    every request: the folders collapse instead."""
    root = make_app(tmp_path)
    for i in range(MAX_GLOBS + 5):
        for j in range(2):
            put(root / "pkg" / f"mod{i:02}" / f"f{j}.py", USES)
    copilot = rules(sc, root, "copilot-instructions")
    assert 'applyTo: "main.py,pkg/**/*.py,sub/**/*.py"\n' in copilot
    assert len(split_front(copilot)[0]) < 200


def test_with_and_without_rule_keep_text_of_ones_own() -> None:
    front = '---\napplyTo: "a.py"\n---\n'
    block = f"{BLOCK_START}\nnotes\n{BLOCK_END}\n"
    text, action = with_rule(None, front, block, "x.md")
    assert (text, action) == (front + block, "created")
    assert without_rule(text, "x.md") is None  # nothing of the user's: the file goes
    mine = text + "\nMy own line.\n"
    new, action = with_rule(mine, '---\napplyTo: "b.py"\n---\n', block, "x.md")
    assert action == "updated" and new.endswith("My own line.\n") and 'applyTo: "b.py"' in new
    assert without_rule(mine, "x.md") == front + "My own line.\n"
    crlf = text.replace("\n", "\r\n")
    assert with_rule(crlf, front, block, "x.md")[0] == crlf
    # A byte order mark stays first, and is not part of the front matter.
    bom = "﻿" + mine
    assert split_front(bom) == (front, block + "\nMy own line.\n")
    assert with_rule(bom, front, block, "x.md")[0] == bom
    assert without_rule(bom, "x.md") == "﻿" + front + "My own line.\n"
    # A block without a front hash was not written for a rule file: no edit to tell.
    assert front_edited('---\napplyTo: "c.py"\n---\n', {}) is False


# ---------------------------------------------------------- keeping in step
def test_a_second_sync_leaves_the_rule_files_alone_and_writes_no_agents_md(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    before = rules(sc, root)
    stamp = (root / CLAUDE).stat().st_mtime_ns
    code, out = sc("sync", root)  # no --target: the rule files are where the notes are
    assert code == EXIT_OK, out
    assert f"{CLAUDE} is up to date: 2 notes for claude-sonnet-4-5 (cutoff 2025-07-31)" in out
    assert f"(from the notes in {CLAUDE}; --model to change)" in out
    assert read(root, CLAUDE) == before and (root / CLAUDE).stat().st_mtime_ns == stamp
    assert not (root / "AGENTS.md").exists()
    assert sc("sync", root, "--check")[0] == EXIT_OK
    assert sc("status", root)[0] == EXIT_OK


def test_a_file_in_a_new_folder_adds_a_glob_and_one_next_to_the_others_needs_none(
    sc, tmp_path
) -> None:
    root = make_app(tmp_path)
    rules(sc, root, "cursor-rules")
    write(root / "sub" / "more.py", USES)  # sub/**/*.py has it already
    assert sc("sync", root, "--check")[0] == EXIT_OK
    put(root / "lib" / "more.py", USES)
    code, out = sc("sync", root, "--check")
    assert code == EXIT_OUT_OF_DATE, out
    assert f"{CURSOR} is out of date: the files it is loaded for would change (1 added)." in out
    code, out = sc("sync", root, "--yes")
    assert code == EXIT_OK and f"✓ Updated {CURSOR}: now loaded for 3 globs (1 added)." in out
    assert "globs: lib/**/*.py, main.py, sub/**/*.py\n" in read(root, CURSOR)
    assert sc("sync", root, "--check")[0] == EXIT_OK


def test_status_and_check_see_a_bumped_package_in_a_rule_file(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    rules(sc, root, "copilot-instructions")
    set_pins(root, ("toylib==2.1", "otherlib==1.0"))
    code, out = sc("status", root)
    assert code == EXIT_OUT_OF_DATE, out
    assert f"{COPILOT}: notes for claude-sonnet-4-5 (cutoff 2025-07-31)" in out
    code, out = sc("status", root, "--json")
    [target] = json.loads(out)["targets"]
    assert target["file"] == COPILOT and target["state"] == "out_of_date"
    assert sc("sync", root, "--check")[0] == EXIT_OUT_OF_DATE
    code, out = sc("sync", root, "--yes")
    assert code == EXIT_OK and f"✓ Updated {COPILOT}: toylib checked again for 2.1" in out
    assert 'description: "toylib 2.1: API changes' in read(root, COPILOT)
    assert parse_block(read(root, COPILOT)).packages["toylib"].version == "2.1"  # type: ignore[union-attr]


@pytest.mark.parametrize("target", list(FILES))
def test_a_hand_edit_of_the_front_matter_or_the_notes_shows(sc, tmp_path, target) -> None:
    root = make_app(tmp_path)
    name = FILES[target]
    text = rules(sc, root, target)
    for edited in (
        text.replace("main.py", "everything.py", 1),
        text.replace("do not pass it.", "do not pass it, ever."),
    ):
        write(root / name, edited)
        code, out = sc("sync", root, "--check")
        assert code == EXIT_EDITED, out
        assert f"The since-cutoff block in {name} was edited by hand" in out
        code, out = sc("status", root)
        assert "edited by hand since since-cutoff wrote it" in out
        # status --json says it too, for the front matter as for the notes.
        code, out = sc("status", root, "--json")
        assert json.loads(out)["targets"][0]["edited"] is True
        assert read(root, name) == edited
    code, out = sc("sync", root, "--yes", "--force")
    assert code == EXIT_OK and "replaced the text edited by hand" in out
    assert read(root, name) == text


def test_a_package_with_no_notes_left_loses_its_file(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    rules(sc, root)
    set_pins(root, ("toylib==1.0", "otherlib==1.0"))
    code, out = sc("sync", root, "--check")
    assert code == EXIT_OUT_OF_DATE
    assert f"{CLAUDE} is out of date: toylib 1.0 in pyproject.toml is the latest release" in out
    code, out = sc("sync", root, "--yes")
    assert code == EXIT_OK, out
    assert (
        f"✓ Removed {CLAUDE}: toylib dropped (1.0 is the latest release 30 days before the "
        "cutoff)." in out
    )
    assert not (root / CLAUDE).exists()
    assert "Text outside the since-cutoff markers is unchanged" not in out  # the file went
    # The last one went: a plain sync no longer knows where the notes went, nor for which
    # model, so sync says so, with the command that writes them there again.
    again = "since-cutoff sync --target claude-rules --model claude-sonnet-4-5 --cutoff 2025-07-31"
    assert (
        "No since-cutoff rule files are left in .claude/rules: when a package needs notes "
        f"again, `{again}` writes them there (a plain sync writes AGENTS.md instead, for the "
        "model your coding agent is set up with)." in flat(out)
    )
    code, out = sc("sync", root, *again.split()[2:])
    assert code == EXIT_OK
    assert ".claude/rules: no notes to write; your code uses none of the APIs" in out
    set_pins(root, ("toylib==2.0", "otherlib==1.0"))
    code, out = sc("sync", root, *again.split()[2:], "--yes")
    assert code == EXIT_OK and f"✓ Created {CLAUDE} with 2 notes." in out, out


@pytest.mark.parametrize(
    ("given", "again"),
    [
        (
            ["--model", "anthropic:claude-haiku-4-5", "--cutoff", "2025-12-31"],
            "--model claude-haiku-4-5 --cutoff 2025-12-31",
        ),
        (["--cutoff", "2025-12-31"], "--cutoff 2025-12-31"),
        (
            ["--cutoff", "2025-12-31", "--cutoff-margin", "0"],
            "--cutoff 2025-12-31 --cutoff-margin 0",
        ),
    ],
)
def test_the_command_after_the_last_file_went_is_for_the_model_sync_ran_for(
    sc, tmp_path, given, again
) -> None:
    """A sync for another model or cutoff removed the last rule file: the command that writes
    the notes there again is for what that sync ran for, not for what the files were for."""
    root = make_app(tmp_path)
    rules(sc, root)  # claude-sonnet-4-5, cutoff 2025-07-31
    # By then toylib 2.1 is out: 2.0 is no newer, and its notes and file go.
    code, out = sc("sync", root, *given, "--yes")
    assert code == EXIT_OK, out
    assert not (root / CLAUDE).exists()
    assert (
        "No since-cutoff rule files are left in .claude/rules: when a package needs notes "
        f"again, `since-cutoff sync --target claude-rules {again}` writes them there" in flat(out)
    )
    assert "claude-sonnet-4-5 --cutoff 2025-07-31" not in flat(out)


def commit_all(root: Path, message: str) -> str:
    """Commit everything in ``root`` (no signing, whoever the user is); the short hash."""
    flags = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", *flags, "-C", str(root), "commit", "-q", "-m", message], check=True)
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True, check=True
    )
    return head.stdout.decode().strip()


@git
def test_a_plain_sync_says_when_the_notes_were_in_rule_files_until_the_last_went(
    sc, tmp_path
) -> None:
    """The last rule file went with its package's notes; a later plain sync (CI, a teammate)
    created AGENTS.md and said nothing of the rule files the project had."""
    root = make_app(tmp_path)
    git_init(root)
    rules(sc, root)
    commit_all(root, "notes")
    set_pins(root, ("toylib==1.0", "otherlib==1.0"))
    assert sc("sync", root, "--yes")[0] == EXIT_OK and not (root / CLAUDE).exists()
    removed = commit_all(root, "toylib 1.0")
    set_pins(root, ("toylib==2.0", "otherlib==1.0"))
    code, out = sc("sync", root, *SONNET, "--check")
    assert code == EXIT_OUT_OF_DATE
    assert (
        f"! .claude/rules had since-cutoff rule files until commit {removed}, which removed the "
        "last of them: `since-cutoff sync --target claude-rules --model claude-sonnet-4-5 "
        "--cutoff 2025-07-31` writes the notes there instead of into AGENTS.md" in flat(out)
    )
    # Once AGENTS.md has its block, nothing more is said; nor where no rule file ever went.
    code, out = sc("sync", root, *SONNET, "--yes")
    assert code == EXIT_OK and "had since-cutoff rule files" in out
    assert "had since-cutoff rule files" not in sc("sync", root, "--check")[1]
    other = make_app(tmp_path / "other")
    git_init(other)
    commit_all(other, "app")
    assert "had since-cutoff rule files" not in sc("sync", other, *SONNET, "--check")[1]


@git
def test_the_command_for_rule_files_a_commit_removed_is_for_the_model_sync_was_given(
    sc, tmp_path, monkeypatch
) -> None:
    """The removed files were for claude-sonnet-4-5; a plain sync given another model names
    that one (the notes it is about to write are for it), and the removed files' model only
    when it was given none."""
    root = make_app(tmp_path)
    git_init(root)
    rules(sc, root)
    commit_all(root, "notes")
    set_pins(root, ("toylib==1.0", "otherlib==1.0"))
    assert sc("sync", root, "--yes")[0] == EXIT_OK and not (root / CLAUDE).exists()
    removed = commit_all(root, "toylib 1.0")
    set_pins(root, ("toylib==2.0", "otherlib==1.0"))
    head = f"! .claude/rules had since-cutoff rule files until commit {removed}, which removed "
    code, out = sc(
        "sync", root, "--model", "anthropic:claude-haiku-4-5", "--cutoff", "2024-12-31", "--check"
    )
    assert code == EXIT_OUT_OF_DATE, out
    assert (
        f"{head}the last of them: `since-cutoff sync --target claude-rules --model "
        "claude-haiku-4-5 --cutoff 2024-12-31` writes the notes there instead of into AGENTS.md"
        in flat(out)
    )
    code, out = sc("sync", root, "--cutoff", "2024-12-31", "--scope", "imported", "--check")
    assert code == EXIT_OUT_OF_DATE, out
    assert (
        f"{head}the last of them: `since-cutoff sync --target claude-rules --cutoff 2024-12-31 "
        "--scope imported` writes the notes there" in flat(out)
    )
    # Given no model, the plain sync uses the coding agent's; the command, the removed files'.
    monkeypatch.setenv("SINCE_CUTOFF_MODEL", "anthropic:claude-haiku-4-5")
    code, out = sc("sync", root, "--check")
    assert code == EXIT_OUT_OF_DATE, out
    assert "claude-haiku-4-5" in out
    assert (
        f"{head}the last of them: `since-cutoff sync --target claude-rules --model "
        "claude-sonnet-4-5 --cutoff 2025-07-31` writes the notes there" in flat(out)
    )


def test_text_of_ones_own_keeps_the_file_since_cutoffs_when_its_notes_go(sc, tmp_path) -> None:
    """The user's text after the block (where the hand-edit message says to move it) stays
    with an empty block, so that the notes come back into the same file, under the same model,
    and not into a new AGENTS.md."""
    root = make_app(tmp_path)
    text = rules(sc, root) + "\nAlso: prefer fetch.\n"
    write(root / CLAUDE, text)
    set_pins(root, ("toylib==1.0", "otherlib==1.0"))
    code, out = sc("sync", root, "--yes")
    assert code == EXIT_OK, out
    assert f"✓ Removed the notes from {CLAUDE}: toylib dropped" in out
    assert "Your own text in it stays" in flat(out)
    after = read(root, CLAUDE)
    front, rest = split_front(after)
    assert front == CLAUDE_FRONT
    assert rest.startswith(f"{BLOCK_START}\n<!-- since-cutoff:meta ")
    assert rest.endswith(f" -->\n{BLOCK_END}\n\nAlso: prefer fetch.\n")
    block = parse_block(after)
    assert block is not None and block.packages == {} and block.edited is False
    assert sc("sync", root, "--check")[0] == EXIT_OK
    assert sc("status", root)[0] == EXIT_OK
    assert sc("sync", root, "--yes")[1].count("Nothing written") == 1  # nothing to change
    set_pins(root, ("toylib==2.0", "otherlib==1.0"))
    code, out = sc("sync", root, "--yes")
    assert code == EXIT_OK and f"✓ Updated {CLAUDE}" in out, out
    assert "(from the notes in .claude/rules" in out  # the model the notes were written for
    assert read(root, CLAUDE) == text
    assert not (root / "AGENTS.md").exists()
    # unapply leaves the user's text, under its front matter.
    assert sc("unapply", root)[0] == EXIT_OK
    assert read(root, CLAUDE) == CLAUDE_FRONT + "Also: prefer fetch.\n"


def test_an_emptied_file_another_since_cutoff_wrote_is_left_as_it_is(sc, tmp_path) -> None:
    """As a block with notes: the same (no) notes, as that version of since-cutoff wrote them,
    are not written again for this one's version alone."""
    root = make_app(tmp_path)
    write(root / CLAUDE, rules(sc, root) + "\nMine.\n")
    set_pins(root, ("toylib==1.0", "otherlib==1.0"))
    assert sc("sync", root, "--yes")[0] == EXIT_OK
    text = read(root, CLAUDE)
    tool = parse_block(text).meta["tool"]  # type: ignore[union-attr]
    older = text.replace(f'"tool":"{tool}"', '"tool":"0.6.0"')
    assert older != text
    write(root / CLAUDE, older)
    code, out = sc("sync", root, "--yes")
    assert code == EXIT_OK and "Nothing written" in out, out
    assert read(root, CLAUDE) == older
    assert sc("sync", root, "--check")[0] == EXIT_OK


def test_an_emptied_file_whose_own_text_goes_is_said_to_go(sc, tmp_path) -> None:
    """It was "its metadata written again", for a file sync removed."""
    root = make_app(tmp_path)
    text = rules(sc, root)
    write(root / CLAUDE, text + "\nMine.\n")
    set_pins(root, ("toylib==1.0", "otherlib==1.0"))
    assert sc("sync", root, "--yes")[0] == EXIT_OK
    write(root / CLAUDE, read(root, CLAUDE).replace("\nMine.\n", ""))
    code, out = sc("sync", root, "--check")
    assert code == EXIT_OUT_OF_DATE
    assert (
        f"{CLAUDE} is out of date: it has no notes and no text of your own left, so the file "
        "would go." in out
    )
    code, out = sc("sync", root, "--yes", "--json")
    assert code == EXIT_OK and not (root / CLAUDE).exists()
    [change] = json.loads(out)["targets"][0]["changes"]
    assert change["done"] == "no notes and no text of your own left"


@pytest.mark.parametrize("bom", ["", "﻿"])
def test_a_crlf_file_whose_notes_go_keeps_its_front_matter_as_it_was(sc, tmp_path, bom) -> None:
    """The front matter read back from a CRLF file (Git for Windows checks files out so) was
    given CRLF line breaks a second time (``\\r\\r\\n``): the file then counted as edited by hand
    for good, and `--force` wrote a second front matter ahead of it."""
    root = make_app(tmp_path)
    text = bom + (rules(sc, root) + "\nMine.\n").replace("\n", "\r\n")
    write(root / CLAUDE, text)
    assert sc("sync", root, "--check")[0] == EXIT_OK
    set_pins(root, ("toylib==1.0", "otherlib==1.0"))
    code, out = sc("sync", root, "--yes")
    assert code == EXIT_OK and f"✓ Removed the notes from {CLAUDE}" in out, out
    after = read(root, CLAUDE)
    assert "\r\r" not in after and after.count("\r\n") == after.count("\n")
    assert after.startswith(bom + CLAUDE_FRONT.replace("\n", "\r\n") + BLOCK_START + "\r\n")
    code, out = sc("status", root)
    assert code == EXIT_OK and "edited by hand" not in out, out
    assert sc("sync", root, "--check")[0] == EXIT_OK
    # And back: the notes come into the same file, as it was.
    set_pins(root, ("toylib==2.0", "otherlib==1.0"))
    code, out = sc("sync", root, "--yes")
    assert code == EXIT_OK and f"✓ Updated {CLAUDE}" in out, out
    assert read(root, CLAUDE) == text
    assert sc("unapply", root)[0] == EXIT_OK
    assert read(root, CLAUDE) == bom + (CLAUDE_FRONT + "Mine.\n").replace("\n", "\r\n")


def test_a_byte_order_mark_is_no_hand_edit_and_stays_first(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    text = rules(sc, root)
    write(root / CLAUDE, "﻿" + text)
    assert sc("sync", root, "--check")[0] == EXIT_OK
    code, out = sc("status", root, "--json")
    [target] = json.loads(out)["targets"]
    assert target["edited"] is False and target["notes"] == []
    put(root / "lib" / "more.py", USES)
    code, out = sc("sync", root, "--yes")
    assert code == EXIT_OK, out
    after = read(root, CLAUDE)
    assert after.startswith("﻿---\npaths:\n") and after.count("paths:") == 1
    assert sc("sync", root, "--check")[0] == EXIT_OK
    write(root / CLAUDE, after + "\nMine.\n")
    assert sc("unapply", root)[0] == EXIT_OK
    assert read(root, CLAUDE).startswith("﻿---\npaths:\n")
    assert read(root, CLAUDE).endswith("---\nMine.\n")


@git
def test_files_git_ignores_are_not_named(sc, tmp_path) -> None:
    """A local scratch file named in a committed rule file made `sync --check` fail on a
    clean checkout, which does not have it."""
    root = make_app(tmp_path)
    git_init(root)
    write(root / ".gitignore", "scratch.py\nlocal/\n")
    write(root / "scratch.py", USES)
    put(root / "local" / "try.py", USES)
    assert split_front(rules(sc, root))[0] == CLAUDE_FRONT
    (root / "scratch.py").unlink()
    shutil.rmtree(root / "local")
    assert sc("sync", root, "--check")[0] == EXIT_OK


def test_git_is_run_by_its_full_path_and_never_from_the_current_folder(
    tmp_path, monkeypatch
) -> None:
    """Run as ``git``, Windows looks for git.exe in the current folder before PATH: a git.exe
    committed to a repository ran on a plain `sync --check` there."""
    name = "git.exe" if os.name == "nt" else "git"
    here, root, real = tmp_path / "here", tmp_path / "project", tmp_path / "real"
    for folder in (here, root, root / "tools", real, here / "rel"):
        put(folder / name, "#!/bin/sh\nexit 1\n")
        (folder / name).chmod(0o755)
    (root / ".git").mkdir()
    entries = ["", ".", "rel", str(here), str(root), str(root / "tools"), str(real)]
    monkeypatch.setenv("PATH", os.pathsep.join(entries))
    monkeypatch.chdir(here)
    assert rules_module.git_command(root) == str(real / name)
    ran: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        ran.append(argv)
        return subprocess.CompletedProcess(argv, 128, b"", b"")

    monkeypatch.setattr(rules_module.subprocess, "run", run)
    assert rules_module.repo_root(root) is None
    assert [argv[0] for argv in ran] == [str(real / name)]
    # No git outside those folders: the checks that need it are skipped.
    monkeypatch.setenv("PATH", os.pathsep.join(entries[:-1]))
    ran.clear()
    assert rules_module.git_command(root) is None
    assert rules_module.ignored(root, ["main.py"]) == set() and ran == []


@git
@pytest.mark.skipif(sys.platform != "win32", reason="Windows runs a git.exe in the current folder")
def test_a_git_exe_in_the_project_does_not_run(sc, tmp_path, monkeypatch) -> None:
    """A probe git.exe (a script launcher, as pip makes them) that leaves a mark if it runs."""
    distlib = pytest.importorskip("pip._vendor.distlib")
    root = make_app(tmp_path)
    git_init(root)
    rules(sc, root)
    marker = tmp_path / "ran.txt"
    script = io.BytesIO()
    with zipfile.ZipFile(script, "w") as z:
        z.writestr("__main__.py", f"open({str(marker)!r}, 'a').write('ran')\nraise SystemExit(1)\n")
    launcher = (Path(distlib.__file__).parent / "t64.exe").read_bytes()
    shebang = f'#!"{sys.executable}"\r\n'.encode()
    (root / "git.exe").write_bytes(launcher + shebang + script.getvalue())
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    monkeypatch.chdir(root)
    subprocess.run(["git", "--version"], capture_output=True, check=False)
    if not marker.exists():
        pytest.skip("this Windows does not look in the current folder first")
    marker.unlink()
    assert sc("sync", root, "--check")[0] == EXIT_OK
    assert sc("sync", root, "--yes")[0] == EXIT_OK
    assert sc("sync", root, *SONNET, "--yes", "--target", "copilot-instructions")[0] == EXIT_OK
    assert not marker.exists()


@git
def test_copilot_instructions_below_the_repository_root_get_a_warning(sc, tmp_path) -> None:
    git_init(tmp_path / "repo")
    root = make_app(tmp_path / "repo" / "services")
    code, out = sc("sync", root, *SONNET, "--yes", "--target", "copilot-instructions")
    assert code == EXIT_OK, out
    assert (
        "GitHub reads .github/instructions at the repository's root "
        f"({(tmp_path / 'repo').resolve().as_posix()}) only" in flat(out)
    )
    # Claude Code reads nested .claude/rules folders: no warning there.
    code, out = sc("sync", root, *SONNET, "--yes", "--target", "claude-rules")
    assert "repository's root" not in out


# ------------------------------------------------------------ the user's own
def test_a_rule_file_since_cutoff_did_not_write_is_never_touched(sc, tmp_path, capsys) -> None:
    root = make_app(tmp_path)
    put(root / CLAUDE, "# My notes on toylib\n")
    put(root / ".claude" / "rules" / "style.md", "Use tabs.\n")
    code, err = errors(capsys, "sync", root, *SONNET, "--yes", "--target", "claude-rules")
    assert code == 1
    assert (
        "since-cutoff-toylib.md exists and has no since-cutoff block, so since-cutoff leaves it "
        "alone; rename or remove it, then run again" in err
    )
    assert read(root, CLAUDE) == "# My notes on toylib\n"
    assert sc("unapply", root)[0] == EXIT_OK
    assert read(root, CLAUDE) == "# My notes on toylib\n"
    assert read(root, ".claude/rules/style.md") == "Use tabs.\n"
    # A file that is not UTF-8 says so, rather than that it has no block.
    (root / CLAUDE).write_bytes(f"{BLOCK_START}\ncaf\xe9\n{BLOCK_END}\n".encode("latin-1"))
    code, err = errors(capsys, "sync", root, *SONNET, "--yes", "--target", "claude-rules")
    assert code == 1 and "since-cutoff-toylib.md is not UTF-8 text" in err


def test_a_rule_file_since_cutoff_leaves_alone_is_said_by_every_command(sc, tmp_path) -> None:
    """A rule file with the markers that is not UTF-8, or is a hard link: status said "no
    since-cutoff notes", unapply "no since-cutoff block found", and a plain sync created
    AGENTS.md beside it, all without a word of it."""
    root = make_app(tmp_path)
    latin = rules(sc, root).replace("do not pass it.", "ne le passez pas, café.")
    (root / CLAUDE).write_bytes(latin.encode("latin-1"))
    said = f"{CLAUDE} is not UTF-8 text, so since-cutoff leaves it alone"
    code, out = sc("status", root)
    assert code == EXIT_OK and f"! {said}" in flat(out)
    code, out = sc("sync", root, *SONNET, "--yes")
    assert code == EXIT_OK and f"! {said}" in flat(out)
    assert "Created AGENTS.md" in out  # as before, but not without a word
    code, out = sc("unapply", root, "--target", "claude-rules")
    assert code == EXIT_OK and f"! {said}" in flat(out)
    assert (root / CLAUDE).read_bytes() == latin.encode("latin-1")
    # status --json: on stderr, so that stdout stays JSON.
    _, out = sc("status", root, "--json")
    assert json.loads(out)["targets"][0]["file"] == "AGENTS.md"
    (root / CLAUDE).unlink()
    elsewhere = tmp_path / "elsewhere.md"
    write(elsewhere, latin.replace("café", "cafe"))
    try:
        os.link(elsewhere, root / CLAUDE)
    except OSError as exc:
        pytest.skip(f"no hard links here: {exc}")
    code, out = sc("status", root)
    assert f"! {CLAUDE} is a hard link, which may be a file of another project" in flat(out)
    # A file of the user's (no markers) is theirs: nothing to say, whatever it is.
    (root / CLAUDE).unlink()
    (root / CLAUDE).write_bytes("# My notes on toylib, café\n".encode("latin-1"))
    assert "leaves it alone" not in sc("status", root)[1]


def test_only_a_formats_own_names_and_its_packages_block_are_its(sc, tmp_path, capsys) -> None:
    """``package_of`` read ``since-cutoff-toylib.instructions.md`` in .claude/rules as the
    claude rule of a package ``toylib.instructions``, and a plain sync removed it."""
    root = make_app(tmp_path)
    copilot = rules(sc, root, "copilot-instructions")
    copy = ".claude/rules/since-cutoff-toylib.instructions.md"
    put(root / copy, copilot)
    assert FORMATS["claude-rules"].package_of(root / copy) is None
    code, out = sc("sync", root, "--yes")
    assert code == EXIT_OK, out
    assert read(root, copy) == copilot and not (root / CLAUDE).exists()
    assert status_files(sc, root) == [COPILOT]
    # A file named for one package that holds another's block is refused, not emptied.
    other = ".github/instructions/since-cutoff-otherlib.instructions.md"
    put(root / other, copilot + "\nMy otherlib notes: pin it.\n")
    code, err = errors(capsys, "sync", root, "--yes")
    assert code == 1
    assert "since-cutoff-otherlib.instructions.md holds the notes for toylib, not otherlib's" in err
    assert read(root, other) == copilot + "\nMy otherlib notes: pin it.\n"
    # unapply leaves it alone too (it removed the block, and left toylib's front matter over
    # the user's text), and says so.
    code, out = sc("unapply", root)
    assert code == EXIT_OK and f"removed the since-cutoff block from {COPILOT}" in out
    assert (
        f"! {other} holds the notes for toylib, not otherlib's, so since-cutoff leaves it alone"
        in flat(out)
    )
    assert read(root, other) == copilot + "\nMy otherlib notes: pin it.\n"


def test_a_rule_file_whose_name_differs_in_case_is_its_packages(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    rules(sc, root)
    renamed = ".claude/rules/since-cutoff-Toylib.md"
    (root / CLAUDE).rename(root / renamed)
    assert status_files(sc, root) == [renamed]
    assert sc("sync", root, "--check")[0] == EXIT_OK
    code, out = sc("unapply", root)
    assert code == EXIT_OK and renamed in out
    assert not (root / ".claude" / "rules" / "since-cutoff-Toylib.md").exists()


def test_a_rules_folder_linked_outside_the_project_is_left_alone(sc, tmp_path, capsys) -> None:
    """Claude Code documents a .claude/rules folder linked into several projects: one
    project's sync wrote its file into the shared folder, and another's status, sync and
    unapply took that file for its own (and removed it)."""
    shared = tmp_path / "shared"
    put(shared / "security.md", "Check inputs.\n")
    a = make_app(tmp_path / "a")
    put(shared / "since-cutoff-toylib.md", rules(sc, a))  # a file another project wrote
    b = make_app(tmp_path / "b", ("toylib==1.0", "otherlib==1.0"))
    link_folder(b / ".claude" / "rules", shared)
    assert status_files(sc, b) == [] and sc("status", b)[0] == EXIT_OK
    assert sc("sync", b, "--check")[0] == EXIT_OK
    assert sc("sync", b, "--yes")[0] == EXIT_OK
    # Each says that it leaves the file there alone.
    code, out = sc("unapply", b)
    assert code == EXIT_OK and flat(out).endswith("no since-cutoff block found")
    assert (
        f"! .claude/rules is a link to {shared.resolve()}, outside the project, so since-cutoff "
        "leaves since-cutoff-toylib.md in it alone" in flat(out)
    )
    assert "leaves since-cutoff-toylib.md in it alone" in flat(sc("status", b)[1])
    assert sorted(p.name for p in shared.iterdir()) == ["security.md", "since-cutoff-toylib.md"]
    # Writing there is refused, before anything is written.
    set_pins(b, ("toylib==2.0", "otherlib==1.0"))
    (shared / "since-cutoff-toylib.md").unlink()
    code, err = errors(capsys, "sync", b, *SONNET, "--yes", "--target", "claude-rules")
    assert code == 1 and ".claude/rules is a link to" in err and "outside the project" in err
    assert sorted(p.name for p in shared.iterdir()) == ["security.md"]


def test_a_hard_linked_rule_file_is_left_alone(sc, tmp_path, capsys) -> None:
    root = make_app(tmp_path)
    text = rules(sc, root)
    elsewhere = tmp_path / "elsewhere.md"
    write(elsewhere, text)
    (root / CLAUDE).unlink()
    try:
        os.link(elsewhere, root / CLAUDE)
    except OSError as exc:
        pytest.skip(f"no hard links here: {exc}")
    put(root / "lib" / "more.py", USES)
    code, err = errors(capsys, "sync", root, *SONNET, "--yes", "--target", "claude-rules")
    assert code == 1 and "is a hard link" in err
    assert elsewhere.read_bytes().decode("utf-8") == text
    # Named on its own, unapply leaves it alone too.
    code, err = errors(capsys, "unapply", root, "--target", CLAUDE)
    assert code == 1
    assert (
        f"{CLAUDE} is a hard link, which may be a file of another project: since-cutoff removes "
        "rule files only inside the project, so it leaves it alone" in err
    )
    assert (root / CLAUDE).exists() and elsewhere.read_bytes().decode("utf-8") == text


def test_unapply_of_a_rule_file_through_a_linked_folder_leaves_it(sc, tmp_path, capsys) -> None:
    shared = tmp_path / "shared"
    text = rules(sc, make_app(tmp_path / "a"))
    put(shared / "since-cutoff-toylib.md", text)
    b = make_app(tmp_path / "b")
    link_folder(b / ".claude" / "rules", shared)
    code, err = errors(capsys, "unapply", b, "--target", CLAUDE)
    assert code == 1 and f"{CLAUDE} is a link to" in err and "outside the project" in err
    assert read(shared, "since-cutoff-toylib.md") == text


def test_a_file_name_that_is_not_utf8_is_matched_and_nothing_is_emptied(sc, tmp_path) -> None:
    """A name with a lone surrogate (NTFS allows one; Linux gives one for bytes that are not
    UTF-8) reached the meta line, the write failed, and the rule file was left empty."""
    root = make_app(tmp_path)
    try:
        (root / "a\udc80b.py").write_text(USES, encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        pytest.skip(f"no such file name here: {exc}")
    text = rules(sc, root)
    assert '  - "a?b.py"\n' in split_front(text)[0]
    # A write that cannot be encoded leaves the file as it was.
    with pytest.raises(NotesFileError, match="not UTF-8"):
        _write(root / CLAUDE, text + "\udc80")
    assert read(root, CLAUDE) == text


def test_unapply_removes_the_rule_files_and_nothing_else(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    put(root / ".cursor" / "rules" / "style.mdc", "---\nalwaysApply: true\n---\nTabs.\n")
    rules(sc, root, "claude-rules")
    rules(sc, root, "cursor-rules")
    code, out = sc("unapply", root, "--target", "cursor-rules")
    assert code == EXIT_OK and f"removed the since-cutoff block from {CURSOR}" in out
    assert not (root / CURSOR).exists() and (root / CLAUDE).exists()
    write(root / "AGENTS.md", MINE)
    code, out = sc("sync", root, *SONNET, "--yes", "--target", "AGENTS.md")
    assert code == EXIT_OK
    code, out = sc("unapply", root)
    assert code == EXIT_OK
    assert f"removed the since-cutoff block from AGENTS.md, {CLAUDE}" in out
    assert not (root / CLAUDE).exists() and read(root, "AGENTS.md") == MINE
    assert read(root, ".cursor/rules/style.mdc") == "---\nalwaysApply: true\n---\nTabs.\n"
    assert sc("unapply", root)[1].strip() == "no since-cutoff block found"


def test_unapply_of_one_rule_file_removes_it_as_a_formats_unapply_does(sc, tmp_path) -> None:
    """``unapply --target <rule file>`` removed the block only, and left a front matter
    without markers that the next sync refused."""
    root = make_app(tmp_path)
    rules(sc, root)
    code, out = sc("unapply", root, "--target", CLAUDE)
    assert code == EXIT_OK and "removed the since-cutoff block from since-cutoff-toylib.md" in out
    assert not (root / CLAUDE).exists()
    rules(sc, root)  # and sync writes it again


def test_sync_says_when_agents_md_keeps_a_second_copy(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    code, _ = sc("sync", root, *SONNET, "--yes")
    assert code == EXIT_OK and (root / "AGENTS.md").exists()
    code, out = sc("sync", root, *SONNET, "--yes", "--target", "claude-rules")
    assert code == EXIT_OK
    assert (
        "AGENTS.md still has a notes block, which agents load on every request: `since-cutoff "
        "unapply --target AGENTS.md` removes it." in flat(out)
    )
    # Both are kept in step from then on.
    assert sc("sync", root, "--check")[0] == EXIT_OK


def test_run_leaves_the_rule_files_to_sync(sc, tmp_path, capsys) -> None:
    root = make_app(tmp_path)
    code, err = errors(capsys, "run", root, "--apply", "--target", "cursor-rules")
    assert code == 2
    assert "run `since-cutoff sync --target cursor-rules`" in err
    assert not (root / ".cursor").exists()
    rules(sc, root)
    # Without --target, run --apply would write AGENTS.md beside the rule files; into a rule
    # file's path, a block that sync would then take for a hand edit. Both stop before any
    # model call.
    code, err = errors(capsys, "run", root, "--apply")
    assert code == 2 and "the notes are in rule files (.claude/rules)" in err
    code, err = errors(capsys, "run", root, "--apply", "--target", CLAUDE)
    assert code == 2 and f"{CLAUDE} is a rule file" in err
    assert not (root / "AGENTS.md").exists()
