# facebookresearch/sam-audio

An excerpt of `pyproject.toml` from https://github.com/facebookresearch/sam-audio, at the repository root.

- Commit: [`bb4c6999d2677c7402360e426afc01ddfad6dce0`](https://github.com/facebookresearch/sam-audio/tree/bb4c6999d2677c7402360e426afc01ddfad6dce0) (2026-05-26)
- License: SAM License (not an SPDX license), https://github.com/facebookresearch/sam-audio/blob/bb4c6999d2677c7402360e426afc01ddfad6dce0/LICENSE

## What was kept

- `pyproject.toml`: `[project]` with its name, version, description, readme,
  requires-python and all 14 dependencies; `[tool.setuptools.packages.find]` and
  `[build-system]`. Authors (with their e-mail addresses), the license table, the ruff
  settings and the URLs are left out. Only the dependency declarations, which are facts
  about the project, are reproduced.

Apart from what is said here, kept lines are as in the original file.

## What it tests

- a pyproject.toml without a lockfile or pins: every dependency is unpinned, and the scan
  takes the latest release on PyPI (`transformers` within `>=4.54.0`);
- PEP 508 git references (`dacvae@git+https://...`, one at a branch): not looked up on
  PyPI, their URL kept for the report.
