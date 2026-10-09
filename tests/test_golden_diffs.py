"""Golden API diffs of real release pairs: tests/fixtures/diffs, recorded from PyPI with
scripts/record_diff_fixtures.py.

Each pair here has breaking changes its own changelog documents, and the tests assert those,
plus what the source shows where the changelog says less, and the notes an agent gets. They
read the recorded diffs, so a change to the notes fails here. A change in since-cutoff's diff
or in griffe that shifts what a real pair reports fails the network test that diffs every pair
again from PyPI (test_dependency_switch.py, or ``record_diff_fixtures.py --check``). (mcp's
and openai's fixtures are read by test_ranking.py and test_dependency_switch.py.)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from since_cutoff.apidiff import (
    DEPRECATED,
    DIFF_SCHEMA,
    PARAM_REMOVED,
    PARAM_REQUIRED,
    REMOVED,
    APIChange,
)
from since_cutoff.notes import diff_note

FIXTURES = Path(__file__).parent / "fixtures" / "diffs"
# The fixtures ship in the sdist: a bigger diff keeps only some kinds (a fixture's "kinds").
MAX_FIXTURE_BYTES = 150 * 1024


def _changes(name: str) -> dict[tuple[str, str, str], APIChange]:
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    changes = map(APIChange.from_dict, data["changes"])
    return {(c.kind, c.path, c.parameter or ""): c for c in changes}


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_every_fixture_is_current_and_small(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == DIFF_SCHEMA, "re-record it: python scripts/record_diff_fixtures.py"
    assert path.stat().st_size <= MAX_FIXTURE_BYTES
    assert path.stem.startswith(f"{data['package']}-{data['from_version']}-{data['to_version']}")


HTTPX_FUNCTIONS = ("request", "stream", "get", "options", "head", "post", "put", "patch", "delete")


def test_httpx_0_28_removed_proxies_and_app() -> None:
    """httpx 0.28.0's changelog: "The deprecated `proxies` argument has now been removed." and
    "The deprecated `app` argument has now been removed." It calls `cert` deprecated: `Client`
    still takes it, with a warning, but the top-level functions no longer do."""
    changes = _changes("httpx-0.27.2-0.28.1")
    client = "httpx.Client.__init__"
    for parameter in ("proxies", "app"):
        change = changes[(PARAM_REMOVED, client, parameter)]
        assert change.also == ["httpx.AsyncClient.__init__"]
    for function in HTTPX_FUNCTIONS:
        assert (PARAM_REMOVED, f"httpx.{function}", "proxies") in changes
        assert (PARAM_REMOVED, f"httpx.{function}", "cert") in changes
    assert (PARAM_REMOVED, client, "cert") not in changes
    assert {kind for kind, _, _ in changes} == {PARAM_REMOVED}  # 0.28 removed no names
    # httpx 0.27.2's own warning names the replacements: "Use 'proxy' or 'mounts' instead."
    assert diff_note([changes[(PARAM_REMOVED, client, "proxies")]]).line == (
        "`Client()` no longer accepts `proxies`; do not pass it. Use `proxy` or `mounts` "
        "instead of `proxies`. [diff + library]"
    )
    # For `app` the sentence says more than the name ("Use the explicit style ..."): it is
    # quoted, and `WSGITransport` is the replacement it states.
    assert diff_note([changes[(PARAM_REMOVED, client, "app")]]).line == (
        "`Client()` no longer accepts `app`; do not pass it. On `app`, httpx 0.27.2 said: "
        "\"Use the explicit style 'transport=WSGITransport(app=...)' instead.\" [diff + library]"
    )


def test_click_8_2_removed_helpoption_and_mix_stderr_and_deprecated_the_old_parser() -> None:
    """click 8.2.0's changelog: "Removes `HelpOption`", "Removes the `mix_stderr` parameter in
    `CliRunner`", and `BaseCommand`, `MultiCommand`, `OptionParser` and the `parser` module
    are deprecated (a module ``__getattr__`` still serves them, with a warning)."""
    changes = _changes("click-8.1.8-8.2.0")
    assert (REMOVED, "click.HelpOption", "") in changes
    # ``catch_exceptions: bool = True`` took its place, but is a new parameter, as click's
    # docstring says: not a rename to suggest.
    mix_stderr = changes[(PARAM_REMOVED, "click.testing.CliRunner.__init__", "mix_stderr")]
    assert (mix_stderr.renamed, mix_stderr.suggestions) == (False, [])
    assert "catch_exceptions" not in diff_note([mix_stderr]).line
    deprecated = {path for kind, path, _ in changes if kind == DEPRECATED}
    assert {"click.BaseCommand", "click.MultiCommand", "click.OptionParser"} <= deprecated
    assert {"click.parser.OptionParser", "click.parser.split_arg_string"} <= deprecated
    assert diff_note([changes[(DEPRECATED, "click.BaseCommand", "")]]).line == (
        "`click.BaseCommand` is deprecated; avoid it in new code. Use `click.Command` instead. "
        "[diff + library]"
    )
    # Not in the changelog, in the source: these methods now take the context.
    for method in ("click.core.Parameter.make_metavar", "click.types.ParamType.get_metavar"):
        assert (PARAM_REQUIRED, method, "ctx") in changes
