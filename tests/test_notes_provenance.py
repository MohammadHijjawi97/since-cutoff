"""Notes from the API diff, with their source and versions (0.4.0, plan A.3).

No model is called: the notes come from the diff alone. Each carries a tag saying what was
checked, a replacement is named only with evidence, similar names are never written into the
notes by default, and the block (format 2) has a meta line and a header saying what the tags mean.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from since_cutoff import cli, mcp_server, report
from since_cutoff.apidiff import (
    DEPRECATED,
    DIFF_SCHEMA,
    HINT_PARAM_DOC,
    HINT_WARNING,
    MOVED,
    PARAM_KEYWORD_ONLY,
    PARAM_REMOVED,
    PARAM_REQUIRED,
    REMOVED,
    APIChange,
    diff_sources,
    stated_names,
)
from since_cutoff.baselines import template_notes
from since_cutoff.cache import DiskCache, stable_hash
from since_cutoff.engine import Engine, PackageScan, Settings
from since_cutoff.notes import (
    BLOCK_END,
    BLOCK_START,
    EVIDENCE_LIBRARY,
    EVIDENCE_MOVE,
    EVIDENCE_RENAME,
    NOTE_DIFF,
    SCOPE_FAILURES,
    TAG_DIFF,
    TAG_LIBRARY,
    TAG_MOVE_CHECKED,
    TAG_NOT_CONFIRMED,
    TAG_PROBABLE_RENAME,
    TAG_TYPE_CHECKED,
    Note,
    apply_block,
    body_hash,
    deps_hash,
    diff_note,
    diff_notes,
    parse_block,
    remove_block,
    render_block,
    render_legacy_block,
    runtime_text,
    split_tags,
    tag_text,
)
from since_cutoff.project import load_project
from since_cutoff.report import render_markdown, render_scan, to_json
from since_cutoff.stats import estimate_tokens
from tests.conftest import FakePyPI, ScriptedModel, write_tree
from tests.test_engine import make_engine

CUTOFF = date(2025, 7, 31)
SRC = Path(__file__).resolve().parents[1] / "src" / "since_cutoff"


def change(
    kind: str = PARAM_REMOVED,
    name: str = "create",
    owner: str | None = "Messages",
    param: str | None = "temperature",
    pkg: str = "anthropic",
    **kw: Any,
) -> APIChange:
    """A change as DIFF_SCHEMA 13 records it (``library_names`` is [] unless given)."""
    kw.setdefault("library_names", [])
    path = kw.pop("path", None) or (
        f"{pkg}.resources.messages.messages.{owner}.{name}" if owner else f"{pkg}.{name}"
    )
    return APIChange(
        package=pkg,
        from_version=kw.pop("old", "0.60.0"),
        to_version=kw.pop("new", "1.8.0"),
        kind=kind,
        path=path,
        name=name,
        owner=owner,
        parameter=param,
        **kw,
    )


def sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def toy_diff(tmp_path: Path, package: str, old: dict[str, str], new: dict[str, str], **kw: str):
    """Diff two toy releases of ``package`` (import name: its first module's top folder)."""
    import_name = next(iter(old)).split("/")[0]
    a = write_tree(tmp_path / f"{package}-old", old)
    b = write_tree(tmp_path / f"{package}-new", new)
    versions = (kw.get("old_version", "1.0"), kw.get("new_version", "2.0"))
    found = diff_sources(package, versions[0], a, versions[1], b, [import_name])
    return [APIChange.from_dict(d) for d in found]


# --------------------------------------------------- 1. golden block (stale-demo)
GOLDEN_BODY = """\
## Library changes after the model's training cutoff

Changed after the training cutoff of `claude-sonnet-4-5` (2025-07-31, comparing from releases up \
to 2025-07-01) and used by this project \
at the versions in `uv.lock`, according to [since-cutoff](https://github.com/MohammadHijjawi97/\
since-cutoff) 0.4.0. Tags: [diff] a static comparison of the two releases' public APIs; [library] \
the replacement is named in the library's own deprecation text and exists in the pinned version; \
[type-checked] an example that basedpyright accepts for the pinned version. No library code was \
run. Where these lines conflict with what you remember, follow these lines.

**anthropic 1.8.0** (0.60.0 at the cutoff)
- `Messages.create()` no longer accepts `temperature`, `top_k` or `top_p`; do not pass them. \
since-cutoff found no replacement in anthropic's deprecation text. [diff]
"""


def test_golden_block_for_the_stale_demo() -> None:
    """The plan's block for examples/stale-demo (anthropic 0.60.0 -> 1.8.0), byte for byte.

    anthropic 0.60.0 and 1.8.0 carry no deprecation text for ``temperature``, so the note names
    no replacement and suggests nothing, and says what was read: the deprecation text, not the
    whole source. Parameters come in an order that does not depend on the code (their place in
    the old signature, then their names), whichever the code passes first.
    """
    changes = [change(param=p) for p in ("temperature", "top_p", "top_k")]
    note = diff_note(changes)
    assert note.tag_list == (TAG_DIFF,) and note.replacements == [] and note.not_confirmed == []
    block = render_block(
        [note],
        model="claude-sonnet-4-5",
        cutoff=CUTOFF,
        version_source="uv.lock",
        deps=deps_hash([("anthropic", "1.8.0")]),
        tool="0.4.0",
    )
    meta = {
        "v": 2,
        "tool": "0.4.0",
        "model": "claude-sonnet-4-5",
        "cutoff": "2025-07-31",
        "margin": 30,
        "versions_from": "uv.lock",
        "deps": sha("anthropic==1.8.0"),
        "body": sha(GOLDEN_BODY),
        "scope": "used",
    }
    meta_line = "<!-- since-cutoff:meta " + json.dumps(meta, separators=(",", ":")) + " -->"
    assert block == f"{BLOCK_START}\n{meta_line}\n{GOLDEN_BODY}{BLOCK_END}\n"


def test_one_bullet_per_api_with_all_its_changes() -> None:
    """Several changes to one callable, its async twin included, make one bullet (#5)."""
    changes = [
        change(param="temperature"),
        change(owner="AsyncMessages", param="top_p"),
        change(kind=PARAM_REQUIRED, param="max_tokens"),
        change(kind=PARAM_KEYWORD_ONLY, param="model"),
        change(name="stream", param="timeout"),  # another API
    ]
    notes = diff_notes(changes)
    assert [n.api for n in notes] == ["Messages.create", "Messages.stream"]
    assert notes[0].bullet == (
        "`Messages.create()` no longer accepts `temperature` or `top_p`; do not pass them. "
        "since-cutoff found no replacement in anthropic's deprecation text. `Messages.create()` "
        "now requires `max_tokens`. Pass `model` to `Messages.create()` by keyword."
    )
    assert [c.parameter for c in notes[0].covered] == [
        "temperature",
        "top_p",
        "max_tokens",
        "model",
    ]


# ------------------------------------------------------ 2. hf01: [diff + library]
HUB_1_21 = {
    "huggingface_hub/__init__.py": """
        from .inference._client import InferenceClient

        __all__ = ["InferenceClient"]
    """,
    "huggingface_hub/inference/__init__.py": "",
    "huggingface_hub/inference/_client.py": """
        import warnings


        class InferenceClient:
            def text_generation(
                self,
                prompt: str,
                *,
                stop: list[str] | None = None,
                stop_sequences: list[str] | None = None,
            ) -> str:
                \"\"\"Generate text.

                Args:
                    prompt (`str`):
                        Input text.
                    stop (`list[str]`, *optional*):
                        Stop generating tokens if a member of `stop` is generated.
                    stop_sequences (`list[str]`, *optional*):
                        Deprecated argument. Use `stop` instead.
                \"\"\"
                if stop_sequences is not None:
                    warnings.warn(
                        "`stop_sequences` is a deprecated argument for `text_generation` task and "
                        "will be removed in version '0.28.0'. Use `stop` instead.",
                        FutureWarning,
                    )
                return prompt
    """,
}
HUB_2_0 = {
    **HUB_1_21,
    "huggingface_hub/inference/_client.py": """
        class InferenceClient:
            def text_generation(self, prompt: str, *, stop: list[str] | None = None) -> str:
                \"\"\"Generate text.\"\"\"
                return prompt
    """,
}


def test_hf01_names_stop_from_the_librarys_own_text(tmp_path) -> None:
    """huggingface-hub 1.21.0 says "Deprecated argument. Use `stop` instead." of
    ``text_generation(stop_sequences=...)``, and 2.0.0 has ``stop``: the note names it, tagged
    [diff + library], with the file of 1.21.0 that says so."""
    changes = toy_diff(
        tmp_path, "huggingface-hub", HUB_1_21, HUB_2_0, old_version="1.21.0", new_version="2.0.0"
    )
    [gone] = [c for c in changes if c.parameter == "stop_sequences"]
    assert gone.library_names == ["stop"]
    assert (gone.hint_source, gone.hint_file) == (
        HINT_PARAM_DOC,
        "huggingface_hub/inference/_client.py",
    )
    note = diff_note([gone])
    assert note.line == (
        "`InferenceClient.text_generation()` no longer accepts `stop_sequences`; do not pass it. "
        "Use `stop` instead of `stop_sequences`. [diff + library]"
    )
    [replacement] = note.replacements
    assert replacement.to_dict() == {
        "text": "stop",
        "evidence": EVIDENCE_LIBRARY,
        "source": "huggingface-hub 1.21.0 huggingface_hub/inference/_client.py",
        "replaces": "stop_sequences",
        "exists_in_locked": True,
    }
    checks = note.checks()
    assert (
        checks["change"] == f"static API diff (griffe), 1.21.0 vs 2.0.0, diff schema {DIFF_SCHEMA}"
    )
    assert "exists in huggingface-hub 2.0.0" in checks["replacement"]
    assert checks["example_type_checks"] is None and checks["measured"] is None
    assert note.applies_to() == {
        "package": "huggingface-hub",
        "version": "2.0.0",
        "cutoff_version": "1.21.0",
    }


def test_a_name_the_text_gives_that_the_pinned_version_lacks_is_not_a_replacement() -> None:
    """``library_names`` holds only names that exist in the new version: none here, so the
    note says that no replacement was found in the library's deprecation text."""
    gone = change(param="stop_sequences", hint="Deprecated argument. Use `stop` instead.")
    note = diff_note([gone])
    assert note.tag_list == (TAG_DIFF,) and "`stop`" not in note.bullet
    assert note.bullet.endswith(
        "since-cutoff found no replacement in anthropic's deprecation text."
    )


def test_advice_is_quoted_not_stated_as_a_replacement() -> None:
    """hub 0.34.3's warning for ``resume_download`` names ``force_download`` as advice ("If you
    want to force a new download, use ..."), not as the replacement: the note quotes it."""
    resume = change(
        pkg="huggingface-hub",
        name="hf_hub_download",
        owner=None,
        param="resume_download",
        path="huggingface_hub.file_download.hf_hub_download",
        import_paths=[
            "huggingface_hub.hf_hub_download",
            "huggingface_hub.file_download.hf_hub_download",
        ],
        old="0.34.3",
        new="2.0.0",
        hint=(
            "`resume_download` is deprecated and will be removed in version 1.0.0. Downloads "
            "always resume when possible. If you want to force a new download, use "
            "`force_download=True`."
        ),
        hint_source=HINT_WARNING,
        hint_file="huggingface_hub/file_download.py",
        library_names=["force_download"],
        still_handled_at="huggingface_hub/utils/_validators.py:187",
    )
    proxies = change(
        pkg="huggingface-hub",
        name="hf_hub_download",
        owner=None,
        param="proxies",
        path=resume.path,
        old="0.34.3",
        new="2.0.0",
        still_handled_at="huggingface_hub/utils/_validators.py:178",
    )
    note = diff_note([resume, proxies])
    # Advice: quoted, and neither a replacement nor [library], whose legend says "the
    # replacement is named in the library's own deprecation text".
    assert note.line == (
        "`huggingface_hub.hf_hub_download()` no longer accepts `proxies` or `resume_download`; "
        'do not pass them. On `resume_download`, huggingface-hub 0.34.3 said: "Downloads always '
        'resume when possible. If you want to force a new download, use `force_download=True`." '
        "since-cutoff found no replacement in huggingface-hub's deprecation text. [diff]"
    )
    assert note.replacements == [] and note.checks()["replacement"] is None
    # The runtime caveat is shown next to the note, never written into it.
    assert "Runtime" not in note.line and "_validators" not in note.line
    assert runtime_text(note.covered) == (
        "2.0.0's source still handles `proxies` and `resume_download` "
        "(huggingface_hub/utils/_validators.py:178-187), so calls passing them may run with a "
        "warning; type checkers reject them."
    )
    assert "still handles `proxies` and `resume_download`" in note.checks()["runtime"]


def test_a_deprecation_message_of_the_pinned_version_names_the_replacement(tmp_path) -> None:
    """toylib 2.0's ``@deprecated("Use Client.shutdown() instead.")``: the replacement exists in
    2.0, and its source is 2.0's file."""
    old = {
        "toylib/__init__.py": "from toylib._client import Client\n",
        "toylib/_client.py": """
        class Client:
            def close(self) -> None: ...
    """,
    }
    new = {
        "toylib/__init__.py": old["toylib/__init__.py"],
        "toylib/_client.py": """
        from typing_extensions import deprecated


        class Client:
            @deprecated("Use Client.shutdown() instead.")
            def close(self) -> None: ...

            def shutdown(self) -> None: ...
    """,
    }
    [closing] = [c for c in toy_diff(tmp_path, "toylib", old, new) if c.kind == DEPRECATED]
    assert closing.library_names == ["toylib.Client.shutdown"]
    assert closing.deprecation_file == "toylib/_client.py"
    note = diff_note([closing])
    assert note.line == (
        "`Client.close` is deprecated; avoid it in new code. Use `Client.shutdown` instead. "
        "[diff + library]"
    )
    # Named as the note names a method; the replacement keeps its full path (results.json).
    assert note.replacements[0].text == "toylib.Client.shutdown"
    assert note.replacements[0].source == "toylib 2.0 toylib/_client.py"


def test_only_names_written_as_code_or_stated_as_the_replacement_count() -> None:
    """A plain word of a deprecation text that happens to name a function ("login") is no
    evidence; one the text states as the replacement ("Use fetch instead.") is."""
    assert stated_names(".. deprecated:: 1.0 Use fetch instead.") == ["fetch"]
    assert stated_names("Replaced by `Client.send`; renamed to receive in 2.0") == [
        "Client.send",
        "receive",
    ]
    assert stated_names("Please login first. If needed, use `x=True`.") == []
    # Quotes write a name as code too, and a text may give alternatives (httpx 0.27).
    assert stated_names("Use 'proxy' or 'mounts' instead.") == ["proxy", "mounts"]
    assert stated_names('Use "Command" instead.') == ["Command"]
    assert stated_names("Use a, b, or c instead.") == ["a", "b", "c"]
    assert stated_names("Use x Or y instead.") == ["x", "y"]
    assert stated_names("It's deprecated; don't use it.") == []


# ------------------------------------------------ 3. evidence: move, rename, similar
def test_a_checked_move_says_where_to_import_it_from(tmp_path, toylib) -> None:
    v1, v2 = toylib
    found = diff_sources("toylib", "1.0", v1.root, "2.0", v2.root, ["toylib"])
    [moved] = [APIChange.from_dict(d) for d in found if d["kind"] == MOVED]
    assert moved.move_evidence == {"compared": "class", "kept": 1, "of": 1}
    note = diff_note([moved])
    assert note.line == (
        "`toylib.helpers.Session` moved to `toylib.Session`: import it with "
        "`from toylib import Session`. [diff + move checked]"
    )
    [r] = note.replacements
    assert (r.evidence, r.text, r.replaces) == (
        EVIDENCE_MOVE,
        "toylib.Session",
        "toylib.helpers.Session",
    )
    assert "keeps 1 of 1 public names" in r.source


RENAME_OLD = {
    "rl/__init__.py": """
    def parse(text: str, pos: int) -> str:
        return text


    def fetch(url: str, *, force_filename: str | None = None) -> bytes:
        return b""
"""
}
RENAME_NEW = {
    "rl/__init__.py": """
    def parse(text: str, start: int) -> str:
        return text


    def fetch(url: str, *, filename: str | None = None) -> bytes:
        return b""
"""
}


def test_a_parameter_renamed_in_place_is_a_labelled_guess(tmp_path) -> None:
    """``pos`` -> ``start`` at the same position with the same type: a probable rename. The
    keyword-only ``force_filename`` -> ``filename`` only looks similar: not confirmed, and not
    in the note."""
    changes = {c.parameter: c for c in toy_diff(tmp_path, "rl", RENAME_OLD, RENAME_NEW)}
    renamed, similar = changes["pos"], changes["force_filename"]
    assert (renamed.renamed, renamed.suggestions) == (True, ["start"])
    assert (similar.renamed, similar.suggestions) == (False, ["filename"])

    note = diff_note([renamed])
    assert note.line == (
        "`rl.parse()` no longer accepts `pos`; do not pass it. `pos` was probably renamed to "
        "`start` (same position and type). [diff; probable rename]"
    )
    assert note.replacements[0].evidence == EVIDENCE_RENAME

    note = diff_note([similar])
    assert "`filename`" not in note.line and note.tag_list == (TAG_DIFF,)
    assert note.not_confirmed == ["filename"]
    # Only when asked for (sync --suggestions), labelled as such.
    asked = diff_note([similar], suggestions=True)
    assert asked.line.endswith(
        "Similar names in 2.0, not confirmed as replacements: `filename`. [diff; not confirmed]"
    )
    assert asked.tag_list == (TAG_DIFF, TAG_NOT_CONFIRMED)


def _runner(flag: str, doc: str = "") -> dict[str, str]:
    return {
        "ck/__init__.py": f'''
    class CliRunner:
        """Runs commands.{doc}
        """

        def __init__(self, charset: str = "utf-8", {flag}: bool = True) -> None:
            pass
'''
    }


def test_a_parameter_the_docs_say_was_removed_is_no_rename(tmp_path) -> None:
    """click 8.2's ``CliRunner`` took ``catch_exceptions: bool = True`` where 8.1 took
    ``mix_stderr: bool = True``: the same position and type, but its docstring says the one was
    added and the other removed. Telling an agent to pass ``catch_exceptions`` instead would
    change what the test runner does."""
    added = (
        "\n\n        .. versionchanged:: 8.2\n            Added the ``catch_exceptions`` parameter."
    )
    removed = "\n\n        .. versionchanged:: 8.2\n            ``mix_stderr`` parameter has been removed."
    for doc in (added, removed, added + removed):
        [change] = toy_diff(
            tmp_path / str(len(doc)), "ck", _runner("mix_stderr"), _runner("catch_exceptions", doc)
        )
        assert (change.parameter, change.renamed, change.suggestions) == ("mix_stderr", False, [])
        assert "renamed" not in diff_note([change]).line
    # A note that names both says what replaces what, and a parameter's description is no
    # version note: neither is evidence against a rename.
    both = (
        "\n\n        .. versionchanged:: 8.2\n"
        "            ``mix_stderr`` was removed in favour of ``catch_exceptions``."
    )
    described = "\n\n        New ``catch_exceptions`` values are accepted."
    for name, doc in (("both", both), ("described", described), ("none", "")):
        [change] = toy_diff(
            tmp_path / name, "ck", _runner("mix_stderr"), _runner("catch_exceptions", doc)
        )
        assert (change.renamed, change.suggestions) == (True, ["catch_exceptions"]), name


WARNED_OLD = {
    "hx/__init__.py": """
    import warnings


    class Client:
        def __init__(self, proxy=None, mounts=None, transport=None, proxies=None, app=None):
            if proxies:
                message = (
                    "The 'proxies' argument is now deprecated."
                    " Use 'proxy' or 'mounts' instead."
                )
                warnings.warn(message, DeprecationWarning)
            if app:
                message = (
                    "The 'app' shortcut is now deprecated."
                    " Use the explicit style 'transport=WSGITransport(app=...)' instead."
                )
                warnings.warn(message, DeprecationWarning)
"""
}
WARNED_NEW = {
    "hx/__init__.py": """
    class WSGITransport:
        def __init__(self, app=None):
            pass


    class Client:
        def __init__(self, proxy=None, mounts=None, transport=None):
            pass
"""
}


def test_a_warning_message_built_before_its_warn_call_is_the_hint(tmp_path) -> None:
    """httpx 0.27 builds each message first (``message = (...)``) and warns with it (its
    ``Client.__init__``, with its own messages): each removed parameter gets the message
    assigned last before its own ``warn``, and the names it states are its replacements. httpx
    0.28's ``Client(proxies=...)`` broke a great deal of code written for 0.27."""
    changes = {c.parameter: c for c in toy_diff(tmp_path, "hx", WARNED_OLD, WARNED_NEW)}
    proxies, app = changes["proxies"], changes["app"]
    assert proxies.hint == (
        "The 'proxies' argument is now deprecated. Use 'proxy' or 'mounts' instead."
    )
    assert app.hint == (
        "The 'app' shortcut is now deprecated. Use the explicit style "
        "'transport=WSGITransport(app=...)' instead."
    )
    assert diff_note([proxies]).line == (
        "`Client()` no longer accepts `proxies`; do not pass it. Use `proxy` or `mounts` "
        "instead of `proxies`. [diff + library]"
    )
    # Advice, not a stated replacement: quoted, and the rest said as before.
    assert diff_note([app]).line == (
        '`Client()` no longer accepts `app`; do not pass it. On `app`, hx 1.0 said: "Use the '
        "explicit style 'transport=WSGITransport(app=...)' instead.\" since-cutoff found no "
        "replacement in hx's deprecation text. [diff]"
    )


FORCE_FILENAME = {
    "pkg": "huggingface-hub",
    "name": "hf_hub_download",
    "owner": None,
    "param": "force_filename",
    "path": "huggingface_hub.file_download.hf_hub_download",
    "old": "0.34.3",
    "new": "2.0.0",
    "suggestions": ["filename"],
}


def test_similar_names_are_labelled_in_the_reports_and_never_in_the_notes() -> None:
    gone = change(**FORCE_FILENAME)
    label = "similar parameters in 2.0.0, not confirmed as replacements: `filename`"
    assert label in report.change_text(gone)
    assert label in mcp_server.change_line(gone)
    assert "`filename`" not in diff_note([gone]).line


def test_no_similar_names_where_the_library_says_there_is_no_replacement() -> None:
    """hub says ``force_filename`` is "deprecated without replacement": `filename` is shown
    nowhere (0.3's MCP line said "similar parameters now: `filename`")."""
    gone = change(
        **FORCE_FILENAME, hint="The `force_filename` argument is deprecated without replacement."
    )
    for text in (
        report.change_text(gone),
        mcp_server.change_line(gone),
        diff_note([gone], suggestions=True).line,
        report._md_change(gone, ()),
    ):
        assert "`filename`" not in text and "similar" not in text.lower()
    assert diff_note([gone]).not_confirmed == []


# -------------------------------------------------- 5. reading the block back
def test_parse_block_reads_format_2_and_detects_hand_edits(tmp_path) -> None:
    notes = [
        diff_note([change(param="temperature")]),
        Note(
            change(pkg="toylib", name="send", owner="Client", old="1.0", new="2.0"),
            "Call `Client().send(q)`.",
            "x",
            True,
            "model",
            (TAG_TYPE_CHECKED,),
        ),
    ]
    block = render_block(notes, model="m", cutoff=CUTOFF, version_source="uv.lock")
    target = tmp_path / "AGENTS.md"
    target.write_bytes(b"# Rules\r\n\r\nBe nice.\r\n")
    apply_block(target, block)  # CRLF, as the file has
    parsed = parse_block(
        target.read_text(encoding="utf-8", newline="") if False else target.read_bytes().decode()
    )
    assert parsed is not None and parsed.version == 2 and parsed.edited is False
    assert parsed.meta["model"] == "m" and parsed.meta["scope"] == "used"
    assert parsed.packages["anthropic"].version == "1.8.0"
    assert parsed.packages["anthropic"].cutoff_version == "0.60.0"
    assert parsed.packages["toylib"].bullets == [("Call `Client().send(q)`.", (TAG_TYPE_CHECKED,))]
    assert parsed.packages["anthropic"].bullets[0][1] == (TAG_DIFF,)

    edited = target.read_bytes().decode().replace("Call `Client()", "Call `Client(key)")
    assert parse_block(edited).edited is True  # type: ignore[union-attr]
    broken = target.read_bytes().decode().replace('"v":2', '"v":2,,')
    assert parse_block(broken).edited is True  # type: ignore[union-attr]
    assert parse_block("# no block here\n") is None


def test_parse_block_reads_the_blocks_0_3_wrote() -> None:
    legacy = render_legacy_block(
        template_notes([change()]),
        model="claude-sonnet-4-5",
        cutoff=CUTOFF,
        version_source="uv.lock",
    )
    parsed = parse_block("intro\n\n" + legacy)
    assert parsed is not None and parsed.version == 1 and parsed.edited is None
    assert parsed.meta == {
        "v": 1,
        "model": "claude-sonnet-4-5",
        "cutoff": "2025-07-31",
        "versions_from": "uv.lock",
    }
    assert parsed.packages["anthropic"].version == "1.8.0"
    assert parsed.packages["anthropic"].bullets == [
        (
            "`Messages.create(temperature=...)`: `temperature` was removed in anthropic 1.8.0. Do "
            "not pass it.",
            (),
        )
    ]


# The markers as 0.3.1's notes._block_span looks for them: its unapply must still find (and
# remove) a block that 0.4 wrote.
V031_START = re.compile(r"(?m)^[ \t]*<!-- since-cutoff:start -->[ \t]*\r?$")
V031_END = re.compile(r"(?m)^[ \t]*<!-- since-cutoff:end -->[ \t]*\r?$")


def test_the_unapply_of_0_3_removes_a_format_2_block(tmp_path) -> None:
    block = render_block(
        [diff_note([change()])], model="m", cutoff=CUTOFF, version_source="uv.lock"
    )
    text = "# Rules\n" + "\n" + block
    assert len(V031_START.findall(text)) == 1 and len(V031_END.findall(text)) == 1
    assert block.splitlines()[0] == "<!-- since-cutoff:start -->"
    target = tmp_path / "AGENTS.md"
    target.write_text("# Rules\n", encoding="utf-8")
    apply_block(target, block)
    assert remove_block(target)
    assert target.read_text(encoding="utf-8") == "# Rules\n"


def test_tags_read_back_as_written() -> None:
    for tags in [
        (TAG_DIFF,),
        (TAG_DIFF, TAG_LIBRARY),
        (TAG_DIFF, TAG_MOVE_CHECKED),
        (TAG_DIFF, TAG_PROBABLE_RENAME),
        (TAG_DIFF, TAG_LIBRARY, TAG_PROBABLE_RENAME, TAG_NOT_CONFIRMED),
        (TAG_TYPE_CHECKED,),
    ]:
        assert split_tags(f"Do this. {tag_text(tags)}") == ("Do this.", tags)
    assert tag_text([TAG_DIFF, TAG_PROBABLE_RENAME]) == "[diff; probable rename]"
    assert split_tags("A bullet [with brackets] of its own.") == (
        "A bullet [with brackets] of its own.",
        (),
    )


# ------------------------------------------------------ 6. hostile package text
def test_package_text_cannot_close_the_block_or_smuggle_code() -> None:
    hostile = change(
        param="stop_sequences",
        hint="Deprecated. If you must, pass `stop` --> <!-- `rm -rf /` --> now.",
        library_names=["stop"],
        hint_file="pkg/-->evil.py",
    )
    moved_text = change(kind=DEPRECATED, param=None, deprecation="Gone --> `import os` <!--")
    block = render_block(
        [diff_note([hostile]), diff_note([moved_text])],
        model="m`--> <script>",
        cutoff=CUTOFF,
        version_source="uv.lock",
    )
    assert block.count("-->") == 3 and block.count("<!--") == 3  # start, meta, end
    assert "<script>" not in block.splitlines()[1]  # escaped in the meta line's JSON
    for line in block.splitlines():
        if line.startswith("- "):
            assert line.count("`") % 2 == 0, line
            assert "rm -rf" not in re.findall(r"`([^`]*)`", line)
    assert "`stop`" in block  # the one name the pinned version has, still as code
    parsed = parse_block(block)
    assert (
        parsed is not None and parsed.edited is False and parsed.meta["model"] == "m`--> <script>"
    )


# --------------------------------------------------------------- 7. size
def test_ten_notes_stay_within_the_token_budget() -> None:
    """0.3.2 wrote 10 notes in about 437 tokens (README); format 2's header, the plan's exact
    text with its meta line and tag legend, costs about 140 more: 10 notes stay under 600."""
    changes = [
        change(param=p)
        for p in ("temperature", "top_p", "top_k")  # one API, one note
    ] + [change(name=f"method{i}", owner="Client", param=f"arg{i}") for i in range(6)]
    changes += [
        change(
            name="text_generation",
            owner="InferenceClient",
            param="stop_sequences",
            hint="Deprecated argument. Use `stop` instead.",
            library_names=["stop"],
        ),
        change(kind=REMOVED, name="Legacy", owner=None, param=None, path="anthropic.Legacy"),
        change(
            kind=PARAM_REQUIRED, name="fetch", owner=None, param="timeout", path="anthropic.fetch"
        ),
    ]
    notes = diff_notes(changes)
    assert len(notes) == 10
    block = render_block(notes, model="claude-sonnet-4-5", cutoff=CUTOFF, version_source="uv.lock")
    assert estimate_tokens(block) < 600


# ---------------------------------------------------- 8. wording: "verified"
_VERIFIED = re.compile(r"\bverif(?:y|ied|ies|ying|ication)\b", re.IGNORECASE)
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


def _user_facing_strings(path: Path) -> list[tuple[int, str]]:
    """The string literals of a module that users read: every string that is not a docstring
    (docstrings are for developers), plus the MCP tools' docstrings, which the calling model
    reads as tool descriptions."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    tool_docs: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
        if isinstance(node, ast.ClassDef) and node.name == "Tools":
            for fn in node.body:
                if isinstance(fn, ast.FunctionDef) and not fn.name.startswith("_"):
                    doc = fn.body[0]
                    if isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant):
                        tool_docs.add(id(doc.value))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings and id(node) not in tool_docs:
                continue
            out.append((node.lineno, node.value))
    return out


def test_no_user_facing_string_says_verified_on_its_own() -> None:
    """since-cutoff says what was checked (a tag) instead of "verified". Identifiers such as
    the JSON keys ``verified`` and ``fixed_by_verified_only`` stay, for results.json."""
    found = []
    for path in sorted(SRC.glob("*.py")):
        for line, text in _user_facing_strings(path):
            if _IDENTIFIER.match(text):
                continue
            if _VERIFIED.search(text):
                found.append(f"{path.name}:{line}: {text[:80]!r}")
    assert found == []


def test_the_cli_description_and_help_say_what_is_checked() -> None:
    parser = cli.build_parser()
    assert parser.description == (
        "Find which dependency APIs your code uses changed after your coding model's training "
        "cutoff, and write short AGENTS.md notes from the API diff, each with its source."
    )
    assert "verif" not in parser.format_help().lower()
    pyproject = (SRC.parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert "verified" not in pyproject


# ---------------------------------------------------------- 9. baseline unchanged
TEMPLATE_BLOCK_0_3 = """\
<!-- since-cutoff:start -->
## Library changes after the model's training cutoff

Generated by [since-cutoff](https://github.com/MohammadHijjawi97/since-cutoff) for \
`claude-sonnet-4-5` (training cutoff 2025-07-31), checked against `uv.lock`. Prefer these over \
what you remember.

**anthropic 1.8.0**
- `Messages.create(temperature=...)`: `temperature` was removed in anthropic 1.8.0. Do not pass it.
- `anthropic.Legacy` no longer exists in anthropic 1.8.0 (Deprecated: use 'Anthropic' x ). Do not \
use it.

**toylib 2.0**
- `toylib.helpers.Session` moved: use `from toylib.sessions import Session` (toylib 2.0).
- `toylib.Client.close` is deprecated in toylib 2.0: Use Client.shutdown() instead. Avoid it in \
new code.
- `toylib.helpers.fetch(timeout=...)` now requires `timeout` in toylib 2.0.
<!-- since-cutoff:end -->
"""


def test_the_template_baseline_block_is_what_0_3_wrote() -> None:
    """``run --compare template`` stays byte-identical to 0.3.2 (its block made by 0.3.2's own
    code for these changes), so its numbers stay comparable with 0.3's."""
    changes = [
        change(),
        change(
            kind=REMOVED,
            name="Legacy",
            owner=None,
            param=None,
            path="anthropic.Legacy",
            hint="Deprecated: use `Anthropic` <!-- x -->",
            library_names=None,
        ),
        change(
            kind=MOVED,
            name="Session",
            owner=None,
            param=None,
            pkg="toylib",
            path="toylib.helpers.Session",
            moved_to="toylib.sessions.Session",
            old="1.0",
            new="2.0",
        ),
        change(
            kind=DEPRECATED,
            name="close",
            owner="Client",
            param=None,
            pkg="toylib",
            path="toylib.Client.close",
            deprecation="Use Client.shutdown() instead.",
            old="1.0",
            new="2.0",
        ),
        change(
            kind=PARAM_REQUIRED,
            name="fetch",
            owner=None,
            param="timeout",
            pkg="toylib",
            path="toylib.helpers.fetch",
            old="1.0",
            new="2.0",
        ),
    ]
    block = render_legacy_block(
        template_notes(changes), model="claude-sonnet-4-5", cutoff=CUTOFF, version_source="uv.lock"
    )
    assert block == TEMPLATE_BLOCK_0_3


# --------------------------------------------------------- the diff (schema 13)
def test_a_diff_cached_before_library_names_is_recomputed(cache: DiskCache, toylib) -> None:
    assert DIFF_SCHEMA >= 13
    v1, v2 = toylib
    pypi = FakePyPI(cache, {}, {("toylib", "1.0"): v1, ("toylib", "2.0"): v2})
    stale = change(
        kind=REMOVED,
        name="stale",
        owner=None,
        param=None,
        pkg="toylib",
        path="toylib.stale",
        old="1.0",
        new="2.0",
    ).to_dict()
    for key in ("library_names", "hint_file", "deprecation_file", "renamed"):
        del stale[key]  # as DIFF_SCHEMA 12 stored it
    cache.set("diffs", stable_hash("diff", 12, "toylib", "1.0", "2.0"), [stale])
    engine = Engine(Settings(), store=cache, llm_cache=DiskCache(cache.root / "llm"), pypi=pypi)
    scan = engine.diff_package(PackageScan("toylib", "2.0", "test", True, cutoff_version="1.0"))
    assert "stale" not in {c.name for c in scan.changes}
    assert scan.changes and all(c.library_names is not None for c in scan.changes)


def test_a_change_from_an_older_diff_still_gets_a_note() -> None:
    """Without ``library_names`` (a diff older than schema 13, or a change made by hand), the
    parameters of the new signature, as far as it goes, are what the text is checked against."""
    old = change(
        param="stop_sequences",
        hint="Deprecated argument. Use `stop` instead.",
        new_signature="text_generation(self, prompt: str, *, stop: list[str] | None = None)",
        library_names=None,
    )
    assert diff_note([old]).tag_list == (TAG_DIFF, TAG_LIBRARY)
    old.new_signature = "text_generation(self, prompt: str, *, details: bool = Fal..."
    assert diff_note([old]).tag_list == (TAG_DIFF,)


def test_the_warning_text_of_the_old_version_names_a_replacement(tmp_path) -> None:
    old = {
        "wl/__init__.py": """
        import warnings


        def download(url: str, resume_download: bool | None = None, force_download: bool = False) -> None:
            if resume_download is not None:
                warnings.warn(
                    "`resume_download` is deprecated. If you want to force a new download, "
                    "use `force_download=True`.",
                    FutureWarning,
                )
    """
    }
    new = {
        "wl/__init__.py": """
        def download(url: str, force_download: bool = False) -> None: ...
    """
    }
    [gone] = toy_diff(tmp_path, "wl", old, new)
    assert (gone.hint_source, gone.hint_file, gone.library_names) == (
        HINT_WARNING,
        "wl/__init__.py",
        ["force_download"],
    )


# ------------------------------------------------------- scan, report and JSON
def make_app(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0"\ndependencies = ["toylib==2.0"]\n'
    )
    (root / "main.py").write_text(
        "from toylib import Client, fetch, legacy_fetch\n"
        "Client().send('hi', temperature=0.2)\n"
        "fetch('u')\nlegacy_fetch('u')\n"
    )
    return root


@pytest.fixture
def app_scan(tmp_path, cache, fake_pypi):
    engine = make_engine(cache, fake_pypi, ScriptedModel())
    return engine.scan(load_project(make_app(tmp_path)), engine.resolve_target())


def test_scan_writes_a_note_for_each_changed_api_the_code_uses(app_scan) -> None:
    lines = [n.line for n in app_scan.diff_notes()]
    assert lines == [  # in the reports' order: what the code names exactly, hard breaks first
        "`toylib.legacy_fetch` was removed; do not use it. Use `toylib.fetch` instead. "
        "[diff + library]",
        # Both changes of send: one bullet.
        "`Client.send()` no longer accepts `temperature`; do not pass it. since-cutoff found no "
        "replacement in toylib's deprecation text. Pass `stream` to `Client.send()` by keyword. "
        "[diff]",
        "`toylib.fetch()` now requires `timeout`. [diff]",
    ]
    # Not used by the code: no note (Session, which it does not import; close, never called).
    assert not any("Session" in line or "close" in line for line in lines)


def test_json_has_the_used_apis_with_their_notes_and_a_preview_of_the_block(app_scan) -> None:
    data = json.loads(json.dumps(to_json(app_scan), default=str))
    legacy, send = data["used_apis"][:2]
    assert {k: send[k] for k in ("package", "cutoff_version", "locked", "versions_from")} == {
        "package": "toylib",
        "cutoff_version": "1.0",
        "locked": "2.0",
        "versions_from": "pyproject.toml",  # the file that pins it
    }
    assert (send["api"], send["display"], send["match"]) == (
        "toylib.Client.send",
        "Client.send",
        "path",
    )
    assert [{k: v for k, v in c.items() if k != "change_id"} for c in send["changes"]] == [
        {
            "kind": PARAM_REMOVED,
            "parameter": "temperature",
            "form": "old_form",  # main.py passes it
            "runtime": {"checked": False, "still_handled_at": None},
        },
        {
            "kind": PARAM_KEYWORD_ONLY,
            "parameter": "stream",
            "form": "uses_api",  # never the old form: a name match cannot tell
            "runtime": {"checked": False, "still_handled_at": None},
        },
    ]
    assert send["replacement"] is None
    assert send["note"]["tags"] == ["diff"] and send["note"]["tag_text"] == "[diff]"
    assert send["note"]["applies_to"] == {
        "package": "toylib",
        "version": "2.0",
        "cutoff_version": "1.0",
    }
    assert send["note"]["checks"]["runtime"] == "not checked"
    assert legacy["replacement"]["evidence"] == EVIDENCE_LIBRARY
    assert legacy["replacement"]["source"] == "toylib 1.0 toylib/helpers.py"
    preview = data["notes_preview"]
    assert preview["targets"] == ["AGENTS.md"] and preview["tokens"] == estimate_tokens(
        preview["block"]
    )
    parsed = parse_block(preview["block"])
    assert parsed is not None and parsed.edited is False and parsed.meta["scope"] == "used"
    assert parsed.meta["versions_from"] == "pyproject.toml"
    assert parsed.meta["deps"] == deps_hash([("toylib", "2.0")])


def test_report_and_console_show_the_notes_with_their_sources(app_scan) -> None:
    md = render_markdown(app_scan)
    assert "## Notes for AGENTS.md (with their sources)" in md
    assert (
        "- `toylib.legacy_fetch` [diff + library]: toylib 1.0 -> 2.0; stated from the API diff; "
        "`toylib.fetch` for `toylib.legacy_fetch`: toylib 1.0 toylib/helpers.py" in md
    )
    from rich.console import Console

    console = Console(width=200, record=True)
    render_scan(console, app_scan)  # each used API with its note under it
    text = console.export_text()
    assert "Your code uses 3 APIs that changed after scripted-1's training cutoff" in text
    assert "    Note: `toylib.fetch()` now requires `timeout`. [diff]" in text


def test_nothing_used_means_no_notes(tmp_path, cache, fake_pypi) -> None:
    root = make_app(tmp_path)
    (root / "main.py").write_text("import json\n")
    engine = make_engine(cache, fake_pypi, ScriptedModel())
    scan = engine.scan(load_project(root), engine.resolve_target())
    assert scan.diff_notes() == [] and scan.notes_block([]) is None
    data = to_json(scan)
    assert data["used_apis"] == [] and data["notes_preview"] is None
    assert "## Notes for AGENTS.md" not in render_markdown(scan)


def test_the_runtime_caveat_is_in_the_terminal_report_and_mcp_lines() -> None:
    gone = change(
        pkg="huggingface-hub",
        name="hf_hub_download",
        owner=None,
        param="resume_download",
        path="huggingface_hub.hf_hub_download",
        new="2.0.0",
        still_handled_at="huggingface_hub/utils/_validators.py:187",
    )
    caveat = (
        "2.0.0's source still handles `resume_download` (huggingface_hub/utils/_validators.py:"
        "187), so calls passing it may run with a warning; type checkers reject it"
    )
    assert f"\n  - Runtime: {caveat}." in report._md_change(gone, ())
    assert caveat in mcp_server.change_line(gone)


def test_cli_scan_prints_the_notes_and_json_has_them(
    tmp_path, capsys, monkeypatch, fake_pypi
) -> None:
    monkeypatch.setenv("SINCE_CUTOFF_CACHE", str(tmp_path / "cli-cache"))
    monkeypatch.setattr(
        cli, "Engine", lambda settings, **kw: Engine(settings, **{**kw, "pypi": fake_pypi})
    )
    root = make_app(tmp_path)
    argv = ["scan", str(root), "--model", "anthropic:claude-sonnet-4-5", "--cutoff", "2025-07-31"]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "Your code uses 3 APIs that changed after claude-sonnet-4-5's training cutoff" in out
    assert "    Note: `toylib.fetch()` now requires `timeout`. [diff]" in out
    assert cli.main([*argv, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["used_apis"]) == 3 and data["notes_preview"]["targets"] == ["AGENTS.md"]


# ------------------------------------------------------------------ run
@pytest.mark.pyright
def test_run_tags_its_notes_and_writes_format_2(tmp_path, cache, fake_pypi) -> None:
    """A run's model notes are [type-checked]; its block is format 2, scope "failures"; results
    carry each note's tags, versions, checks and what the held-out test measured."""
    engine = make_engine(
        cache, fake_pypi, ScriptedModel(knows={"session"}), max_probes=10, heldout=1, regression=0
    )
    scan = engine.scan(load_project(make_app(tmp_path)), engine.resolve_target())
    run = engine.run(scan)
    assert run.notes and all(n.tag_list == (TAG_TYPE_CHECKED,) for n in run.notes)
    assert all(n.writer == "scripted:scripted-1" for n in run.notes)
    parsed = parse_block(run.block or "")
    assert parsed is not None and parsed.meta["scope"] == SCOPE_FAILURES and parsed.edited is False
    assert all(
        line.endswith("[type-checked]")
        for line in (run.block or "").splitlines()
        if line.startswith("- ")
    )
    data = to_json(scan, run)
    detail = data["notes_detail"][0]
    assert detail["tags"] == ["type_checked"] and detail["verified"] is True
    assert detail["checks"]["example_type_checks"] is True
    assert detail["checks"]["measured"]["pairs"] == 1
    assert detail["applies_to"] == {"package": "toylib", "version": "2.0", "cutoff_version": "1.0"}
    assert data["notes"]["type_checked"] == data["notes"]["verified"] == len(run.notes)
    md = render_markdown(scan, run)
    assert "## Notes for AGENTS.md (with their sources)" in md
    assert "written by `scripted:scripted-1`; its example type-checks against toylib 2.0" in md


@pytest.mark.pyright
def test_run_apply_writes_a_block_that_0_3_can_remove(tmp_path, capsys, scripted_cli) -> None:
    root = make_app(tmp_path)
    (root / "CLAUDE.md").write_text("# Mine\n", encoding="utf-8")  # only CLAUDE.md: it is used
    argv = ["run", str(root), "--model", "anthropic:claude-sonnet-4-5", "--cutoff", "2025-07-31"]
    argv += ["--max-probes", "2", "--heldout", "1", "--regression", "0", "--apply"]
    scripted_cli.append(ScriptedModel())
    assert cli.main(argv) == 0
    text = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert not (root / "AGENTS.md").exists()
    assert "<!-- since-cutoff:meta " in text and "[type-checked]" in text
    assert V031_START.search(text) and V031_END.search(text)
    assert cli.main(["unapply", str(root)]) == 0
    assert (root / "CLAUDE.md").read_text(encoding="utf-8") == "# Mine\n"


def test_body_hash_ignores_line_endings() -> None:
    assert body_hash("a\r\nb\n") == body_hash("a\nb\n") != body_hash("a\nc\n")
    assert deps_hash([("b", "1"), ("a", "2")]) == deps_hash([("a", "2"), ("b", "1")])


def test_a_note_without_tags_reads_as_0_3_did() -> None:
    """A Note made the 0.3 way (no tags): a verified model note is [type-checked], anything
    else [diff]; and every note's JSON keeps the 0.3 keys."""
    model = Note(change(), "b", "x", True, "model")
    other = Note(change(), "b", None, False, NOTE_DIFF)
    assert model.tag_list == (TAG_TYPE_CHECKED,) and other.tag_list == (TAG_DIFF,)
    keys = {"change_id", "package", "bullet", "example", "verified", "source"}
    assert keys <= set(model.to_dict())
    with pytest.raises(ValueError):
        diff_note([])
