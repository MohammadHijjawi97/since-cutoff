# psf/requests-html

An excerpt of `Pipfile` and `Pipfile.lock` from https://github.com/psf/requests-html, at the repository root.

- Commit: [`075ac162dc62fc532037df0d98954ab840a97516`](https://github.com/psf/requests-html/tree/075ac162dc62fc532037df0d98954ab840a97516) (2023-04-03)
- License: MIT, https://github.com/psf/requests-html/blob/075ac162dc62fc532037df0d98954ab840a97516/LICENSE

## What was kept

- `Pipfile.lock`: 11 of the 22 `default` packages and 12 of the 69 `develop` ones (the
  project's own editable entry, `requests-html`, included), and `_meta`. Each `hashes`
  list is cut to its first hash, which loses its comma. Written as Pipenv writes it (4-space
  JSON, sorted keys).
- `Pipfile`: the whole file.

Apart from what is said here, kept lines are as in the original file.

## What it tests

- the project itself installed for its tests: `e1839a8 = {path = ".", editable = true}`
  (Pipenv names it after a hash), which is not a dependency;
- `"*"` everywhere: the versions come from the lock alone;
- lock entries with markers (`tomli`, `jeepney`).
