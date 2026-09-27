"""Baseline notes (``run --compare``): the blocks, the shared pairing and the reports."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from since_cutoff import cli
from since_cutoff.apidiff import (
    KIND_CHANGED,
    PARAM_KEYWORD_ONLY,
    PARAM_REMOVED,
    APIChange,
    diff_sources,
    find_object,
    load_api,
)
from since_cutoff.baselines import (
    ARM_SIGNATURES,
    ARM_TEMPLATE,
    ARM_VERIFIED,
    qualified,
    reference,
    replacement,
    replacement_candidates,
    signature_bullets,
    signature_notes,
    template_notes,
)
from since_cutoff.engine import (
    ANOTHER_BLOCK_ERROR,
    ERROR,
    PASS,
    STALE,
    Attempt,
    Reporter,
    RunResult,
)
from since_cutoff.notes import BLOCK_END, BLOCK_START, template_bullet
from since_cutoff.project import load_project
from since_cutoff.providers.base import Completion
from since_cutoff.pypi import SourceTree
from since_cutoff.report import (
    ARM_TITLES,
    _arms_md,
    _baseline_lines,
    arms_summary,
    head_to_head,
    headline,
    pairing_summary,
    render_markdown,
    summary,
    to_json,
)
from since_cutoff.stats import estimate_tokens
from tests.conftest import ScriptedModel
from tests.test_engine import make_engine, make_project

pyright = pytest.mark.pyright

# What the notes must say for the scripted model to get each failing toylib change right. The
# verified notes say all four; the template baseline never shows how to call ``fetch`` in place
# of ``legacy_fetch``, and the signatures baseline never says ``temperature`` is gone.
LEARNS_FROM = {
    "send": "temperature",
    "fetch": "timeout",
    "legacy": "fetch(url",
    "close": "shutdown",
}
BOTH = [ARM_TEMPLATE, ARM_SIGNATURES]
BLOCKS = (ARM_VERIFIED, ARM_TEMPLATE)


# ------------------------------------------------------------------ helpers
@pytest.fixture
def toy_changes(toylib: tuple[SourceTree, SourceTree]) -> dict[str, APIChange]:
    """The toylib 1.0 -> 2.0 changes by name, each under its shortest path."""
    v1, v2 = toylib
    found = [
        APIChange.from_dict(d)
        for d in diff_sources("toylib", "1.0", v1.root, "2.0", v2.root, ["toylib"])
    ]
    by_name: dict[str, APIChange] = {}
    for c in sorted(found, key=lambda c: len(c.path)):
        if c.kind != PARAM_KEYWORD_ONLY:  # the same send() as its param_removed twin
            by_name.setdefault(c.name, c)
    return by_name


class Api:
    """An ApiLookup over toylib 2.0 that records which changes it was asked about."""

    def __init__(self, tree: SourceTree | None) -> None:
        self.root = load_api("toylib", tree.root) if tree is not None else None
        self.asked: list[str] = []

    def __call__(self, change: APIChange) -> object:
        self.asked.append(change.name)
        return self.root


def arm_pair(cid: str, task: str, without: str, **arms: str) -> list[Attempt]:
    """One held-out task answered without notes and with each arm's notes."""
    change = APIChange("pkg", "1", "2", PARAM_REMOVED, f"pkg.{cid}", cid, None, "p")
    out = [Attempt(change, task, "heldout", False, outcome=without)]
    out += [Attempt(change, task, "heldout", True, outcome=o, arm=a) for a, o in arms.items()]
    return out


def compare_run(tmp_path: Path, cache, fake_pypi, compare: list[str], model: ScriptedModel):
    engine = make_engine(
        cache, fake_pypi, model, max_probes=10, heldout=2, regression=2, compare=compare
    )
    engine.settings.today = date(2026, 9, 27)
    scan = engine.scan(load_project(make_project(tmp_path)), engine.resolve_target())
    return scan, engine.run(scan)


# ------------------------------------------------------------- the baselines
def test_the_template_baseline_is_the_diff_in_plain_sentences(toy_changes):
    changes = list(toy_changes.values())
    notes = template_notes(changes)
    assert [n.bullet for n in notes] == [template_bullet(c) for c in changes]
    assert all(n.source == ARM_TEMPLATE and not n.verified and n.example is None for n in notes)


def test_the_signatures_baseline_shows_the_api_to_use(toylib, toy_changes):
    api = Api(toylib[1])
    bullets = {name: signature_bullets(c, api) for name, c in toy_changes.items()}
    fetch = "(url: str, *, timeout: float, retries: int = 3) -> bytes`: Fetch a URL with retries."
    assert bullets == {
        # A changed callable: its new signature and docstring, under its full path.
        "send": [
            "`toylib.Client.send(self, message: str, *, stream: bool = False) -> str`: "
            "Send a message and return the reply."
        ],
        "fetch": ["`toylib.helpers.fetch" + fetch],
        # A removal: the bare fact, then the replacement its old deprecation note names.
        "legacy_fetch": ["`toylib.legacy_fetch` is not in toylib 2.0.", "`toylib.fetch" + fetch],
        # A deprecation: the replacement its @deprecated message names, instead of itself.
        "close": ["`toylib.Client.shutdown(self) -> None`: Shut the client down."],
        # A move: the object at its new path.
        "Session": ["`class toylib.Session`: A reusable HTTP session."],
    }
    # The new version's API is only loaded to look up a replacement.
    assert sorted(api.asked) == ["close", "legacy_fetch"]


def test_without_the_new_api_the_signatures_baseline_keeps_what_the_diff_has(toy_changes):
    api = Api(None)
    assert signature_bullets(toy_changes["legacy_fetch"], api) == [
        "`toylib.legacy_fetch` is not in toylib 2.0."
    ]
    assert signature_bullets(toy_changes["close"], api) == [
        "`toylib.Client.close(self) -> None`: Close the client."
    ]


def test_the_signatures_baseline_shows_an_api_once(toylib, toy_changes):
    notes = signature_notes([toy_changes["legacy_fetch"], toy_changes["fetch"]], Api(toylib[1]))
    assert [n.bullet.split("(")[0] for n in notes] == [
        "`toylib.legacy_fetch` is not in toylib 2.0.",
        "`toylib.fetch",  # the same function as toylib.helpers.fetch: not repeated
    ]
    assert all(n.source == ARM_SIGNATURES for n in notes)

    # Two changes to one callable: shown once. Two callables that read alike: both shown.
    send = toy_changes["send"]
    twin = APIChange(**{**send.to_dict(), "kind": PARAM_KEYWORD_ONLY, "parameter": "stream"})
    other = APIChange(
        **{**send.to_dict(), "path": "toylib.AsyncClient.send", "owner": "AsyncClient"}
    )
    bullets = [n.bullet for n in signature_notes([send, twin, other], Api(None))]
    assert [b.split("(")[0] for b in bullets] == ["`toylib.Client.send", "`toylib.AsyncClient.send"]


def test_a_replacement_is_a_function_or_class_the_library_names(toylib, toy_changes):
    api = Api(toylib[1])
    close = toy_changes["close"]

    def named(text: str) -> str | None:
        found = replacement(APIChange(**{**close.to_dict(), "deprecation": text}), api)
        return found[0] if found else None

    assert named("Use `toylib.Session` instead.") == "toylib.Session"
    assert named("Call shutdown() instead.") == "toylib.Client.shutdown"  # in the owner
    assert named("Prefer the fetch helper.") == "toylib.fetch"  # a plain word, in the package
    assert named("Use Client or close instead.") is None  # the owner, and the object itself
    assert named("Use `toylib.nothing_here` instead.") is None
    assert named("Use `toylib._client.Client.shutdown`.") is None  # private
    assert named("Use `toylib.helpers` instead.") is None  # a module, not a callable
    # Only removals and deprecations have a replacement.
    assert replacement(toy_changes["send"], api) is None


def test_replacement_candidates_put_code_before_words():
    assert replacement_candidates(".. deprecated:: 1.0 Use fetch instead.") == ["fetch"]
    assert replacement_candidates("Use Client.shutdown() instead.") == ["Client.shutdown"]
    assert replacement_candidates("Deprecated: use :func:`~pkg.load` or reader.read.") == [
        "pkg.load",
        "reader.read",
    ]
    assert replacement_candidates("Will be removed, prefer open_stream(), see Stream") == [
        "open_stream",
        "Stream",
    ]


def test_signatures_are_named_by_their_full_path():
    assert qualified("send(self, q: str) -> str", "pkg.Client.send") == (
        "pkg.Client.send(self, q: str) -> str"
    )
    assert qualified("__init__(self, key: str)", "pkg.Client.__init__") == (
        "class pkg.Client(self, key: str)"
    )
    assert qualified("class Session", "pkg.Session") == "class pkg.Session"
    assert qualified("timeout: float", "pkg.DEFAULT.timeout") == "pkg.DEFAULT.timeout: float"
    assert qualified("class Impl", "pkg.Base") == "class Impl"  # an alias keeps its own name
    assert qualified("sender(x)", "pkg.send") == "sender(x)"


def test_a_reference_keeps_package_text_inside_its_code_span():
    bullet = reference(
        "pkg.f", "f(x: Literal['`']) -> None", "Does <!-- things -->   `now`. " + "x" * 300
    )
    assert bullet.startswith("`pkg.f(x: Literal[''']) -> None`: Does  things  'now'. xxx")
    assert bullet.count("`") == 2 and "<!--" not in bullet and bullet.endswith("...")
    assert reference("pkg.f", None, None) == "`pkg.f`."
    assert reference("pkg.f", "f()", "Frobs") == "`pkg.f()`: Frobs."


def test_a_kind_change_gets_its_docstring_from_the_new_api(toylib):
    change = APIChange("toylib", "1.0", "2.0", KIND_CHANGED, "toylib.Session", "Session")
    change.new_signature = "class Session"
    assert signature_bullets(change, Api(toylib[1])) == [
        "`class toylib.Session`: A reusable HTTP session."
    ]
    root = load_api("toylib", toylib[1].root)
    assert find_object(root, "toylib.Session") is not None
    assert find_object(root, "otherlib.Session") is None


# ------------------------------------------------------------- the pairing
def test_every_arm_is_paired_with_the_same_answer_without_notes():
    run = RunResult(scan=None)  # type: ignore[arg-type]
    run.heldout = [
        *arm_pair("a", "1", STALE, verified=PASS, template=PASS),
        *arm_pair("a", "2", STALE, verified=PASS, template=STALE),
        *arm_pair("b", "1", STALE, verified=STALE, template=PASS),
        *arm_pair("c", "1", STALE, verified=PASS, template=ERROR),  # not counted for template
        *arm_pair("d", "1", PASS, verified=PASS, template=PASS),
    ]
    verified, template = run.pairing("heldout"), run.pairing("heldout", ARM_TEMPLATE)
    assert (verified.n, verified.before, verified.after, verified.changes_fixed) == (5, 1, 4, 2)
    assert (template.n, template.before, template.after, template.changes_fixed) == (4, 1, 3, 1)
    assert template.excluded_reasons[ERROR] == 1
    assert run.pairing("heldout", ARM_SIGNATURES).n == 0  # an arm nobody answered with

    # Compared, the blocks count the same pairs: c's error with the template leaves c out of
    # both. a: fixed by verified, half by template; b: template only.
    verified, template = (run.pairing("heldout", arm, common=True) for arm in BLOCKS)
    assert (verified.n, verified.before, verified.after, verified.changes_fixed) == (4, 1, 3, 1)
    assert (template.n, template.before, template.after, template.changes_fixed) == (4, 1, 3, 1)
    assert verified.excluded_reasons[ANOTHER_BLOCK_ERROR] == 1
    assert template.excluded_reasons[ERROR] == 1 and ANOTHER_BLOCK_ERROR not in (
        template.excluded_reasons
    )
    assert head_to_head(verified, template) == {
        "changes": 3,
        "fixed_by_verified_only": 1,
        "fixed_by_this_only": 1,
        "sign_test_p": 1.0,
    }


def test_an_error_with_one_block_cannot_hand_it_a_fix():
    """Change x, two held-out tasks: the verified notes fix one of the two, so x is not fixed.
    The template fixes the same one and errors on the other: paired on its own, it would count
    x as fixed on one task and win the head-to-head. Compared on the same pairs, it does not."""
    run = RunResult(scan=None)  # type: ignore[arg-type]
    run.heldout = [
        *arm_pair("x", "1", STALE, verified=PASS, template=PASS),
        *arm_pair("x", "2", STALE, verified=STALE, template=ERROR),
    ]
    run.arms = {ARM_VERIFIED: "- verified", ARM_TEMPLATE: "- template"}
    template = arms_summary(run)[ARM_TEMPLATE]
    assert template["versus_verified"] == {
        "changes": 1,
        "fixed_by_verified_only": 0,
        "fixed_by_this_only": 0,
        "sign_test_p": 1.0,
    }
    assert (template["heldout"]["n"], template["heldout"]["excluded_reasons"][ERROR]) == (1, 1)
    # The verified notes' own result keeps both of its pairs, as in a run without --compare.
    assert run.pairing("heldout").n == 2 and run.pairing("heldout").changes_fixed == 0


def test_every_block_shows_the_same_answers_without_notes():
    """a: an error with the template; b, c: fixed by both. Both rows count the same 2 pairs
    with the same rate without notes, and each says which pairs it left out."""
    run = RunResult(scan=None)  # type: ignore[arg-type]
    run.heldout = [
        *arm_pair("a", "1", PASS, verified=PASS, template=ERROR),
        *arm_pair("b", "1", STALE, verified=PASS, template=PASS),
        *arm_pair("c", "1", STALE, verified=PASS, template=PASS),
    ]
    run.arms = {ARM_VERIFIED: "- verified", ARM_TEMPLATE: "- template"}
    arms = arms_summary(run)
    table = _arms_md(arms, run.arms)
    rows = [line for line in table if line.startswith("| ") and not line.startswith("| notes |")]
    assert [row.split(" | ")[2:4] for row in rows] == [
        ["0% -> 100% of 2", "1 error with another block"],
        ["0% -> 100% of 2", "1 error"],
    ]
    # The verified notes' own line counts a's pair too; a last line puts them on the same pairs.
    alone = pairing_summary(run.pairing("heldout"))
    assert (alone["n"], alone["before"]) == (3, 1)
    assert [t.plain for t in _baseline_lines(arms, alone)][-1] == (
        "Blocks compared on the 2 held-out pairs all could count; verified notes on them: "
        "0% -> 100%; not counted: 1 error with another block"
    )
    assert len(_baseline_lines(arms, arms[ARM_VERIFIED]["heldout"])) == 1  # nothing left out


# ------------------------------------------------------------- end to end
@pyright
def test_compare_answers_each_task_once_without_notes_and_once_per_arm(tmp_path, cache, fake_pypi):
    model = ScriptedModel(knows={"session"}, learns_from=LEARNS_FROM)
    _, run = compare_run(tmp_path, cache, fake_pypi, BOTH, model)

    assert list(run.arms) == [ARM_VERIFIED, ARM_TEMPLATE, ARM_SIGNATURES]
    assert run.arms[ARM_VERIFIED] == run.block
    for block in run.arms.values():
        assert block.startswith(BLOCK_START) and block.rstrip().endswith(BLOCK_END)
        # The same header, so only the bullets differ.
        assert block.splitlines()[:4] == run.block.splitlines()[:4]

    # 4 failing changes x 2 held-out tasks, plus 1 regression task, each answered 4 times.
    tasks = {(a.role, a.task) for a in run.heldout}
    assert len(tasks) == 9 and len(run.heldout) == 9 * 4
    for role, task in tasks:
        answers = [a for a in run.heldout if (a.role, a.task) == (role, task)]
        assert sorted((a.with_notes, a.arm) for a in answers if a.with_notes) == sorted(
            (True, arm) for arm in run.arms
        )
        assert sum(not a.with_notes for a in answers) == 1
    solver = [user for system, user in model.calls if system.startswith("You are a senior")]
    assert all(solver.count(task) == 4 for _, task in tasks)  # nothing answered twice
    # No model call writes a baseline: one note-writer call per verified note.
    notes_calls = [s for s, _ in model.calls if s.startswith("You keep AGENTS.md")]
    assert len(notes_calls) == len(run.notes) == 4

    fixed = {
        arm: {c.change: c.outcome for c in run.pairing("heldout", arm).per_change}
        for arm in run.arms
    }
    by_name = {c.change: c.change.split("`")[1] for c in run.pairing("heldout").per_change}
    outcomes = {
        arm: sorted(by_name[change] for change, o in per.items() if o == "fixed")
        for arm, per in fixed.items()
    }
    assert outcomes == {
        ARM_VERIFIED: sorted(
            [
                "toylib.Client.close",
                "toylib.Client.send(temperature=...)",
                "toylib.helpers.fetch(timeout=...)",
                "toylib.legacy_fetch",
            ]
        ),
        ARM_TEMPLATE: sorted(
            [
                "toylib.Client.close",
                "toylib.Client.send(temperature=...)",
                "toylib.helpers.fetch(timeout=...)",
            ]
        ),
        ARM_SIGNATURES: sorted(
            ["toylib.Client.close", "toylib.helpers.fetch(timeout=...)", "toylib.legacy_fetch"]
        ),
    }
    for arm in run.arms:
        regression = run.pairing("regression", arm)
        assert (regression.n, regression.after, regression.broken) == (1, 1, 0)


@pyright
def test_reports_show_each_arm(tmp_path, cache, fake_pypi):
    model = ScriptedModel(knows={"session"}, learns_from=LEARNS_FROM)
    scan, run = compare_run(tmp_path, cache, fake_pypi, BOTH, model)

    s = summary(scan, run)
    assert list(s["arms"]) == [ARM_VERIFIED, ARM_TEMPLATE, ARM_SIGNATURES]
    for arm, entry in s["arms"].items():
        assert entry["tokens"] == estimate_tokens(run.arms[arm])
    assert s["arms"][ARM_VERIFIED]["tokens"] == s["notes"]["tokens"]
    assert s["arms"][ARM_VERIFIED]["heldout"] == s["heldout"]["heldout"]
    assert s["arms"][ARM_VERIFIED]["versus_verified"] is None
    template = s["arms"][ARM_TEMPLATE]
    h = template["heldout"]
    assert (h["n"], h["before"], h["after"], h["changes"], h["changes_fixed"]) == (8, 0, 6, 4, 3)
    assert [round(x, 4) for x in h["ci95_changes_fixed"]] == [0.3006, 0.9544]
    assert template["regression"]["n"] == 1 and template["regression"]["after"] == 1
    assert template["versus_verified"] == {
        "changes": 4,
        "fixed_by_verified_only": 1,
        "fixed_by_this_only": 0,
        "sign_test_p": 1.0,
    }
    assert template["bullets"] == 4 and s["arms"][ARM_SIGNATURES]["bullets"] == 4
    assert s["settings"]["compare"] == BOTH

    lines = [t.plain for t in headline(scan, run, s)]
    assert lines[-2:] == [
        f"Baseline template, about {template['tokens']} tokens: held-out 0% -> 75%; changes "
        "fixed 3 of 4, 95% CI 30-95%; 0 broken",
        f"Baseline signatures, about {s['arms'][ARM_SIGNATURES]['tokens']} tokens: held-out "
        "0% -> 75%; changes fixed 3 of 4, 95% CI 30-95%; 0 broken",
    ]

    md = render_markdown(scan, run)
    assert "### Compared with baseline notes" in md
    assert (
        f"| verified notes | {s['notes']['tokens']} | 0% -> 100% of 8 | none | 4 of 4, 51-100% "
        "| 0 | 1 of 1 | - |"
    ) in md
    assert (
        f"| template baseline | {template['tokens']} | 0% -> 75% of 8 | none | 3 of 4, 30-95% "
        "| 0 | 1 of 1 | 1 / 0, p=1 |"
    ) in md
    assert (
        "| change | task | without notes | verified notes | template baseline "
        "| signatures baseline |"
    ) in md
    assert "| baseline notes compared | template, signatures (--compare) |" in md
    assert "<details><summary>template baseline block</summary>" in md
    assert run.arms[ARM_SIGNATURES].strip() in md

    data = json.loads(json.dumps(to_json(scan, run), default=str))
    assert {arm: entry["block"] for arm, entry in data["arms"].items()} == run.arms
    heldout = [a for a in data["attempts"] if a["role"] != "probe"]
    assert {a["arm"] for a in heldout if a["with_notes"]} == set(run.arms)
    assert all(a["arm"] is None for a in data["attempts"] if not a["with_notes"])


@pyright
def test_a_run_without_compare_is_unchanged(tmp_path, cache, fake_pypi):
    stages: list[str] = []

    class Stages(Reporter):
        def stage(self, title: str, total: int | None = None) -> None:
            stages.append(title)

    engine = make_engine(cache, fake_pypi, ScriptedModel(knows={"session"}), heldout=2)
    engine.reporter = Stages()
    scan = engine.scan(load_project(make_project(tmp_path)), engine.resolve_target())
    run = engine.run(scan)

    assert run.arms == {} and "compare" not in run.settings
    assert all(a.arm == ARM_VERIFIED for a in run.heldout)
    assert len(run.heldout) == 2 * 9  # without and with the notes, per task
    assert "Verifying the notes on 18 held-out answers" in stages
    s = summary(scan, run)
    assert "arms" not in s and "compare" not in s["settings"]
    assert all("arm" not in a for a in to_json(scan, run)["attempts"])
    md = render_markdown(scan, run)
    assert "Compared with baseline notes" not in md and "baseline notes compared" not in md
    assert "| change | task | without notes | with notes |\n|---|---|---|---|\n" in md
    assert not any(line.plain.startswith("Baseline") for line in headline(scan, run, s))


@pyright
def test_the_verified_arm_reuses_the_answers_of_a_run_without_compare(tmp_path, cache, fake_pypi):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = ScriptedModel(knows={"session"}, learns_from=LEARNS_FROM)
    _, plain = compare_run(tmp_path / "a", cache, fake_pypi, [], first)
    second = ScriptedModel(knows={"session"}, learns_from=LEARNS_FROM)
    _, compared = compare_run(tmp_path / "b", cache, fake_pypi, [ARM_TEMPLATE], second)

    # The same prompts, so the same cache keys: the only new calls are the template arm's.
    assert len(second.calls) == 9  # 8 held-out tasks and 1 regression check
    assert all(compared.arms[ARM_TEMPLATE].strip() in system for system, _ in second.calls)
    assert compared.pairing("heldout").to_dict() == plain.pairing("heldout").to_dict()


@pyright
def test_a_baseline_that_errors_is_not_counted_against_the_others(tmp_path, cache, fake_pypi):
    class TemplateDown(ScriptedModel):
        def complete(self, system: str, user: str) -> Completion:
            if "Do not pass it." in system:  # only the template block says this
                from since_cutoff.errors import ProviderError

                raise ProviderError("rate limited")
            return super().complete(system, user)

    model = TemplateDown(knows={"session"}, learns_from=LEARNS_FROM)
    scan, run = compare_run(tmp_path, cache, fake_pypi, [ARM_TEMPLATE], model)
    template = run.pairing("heldout", ARM_TEMPLATE)
    assert template.n == 0 and template.excluded_reasons[ERROR] == 8
    s = summary(scan, run)
    assert s["heldout"]["heldout"]["n"] == 8  # the verified notes' own result still counts
    # Compared, no pair counts: every one errored with the template.
    arms = s["arms"]
    assert arms[ARM_TEMPLATE]["versus_verified"]["changes"] == 0
    assert arms[ARM_VERIFIED]["heldout"]["n"] == 0
    assert arms[ARM_VERIFIED]["heldout"]["excluded_reasons"][ANOTHER_BLOCK_ERROR] == 8
    md = render_markdown(scan, run)
    assert "| n/a | 8 error with another block | n/a | 0 |" in md
    assert "| n/a | 8 error | n/a | 0 |" in md
    lines = [t.plain for t in headline(scan, run, s)]
    assert (
        "Held-out tasks correct without -> with notes: 0% -> 100%, 8 paired tasks from 4 API "
        "changes"
    ) in lines
    assert lines[-1] == (
        "Blocks compared on the 0 held-out pairs all could count; verified notes on them: "
        "n/a -> n/a; not counted: 8 error with another block"
    )


@pyright
def test_compare_needs_a_failure_to_compare(tmp_path, cache, fake_pypi):
    model = ScriptedModel(knows={"send", "fetch", "legacy", "session", "close"})
    scan, run = compare_run(tmp_path, cache, fake_pypi, BOTH, model)
    assert not run.failing() and run.arms == {}
    assert "arms" not in summary(scan, run)


# ------------------------------------------------------------------- the CLI
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("template", [ARM_TEMPLATE]),
        ("signatures,template", [ARM_SIGNATURES, ARM_TEMPLATE]),
        ("template, template", [ARM_TEMPLATE]),
        ("none", []),
    ],
)
def test_compare_takes_a_list_of_baselines(value, expected):
    args = cli.build_parser().parse_args(["run", "--compare", value])
    assert args.compare == expected


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--compare", "docs"], "unknown baseline 'docs'"),
        (["--compare", "none,template"], "'none' cannot be combined"),
        (["--compare", "template", "--no-fix"], "--no-fix skips"),
        (["--compare", "template", "--heldout", "0"], "--heldout 0 leaves none"),
    ],
)
def test_compare_rejects_what_it_cannot_measure(tmp_path, capsys, argv, message):
    with pytest.raises(SystemExit) as exit_info:  # argparse errors exit directly
        code = cli.main(["run", str(tmp_path), *argv])
        raise SystemExit(code)
    assert exit_info.value.code == 2
    assert message in capsys.readouterr().err


@pyright
def test_cli_compares_the_notes_with_a_baseline(tmp_path, capsys, scripted_cli):
    root = make_project(tmp_path)
    argv = ["run", str(root), "--model", "anthropic:claude-sonnet-4-5", "--cutoff", "2025-07-31"]
    argv += ["--max-probes", "2", "--heldout", "1", "--regression", "0"]
    scripted_cli.append(ScriptedModel(learns_from=LEARNS_FROM))
    assert cli.main([*argv, "--compare", "template"]) == 0
    out = capsys.readouterr().out
    assert "Verifying the notes and 1 baseline on" in out
    assert "Baseline template, about" in out
    results = json.loads((root / ".since-cutoff" / "results.json").read_text(encoding="utf-8"))
    assert list(results["arms"]) == [ARM_VERIFIED, ARM_TEMPLATE]
    assert results["settings"]["compare"] == [ARM_TEMPLATE]
    report = (root / ".since-cutoff" / "report.md").read_text(encoding="utf-8")
    assert f"| {ARM_TITLES[ARM_TEMPLATE]} |" in report


def test_every_arm_has_a_report_title():
    assert set(ARM_TITLES) == {ARM_VERIFIED, ARM_TEMPLATE, ARM_SIGNATURES}
