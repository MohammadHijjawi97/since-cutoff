# Textualize/rich

An excerpt of `pyproject.toml` and `poetry.lock` from https://github.com/Textualize/rich, at the repository root.

- Commit: [`9d8f9a372cc5916fd4781fec207ced7ddac2f08f`](https://github.com/Textualize/rich/tree/9d8f9a372cc5916fd4781fec207ced7ddac2f08f) (2026-06-23)
- License: MIT, https://github.com/Textualize/rich/blob/9d8f9a372cc5916fd4781fec207ced7ddac2f08f/LICENSE

## What was kept

- `poetry.lock` (Poetry 2.1.3, lock-version 2.1): 17 of its 49 packages, the 10 direct
  ones and 7 transitive ones; `[extras]` and `[metadata]`. Each `files` list is cut to its
  first file, and `[package.dependencies]` keeps the kept packages.
- `pyproject.toml`: the whole file without its `authors` line.

Apart from what is said here, kept lines are as in the original file.

## What it tests

- the old Poetry layout: `[tool.poetry.dependencies]` (with `python`, which is not a
  dependency), an optional dependency behind an extra (`ipywidgets`, extra `jupyter`), and
  `[tool.poetry.dev-dependencies]`;
- Poetry-only constraints (`^2.13.0`), which pin nothing;
- `markers` given per dependency group (`colorama`).
