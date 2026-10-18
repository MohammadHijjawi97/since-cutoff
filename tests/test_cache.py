"""The disk cache: where it lives, that a failed or unreadable entry never loses data, its
sizes (``cache info``), and the cap on the extracted sources."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from since_cutoff import cache as cache_module
from since_cutoff.cache import (
    MB,
    SOURCES_MARKER,
    DiskCache,
    clear,
    default_cache_dir,
    evict_tree,
    max_sources_bytes,
    source_entries,
    sources_over_cap,
    stats,
)


@pytest.mark.parametrize(
    ("platform", "env", "expected"),
    [
        ("win32", {"LOCALAPPDATA": "local"}, "local/since-cutoff/Cache"),
        ("win32", {}, "home/AppData/Local/since-cutoff/Cache"),
        ("darwin", {"XDG_CACHE_HOME": "xdg"}, "home/Library/Caches/since-cutoff"),
        ("linux", {"XDG_CACHE_HOME": "xdg"}, "xdg/since-cutoff"),
        ("linux", {}, "home/.cache/since-cutoff"),
    ],
    ids=["windows", "windows-no-localappdata", "macos", "xdg", "linux"],
)
def test_the_cache_follows_each_platforms_convention(
    monkeypatch, tmp_path, platform, env, expected
) -> None:
    for name in ("SINCE_CUTOFF_CACHE", "LOCALAPPDATA", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, str(tmp_path / value))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(cache_module, "sys", SimpleNamespace(platform=platform))
    assert default_cache_dir() == tmp_path / expected


def test_since_cutoff_cache_overrides_the_location(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("SINCE_CUTOFF_CACHE", "~/sc-cache")
    assert default_cache_dir() == tmp_path / "sc-cache"
    assert DiskCache().root == (tmp_path / "sc-cache").resolve()


def test_a_failed_write_keeps_the_previous_entry_and_no_temporary_file(tmp_path) -> None:
    cache = DiskCache(tmp_path)
    cache.set("answers", "k", {"text": "first"})
    loop: list[object] = []
    loop.append(loop)  # cannot be written as JSON
    with pytest.raises(ValueError, match="Circular reference"):
        cache.set("answers", "k", {"text": loop})
    assert cache.get("answers", "k") == {"text": "first"}
    assert [p.name for p in (tmp_path / "answers").iterdir()] == ["k.json"]


@pytest.mark.parametrize("content", [b'{"info": ', b"\xff\xfe\x00garbage", b""])
def test_an_unreadable_entry_is_a_miss_and_is_written_over(tmp_path, content) -> None:
    cache = DiskCache(tmp_path)
    path = cache.path("pypi", "toy")
    path.parent.mkdir()
    path.write_bytes(content)  # a disk that filled up, or a crash of an older version
    assert cache.get("pypi", "toy") is None
    cache.set("pypi", "toy", {"releases": {"1.0": []}})
    assert cache.get("pypi", "toy") == {"releases": {"1.0": []}}


def test_a_disabled_cache_writes_nothing(tmp_path) -> None:
    """``run --fresh`` asks the model again and stores nothing."""
    DiskCache(tmp_path, enabled=False).set("answers", "new", {"text": "x"})
    assert not (tmp_path / "answers").exists()
    DiskCache(tmp_path).set("answers", "old", {"text": "cached"})
    fresh = DiskCache(tmp_path, enabled=False)
    assert fresh.get("answers", "old") is None  # not read either
    fresh.set("answers", "old", {"text": "fresh"})
    assert DiskCache(tmp_path).get("answers", "old") == {"text": "cached"}


# ------------------------------------------------------------------- sizes
def test_stats_count_entries_files_and_bytes_per_kind(tmp_path) -> None:
    root = tmp_path / "cache"
    (root / "pypi").mkdir(parents=True)
    (root / "pypi" / "toy.json").write_bytes(b"x" * 10)
    tree = root / "sources" / "toy-1.0"
    (tree / "toy").mkdir(parents=True)
    (tree / "toy" / "__init__.py").write_bytes(b"y" * 20)
    (tree / SOURCES_MARKER).write_text("{}", encoding="utf-8")
    long_ago = time.time() - 5 * 86400
    os.utime(tree / SOURCES_MARKER, (long_ago, long_ago))
    by_name = {s.name: s for s in stats(root)}
    assert [s.name for s in stats(root)] == list(cache_module.NAMESPACES)
    pypi, sources, diffs = by_name["pypi"], by_name["sources"], by_name["diffs"]
    assert (pypi.entries, pypi.files, pypi.bytes) == (1, 1, 10)
    assert (sources.entries, sources.files, sources.bytes) == (1, 2, 22)
    assert sources.oldest == pytest.approx(long_ago, abs=2)  # the marker's mtime: last used
    assert (diffs.entries, diffs.files, diffs.bytes, diffs.oldest) == (0, 0, 0, None)
    assert [s.name for s in stats(root) if s.model_output] == ["tasks", "answers", "notes"]
    # A cache that does not exist yet.
    assert all((s.entries, s.bytes, s.oldest) == (0, 0, None) for s in stats(tmp_path / "none"))


def test_clear_removes_only_the_given_kinds(tmp_path) -> None:
    root = tmp_path / "cache"
    for name in ("pypi", "sources", "diffs", "answers"):
        (root / name / "entry").mkdir(parents=True)
    assert clear(root, ["sources", "sources-meta"]) == ["sources"]  # sources-meta was not there
    assert sorted(p.name for p in root.iterdir()) == ["answers", "diffs", "pypi"]
    assert clear(root) == ["pypi", "diffs", "answers"]
    assert not root.exists()  # nothing else was in it


# ------------------------------------------------------------- sources cap
def test_the_sources_cap_comes_from_the_environment(monkeypatch, caplog) -> None:
    monkeypatch.delenv("SINCE_CUTOFF_CACHE_MAX_MB", raising=False)
    assert max_sources_bytes() == 2048 * MB
    monkeypatch.setenv("SINCE_CUTOFF_CACHE_MAX_MB", "100")
    assert max_sources_bytes() == 100 * MB
    monkeypatch.setenv("SINCE_CUTOFF_CACHE_MAX_MB", "1.5")
    assert max_sources_bytes() == int(1.5 * MB)
    for off in ("0", "-5", " "):
        monkeypatch.setenv("SINCE_CUTOFF_CACHE_MAX_MB", off)
        assert max_sources_bytes() == (None if off.strip() else 2048 * MB)
    # float() takes infinities and NaN; none of them is a size (int(inf) would raise).
    for nonsense in ("lots", "inf", "-inf", "nan", "1e400", "Infinity"):
        monkeypatch.setenv("SINCE_CUTOFF_CACHE_MAX_MB", nonsense)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="since_cutoff.cache"):
            assert max_sources_bytes() == 2048 * MB
        assert f"SINCE_CUTOFF_CACHE_MAX_MB={nonsense!r} is not a number" in caplog.text


def make_tree(root: Path, key: str, *, size: int, used_ago: float, record: bool = True) -> Path:
    """A published source tree of ``size`` bytes, last used ``used_ago`` seconds ago; without
    ``record``, as versions before 0.6 wrote it: with no size in the marker."""
    tree = root / "sources" / key
    (tree / "pkg").mkdir(parents=True)
    (tree / "pkg" / "__init__.py").write_bytes(b"#" * size)
    marker = {"schema": 2, "import_names": ["pkg"], "requires": []}
    if record:
        marker["bytes"] = size
    (tree / SOURCES_MARKER).write_text(json.dumps(marker), encoding="utf-8")
    stamp = time.time() - used_ago
    os.utime(tree / SOURCES_MARKER, (stamp, stamp))
    return tree


def test_the_least_recently_used_trees_go_first_never_a_kept_or_a_recent_one(tmp_path) -> None:
    day = 86400
    make_tree(tmp_path, "a-1.0", size=100, used_ago=3 * day)
    make_tree(tmp_path, "kept-1.0", size=100, used_ago=2.5 * day)
    make_tree(tmp_path, "b-1.0", size=100, used_ago=2 * day)
    make_tree(tmp_path, "c-1.0", size=100, used_ago=1 * day)
    make_tree(tmp_path, "recent-1.0", size=100, used_ago=600)  # another process may read it
    assert [e.key for e in source_entries(tmp_path)] == [
        "a-1.0",
        "kept-1.0",
        "b-1.0",
        "c-1.0",
        "recent-1.0",
    ]
    # 500 bytes in all, 250 allowed: a, then b (kept-1.0 is in use), then c brings it to 200.
    plan = sources_over_cap(tmp_path, 250, keep={"kept-1.0"})
    assert [e.key for e in plan] == ["a-1.0", "b-1.0", "c-1.0"]
    # Only the recent one could still go, and it never does: the cap is not met.
    assert [e.key for e in sources_over_cap(tmp_path, 50, keep={"kept-1.0"})] == [
        "a-1.0",
        "b-1.0",
        "c-1.0",
    ]
    assert sources_over_cap(tmp_path, 500) == []  # within the cap: nothing to do


def test_a_tree_extracted_before_sizes_were_recorded_is_measured_once(tmp_path) -> None:
    tree = make_tree(tmp_path, "old-1.0", size=300, used_ago=86400, record=False)
    marker = tree / SOURCES_MARKER
    used = marker.stat().st_mtime
    [entry] = source_entries(tmp_path)
    assert entry.bytes == 300  # the files, not the marker
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["bytes"] == 300 and data["import_names"] == ["pkg"]  # the rest is kept
    assert marker.stat().st_mtime == used  # measuring is not using


def test_a_tree_being_extracted_or_broken_is_not_counted(tmp_path) -> None:
    make_tree(tmp_path, "ok-1.0", size=10, used_ago=86400)
    (tmp_path / "sources" / "new-1.0.tmp-x7" / "pkg").mkdir(parents=True)  # no marker yet
    broken = tmp_path / "sources" / "broken-1.0"
    broken.mkdir()
    (broken / SOURCES_MARKER).write_text("{", encoding="utf-8")
    assert [e.key for e in source_entries(tmp_path)] == ["ok-1.0"]
    assert source_entries(tmp_path / "none") == []


def test_evict_tree_removes_the_tree_and_leaves_nothing_behind(tmp_path) -> None:
    tree = make_tree(tmp_path, "a-1.0", size=10, used_ago=0)
    assert evict_tree(tree) is True
    assert list((tmp_path / "sources").iterdir()) == []
    assert evict_tree(tree) is False  # already gone


@pytest.mark.parametrize("leftover", ["a-1.0.tmp-evicted", "a-1.0.tmp-evicted-0123abcd"])
def test_what_an_interrupted_eviction_left_is_no_tree_and_in_nobodys_way(
    tmp_path, leftover
) -> None:
    # The process stopped after renaming a-1.0 away and before deleting it (0.6.0 named every
    # one ``<key>.tmp-evicted``); a-1.0 was then extracted again.
    gone = make_tree(tmp_path, leftover, size=100, used_ago=3 * 86400)
    tree = make_tree(tmp_path, "a-1.0", size=10, used_ago=86400)
    assert [e.key for e in source_entries(tmp_path)] == ["a-1.0"]
    sources = {k.name: k for k in stats(tmp_path, ["sources"])}["sources"]
    assert (sources.entries, sources.files, sources.bytes) == (1, 4, gone_size(gone, tree))
    assert sources.oldest == pytest.approx(time.time() - 86400, abs=60)
    assert evict_tree(tree) is True  # the leftover's name is not in the way
    assert [p.name for p in (tmp_path / "sources").iterdir()] == [leftover]
    cache_module.remove_evicted_leftovers(tmp_path)
    assert list((tmp_path / "sources").iterdir()) == []
    cache_module.remove_evicted_leftovers(tmp_path / "none")  # no cache: nothing to do


def gone_size(*trees: Path) -> int:
    return sum(p.stat().st_size for t in trees for p in t.rglob("*") if p.is_file())
