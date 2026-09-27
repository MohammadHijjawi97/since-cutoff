"""Fix notes: short, verified bullets written into AGENTS.md / CLAUDE.md.

A note is only kept when its example type-checks cleanly against the exact version the project
uses, and every identifier the bullet recommends also appears in that verified example. Notes
that cannot be verified fall back to a plain, deterministic statement of the change.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from since_cutoff.apidiff import (
    DEPRECATED,
    KIND_CHANGED,
    MOVED,
    PARAM_KEYWORD_ONLY,
    PARAM_POSITIONAL_ONLY,
    PARAM_REMOVED,
    PARAM_REQUIRED,
    REMOVED,
    APIChange,
)
from since_cutoff.errors import SinceCutoffError

BLOCK_START = "<!-- since-cutoff:start -->"
BLOCK_END = "<!-- since-cutoff:end -->"
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BACKTICK = re.compile(r"`([^`]+)`")
_COMMON = {
    "import",
    "from",
    "as",
    "None",
    "True",
    "False",
    "self",
    "cls",
    "await",
    "async",
    "def",
    "return",
    "with",
    "for",
    "in",
    "if",
    "else",
    "and",
    "or",
    "not",
    "is",
    "lambda",
    "str",
    "int",
    "float",
    "bool",
    "list",
    "dict",
    "tuple",
    "print",
    "kwargs",
    "args",
}


@dataclass
class Note:
    change: APIChange
    bullet: str
    example: str | None
    verified: bool
    source: str  # "model" or "template"

    def to_dict(self) -> dict[str, object]:
        return {
            "change_id": self.change.id,
            "package": self.change.package,
            "bullet": self.bullet,
            "example": self.example,
            "verified": self.verified,
            "source": self.source,
        }


def template_bullet(change: APIChange) -> str:
    """A factual bullet derived only from the API diff (never wrong, sometimes less helpful)."""
    pkg = f"{change.package} {change.to_version}"
    hint = f" ({_safe(change.hint).rstrip('.')})" if change.hint else ""
    if change.kind == MOVED and change.moved_to:
        module, _, name = change.moved_to.rpartition(".")
        return f"`{change.path}` moved: use `from {module} import {name}` ({pkg})."
    if change.kind == REMOVED:
        other = (
            f" The `{change.name}` at `{change.namesake}` is a different object."
            if change.namesake
            else ""
        )
        return f"`{change.display}` no longer exists in {pkg}{hint}. Do not use it.{other}"
    if change.kind == PARAM_REMOVED:
        return (
            f"`{change.display}`: `{change.parameter}` was removed in {pkg}{hint}. Do not pass it."
        )
    if change.kind == PARAM_REQUIRED:
        return f"`{change.display}` now requires `{change.parameter}` in {pkg}."
    if change.kind == PARAM_KEYWORD_ONLY:
        return f"`{change.owner + '.' if change.owner else ''}{change.name}`: pass `{change.parameter}` by keyword in {pkg}."
    if change.kind == PARAM_POSITIONAL_ONLY:
        return f"`{change.owner + '.' if change.owner else ''}{change.name}`: pass `{change.parameter}` positionally in {pkg}."
    if change.kind == DEPRECATED:
        text = _safe(change.deprecation or "").rstrip(".")
        msg = (
            f": {text}" if text and text.lower() not in ("deprecated", "deprecated method") else ""
        )
        if change.parameter:
            return (
                f"`{change.display}`: `{change.parameter}` is deprecated in {pkg}{msg}. "
                "Avoid it in new code."
            )
        return f"`{change.path}` is deprecated in {pkg}{msg}. Avoid it in new code."
    if change.kind == KIND_CHANGED:
        kinds = (
            f"changed from {change.old_kind} to {change.new_kind}"
            if change.old_kind and change.new_kind
            else "changed kind"
        )
        return f"`{change.path}` {kinds} in {pkg}; check its new signature before use."
    return change.describe()


def clean_bullet(text: str) -> str:
    text = " ".join(text.strip().split())
    return text[2:].strip() if text.startswith(("- ", "* ")) else text


def bullet_is_grounded(bullet: str, example: str, change: APIChange) -> bool:
    """Every identifier the bullet puts in backticks must be backed by the example or the diff."""
    allowed = set(_IDENT.findall(example))
    for text in (
        change.path,
        change.display,
        change.parameter or "",
        change.moved_to or "",
        change.old_signature or "",
        change.new_signature or "",
        change.package,
    ):
        allowed.update(_IDENT.findall(text))
    allowed.update(_IDENT.findall(change.package.replace("-", "_")))
    allowed |= _COMMON
    for span in _BACKTICK.findall(bullet):
        # Ignore string literals inside the span: model names, file names, etc.
        code = re.sub(r"([\"']).*?\1", "''", span)
        if not api_identifiers(code) <= allowed:
            return False
    return True


def api_identifiers(code: str) -> set[str]:
    """Identifiers in a code span that name library API: attributes, keywords, calls, classes.

    Plain lower-case variable names (``client`` in ``client.shutdown()``) are placeholders the
    bullet may choose freely, so they are not required to appear in the example.
    """
    found = set(re.findall(r"\.\s*([A-Za-z_]\w*)", code))
    found |= set(re.findall(r"([A-Za-z_]\w*)\s*=(?!=)", code))
    found |= set(re.findall(r"(?<![\w.])([A-Za-z_]\w*)\s*\(", code))
    found |= set(re.findall(r"(?<![\w.])([A-Z]\w*)", code))
    found |= set(re.findall(r"(?:from|import)\s+([\w.]+)", code))
    expanded: set[str] = set()
    for f in found:
        expanded.update(_IDENT.findall(f))
    return expanded


def _safe(text: str, limit: int = 160) -> str:
    """Package-provided text (docstrings, deprecation messages) is untrusted: keep it short,
    single-line, and unable to close the block or smuggle code spans."""
    text = " ".join(text.split())
    text = text.replace("<!--", "").replace("-->", "").replace("`", "'")
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def render_block(notes: Iterable[Note], *, model: str, cutoff: date, version_source: str) -> str:
    by_pkg: dict[tuple[str, str], list[Note]] = {}
    for n in notes:
        by_pkg.setdefault((n.change.package, n.change.to_version), []).append(n)
    lines = [
        BLOCK_START,
        "## Library changes after the model's training cutoff",
        "",
        f"Generated by [since-cutoff](https://github.com/MohammadHijjawi97/since-cutoff) for "
        f"`{model}` (training cutoff {cutoff.isoformat()}), checked against `{version_source}`. "
        "Prefer these over what you remember.",
    ]
    for (pkg, version), items in sorted(by_pkg.items()):
        lines += ["", f"**{pkg} {version}**"]
        seen: set[str] = set()
        for n in items:
            bullet = n.bullet.replace("<!--", "").replace("-->", "")
            bullet = " ".join(bullet.split())
            if not bullet or bullet in seen:
                continue
            seen.add(bullet)
            lines.append(f"- {bullet}")
    lines.append(BLOCK_END)
    return "\n".join(lines) + "\n"


class NotesFileError(SinceCutoffError):
    """AGENTS.md/CLAUDE.md has markers since-cutoff cannot safely edit."""


def _read(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _block_span(text: str, path: Path) -> tuple[int, int] | None:
    """Character span of the single well-formed block (markers on their own lines), if any."""
    starts = [
        m.start() for m in re.finditer(rf"(?m)^[ \t]*{re.escape(BLOCK_START)}[ \t]*\r?$", text)
    ]
    ends = [m.end() for m in re.finditer(rf"(?m)^[ \t]*{re.escape(BLOCK_END)}[ \t]*\r?$", text)]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        raise NotesFileError(
            f"{path.name} has unbalanced or repeated since-cutoff markers; fix or remove them by "
            "hand, then run again"
        )
    end = ends[0]
    if text[end : end + 2] == "\r\n":
        end += 2
    elif text[end : end + 1] == "\n":
        end += 1
    return starts[0], end


def apply_block(path: Path, block: str) -> str:
    """Insert or replace the since-cutoff block in ``path``. Returns what was done.

    Only a block whose markers sit on their own lines is replaced; anything else in the file is
    left byte-for-byte unchanged (including its line endings).
    """
    if not path.exists():
        _write(path, block)
        return "created"
    text = _read(path)
    nl = "\r\n" if "\r\n" in text else "\n"
    body = block.replace("\r\n", "\n").replace("\n", nl)
    span = _block_span(text, path)
    if span is not None:
        new, action = text[: span[0]] + body + text[span[1] :], "updated"
    else:
        if not text or text.endswith(nl + nl):
            sep = ""
        elif text.endswith(nl):
            sep = nl
        else:
            sep = nl + nl
        new, action = text + sep + body, "appended to"
    if new != text:
        _write(path, new)
    return action


def remove_block(path: Path) -> bool:
    """Remove the since-cutoff block. Returns False (and leaves the file alone) if there is none."""
    if not path.exists():
        return False
    text = _read(path)
    span = _block_span(text, path)
    if span is None:
        return False
    nl = "\r\n" if "\r\n" in text else "\n"
    before, after = text[: span[0]], text[span[1] :]
    new = before.rstrip() + (nl + nl + after.lstrip() if after.strip() else "")
    new = new.rstrip() + nl if new.strip() else ""
    _write(path, new)
    return True
