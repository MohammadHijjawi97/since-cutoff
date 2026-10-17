"""Property-based tests: rules that must hold for every input, not only for the examples in the
other test files. Hypothesis generates the inputs (see the profiles in conftest.py)."""

from __future__ import annotations

import ast
import io
import os
import re
import shutil
import tokenize
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st

from since_cutoff.apidiff import (
    DEPRECATED,
    MOVED,
    PARAM_KEYWORD_ONLY,
    PARAM_REMOVED,
    REMOVED,
    APIChange,
)
from since_cutoff.cache import DiskCache
from since_cutoff.checker import _prepare, extract_code, sanitize
from since_cutoff.mcp_server import _matching, _no_match, _symbol_terms
from since_cutoff.models import (
    ModelRegistry,
    bare_model_id,
    normalize_model_id,
    parse_cutoff,
)
from since_cutoff.notes import BLOCK_END, BLOCK_START, apply_block, remove_block
from since_cutoff.pypi import PyPI, Release, _safe_target
from since_cutoff.selection import select
from since_cutoff.stats import cluster_bootstrap_interval, sign_test, wilson_interval

# A fixture shared by the examples of one test is fine where each example starts by writing
# what it reads (the notes file, the cache entry).
shared_fixture = settings(suppress_health_check=[HealthCheck.function_scoped_fixture])


# ------------------------------------------------------------- model cutoffs
@given(st.dates())
def test_a_full_date_reads_back_as_itself(day: date) -> None:
    assert parse_cutoff(day.isoformat()) == day
    assert parse_cutoff(f"  {day.isoformat()}\n") == day


@given(st.dates())
def test_a_month_or_a_year_means_its_last_day(day: date) -> None:
    month = parse_cutoff(f"{day.year:04d}-{day.month:02d}")
    assert (month.year, month.month) == (day.year, day.month)
    assert month >= day
    assert month == date.max or (month + timedelta(days=1)).month != day.month
    assert parse_cutoff(f"{day.year:04d}-{day.month}") == month  # "2025-7" too
    assert parse_cutoff(f"{day.year:04d}") == date(day.year, 12, 31)


@given(st.text(max_size=16) | st.from_regex(r"\d{4}(-\d{1,3}){0,3}", fullmatch=True))
def test_a_cutoff_is_a_date_or_a_value_error(text: str) -> None:
    # --cutoff and the MCP tools turn a ValueError into a clean error message.
    try:
        parsed = parse_cutoff(text)
    except ValueError:
        return
    assert parse_cutoff(parsed.isoformat()) == parsed


# Model ids as agents and routers spell them: a maker's id ("claude-sonnet-4.5", "gpt-4.1",
# "qwen3") with a snapshot date, a tag or a context size, behind a router's prefix.
_WORD = st.from_regex(r"[a-z][a-z0-9]{0,5}", fullmatch=True).filter(lambda w: w != "latest")
_NUMBER = st.integers(0, 9).map(str) | st.tuples(st.integers(0, 9), st.integers(0, 9)).map(
    lambda t: f"{t[0]}.{t[1]}"
)
_BASE_ID = st.tuples(_WORD, st.lists(_WORD | _NUMBER, max_size=3)).map(
    lambda t: "-".join([t[0], *t[1]])
)
_SNAPSHOT_DAY = st.dates(date(2023, 1, 1), date(2099, 12, 31))
_SUFFIX = st.one_of(
    st.just(""),
    _SNAPSHOT_DAY.map(lambda d: d.strftime("-%Y%m%d")),  # Anthropic: -20250929
    _SNAPSHOT_DAY.map(lambda d: d.strftime("-%Y-%m-%d")),  # OpenAI: -2025-04-14
    st.just("-latest"),
    _WORD.map(lambda w: f":{w}"),  # Ollama: qwen3:32b
)
_ROUTER = st.sampled_from(["", "openrouter/", "anthropic/", "openai/", "ollama/"])
_CONTEXT = st.sampled_from(["", "[1m]", "[200k]"])


@example("", "gpt-4.1", "-2025-04-14", "", False)  # OpenAI's dated snapshots
@example("", "claude-sonnet-4.6", "-20260101", "", False)  # dots and a date at once
@given(_ROUTER, _BASE_ID, _SUFFIX, _CONTEXT, st.booleans())
def test_every_spelling_of_a_model_id_leads_back_to_it(
    router: str, base: str, suffix: str, context: str, upper: bool
) -> None:
    spelled = f"{router}{base}{suffix}{context}"
    candidates = normalize_model_id(f" {spelled.upper() if upper else spelled} ")
    assert candidates[0] == f"{base}{suffix}"  # the most specific: only router and tag go
    assert base in candidates
    assert base.replace(".", "-") in candidates  # models.dev spells "claude-sonnet-4-5"
    assert len(set(candidates)) == len(candidates)
    assert all(c and c == c.lower() for c in candidates)


@example("openrouter/", "a-0.0", "-latest", "[1m]")
@given(_ROUTER, _BASE_ID, _SUFFIX, _CONTEXT)
def test_normalizing_a_candidate_again_finds_nothing_new(
    router: str, base: str, suffix: str, context: str
) -> None:
    candidates = normalize_model_id(f"{router}{base}{suffix}{context}")
    for candidate in candidates:
        assert set(normalize_model_id(candidate)) <= set(candidates), candidate


@given(
    _BASE_ID,
    st.sampled_from(["", "us.", "eu.", "apac.", "global."]),
    st.sampled_from(["", "-v1", "-v1:0", "-v2:1"]),
    st.sampled_from(["", "@20250929", "@001"]),
)
def test_the_bare_id_drops_bedrock_vertex_and_router_decorations(
    base: str, region: str, version: str, vertex: str
) -> None:
    assume(not re.search(r"-v\d+$", base))  # that would be Bedrock's version ("claude-v2")
    for spelled in (
        f"{region}anthropic.{base}{version}",  # Amazon Bedrock
        f"{base}{vertex}",  # Vertex AI
        f"openrouter/anthropic/{base}",
        f"{base}[1m]",
    ):
        assert bare_model_id(spelled) == base, spelled
        assert bare_model_id(bare_model_id(spelled)) == base
        assert base in normalize_model_id(spelled)


# Offline and without a cache, the registry reads the models.dev snapshot bundled with it.
_REGISTRY = ModelRegistry(DiskCache(enabled=False), offline=True)
_KNOWN = sorted(
    (m.id, m.provider)
    for m in _REGISTRY.all_models()
    if m.knowledge and m.provider in ("anthropic", "openai", "google", "xai", "mistral")
)


@given(
    st.sampled_from(_KNOWN),
    st.sampled_from(["{}", "{}[1m]", " {} ", "openrouter/{}", "{}-20991231", "{}-2099-12-31"]),
    st.booleans(),
)
def test_a_known_model_is_found_however_it_is_spelled(
    known: tuple[str, str], spelling: str, upper: bool
) -> None:
    model_id, provider = known
    spelled = spelling.format(model_id)
    info = _REGISTRY.lookup(spelled.upper() if upper else spelled, provider)
    assert info is not None and info.id == model_id


# ------------------------------------------------------------------- statistics
@example((0, 11))  # the lower bound was 2.8e-17, above the rate of 0
@example((6, 6))  # the upper bound was 0.9999999999999999, below the rate of 1
@given(st.integers(1, 10_000).flatmap(lambda n: st.tuples(st.integers(0, n), st.just(n))))
def test_a_wilson_interval_is_a_range_of_rates_around_the_observed_one(counts: tuple[int, int]):
    successes, n = counts
    lo, hi = wilson_interval(successes, n)
    assert 0.0 <= lo <= successes / n <= hi <= 1.0
    assert lo < hi


@given(st.integers(0, 300), st.integers(0, 300))
def test_the_sign_test_is_a_symmetric_p_value(positive: int, negative: int) -> None:
    p = sign_test(positive, negative)
    assert 0.0 < p <= 1.0
    assert sign_test(negative, positive) == p
    if positive == negative:
        assert p == 1.0
    if positive >= negative:  # a more lopsided count is never less significant
        assert sign_test(positive + 1, negative) <= p


_CLUSTERS = st.lists(
    st.integers(0, 6).flatmap(lambda n: st.tuples(st.just(n), st.integers(-n, n))), max_size=8
)


@given(_CLUSTERS)
def test_the_bootstrap_interval_stays_within_the_changes_own_rates(
    clusters: list[tuple[int, int]],
) -> None:
    interval = cluster_bootstrap_interval(clusters, resamples=200)
    kept = [(n, net) for n, net in clusters if n > 0]
    if len(kept) < 2:
        assert interval is None
        return
    assert interval is not None
    lo, hi = interval
    rates = [net / n for n, net in kept]
    assert min(rates) - 1e-12 <= lo <= hi <= max(rates) + 1e-12
    assert cluster_bootstrap_interval(clusters, resamples=200) == interval  # seeded


@given(st.lists(st.integers(1, 6), min_size=2, max_size=8), st.sampled_from([-1, 0, 1]))
def test_when_every_change_agrees_the_interval_is_the_observed_difference(
    sizes: list[int], sign: int
) -> None:
    clusters = [(n, sign * n) for n in sizes]
    observed = sum(net for _, net in clusters) / sum(sizes)
    assert cluster_bootstrap_interval(clusters, resamples=200) == (observed, observed)


# -------------------------------------------------------------------- selection
_KINDS = [REMOVED, MOVED, PARAM_REMOVED, PARAM_KEYWORD_ONLY, DEPRECATED]


@st.composite
def api_changes(draw: st.DrawFn) -> APIChange:
    package = draw(st.sampled_from(["alpha", "beta", "gamma"]))
    kind = draw(st.sampled_from(_KINDS))
    owner = draw(st.sampled_from([None, "Client", "AsyncClient", "Session"]))
    name = draw(st.sampled_from(["create", "send", "close", "fetch", "VERSION"]))
    module = draw(st.sampled_from(["", "resources.", "_internal.", "cli."]))
    parameter = draw(st.none() | st.sampled_from(["timeout", "stream"]))
    return APIChange(
        package,
        "1.0",
        "2.0",
        kind,
        f"{package}.{module}{owner + '.' if owner else ''}{name}",
        name,
        owner=owner,
        parameter=parameter if kind.startswith("param") or kind == DEPRECATED else None,
        occurrences=draw(st.integers(1, 5)),
    )


@given(st.lists(api_changes(), max_size=25), st.integers(-1, 30))
def test_selection_fills_the_budget_once_per_concept_and_shares_it_fairly(
    changes: list[APIChange], budget: int
) -> None:
    by_package: dict[str, list[APIChange]] = {}
    for c in changes:
        by_package.setdefault(c.package, []).append(c)
    picked = select(by_package, {}, budget)

    assert all(any(p is c for c in changes) for p in picked)
    keys = [p.concept_key for p in picked]
    assert len(keys) == len(set(keys))  # near-duplicates are probed once
    concepts = {p: {c.concept_key for c in cs} for p, cs in by_package.items()}
    assert len(picked) == min(max(budget, 0), sum(len(k) for k in concepts.values()))
    # Round-robin: a package gets two more probes than another only once the other ran out.
    count = Counter(p.package for p in picked)
    for p in concepts:
        for q in concepts:
            assert count[p] <= count[q] + 1 or count[q] == len(concepts[q])


# ------------------------------------------------------------------------ notes
# Lines of an AGENTS.md: text, indentation, trailing spaces (a Markdown line break), blank
# lines and the markers mentioned in prose, but never a marker on a line of its own. A blank
# line holds only spaces and tabs, as in Markdown.
_LINE_BREAKS = "\r\n\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"  # where str.splitlines() splits
_LINE = st.one_of(
    st.text(st.characters(exclude_characters=_LINE_BREAKS, codec="utf-8"), max_size=12),
    st.sampled_from(["", "  ", "# Rules", "- be nice  ", "\tcode", f"See `{BLOCK_START}`."]),
).filter(lambda line: line.strip(" \t") not in (BLOCK_START, BLOCK_END))
_EOL = st.sampled_from(["\n", "\r\n"])


@st.composite
def notes_files(draw: st.DrawFn) -> str:
    """A file's text: its lines, each with its own line ending (mixed ones too), and a last
    line with or without one."""
    lines = draw(st.lists(st.tuples(_LINE, _EOL), max_size=6))
    last = draw(st.sampled_from(["", "last line", "  "]))
    return "".join(text + eol for text, eol in lines) + last


def block(*notes: str) -> str:
    body = "".join(f"- {n}\n" for n in notes)
    return (
        f"{BLOCK_START}\n## Library changes after the model's training cutoff\n{body}{BLOCK_END}\n"
    )


@shared_fixture
@example(text="# Rules", notes=["x"])  # no line break at the end: one was added
@example(text="# Rules\r\n\r\n\r\n", notes=[])  # blank lines at the end went
@example(text="Line break:  \n  ", notes=[])  # and so did trailing spaces
@given(notes_files(), st.lists(st.text(max_size=10).map(lambda s: " ".join(s.split())), max_size=3))
def test_apply_twice_is_apply_once_and_unapply_restores_the_file(
    tmp_path: Path, text: str, notes: list[str]
) -> None:
    target = tmp_path / "AGENTS.md"
    target.write_bytes(text.encode())
    assert remove_block(target) is False  # no block: the file is left alone
    assert target.read_bytes() == text.encode()

    apply_block(target, block(*notes))
    once = target.read_bytes()
    assert once.startswith(text.encode())  # the rest of the file is unchanged
    assert once.count(BLOCK_START.encode()) == text.count(BLOCK_START) + 1
    if "\r\n" in text:  # the block takes the file's line endings
        assert b"\n" not in once[len(text.encode()) :].replace(b"\r\n", b"")
    assert apply_block(target, block(*notes)) == "updated"
    assert target.read_bytes() == once

    assert remove_block(target) is True
    assert target.read_bytes() == text.encode()


@shared_fixture
@given(notes_files(), st.text(max_size=10), st.text(max_size=10))
def test_applying_new_notes_replaces_the_old_ones(
    tmp_path: Path, text: str, old: str, new: str
) -> None:
    target = tmp_path / "AGENTS.md"
    target.write_bytes(text.encode())
    apply_block(target, block(" ".join(old.split())))
    apply_block(target, block(" ".join(new.split())))
    replaced = target.read_bytes()
    target.write_bytes(text.encode())
    apply_block(target, block(" ".join(new.split())))
    assert replaced == target.read_bytes()


@shared_fixture
@example(before="", after="\n\nFirst line\n")  # it began with blank lines
@example(before="# Rules  \n", after="    indented code")  # spaces were stripped
@given(notes_files(), notes_files())
def test_unapply_keeps_every_line_around_a_block_moved_into_the_middle(
    tmp_path: Path, before: str, after: str
) -> None:
    nl = "\r\n" if "\r\n" in before + after else "\n"
    if before and not before.endswith("\n"):
        before += nl
    assume(after.strip(" \t\r\n"))
    target = tmp_path / "AGENTS.md"
    target.write_bytes(f"{before}{block('x').replace(chr(10), nl)}{after}".encode())
    assert remove_block(target) is True
    left = target.read_bytes().decode()

    def lines(text: str) -> list[str]:  # the lines that are not blank, exactly as written
        return [line for line in text.splitlines() if line.strip(" \t")]

    assert lines(left) == lines(before) + lines(after)
    assert left.endswith(after.splitlines(keepends=True)[-1])  # the end is not rewritten
    if not lines(before):  # no blank lines where the block was
        assert left.splitlines()[0].strip(" \t")


# ----------------------------------------------------------------- model answers
# Too deep for the parser of Python 3.13 (a RecursionError), which 3.10 reads; and too deep for
# every Python (a MemoryError).
_DEEP = "x = " + " + ".join(['"a"'] * 5000)
_NESTED = "x = " + "-" * 10_000 + "1"


def _parses(code: str) -> bool:
    try:
        ast.parse(code)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False
    return True


@example("x = 1\x00")  # a ValueError before Python 3.12
@example("```python\nx = 1\x00\n```")  # a fenced block is not parsed before it is checked
@example(_DEEP)
@example(f"Here it is:\n```python\n{_DEEP}\n```")
@example(_NESTED)
@example(f"```python\n{_NESTED}\n```")
@given(st.text())
def test_any_model_answer_is_code_or_no_code(answer: str) -> None:
    # A model's answer is cached before it is read, so a crash here came back on every run.
    code = extract_code(answer)
    if code is not None:
        assert code.endswith("\n")
        # A note's example reaches the checker even when it is no code at all.
        assert _prepare(code, ("toylib",)).syntax_ok is _parses(code)


_CHECKER_OFF = st.sampled_from(
    ["# type: ignore", "# pyright: ignore[reportCallIssue]", "#mypy: ignore-errors", "# ok"]
)
# Code with a NUL byte cannot be tokenized or parsed: it is scored as a syntax error anyway.
_LITERAL = st.text(st.characters(exclude_characters='"\\\r\n\x00', codec="utf-8"), max_size=8)


@st.composite
def snippets(draw: st.DrawFn) -> str:
    """Code a model might write: statements with string literals holding any character (a
    form feed, U+2028), lines of a form feed alone, and comments that turn the checker off."""
    lines = []
    for i in range(draw(st.integers(1, 5))):
        line = draw(st.sampled_from(["\x0c", f'x{i} = "{draw(_LITERAL)}"', f"f{i}()"]))
        if draw(st.booleans()):
            line += "  " + draw(_CHECKER_OFF)
        lines.append(line + draw(st.sampled_from(["\n", "\r\n"])))
    return "".join(lines)


def _checker_comments(code: str) -> list[str]:
    tokens = tokenize.generate_tokens(io.StringIO(code).readline)
    return [t.string for t in tokens if t.type == tokenize.COMMENT and "ignore" in t.string]


@example('s = "\u2028"\nf()  # type: ignore\n')  # the comment after it stayed
@example("\x0c\nf()  # type: ignore\n")
@given(snippets())
def test_generated_code_cannot_turn_the_checker_off(code: str) -> None:
    clean = sanitize(code)
    assert _checker_comments(clean) == []
    assert ast.dump(ast.parse(clean)) == ast.dump(ast.parse(code))  # the same code otherwise


# ----------------------------------------------------------------- MCP symbols
@example("client.messages.create(model=m))")  # one ")" too many
@given(st.text())
def test_any_symbol_parses_into_plain_dotted_names(symbol: str) -> None:
    terms = _symbol_terms(symbol)
    for segments in terms:
        assert segments
        for s in segments:
            assert s and not any(ch.isspace() or ch in "`().," for ch in s), s
    # The case-keeping parse lines up with it term by term (_matching zips the two).
    kept = _symbol_terms(symbol, keep_case=True)
    assert [[s.lower() for s in t] for t in kept] == terms


_IDENT = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,6}", fullmatch=True)
_DOTTED = st.lists(_IDENT, min_size=1, max_size=4).map(".".join)


def _arguments() -> st.SearchStrategy[str]:
    """What an agent writes between the parentheses: names, strings, keywords, nested calls."""
    leaf = st.sampled_from(["", "x", "1", "'a, b'", "model=m", "*args", "**kw", "[1, 2]"])
    return st.recursive(
        leaf,
        lambda inner: st.one_of(
            st.lists(inner, min_size=1, max_size=3).map(", ".join),
            st.tuples(_DOTTED, inner).map(lambda t: f"{t[0]}({t[1]})"),
        ),
        max_leaves=6,
    )


@given(st.lists(st.tuples(_DOTTED, st.none() | _arguments()), min_size=1, max_size=3))
def test_call_syntax_is_stripped_from_every_symbol(calls: list[tuple[str, str | None]]) -> None:
    written = " , ".join(
        f"`await {path}({args})`" if args is not None else path for path, args in calls
    )
    assert _symbol_terms(written) == [path.lower().split(".") for path, _ in calls]


_CHANGES = [
    APIChange(
        "anthropic", "0.60", "1.8", REMOVED, "anthropic.resources.Completions", "Completions"
    ),
    APIChange(
        "anthropic",
        "0.60",
        "1.8",
        PARAM_REMOVED,
        "anthropic.resources.Messages.create",
        "create",
        owner="Messages",
        parameter="temperature",
        also=["anthropic.Messages.create"],
    ),
    APIChange(
        "anthropic",
        "0.60",
        "1.8",
        MOVED,
        "anthropic.types.Usage",
        "Usage",
        moved_to="anthropic.types.usage.Usage",
    ),
]


@given(st.text() | st.lists(_DOTTED | _IDENT, max_size=3).map(",".join))
def test_the_symbol_filter_never_fails(symbol: str) -> None:
    found, _loose = _matching(_CHANGES, symbol)
    assert all(any(f is c for c in _CHANGES) for f in found)
    old, new = (
        Release("0.60", datetime.now(timezone.utc), False, ()),
        Release("1.8", datetime.now(timezone.utc), False, ()),
    )
    assert symbol in _no_match(symbol, old, new, len(_CHANGES))


# ------------------------------------------------------- versions at a cutoff
_VERSION = st.builds(
    lambda release, pre, post, dev: f"{release}{pre}{post}{dev}",
    st.lists(st.integers(0, 3), min_size=1, max_size=3).map(lambda p: ".".join(map(str, p))),
    st.sampled_from(["", "", "a1", "b2", "rc1"]),
    st.sampled_from(["", "", ".post1"]),
    st.sampled_from(["", "", "", ".dev0"]),
)
_DAY = st.dates(date(2024, 1, 1), date(2024, 3, 31))
_UPLOAD = st.builds(
    lambda d, t: datetime.combine(d, t).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    _DAY,
    st.times(),
)
_FILES = st.lists(
    st.fixed_dictionaries({"upload_time_iso_8601": _UPLOAD, "yanked": st.booleans()}),
    max_size=3,
)


class _Index(PyPI):
    """PyPI whose project metadata is given, so that releases() reads it as from PyPI."""

    def __init__(self, releases: dict[str, list[dict[str, Any]]]) -> None:
        super().__init__(DiskCache(enabled=False))
        self._doc = {"info": {"name": "pkg"}, "releases": releases}

    def project(self, name: str) -> dict[str, Any]:
        return self._doc


def _utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def _expected(releases: list[Release], when: date) -> Release | None:
    """What version_at promises: the highest final release published by then, never a yanked
    one; for a package that had only pre-releases, the highest of those, never a dev release."""
    public = [r for r in releases if not r.yanked and r.uploaded.date() <= when]
    finals = [r for r in public if r.is_final]
    pres = [r for r in public if not r.parsed.is_devrelease]
    pool = finals or pres
    return max(pool, key=lambda r: r.parsed) if pool else None


@given(st.dictionaries(_VERSION, _FILES, max_size=8), _DAY, _DAY)
def test_the_version_at_a_cutoff_is_the_newest_one_published_by_then(
    releases: dict[str, list[dict[str, Any]]], when: date, later: date
) -> None:
    when, later = min(when, later), max(when, later)
    pypi = _Index(releases)
    known = pypi.releases("pkg")
    for r in known:  # a release is yanked when all its files are, and dated by its first one
        files = releases[r.version]
        assert r.yanked == all(f["yanked"] for f in files)
        assert r.uploaded == min(_utc(f["upload_time_iso_8601"]) for f in files)

    at = pypi.version_at("pkg", when)
    expected = _expected(known, when)
    assert (at and at.parsed) == (expected and expected.parsed)
    if at is not None:
        assert at.uploaded.date() <= when and not at.yanked and not at.parsed.is_devrelease

    # A later cutoff never goes back to an older release, except that the first final
    # release replaces the pre-releases before it (which may be numbered higher).
    then = pypi.version_at("pkg", later)
    if at is not None:
        assert then is not None
        assert then.parsed >= at.parsed or (then.is_final and not at.is_final)
        if at.is_final:
            assert then.is_final


# ---------------------------------------------------------- archive path guard
# Member names in both spellings: "/" and "\" separators, "..", absolute paths, drive
# letters, UNC shares, NTFS streams, and names Windows trims ("x. ", "...", ".. "). Letters
# stay within "abC", which spell no device name (CON, NUL, AUX, ...). zipfile and tarfile cut
# a name at a NUL byte, so there is none.
_PARTS = st.sampled_from(
    ["a", "b.py", "..", ".", "", " ", "...", ".. ", " ..", "a. ", "C:", "c:a", "~", "a b"]
) | st.text(st.sampled_from("abC.: ~$*?"), min_size=1, max_size=4)
_ROOTS = st.sampled_from(["", "", "/", "\\", "//", "\\\\", "C:", "C:/", "C:\\", "\\\\?\\C:\\"])


@st.composite
def archive_members(draw: st.DrawFn) -> str:
    parts = draw(st.lists(_PARTS, min_size=1, max_size=5))
    seps = draw(st.lists(st.sampled_from(["/", "\\"]), min_size=len(parts), max_size=len(parts)))
    name = draw(_ROOTS) + "".join(p + s for p, s in zip(parts, seps, strict=True))[:-1]
    return name + draw(st.sampled_from(["", ".py", "/x.py"]))


@pytest.fixture
def extraction_dir(tmp_path: Path) -> Path:
    """A directory deep enough that a "../" or two still lands inside tmp_path."""
    dest = tmp_path / "a" / "b" / "dest"
    dest.mkdir(parents=True)
    return dest


def _inside(path: str, directory: str) -> bool:
    return os.path.commonpath([path, directory]) == directory


@shared_fixture
@example(".../x.py")  # a folder Windows cannot delete
@example("toy./x.py")  # Windows writes it as toy/x.py
@given(archive_members())
def test_no_archive_member_lands_outside_the_extraction_directory(
    extraction_dir: Path, member: str
) -> None:
    target = _safe_target(extraction_dir, member)
    parts = PurePosixPath(member.replace("\\", "/")).parts
    if (
        member.startswith(("/", "\\"))
        or PureWindowsPath(member).drive
        or any(":" in p or p.endswith((".", " ")) for p in parts)  # "..", NTFS streams
    ):
        assert target is None
    if target is None:
        return
    assert target == extraction_dir.joinpath(*parts)  # the member's own name, nothing else
    # Where the system itself puts that path, without looking at the disk (resolve() would):
    # Windows drops trailing dots and spaces, for one.
    assert _inside(os.path.abspath(target), os.path.abspath(extraction_dir))  # noqa: PTH100

    # And writing it as extraction does creates nothing outside (or fails, which makes the
    # extraction a "could not extract" error).
    top = extraction_dir.parent.parent.parent
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"")
    except (OSError, ValueError):
        pass
    outside = [p for p in top.rglob("*") if not _inside(str(p), str(extraction_dir))]
    assert sorted(outside) == [top / "a", top / "a" / "b"]
    shutil.rmtree(extraction_dir)
    extraction_dir.mkdir()
