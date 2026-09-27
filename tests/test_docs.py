"""The READMEs and docs against the code (plan C.1, C.4, C.6).

- every `since-cutoff <command> --flag` they show is a command and a flag the CLI has;
- the scan output and the note on the README's first screen are what the code writes;
- "What 'verified' means" names every evidence tag as notes.py writes it;
- no English page says "verified" on its own;
- every link to a heading of a README or docs page finds that heading.

Only README.md ships in the sdist, so a page that is not there is skipped.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from since_cutoff.apidiff import PARAM_REMOVED, APIChange
from since_cutoff.cli import build_parser
from since_cutoff.notes import (
    TAG_DIFF,
    TAG_LIBRARY,
    TAG_MOVE_CHECKED,
    TAG_NOT_CONFIRMED,
    TAG_PROBABLE_RENAME,
    TAG_TYPE_CHECKED,
    diff_note,
    tag_text,
)
from since_cutoff.report import NOTES_READY

ROOT = Path(__file__).resolve().parents[1]
REPO_URL = "https://github.com/MohammadHijjawi97/since-cutoff"
READMES = ("README.md", "README.zh-CN.md", "README.es.md", "README.fr.md")
PAGES = (
    *READMES,
    "docs/how-it-works.md",
    "docs/index.md",
    "docs/es/index.md",
    "docs/fr/index.md",
    "docs/ai-stack.md",
    "PRIVACY.md",
    "CONTRIBUTING.md",
    "skills/since-cutoff/SKILL.md",
)
ENGLISH = (
    "README.md",
    "docs/how-it-works.md",
    "docs/index.md",
    "docs/ai-stack.md",
    "PRIVACY.md",
    "CONTRIBUTING.md",
    "skills/since-cutoff/SKILL.md",
)


def _text(name: str) -> str:
    path = ROOT / name
    if not path.exists():
        pytest.skip(f"{name} is not part of this checkout")
    return path.read_text(encoding="utf-8")


def _options() -> dict[str, set[str]]:
    parser = build_parser()
    [sub] = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    return {name: set(p._option_string_actions) for name, p in sub.choices.items()}


_CODE = re.compile(r"```[^\n]*\n(.*?)```|`([^`\n]+)`", re.S)
_SUBCOMMANDS = "run|scan|sync|status|unapply|models|cache|mcp"
# ``since-cutoff sync --check`` anywhere in code; in an inline code span, also ``scan -v``
# (not the `mcp` of `claude mcp add --scope user ...`).
_COMMAND = re.compile(rf"since-cutoff(?:@\S+|==\S+)?\s+({_SUBCOMMANDS})\b([^\n#|;&`]*)")
_SHORT = re.compile(rf"^({_SUBCOMMANDS})\b([^\n#|;&`]*)")


def _commands(text: str) -> list[tuple[str, list[str]]]:
    """``(command, flags)`` for every use of a subcommand in the page's code, as in
    ``since-cutoff sync --check`` or `` `scan -v` ``."""
    found = []
    for block, span in _CODE.findall(text):
        matches = list(_COMMAND.finditer(block or span))
        if span and not matches:
            matches = list(_SHORT.finditer(span))
        for m in matches:
            flags = [t.split("=", 1)[0] for t in m.group(2).split() if t.startswith("-")]
            found.append((m.group(1), flags))
    return found


@pytest.mark.parametrize("name", PAGES)
def test_every_command_and_flag_the_docs_show_exists(name: str) -> None:
    options = _options()
    commands = _commands(_text(name))
    assert commands, name
    for command, flags in commands:
        unknown = [f for f in flags if f not in options[command]]
        assert not unknown, (name, command, unknown)


def test_the_flag_check_catches_a_flag_the_cli_does_not_have() -> None:
    options = _options()
    [(command, flags)] = _commands("Run `since-cutoff sync --no-such-flag` in CI.")
    assert command == "sync" and flags == ["--no-such-flag"]
    assert "--no-such-flag" not in options["sync"] and "--check" in options["sync"]


def _anthropic_note() -> str:
    """The note the sample project gets for `Messages.create` (anthropic 0.60.0 -> 1.8.0)."""
    changes = [
        APIChange(
            "anthropic",
            "0.60.0",
            "1.8.0",
            PARAM_REMOVED,
            "anthropic.resources.messages.messages.Messages.create",
            "create",
            owner="Messages",
            parameter=p,
        )
        for p in ("top_p", "temperature", "top_k")
    ]
    return diff_note(changes).line


def _unwrapped(text: str) -> str:
    return re.sub(r"\s+", " ", text)


@pytest.mark.parametrize("name", READMES)
def test_the_first_screen_quotes_what_the_scan_writes(name: str) -> None:
    text = _text(name)
    first = text.split("\n## ", 1)[0]
    [console] = re.findall(r"```console\n(.*?)```", first, re.S)
    lines = console.splitlines()
    assert lines[0] == "$ uvx since-cutoff scan --model anthropic:claude-sonnet-4-5"
    assert lines[1] == (
        "Your code uses 2 APIs that changed after claude-sonnet-4-5's training cutoff (2025-07-31)"
    )
    note = _anthropic_note()
    assert note.endswith(" [diff]")
    shown = _unwrapped(console.split("    Note: ", 1)[1].split("\n\n", 1)[0])
    assert shown == note
    ready = NOTES_READY.format(
        count="2 notes", them="them", target="AGENTS.md", versions="pyproject.toml"
    )
    assert _unwrapped(console.rstrip().split("\n\n")[-1]) == ready
    # The block `sync` writes has the same bullet, under the package's line.
    assert f"**anthropic 1.8.0** (0.60.0 at the cutoff)\n- {note}\n" in text
    # One command that needs no API key.
    assert "```bash\nuvx since-cutoff scan\n```" in first


@pytest.mark.parametrize("name", [*READMES, "docs/how-it-works.md"])
def test_what_verified_means_names_every_tag(name: str) -> None:
    text = _text(name)
    tags = [
        tag_text(t)
        for t in (
            (TAG_DIFF,),
            (TAG_DIFF, TAG_LIBRARY),
            (TAG_DIFF, TAG_MOVE_CHECKED),
            (TAG_DIFF, TAG_PROBABLE_RENAME),
            (TAG_TYPE_CHECKED,),
            (TAG_DIFF, TAG_NOT_CONFIRMED),
        )
    ]
    assert tags[-1] == "[diff; not confirmed]"
    rows = re.findall(r"^\| `(\[[^`]+\])` \|", text, re.M)
    assert rows == ["[diff]", *tags[1:5], "[not confirmed]"], name
    for tag in tags:
        assert f"`{tag}`" in text, (name, tag)


_VERIFIED = re.compile(r"\bverif(?:y|ied|ies|ying|ication)\b", re.I)


@pytest.mark.parametrize("name", ENGLISH)
def test_no_english_page_says_verified_on_its_own(name: str) -> None:
    """Plan C.4/C.6: the word is quoted where it is explained, and `verified` is a JSON key."""
    text = re.sub(r'"verified"|`verified`|\(#[^)]*\)', "", _text(name))
    assert not _VERIFIED.findall(text), name


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces as hyphens."""
    text = re.sub(r"[`*]", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(text: str) -> set[str]:
    outside = re.sub(r"```.*?```", "", text, flags=re.S)
    return {_slug(h) for h in re.findall(r"^#{1,6} (.+)$", outside, re.M)}


@pytest.mark.parametrize("name", PAGES)
def test_every_link_to_a_heading_finds_it(name: str) -> None:
    text = _text(name)
    links = [(name, a) for a in re.findall(r"\]\(#([^)]+)\)", text)]
    links += [("README.md", a) for a in re.findall(re.escape(REPO_URL) + r"#([^)\s]+)\)", text)]
    links += re.findall(
        re.escape(REPO_URL) + r"/blob/main/((?:README[\w.-]*|docs/[\w./-]+)\.md)#([^)\s]+)\)", text
    )
    for page, anchor in links:
        target = ROOT / page
        if not target.exists():
            continue  # an sdist
        assert anchor in _anchors(target.read_text(encoding="utf-8")), (name, page, anchor)


def test_the_slug_is_githubs() -> None:
    assert _slug("Use it from any agent (MCP)") == "use-it-from-any-agent-mcp"
    assert _slug('What "verified" means') == "what-verified-means"
    assert _slug("在任意 Agent 中使用（MCP）") == "在任意-agent-中使用mcp"
    assert _slug("Utilisation depuis n'importe quel agent (MCP)") == (
        "utilisation-depuis-nimporte-quel-agent-mcp"
    )
    assert _slug("Mantener las notas al día: sync y status") == (
        "mantener-las-notas-al-día-sync-y-status"
    )
