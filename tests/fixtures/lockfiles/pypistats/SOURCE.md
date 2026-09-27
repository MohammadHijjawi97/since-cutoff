# psf/pypistats.org

An excerpt of `pyproject.toml`, `requirements.in`, `requirements.txt`, `requirements-dev.in`, `requirements-dev.txt` and `poetry.lock` from https://github.com/psf/pypistats.org, at the repository root.

- Commit: [`6ed2ed7fce0487ae6c0b58265c49edc1c2621721`](https://github.com/psf/pypistats.org/tree/6ed2ed7fce0487ae6c0b58265c49edc1c2621721) (2026-07-13)
- License: Apache-2.0, https://github.com/psf/pypistats.org/blob/6ed2ed7fce0487ae6c0b58265c49edc1c2621721/LICENSE

## What was kept

- `requirements.txt` (pip-compile --generate-hashes): 21 of its 72 pins, each with its
  first hash (without the `\` that continued it) and its whole `# via` annotation;
  `requirements.in`: the 11 kept of its 17.
- `requirements-dev.txt` (pip-compile --allow-unsafe): 7 of its 14 pins, pip and
  setuptools from the "unsafe" section included; `requirements-dev.in`: the whole file.
- `poetry.lock` (lock-version 1.0, from 2020, used by nothing): 10 of its 63 packages, and
  `[metadata]` with the kept packages' first file.
- `pyproject.toml`: the whole file without its `authors` line.

Apart from what is said here, kept lines are as in the original file.

## What it tests

- pip-compile output: `# via -r requirements.in` marks a direct dependency, `# via kombu`
  a transitive one, in one-line and multi-line annotations and in both files;
- a stale poetry.lock in a project not set up for Poetry, whose versions disagree with the
  pins: ignored, with a warning, and its lock-only packages (`zipp`) do not count.
