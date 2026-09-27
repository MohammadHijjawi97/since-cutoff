# pdm-project/pdm

An excerpt of `pyproject.toml` and `pdm.lock` from https://github.com/pdm-project/pdm, at the repository root.

- Commit: [`e8ced850bc1be15501dc1c4139e96c4605f85f2b`](https://github.com/pdm-project/pdm/tree/e8ced850bc1be15501dc1c4139e96c4605f85f2b) (2026-09-22)
- License: MIT, https://github.com/pdm-project/pdm/blob/e8ced850bc1be15501dc1c4139e96c4605f85f2b/LICENSE

## What was kept

- `pdm.lock` (lock_version 4.5.1, strategy inherit_metadata): 26 of its 96 packages
  (29 entries: `hishel`, `httpx` and `mkdocstrings` have a second entry for an extra), and
  its `[metadata]`. Dependency lists keep the kept packages; each `files` list is cut to
  its first file.
- `pyproject.toml`: `[build-system]`, `[project]` without authors, readme, keywords,
  classifiers and URLs, 14 of its 24 dependencies, the extras, and the kept lines of the
  dependency groups. Tool settings are left out.

Apart from what is said here, kept lines are as in the original file.

## What it tests

- a pdm.lock, which does not say which packages are direct: `[project]`, extras and
  dependency groups do;
- the same package locked again for an extra (`httpx[socks]`);
- extras that the lock leaves out (`copier`, `cookiecutter`): unpinned, with their
  declared range;
- a self-reference in a group (`pdm[pytest]`).
