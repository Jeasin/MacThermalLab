"""`mtl mark` 的输入方式。

用户的实际用法是随口一句，不想为空格操心：

    mtl mark 风扇开
    mtl mark 风扇 开到最大

所以事件名收 `nargs="+"`，把词拼回一句话，不再要求加引号。
"""

from __future__ import annotations

from mac_thermal_lab import cli
from mac_thermal_lab.paths import resolve_paths
from mac_thermal_lab.storage import (
    SessionPaths,
    acquire_lock,
    iter_jsonl,
    write_json,
)


def _active_session() -> tuple[SessionPaths, "object"]:
    root = resolve_paths().recordings_dir / "2026-11-14_201000_oot"
    root.mkdir(parents=True, exist_ok=True)
    paths = SessionPaths(root)
    write_json(
        paths.metadata,
        {
            "schema": 1,
            "session_id": paths.name,
            "name": "oot",
            "started_at": "2026-11-14T11:05:52.000+00:00",
            "ended_at": None,
            "interval_ms": 2000,
            "status": "running",
            "thermal": {"enabled": False, "sudo_mode": "disabled", "error": None},
        },
    )
    paths.ensure_runtime_dirs()
    lock = acquire_lock(paths.lock)
    assert lock is not None
    write_json(paths.state, {"status": "running", "pid": 0, "interval_ms": 2000})
    return paths, lock


def test_mark_accepts_unquoted_words(capsys):
    paths, lock = _active_session()
    try:
        assert cli.main(["mark", "风扇开"]) == 0
        assert cli.main(["mark", "风扇", "开到", "最大"]) == 0
    finally:
        lock.close()

    events = [e["event"] for e in iter_jsonl(paths.events)]
    assert events == ["风扇开", "风扇 开到 最大"]
    out = capsys.readouterr().out
    assert "风扇 开到 最大" in out


def test_mark_keeps_optional_note_separate(capsys):
    paths, lock = _active_session()
    try:
        assert cli.main(["mark", "换", "充电器", "-n", "65W"]) == 0
    finally:
        lock.close()

    event = list(iter_jsonl(paths.events))[0]
    assert event["event"] == "换 充电器"
    assert event["note"] == "65W"
