"""Re-record the real API diffs in tests/fixtures/diffs from PyPI.

    python scripts/record_diff_fixtures.py                          # every fixture
    python scripts/record_diff_fixtures.py mcp-1.28.1-2.2.0.json    # only this one
    python scripts/record_diff_fixtures.py --check                  # write nothing

Each fixture names its package and its two versions. Both releases are downloaded with
`PyPI.source()` into the since-cutoff cache (`SINCE_CUTOFF_CACHE` puts it elsewhere) and diffed
with `diff_sources()`, as `since-cutoff scan` does; no model is called. The file keeps its shape:
`schema` (the current DIFF_SCHEMA), `package`, `from_version`, `to_version`, `import_names` and
`changes`, and a fixture with `kinds` keeps only the changes of those kinds
(`openai-2.44.0-3.19.2-switch.json` keeps the `dependency_switched` one of its diff).

Run it after a DIFF_SCHEMA bump (the tests that read a fixture fail until then), then read the
fixtures' diff and update the tests that quote them. To add a pair, write a file with its
`package`, `from_version` and `to_version` (and `kinds`) and run the script with its name.
Each fixture prints whether it changed,
and which changes, by kind, path and parameter, appeared or went missing. With `--check`
nothing is written and the exit code is 1 when a fixture would change. The same releases give
the same file on every machine.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from since_cutoff.apidiff import DIFF_SCHEMA, diff_sources
from since_cutoff.cache import DiskCache
from since_cutoff.errors import PackageIndexError
from since_cutoff.pypi import PyPI

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "diffs"


def record(pypi: Any, fixture: dict[str, Any]) -> dict[str, Any]:
    """The fixture as the code diffs its two releases now (``pypi``: what ``PyPI.source``
    gives)."""
    package = fixture["package"]
    old = pypi.source(package, fixture["from_version"])
    new = pypi.source(package, fixture["to_version"])
    names = sorted(set(old.import_names) | set(new.import_names))
    changes = diff_sources(
        package,
        old.version,
        old.root,
        new.version,
        new.root,
        names,
        old_requires=old.requires,
        new_requires=new.requires,
        new_compiled=list(new.compiled),
    )
    data: dict[str, Any] = {
        "schema": DIFF_SCHEMA,
        "package": package,
        "from_version": old.version,
        "to_version": new.version,
        "import_names": names,
    }
    if "kinds" in fixture:
        data["kinds"] = fixture["kinds"]
        changes = [c for c in changes if c["kind"] in fixture["kinds"]]
    data["changes"] = changes
    return data


def text(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=1) + "\n"


def _keys(data: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {(c["kind"], c["path"], c.get("parameter") or "") for c in data.get("changes", ())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("fixtures", nargs="*", help="file names in tests/fixtures/diffs")
    parser.add_argument("--check", action="store_true", help="write nothing")
    args = parser.parse_args(argv)
    paths = [FIXTURES / Path(name).name for name in args.fixtures]
    pypi = PyPI(DiskCache())
    changed = 0
    for path in paths or sorted(FIXTURES.glob("*.json")):
        before = json.loads(path.read_text(encoding="utf-8"))
        try:
            after = record(pypi, before)
        except PackageIndexError as exc:
            print(f"{path.name}: {exc}", file=sys.stderr)
            return 2
        if text(after) == path.read_text(encoding="utf-8"):
            print(f"{path.name}: unchanged")
            continue
        changed += 1
        if not args.check:
            path.write_text(text(after), encoding="utf-8", newline="\n")
        verb = "would change" if args.check else "re-recorded"
        count = len(after["changes"])
        print(f"{path.name}: {verb} ({count} change{'' if count == 1 else 's'})")
        appeared, missing = _keys(after) - _keys(before), _keys(before) - _keys(after)
        for sign, keys in (("+", appeared), ("-", missing)):
            for kind, where, parameter in sorted(keys):
                print(f"  {sign} {kind} {where}" + (f" ({parameter})" if parameter else ""))
        if not appeared and not missing:
            print("  the same changes, with other details")
    return 1 if args.check and changed else 0


if __name__ == "__main__":
    sys.exit(main())
