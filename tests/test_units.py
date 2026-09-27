"""Unit tests for prompts, notes, selection, statistics and the cache."""

from __future__ import annotations

import ast
import json
import math

import pytest

from since_cutoff import prompts
from since_cutoff.apidiff import (
    DEPRECATED,
    MOVED,
    PARAM_REMOVED,
    PARAM_REQUIRED,
    REMOVED,
    APIChange,
)
from since_cutoff.cache import DiskCache, stable_hash
from since_cutoff.notes import (
    BLOCK_END,
    BLOCK_START,
    Note,
    apply_block,
    bullet_is_grounded,
    clean_bullet,
    remove_block,
    render_block,
    template_bullet,
)
from since_cutoff.project import scan_file
from since_cutoff.selection import select
from since_cutoff.stats import (
    cluster_bootstrap_interval,
    estimate_tokens,
    format_p,
    pct,
    points,
    sign_test,
    wilson_interval,
)


def change(
    kind=PARAM_REMOVED, name="create", owner="Messages", param="temperature", pkg="anthropic", **kw
) -> APIChange:
    return APIChange(
        package=pkg,
        from_version="0.60.0",
        to_version="1.8.0",
        kind=kind,
        path=f"{pkg}.resources.{owner}.{name}" if owner else f"{pkg}.{name}",
        name=name,
        owner=owner,
        parameter=param,
        **kw,
    )


# -------------------------------------------------------------------- prompts
def test_task_prompt_forbids_identifiers_but_allows_parameter_words():
    text = prompts.task_prompt(change(), ["anthropic"], 3)
    assert "`create`" in text and "`Messages`" in text
    assert "Never write these identifiers" in text
    assert "`temperature`" not in text.split("Never write these identifiers:")[1].split("\n")[0]


def test_parse_tasks_drops_tasks_that_leak_identifiers():
    c = change(kind=REMOVED, name="get_relevant_documents", owner="BaseRetriever", param=None)
    reply = json.dumps(
        {
            "tasks": [
                "Call get_relevant_documents on it.",
                "Find the three most relevant documents for 'x'.",
            ]
        }
    )
    tasks, reason = prompts.parse_tasks(reply, c)
    assert tasks == ["Find the three most relevant documents for 'x'."] and reason is None


def test_parse_tasks_reports_skip_reasons():
    tasks, reason = prompts.parse_tasks('{"tasks": [], "skip_reason": "internal only"}', change())
    assert tasks == [] and reason == "internal only"


@pytest.mark.parametrize(
    "text",
    ['{"a": 1}', '```json\n{"a": 1}\n```', 'Sure! {"a": 1} Hope that helps.'],
)
def test_parse_json_object_is_tolerant(text):
    assert prompts.parse_json_object(text) == {"a": 1}


def test_parse_json_object_rejects_non_objects():
    assert prompts.parse_json_object("[1, 2]") is None
    assert prompts.parse_json_object("nothing") is None


def test_solver_prompt_carries_the_pin_and_optional_notes():
    plain = prompts.solver_system("anthropic", "1.8.0")
    assert "anthropic==1.8.0" in plain and "AGENTS.md" not in plain
    assert "- use X" in prompts.solver_system("anthropic", "1.8.0", "- use X")


# ---------------------------------------------------------------------- notes
@pytest.mark.parametrize(
    ("c", "needle"),
    [
        (change(), "`temperature` was removed in anthropic 1.8.0"),
        (change(kind=PARAM_REQUIRED, param="timeout"), "now requires `timeout`"),
        (change(kind=REMOVED, param=None, hint="use invoke instead"), "(use invoke instead)"),
        (
            change(
                kind=MOVED, param=None, owner=None, name="Session", moved_to="pkg.sessions.Session"
            ),
            "from pkg.sessions import Session",
        ),
        (
            change(kind=DEPRECATED, param=None, deprecation="Use shutdown()."),
            "deprecated in anthropic 1.8.0: Use shutdown()",
        ),
    ],
)
def test_template_bullets(c, needle):
    assert needle in template_bullet(c)


def test_grounding_requires_recommended_api_to_be_in_the_example():
    c = change()
    example = "import anthropic\nanthropic.Anthropic().messages.create(model='m', max_tokens=5, messages=[])\n"
    assert bullet_is_grounded(
        "`client.messages.create(temperature=...)`: omit `temperature`.", example, c
    )
    assert bullet_is_grounded("Use `client.messages.create(model='claude-x')`.", example, c)
    assert not bullet_is_grounded("Use `client.messages.generate(...)` instead.", example, c)
    assert not bullet_is_grounded("Pass `sampling=Sampling(t=0.2)`.", example, c)


def test_clean_bullet():
    assert clean_bullet("-   use  `x`\n now ") == "use `x` now"


def test_block_apply_is_idempotent_and_reversible(tmp_path):
    target = tmp_path / "AGENTS.md"
    target.write_text("# Project rules\n\nBe nice.\n", encoding="utf-8")
    block = render_block(
        [Note(change(), "`a` -> `b`.", None, True, "template")],
        model="m",
        cutoff=__import__("datetime").date(2025, 7, 31),
        version_source="uv.lock",
    )
    assert apply_block(target, block) == "appended to"
    once = target.read_text(encoding="utf-8")
    assert apply_block(target, block) == "updated"
    assert target.read_text(encoding="utf-8") == once
    assert once.count(BLOCK_START) == 1 and "Be nice." in once
    assert remove_block(target)
    assert target.read_text(encoding="utf-8") == "# Project rules\n\nBe nice.\n"


def test_block_is_created_when_missing(tmp_path):
    target = tmp_path / "CLAUDE.md"
    assert apply_block(target, f"{BLOCK_START}\nx\n{BLOCK_END}\n") == "created"


def test_render_block_groups_and_dedupes():
    notes = [
        Note(change(), "same", None, True, "model"),
        Note(change(name="stream"), "same", None, True, "model"),
        Note(change(pkg="openai"), "other", None, True, "model"),
    ]
    block = render_block(
        notes, model="m", cutoff=__import__("datetime").date(2025, 1, 1), version_source="uv.lock"
    )
    assert (
        block.count("- same") == 1
        and "**openai 1.8.0**" in block
        and "**anthropic 1.8.0**" in block
    )


# ------------------------------------------------------------------ selection
def test_selection_prefers_used_symbols_and_spreads_across_packages():
    a = [change(name=f"f{i}", param=f"p{i}") for i in range(5)] + [change(name="used", param="q")]
    b = [change(pkg="openai", name=f"g{i}", param=f"p{i}") for i in range(5)]
    # `client.messages.used(...)`: the method and its class are both named in the code.
    files = (scan_file(ast.parse("import anthropic\nclient.messages.used()\n")),)
    picked = select({"anthropic": a, "openai": b}, {"anthropic": files}, 4)
    assert picked[0].name == "used"
    assert [c.package for c in picked].count("openai") == 2


def test_selection_dedupes_concepts():
    twins = [change(owner="Messages"), change(owner="AsyncMessages")]
    assert len(select({"anthropic": twins}, {}, 10)) == 1


# ---------------------------------------------------------------- stats/cache
def test_wilson_interval_bounds():
    lo, hi = wilson_interval(0, 10)
    assert lo == 0.0 and 0.2 < hi < 0.35
    lo, hi = wilson_interval(10, 10)
    assert hi == 1.0 and 0.65 < lo < 0.8
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_interval_known_value():
    # 6 of 10: the textbook Wilson score interval is 0.3127-0.8318.
    lo, hi = wilson_interval(6, 10)
    assert (round(lo, 4), round(hi, 4)) == (0.3127, 0.8318)


@pytest.mark.parametrize(
    ("positive", "negative", "p"),
    [
        (4, 0, 0.125),
        (0, 4, 0.125),
        (6, 0, 0.03125),
        (10, 2, 0.03857421875),  # R: binom.test(10, 12)$p.value
        (5, 5, 1.0),
        (1, 0, 1.0),
        (0, 0, 1.0),
    ],
)
def test_sign_test_known_values(positive, negative, p):
    assert sign_test(positive, negative) == pytest.approx(p, abs=1e-12)


def test_sign_test_matches_the_definition_of_a_two_sided_exact_p_value():
    # Sum the probabilities of every outcome no more likely than the one observed.
    for n in range(1, 16):
        probs = [math.comb(n, k) / 2**n for k in range(n + 1)]
        for k in range(n + 1):
            expected = sum(q for q in probs if q <= probs[k] * (1 + 1e-9))
            assert sign_test(k, n - k) == pytest.approx(min(1.0, expected))
    with pytest.raises(ValueError):
        sign_test(-1, 3)


def test_cluster_bootstrap_resamples_whole_clusters():
    # Two changes of ten pairs each, one all fixed and one all broken. Resampling pairs would
    # give a narrow interval around 0; resampling changes can draw the same change twice.
    assert cluster_bootstrap_interval([(10, 10), (10, -10)]) == (-1.0, 1.0)
    assert cluster_bootstrap_interval([(1, 1), (1, 0)]) == (0.0, 1.0)
    # Every change moved alike: every resample gives the same pooled difference.
    assert cluster_bootstrap_interval([(2, 1), (4, 2), (6, 3)]) == (0.5, 0.5)
    # Nothing to resample.
    assert cluster_bootstrap_interval([]) is None
    assert cluster_bootstrap_interval([(3, 2)]) is None
    assert cluster_bootstrap_interval([(3, 2), (0, 0)]) is None


def test_cluster_bootstrap_is_seeded_and_the_same_on_every_python():
    clusters = [(2, 2)] * 6 + [(2, 0)] * 4  # 6 of 10 changes fixed on both of their tasks
    lo, hi = cluster_bootstrap_interval(clusters)
    assert (lo, hi) == (0.3, 0.9)  # pinned: a different Python must not move it
    assert cluster_bootstrap_interval(clusters) == (lo, hi)
    assert lo <= 12 / 20 <= hi
    lo2, hi2 = cluster_bootstrap_interval([(3, 1), (2, 2), (1, 0), (2, -1), (4, 3)])
    assert (round(lo2, 4), round(hi2, 4)) == (-0.1111, 0.7692)
    other_lo, other_hi = cluster_bootstrap_interval(clusters, seed=1, resamples=2000)
    assert other_lo <= 12 / 20 <= other_hi


def test_p_values_and_points_are_formatted_for_reading():
    assert format_p(0.125) == "p=0.125"
    assert format_p(0.03125) == "p=0.031"
    assert format_p(1.0) == "p=1"
    assert format_p(0.0004) == "p<0.001"
    assert (points(0.6), points(-0.05), points(-0.001), points(0.0)) == ("+60", "-5", "+0", "+0")


def test_pct_and_tokens():
    assert pct(1, 3) == "33%" and pct(0, 0) == "n/a"
    assert estimate_tokens("x" * 40) == 10


def test_cache_round_trip_and_disable(tmp_path):
    c = DiskCache(tmp_path)
    c.set("ns", "k", {"a": [1, 2]})
    assert c.get("ns", "k") == {"a": [1, 2]}
    assert c.get("ns", "k", max_age=-1) is None
    off = DiskCache(tmp_path, enabled=False)
    assert off.get("ns", "k") is None
    assert stable_hash({"b": 1, "a": 2}) == stable_hash({"a": 2, "b": 1})
