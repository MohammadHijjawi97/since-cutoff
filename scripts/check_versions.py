"""Check that every file naming the release version agrees with it.

    python scripts/check_versions.py          # every field agrees with src/since_cutoff/__init__.py
    python scripts/check_versions.py 0.2.0    # every field is 0.2.0 (release.yml passes the tag)

Exits with code 1 and lists each field that differs. `tests/test_ci.py` runs the same check.
Only the standard library is used, so it runs before anything is installed.

The Claude Code plugin's SessionStart hook (`hooks/hooks.json`) runs `since-cutoff status`,
which 0.4.0 added. The plugin installs from the repository, so the hook must not be there before
a release that has `status` is on PyPI (every session start would fail): until then it waits in
`hooks/hooks.json.in`, and a release from 0.4.0 on must have it, pinned to that release.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "since-cutoff"
_VERSION = r"(\d+\.\d+\.\d+\S*)"
# The first release with `since-cutoff status --hook`, which the plugin's SessionStart hook runs.
STATUS_SINCE = (0, 4, 0)
HOOKS = "hooks/hooks.json"


def _text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _json(name: str) -> Any:
    return json.loads(_text(name))


def _match(name: str, pattern: str) -> str | None:
    found = re.search(pattern, _text(name), re.MULTILINE)
    return found.group(1) if found else None


def _pin(name: str) -> str | None:
    """The version in an MCP launcher config: `uvx since-cutoff==X mcp`."""
    first = _json(name)["mcpServers"][PACKAGE]["args"][0]
    return first.split("==", 1)[1] if first.startswith(f"{PACKAGE}==") else None


def _hook_pin(name: str = HOOKS) -> str | None:
    """The version the Claude Code plugin's SessionStart hook runs: `uvx since-cutoff==X status`."""
    [entry] = _json(name)["hooks"]["SessionStart"]
    [hook] = entry["hooks"]
    found = re.fullmatch(rf"uvx {PACKAGE}=={_VERSION} status --hook", hook["command"])
    return found.group(1) if found else None


def has_status(version: str) -> bool:
    """Whether a release has `since-cutoff status` (0.4.0 and its pre-releases on)."""
    numbers = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    return numbers is not None and tuple(map(int, numbers.groups())) >= STATUS_SINCE


def hook_problems(expected: str) -> list[str]:
    """What is wrong with the plugin's SessionStart hook for a release of ``expected``: before
    0.4.0 there must be none (the release on PyPI has no `status` command to run); from 0.4.0
    on it must be there, pinned to the release."""
    active = (ROOT / HOOKS).exists()
    if not has_status(expected):
        if not active:
            return []
        return [
            f"{HOOKS}: its SessionStart hook runs `since-cutoff status`, which since-cutoff "
            f"{expected} does not have (0.4.0 added it), so every session start would fail: "
            f"keep it as {HOOKS}.in until a release that has `status` is on PyPI"
        ]
    try:
        pin = _hook_pin() if active else None
    except (OSError, LookupError, ValueError):
        pin = None
    if pin == expected:
        return []
    staged = f"; {HOOKS}.in has it" if (ROOT / f"{HOOKS}.in").exists() and not active else ""
    return [f"{HOOKS} SessionStart pin: {pin or 'missing'} (expected {expected}{staged})"]


def _marketplace_entry() -> Any:
    [entry] = [
        p for p in _json(".claude-plugin/marketplace.json")["plugins"] if p["name"] == PACKAGE
    ]
    return entry.get("version")


FIELDS: dict[str, Callable[[], Any]] = {
    "src/since_cutoff/__init__.py __version__": lambda: _match(
        "src/since_cutoff/__init__.py", rf'^__version__ = "{_VERSION}"$'
    ),
    "server.json version": lambda: _json("server.json")["version"],
    "server.json packages[0].version": lambda: _json("server.json")["packages"][0]["version"],
    ".claude-plugin/plugin.json version": lambda: _json(".claude-plugin/plugin.json")["version"],
    ".claude-plugin/marketplace.json plugin version": _marketplace_entry,
    "plugin.json version": lambda: _json("plugin.json")["version"],
    ".codex-plugin/plugin.json version": lambda: _json(".codex-plugin/plugin.json")["version"],
    "gemini-extension.json version": lambda: _json("gemini-extension.json")["version"],
    ".mcp.json pin": lambda: _pin(".mcp.json"),
    "mcp.json pin": lambda: _pin("mcp.json"),
    "CITATION.cff version": lambda: _match("CITATION.cff", rf"^version: {_VERSION}$"),
    "action.yml since-cutoff-version default": lambda: _match(
        "action.yml", rf'^  since-cutoff-version:\n(?:    .*\n)*?    default: "{_VERSION}"$'
    ),
    "CHANGELOG.md newest release": lambda: _match("CHANGELOG.md", rf"^## {_VERSION} - "),
    "README.md pre-commit rev": lambda: _match("README.md", rf"^    rev: v{_VERSION}$"),
    "README.md action input table": lambda: _match(
        "README.md", rf"^\| `since-cutoff-version` \| `{_VERSION}` \|"
    ),
    "README.zh-CN.md pre-commit rev": lambda: _match("README.zh-CN.md", rf"^    rev: v{_VERSION}$"),
}


def versions() -> dict[str, str | None]:
    """Every field's version; None when the file or the field is missing."""
    found: dict[str, str | None] = {}
    for label, read in FIELDS.items():
        try:
            found[label] = read()
        except (OSError, LookupError, ValueError):
            found[label] = None
    return found


def problems(expected: str | None = None) -> list[str]:
    found = versions()
    expected = expected or found["src/since_cutoff/__init__.py __version__"]
    out = [
        f"{label}: {value or 'missing'} (expected {expected})"
        for label, value in found.items()
        if value != expected
    ]
    out += hook_problems(str(expected))
    # The citation's release date is the date of that release in the changelog.
    released = _match(
        "CHANGELOG.md", rf"^## {re.escape(str(expected))} - (\d{{4}}-\d{{2}}-\d{{2}})"
    )
    cited = _match("CITATION.cff", r'^date-released: "?(\d{4}-\d{2}-\d{2})"?$')
    if released != cited:
        out.append(f"CITATION.cff date-released: {cited} (CHANGELOG.md says {released})")
    return out


def main(argv: list[str]) -> int:
    expected = argv[1].removeprefix("v") if len(argv) > 1 else None
    found = problems(expected)
    for line in found:
        print(line)
    if not found:
        print(f"All {len(FIELDS)} version fields agree, and so does the plugin's hook.")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
