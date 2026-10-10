"""Which changed APIs the code uses, precisely enough to drive "old form", `--fail-on
old-form`, the annotations and the notes (0.4 review).

- a beta mirror (``client.beta.messages.create``) is another API than the one outside beta;
- a keyword counts only when it is passed to the changed callable itself, not to another
  library's ``create`` in the same file;
- a removed name is not reported under another name of the same object;
- ``sync --scope imported`` does not spend its slots on a params class's mirror of a lost
  parameter, or on an internal hook.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from since_cutoff import cli
from since_cutoff.apidiff import (
    PARAM_REMOVED,
    PARAM_REQUIRED,
    REMOVED,
    APIChange,
    diff_sources,
)
from since_cutoff.engine import (
    CHANGED,
    Engine,
    ModelTarget,
    PackageScan,
    ScanResult,
)
from since_cutoff.notes import SCOPE_IMPORTED, diff_notes
from since_cutoff.project import Project, scan_file
from since_cutoff.selection import (
    BUILTIN_MEMBERS,
    COMMON_NAMES,
    OLD_FORM,
    USES_API,
    collapse,
    form,
    unique_member_names,
    used_names,
)
from tests.conftest import write_tree
from tests.test_ci import scan_app


def uses(code: str, file: str = "app/main.py") -> list[Any]:
    return [scan_file(ast.parse(code), file)]


def toy_diff(tmp_path: Path, package: str, old: dict[str, str], new: dict[str, str], **kw: str):
    import_name = next(iter(old)).split("/")[0]
    a = write_tree(tmp_path / f"{package}-old", old)
    b = write_tree(tmp_path / f"{package}-new", new)
    versions = (kw.get("old_version", "1.0"), kw.get("new_version", "2.0"))
    found = diff_sources(package, versions[0], a, versions[1], b, [import_name])
    return [APIChange.from_dict(d) for d in found]


# --------------------------------------------------------------- beta mirrors
def _sdk(beta_extra: str) -> dict[str, str]:
    return {
        "anthropic/__init__.py": """
            from ._client import Anthropic

            __all__ = ["Anthropic"]
        """,
        "anthropic/_client.py": """
            from .resources.beta.beta import Beta
            from .resources.messages.messages import Messages


            class Anthropic:
                messages: Messages
                beta: Beta
        """,
        "anthropic/resources/__init__.py": "",
        "anthropic/resources/messages/__init__.py": "",
        "anthropic/resources/messages/messages.py": """
            class Messages:
                def create(self, *, model: str, max_tokens: int%s) -> str:
                    return model
        """
        % ("" if beta_extra == "new" else ", temperature: float | None = None"),
        "anthropic/resources/beta/__init__.py": "",
        "anthropic/resources/beta/beta.py": """
            from .messages.messages import Messages


            class Beta:
                messages: Messages
        """,
        "anthropic/resources/beta/messages/__init__.py": "",
        "anthropic/resources/beta/messages/messages.py": """
            class Messages:
                def create(self, *, model: str, max_tokens: int%s) -> str:
                    return model
        """
        % (
            ""
            if beta_extra == "new"
            else ", temperature: float | None = None, output_format: dict | None = None"
        ),
    }


NON_BETA_CALL = """
import anthropic

client = anthropic.Anthropic()
client.messages.create(model="m", max_tokens=1, messages=[])
"""
BETA_CALL = """
import anthropic

client = anthropic.Anthropic()
client.beta.messages.create(model="m", max_tokens=1, output_format={"type": "json"})
"""


def test_a_beta_only_change_is_not_said_of_the_api_outside_beta(tmp_path) -> None:
    """anthropic 0.77.0 -> 1.8.0: both ``Messages.create`` lost ``temperature``, only the beta
    one ``output_format``. The note for code that calls ``client.messages.create`` must not say
    it no longer accepts ``output_format`` (0.4's first cut said so: one API key for both)."""
    changes = toy_diff(
        tmp_path,
        "anthropic",
        _sdk("old"),
        _sdk("new"),
        old_version="0.77.0",
        new_version="1.8.0",
    )
    [beta_only] = [c for c in changes if c.parameter == "output_format"]
    plain = next(c for c in changes if c.parameter == "temperature" and not c.in_beta)
    assert beta_only.in_beta and not plain.in_beta
    assert beta_only.api_key != plain.api_key  # two APIs, though both are Messages.create

    distinct = collapse(changes)
    files = uses(NON_BETA_CALL)
    assert used_names(beta_only, files) == ()
    used = [c for c in distinct if used_names(c, files)]
    [note] = diff_notes(used)
    assert note.api == "Messages.create"
    assert "temperature" in note.bullet and "output_format" not in note.bullet

    # Code that calls the beta mirror gets its note, named so that it cannot be read as the
    # API outside beta.
    files = uses(BETA_CALL)
    assert used_names(beta_only, files) == ("create", "output_format")
    notes = {n.api: n for n in diff_notes([c for c in distinct if used_names(c, files)])}
    beta = notes["anthropic.resources.beta.messages.messages.Messages.create"]
    assert beta.line.startswith(
        "`anthropic.resources.beta.messages.messages.Messages.create()` no longer accepts "
        "`output_format`"
    )


# ------------------------------------------------ keywords passed to the callable
def change(kind: str, path: str, name: str, owner: str | None, parameter: str | None) -> APIChange:
    return APIChange(
        "pkg",
        "1",
        "2",
        kind,
        path,
        name,
        owner=owner,
        parameter=parameter,
        import_paths=[path],
    )


TWO_SDKS = """
import anthropic
import openai

client = anthropic.Anthropic()
oai = openai.OpenAI()


def ask(q):
    client.messages.create(model="m", max_tokens=100, messages=[q])
    return oai.chat.completions.create(model="o", messages=[q], temperature=0.2, top_p=0.9)
"""


def test_a_keyword_passed_to_another_librarys_create_is_not_the_old_form() -> None:
    """One file calls anthropic's ``client.messages.create`` without ``temperature`` and
    openai's ``chat.completions.create(temperature=...)``: anthropic's call "uses this API";
    it is not the old form (which drove ``--fail-on old-form`` and a warning annotation)."""
    temperature = change(
        PARAM_REMOVED,
        "anthropic.resources.messages.Messages.create",
        "create",
        "Messages",
        "temperature",
    )
    files = uses(TWO_SDKS)
    assert used_names(temperature, files) == ("create",)
    assert form(temperature, used_names(temperature, files)) == USES_API
    passed = TWO_SDKS.replace("max_tokens=100,", "max_tokens=100, temperature=0.5,")
    assert used_names(temperature, uses(passed)) == ("create", "temperature")
    assert form(temperature, ("create", "temperature")) == OLD_FORM


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        # A method matched on its class: the keyword must go to that class's method.
        (
            "from pkg import Client\nimport other\nc = Client()\nc.send('hi')\n"
            "other.Session().send(temperature=0.2)\n",
            ("send",),
        ),
        ("from pkg import Client\nc = Client()\nc.send('hi', temperature=0.2)\n", None),
        ("from pkg import Client\nClient().send('hi', temperature=0.2)\n", None),
        # In a subclass.
        (
            "from pkg import Client\n\nclass Mine(Client):\n    def go(self):\n"
            "        return self.send('hi', temperature=0.2)\n",
            None,
        ),
    ],
)
def test_a_keyword_counts_for_a_method_only_when_passed_to_it(code, expected) -> None:
    send = change(PARAM_REMOVED, "pkg.Client.send", "send", "Client", "temperature")
    assert used_names(send, uses(code)) == (expected or ("send", "temperature"))


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("from pkg import fetch\nimport other\nfetch('u')\nother.Pool().fetch(retries=5)\n", False),
        ("from pkg import fetch\nfetch('u', retries=5)\n", True),
        ("from pkg import fetch as get\nget('u', retries=5)\n", True),
        ("import pkg\npkg.fetch('u', retries=5)\n", True),
        ("from pkg import *\nfetch('u', retries=5)\n", True),
    ],
)
def test_a_keyword_counts_for_a_function_only_when_passed_to_it(code, expected) -> None:
    fetch = change(PARAM_REMOVED, "pkg.fetch", "fetch", None, "retries")
    names = ("fetch", "retries") if expected else ("fetch",)
    assert used_names(fetch, uses(code)) == names


def test_scan_fail_on_old_form_ignores_a_keyword_of_another_call(
    tmp_path, capsys, monkeypatch, fake_pypi
) -> None:
    """End to end: toylib 2.0's ``Client.send`` lost ``temperature``; the file passes
    ``temperature`` only to another library's ``send``."""
    monkeypatch.setenv("SINCE_CUTOFF_CACHE", str(tmp_path / "cli-cache"))
    monkeypatch.setattr(
        cli, "Engine", lambda settings, **kw: Engine(settings, **{**kw, "pypi": fake_pypi})
    )
    root = tmp_path / "app"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0"\ndependencies = ["toylib==2.0"]\n'
    )
    main = "from toylib import Client\nimport other\n\nClient().send('hi')\n"
    (root / "main.py").write_text(main + "other.Session().send('x', temperature=0.2)\n")
    argv = ["scan", str(root), "--cutoff", "2025-07-31", "--fail-on", "old-form"]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "calls send" in out and "passes temperature" not in out
    (root / "main.py").write_text(main.replace("send('hi')", "send('hi', temperature=0.2)"))
    assert cli.main(argv) == 3


# ----------------------------------------------------- names of one object
ALIASES_OLD = {
    "pkg/__init__.py": "",
    "pkg/utils.py": """
        class Warn(UserWarning):
            pass


        Old1 = Warn
        Old2 = Warn
        Keep = Warn
    """,
}
ALIASES_NEW = {
    "pkg/__init__.py": "",
    "pkg/utils.py": """
        class Warn(UserWarning):
            pass


        Keep = Warn
    """,
}


def test_two_removed_names_of_one_class_are_two_changes_each_under_its_own_name(
    tmp_path,
) -> None:
    """cryptography 45 -> 50: ``DeprecatedIn37`` and ``DeprecatedIn45`` are both
    ``= CryptographyDeprecationWarning``. Code importing one uses that one: the scan reported
    the other too, under a name the file never uses."""
    changes = {c.name: c for c in toy_diff(tmp_path, "pkg", ALIASES_OLD, ALIASES_NEW)}
    old1, old2 = changes["Old1"], changes["Old2"]
    assert (old1.kind, old2.kind) == (REMOVED, REMOVED)
    assert old1.import_paths == ["pkg.utils.Old1"]
    assert old2.import_paths == ["pkg.utils.Old2"]
    files = uses("from pkg.utils import Old1\n")
    assert used_names(old1, files) == ("Old1",)
    assert used_names(old2, files) == ()


def test_a_re_export_under_the_same_name_still_counts(tmp_path) -> None:
    old = {
        "pkg/__init__.py": "from pkg._impl import Thing\n\n__all__ = ['Thing']\n",
        "pkg/_impl.py": "class Thing:\n    pass\n",
    }
    new = {"pkg/__init__.py": "", "pkg/_impl.py": ""}
    [gone] = [c for c in toy_diff(tmp_path, "pkg", old, new) if c.name == "Thing"]
    assert "pkg.Thing" in (gone.import_paths or [])
    assert used_names(gone, uses("from pkg import Thing\n")) == ("Thing",)


# -------------------------------------------------- sync --scope imported
def test_scope_imported_skips_params_mirrors_and_internal_hooks(tmp_path) -> None:
    """agent-app: anthropic's ``MessageCreateParamsBase.temperature`` (a TypedDict field) is
    ``Messages.create``'s lost parameter again; pypistats: sqlalchemy's
    ``declarative_scan_for_composite`` needs a parameter of a type private to sqlalchemy.
    Neither takes one of the 5 slots."""

    def c(kind: str, path: str, name: str, owner: str | None, param: str | None, **kw: Any):
        return APIChange(
            "sdk", "1.0", "2.0", kind, path, name, owner, param, import_paths=[path], **kw
        )

    create = c(PARAM_REMOVED, "sdk.messages.Messages.create", "create", "Messages", "temperature")
    mirrors = [
        c(REMOVED, f"sdk.types.MessageCreateParamsBase.{p}", p, "MessageCreateParamsBase", None)
        for p in ("temperature", "top_p")
    ]
    lost_top_p = c(PARAM_REMOVED, "sdk.messages.Messages.create", "create", "Messages", "top_p")
    hook = c(
        PARAM_REQUIRED,
        "sdk.orm.MappedColumn.declarative_scan_for_composite",
        "declarative_scan_for_composite",
        "MappedColumn",
        "decl_scan",
        new_signature=(
            "declarative_scan_for_composite(self, registry, cls, key: str, "
            "decl_scan: _ClassScanMapperConfig) -> None"
        ),
    )
    others = [c(REMOVED, f"sdk.old{i}", f"old{i}", None, None) for i in range(2)]
    package = PackageScan("sdk", "2.0", "uv.lock", True, True, CHANGED, cutoff_version="1.0")
    package.import_names = ["sdk"]
    package.changes = [create, lost_top_p, *mirrors, hook, *others]
    code = "from sdk.messages import Messages\nMessages().create(model='m')\n"
    project = Project(tmp_path, [], "uv.lock", imported_modules={"sdk"}, files=uses(code))
    scan = ScanResult(project, ModelTarget.cutoff_only(date(2025, 7, 31)), [package])
    # 6 APIs, 5 slots: without the rule, 2 of them went to the mirrors and 1 to the hook.
    apis = {n.api for n in scan.scope_notes(SCOPE_IMPORTED)}
    assert apis == {"Messages.create", "sdk.old0", "sdk.old1"}
    # Only for the APIs the code does not use: a used one always gets its note.
    code += "from sdk.orm import MappedColumn\nMappedColumn.declarative_scan_for_composite(1)\n"
    scan = ScanResult(
        Project(tmp_path, [], "uv.lock", imported_modules={"sdk"}, files=uses(code)),
        ModelTarget.cutoff_only(date(2025, 7, 31)),
        [package],
    )
    apis = {n.api for n in scan.scope_notes(SCOPE_IMPORTED)}
    assert "MappedColumn.declarative_scan_for_composite" in apis


def test_scope_imported_writes_nothing_for_an_internal_hook_alone(tmp_path) -> None:
    hook = APIChange(
        "sqlalchemy",
        "2.0.42",
        "2.0.43",
        PARAM_REQUIRED,
        "sqlalchemy.orm.MappedColumn.declarative_scan_for_composite",
        "declarative_scan_for_composite",
        "MappedColumn",
        "decl_scan",
        new_signature="f(self, decl_scan: orm._ClassScanMapperConfig) -> None",
        import_paths=["sqlalchemy.orm.MappedColumn.declarative_scan_for_composite"],
    )
    package = PackageScan(
        "sqlalchemy", "2.0.43", "uv.lock", True, True, CHANGED, cutoff_version="2.0.42"
    )
    package.import_names, package.changes = ["sqlalchemy"], [hook]
    project = Project(
        tmp_path, [], "uv.lock", imported_modules={"sqlalchemy"}, files=uses("import sqlalchemy\n")
    )
    scan = ScanResult(project, ModelTarget.cutoff_only(date(2025, 7, 31)), [package])
    assert scan.scope_notes(SCOPE_IMPORTED) == []


def test_the_real_scan_still_finds_what_the_code_uses(tmp_path, cache, fake_pypi) -> None:
    """The toy app of the other tests: nothing lost by the stricter matching."""
    root = tmp_path / "app"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0"\ndependencies = ["toylib==2.0"]\n'
    )
    (root / "main.py").write_text(
        "from toylib import Client, fetch\nClient().send('hi', temperature=0.2)\nfetch('u')\n"
    )
    scan = scan_app(root, cache, fake_pypi, date(2025, 7, 31))
    forms = {u.note.api: u.form for u in scan.used_apis()}
    assert forms == {"Client.send": OLD_FORM, "toylib.fetch": USES_API}


def test_a_builtin_types_member_is_never_a_name_match() -> None:
    # On ordinary code, ``a_set.union()`` was polars' ``Enum.union`` and ``dt.fromisoformat()``
    # pandas' ``Timestamp.fromisoformat`` by name alone; ``arr.shape`` and ``args.verbose``
    # were a package's too.
    names = ("union", "fromisoformat", "is_integer", "shape", "verbose", "swapaxes")
    scanned = PackageScan("pkg", "2", "lock", True)
    scanned.changes = [change(REMOVED, f"pkg.Thing.{n}", n, "Thing", None) for n in names]
    assert {"union", "fromisoformat", "is_integer"} <= BUILTIN_MEMBERS
    assert {"shape", "view", "to_dict", "to_json", "labels", "verbose", "rank"} <= COMMON_NAMES
    assert unique_member_names([scanned]) == {"swapaxes"}
