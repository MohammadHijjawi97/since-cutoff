# unslothai/unsloth

An excerpt of `pyproject.toml` from https://github.com/unslothai/unsloth, at the repository root.

- Commit: [`fdbc8676a372670d30bc282fd252c51a0b0a2f39`](https://github.com/unslothai/unsloth/tree/fdbc8676a372670d30bc282fd252c51a0b0a2f39) (2026-09-27)
- License: Apache-2.0, https://github.com/unslothai/unsloth/blob/fdbc8676a372670d30bc282fd252c51a0b0a2f39/LICENSE

## What was kept

- `pyproject.toml`: 154 of its 1738 lines, each as in the original: `[build-system]`,
  `[project]` (name, dynamic, requires-python, license, the 8 base dependencies),
  `[project.scripts]`, and 14 of its 195 extras, some with only some of their lines:
  `studio`, `triton`, `huggingfacenotorch`, `audio-torch211`, `audio-torch280`,
  `huggingface`, `windows`, `cu118only`, `cu126onlytorch2100`, `cu126onlytorch2121`,
  `cu130onlytorch2121`, `cu118-torch211`, `flashattention` and `intelgputorch260`.

Apart from what is said here, kept lines are as in the original file.

## What it tests

- pins for one Python only (`typer==0.27.1 ; python_version >= '3.10'`,
  `typer==0.23.2 ; python_version < '3.10'`): the one for the project's Python;
- markers on the platform and the machine (`platform_machine == 'ARM64'`), which give the
  same result on every machine the scan runs on;
- one name declared in many extras: plain PyPI requirements win over direct URLs
  (`torch`, `xformers`), the newest pin wins (`torch==2.12.1+cu130`), and names without a
  pin get the range all their declarations allow;
- a name declared only by direct URL (`pytorch_triton_xpu`): not looked up on PyPI.
