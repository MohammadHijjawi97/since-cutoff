"""The READMEs and docs against the code (plan C.1, C.4, C.6).

- every `since-cutoff <command> --flag` they show is a command and a flag the CLI has;
- the scan output and the note on the README's first screen are what the code writes;
- "What 'verified' means" names every evidence tag as notes.py writes it;
- no English page says "verified" on its own;
- every link to a heading of a README or docs page finds that heading;
- docs/reference.md has a row for every environment variable the code reads, and none for one
  it does not.

Only README.md ships in the sdist, so a page that is not there is skipped.
"""

from __future__ import annotations

import argparse
import ast
import re
from collections.abc import Iterable
from pathlib import Path

import pytest

from since_cutoff.apidiff import PARAM_REMOVED, APIChange
from since_cutoff.cli import build_parser
from since_cutoff.hosts import ENV_VARS
from since_cutoff.notes import (
    TAG_DIFF,
    TAG_LIBRARY,
    TAG_METADATA,
    TAG_MOVE_CHECKED,
    TAG_NOT_CONFIRMED,
    TAG_PROBABLE_RENAME,
    TAG_TYPE_CHECKED,
    diff_note,
    tag_text,
)
from since_cutoff.providers import OPENAI_COMPATIBLE
from since_cutoff.report import NOTES_READY

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "since_cutoff"
REPO_URL = "https://github.com/MohammadHijjawi97/since-cutoff"
READMES = ("README.md", "README.zh-CN.md", "README.es.md", "README.fr.md")
PAGES = (
    *READMES,
    "docs/how-it-works.md",
    "docs/reference.md",
    "docs/index.md",
    "docs/es/index.md",
    "docs/fr/index.md",
    "docs/ai-stack.md",
    "docs/troubleshooting.md",
    "PRIVACY.md",
    "CONTRIBUTING.md",
    "skills/since-cutoff/SKILL.md",
)
ENGLISH = (
    "README.md",
    "docs/reference.md",
    "docs/how-it-works.md",
    "docs/index.md",
    "docs/ai-stack.md",
    "docs/troubleshooting.md",
    "PRIVACY.md",
    "CONTRIBUTING.md",
    "skills/since-cutoff/SKILL.md",
)


def _text(name: str) -> str:
    path = ROOT / name
    if not path.exists():
        pytest.skip(f"{name} is not part of this checkout")
    return path.read_text(encoding="utf-8")


# Rows of docs/reference.md for variables since-cutoff does not read by name: Python's urllib
# and ssl read the proxy and certificate settings, and run removes CLAUDE_CODE_ENTRYPOINT from
# the environment of its `claude` subprocess.
NOT_READ_BY_NAME = {
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "CLAUDE_CODE_ENTRYPOINT",
}
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")


def _env_reads(sources: Iterable[str]) -> set[str]:
    """The environment variables the code reads by name: ``os.environ.get("X")``,
    ``os.getenv("X")``, ``os.environ["X"]``, ``"X" in os.environ``, the same on a mapping given
    for the environment (``env.get("X")``, ``(env or {}).get("X")``), hosts' ``places.var("X")``,
    and each of them with a module constant for the name (``os.environ.get(CUTOFF_MARGIN_VAR)``).
    """
    trees = [ast.parse(source) for source in sources]
    constants = {
        target.id: node.value.value
        for tree in trees
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }

    def name(node: ast.expr) -> str | None:
        value = node.value if isinstance(node, ast.Constant) else None
        if isinstance(node, ast.Name):
            value = constants.get(node.id)
        return value if isinstance(value, str) and _ENV_NAME.fullmatch(value) else None

    def environment(node: ast.expr) -> bool:
        if isinstance(node, ast.IfExp):
            return environment(node.body) or environment(node.orelse)
        if isinstance(node, ast.BoolOp):
            return any(environment(value) for value in node.values)
        last = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")
        return last in ("environ", "env")

    found = set()
    for tree in trees:
        for node in ast.walk(tree):
            key: ast.expr | None = None
            if isinstance(node, ast.Call) and node.args:
                func = node.func
                called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                on_env = isinstance(func, ast.Attribute) and environment(func.value)
                if called in ("getenv", "var") or (on_env and called in ("get", "pop")):
                    key = node.args[0]
            elif isinstance(node, ast.Subscript) and environment(node.value):
                key = node.slice
            elif (
                isinstance(node, ast.Compare)
                and isinstance(node.ops[0], (ast.In, ast.NotIn))
                and environment(node.comparators[0])
            ):
                key = node.left
            read = name(key) if key is not None else None
            if read:
                found.add(read)
    return found


def _env_table(text: str) -> set[str]:
    """The variables in the Variable column of the page's environment variables table."""
    section = text.split("## Environment variables", 1)[1].split("\n## ", 1)[0]
    rows = [line.split("|") for line in section.splitlines() if line.startswith("| ")]
    return {name for row in rows[1:] for name in re.findall(r"`([A-Z][A-Z0-9_]+)`", row[2])}


def test_reference_lists_environment_variables_read_by_the_code() -> None:
    documented = _env_table(_text("docs/reference.md"))
    sources = [p.read_text(encoding="utf-8") for p in sorted(SRC.rglob("*.py"))]
    names = _env_reads(sources) | set(ENV_VARS)
    # Provider keys are read through a variable (os.environ.get(key_env)): add them.
    names.update(key for _, key in OPENAI_COMPATIBLE.values() if key)
    assert not names - documented, f"no row for {sorted(names - documented)}"
    stale = documented - names - NOT_READ_BY_NAME
    assert not stale, f"rows for variables the code does not read: {sorted(stale)}"


def test_the_environment_check_finds_each_way_of_reading_a_variable() -> None:
    source = """
import os
MARGIN_VAR = "SINCE_CUTOFF_CONSTANT"
TYPED_VAR: str = "SINCE_CUTOFF_TYPED"
a = os.environ.get("SINCE_CUTOFF_GET")
b = os.getenv("SINCE_CUTOFF_GETENV", "")
c = os.environ["SINCE_CUTOFF_ITEM"]
d = "SINCE_CUTOFF_IN" in os.environ
e = os.environ.get(MARGIN_VAR), os.getenv(TYPED_VAR)
f = (os.environ if env is None else env).get("SINCE_CUTOFF_MAPPING")
g = (env or {}).get("SINCE_CUTOFF_OR"), self.env.get("SINCE_CUTOFF_ATTRIBUTE")
h = places.var("SINCE_CUTOFF_VAR")
i = settings.get("NOT_THE_ENVIRONMENT"), os.environ.get("lowercase"), os.environ.get(name)
"""
    assert _env_reads([source]) == {
        "SINCE_CUTOFF_CONSTANT",
        "SINCE_CUTOFF_TYPED",
        "SINCE_CUTOFF_GET",
        "SINCE_CUTOFF_GETENV",
        "SINCE_CUTOFF_ITEM",
        "SINCE_CUTOFF_IN",
        "SINCE_CUTOFF_MAPPING",
        "SINCE_CUTOFF_OR",
        "SINCE_CUTOFF_ATTRIBUTE",
        "SINCE_CUTOFF_VAR",
    }
    table = "## Environment variables\n\n| G | V | R | E |\n|---|---|---|---|\n"
    assert _env_table(table + "| a | `ONE`, `TWO` | `THREE` | `FOUR` |\n\n## Files") == {
        "ONE",
        "TWO",
    }


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
            request_extras=["extra_body", "extra_query"],  # what 1.8.0's create() takes
        )
        for p in ("top_p", "temperature", "top_k")
    ]
    return diff_note(changes).line


def _unwrapped(text: str) -> str:
    return re.sub(r"\s+", " ", text)


@pytest.mark.parametrize("name", READMES)
def test_the_first_screen_quotes_what_the_scan_writes(name: str) -> None:
    text = _text(name)
    # The first screen: what it is, then the first two sections, Install and Quick start.
    sections = text.split("\n## ", 3)
    first = "\n## ".join(sections[:3])
    [console] = re.findall(r"```console\n(.*?)```", first, re.S)
    # Install comes first: its commands are in the first section, within the first 50
    # lines and before the scan output, which is in the second (0.5.0 opened with the
    # scan output and showed the install commands at line 137, after The problem).
    assert "pipx install since-cutoff" in sections[1]
    assert "pip install since-cutoff" in sections[1]
    install = text.index("pipx install since-cutoff")
    assert text.count("\n", 0, install) < 50
    assert install < text.index("```console")
    assert sections[2].count("```console") == 1
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
    assert f"**anthropic 1.8.0** (compared from 0.56.0)\n- {note}\n" in text
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
            (TAG_DIFF, TAG_METADATA),
            (TAG_DIFF, TAG_PROBABLE_RENAME),
            (TAG_TYPE_CHECKED,),
            (TAG_DIFF, TAG_NOT_CONFIRMED),
        )
    ]
    assert tags[3] == "[diff + metadata]" and tags[-1] == "[diff; not confirmed]"
    rows = re.findall(r"^\| `(\[[^`]+\])` \|", text, re.M)
    assert rows == ["[diff]", *tags[1:6], "[not confirmed]"], name
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


_UNQUOTED_EXTRA = re.compile(r'(?<!")since-cutoff\[run\]|since-cutoff\[run\](?!")')


@pytest.mark.parametrize("name", [*PAGES, "CHANGELOG.md"])
def test_the_run_extra_is_always_quoted(name: str) -> None:
    """Unquoted, zsh takes ``since-cutoff[run]`` for a glob and the install fails."""
    lines = [line for line in _text(name).splitlines() if _UNQUOTED_EXTRA.search(line)]
    assert lines == []


@pytest.mark.parametrize("name", READMES)
def test_every_readme_says_how_to_install_run_and_what_the_cache_commands_are(name: str) -> None:
    text = _text(name)
    for needed in (
        'pip install "since-cutoff[run]"',
        "uvx --with basedpyright since-cutoff run",
        "since-cutoff cache info",
        "`--sources`",
        "SINCE_CUTOFF_CACHE_MAX_MB",
    ):
        assert needed in text, needed


@pytest.mark.parametrize(
    "fragment",
    (
        "No dependency could be checked",
        "MB download limit (--max-download-mb)",
        "could not find importable modules in",
        "Not written: there is no terminal to ask in",
        "The since-cutoff block in",
        "was edited by hand; sync would replace it",
        "unknown model '",
        "Pass its training cutoff explicitly",
        "models.dev has no knowledge cutoff for '",
        "the `claude` CLI was not found on PATH",
        "ANTHROPIC_API_KEY is not set",
        "run checks the model's answers with the basedpyright type checker, which is not",
        'installed: pip install "since-cutoff[run]", or uvx --with basedpyright since-cutoff run',
        "CLAUDE.md does not import AGENTS.md, so Claude Code does not read the notes in AGENTS.md",
        "CLAUDE.md does not import AGENTS.md, so the notes go to both files",
        "CLAUDE.md imports AGENTS.md, so its since-cutoff block is a second copy of the notes",
    ),
)
def test_troubleshooting_quotes_current_source_text(fragment: str) -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "src/since_cutoff").rglob("*.py"))
    )
    assert fragment in source
    assert fragment in _text("docs/troubleshooting.md")
