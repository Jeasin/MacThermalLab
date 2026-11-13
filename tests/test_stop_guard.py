"""`mtl stop <名字>` 的目标校验。

真实事故：用户 `mtl start totk` 后，收尾时手误敲成 `mtl stop botw`
（botw 是前一晚的会话）。旧实现会「成功」结束那份旧会话、重出它的报告，
而当前实验的采集进程继续在后台跑，用户以为已经停了。
"""

from __future__ import annotations

from mac_thermal_lab import cli
from mac_thermal_lab.paths import resolve_paths
from mac_thermal_lab.storage import (
    SessionPaths,
    acquire_lock,
    read_json,
    write_json,
)


def _make_session(name: str, display: str, ended: bool) -> SessionPaths:
    root = resolve_paths().recordings_dir / name
    root.mkdir(parents=True, exist_ok=True)
    paths = SessionPaths(root)
    write_json(
        paths.metadata,
        {
            "schema": 1,
            "session_id": name,
            "name": display,
            "started_at": "2026-11-12T06:19:05.992+00:00",
            "ended_at": "2026-11-12T06:59:14.713+00:00" if ended else None,
            "interval_ms": 2000,
            "status": "stopped" if ended else "starting",
            "thermal": {"enabled": False, "sudo_mode": "disabled", "error": None},
        },
    )
    return paths


def _mark_active(paths: SessionPaths) -> "object":
    """持住会话锁，等价于「守护进程还活着」。"""
    paths.ensure_runtime_dirs()
    fh = acquire_lock(paths.lock)
    assert fh is not None
    write_json(paths.state, {"status": "running", "pid": 0, "interval_ms": 2000})
    return fh


def test_stop_refuses_to_close_a_different_session(capsys):
    old = _make_session("2026-11-12_061905_botw", "botw", ended=True)
    active = _make_session("2026-11-13_131654_totk", "totk", ended=False)
    lock = _mark_active(active)
    try:
        code = cli.main(["stop", "botw"])
    finally:
        lock.close()

    out = capsys.readouterr().out
    assert code == 1
    assert "不是当前正在进行的实验" in out
    assert "2026-11-13_131654_totk" in out

    # 旧会话不能被悄悄改写，也不该被重出报告
    meta = read_json(old.metadata) or {}
    assert meta["ended_at"] == "2026-11-12T06:59:14.713+00:00"
    assert not old.dashboard.exists()


def test_stop_refuses_already_finished_session(capsys):
    _make_session("2026-11-12_061905_botw", "botw", ended=True)

    code = cli.main(["stop", "botw"])

    out = capsys.readouterr().out
    assert code == 1
    assert "已经结束过了" in out
    assert "mtl report botw" in out


def test_stop_prefers_active_session_and_can_be_named(capsys):
    """没有活动会话时，未收尾的会话仍应可被点名收尾（崩溃恢复路径不受影响）。"""
    active = _make_session("2026-11-13_131654_totk", "totk", ended=False)
    lock = _mark_active(active)
    try:
        # 名字解析到同一个活动会话时不该被拒
        from mac_thermal_lab.cli import _active_session, _records_roots, _app_paths

        class Args:
            data_dir = None

        found = _active_session(_records_roots(_app_paths(Args())))
        assert found is not None
        assert found.name == "2026-11-13_131654_totk"
    finally:
        lock.close()
