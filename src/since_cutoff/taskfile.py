"""The tasks of a run as a file: ``run --tasks-out`` writes it, ``run --tasks-from`` reuses it.

Re-running on the same tasks separates what the model under test does from what the task writer
happened to write, and lets anyone repeat a measurement exactly. The format::

    {
      "tool": "since-cutoff 0.2.0",
      "prompt_version": 2,
      "task_model": "claude-code:claude-sonnet-4-6",
      "tasks": {
        "<change fingerprint>": {"change": "<one-line description>", "tasks": ["...", "..."]}
      }
    }

The first task of a change is its probe; the others are held out. Changes are keyed by
:attr:`APIChange.fingerprint`, which does not depend on the version pair, so a file still
applies after a dependency upgrade to the changes that are still there. ``tool``,
``prompt_version`` and ``task_model`` say who wrote the tasks; a file written from reused tasks
keeps them.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from since_cutoff import __version__
from since_cutoff.apidiff import APIChange
from since_cutoff.errors import SinceCutoffError
from since_cutoff.prompts import PROMPT_VERSION, distinct_tasks

# Prefix of the skip reason for an API change the tasks file does not cover.
NOT_IN_TASKS_FILE = "not in the tasks file"


@dataclass
class TaskFile:
    """Tasks read with ``--tasks-from``: change fingerprint -> tasks, probe first."""

    tasks: dict[str, list[str]]
    source: str  # the file name, for reports and skip reasons
    tool: str | None = None
    prompt_version: int | None = None
    task_model: str | None = None  # the model that wrote the tasks, if the file says

    def tasks_for(self, change: APIChange, n: int) -> tuple[list[str], str | None]:
        """Up to ``n`` distinct tasks for a change, or none and the reason, like the task writer.

        A task that repeats an earlier one (the probe included), ignoring case, punctuation and
        spacing, is dropped, as the task writer's are (:func:`distinct_tasks`). As with written
        tasks, a change then needs at least ``min(n, 2)`` tasks: a probe and, when held-out
        tasks were asked for, one of them.
        """
        tasks = self.tasks.get(change.fingerprint)
        if tasks is None:
            return [], f"{NOT_IN_TASKS_FILE} {self.source}"
        distinct = distinct_tasks(tasks)
        need = min(n, 2)
        if len(distinct) < need:
            have = f"{len(distinct)} task{'' if len(distinct) == 1 else 's'}"
            repeats = " once repeats are dropped" if len(distinct) < len(tasks) else ""
            return [], f"{self.source} has {have} for this change{repeats}, {need} needed"
        return distinct[:n], None

    def provenance(self) -> dict[str, Any]:
        """Who wrote these tasks, as the file says (None where it does not)."""
        return {
            "task_model": self.task_model,
            "prompt_version": self.prompt_version,
            "tool": self.tool,
        }


def tasks_document(
    used: Iterable[tuple[APIChange, list[str]]],
    *,
    task_model: str | None,
    prompt_version: int | None = PROMPT_VERSION,
    tool: str | None = None,
) -> dict[str, Any]:
    """The JSON document for ``--tasks-out``: every probed change with the tasks it used.

    ``task_model``, ``prompt_version`` and ``tool`` name who wrote the tasks: this run's task
    writer by default, or the writer a ``--tasks-from`` file names when the tasks came from it.
    """
    return {
        "tool": tool or f"since-cutoff {__version__}",
        "prompt_version": prompt_version,
        "task_model": task_model,
        "tasks": {
            change.fingerprint: {"change": change.describe(), "tasks": list(tasks)}
            for change, tasks in used
        },
    }


def write_tasks(path: Path, document: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", "utf-8")
    except OSError as exc:
        raise SinceCutoffError(f"cannot write the tasks to {path}: {exc.strerror or exc}") from exc


def read_tasks(path: Path) -> TaskFile:
    """Read a ``--tasks-out`` file. Anything malformed is an error, before any model call.

    UTF-8 with or without a byte order mark (Notepad and Windows PowerShell write one).
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise SinceCutoffError(f"cannot read the tasks file {path}: {exc.strerror or exc}") from exc
    except ValueError as exc:
        raise SinceCutoffError(f"the tasks file {path} is not valid JSON: {exc}") from exc
    entries = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        raise SinceCutoffError(
            f'the tasks file {path} has no "tasks" object; write one with run --tasks-out'
        )
    tasks: dict[str, list[str]] = {}
    for fingerprint, entry in entries.items():
        items = entry.get("tasks") if isinstance(entry, dict) else entry
        if not isinstance(items, list) or not all(isinstance(t, str) and t.strip() for t in items):
            raise SinceCutoffError(
                f"the tasks file {path}: the tasks of {fingerprint} must be a list of non-empty "
                "strings"
            )
        tasks[str(fingerprint)] = [t.strip() for t in items]
    version = data.get("prompt_version")
    writer = data.get("task_model")
    return TaskFile(
        tasks,
        path.name,
        tool=str(data["tool"]) if data.get("tool") else None,
        prompt_version=version if isinstance(version, int) else None,
        task_model=(writer.strip() or None) if isinstance(writer, str) else None,
    )
