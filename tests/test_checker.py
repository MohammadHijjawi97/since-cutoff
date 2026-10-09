from __future__ import annotations

import shutil
import sys

import pytest

from since_cutoff.checker import Checker, extract_code, pyright_command
from since_cutoff.engine import DEPRECATED, INVALID, OFFTASK, PASS, STALE, WRONG, classify
from since_cutoff.errors import CheckerError

pytestmark = pytest.mark.pyright


@pytest.fixture
def checker(cache):
    return Checker(cache, python_version="3.12")


def run(checker, toylib, code: str):
    v1, v2 = toylib
    new = checker.check(v2, {"x": code})["x"]
    old = checker.check(v1, {"x": code})["x"]
    return classify(new, old)


def test_code_valid_only_for_the_old_version_is_stale(checker, toylib):
    outcome, errors = run(
        checker, toylib, "from toylib import Client\nClient().send('hi', temperature=0.2)\n"
    )
    assert outcome == STALE
    assert errors and "temperature" in errors[0]


def test_code_valid_for_the_new_version_passes(checker, toylib):
    assert run(checker, toylib, "from toylib import Client\nClient().send('hi')\n")[0] == PASS


def test_invented_api_is_wrong_not_stale(checker, toylib):
    assert run(checker, toylib, "from toylib import Client\nClient().transmit('hi')\n")[0] == WRONG


def test_removed_import_is_stale(checker, toylib):
    assert run(checker, toylib, "from toylib import legacy_fetch\nlegacy_fetch('u')\n")[0] == STALE


def test_missing_new_required_argument_is_stale(checker, toylib):
    assert run(checker, toylib, "from toylib import fetch\nfetch('u')\n")[0] == STALE


def test_pep702_deprecated_call_is_flagged(checker, toylib):
    code = "from toylib import Client\nc = Client()\nc.close()\n"
    assert run(checker, toylib, code)[0] == DEPRECATED


def test_errors_are_attributed_through_attributes_and_self(checker, toylib):
    code = (
        "import toylib\n"
        "class Bot:\n"
        "    def __init__(self) -> None:\n"
        "        self.client = toylib.Client()\n"
        "    def ask(self, q: str) -> str:\n"
        "        return self.client.send(q, temperature=0.1)\n"
    )
    assert run(checker, toylib, code)[0] == STALE


def test_other_packages_and_unrelated_mistakes_do_not_count(checker, toylib):
    code = (
        "import numpy as np\n"
        "from toylib import Client\n"
        "x = np.zeros(3)\n"
        "y = undefined_name + 1\n"
        "Client().send('hi')\n"
    )
    _, v2 = toylib
    res = checker.check(v2, {"x": code})["x"]
    assert classify(res, None)[0] == PASS
    assert any("undefined_name" in d.message for d in res.other_errors)


def test_code_that_ignores_the_package_is_off_task(checker, toylib):
    assert run(checker, toylib, "import json\nprint(json.dumps({}))\n")[0] == OFFTASK


def test_type_ignore_comments_cannot_hide_stale_code(checker, toylib):
    code = "from toylib import Client\nClient().send('hi', temperature=0.2)  # type: ignore\n"
    assert run(checker, toylib, code)[0] == STALE


def test_syntax_errors_are_invalid(checker, toylib):
    assert run(checker, toylib, "from toylib import Client\ndef broken(:\n")[0] == INVALID


def test_many_snippets_in_one_run(checker, toylib):
    _, v2 = toylib
    snippets = {str(i): f"from toylib import Client\nClient().send('{i}')\n" for i in range(20)}
    results = checker.check(v2, snippets)
    assert len(results) == 20 and all(not r.api_errors for r in results.values())


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("Here you go:\n```python\nprint(1)\n```\nDone.", "print(1)\n"),
        ("```py\nx = 1\n```", "x = 1\n"),
        ("```\ny = 2\n```", "y = 2\n"),
        ("```python\nshort\n```\n```python\nthe_longer_block = 1\n```", "the_longer_block = 1\n"),
        ("z = 3", "z = 3\n"),
        ("Sorry, I cannot help with that.", None),
        ("", None),
    ],
)
def test_extract_code(answer, expected):
    assert extract_code(answer) == expected


def test_without_basedpyright_the_error_names_the_extra(monkeypatch, cache) -> None:
    """basedpyright is the ``since-cutoff[run]`` extra, not a dependency: scan, sync, status
    and the MCP server never run it."""
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
