# Contributing

Thanks for helping! Issues, pull requests, results from your own projects and questions are all
welcome, and first-time contributors are very welcome.

## Where to start

- **Pick an issue.** [Good first issues](https://github.com/MohammadHijjawi97/since-cutoff/labels/good%20first%20issue)
  are small and self-contained; [help wanted](https://github.com/MohammadHijjawi97/since-cutoff/labels/help%20wanted)
  ones are bigger. Comment on the issue to say you're taking it, and I'll assign it to you.
- **Share a result.** Ran it on your project? Post what it found in
  [Share your results](https://github.com/MohammadHijjawi97/since-cutoff/discussions/6).
  False positives in the API diff are especially useful ([template](https://github.com/MohammadHijjawi97/since-cutoff/issues/new?template=false_positive.yml)).
- **Translate.** The README exists in English, Chinese, Spanish and French; another language
  is a great first pull request.
- **Not sure?** Open a draft pull request early, or ask in
  [Discussions](https://github.com/MohammadHijjawi97/since-cutoff/discussions).

## How the code is organised

| Module | What it does |
|---|---|
| `project.py` | Reads lockfiles, requirements files and `.venv`, and scans the project's imports |
| `pypi.py` | PyPI metadata and release dates; downloads and safely extracts sources |
| `models.py` | Training cutoffs (models.dev plus a bundled snapshot) and model-id normalisation |
| `apidiff.py` | The static API diff between two versions (griffe): removals, moves, parameters, deprecations |
| `selection.py` | Ranks the changes and picks which ones to probe |
| `prompts.py` | Task, solver and note prompts, and the leak filter for tasks |
| `checker.py` | Type-checks answers with basedpyright against a version, in an isolated environment |
| `engine.py` | Ties it together: scan, probe, classify (stale / wrong / deprecated / correct), notes, held-out tests |
| `notes.py` | Renders, applies and removes the notes block in `AGENTS.md` / `CLAUDE.md` |
| `report.py` | Terminal, Markdown and JSON reports |
| `mcp_server.py` | The `since-cutoff mcp` server and its tools |
| `providers/` | Model providers (Claude Code CLI, Anthropic, OpenAI-compatible APIs) |
| `cli.py` | Command-line interface |

## Development setup

```bash
git clone https://github.com/MohammadHijjawi97/since-cutoff
cd since-cutoff
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e . pytest ruff mypy
```

## Checks

```bash
pytest -m "not network"     # fast, offline; uses a fake PyPI and a scripted model
pytest -m network           # talks to PyPI
ruff check src tests && ruff format --check src tests
mypy
```

The offline suite needs no API keys: `tests/conftest.py` defines a toy library with two versions
and a scripted model that "knows" only the old one, so the whole pipeline (scan, probe, notes,
held-out verification) runs end to end in CI.

Run one file or one test while you work: `pytest tests/test_project.py -k conda -q`.
CI runs the same checks on Linux, macOS and Windows with Python 3.10 to 3.13, so keep paths
`pathlib`-based and never assume `/` in file names.

## Pull requests

- One topic per pull request, with a test that fails without the change.
- Add a line to the "Unreleased" section of `CHANGELOG.md` for anything users would notice.
- AI-assisted contributions are fine, as long as you have read and understood every line, ran
  the checks yourself, and wrote the pull request description in your own words.
- I try to review within a couple of days. If a pull request has conflicts after a release,
  say so and I can rebase it for you.

## Good first contributions

- **More lockfile formats** (`project.py`), with a test in `tests/test_project.py`.
- **More providers** (`providers/`): anything that turns a system + user prompt into text.
  Providers must call the model **without tools, retrieval or project context**.
- **False positives or negatives in the API diff**: please include the package, both versions
  and the symbol. A regression test with a tiny source tree (see `tests/test_apidiff.py`) is the
  best way to fix them.
- **JavaScript/TypeScript support** (planned): the same pipeline with `.d.ts` diffs and `tsc`.

## Principles

- Every number must be reproducible from `results.json`; no LLM judges.
- Never execute package code or model-generated code.
- Keep notes short: they are paid for on every agent turn.

## Releasing

1. Set the new version everywhere and date the changelog:
   - `src/since_cutoff/__init__.py`, `server.json` (twice), `CITATION.cff` (`version` and
     `date-released`) and the `since-cutoff-version` default in `action.yml`;
   - the plugin and extension manifests: `.claude-plugin/plugin.json`, the plugin entry in
     `.claude-plugin/marketplace.json`, `plugin.json`, `.codex-plugin/plugin.json` and
     `gemini-extension.json`;
   - the pinned MCP launchers `.mcp.json` and `mcp.json` (`since-cutoff==X.Y.Z`);
   - the README pins: the pre-commit `rev: vX.Y.Z` in `README.md` and `README.zh-CN.md`, and
     the `since-cutoff-version` default in the action's input table;
   - turn the changelog's "Unreleased" section into `## X.Y.Z - YYYY-MM-DD`.

   `python scripts/check_versions.py` lists every field that still differs, and `pytest` runs
   the same check. The user configs in the README (`uvx since-cutoff@latest mcp`) and the
   `Dockerfile` (`since-cutoff>=...`) need no change.
2. Tag `vX.Y.Z` and push the tag. `.github/workflows/release.yml` runs only for full `vX.Y.Z`
   tags, and then:
   - checks that every version field equals the tag (`scripts/check_versions.py`), and builds;
   - publishes to PyPI with trusted publishing (configure the pending publisher on pypi.org once);
   - publishes `server.json` to the MCP Registry;
   - creates the GitHub release with the changelog entry and the built files.
3. Once the release is on PyPI, move the major tag by hand, so that
   `uses: MohammadHijjawi97/since-cutoff@v0` picks it up (the workflow never moves it, and
   pushing `v0` does not start a release):

   ```bash
   git tag -f v0 "vX.Y.Z^{commit}" && git push -f origin v0
   ```
4. To list a release on the GitHub Marketplace, edit it on GitHub, tick "Publish this Action to
   the GitHub Marketplace" and save (the first time also asks to accept the Marketplace
   agreement; it needs two-factor authentication). `@v0` works without the listing.

The social preview images (`docs/img/og.png`, `docs/img/social-preview.png`) are rendered from
`scripts/social-card.html`; the file says how. Upload `social-preview.png` under the repository's
Settings, "Social preview".
