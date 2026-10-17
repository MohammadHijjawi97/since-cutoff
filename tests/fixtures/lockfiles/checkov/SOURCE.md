# bridgecrewio/checkov

An excerpt of `Pipfile` and `Pipfile.lock` from https://github.com/bridgecrewio/checkov, at the repository root.

- Commit: [`d89e8dd4cf64fa51a4c3b72561b151fdd5c67cb7`](https://github.com/bridgecrewio/checkov/tree/d89e8dd4cf64fa51a4c3b72561b151fdd5c67cb7) (2026-09-27)
- License: Apache-2.0, https://github.com/bridgecrewio/checkov/blob/d89e8dd4cf64fa51a4c3b72561b151fdd5c67cb7/LICENSE

## What was kept

- `Pipfile.lock`: 19 of the 99 `default` packages and 13 of the 87 `develop` ones, and
  `_meta`. Each `hashes` list is cut to its first hash, which loses its comma. Written as
  Pipenv writes it (4-space JSON, sorted keys).
- `Pipfile`: the lines of the kept packages, `[[source]]` and `[requires]`.

Apart from what is said here, kept lines are as in the original file.

## What it tests

- `[packages]` and `[dev-packages]` of a Pipfile, both direct, and the lock's own
  transitive packages;
- a package in both sections (`jsonschema`);
- table entries with markers and an index (`pyston`), extras (`boto3-stubs-lite`) and
  `==` pins that agree with the lock (`coverage`, `setuptools`).
