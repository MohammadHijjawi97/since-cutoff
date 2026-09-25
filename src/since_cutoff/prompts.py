"""Prompt templates. They are versioned: changing a template changes cache keys."""

from __future__ import annotations

import json
import re
from typing import Any

from since_cutoff.apidiff import APIChange

PROMPT_VERSION = 2

# --------------------------------------------------------------- task writer
TASK_SYSTEM = (
    "You write evaluation tasks for AI coding assistants. You are precise, concrete and brief. "
    "Reply with a single JSON object and nothing else."
)

TASK_TEMPLATE = """A Python library changed its public API. Write {n} short, realistic coding tasks that a developer could give to a coding assistant, where a correct solution for {package}=={new_version} has to deal with this change.

Library: {package} (import as: {imports})
Change: {change}
Old API in {package} {old_version}:
  {old_signature}
  {old_doc}
New API in {package} {new_version}:
  {new_signature}
  {new_doc}{extra}

Rules:
- Each task is 1-3 sentences and asks for a small, self-contained Python function or script that uses {package}.
- A developer who only knows {package} {old_version} would naturally reach for the old API to solve it.
- Describe the goal in plain words, the way a developer talks. Never write these identifiers: {forbidden}. Never mention versions, deprecations, migrations or that anything changed.
- Use concrete values (names, numbers, file names, model names).
- Each task must be solvable with {package}=={new_version} and the standard library only.
- Make the {n} tasks clearly different from each other.
If no sensible task exists (for example the change only affects internals or constants nobody uses), return an empty list and a reason.

Return JSON: {{"tasks": ["...", "..."], "skip_reason": null}}"""


def forbidden_identifiers(change: APIChange) -> list[str]:
    names = {change.name}
    if change.owner:
        names.add(change.owner)
    if change.moved_to:
        names.add(change.moved_to.rsplit(".", 1)[-1])
    names.add(change.path)
    # Parameter names are ordinary words ("temperature", "timeout"); allow those.
    return sorted(n for n in names if n and not n.startswith("__"))


def task_prompt(change: APIChange, import_names: list[str], n: int) -> str:
    extra = ""
    if change.moved_to:
        extra += f"\nThe object now lives at: {change.moved_to}"
    if change.hint:
        extra += f"\nThe old version's deprecation note said: {change.hint}"
    if change.deprecation:
        extra += f"\nDeprecation message in the new version: {change.deprecation}"
    return TASK_TEMPLATE.format(
        n=n,
        package=change.package,
        imports=", ".join(import_names) or change.package,
        change=change.describe(),
        old_version=change.from_version,
        new_version=change.to_version,
        old_signature=change.old_signature or change.path,
        old_doc=change.old_doc or "",
        new_signature=change.new_signature or "(removed)",
        new_doc=change.new_doc or "",
        extra=extra,
        forbidden=", ".join(f"`{f}`" for f in forbidden_identifiers(change)),
    )


def parse_tasks(text: str, change: APIChange) -> tuple[list[str], str | None]:
    """Keep only tasks that give nothing away.

    A task is dropped if it names the changed object (in any identifier-like spelling), or if
    it mentions, *as code*, the changed name, the changed parameter, or the replacement API
    (from the deprecation hint, the move target or names new in the signature).
    """
    data = parse_json_object(text)
    if data is None:
        return [], "task writer did not return JSON"
    tasks = [t.strip() for t in data.get("tasks") or [] if isinstance(t, str) and t.strip()]
    reason = data.get("skip_reason")
    leaky = [f for f in forbidden_identifiers(change) if _looks_like_identifier(f)]
    code_forms = leak_identifiers(change)
    clean = [
        t
        for t in tasks
        if not any(_mentions(t, f) for f in leaky)
        and not any(_mentions_as_code(t, f) for f in code_forms)
    ]
    if tasks and not clean:
        return [], "every task gave away the changed API or its replacement"
    return clean, (str(reason) if reason and not clean else None)


_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NOT_API = {
    "self",
    "cls",
    "None",
    "True",
    "False",
    "str",
    "int",
    "float",
    "bool",
    "list",
    "dict",
    "Any",
    "Optional",
    "Union",
    "kwargs",
    "args",
}


def leak_identifiers(change: APIChange) -> set[str]:
    """Names that must not appear in a task written as code."""
    names = {change.name}
    if change.parameter:
        names.add(change.parameter)
    if change.moved_to:
        names.add(change.moved_to.rsplit(".", 1)[-1])
    for text in (change.hint or "", change.deprecation or ""):
        for span in re.findall(r"`([^`]+)`", text):
            names.update(_WORD.findall(span))
        names.update(re.findall(r"([A-Za-z_]\w*)\s*\(", text))
        names.update(w for w in _WORD.findall(text) if _looks_like_identifier(w))
    old = set(_WORD.findall(change.old_signature or ""))
    names.update(
        w for w in _WORD.findall(change.new_signature or "") if w not in old and len(w) > 2
    )
    names = {n for n in names if n and n not in _NOT_API and not n.startswith("__")}
    if not change.name.startswith("__"):
        names.add(change.name)  # even builtin-looking names (``dict``) when written as code
    return names


def _mentions_as_code(text: str, name: str) -> bool:
    e = re.escape(name)
    if re.search(rf"`[^`]*(?<![\w]){e}(?![\w])[^`]*`", text):
        return True
    if re.search(rf"(?<![\w]){e}\s*[(=]", text) or re.search(rf"\.{e}(?![\w])", text):
        return True
    return _looks_like_identifier(name) and _mentions(text, name)


def _looks_like_identifier(name: str) -> bool:
    return "_" in name or "." in name or any(c.isupper() for c in name[1:])


def _mentions(text: str, identifier: str) -> bool:
    return re.search(rf"(?<![\w.]){re.escape(identifier)}(?![\w])", text) is not None


# ---------------------------------------------------------------- solver
SOLVER_TEMPLATE = """You are a senior Python developer working in an existing codebase.
The project pins {package}=={version} in its lockfile. Use that library to complete the task.
Reply with exactly one ```python code block containing complete, self-contained code (all imports included). No explanations."""

NOTES_PREAMBLE = "\n\nProject notes (from AGENTS.md):\n"


def solver_system(package: str, version: str, notes: str | None = None) -> str:
    text = SOLVER_TEMPLATE.format(package=package, version=version)
    if notes:
        text += NOTES_PREAMBLE + notes.strip()
    return text


# ------------------------------------------------------------- note writer
NOTE_SYSTEM = (
    "You keep AGENTS.md files accurate and short. You only state facts supported by the "
    "evidence you are given. Reply with a single JSON object and nothing else."
)

NOTE_TEMPLATE = """Coding assistants trained before {package} {new_version} keep writing outdated code for it. Write ONE bullet for the project's AGENTS.md that stops that mistake.

Change: {change}
Old API ({old_version}): {old_signature}
  {old_doc}
New API ({new_version}): {new_signature}
  {new_doc}{extra}

Outdated code an assistant wrote:
```python
{code}
```
Type checker errors against {package}=={new_version}:
{errors}

Rules for the bullet:
- At most 2 short sentences, at most 45 words.
- Start with the outdated usage in backticks, then say what to do instead in {package} {new_version}.
- If the feature was removed with no replacement, say to omit it or what to do instead. Do not invent APIs.
Also write a minimal, complete Python example of the correct usage for {package}=={new_version}. It will be type-checked against that exact version, so use only APIs you are confident exist in the new API shown above.
{feedback}
Return JSON: {{"bullet": "...", "example": "<python code>"}}"""


def note_prompt(
    change: APIChange,
    code: str,
    errors: list[str],
    feedback: str | None = None,
) -> str:
    extra = ""
    if change.moved_to:
        extra += f"\nThe object now lives at: {change.moved_to}"
    if change.hint:
        extra += f"\nThe old version's deprecation note said: {change.hint}"
    if change.deprecation:
        extra += f"\nDeprecation message: {change.deprecation}"
    if change.suggestions:
        extra += "\nSimilar names in the new version (may or may not be related): " + ", ".join(
            change.suggestions[:3]
        )
    fb = f"\nYour previous example failed the type check: {feedback}\nFix it." if feedback else ""
    return NOTE_TEMPLATE.format(
        package=change.package,
        new_version=change.to_version,
        old_version=change.from_version,
        change=change.describe(),
        old_signature=change.old_signature or change.path,
        old_doc=change.old_doc or "",
        new_signature=change.new_signature or "(removed)",
        new_doc=change.new_doc or "",
        extra=extra,
        code=code.strip()[:3000],
        errors="\n".join(f"- {e}" for e in errors[:6]) or "- (none reported)",
        feedback=fb,
    )


# --------------------------------------------------------------- utilities
def parse_json_object(text: str) -> dict[str, Any] | None:
    """Parse the first JSON object in a model reply (tolerates code fences and chatter)."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for cand in candidates:
        try:
            value = json.loads(cand)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None
