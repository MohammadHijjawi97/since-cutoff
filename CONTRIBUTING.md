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

1. Update `__version__` in `src/since_cutoff/__init__.py`, `.claude-plugin/plugin.json` and
   `CHANGELOG.md`.
2. Tag `vX.Y.Z` and push the tag. `.github/workflows/release.yml` publishes to PyPI with trusted
   publishing (configure the pending publisher on pypi.org once).
