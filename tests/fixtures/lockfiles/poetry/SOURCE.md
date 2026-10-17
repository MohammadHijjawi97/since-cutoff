# python-poetry/poetry

An excerpt of `pyproject.toml` and `poetry.lock` from https://github.com/python-poetry/poetry, at the repository root.

- Commit: [`130f053073b81e4d9f4f2ec64a23988792a44989`](https://github.com/python-poetry/poetry/tree/130f053073b81e4d9f4f2ec64a23988792a44989) (2026-09-27)
- License: MIT, https://github.com/python-poetry/poetry/blob/130f053073b81e4d9f4f2ec64a23988792a44989/LICENSE

## What was kept

- `poetry.lock` (lock-version 2.1): 25 of its 77 packages (26 of its 78 entries): 21
  direct ones and 4 transitive ones, one of them (`rapidfuzz`) locked twice. Each `files` list is cut to its first file.
- `pyproject.toml`: `[project]` without authors, maintainers, URLs and classifiers, the
  kept dependencies; the kept lines of the `dev`, `test` and `typing` groups; the optional
  `github-actions` group; `[build-system]`. Tool settings are left out.

Apart from what is said here, kept lines are as in the original file.

## What it tests

- PEP 621 `[project]` dependencies in Poetry's style (`"cleo (>=2.1.0,<3.0.0)"`) with
  markers, next to `[tool.poetry.group.*]` groups, an optional one included;
- a package declared in `[project]` and again in a group (`dulwich`);
- a package locked once per Python range (`rapidfuzz` 3.14.5 below Python 3.15, 3.14.6
  from 3.15): the one for the project's Python.
