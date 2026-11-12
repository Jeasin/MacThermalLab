"""时间戳、目录与锁的基础行为。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from mac_thermal_lab import storage


def test_parse_iso_with_offset():
    ts = storage.parse_timestamp("2026-11-11T11:43:35.377979+00:00")
    assert ts is not None
    assert ts.tzinfo == timezone.utc
    assert ts.hour == 11


def test_parse_iso_z_suffix():
    ts = storage.parse_timestamp("2026-11-11T11:43:35Z")
    assert ts is not None and ts.hour == 11


def test_parse_powermetrics_style():
    ts = storage.parse_timestamp("Wed Nov 11 19:43:21 2026 +0800")
    assert ts is not None
    assert ts.astimezone(timezone.utc).hour == 11


def test_parse_space_padded_day():
    ts = storage.parse_timestamp("Wed Nov  5 19:43:21 2026 +0800")
    assert ts is not None
    assert ts.day == 5


def test_parse_naive_treated_as_local():
    ts = storage.parse_timestamp("2026-11-11 19:43:21")
    assert ts is not None
    assert storage.fmt_local(ts) == "2026-11-11 19:43:21"


def test_parse_bad_values():
    assert storage.parse_timestamp(None) is None
    assert storage.parse_timestamp("") is None
    assert storage.parse_timestamp("yesterday") is None


def test_slugify_keeps_chinese():
    assert storage.slugify("烤机 测试/第二轮") == "烤机-测试-第二轮"
    assert storage.slugify("") == "session"


def test_create_session_dir_unique(tmp_path: Path):
    first = storage.create_session_dir(tmp_path, "test")
    second = storage.create_session_dir(tmp_path, "test")
    assert first.root != second.root
    assert first.root.exists() and second.root.exists()


def test_atomic_write_json(tmp_path: Path):
    path = tmp_path / "a.json"
    storage.write_json(path, {"x": 1})
    assert storage.read_json(path) == {"x": 1}
    storage.write_json(path, {"x": 2})
    assert storage.read_json(path) == {"x": 2}
    assert not (tmp_path / "a.json.tmp").exists()


def test_iter_jsonl_skips_broken_lines(tmp_path: Path):
    path = tmp_path / "x.jsonl"
    path.write_text('{"a":1}\nbroken\n{"a":2}\n', encoding="utf-8")
    assert [r["a"] for r in storage.iter_jsonl(path)] == [1, 2]


def test_lock_lifecycle(tmp_path: Path):
    lock = tmp_path / "daemon.lock"
    assert storage.lock_is_held(lock) is False  # 文件都不存在
    handle = storage.acquire_lock(lock)
    assert handle is not None
    assert storage.lock_is_held(lock) is True
    second = storage.acquire_lock(lock)
    assert second is None  # 同一把锁不能被抢
    handle.close()
    assert storage.lock_is_held(lock) is False


def test_fmt_duration():
    assert storage.fmt_duration(0) == "0s"
    assert storage.fmt_duration(75) == "1m15s"
    assert storage.fmt_duration(3675) == "1h01m15s"
    assert storage.fmt_duration(None) == "-"
