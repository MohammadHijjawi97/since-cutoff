# Does a dependency block pay for its tokens?

A technical report on whether an always-loaded block of dependency-change notes in `AGENTS.md` or `CLAUDE.md`, the
block that `since-cutoff sync` writes, is worth what it costs. Mohammad Hijjawi, October 2026.

- Report: [`paper/paper.pdf`](paper/paper.pdf) (40 pages; LaTeX source in [`paper/`](paper/))
- Data: [`data/`](data/) (details in [`data/README.md`](data/README.md))
- Scripts: [`scripts/`](scripts/)
- Main study's tasks, hidden tests and transcripts:
  [since-cutoff-benchmark](https://github.com/MohammadHijjawi97/since-cutoff-benchmark)

## What the study asked

A coding model knows library APIs as of its training cutoff. since-cutoff diffs each locked dependency against the
release that was current at that cutoff, with no model call, and can write the breaking changes into the project's
context file: a header naming the changed packages, then up to five change lines per package. Such a block is loaded
into every agent context, so it is paid for every time, while it can only help in the contexts that touch a changed
API. The study asks when that trade pays.

Eleven completed studies (1,607 Claude Code and open-weight agent sessions) compared the block with no block, the header alone, length-matched irrelevant lines and a generic "your API knowledge
may be out of date" warning. The tasks come from a pool of 35 small Python tasks with hidden tests: each study ran 8
to 31 tasks that need a changed API for the model tested (with or without controls), and a case study ran two tasks in
a real repository. A break-even model then puts the measured saving next to the block's cost in one
developer's real agent contexts.

## Findings in plain words

1. **Exposure is common, real breakage is rarer.** 53% of random popular Python repositories with pins lock a
   dependency released after the June 2026 cutoff of the models tested; the matcher finds code that uses a changed API
   in 12%. An AI judge judged none of 48 audited true uses to fail at runtime (only 14 of them were checked by
   running code).
2. **On Opus 5.5 the block saved a little, and only there.** Opus 5.5 solved its own small tasks almost always
   without help, so the block could only change cost. In the main study it cost 0.80 of no block (95% CI 0.70 to
   0.90); on eight new tasks it used 0.79 of the tokens (CI 0.68 to 0.92), which met the pre-committed rule but is not
   detected after Holm correction (p = 0.094); a replication saved 15% with a CI that includes no saving. Two of ten
   pre-committed efficiency rules were met, with no correction across rules. Sonnet 5.5, Haiku 5.5, an older-cutoff
   model and one open-weight agent (Qwen3-Coder) showed no detectable saving.
3. **The header alone did about as well.** No pre-committed test separated the full block from the header alone or
   from the generic warning; in the study that ran all arms together, tokens were 0.73 of no block with the header and
   0.75 with the full block. since-cutoff 0.6.0 writes the header alone with `sync --header-only`, the full block's
   header word for word.
4. **The block is cheap, but affected contexts alone do not repay it.** In the author's 1,308 agent contexts (mostly
   subagent runs, 45.3 model calls on average) the median real block (704 tokens) costs about $0.012 per context,
   0.4% of a context's measured cost ($2.96). At no effect on other contexts, and assuming a fixed saving per affected context, it pays only once 14% of contexts need a
   changed API (14 to 49% over real block sizes and savings); the measured share is 0.8% (a lower bound) to 7.0%. At
   those shares it loses about $11 and $6 per 1,000 contexts.
5. **The effect on all other contexts decides, and it is unknown.** On control tasks the block made other sessions
   6.6% cheaper (cost ratio 0.93, CI 0.84 to 1.04). Carried to real contexts that would mean +$184 per 1,000 contexts
   (CI -$117 to +$590), positive in 89% of bootstrap draws; but the estimate cannot be told from zero and comes from
   short control sessions on tasks the tested model wrote.
6. **Recommendation.** Leaving the block off by default is a loss-averse choice, not a finding that it loses money. A
   live A/B test of the net effect over all sessions settles it for a given fleet: about 2,500 to 2,900 sessions per
   arm at the benchmark's spread (unpaired test of mean cost, 20% of sessions affected). Offline tests should run
   header-only and generic arms in the same batches.

All tasks were written by Claude Code sessions running Opus 5.5, the only model on which a saving was detected, and no
human has checked them or the AI-judged audits. Every claim is scoped to such tasks.

## Reproduce

From `research/dependency-block-study` (Python 3.10+; the statistics scripts use the standard library, the figures
need matplotlib):

```bash
# ratios, bootstrap CIs and exact tests from the per-run tables
python scripts/recompute_cost.py data/runs/main-2026-09-28 --pairs C/A,E/A
python scripts/recompute_cost.py data/runs/tasks2-opus55-2026-10-22 --pairs C/A,H/A,C/H
python scripts/pooled_old_new.py data/runs/main-2026-09-28 data/runs/tasks2-opus55-2026-10-22

# Table 1 numbers and its raw and Holm-adjusted p values; break-even and size-cap numbers
# (these take the study folder as argument; data/ has its layout, and outputs are written under data/)
RUNS_TABLES=runs python scripts/round5_table1.py data
RUNS_TABLES=runs python scripts/round22_holm.py data
RUNS_TABLES=runs python scripts/breakeven2d.py data
RUNS_TABLES=runs python scripts/round13_numbers.py data
python scripts/breakeven2d_contexts.py data

# figures (ratios and the break-even figure)
python scripts/make_figures.py --analysis data/analysis --out paper/figures

# the report
cd paper && pdflatex main && bibtex main && pdflatex main && pdflatex main && cp main.pdf paper.pdf
```

`data/README.md` lists the script and output behind every appendix analysis. Not re-runnable from this folder: the
estimate of f, the per-context cost and the unified-unit bootstrap (they read the author's private Claude Code
transcripts; only their aggregate outputs are here), the scripts that read the benchmark sessions' stream logs, and
the exposure scans (the raw scans name repositories).

## Folder layout

| path | what it is |
|---|---|
| `paper/` | `main.tex`, `refs.bib`, the ACL style files, `figures/` and the built `paper.pdf` |
| `data/protocol/` | pre-registrations, addendum sections 1 to 14, the offline studies' protocols, tag and first-session times |
| `data/configs/` | harness and agent configuration per study |
| `data/tasks/`, `data/tasks2/`, `data/case/` | task definitions and validation records |
| `data/notes/` | every frozen block given to an agent, with manifests |
| `data/runs/` | de-identified per-run tables of the eleven completed studies |
| `data/analysis/` | aggregated results and script outputs |
| `data/recall/` | ground truth and judgments of the detector-recall study |
| `scripts/` | analysis, figure and run scripts |

## Limitations

- Small and self-written: 25 post-cutoff tasks in seven libraries for Opus 5.5, all written by Opus 5.5 and checked by
  no human; Opus 5.5 was at ceiling, so no pass-rate claim is possible.
- One agent shows the effect: ten of the eleven studies ran Claude Code. A second open-model study was started and
  stopped before completion; its partial runs are not analysed. A pre-committed lockfile-only version list was
  registered but not run (the Claude CLI login had expired), and the Context7 comparison hit a quota error.
- f comes from one developer's own agent contexts, in work heavy on AI libraries; the effect u on other contexts is
  measured on short benchmark control sessions and carried to real contexts as a constant relative effect.
- The detector ground truth and both exposure audits are single AI judgments without human review.

## How to cite

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

## AI assistance

The experiments, analysis code and text of this report were produced with an AI coding assistant (Claude Code, Claude
Opus 5.5) working under the author's direction. Claude Code sessions running Opus 5.5 also wrote all benchmark tasks,
and the detector ground truth and exposure audits are AI judgments; the report's "AI Assistance" section lists the
rest.
