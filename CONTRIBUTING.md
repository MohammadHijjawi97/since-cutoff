# Contributing

Thanks for helping! Issues and pull requests are welcome.

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
