"""Check that every file naming the release version agrees with it.

    python scripts/check_versions.py          # every field agrees with src/since_cutoff/__init__.py
    python scripts/check_versions.py 0.2.0    # every field is 0.2.0 (release.yml passes the tag)

Exits with code 1 and lists each field that differs. `tests/test_ci.py` runs the same check.
Only the standard library is used, so it runs before anything is installed.
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
        print(f"All {len(FIELDS)} version fields agree.")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
