"""What a note from the API diff says, and on what evidence (0.4 review).

- a replacement, and the [library] tag, only where the library states one; advice is quoted,
  and where the pinned release says "deprecated without replacement", the note says so;
- "no replacement" says what was read (the deprecation text), right after what it is about;
- the bullet does not depend on which of the API's changes the code uses;
- a stubs package's note says what type checkers reject, not what the runtime does;
- quotes are whole sentences, a member replacement is ``Class.name``, nothing is said twice;
- the "Runtime:" line gives each file's lines, whatever the order of the changes;
- results.json lists every replacement of a used API.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from since_cutoff.apidiff import (
    DEPRECATED,
    HINT_WARNING,
    PARAM_KEYWORD_ONLY,
    PARAM_REMOVED,
    REMOVED,
    APIChange,
)
from since_cutoff.cache import DiskCache
from since_cutoff.notes import (
    EVIDENCE_LIBRARY,
    TAG_DIFF,
    TAG_LIBRARY,
    diff_note,
    render_block,
    runtime_text,
)
from since_cutoff.pypi import SourceTree
from since_cutoff.report import render_markdown, render_scan_markdown, to_json
from tests.conftest import FakePyPI, write_tree
from tests.test_ci import scan_app
from tests.test_notes_provenance import change, toy_diff

CUTOFF = date(2025, 7, 31)


def one_package_pypi(
    cache: DiskCache,
    tmp_path: Path,
    name: str,
    old: dict[str, str],
    new: dict[str, str],
    versions: tuple[str, str] = ("1.0", "2.0"),
) -> FakePyPI:
    """A fake PyPI with two releases of one package: ``versions[0]`` before the cutoff
    (2025-07-31), ``versions[1]`` after it."""
    import_name = next(iter(old)).split("/")[0]
    trees = {
        (name, v): SourceTree(name, v, write_tree(tmp_path / f"{name}-{v}", files), (import_name,))
        for v, files in zip(versions, (old, new), strict=True)
    }
    return FakePyPI(
        cache, {name: [(versions[0], "2025-01-10"), (versions[1], "2025-10-01")]}, trees
    )


def app(tmp_path: Path, requirement: str, code: str) -> Path:
    root = tmp_path / "app"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "app"\nversion = "0"\ndependencies = ["{requirement}"]\n'
    )
    (root / "main.py").write_text(code)
    return root


# ---------------------------------------------- huggingface-hub 0.34.3 -> 2.0.0
HUB_OLD = {
    "huggingface_hub/__init__.py": """
        from ._snapshot_download import snapshot_download
        from .file_download import hf_hub_download

        __all__ = ["hf_hub_download", "snapshot_download"]
    """,
    "huggingface_hub/file_download.py": """
        import warnings


        def hf_hub_download(
            repo_id: str,
            filename: str,
            *,
            force_download: bool = False,
            local_dir: str | None = None,
            local_dir_use_symlinks: bool | str = "auto",
            resume_download: bool | None = None,
            force_filename: str | None = None,
            proxies: dict | None = None,
        ) -> str:
            \"\"\"Download a file.\"\"\"
            if resume_download is not None:
                warnings.warn(
                    "`resume_download` is deprecated and will be removed in version 1.0.0. "
                    "Downloads always resume when possible. If you want to force a new download, "
                    "use `force_download=True`.",
                    FutureWarning,
                )
            if local_dir_use_symlinks != "auto":
                warnings.warn(
                    "`local_dir_use_symlinks` parameter is deprecated and will be ignored. The "
                    "process to download files to a local folder has been updated and do not rely "
                    "on symlinks anymore. You only need to pass a destination folder as`local_dir`."
                )
            return filename
    """,
    "huggingface_hub/_snapshot_download.py": """
        def snapshot_download(
            repo_id: str,
            *,
            local_dir_use_symlinks: bool | str = "auto",
            resume_download: bool | None = None,
            force_filename: str | None = None,
            proxies: dict | None = None,
        ) -> str:
            return repo_id
    """,
}
HUB_NEW = {
    "huggingface_hub/__init__.py": HUB_OLD["huggingface_hub/__init__.py"],
    "huggingface_hub/utils/__init__.py": "from ._validators import validate_hf_hub_args\n",
    "huggingface_hub/utils/_validators.py": """
        import warnings
        from functools import wraps


        def validate_hf_hub_args(fn):
            @wraps(fn)
            def _inner(*args, **kwargs):
                kwargs = smoothly_deprecate_legacy_arguments(fn.__name__, kwargs)
                return fn(*args, **kwargs)

            return _inner


        def smoothly_deprecate_legacy_arguments(fn_name: str, kwargs: dict) -> dict:
            \"\"\"Smoothly deprecate legacy arguments in the `huggingface_hub` codebase.

            List of deprecated arguments:
                - `proxies`:
                    To set up proxies, use the HTTP_PROXY environment variable.

                - `resume_download`: deprecated without replacement. Downloads always resume.
                - `force_filename`: deprecated without replacement. Filename is always the same.
                - `local_dir_use_symlinks`: deprecated without replacement. No symlinks anymore.
            \"\"\"
            new_kwargs = kwargs.copy()
            proxies = new_kwargs.pop("proxies", None)
            if proxies is not None:
                warnings.warn(f"The `proxies` argument is ignored in `{fn_name}`.")
            resume_download = new_kwargs.pop("resume_download", None)
            force_filename = new_kwargs.pop("force_filename", None)
            local_dir_use_symlinks = new_kwargs.pop("local_dir_use_symlinks", None)
            return new_kwargs
    """,
    "huggingface_hub/file_download.py": """
        from .utils import validate_hf_hub_args


        @validate_hf_hub_args
        def hf_hub_download(
            repo_id: str, filename: str, *, force_download: bool = False, local_dir: str | None = None
        ) -> str:
            \"\"\"Download a file.\"\"\"
            return filename
    """,
    "huggingface_hub/_snapshot_download.py": """
        from .utils import validate_hf_hub_args


        @validate_hf_hub_args
        def snapshot_download(repo_id: str) -> str:
            return repo_id
    """,
}
HUB_CODE = """
from huggingface_hub import hf_hub_download, snapshot_download

hf_hub_download("r", "f", resume_download=True, local_dir_use_symlinks=False)
snapshot_download("r", resume_download=True)
"""


def test_advice_is_no_replacement_and_the_pinned_release_says_there_is_none(
    tmp_path, cache
) -> None:
    """huggingface-hub 0.34.3's warnings advise `force_download=True` and `local_dir`; 2.0.0's
    ``_validators.py`` says ``resume_download``, ``force_filename`` and
    ``local_dir_use_symlinks`` are "deprecated without replacement". 0.4's first cut tagged
    ``hf_hub_download``'s note [diff + library], and gave `force_download` as the replacement
    for `resume_download` (it does the opposite) in results.json, the Markdown table and
    report.md, while ``snapshot_download``'s same parameters got [diff]."""
    pypi = one_package_pypi(
        cache, tmp_path, "huggingface-hub", HUB_OLD, HUB_NEW, ("0.34.3", "2.0.0")
    )
    root = app(tmp_path, "huggingface-hub==2.0.0", HUB_CODE)
    scan = scan_app(root, cache, pypi, CUTOFF)
    changes = scan.package("huggingface-hub").changes
    resume = next(
        c for c in changes if c.parameter == "resume_download" and c.name == "hf_hub_download"
    )
    assert (resume.still_handled_at or "").startswith("huggingface_hub/utils/_validators.py:")
    assert resume.still_handled_text is not None
    assert resume.still_handled_text.startswith("deprecated without replacement.")
    assert resume.hint_source == HINT_WARNING and resume.library_names == ["force_download"]

    used = {u.note.api: u for u in scan.used_apis()}
    download = used["huggingface_hub.hf_hub_download"]
    snapshot = used["huggingface_hub.snapshot_download"]
    for u in (download, snapshot):  # the same treatment for both
        assert u.note.tag_list == (TAG_DIFF,) and u.note.replacements == []
        assert "force_download" not in u.note.line and "`local_dir`" not in u.note.line
        assert (  # in the order of the old signature
            "huggingface-hub's deprecation text says there is no replacement for "
            "`local_dir_use_symlinks`, `resume_download` or `force_filename`. since-cutoff found "
            "no replacement for `proxies` in huggingface-hub's deprecation text. [diff]"
        ) in u.note.line
    data = json.loads(json.dumps(to_json(scan), default=str))
    for entry in data["used_apis"]:
        assert entry["replacement"] is None and entry["replacements"] == []
        assert entry["note"]["tags"] == ["diff"] and entry["note"]["checks"]["replacement"] is None
    markdown = render_scan_markdown(scan)
    assert "`force_download` for" not in markdown and "`local_dir` for" not in markdown
    assert "(named in the library's deprecation text)" not in markdown
    assert markdown.count("| none named |") == 2
    report = render_markdown(scan)
    assert "`force_download` for" not in report and "`local_dir` for" not in report


def test_a_name_in_an_unrelated_sentence_is_no_replacement() -> None:
    """ "This argument is ignored when `token` is set." names ``token``, which exists in the new
    version: not a replacement for ``proxies``, and no [library] tag."""
    proxies = change(
        param="proxies",
        hint="This argument is ignored when `token` is set.",
        library_names=["token"],
    )
    note = diff_note([proxies])
    assert TAG_LIBRARY not in note.tag_list and note.replacements == []
    assert "Use `token`" not in note.line


def test_a_stated_replacement_is_still_one() -> None:
    gone = change(
        param="stop_sequences",
        hint="Deprecated argument. Use `stop` instead.",
        library_names=["stop"],
    )
    note = diff_note([gone])
    assert note.tag_list == (TAG_DIFF, TAG_LIBRARY)
    assert "Use `stop` instead of `stop_sequences`." in note.line
    assert [r.evidence for r in note.replacements] == [EVIDENCE_LIBRARY]


# ------------------------------------------------- "no replacement", said precisely
def test_no_replacement_follows_what_it_is_about() -> None:
    """toylib's ``Client.send`` lost ``temperature`` and made ``stream`` keyword-only: "names
    no replacement" came last, and read as if about ``stream``; and "toylib's source names no
    replacement" claimed more than was read (numpy's source names ``np.float64`` for
    ``np.float_`` in a table since-cutoff does not read)."""
    note = diff_note(
        [
            change(param="temperature"),
            change(kind=PARAM_KEYWORD_ONLY, param="stream"),
        ]
    )
    assert note.bullet == (
        "`Messages.create()` no longer accepts `temperature`; do not pass it. since-cutoff found "
        "no replacement in anthropic's deprecation text. Pass `stream` to `Messages.create()` "
        "by keyword."
    )
    assert "source names" not in note.bullet


ANTHROPIC_180_CREATE = (
    "create(self, *, max_tokens: int, messages: Iterable[MessageParam], model: ModelParam, "
    "stream: Literal[False] | Omit = omit, extra_headers: Headers | None = None, "
    "extra_query: Query | None = None, extra_body: Body | None = None, "
    "timeout: float | httpx.Timeout | None | NotGiven = not_given) -> Message"
)


def test_a_removed_request_field_points_to_extra_body_not_to_dropping_it() -> None:
    """anthropic 1.8.0 took ``temperature``, ``top_k`` and ``top_p`` out of ``create()``'s
    signature. "do not pass them" made every agent in the benchmark pilot drop the temperature
    the task asked for; the pinned method still sends fields through ``extra_body``."""
    changes = [
        change(param=p, new_signature=ANTHROPIC_180_CREATE)
        for p in ("temperature", "top_k", "top_p")
    ]
    note = diff_note(changes)
    assert note.line == (
        "`Messages.create()` no longer accepts `temperature`, `top_k` or `top_p` as keyword "
        "arguments. If the API still needs them, pass them through its `extra_body` or "
        "`extra_query` argument. since-cutoff found no replacement in anthropic's deprecation "
        "text. [diff]"
    )
    assert "do not pass" not in note.line
    one = diff_note([change(param="temperature", new_signature=ANTHROPIC_180_CREATE)])
    assert one.line.startswith(
        "`Messages.create()` no longer accepts `temperature` as a keyword argument. If the API "
        "still needs it, pass it through its `extra_body` or `extra_query` argument."
    )


def test_the_diff_records_request_extras_from_every_parameter(tmp_path: Path) -> None:
    """The recorded signature is cut at 400 characters, and anthropic 1.8's ``create()`` is
    longer: ``extra_body`` was past the cut, so the note fell back to "do not pass them"."""
    many = ", ".join(f"option_{i}: int | None = None" for i in range(40))
    old = {
        "sdk/__init__.py": "from sdk.client import Client\n",
        "sdk/client.py": (
            "class Client:\n"
            "    def create(self, *, model: str, temperature: float | None = None) -> str:\n"
            "        return model\n"
            "    def send(self, *, text: str, retries: int = 0) -> str:\n"
            "        return text\n"
        ),
    }
    new = {
        "sdk/__init__.py": "from sdk.client import Client\n",
        "sdk/client.py": (
            "class Client:\n"
            f"    def create(self, *, model: str, {many}, extra_query: dict | None = None,\n"
            "               extra_body: dict | None = None) -> str:\n"
            "        return model\n"
            "    def send(self, *, text: str) -> str:\n"
            "        return text\n"
        ),
    }
    found = {c.parameter: c for c in toy_diff(tmp_path, "sdk", old, new) if c.kind == PARAM_REMOVED}
    create, send = found["temperature"], found["retries"]
    assert "extra_body" not in (create.new_signature or "")  # past the cut
    assert create.request_extras == ["extra_body", "extra_query"] and send.request_extras == []
    assert "pass it through its `extra_body` or `extra_query` argument" in diff_note([create]).line
    assert "do not pass it" in diff_note([send]).line


def test_extra_request_arguments_are_read_from_the_pinned_signature_only() -> None:
    query_only = "list(self, *, limit: int | Omit = omit, extra_query: Query | None = None)"
    note = diff_note(
        [change(name="list", owner="Files", param="after_id", new_signature=query_only)]
    )
    assert "pass it through its `extra_query` argument." in note.line
    for signature in (
        None,
        "create(self, *, model, my_extra_body=None, extra_bodyguard=None)",  # look-alikes
        "create(self, *, model, **extra_body)",  # a **kwargs takes the parameter; not reported
    ):
        plain = diff_note([change(param="temperature", new_signature=signature)])
        assert plain.line.startswith(
            "`Messages.create()` no longer accepts `temperature`; do not pass it."
        )
    stubs = diff_note(
        [change(pkg="anthropic-stubs", param="temperature", new_signature=ANTHROPIC_180_CREATE)]
    )
    assert "extra_body" not in stubs.line and "type checkers reject it" in stubs.line


def test_no_replacement_names_what_it_is_about_when_others_have_one() -> None:
    note = diff_note(
        [
            change(param="stop_sequences", hint="Use `stop` instead.", library_names=["stop"]),
            change(param="best_of"),
        ]
    )
    assert note.bullet.endswith(
        "Use `stop` instead of `stop_sequences`. since-cutoff found no replacement for `best_of` "
        "in anthropic's deprecation text."
    )


# ------------------------------------------------- the same bullet, whatever the code uses
def test_the_bullet_and_the_block_do_not_depend_on_the_order_of_the_changes() -> None:
    """The reports list what the code passes first; the note did too, so a new
    ``hf_hub_download(proxies=...)`` call reworded the bullet, and `sync --check` and the
    pre-commit hook failed on unchanged facts."""
    signature = "create(self, *, model, temperature=None, top_p=None, top_k=None, stream=False)"
    changes = [change(param=p, old_signature=signature) for p in ("top_k", "temperature", "top_p")]
    lines = {diff_note(order).line for order in (changes, changes[::-1], changes[1:] + changes[:1])}
    assert lines == {
        "`Messages.create()` no longer accepts `temperature`, `top_p` or `top_k`; do not pass "
        "them. since-cutoff found no replacement in anthropic's deprecation text. [diff]"
    }  # in the old signature's order
    fetch = change(kind=REMOVED, name="Legacy", owner=None, param=None, path="anthropic.Legacy")
    one = render_block(
        [diff_note(changes), diff_note([fetch])], model="m", cutoff=CUTOFF, version_source="uv.lock"
    )
    other = render_block(
        [diff_note([fetch]), diff_note(changes[::-1])],
        model="m",
        cutoff=CUTOFF,
        version_source="uv.lock",
    )
    assert one == other


# ---------------------------------------------------------------- stubs packages
def test_a_stubs_package_says_what_type_checkers_reject() -> None:
    """pandas-stubs 2.3.3 dropped ``delim_whitespace`` and ``verbose`` from ``read_csv``;
    pandas 2.3.3 still accepts them (with a FutureWarning)."""
    params = [
        change(
            pkg="pandas-stubs",
            name="read_csv",
            owner=None,
            param=p,
            path="pandas.io.parsers.readers.read_csv",
            import_paths=["pandas.read_csv", "pandas.io.parsers.readers.read_csv"],
        )
        for p in ("delim_whitespace", "verbose")
    ]
    note = diff_note(params)
    assert note.line.startswith(
        "pandas-stubs no longer declares `delim_whitespace` or `verbose` for `pandas.read_csv()`; "
        "type checkers reject them."
    )
    assert "no longer accepts" not in note.line
    removed = change(
        pkg="types-requests",
        kind=REMOVED,
        name="old",
        owner=None,
        param=None,
        path="requests.old",
    )
    assert diff_note([removed]).line.startswith(
        "types-requests no longer declares `requests.old`; type checkers reject it."
    )


# ------------------------------------------------------------------- quotes
def test_a_quote_is_whole_sentences_or_none() -> None:
    """pydantic's "The 'merge_field_infos()' method is deprecated and will be removed in a
    future version. If you relied on this method, ..." was cut at 120 characters mid-sentence;
    both sentences say nothing the bullet does not (or ask to open an issue)."""
    merge = change(
        kind=DEPRECATED,
        pkg="pydantic",
        name="merge_field_infos",
        owner="FieldInfo",
        param=None,
        deprecation=(
            "The 'merge_field_infos()' method is deprecated and will be removed in a future "
            "version. If you relied on this method, please open an issue in the Pydantic issue "
            "tracker."
        ),
    )
    line = diff_note([merge]).line
    assert line == "`FieldInfo.merge_field_infos` is deprecated; avoid it in new code. [diff]"
    long = change(
        kind=DEPRECATED,
        param=None,
        deprecation=(
            "Use the streaming helpers. "
            + "They handle retries, timeouts and partial results for you in every case. " * 3
        ),
    )
    line = diff_note([long]).line
    assert '"Use the streaming helpers.' in line and "..." not in line
    assert line.endswith('for you in every case." [diff]')


def test_a_member_replacement_is_named_as_the_note_names_a_method() -> None:
    predict = change(
        kind=REMOVED,
        pkg="langchain-core",
        name="predict",
        owner="BaseChatModel",
        param=None,
        hint="Use invoke instead.",
        library_names=["langchain_core.language_models.chat_models.BaseChatModel.invoke"],
    )
    note = diff_note([predict])
    assert "Use `BaseChatModel.invoke` instead." in note.line
    assert note.replacements[0].text == (
        "langchain_core.language_models.chat_models.BaseChatModel.invoke"
    )


def test_advice_about_the_api_itself_does_not_repeat_its_name() -> None:
    """langgraph: "`langgraph.graph.MessageGraph` is deprecated; ... On
    `langgraph.graph.MessageGraph`, langgraph 1.2.12 says: ..." repeated the subject."""
    graph = change(
        kind=DEPRECATED,
        pkg="langgraph",
        name="MessageGraph",
        owner=None,
        param=None,
        path="langgraph.graph.MessageGraph",
        deprecation="Please use StateGraph with a 'messages' key instead.",
        library_names=["langgraph.graph.StateGraph"],
        new="1.2.12",
    )
    line = diff_note([graph]).line
    assert line == (
        "`langgraph.graph.MessageGraph` is deprecated; avoid it in new code. langgraph 1.2.12 "
        "says: \"Please use StateGraph with a 'messages' key instead.\" [diff]"
    )


def test_the_same_sentence_is_said_once() -> None:
    """transformers: one API deprecated under several paths said "is deprecated; avoid it in
    new code" three times, with the same quote each time."""
    same = [
        change(
            kind=DEPRECATED,
            name="Proc",
            owner=None,
            param=None,
            path=f"anthropic.m{i}.Proc",
            deprecation="Use `Other` in new code: it is faster.",
        )
        for i in range(3)
    ]
    line = diff_note(same).line
    assert line.count("is deprecated; avoid it in new code.") == 1
    assert line.count("it is faster") == 1


# ------------------------------------------------------------------- runtime
def test_the_runtime_line_gives_every_line_whatever_the_order() -> None:
    def handled(p: str, at: str) -> APIChange:
        return change(pkg="huggingface-hub", param=p, new="2.0.0", still_handled_at=at)

    changes = [
        handled("force_filename", "huggingface_hub/utils/_validators.py:195"),
        handled("proxies", "huggingface_hub/utils/_validators.py:178"),
        handled("resume_download", "huggingface_hub/utils/_validators.py:187"),
        handled("local_dir_use_symlinks", "huggingface_hub/utils/_validators.py:203"),
    ]
    texts = {runtime_text(order) for order in (changes, changes[::-1], changes[1:] + changes[:1])}
    assert texts == {
        "2.0.0's source still handles `force_filename`, `local_dir_use_symlinks`, `proxies` and "
        "`resume_download` (huggingface_hub/utils/_validators.py:178-203), so calls passing them "
        "may run with a warning; type checkers reject them."
    }
    elsewhere = [changes[0], handled("token", "huggingface_hub/other.py:12")]
    assert "(huggingface_hub/other.py:12, huggingface_hub/utils/_validators.py:195)" in (
        runtime_text(elsewhere) or ""
    )


# ------------------------------------------------------- results.json replacements
DL_OLD = {
    "dllib/__init__.py": """
        def download(url: str, *, timeout: float = 1.0, timeout_s: float | None = None,
                     retries: int = 3, retries_count: int | None = None) -> bytes:
            \"\"\"Download.

            Args:
                timeout_s: Deprecated. Use `timeout` instead.
                retries_count: Deprecated. Use `retries` instead.
            \"\"\"
            return b""
    """
}
DL_NEW = {
    "dllib/__init__.py": """
        def download(url: str, *, timeout: float = 1.0, retries: int = 3) -> bytes:
            return b""
    """
}


def test_json_lists_every_replacement_of_a_used_api(tmp_path, cache) -> None:
    """``used_apis[].replacement`` is the first (the plan's schema); ``replacements`` has
    them all, as the Markdown table does."""
    pypi = one_package_pypi(cache, tmp_path, "dllib", DL_OLD, DL_NEW)
    root = app(
        tmp_path,
        "dllib==2.0",
        "from dllib import download\ndownload('u', timeout_s=1, retries_count=2)\n",
    )
    scan = scan_app(root, cache, pypi, CUTOFF)
    [entry] = to_json(scan)["used_apis"]
    texts: list[Any] = [(r["text"], r["replaces"]) for r in entry["replacements"]]
    assert texts == [("timeout", "timeout_s"), ("retries", "retries_count")]  # signature order
    assert entry["replacement"] == entry["replacements"][0]
    assert entry["note"]["replacements"] == entry["replacements"]
    assert "`retries` for `retries_count`" in render_scan_markdown(scan)
    assert "`timeout` for `timeout_s`" in render_scan_markdown(scan)


def test_results_json_lists_a_few_import_paths_per_change(tmp_path, cache, fake_pypi) -> None:
    """A method of transformers' PreTrainedModel had 6,000 import paths, each in results.json
    (26 MB for ai-stack). They are all in the diff cache and used to match; results.json
    lists the shortest 5 and how many there are."""
    root = app(tmp_path, "toylib==2.0", "from toylib import fetch\nfetch('u')\n")
    scan = scan_app(root, cache, fake_pypi, CUTOFF)
    fetch = next(c for c in scan.package("toylib").changes if c.name == "fetch")
    fetch.import_paths = [f"toylib.m{i}.fetch" for i in range(40)]
    data = to_json(scan)
    [entry] = [c for c in data["scan"][0]["changes"] if c["name"] == "fetch"]
    assert entry["import_paths"] == [f"toylib.m{i}.fetch" for i in range(5)]
    assert entry["import_paths_total"] == 40
    assert len(fetch.import_paths) == 40  # the change itself keeps them all
