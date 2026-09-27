"""The disk cache: where it lives, and that a failed or unreadable entry never loses data."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from since_cutoff import cache as cache_module
from since_cutoff.cache import DiskCache, default_cache_dir


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
