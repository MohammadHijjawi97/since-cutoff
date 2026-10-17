---
layout: default
title: Notes, docs or neither? A coding-agent benchmark
description: 360 Claude Code sessions on 24 Python tasks, 17 of them on library changes made after the model's training cutoff, run alone, with Context7, with since-cutoff notes, with both, and with Context7 use required. Protocol frozen before the main run.
---

*Mohammad Hijjawi · September 2026 ·
[benchmark repository](https://github.com/MohammadHijjawi97/since-cutoff-benchmark) ·
[since-cutoff on GitHub](https://github.com/MohammadHijjawi97/since-cutoff)*

*This page reports the study as it was run in September 2026, with the notes that since-cutoff
0.4.1 wrote. Later versions write different notes (0.5.0, for example, adds dependency switches
such as `httpx` to `httpx2`), so these numbers belong to 0.4.1; a run with a later version would
be a separate, labelled study.*

## Summary

In 360 headless Claude Code sessions (model `claude-opus-5-5`) on 24 small Python tasks, putting
since-cutoff 0.4.1's notes in `CLAUDE.md` made the 17 post-cutoff tasks cheaper and shorter to
solve than Claude Code alone: cost ratio 0.80 (95% CI 0.70 to 0.90), turns 0.83 (0.74 to 0.91)
and wall time 0.87 (0.79 to 0.95), which meets the pre-registered efficiency rule. Pass rates cannot
separate the arms: Claude Code alone already passed 94.1% of the post-cutoff tasks, above the
pre-registered 90% ceiling, so this benchmark makes no claim that the notes make the agent succeed
more often. The one task where outcomes differed, ant01 (0 of 3 runs in A and B; 3 of 3 in C, D
and E, where E has no notes), is a task whose note wording was changed after the pilot; with ant01
and the other pilot task removed, every arm passes every post-cutoff task and the cost ratio is 0.78
(0.68 to 0.88).

## The question

A coding model learned its libraries before its training cutoff. When a project pins a newer
release, the model's memory of that API can be wrong. since-cutoff writes short notes about such
changes into `AGENTS.md` or `CLAUDE.md`, from a static diff of the package's public API between
the release current at the model's cutoff and the pinned release. The other common answer is a
documentation server such as [Context7](https://github.com/upstash/context7), which the agent
queries while it works.

The pre-registered question ([PROTOCOL.md section 1](https://github.com/MohammadHijjawi97/since-cutoff-benchmark/blob/main/bench/PROTOCOL.md#1-question)):
on the same tasks, does Claude Code finish correctly more often, or with fewer correction attempts,
less time and less cost, with its usual tools, with Context7, with since-cutoff's notes, or with
both? A fifth arm, with Context7 use required, was added after the pilot.

## Design

### Arms

| arm | added to Claude Code's own tools |
|---|---|
| **A** | nothing |
| **B** | Context7 MCP server (`@upstash/context7-mcp` 4.1.1) and Context7's own recommended rule in `CLAUDE.md`, verbatim: "Always use Context7 when I need library/API documentation, code generation, setup or configuration steps without me having to explicitly ask." |
| **C** | since-cutoff 0.4.1's notes for the project's packages in `CLAUDE.md`, generated and frozen before the main run |
| **D** | B and C together |
| **E** | Context7 with a rule that requires a lookup before any library code: call `resolve-library-id`, then `query-docs` for the classes, functions and parameters about to be used, "even when you think you know the API" (added after the pilot, [section 12.z](https://github.com/MohammadHijjawi97/since-cutoff-benchmark/blob/main/bench/PROTOCOL.md#12z-pilot-outcome-and-decisions-2026-09-28-before-the-main-run)) |

Every arm had the same base `CLAUDE.md` block (dependencies are pinned and installed in `.venv`,
run `pytest -q`, do not install or upgrade packages), so having a `CLAUDE.md` is not what differs.

Fixed for every session: Claude Code 2.1.283 in headless mode (`claude -p`), `--model
claude-opus-5-5 --effort medium`, at most 40 turns, a $3 budget and 15 minutes; tools Bash, Read,
Edit, Write, Glob, Grep, TodoWrite and Skill; WebFetch, WebSearch, pip, uv, curl, wget, npm and npx
denied. Each session ran in a fresh project folder with a fresh virtual environment built offline
from locked, hashed wheels (Python 3.13, Windows 11). The runs used the maintainer's Claude login
(`apiKeySource` = none in every session) with `--setting-sources project`, so no user settings,
hooks or plugins were loaded. Context7 ran without an API key. Three sessions ran in parallel, in an
order randomised in blocks (seed 20260929).

### Tasks and strata

Each task is a small repository with realistic, working use of one library, an instruction, visible
tests and hidden tests. A task was admitted only when `python -m bench validate` passed: the
unmodified repository fails the hidden tests at the pinned version; a reference solution passes;
a solution written the cutoff-era way fails at the pinned version and passes at the cutoff-era
version; and no answer token appears in the repository or the instruction. Every task and hidden
test was reviewed in a separate Claude Code session that did not write the tasks, following plan
B.4; that session ran `claude-opus-5-5`, the model under test
([REVIEW.md](https://github.com/MohammadHijjawi97/since-cutoff-benchmark/blob/main/REVIEW.md)).
The model's training cutoff was taken as 2026-06-30.

| stratum | tasks | package: pinned (release at the cutoff) | role |
|---|---|---|---|
| post-cutoff (primary endpoint) | 17: ant01-ant05, oai01-oai03, hf01-hf04, mcp01, mcp02, mcp04-mcp06 | anthropic 1.8.0 (0.115.0), openai 3.19.2 (2.44.0), huggingface-hub 2.0.0 (1.21.0), mcp 2.2.0 (1.28.1) | the API the task needs changed after the cutoff |
| pre-cutoff control | 5: pd01-pd03, lc01, lc02 | pandas 3.0.6 (3.0.3), langchain-core 1.6.5 (1.4.8) | the change predates the cutoff: do the notes get in the way? |
| no-change control | 1: oai04 | openai 3.19.2 (2.44.0) | nothing the task needs changed |
| pinned-behind | 1: oai05 | openai 2.44.0, behind the latest 3.19.2 | oai01 with an older pin: the newer answer fails |

Two planned post-cutoff tasks (ant06, mcp03) and one planned control (hf05) failed the premise
check and were dropped with a published reason, so the primary endpoint has 17 tasks instead of the
19 the plan assumed.

### What "pass" means

After the session the project is copied into a fresh folder, a clean virtual environment is built
from the task's lock, and the hidden tests run with harness-owned pytest options, so the agent's own
pytest configuration or `conftest.py` cannot change the result. Credentials are removed from the
environment and the network is blocked except loopback. **A run passes when every hidden test
passes and the pins are unchanged** (no lock or requirements file and no dependency field in
`pyproject.toml` changed). Runs that hit a limit are graded as they stand. Infrastructure failures
are recorded, not graded, and re-run (at most 3 attempts). The unit of analysis is the task: its
pass rate in an arm is passes out of 3.

### Pre-registration

- **Frozen before the main run:** `bench/PROTOCOL.md` (question, endpoints, decision rules),
  the analysis code, the task hashes (hidden tests by sha256 only), the environment lock hashes and
  the notes, committed and tagged
  [`main-freeze-2026-09-28`](https://github.com/MohammadHijjawi97/since-cutoff-benchmark/tree/main-freeze-2026-09-28)
  (commit `de23b80`, 03:24:52 UTC) before the main plan was written (03:25:13 UTC). Hashes in
  `bench/HASHES.json`: harness `9227ebaf60e2…`, configuration `06b0642dc85a…`, notes manifest
  `f7e0b28399773c60…`. `PROTOCOL.md` at the tag has sha256
  `c7ce737acc7c63fa0f2d19800175c0e4576a5dd6acc1376d0e842b1b37a6f621` (the file as stored in git).
  The freeze is an unsigned git tag in a repository that stayed private, with no remote, until the
  results were published. Its times are the author's git timestamps and cannot be checked
  independently.
- **Seeds:** plan 20260929, fixed after the pilot and before the plan; bootstrap 20261001 with
  10,000 resamples, in `bench/bench.toml` since the first harness commit.
- **Notes:** generated by the released since-cutoff 0.4.1 from PyPI with
  `sync --yes --scope imported`, its default budget of 5 APIs per package, in a copy of each task's
  repository with no access to hidden tests or solutions; frozen, and checked by hash in every run.
  A non-empty block is about 285 to 505 tokens. They cover the task's change in **5 of the 17**
  post-cutoff tasks (ant01, ant03, hf01, hf02, hf03) and part of it in mcp01. The switch from
  `httpx` to `httpx2` behind ant04, hf04 and oai01-oai03 is a dependency change, not a change to the
  package's own API, so a static API diff does not report it. oai05 and pd01-pd03 get an empty
  block, so C equals A there
  ([section 12.w](https://github.com/MohammadHijjawi97/since-cutoff-benchmark/blob/main/bench/PROTOCOL.md#12w-main-run-notes-frozen-2026-09-28-before-the-main-plan)).
- **Analysis:** primary endpoint Δ(C−A), the mean over post-cutoff tasks of the pass-rate
  difference, with a task-bootstrap 95% CI and an exact sign-flip test, Holm-adjusted with D−B, B−A
  and D−C. Cost, turns, wall time and tokens: geometric mean of the per-task median ratios, with
  task-bootstrap CIs. Controls: non-inferiority of C−A with a −10 point margin. Decision rules and
  the ceiling rule are in
  [section 8](https://github.com/MohammadHijjawi97/since-cutoff-benchmark/blob/main/bench/PROTOCOL.md#8-decision-rules-plan-b8-applied-automatically-in-report-section-h).
- **Added after the pilot, before the freeze** (section 12.z): arm E, the E row in the decision
  rules, and the rule that the report is published twice, with all tasks and without the two pilot
  tasks, and that a claim counts as supported only if it holds in both.
- **Pilot:** 16 sessions on ant01 and mcp01 (seed 20260928, since-cutoff 0.4.0 notes), excluded
  from every analysis ([pilot report](https://github.com/MohammadHijjawi97/since-cutoff-benchmark/blob/main/results/pilot-2026-09-28/report.md)).

## Results

All 360 planned sessions have a valid record (361 attempts: one attempt was recorded as a harness
error because the grading environment could not be built, and was re-run as the protocol
prescribes). No session hit a limit, no session changed a lock file or the virtual environment, and
no task had mixed outcomes across its 3 runs in any arm. Context7 was connected, without server
errors, in all 216 B, D and E sessions. Everything below comes from
[`report.json`](https://github.com/MohammadHijjawi97/since-cutoff-benchmark/blob/main/results/main-2026-09-28/report.json)
and [`report.md`](https://github.com/MohammadHijjawi97/since-cutoff-benchmark/blob/main/results/main-2026-09-28/report.md)
unless marked otherwise.

### (a) Pass rates

Mean per-task pass rate, task-bootstrap 95% CI:

| stratum | A | B | C | D | E |
|---|---|---|---|---|---|
| post-cutoff (17 tasks) | 94.1% [82.4%, 100.0%] | 94.1% [82.4%, 100.0%] | 100.0% [100.0%, 100.0%] | 100.0% [100.0%, 100.0%] | 100.0% [100.0%, 100.0%] |
| pre-cutoff control (5) | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| no-change control (1) | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| pinned-behind (1) | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |

Paired contrasts on the post-cutoff tasks:

| contrast | Δ pass rate | 95% CI | sign-flip p | Holm p | tasks fixed / broken (majority of 3), McNemar p |
|---|---|---|---|---|---|
| C−A (primary) | +5.9 pp | [+0.0, +17.6] | 1.000 | 1.000 | 1 / 0, 1.000 |
| D−B | +5.9 pp | [+0.0, +17.6] | 1.000 | 1.000 | 1 / 0, 1.000 |
| B−A | +0.0 pp | [+0.0, +0.0] | 1.000 | 1.000 | 0 / 0, 1.000 |
| D−C | +0.0 pp | [+0.0, +0.0] | 1.000 | 1.000 | 0 / 0, 1.000 |
| C−B (extra) | +5.9 pp | [+0.0, +17.6] | 1.000 | n/a | 1 / 0, 1.000 |
| C−E (extra) | +0.0 pp | [+0.0, +0.0] | 1.000 | n/a | 0 / 0, 1.000 |
| E−B (extra) | +5.9 pp | [+0.0, +17.6] | 1.000 | n/a | 1 / 0, 1.000 |

Every +5.9 point difference is the same single task, ant01. On the 7 control tasks, C−A and D−A
are +0.0 points with a one-sided 95% lower bound of +0.0 points (margin −10): non-inferior. Four of
the 7 (oai05, pd01, pd02, pd03) have an empty note block, so C's input equals A's there; the notes
were present in 3 (lc01, lc02, oai04).

### (b) Cost, turns, time and tokens

Median over the 17 post-cutoff tasks of each task's median session:

| metric | A | B | C | D | E |
|---|---|---|---|---|---|
| cost (USD) | 0.38 | 0.37 | 0.33 | 0.32 | 0.36 |
| turns | 20 | 19 | 18 | 17 | 19 |
| wall time (s) | 62.3 | 62.6 | 56.1 | 61.5 | 73.5 |
| output tokens | 4,873 | 4,460 | 4,264 | 4,035 | 4,960 |
| total tokens | 360,830 | 308,233 | 254,316 | 254,739 | 327,889 |

Ratios (geometric mean of per-task median ratios, task-bootstrap 95% CI); below 1 means the first
arm used less:

| metric | C/A | C/B | C/E | D/B | E/B |
|---|---|---|---|---|---|
| cost | 0.80 [0.70, 0.90] | 0.85 [0.74, 0.96] | 0.95 [0.78, 1.19] | 0.73 [0.59, 0.88] | 0.89 [0.75, 1.05] |
| turns | 0.83 [0.74, 0.91] | 0.90 [0.80, 1.02] | 0.82 [0.75, 0.90] | 0.86 [0.73, 1.00] | 1.09 [0.97, 1.23] |
| wall time | 0.87 [0.79, 0.95] | 0.88 [0.80, 0.96] | 0.80 [0.72, 0.90] | 0.95 [0.86, 1.05] | 1.10 [0.98, 1.20] |
| output tokens | 0.82 [0.73, 0.91] | 0.84 [0.72, 0.96] | 0.81 [0.74, 0.90] | 0.89 [0.78, 1.01] | 1.03 [0.92, 1.15] |
| total tokens | 0.73 [0.59, 0.87] | 0.74 [0.63, 0.87] | 0.79 [0.64, 0.99] | 0.69 [0.53, 0.87] | 0.94 [0.77, 1.13] |

C/A and D/B belong to the pre-registered contrasts (with B/A: cost 0.95 [0.87, 1.02], and D/C:
cost 0.86 [0.67, 1.05]); C/B, C/E and E/B are the extra contrasts added with arm E. None of these
intervals is adjusted for multiplicity. Cost is the `total_cost_usd` that Claude Code reports for
the session, at API prices; it is not what the subscription login was billed. Total tokens include
cache reads, which make up most of them.

Cost per solved post-cutoff task (total cost divided by passes, failed runs included): A $0.45
(48/51), B $0.43 (48/51), C $0.34 (51/51), D $0.32 (51/51), E $0.36 (51/51).

Correction attempts did not change: failing `pytest` runs per session were 0.14 in A and 0.14 in C
(ratio with a +1 offset 1.00 [1.00, 1.00]; Wilcoxon p = 1.000).

### (c) Process metrics

Post-cutoff tasks, 51 sessions per arm:

| metric | A | B | C | D | E |
|---|---|---|---|---|---|
| stale API in the first edit of a target file | 11.8% | 11.8% | 2.0% | 3.9% | 2.0% |
| stale API in the final diff | 13.7% | 11.8% | 2.0% | 5.9% | 5.9% |
| sessions that called Context7 | 0.0% | 19.6% | 0.0% | 2.0% | 100.0% |
| Context7 calls per session | 0.00 | 0.35 | 0.00 | 0.04 | 2.12 |
| reads of `.venv`/site-packages per session | 5.94 | 5.14 | 4.22 | 4.84 | 5.65 |
| `pytest` runs per session | 1.29 | 1.16 | 1.24 | 1.25 | 1.25 |
| failing `pytest` runs per session | 0.14 | 0.08 | 0.14 | 0.10 | 0.12 |
| sessions that used the bundled `claude-api` skill | 27.5% | 23.5% | 21.6% | 15.7% | 9.8% |

"Stale API" means the task's regular expressions for the cutoff-era API matched the code (Python
comments ignored); it is a pattern match, not a test failure. In counts, the first edit was stale in
6 of 51 A sessions (ant01 three times, ant04 twice, hf02 once) and 1 of 51 C sessions (oai02). These
rates are descriptive; the pre-registered test that uses them (next section) was not met.

With its own recommended rule, the agent called Context7 in 10 of 51 post-cutoff sessions in B
(mcp01 and mcp06 three times each; ant04, hf01, hf04 and oai01 once each) and in 1 of 51 in D. In E
it called Context7 in every session, but from 06:25:53 UTC to the end of the run every Context7
call returned a "Monthly quota exceeded" message instead of documentation; E received documentation
in 34 of its 51 post-cutoff sessions (see [deviations and incidents](#deviations-and-incidents)).

### (d) Per task

Passes out of 3 runs per arm:

| task | package | what changed at the pin | A | B | C | D | E | notes cover it |
|---|---|---|---|---|---|---|---|---|
| ant01 | anthropic 1.8.0 | `Messages.create()` no longer takes `temperature`, `top_k`, `top_p` | 0/3 | 0/3 | 3/3 | 3/3 | 3/3 | yes |
| ant02 | anthropic 1.8.0 | raw response `.text` is now a method | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | no |
| ant03 | anthropic 1.8.0 | beta `Messages.create()` no longer takes `output_format` | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | yes |
| ant04 | anthropic 1.8.0 | the SDK moved from `httpx` to `httpx2` and rejects `httpx` objects | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | no |
| ant05 | anthropic 1.8.0 | async raw-response `parse()` must be awaited | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | no |
| hf01 | huggingface-hub 2.0.0 | `text_generation(stop_sequences=)` removed, use `stop` | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | yes |
| hf02 | huggingface-hub 2.0.0 | `HfApi.upload_large_folder` removed | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | yes |
| hf03 | huggingface-hub 2.0.0 | `list_models(model_name=)` removed, use `search` | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | yes |
| hf04 | huggingface-hub 2.0.0 | HTTP client and its errors moved from `httpx` to `httpx2` | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | no |
| mcp01 | mcp 2.2.0 | `FastMCP` renamed `MCPServer`; tool errors need `ToolError` | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | partly (the rename) |
| mcp02 | mcp 2.2.0 | `isError`, `structuredContent` renamed `is_error`, `structured_content` | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | no |
| mcp04 | mcp 2.2.0 | host and port moved off the server constructor | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | no |
| mcp05 | mcp 2.2.0 | `streamablehttp_client` removed, use `streamable_http_client` | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | no (ranked sixth; the budget is 5) |
| mcp06 | mcp 2.2.0 | in-memory `create_connected_server_and_client_session` removed | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | no |
| oai01 | openai 3.19.2 | custom HTTP client: `httpx` replaced by `httpx2` | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | no |
| oai02 | openai 3.19.2 | `httpx2` breaks a respx test double | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | no |
| oai03 | openai 3.19.2 | stream transport errors now raised as SDK errors; `httpx2` | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | no |
| oai04 | openai 3.19.2 | nothing the task needs (no-change control) | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | n/a |
| oai05 | openai 2.44.0 | pinned behind: the 3.x answer fails | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | empty block |
| pd01 | pandas 3.0.6 | string dtype and Copy-on-Write (before the cutoff) | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | empty block |
| pd02 | pandas 3.0.6 | `DataFrame.applymap` removed (before the cutoff) | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | empty block |
| pd03 | pandas 3.0.6 | `fillna(method=)` removed (before the cutoff) | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | empty block |
| lc01 | langchain-core 1.6.5 | `get_relevant_documents` removed (before the cutoff) | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | no |
| lc02 | langchain-core 1.6.5 | `predict` and `__call__` removed (before the cutoff) | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | no |

### (e) Pre-registered claim checks

As the report states them (section (h) of each report). In the harness, the four pass-rate rows and
the E row also require that the ceiling rule is not met, so none of them can be met in this run.

| rule | evidence (all 24 tasks) | all tasks | without ant01, mcp01 |
|---|---|---|---|
| ceiling: A ≥ 90% on post-cutoff tasks → no pass-rate claim | A = 94.1% | **MET** | **MET** (A = 100.0%) |
| notes reduce stale-API failures: C−A CI lower > 0, Holm p < .05, lower stale first edit in C | C−A +5.9 pp [+0.0, +17.6], Holm p 1.000; stale first edit A 11.8% vs C 2.0% | not met | not met |
| notes add value on top of documentation access: D−B CI lower > 0 | D−B +5.9 pp [+0.0, +17.6] | not met | not met |
| notes are cheaper than doc lookups for the same success: C−B CI within ±10 pp and C/B cost CI upper < 1 | C−B [+0.0, +17.6]; C/B cost 0.85 [0.74, 0.96] | not met | not met |
| notes are cheaper than documentation that is looked up (arm E): C−E CI within ±10 pp and C/E cost CI upper < 1 | C−E [+0.0, +0.0]; C/E cost 0.95 [0.78, 1.19]; E Context7 call rate 100.0% | not met | not met |
| notes and Context7 complement each other: D ≥ max(B, C) and D/B cost ≤ 1 | B 94.1%, C 100.0%, D 100.0%; D/B cost 0.73 | not met | not met |
| notes don't hurt: one-sided 95% lower bound of C−A on controls ≥ −10 pp | 7 control tasks (notes non-empty in 3 of 7), C−A +0.0 pp, lower +0.0 pp | **MET** | **MET** |
| notes reduce correction attempts / time / cost: a C/A ratio CI below 1 | cost 0.80 [0.70, 0.90]; turns 0.83 [0.74, 0.91]; wall time 0.87 [0.79, 0.95]; failing pytest runs (+1) 1.00 [1.00, 1.00] | **MET** | **MET** |
| low Context7 use (B call rate < 80%): C vs B compares notes with an unused tool | B call rate 19.6% | **MET** | **MET** (15.6%) |
| per-task coverage decides the wording | C beat A only on ant01, a covered task | descriptive | descriptive |

Two claims hold in both analyses: the notes did not lower pass rates on the control tasks (3 of
the 7 had non-empty notes), and the notes reduced time and cost. The second rule is met when any
one of four C/A intervals lies below 1; here three did (cost, turns, wall time) and the fourth,
failing `pytest` runs, did not move. Without the ceiling, the C/B row would have been met in the
sensitivity analysis (C−B [+0.0, +0.0], C/B cost 0.83 [0.71, 0.95]) and the complement row in
both; the C/E row fails on cost regardless, because its interval reaches 1.19 (1.22 without ant01
and mcp01).

### (f) Sensitivity analysis without the pilot tasks

ant01 and mcp01 were run in the pilot, and since-cutoff's note wording was changed after seeing
ant01's pilot runs, so the protocol requires the report again without both
([`report.without-ant01-mcp01.md`](https://github.com/MohammadHijjawi97/since-cutoff-benchmark/blob/main/results/main-2026-09-28/report.without-ant01-mcp01.md)):
15 post-cutoff tasks, 330 sessions. Every arm passes every post-cutoff task (100.0%), so all
pass-rate contrasts are +0.0 points.

| metric | C/A | C/B | C/E | D/B | E/B |
|---|---|---|---|---|---|
| cost | 0.78 [0.68, 0.88] | 0.83 [0.71, 0.95] | 0.94 [0.75, 1.22] | 0.71 [0.56, 0.87] | 0.88 [0.72, 1.06] |
| turns | 0.80 [0.71, 0.89] | 0.86 [0.77, 0.97] | 0.81 [0.74, 0.89] | 0.84 [0.71, 0.97] | 1.07 [0.95, 1.18] |
| wall time | 0.86 [0.77, 0.95] | 0.87 [0.78, 0.95] | 0.79 [0.70, 0.90] | 0.95 [0.85, 1.05] | 1.10 [0.97, 1.22] |
| output tokens | 0.79 [0.70, 0.88] | 0.80 [0.69, 0.91] | 0.80 [0.72, 0.88] | 0.86 [0.75, 0.98] | 1.01 [0.90, 1.11] |
| total tokens | 0.69 [0.56, 0.84] | 0.70 [0.59, 0.81] | 0.78 [0.61, 1.00] | 0.66 [0.50, 0.85] | 0.90 [0.73, 1.10] |

Stale API in the first edit: A 6.7%, B 6.7%, C 2.2%, D 4.4%, E 2.2%. B called Context7 in 15.6% of
sessions. Cost per solved task: A $0.42, B $0.41, C $0.34, D $0.32, E $0.36. The claim checks come
out the same as with all tasks.

## What this means, and what it does not

**The agent rarely needed help to succeed.** Claude Code alone passed 16 of the 17 post-cutoff tasks
in all three runs. The virtual environment holds the pinned package, and in 45 of 51 post-cutoff
sessions the agent read files under `.venv`/site-packages (5.94 reads per session on average), so it
could see the current API instead of relying on memory. In this setting the benchmark mostly
measures how much work a correct answer takes, not whether the agent gets there.

**The notes made that work smaller.** With the notes, sessions on post-cutoff tasks cost less and
took fewer turns and less time, in both analyses (cost 0.80 [0.70, 0.90] with all tasks, 0.78
[0.68, 0.88] without the pilot tasks), and read the installed package less often (4.22 reads per
session against 5.94). The first edit used the cutoff-era API less often (1 of 51 sessions against 6
of 51), which is descriptive only. Why the notes save work is not established here. The saving is
not confined to the tasks whose change the notes named: in an unplanned, descriptive split computed
from `runs.csv` with the report's own ratio function and seed, C/A cost was 0.75 [0.52, 0.99] on the
5 covered tasks and 0.83 [0.75, 0.91] on the 12 others. The notes' header tells the agent which of
its packages changed after its cutoff, which may shorten the search even when the list does not
name the change; this benchmark cannot tell whether that, or something else, is the reason.

**ant01, stated plainly.** ant01 asks for a classification call with temperature 0; anthropic 1.8.0
removed `temperature` from `Messages.create()`'s signature, and the request field is still reachable
through `extra_body`. In A and B, all six runs wrote `temperature=0` as remembered, read nothing from
the installed package, passed the visible test (which does not exercise that call) and failed the
hidden test with a `TypeError`. C, D and E passed 3 of 3. In the pilot, since-cutoff 0.4.0's note
said not to pass these parameters, every C and D run followed it, and those runs failed. since-cutoff
0.4.1 states the removal and, where the pinned method has an `extra_body` argument, points to it. That
rule is generic, but it was written after seeing ant01's pilot runs, so ant01 is not independent
evidence for the notes; without it (and mcp01), every arm passes every post-cutoff task. Arm E, which
has no notes, also passed ant01 3 of 3: each E run read the installed package twice, including one
whose only Context7 call returned the quota message.

**Context7.** With its own recommended rule (B), the agent called Context7 in 19.6% of post-cutoff
sessions, and B showed no clear difference from A in pass rate (B−A +0.0 points) or cost (B/A
0.95 [0.87, 1.02]). With lookups required (E), Context7 was called in every session and E passed as
often as C. C took fewer turns and less time than E (0.82 [0.75, 0.90] and 0.80 [0.72, 0.90]); the
cost difference is unclear (0.95 [0.78, 1.19]). These are extra, unadjusted contrasts, and E's
lookups returned only a quota message in the last part of the run, so they do not measure the
quality of Context7's documentation. Notes and Context7 together (D) cost less than Context7 alone
(D/B 0.73 [0.59, 0.88]), but in D the agent called Context7 in only 1 of 51 post-cutoff sessions;
D's cost did not differ clearly from C's (D/C 0.86 [0.67, 1.05]), and its sessions took somewhat
longer (wall time D/C 1.08 [1.00, 1.19]).

**What it does not show.**

- No pass-rate effect in either direction: the ceiling rule was met. The pilot's warning for it did
  not fire (A passed 2 of 4 pilot runs), so the main run was not designed around a ceiling.
- One model (`claude-opus-5-5` at medium effort), one agent (Claude Code 2.1.283), one operating
  system (Windows), 24 small tasks with a median of about 20 turns and one minute, and 3 runs per
  task and arm. Other models, especially ones with older cutoffs, other agents, larger repositories,
  or settings where the agent cannot read the installed package may behave differently.
- 4 of the 7 control tasks have empty note blocks, so the "notes don't hurt" check has notes in
  only 3 (lc01, lc02, oai04).
- The tasks were written for this benchmark by the project that makes since-cutoff. The admission
  gates, the review, the controls and the frozen protocol limit that bias; they do not remove it.
- The notes came from one tool version with one scope setting and covered 5 of the 17 changes. A
  static diff of a package's own API does not see a dependency switch such as `httpx` to `httpx2`,
  or behaviour that changes behind an unchanged signature.
- Cost is Claude Code's reported cost at API prices, not a bill.

## Deviations and incidents

Each is recorded in
[`bench/PROTOCOL.md`](https://github.com/MohammadHijjawi97/since-cutoff-benchmark/blob/main/bench/PROTOCOL.md)
unless noted.

1. **17 post-cutoff tasks instead of 19.** ant06 and mcp03 (and the control hf05) failed the
   premise check and were dropped with a runtime probe each, which lowers the power of the
   pass-rate test (section 4).
2. **Harness decisions made before any session** (section 12): harness-owned grading options,
   instruction fixes in oai02, hf01 and ant03, old-API "lure" modules removed or rewritten in seven
   repositories after the review, one module of working library use added to seven repositories,
   `leak_allow` for mcp04, and loopback sockets in the mcp04 and mcp05 hidden tests.
3. **Login instead of token authentication** (section 12.x): section 3 describes a token with an
   isolated home folder; the runs used the maintainer's normal login with
   `--setting-sources project` (`auth = "login"`, `isolate_home = false` in `bench.toml`), so user
   settings, hooks and plugins were not loaded, but the home folder was the real one (not in
   `PROTOCOL.md`, although section 12 required `isolate_home = false` to be recorded there). Inline
   `python -c` commands were denied in every arm, and the environment scrub forced the permission
   mode to `default` (headless, equivalent to the allow-list).
4. **Notes tool version and scope** (sections 12.y, 12.w): section 2 names since-cutoff 0.4.0rc1;
   the main run used the released 0.4.1 with `--scope imported`, decided and frozen before the main
   plan.
5. **Arm E added after the pilot** (section 12.z), because B called Context7 in 2 of 4 pilot
   sessions, below the pre-registered 80% threshold. The pre-registered contrasts and their Holm
   family were not changed.
6. **Tool changed after seeing a pilot task** (section 12.z): the ant01 note wording described
   above. ant01 and mcp01 are marked pilot-exposed, and the sensitivity analysis was added before
   the main run.
7. **Localhost port overlap in mcp04** (section 12.v): runs of the same task on different workers
   could reach each other's server on `127.0.0.1:8765` while checking their own work. Grading was
   not affected; the confound is limited to what an agent saw during its own checks.
8. **Credential exposure and a harness change during the run** (section 12.u): after 181 sessions,
   the secret scan found the maintainer's saved Hugging Face token in two hf04 transcripts, read by
   the agent's own check scripts and sent only to an in-memory mock transport. The run was stopped,
   every per-run environment got a start-up file that hides the token, the transcripts were redacted
   and the maintainer was asked to revoke the token. 40 hf-task sessions ran before the change and
   20 after; 181 sessions ran with harness hash `9227ebaf60e2` and 179 with `90165f07c60f` (the
   20 and the hash split are counted from `runs.jsonl`; not in `PROTOCOL.md`). The analysis code
   did not change.
9. **Context7 quota exhausted (found while writing this page; recorded afterwards in
   `PROTOCOL.md` section 12.t).** Context7 ran without an API key. From 06:25:53 UTC, the 215th of 360 sessions by start
   time, to the end of the run, every Context7 call returned "Monthly quota exceeded" instead of
   documentation. Affected sessions: 30 of 72 in E (17 of 51 post-cutoff), 9 of the 23 B sessions
   that called Context7 (1 of 10 post-cutoff) and 5 of the 13 such D sessions (none post-cutoff).
   E's 100% call rate counts calls, not documentation received, and the report's documentation hit
   rates count these calls as misses. Results for E, and the C/E and E/B contrasts, describe
   "lookups required, and failing for roughly the last 40% of the run". Exploratory, not
   pre-registered: on the 34 post-cutoff blocks where E did receive documentation, C and E both
   passed 34 of 34, and the session-level C/E ratios (bootstrap over sessions, seed 20261001) were
   turns 0.79 [0.71, 0.89], cost 0.86 [0.69, 1.04] and wall time 0.81. A re-run of the affected
   B/D/E sessions with a Context7 key would be reported as a separate, labelled study.
10. **Robustness check not run (not in `PROTOCOL.md`):** the logistic mixed model
    `y ~ condition + (1|task)` that section 7 lists as run outside the harness was not fitted for
    this report. The pre-registered contrasts above do not depend on it.
11. **Protocol text left as frozen (not in `PROTOCOL.md`):** its title still says "DRAFT, not
    yet tagged", and it refers to the author's unpublished planning documents (plan B, `bench.md`).
    The file is as it was at the tag, plus sections 12.v and 12.u; it was not rewritten after the
    run, so that the frozen text and the published text are the same.
12. **Edits made while preparing publication (not in `PROTOCOL.md`):** after the reports were
    written, a prefix of the same token, quoted in the final message of `hf04_D_r1_d017`, was
    redacted in that run's files and its line in `runs.jsonl`; dummy credentials in the harness
    tests were split into two string literals so the secret scan does not flag them; and a line
    naming the reviewer was added to `REVIEW.md`. Rebuilding the reports from the edited records
    reproduces the committed ones except for their timestamp.

## Related work

- **GitChameleon 2.0** (Diganta Misra, Nizar Islah, Victor May and others,
  [arXiv:2507.12367](https://arxiv.org/abs/2507.12367), 2025): 328 Python code-completion problems
  over 26 libraries, each tied to a library version and scored by executable hidden tests; enterprise
  models reach 48-51% at baseline, and the paper's retrieval arm added 7.3 points for Claude 3.7
  Sonnet, 10.0 for GPT-4.1 and 6.7 for Gemini 2.5 Pro. Almost all of its target versions are older
  than the release a current model defaults to, and each problem is a code-completion prompt. Here
  the pinned versions are newer than the cutoff, and an agent works in a repository where the
  package is installed.
- **CodeUpdateArena** (Zeyu Leo Liu, Shrey Pandit, Xi Ye, Eunsol Choi, Greg Durrett,
  [arXiv:2407.06249](https://arxiv.org/abs/2407.06249), 2024): knowledge editing for synthetic
  updates to 54 functions from 7 Python packages, with 670 program-synthesis tasks.
- **VersiCode** (Tongtong Wu, Weigang Wu, Xingyu Wang and others,
  [arXiv:2406.07411](https://arxiv.org/abs/2406.07411), 2024): version-specific code completion
  and version-aware code migration.
- **LibEvoBench** (Daniele Cipollone, Sergey Titov, Maliheh Izadi, Egor Bogomolov, Arie van Deursen,
  [arXiv:2606.25402](https://arxiv.org/abs/2606.25402), 2026, DL4Code workshop at ICML 2026):
  several tasks across versions of widely used Python libraries; models do worse on APIs that
  evolved, naming the target version does not help, and relevant documentation does.
- **When LLMs Lag Behind: Knowledge Conflicts from Evolving APIs in Code Generation** (Ahmed Nusayer
  Ashik, Shaowei Wang, Tse-Hsun Chen, Muhammad Asaduzzaman, Yuan Tian,
  [arXiv:2604.09515](https://arxiv.org/abs/2604.09515), 2026): 270 real API updates from eight
  Python libraries and 11 models; without comprehensive documentation 42.55% of generated examples
  run in the target environment, structured documentation raises that to 66.36%, and reasoning
  strategies such as self-reflection improve the executable rate by 11%.
- **How and Why LLMs Use Deprecated APIs in Code Completion? An Empirical Study** (Chong Wang,
  Kaifeng Huang, Jian Zhang, Yebo Feng, Lyuye Zhang, Yang Liu, Xin Peng,
  [arXiv:2406.09834](https://arxiv.org/abs/2406.09834), 2024; later retitled "LLMs Meet Library
  Evolution: Evaluating Deprecated API Usage in LLM-based Code Completion", ICSE 2025): seven LLMs,
  145 API mappings from eight Python libraries, more than 28,000 completion prompts, and two
  lightweight mitigations.
- **Context7** ([upstash/context7](https://github.com/upstash/context7), MIT): an MCP server whose
  `resolve-library-id` and `query-docs` tools put documentation snippets into the agent's context.
  It works without an API key at a lower rate limit, which is how it ran here.

Most of this work measures models answering prompts on fixed task sets (GitChameleon 2.0 also
evaluates agents and code assistants). This benchmark measures one agent with tools, tests and the
installed package at hand; that setting is a likely reason the pass rates hit the ceiling and the
differences showed up in effort instead.

## How to reproduce it

The [benchmark repository](https://github.com/MohammadHijjawi97/since-cutoff-benchmark) holds the
harness (standard-library Python), the 24 tasks with their hidden tests and reference solutions,
the dropped tasks with their probes, the frozen notes, the environment locks, the protocol, and every
session: all 361 attempt records (360 valid), each with its transcript (`stream.jsonl`), final diff,
`CLAUDE.md` and prompt, and a grading log for every valid one.

The reports can be rebuilt from the recorded sessions without any model call; while writing this
page, both came out identical to the committed ones except for their timestamp:

```bash
git clone https://github.com/MohammadHijjawi97/since-cutoff-benchmark
cd since-cutoff-benchmark
python -m bench report main-2026-09-28
python -m bench report main-2026-09-28 --exclude-tasks ant01,mcp01
```

Running it again needs Windows, Claude Code 2.1.283 with a Claude login, Python 3.13, Node.js with
`@upstash/context7-mcp` 4.1.1, and the paths in `bench/bench.toml` adjusted. A Context7 API key
avoids the quota problem above, but it changes arms B, D and E relative to this run. The frozen
protocol is the tag `main-freeze-2026-09-28`; use the main branch, which adds the credential fix
from section 12.u and leaves the analysis code unchanged. The sequence follows
[section 10](https://github.com/MohammadHijjawi97/since-cutoff-benchmark/blob/main/bench/PROTOCOL.md#10-operating-procedure),
with `prepare --download` first so that `validate` finds the wheels it builds its environments from:

```bash
python -m bench prepare --download --install-context7      # wheels into the wheelhouse, offline preflight
python -m bench validate                                   # gates and leak check for every task
python -m bench prepare --allow-calls                      # plus about 6 short model-call checks
python -m bench plan --study-id main-<date> --seed 20260929
python -m bench run main-<date> --workers 3
python -m bench report main-<date>
python -m bench report main-<date> --exclude-tasks ant01,mcp01
```

For scale: the 360 sessions of this run had a reported cost of $112.97 at API prices, with a median
session of $0.26 and 59 seconds (from `runs.csv`).
