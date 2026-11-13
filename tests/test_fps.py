"""Metal HUD 帧率解析测试。

样本格式来自 Apple 文档：metal-HUD: 首帧号,估计丢帧,进程内存MB,帧间隔ms,GPU时间ms,...
（后半段是每帧一对，条数不固定）
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from mac_thermal_lab import fps


def _record(message: str, ts: datetime, process: str = "Game") -> dict:
    return {
        "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S.%f+0800"),
        "eventMessage": message,
        "processImagePath": f"/Applications/{process}.app/Contents/MacOS/{process}",
    }


def test_parse_hud_message_units_and_fps():
    # 3 帧，间隔约 16.7ms → 60 FPS 左右
    parsed = fps.parse_hud_message(
        "metal-HUD: 12000,0,56.05,16.67,8.32,16.60,8.10,16.90,8.50"
    )
    assert parsed is not None
    assert parsed["frame_no"] == 12000
    assert parsed["missed"] == 0
    assert parsed["memory_mb"] == pytest.approx(56.05)
    assert parsed["frames"] == 3
    assert 59.0 < parsed["fps"] < 60.2
    assert parsed["interval_ms"] == pytest.approx(16.723, abs=0.01)
    assert parsed["gpu_ms"] == pytest.approx(8.31, abs=0.02)
    # 1% low 取最差一帧 → 比平均低
    assert parsed["fps_1pct"] < parsed["fps"]


def test_parse_hud_message_rejects_non_hud_and_short_lines():
    assert fps.parse_hud_message("something else entirely") is None
    assert fps.parse_hud_message(None) is None
    assert fps.parse_hud_message("metal-HUD: 12") is None


def test_parse_hud_message_tolerates_trailing_empty_fields():
    parsed = fps.parse_hud_message("metal-HUD: 500,2,88.0,33.30,12.0,,")
    assert parsed is not None
    assert parsed["frames"] == 1
    assert parsed["fps"] == pytest.approx(30.03, abs=0.1)


def test_parse_log_timestamp_handles_compact_offset():
    ts = fps.parse_log_timestamp("2026-11-13 14:00:00.123456+0800")
    assert ts is not None
    assert ts.utcoffset() == timedelta(hours=8)
    assert ts.astimezone(timezone.utc).hour == 6
    assert fps.parse_log_timestamp("not a time") is None


def test_stream_parser_extracts_sample_and_process():
    parser = fps.HudStreamParser()
    base = datetime(2026, 11, 13, 14, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    sample = parser.feed(_record("metal-HUD: 600,0,120.5,16.70,9.0,16.70,9.0", base))
    assert sample is not None
    assert sample["process"] == "Game"
    assert sample["fps"] == pytest.approx(59.88, abs=0.1)
    assert sample["frames"] == 2
    # 时间戳必须带时区，且换到 UTC 存
    assert sample["timestamp"].endswith("+00:00")


def test_stream_parser_falls_back_to_frame_delta():
    """帧间隔字段缺失时，用帧号差分兜底，而不是直接丢样本。"""
    parser = fps.HudStreamParser()
    t0 = datetime(2026, 11, 13, 14, 0, 0, tzinfo=timezone.utc)
    first = parser.feed(_record("metal-HUD: 1000,0,50.0", t0))
    assert first is not None
    assert first["fps"] is None  # 第一条没有参照，只能空

    second = parser.feed(_record("metal-HUD: 1060,0,50.0", t0 + timedelta(seconds=1)))
    assert second is not None
    assert second["fps"] == pytest.approx(60.0, abs=0.01)
    assert second["fps_by_count"] == pytest.approx(60.0, abs=0.01)


def test_stream_parser_process_filter():
    parser = fps.HudStreamParser("ryujinx")
    ts = datetime(2026, 11, 13, 14, 0, 0, tzinfo=timezone.utc)
    message = "metal-HUD: 10,0,10.0,16.7,8.0"
    assert parser.feed(_record(message, ts, process="Safari")) is None
    assert parser.feed(_record(message, ts, process="Ryujinx")) is not None


def test_summarize_reports_1pct_low_and_processes():
    samples = [
        {"fps": 60.0, "process": "Game"},
        {"fps": 30.0, "process": "Game"},
        {"fps": 60.0, "process": "Game"},
        {"fps": None, "process": "Game"},
    ]
    result = fps.summarize(samples)
    assert result["fps_samples"] == 3
    assert result["fps_avg"] == pytest.approx(50.0)
    assert result["fps_min"] == 30.0
    assert result["fps_max"] == 60.0
    assert result["fps_1pct_low"] == 30.0
    assert result["processes"] == ["Game"]


def test_build_log_stream_command_uses_absolute_log_and_ndjson():
    cmd = fps.build_log_stream_command()
    assert cmd[0] == "/usr/bin/log"
    assert "--style" in cmd and "ndjson" in cmd
    filtered = fps.build_log_stream_command("Ryujinx")
    predicate = filtered[filtered.index("--predicate") + 1]
    assert "metal-HUD" in predicate
    assert "Ryujinx" in predicate


def test_specimen_line_from_jsonl_roundtrip(tmp_path):
    """整条 ndjson 走一遍 HudStreamParser，确认不会因字段名不同而失效。"""
    ts = datetime(2026, 11, 13, 14, 0, 0, tzinfo=timezone.utc)
    record = _record("metal-HUD: 900,1,64.0,16.67,8.0,16.67,8.0,16.67,8.0", ts)
    path = tmp_path / "hud.log"
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    parser = fps.HudStreamParser()
    samples = [
        parser.feed(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(samples) == 1
    assert samples[0]["missed"] == 1
    assert samples[0]["fps"] == pytest.approx(59.99, abs=0.1)


# --------------------------------------------------------------------------- #
# 端到端：fps.jsonl → 合并 → 报告
# --------------------------------------------------------------------------- #
def _fps_session(tmp_path):
    from mac_thermal_lab.storage import SessionPaths, write_json

    root = tmp_path / "2026-11-13_140000_game"
    root.mkdir()
    paths = SessionPaths(root)
    start = datetime(2026, 11, 13, 6, 0, 0, tzinfo=timezone.utc)
    write_json(
        paths.metadata,
        {
            "schema": 1,
            "session_id": root.name,
            "name": "帧率测试",
            "started_at": start.isoformat(),
            "ended_at": (start + timedelta(seconds=30)).isoformat(),
            "interval_ms": 2000,
            "status": "stopped",
            "host": {"os_version": "26.5.2"},
            "soc": {"chip_name": "Apple M5"},
            "thermal": {"enabled": False, "sudo_mode": "disabled", "error": None},
            "game": {"enabled": True, "filter": None, "process": "Game", "error": None},
        },
    )
    lines = []
    for i in range(5):
        ts = start + timedelta(seconds=i)
        sample = {
            "timestamp": ts.isoformat(),
            "process": "Game",
            "fps": 58.0 + i,
            "fps_1pct": 45.0,
            "frames": 60,
            "interval_ms": 17.1,
            "gpu_ms": 8.0,
        }
        lines.append(json.dumps(sample))
    paths.fps_samples.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return paths


def test_fps_samples_reach_merged_rows_and_summary(tmp_path):
    from mac_thermal_lab.analysis import build_merged_rows, load_session, summary

    paths = _fps_session(tmp_path)
    data = load_session(paths)
    assert len(data.fps) == 5

    rows = build_merged_rows(data)
    hud_rows = [r for r in rows if r["source"] == "metal-hud"]
    assert len(hud_rows) == 5
    assert hud_rows[0]["fps"] == 58.0
    assert hud_rows[0]["game_process"] == "Game"

    result = summary(data)
    assert result["fps"]["fps_samples"] == 5
    assert result["fps"]["fps_avg"] == pytest.approx(60.0)
    assert result["fps"]["processes"] == ["Game"]


def test_report_renders_fps_panel(tmp_path):
    import re

    from mac_thermal_lab.report import generate_report

    paths = _fps_session(tmp_path)
    path = generate_report(paths)
    html = path.read_text(encoding="utf-8")
    # 子图标题走 plotly JSON，中文会被转义成 \uXXXX，所以只查 ASCII 部分
    assert "Metal HUD" in html
    assert "帧率统计" in html
    assert html.count("Plotly.newPlot") == 1
    # 内嵌的 plotly.js 源码里本来就有 cdn.plot.ly 这个默认配置字符串，
    # 所以判据是「没有外部 script/link 引用」，不是字符串不出现。
    assert not re.search(r"<script[^>]+src=", html)
