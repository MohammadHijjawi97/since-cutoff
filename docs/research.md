---
layout: default
title: Does a dependency block pay for its tokens?
description: A technical report on the dependency-change block that since-cutoff writes into AGENTS.md or CLAUDE.md. 1,607 agent sessions with controls run in the same batches, and a break-even model in one unit with a measured context cost.
---

*Mohammad Hijjawi · October 2026 ·
[report (PDF)](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/research/dependency-block-study/paper/paper.pdf) ·
[data and scripts](https://github.com/MohammadHijjawi97/since-cutoff/tree/main/research/dependency-block-study)*

`since-cutoff sync` can write a block into your `AGENTS.md` or `CLAUDE.md`: a header that names the dependencies whose
public API changed after your coding model's training cutoff, then up to five change lines per package. The block is
loaded into every agent context, so it is paid for every time, while it can only help in the contexts that need a
changed API. This report asks when that trade pays.

## What was run

Eleven completed studies, 1,607 sessions in all: Claude Code with Opus 5.5, Sonnet 5.5 and Haiku 5.5, an older-cutoff
model, and one open-weight agent (Qwen3-Coder in mini-swe-agent). The tasks come from a pool of 35 small Python tasks
with hidden tests, 25 of which need an API that changed after the June 2026 cutoff; each study ran between 8 and 31
tasks that need a changed API for the model tested (with or without controls), and a case study ran two tasks in a real repository. Each
study's protocol and decision rules were committed
in git before its first session. The arms were:

| arm | what the agent gets |
|---|---|
| None | the base `CLAUDE.md` only |
| Notes | the full block: header plus change lines |
| Header | the header alone: package names and both versions |
| Irrelevant | the header plus true change lines of a package the task does not use, length-matched |
| Generic | a warning that API knowledge may be out of date, with no package named |

The first study is the [agent benchmark](benchmark.md). The report adds a replication, eight new tasks in three more
libraries, a study that ran all five arms in the same batches, three more models and four follow-ups, a scan of 200
GitHub repositories (100 random ones with pins and 100 that import an LLM, agent or model library), a detector-recall study, and an estimate of how often real agent contexts need a changed
API, from the author's own Claude Code transcripts.

## Findings

**Exposure is common, real breakage is rarer.** 53% of random popular Python repositories with pins lock a dependency
released after the cutoff; the matcher finds code that uses a changed API in 12%. An AI judge judged none of 48
audited true uses to fail at runtime, though only 14 were checked by running code.

**On Opus 5.5 the block saved a little, and only there.** Opus 5.5 solved its own small tasks almost always without
help, so the block could only change cost. In the main study it cost 0.80 of no block (95% CI 0.70 to 0.90). On eight
new tasks it used 0.79 of the tokens (CI 0.68 to 0.92), which met the pre-committed rule, but with Holm correction
across that study's three contrasts p = 0.094, so it is not counted as detected. A replication saved 15% with a CI
that includes no saving. Two of ten pre-committed efficiency rules were met, with no correction across rules. No
saving was detected on Sonnet 5.5, Haiku 5.5, the older-cutoff model or the open-weight agent.

**The header alone did about as well.** No pre-committed test separated the full block from the header alone or
from the generic warning. In the study that ran all five arms together, tokens were 0.73 of no block with the header,
0.75 with the full block, 0.81 with the irrelevant lines and 0.94 with the generic warning. since-cutoff 0.6.0 writes
the header alone with `sync --header-only`, the full block's header word for word.

**The block is cheap, but affected contexts alone do not repay it.** Every length in the break-even model is counted
in model calls. In the author's 1,308 agent contexts (mostly subagent runs, 45.3 model calls on average) the median
real block, 704 tokens, costs about $0.012 per context: 0.4% of a context's measured cost of $2.96. If the block has no
effect on the other contexts and saves a fixed amount per affected context, it pays only once 14% of contexts need a changed API (14 to 49% over real block sizes
and savings). The measured share is 0.8% (a lower bound) to 7.0%, and at those shares the block loses about $11 and $6
per 1,000 contexts.

**The effect on all other contexts decides, and it is unknown.** On control tasks the block made sessions 6.6%
cheaper (cost ratio 0.93, CI 0.84 to 1.04). Carried to real contexts, that would be a gain of $184 per 1,000 contexts
(CI -$117 to +$590), positive in 89% of bootstrap draws. But the estimate cannot be told from zero, and it comes from
short control sessions on tasks the tested model wrote.

## What this means for you

- Leaving the block off by default is a loss-averse choice: it avoids a small, bounded cost and gives up an uncertain
  gain. It is not a finding that the block loses money.
- To decide for your own fleet, run a live A/B test of the net effect over all sessions. At the benchmark's spread,
  with 20% of sessions affected, an unpaired test of mean cost needs about 2,500 to 2,900 sessions per arm.
- Offline, test any block against header-only and generic-warning arms in the same batches: drift between sittings
  was as large as the effect.
- Keep the block small: at no effect on other contexts it pays for itself only under about 2,700 to 5,100 x f tokens
  in contexts like the author's, where f is the share of contexts that need a changed API.

## Limits

All tasks were written by Claude Code sessions running Opus 5.5, the only model on which a saving was detected, and no
human has checked them or the AI-judged audits. The estimate of how often contexts need a changed API comes from one
developer's work. A lockfile-only version list was registered but not run (the Claude CLI login had expired), the
Context7 arms hit a quota error, and a second open-model study was started and stopped before completion; its partial
runs are not analysed. The report lists every limitation and the AI assistance used to produce it.

## Cite

```bibtex
@techreport{hijjawi2026dependencyblock,
  author      = {Hijjawi, Mohammad},
  title       = {Is a Dependency-Change Block in a Context File Worth Its Tokens? A Break-Even Model and Coding-Agent Studies},
  institution = {since-cutoff project},
  type        = {Technical report},
  year        = {2026},
  month       = oct,
  url         = {https://github.com/MohammadHijjawi97/since-cutoff/tree/main/research/dependency-block-study}
}
```
