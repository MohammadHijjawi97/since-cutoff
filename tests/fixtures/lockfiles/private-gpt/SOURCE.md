# zylon-ai/private-gpt

An excerpt of `pyproject.toml` and `uv.lock` from https://github.com/zylon-ai/private-gpt, at the repository root.

- Commit: [`01ac43d72bc4c0b7565994197fe23476539059a7`](https://github.com/zylon-ai/private-gpt/tree/01ac43d72bc4c0b7565994197fe23476539059a7) (2026-09-21)
- License: Apache-2.0, https://github.com/zylon-ai/private-gpt/blob/01ac43d72bc4c0b7565994197fe23476539059a7/LICENSE

## What was kept

- `uv.lock`: 29 of its 355 packages (30 of its 356 entries): the project itself
  (`private-gpt`, an editable source), 25 of its 104 direct dependencies and 3 transitive
  ones, among them `onnxruntime`, which is locked twice (1.20.1 for Windows, 1.25.1
  elsewhere). The `private-gpt` entry keeps the extras of the pyproject.toml excerpt only,
  and its `provides-extras` line lists those. Dependency lists keep the kept packages; each
  `wheels` list is cut to its first wheel.
- `pyproject.toml`: `[project]` without `authors`, 8 of its 13 base dependencies, 15 of its
  54 extras (with the lines of the kept packages), `[tool.uv]` and `[build-system]`.

Apart from what is said here, kept lines are as in the original file.

## What it tests

- a uv.lock that is not a workspace: the direct dependencies are the project's base
  dependencies and every extra, `lint`/`test`/`typecheck` included; the project is not one;
- extras that include other extras of the project (`private-gpt[tokenizer-local]`);
- one name spelled two ways (`huggingface_hub`, `huggingface-hub`);
- a package with pre-releases only (`sqlgpt-parser 0.0.1a5`);
- a fork by platform (`onnxruntime`): the same version for `--python 3.11` on every
  machine.
