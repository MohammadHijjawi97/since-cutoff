"""``since-cutoff explain API`` and the MCP tool ``explain_api`` (issue #28): everything
since-cutoff knows about one API, from the scan sync runs, with no model call and nothing
written.

The toy project and the fake PyPI are test_sync.py's: toylib 1.0 is the release at the cutoff
2025-07-31 and the project pins 2.0, in which `Client.send` lost `temperature` ([diff]),
`legacy_fetch` was removed with "Use fetch instead." in its docstring ([diff + library]),
`Client.close` is deprecated with "Use Client.shutdown() instead." and
`toylib.helpers.Session` moved to `toylib.Session` ([diff + move checked])."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

import since_cutoff.explain as explain_module
from since_cutoff import cli
from since_cutoff.cache import DiskCache
from since_cutoff.errors import PackageIndexError
from since_cutoff.explain import (
    SUFFIX,
    Found,
    Lookup,
    _change_lines,
    _declared,
    candidates,
    lookup_lines,
)
from since_cutoff.mcp_server import Tools, build_server
from since_cutoff.models import ModelRegistry
from tests import test_sync
from tests.test_ci import scan_app
from tests.test_sync import SONNET, agents, make_app, run_block, set_pins, write

# The fixtures of test_sync.py: the fake PyPI, and the CLI without network (``sc``).
pypi = test_sync.pypi
sc = test_sync.sc


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def explain(sc: Any, root: Path, api: str, *flags: str) -> tuple[int, str]:
    """``explain API FLAGS PATH`` in a project, which it leaves as it was (an option between
    the API and the path, which Python 3.10's argparse did not take)."""
    before = snapshot(root)
    code, out = sc("explain", api, *flags, root)
    assert snapshot(root) == before  # nothing written
    return code, out


def flat(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture
def app(sc: Any, tmp_path: Path) -> Path:
    root = make_app(tmp_path)
    assert sc("sync", root, *SONNET, "--yes")[0] == 0
    return root


def test_a_diff_note_with_both_signatures_and_where_the_code_uses_it(sc, app) -> None:
    code, out = explain(sc, app, "Client.send")
    assert code == 0, out
    # The model and cutoff the notes were written for.
    assert "(from the notes in AGENTS.md; --model to change)" in out
    assert "## Client.send (toylib)" in out
    assert (
        "- `Client.send()` no longer accepts `temperature`; do not pass it. since-cutoff found no "
        "replacement in toylib's deprecation text. Pass `stream` to `Client.send()` by keyword. "
        "[diff]" in out
    )
    assert "Written in: AGENTS.md (from the diff)." in out
    assert (
        "Package: toylib 2.0 (2025-10-01), from pyproject.toml; compared from 1.0 (2025-01-10), "
        "the latest release 30 days before the cutoff." in out
    )
    assert "- param_removed: `toylib.Client.send(temperature=...)`: parameter `temperature`" in out
    assert "  1.0: send(self, message: str, temperature: float = 1.0, stream: bool = False)" in out
    assert "  2.0: send(self, message: str, *, stream: bool = False) -> str" in out
    assert out.count("  2.0: send(self") == 1  # both changes of one API: said once
    # The changed parameter in each release, which a long signature may cut off.
    assert "  parameter `temperature`: in 1.0, `temperature: float = 1.0`; in 2.0, removed" in out
    assert "parameter `stream`" not in out  # it reads the same: keyword-only says it
    assert "- [diff] a static comparison of the two releases' public APIs" in out
    assert "- checked: static API diff (griffe), 1.0 vs 2.0, diff schema" in out
    assert "- runtime: not checked" in out
    assert "- main.py: old form, passes temperature to send" in out
    assert "- sub/other.py: uses this API, calls send" in out
    assert "- no example was type-checked against the pinned version" in out


def test_a_parameter_past_a_cut_signature_says_so(cache, pypi, tmp_path) -> None:
    """anthropic's Messages.create: the 400 characters of its signature end before the
    `temperature` the note is about."""
    cut = "create(self, *, model: str, max_tokens: int, system: str | None = None, temperat..."
    assert _declared(cut, "model") == "`model: str`"
    assert _declared(cut, "top_k") == "past the end of the signature above (cut at 400 characters)"
    assert _declared("f(a, b)", "c") == "not a parameter"
    assert _declared("f(x: dict[str, int] = {'a': 1}, *, c=2)", "c") == "`c=2`"
    scan = scan_app(make_app(tmp_path), cache, pypi, date(2025, 7, 31))
    [f] = [f for f in candidates(scan) if f.api == "Client.send"]
    removed = replace(f.note.covered[0], old_signature="send(self, message: str, s...")
    assert (
        "  parameter `temperature`: in 1.0, past the end of the signature above (cut at 400 "
        "characters); in 2.0, removed" in _change_lines(scan, removed, f.package)
    )
    # The cut ends inside the declaration: it is not shown as if it were all of it.
    assert _declared("create(self, *, model: str, max_tokens: int = 1...", "max_tokens") == (
        "`max_tokens: int = 1...` (the signature above is cut at 400 characters)"
    )
    # A cut in the return annotation leaves the parameters whole.
    assert _declared("f(a, b: int) -> dict[str, Lon...", "b") == "`b: int`"
    inside = replace(f.note.covered[0], old_signature="send(self, message: str, temperature: fl...")
    assert (
        "  parameter `temperature`: in 1.0, `temperature: fl...` (the signature above is cut at "
        "400 characters); in 2.0, removed" in _change_lines(scan, inside, f.package)
    )


def test_a_bullet_shared_with_other_apis_and_a_parameter_still_read(
    cache, pypi, tmp_path, monkeypatch
) -> None:
    """A block writes the notes that say the same of several APIs as one bullet (#99):
    explain shows that bullet beside the API's own note; and the pinned release's code that
    still reads a removed parameter, in its own words."""
    from since_cutoff.notes import diff_notes

    scan = scan_app(make_app(tmp_path), cache, pypi, date(2025, 7, 31))
    [f] = [f for f in candidates(scan) if f.api == "toylib.fetch"]
    c = f.note.covered[0]
    twin = replace(c, path="toylib.fetch_all", name="fetch_all", import_paths=None)
    [merged] = diff_notes([c, twin], merge=True)
    assert merged.apis == ["toylib.fetch", "toylib.fetch_all"]
    monkeypatch.setattr(scan, "scope_notes", lambda *args, **kwargs: [merged])
    lines = explain_module.explain_lines(scan, f, {})
    i = lines.index("The block writes it in a bullet shared with other APIs:")
    assert lines[i - 1] == f"- {f.note.line}" and lines[i + 1] == f"- {merged.line}"
    assert explain_module._found_json(scan, f, {}, None)["block_bullet"] == merged.line
    [send] = [f for f in candidates(scan) if f.api == "Client.send"]
    still = replace(send.note.covered[0], still_handled_text="temperature is ignored")
    assert '  2.0\'s code that still reads it says: "temperature is ignored"' in _change_lines(
        scan, still, send.package
    )


def test_a_name_in_nested_call_syntax_is_the_name() -> None:
    """``(``[^()]*``)$`` left a call whose arguments are calls as it was."""
    clean = explain_module.clean_name
    assert clean("`Client.send(timeout=Timeout(5))`") == "Client.send"
    assert clean(" ``fetch()`` ") == "fetch"
    assert clean("`fetch`()") == "fetch"  # a call after the backticks: twice round
    assert clean("send())") == "send())"  # brackets that do not balance stay


def test_a_library_replacement_with_the_text_and_file_it_rests_on(sc, app) -> None:
    code, out = explain(sc, app, "legacy_fetch")
    assert code == 0, out
    assert "- `toylib.legacy_fetch` was removed; do not use it. Use `toylib.fetch` instead." in out
    assert "[diff + library]" in out
    assert '  toylib 1.0 said (toylib/helpers.py), docstring: ".. deprecated:: 1.0 Use fetch' in out
    assert (
        "- replacement `toylib.fetch` for `toylib.legacy_fetch` (library_text): toylib 1.0 "
        "toylib/helpers.py" in out
    )
    assert "- [library] the replacement is named in the library's own deprecation text" in out
    # The code does not use it: no block has it, and nothing says where it is used.
    assert "- nowhere: your code does not use this API (it imports the package)" in out
    assert "Not in a notes block yet" in out


def test_a_deprecation_with_the_librarys_text_and_its_file(sc, app) -> None:
    code, out = explain(sc, app, "Client.close")
    assert code == 0, out
    assert (
        "- deprecated: `toylib.Client.close` is deprecated: Use Client.shutdown() instead." in out
    )
    assert '  toylib 2.0 says (toylib/_client.py): "Use Client.shutdown() instead."' in out
    assert (
        "- replacement `toylib.Client.shutdown` for `Client.close` (library_text): toylib 2.0 "
        "toylib/_client.py" in out
    )


def test_a_moved_api_with_what_the_move_kept(sc, app) -> None:
    code, out = explain(sc, app, "Session")
    assert code == 0, out
    assert (
        "- `toylib.helpers.Session` moved to `toylib.Session`: import it with `from toylib "
        "import Session`. [diff + move checked]" in out
    )
    assert "  move evidence: compared class, kept 1, of 1" in out
    assert (
        "- replacement `toylib.Session` for `toylib.helpers.Session` (move_checked): toylib 2.0 "
        "API diff: the object at `toylib.Session` keeps 1 of 1 public names of the old one" in out
    )


@pytest.mark.parametrize(
    "name",
    [
        "toylib.fetch",
        "fetch",
        "toylib.helpers.fetch",
        "fetch()",
        "TOYLIB.FETCH",
        "`toylib.fetch()`",  # as a note prints it
        "`toylib.helpers.fetch(timeout=...)`",
    ],
)
def test_an_api_by_any_of_its_names(sc, app, name) -> None:
    code, out = explain(sc, app, name)
    assert code == 0 and "## toylib.fetch (toylib)" in out, out
    assert "- `toylib.fetch()` now requires `timeout`. [diff]" in out
    assert "- main.py: uses this API, calls fetch" in out


def test_a_module_path_goes_by_the_pypi_name_too(cache, pypi, tmp_path) -> None:
    root = make_app(tmp_path)
    scan = scan_app(root, cache, pypi, date(2025, 7, 31))
    [f] = [f for f in candidates(scan) if f.api == "toylib.fetch"]
    # As huggingface-hub's huggingface_hub.hf_hub_download.
    changes = [replace(c, package="toy-lib") for c in f.note.covered]
    note = replace(f.note, change=changes[0], changes=changes)
    names = Found(note, replace(f.package, name="toy-lib"), f.used).names()
    assert "toylib.fetch" in names and "toy-lib.fetch" in names


def test_a_switched_dependency_by_either_name_with_its_requires_dist(cache, pypi, tmp_path) -> None:
    from since_cutoff.engine import CHANGED, PackageScan
    from since_cutoff.notes import dependency_detail, diff_notes
    from tests.test_dependency_switch import _switch

    switch = _switch(tmp_path / "switch")
    [note] = diff_notes([switch])
    p = PackageScan(switch.package, switch.to_version, "pyproject.toml", True, status=CHANGED)
    names = Found(note, p, None).names()
    assert {switch.name, switch.switched_to} <= names
    scan = scan_app(make_app(tmp_path), cache, pypi, date(2025, 7, 31))
    assert f"  {dependency_detail(switch)}" in _change_lines(scan, switch, p)


def test_an_unknown_api_lists_close_matches(sc, app) -> None:
    code, out = explain(sc, app, "fetchh")
    assert code == 1
    assert (
        "No API named 'fetchh' changed in toylib after claude-sonnet-4-5's training cutoff "
        "(2025-07-31). Close matches: toylib.fetch" in out
    )
    code, out = explain(sc, app, "send", "--package", "notadep")
    assert code == 1 and "notadep is not a dependency of this project" in out


def test_two_given_releases_of_a_package_that_is_no_dependency(sc, tmp_path) -> None:
    """--from and --to scan the package all the same: a name it does not have is no reason to
    say that it is not a dependency."""
    root = make_app(tmp_path, ("otherlib==1.0",))
    versions = ("--package", "toylib", "--from", "1.0", "--to", "2.0", *SONNET)
    code, out = explain(sc, root, "send", *versions)
    assert code == 0 and "## Client.send (toylib)" in out, out
    code, out = explain(sc, root, "nosuch", *versions)
    assert code == 1 and "not a dependency" not in out
    assert "No API named 'nosuch' changed in toylib between 1.0 and 2.0." in out
    code, out = explain(sc, root, "nosuch", *versions, "--json")
    assert json.loads(out)["not_a_dependency"] is False
    code, out = explain(sc, root, "nosuch", "--package", "toylib", *SONNET, "--json")
    assert json.loads(out)["not_a_dependency"] is True


def test_a_lockfile_change_is_said_once_for_all_the_rule_files(sc, tmp_path) -> None:
    """With the rule files of three formats, one pin change made a line for each file."""
    root = make_app(tmp_path)
    for target in ("claude-rules", "cursor-rules", "copilot-instructions"):
        assert sc("sync", root, *SONNET, "--yes", "--target", target)[0] == 0
    set_pins(root, ("toylib==2.1", "otherlib==1.0"))
    code, out = explain(sc, root, "Client.send")
    assert code == 0, out
    assert flat(out).count("changed since the notes in") == 1
    assert (
        "pyproject.toml changed since the notes in .claude/rules/since-cutoff-toylib.md, "
        ".cursor/rules/since-cutoff-toylib.mdc and "
        ".github/instructions/since-cutoff-toylib.instructions.md were written: toylib 2.0 -> "
        "2.1: `since-cutoff sync` brings the notes in step." in flat(out)
    )
    code, out = sc("sync", root, "--check")
    assert flat(out).count("changed since the notes in") == 1, out


def test_rule_files_whose_package_kept_its_version_say_nothing_of_the_others(tmp_path) -> None:
    """Every rule file's deps hash covers every dependency: one bump gave a line "other
    dependencies" for each of the other packages' files."""
    from since_cutoff.project import load_project
    from since_cutoff.rules import FORMATS
    from since_cutoff.sync import (
        DEPENDENCIES_CHANGED,
        OUT_OF_DATE,
        PackageStatus,
        TargetFile,
        TargetStatus,
        lockfile_notices,
    )

    root = make_app(tmp_path)
    project = load_project(root)
    claude = FORMATS["claude-rules"]

    def status(name: str, bumped: bool) -> TargetStatus:
        t = TargetFile(
            claude.path(root, name), f".claude/rules/since-cutoff-{name}.md", "", None, None, claude
        )
        s = TargetStatus(t, problems=[(DEPENDENCIES_CHANGED, "other dependencies changed")])
        if bumped:
            s.packages = [PackageStatus(name, "1.0", "2.0", "pyproject.toml", OUT_OF_DATE)]
        return s

    agents_md = TargetFile(root / "AGENTS.md", "AGENTS.md", "", None, None)
    others = TargetStatus(agents_md, problems=[(DEPENDENCIES_CHANGED, "other dependencies")])
    said = lockfile_notices(project, [status("a", True), status("b", False), others])
    assert said == [
        "pyproject.toml changed since the notes in .claude/rules/since-cutoff-a.md were "
        "written: a 1.0 -> 2.0",
        "pyproject.toml changed since the notes in AGENTS.md were written: other dependencies",
    ]
    # Only other dependencies changed: one line, a format's files by their folder.
    said = lockfile_notices(project, [status("a", False), status("b", False)])
    assert said == [
        "pyproject.toml changed since the notes in the rule files in .claude/rules were "
        "written: other dependencies"
    ]


def test_a_name_a_replacement_only_names_is_not_written_in_the_block(sc, tmp_path) -> None:
    """The block's bullet for `legacy_fetch` names `toylib.fetch` as its replacement: that is
    no note for `fetch`, which changed too."""
    root = make_app(tmp_path)
    write(root / "main.py", "from toylib import legacy_fetch\nlegacy_fetch('u')\n")
    write(root / "sub" / "other.py", "import json\n")
    assert sc("sync", root, *SONNET, "--yes")[0] == 0
    assert "Use `toylib.fetch` instead." in agents(root)
    code, out = explain(sc, root, "toylib.fetch")
    assert code == 0, out
    assert "Written in" not in out and "Not in a notes block yet" in out


def test_another_note_for_the_api_in_a_block_is_shown_not_claimed(sc, app) -> None:
    write(app / "AGENTS.md", agents(app).replace("do not pass it.", "do not pass it, ever."))
    code, out = explain(sc, app, "Client.send")
    assert code == 0, out
    assert "Written in" not in out
    assert (
        "AGENTS.md has another note for it, for toylib 2.0 (`since-cutoff sync` writes the note "
        "above in its place):" in out
    )
    assert "- `Client.send()` no longer accepts `temperature`; do not pass it, ever." in out
    # For another cutoff than the block's, sync would not write the note above there.
    code, out = explain(sc, app, "Client.send", "--cutoff", "2024-09")
    assert "written for another model, cutoff or --cutoff-margin than this explanation" in out


def test_the_same_note_for_other_releases_says_them(sc, app) -> None:
    code, out = explain(sc, app, "fetch", "--package", "toylib", "--from", "1.1", "--to", "2.1")
    assert code == 0, out
    assert (
        "Written in: AGENTS.md (the same note, in its section for toylib 2.0 compared from 1.0)."
        in out
    )


def test_a_stale_block_and_a_package_that_could_not_be_checked_are_said(
    sc, app, pypi, monkeypatch
) -> None:
    """With the pin moved back to a release before the change, explain said only that no API
    of that name changed; with PyPI unreachable for the package, the same."""
    set_pins(app, ("toylib==1.0", "otherlib==1.0"))
    code, out = explain(sc, app, "Client.send")
    assert code == 1
    assert (
        "pyproject.toml changed since the notes in AGENTS.md were written: toylib 2.0 -> 1.0: "
        "`since-cutoff sync` brings the notes in step." in flat(out)
    )
    assert (
        "AGENTS.md has a note for it, for toylib 2.0, which this scan does not find: the notes "
        "may be out of date (`since-cutoff status`, `since-cutoff sync`)." in flat(out)
    )
    assert "- `Client.send()` no longer accepts `temperature`" in out
    set_pins(app, ("toylib==2.0", "otherlib==1.0"))
    releases = pypi.releases

    def unreachable(name: str) -> Any:
        if name == "toylib":
            raise PackageIndexError("PyPI could not be reached")
        return releases(name)

    monkeypatch.setattr(pypi, "releases", unreachable)
    code, out = explain(sc, app, "toylib.Client.send")
    assert code == 1
    text = flat(out)
    assert "toylib could not be checked (" in text and "so it has no notes here." in text
    assert "explain cannot say whether 'toylib.Client.send' changed in it." in text
    assert "AGENTS.md has a note for it, for toylib 2.0" in text


def test_json_has_the_same_and_more(sc, app) -> None:
    code, out = explain(sc, app, "send", "--json")
    assert code == 0
    result = json.loads(out)
    assert (result["query"], result["model"], result["cutoff"]) == (
        "send",
        "claude-sonnet-4-5",
        "2025-07-31",
    )
    assert result["match"] == "end_of_name" and result["truncated"] is False
    assert result["matches"] == [{"display": "Client.send", "package": "toylib"}]
    [found] = result["found"]
    # As results.json's used_apis names them: the full path, and the note's name.
    assert found["api"] == "toylib.Client.send" and found["display"] == "Client.send"
    assert found["in_blocks"] == [
        {
            "file": "AGENTS.md",
            "how": "diff",
            "bullet": found["note"]["line"],
            "version": "2.0",
            "compared_from": "1.0",
        }
    ]
    assert found["note"]["tags"] == ["diff"] and found["note"]["line"].endswith("[diff]")
    assert [c["kind"] for c in found["changes"]] == ["param_removed", "param_keyword_only"]
    assert found["changes"][0]["change_id"] == found["note"]["change_ids"][0]
    assert (
        found["changes"][0]["new_signature"]
        == "send(self, message: str, *, stream: bool = False) -> str"
    )
    assert found["block_bullet"] is None
    forms = {(u["file"], u["form"]) for u in found["uses"]}
    assert {("main.py", "old_form"), ("sub/other.py", "uses_api")} <= forms
    assert all(u["line"] is None for u in found["uses"])  # file-level until issue #8
    code, out = explain(sc, app, "fetchh", "--json")
    nothing = json.loads(out)
    assert code == 1 and nothing["found"] == [] and nothing["close_matches"] == ["toylib.fetch"]
    assert nothing["exit_code"] == 1


def test_another_cutoff_and_two_given_releases(sc, app) -> None:
    # A cutoff before toylib 1.0: it is compared from 0.9.
    code, out = explain(sc, app, "Client.send", "--cutoff", "2024-09")
    assert code == 0 and "compared from 0.9 (2024-06-01)" in out
    code, out = explain(sc, app, "fetch", "--package", "toylib", "--from", "1.1", "--to", "2.1")
    assert code == 0, out
    assert (
        "For claude-sonnet-4-5's training cutoff (2025-07-31); toylib from 1.1 (--from) to 2.1 "
        "(--to)." in out
    )
    assert (
        "Package: toylib 2.1 (2025-11-01), given with --to; compared from 1.1 (2025-09-01), "
        "given with --from." in out
    )
    assert "- main.py: uses this API, calls fetch" in out
    code, out = explain(sc, app, "fetch", "--package", "toylib", "--from", "1.1", "--json")
    [found] = json.loads(out)["found"]
    assert (found["versions_from"], found["from_given"], found["to_given"]) == (
        "pyproject.toml",
        True,
        False,
    )
    code, out = explain(sc, app, "fetch", "--package", "toylib", "--to", "2.1", "--json")
    [found] = json.loads(out)["found"]
    assert (found["versions_from"], found["to_given"]) == ("given with --to", True)
    code, out = explain(sc, app, "fetch", "--package", "toylib", "--from", "1.1")
    assert (
        "Package: toylib 2.0 (2025-10-01), from pyproject.toml; compared from 1.1 (2025-09-01), "
        "given with --from." in out
    )


def test_a_package_first_released_after_the_cutoff(sc, app) -> None:
    code, out = explain(sc, app, "toylib", "--cutoff", "2024-01-01")
    assert code == 0, out
    assert "- toylib first appeared on PyPI on 2024-06-01, after the cutoff" in out
    assert "- new package: first released 2024-06-01, after the cutoff; no release by" in out
    assert "new_package: `toylib` changed" not in out
    # Its note is for the files that import it.
    assert "- main.py: imports it\n- sub/other.py: imports it" in out
    assert "behind an unchanged signature" not in out
    assert "nothing of the library was run, nor its API read: there is no diff" in out
    code, out = explain(sc, app, "toylib", "--cutoff", "2024-01-01", "--json")
    [found] = json.loads(out)["found"]
    assert [u["file"] for u in found["uses"]] == ["main.py", "sub/other.py"]


def test_a_type_checked_bullet_is_the_note_shown(sc, tmp_path, cache, pypi) -> None:
    root = make_app(tmp_path)
    run_block(root, scan_app(root, cache, pypi, date(2025, 7, 31)))
    assert sc("sync", root, "--yes")[0] == 0  # keeps the [type-checked] note of Client.send
    code, out = explain(sc, root, "Client.send")
    assert code == 0, out
    assert "Note:\n- Call `Client().send(q)`. [type-checked]\n" in out
    assert "Written in: AGENTS.md ([type-checked])." in out
    assert "its example and the type checker's verdict are in .since-cutoff/results.json" in out
    assert "It stands in for the note from the diff:\n- `Client.send()` no longer accepts" in out
    assert "Evidence [type-checked], and [diff] for the note from the diff:" in out
    assert "- [type-checked] an example that basedpyright accepts for the pinned version" in out
    assert "- example: type-checked against toylib 2.0 by `since-cutoff run`" in out
    assert "no example was type-checked" not in out
    code, out = explain(sc, root, "Client.send", "--json")
    [found] = json.loads(out)["found"]
    assert found["block_bullet"] == "Call `Client().send(q)`. [type-checked]"
    assert found["in_blocks"][0]["how"] == "type_checked"


def test_several_matches_say_so_and_too_many_list_their_names(
    sc, app, cache, pypi, monkeypatch
) -> None:
    scan = scan_app(app, cache, pypi, date(2025, 7, 31))
    found = candidates(scan)
    two = [f for f in found if f.api in ("Client.send", "Client.close")]
    out = lookup_lines(scan, Lookup("x", two, match=SUFFIX), {})
    assert (
        "2 APIs match 'x' by the end of the name: Client.send (toylib), Client.close"
        in "\n".join(out)
    )
    assert sum(line.startswith("## ") for line in out) == 2
    monkeypatch.setattr(explain_module, "MATCHES_SHOWN", 0)
    code, out = explain(sc, app, "send")
    assert code == 1
    assert (
        "1 APIs match 'send' by the end of the name: Client.send (toylib). Give one of them "
        "(exit code 1)." in out
    )
    code, out = explain(sc, app, "send", "--json")
    result = json.loads(out)
    assert code == 1 and result["truncated"] is True and result["found"] == []
    assert result["matches"] == [{"display": "Client.send", "package": "toylib"}]


def test_from_and_to_need_a_package(sc, app, capsys) -> None:
    assert cli.main(["explain", "fetch", str(app), "--from", "1.0"]) == 2
    assert "explain --from and --to need --package" in capsys.readouterr().err
    argv = ["explain", "fetch", str(app), "--package", "toylib", "--from", "2.0", "--to", "1.1"]
    assert cli.main(argv) == 1
    assert "toylib 1.1 is not newer than 2.0: there is no change to explain" in " ".join(
        capsys.readouterr().err.split()
    )


def test_options_go_before_or_after_the_path(sc, app, capsys) -> None:
    """Python 3.10's argparse took no option between the API and the path."""
    for argv in (
        ["explain", "Client.send", "--json", str(app)],
        ["explain", "Client.send", str(app), "--json"],
    ):
        assert cli.main(argv) == 0
        assert json.loads(capsys.readouterr().out)["found"]
    with pytest.raises(SystemExit):
        cli.main(["explain", "Client.send", str(app), "extra"])
    assert "unrecognized arguments: extra" in capsys.readouterr().err


def test_explain_without_notes_uses_the_model_given(sc, tmp_path) -> None:
    root = make_app(tmp_path)
    code, out = explain(sc, root, "Client.send", *SONNET)
    assert code == 0 and "Not in a notes block" not in out and "Written in" not in out
    assert not (root / "AGENTS.md").exists()


def test_the_mcp_tool_explains_too(app, cache: DiskCache, pypi) -> None:
    registry = ModelRegistry(cache, offline=True)
    tools = Tools(cache, registry=registry, pypi=pypi, today=date(2026, 9, 1))
    out = tools.explain_api("legacy_fetch", str(app))
    assert out.startswith("# legacy_fetch\n")
    assert "For claude-sonnet-4-5's training cutoff (2025-07-31)" in out  # the notes' model
    assert "[diff + library]" in out and "toylib 1.0 toylib/helpers.py" in out
    out = tools.explain_api("send", str(app), cutoff="2025-07")
    assert "## Client.send (toylib)" in out and "- main.py: old form" in out
    out = tools.explain_api(
        "fetch", str(app), package="toylib", from_version="1.1", to_version="2.1"
    )
    assert "given with to_version; compared from 1.1 (2025-09-01), given with from_version" in out
    with pytest.raises(Exception, match="pass `package`"):
        tools.explain_api("send", str(app), from_version="1.0")


def test_the_mcp_tool_over_the_protocol_reads_the_blocks_and_writes_nothing(
    app, cache: DiskCache, pypi
) -> None:
    import anyio
    from mcp import Client

    registry = ModelRegistry(cache, offline=True)
    server = build_server(Tools(cache, registry=registry, pypi=pypi, today=date(2026, 9, 1)))
    before = snapshot(app)

    async def main() -> None:
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool(
                "explain_api", {"api": "Client.send", "project_dir": str(app)}
            )
            assert not result.is_error
            text = result.content[0].text  # type: ignore[union-attr]
            assert "Written in: AGENTS.md (from the diff)." in text
            assert "(from the notes in AGENTS.md" not in text  # no progress lines in the text
            missing = await client.call_tool(
                "explain_api", {"api": "nothing.like.it", "project_dir": str(app)}
            )
            assert "No API named 'nothing.like.it'" in missing.content[0].text  # type: ignore[union-attr]

    anyio.run(main)
    assert snapshot(app) == before
