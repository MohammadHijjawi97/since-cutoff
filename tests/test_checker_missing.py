"""The checker when basedpyright is not installed. Not in test_checker.py, whose tests are all
marked ``pyright`` (they run basedpyright): ``pytest -m "not pyright"``, the run without the
type checker, is where this one matters."""

from __future__ import annotations

import shutil
import sys

import pytest

from since_cutoff.checker import Checker, pyright_command
from since_cutoff.errors import CheckerError


def test_without_basedpyright_the_error_names_the_extra(monkeypatch, cache, request) -> None:
    """basedpyright is the ``"since-cutoff[run]"`` extra, not a dependency: scan, sync, status
    and the MCP server never run it."""
    assert request.node.get_closest_marker("pyright") is None  # not deselected without it
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setitem(sys.modules, "basedpyright", None)  # makes the import fail
    with pytest.raises(CheckerError) as info:
        pyright_command()
    message = str(info.value)
    assert message.startswith("run checks the model's answers with the basedpyright type checker")
    assert 'pip install "since-cutoff[run]"' in message
    assert message.endswith("or uvx --with basedpyright since-cutoff run")
    with pytest.raises(CheckerError, match="since-cutoff\\[run\\]"):
        Checker(cache)  # the engine builds one at the first probe: the same message there
