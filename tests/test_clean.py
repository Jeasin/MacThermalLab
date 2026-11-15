"""瘦身（mtl clean）：归档原始日志，只留能重建报告的数据。

关键约束是「一个数都不能变」：精简只允许丢每行重复的硬件描述，
合并后的时间轴必须与原始数据逐行一致。
"""

from __future__ import annotations

import json

from mac_thermal_lab import clean, cli
from mac_thermal_lab import macmon as macmon_mod
from mac_thermal_lab.analysis import build_merged_rows, load_session
from mac_thermal_lab.paths import resolve_paths
from mac_thermal_lab.report import generate_report
from mac_thermal_lab.storage import (
    acquire_lock,
    read_json,
    write_json,
)


def _rows(data) -> list[str]:
    return [json.dumps(row, sort_keys=True, default=str) for row in build_merged_rows(data)]


def test_slim_record_drops_repeated_hardware_description():
    record = {
        "timestamp": "2026-11-14T11:06:00.921393+00:00",
        "cpu_power": 2.5,
        "pcpu_freq_mhz": 4248,
        "pcpu_active_ratio": 0.0,
        "ecpu_freq_mhz": 2368,
        "ecpu_active_ratio": 0.2,
        "cpu_tiers": [{"label": "E", "cores": 6, "freqs": [972, 1152]}],
        "ecpu_cores": [{"core_id": 0, "freq_mhz": 2368}],
        "pcpu_cores": [{"core_id": 0, "freq_mhz": 4248}],
        "soc": {
            "chip_name": "Apple M5",
            "mac_model": "Mac17,3",
            "pcpu_cores": 4,
            "cpu_tiers": [{"cores": 6}],
            "pcpu_freqs": [972, 1152],
            "gpu_freqs": [0, 338],
        },
    }
    slim = macmon_mod.slim_record(record)

    assert "cpu_tiers" not in slim
    assert "ecpu_cores" not in slim and "pcpu_cores" not in slim
    # 型号与核数留着，频率阶梯是常量，丢掉
    assert slim["soc"] == {"chip_name": "Apple M5", "mac_model": "Mac17,3", "pcpu_cores": 4}
    assert slim["pcpu_active_ratio"] == 0.0               # 0.0 是真实值，不许当成缺失
    assert slim["cpu_power"] == 2.5


def test_clean_is_idempotent(session):
    app = resolve_paths()
    clean.clean_session(session, app)
    size_once = session.metrics_clean.stat().st_size
    clean.clean_session(session, app)
    assert session.metrics_clean.stat().st_size == size_once


def test_slim_record_keeps_tiers_when_顶层缺失():
    """某些 macmon 版本只给分档表：这时宁可不瘦身，也不能把数据删了。"""
    record = {
        "timestamp": "2026-11-14T11:06:00+00:00",
        "cpu_tiers": [{"label": "P", "freq_mhz": 4248, "active_ratio": 0.5}],
    }
    assert "cpu_tiers" in macmon_mod.slim_record(record)


def test_zero_active_ratio_is_not_treated_as_missing():
    """0.0 曾经被 `or` 吃掉，导致空闲核心的占用率变成空值。"""
    sample = macmon_mod.parse_sample({
        "timestamp": "2026-11-14T11:06:00+00:00",
        "pcpu_active_ratio": 0.0,
        "pcpu_freq_mhz": 4248,
        "cpu_tiers": [{"label": "P", "active_ratio": 0.9}],
    })
    assert sample is not None
    assert sample.get("pcpu_active_pct") == 0.0


def test_clean_is_lossless_for_merged_timeline(session):
    before = _rows(load_session(session))

    result = clean.clean_session(session, resolve_paths())

    assert result.skipped is None
    assert session.metrics_clean.exists()
    assert not session.macmon_raw.exists()
    after = _rows(load_session(session))
    assert after == before


def test_clean_moves_raw_into_cache_and_can_restore(session):
    app = resolve_paths()
    result = clean.clean_session(session, app)

    archive = clean.archive_dir(app, session)
    assert result.archived
    assert (archive / "macmon.jsonl").exists()
    assert (archive / "powermetrics.log").exists()
    meta = read_json(session.metadata)
    assert meta["raw_archived"]["files"]

    restored = clean.restore_raw(session, app)
    names = {path.name for path in restored}
    assert {"macmon.jsonl", "powermetrics.log"} <= names
    assert session.macmon_raw.exists()
    assert "raw_archived" not in read_json(session.metadata)


def test_restore_does_not_overwrite_newer_files(session):
    app = resolve_paths()
    clean.clean_session(session, app)
    session.macmon_raw.write_text('{"timestamp": "2026-11-14T11:06:00+00:00"}\n',
                                  encoding="utf-8")
    clean.restore_raw(session, app)
    assert json.loads(session.macmon_raw.read_text(encoding="utf-8")) == {
        "timestamp": "2026-11-14T11:06:00+00:00"
    }


def test_clean_skips_running_session(session):
    write_json(session.metadata, {**read_json(session.metadata), "ended_at": None})
    result = clean.clean_session(session, resolve_paths())
    assert result.skipped == "仍在采集中"
    assert session.macmon_raw.exists()


def test_report_can_be_rebuilt_after_raw_is_archived(session):
    clean.clean_session(session, resolve_paths())
    assert not session.macmon_raw.exists()

    html = generate_report(session).read_text(encoding="utf-8")
    assert "Plotly.newPlot" in html
    data = load_session(session)
    assert len(data.macmon) > 0
    assert any("已归档" in warning for warning in data.warnings)


def test_purge_removes_archive_but_keeps_session_data(session):
    app = resolve_paths()
    clean.clean_session(session, app)
    count, size = clean.purge_archive(session, app)

    assert count > 0 and size > 0
    assert not clean.archive_dir(app, session).exists()
    assert session.metrics_clean.exists()
    assert session.dashboard.exists()
    assert "raw_archived" not in read_json(session.metadata)


def test_cli_clean_dry_run_touches_nothing(session, capsys):
    assert cli.main(["--data-dir", str(session.root.parent), "clean", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "--dry-run" in out
    assert session.macmon_raw.exists()
    assert not session.metrics_clean.exists()


def test_cli_clean_then_purge(session, capsys):
    app = resolve_paths()
    data_dir = str(session.root.parent)
    assert cli.main(["--data-dir", data_dir, "clean"]) == 0
    assert "省" in capsys.readouterr().out
    assert clean.archive_dir(app, session).exists()

    assert cli.main(["--data-dir", data_dir, "clean", "--purge"]) == 0
    assert "释放" in capsys.readouterr().out


def test_lock_held_counts_as_running(session):
    session.ensure_runtime_dirs()
    lock = acquire_lock(session.lock)
    try:
        assert clean.is_running(session) is True
    finally:
        lock.close()
    assert clean.is_running(session) is False
