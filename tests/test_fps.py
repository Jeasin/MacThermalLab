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
    """帧间隔字段缺失时，用帧号差分兜底；第一条没有参照就不落库。"""
    parser = fps.HudStreamParser()
    t0 = datetime(2026, 11, 13, 14, 0, 0, tzinfo=timezone.utc)
    first = parser.feed(_record("metal-HUD: 1000,0,50.0", t0))
    assert first is None  # 第一条算不出帧率，不写进 fps.jsonl

    second = parser.feed(_record("metal-HUD: 1060,0,50.0", t0 + timedelta(seconds=1)))
    assert second is not None
    assert second["fps"] == pytest.approx(60.0, abs=0.01)
    assert second["fps_by_count"] == pytest.approx(60.0, abs=0.01)
    assert second["frames"] == 60


def test_stream_parser_process_filter():
    parser = fps.HudStreamParser("ryujinx")
    ts = datetime(2026, 11, 13, 14, 0, 0, tzinfo=timezone.utc)
    message = "metal-HUD: 10,0,10.0,16.7,8.0"
    assert parser.feed(_record(message, ts, process="Safari")) is None
    assert parser.feed(_record(message, ts, process="Ryujinx")) is not None


def test_stream_parser_filter_ignores_case_space_and_punctuation():
    """--game "Ocarina of Time" 要能对上进程名 OcarinaOfTime。"""
    parser = fps.HudStreamParser("Ocarina of Time")
    ts = datetime(2026, 11, 13, 14, 0, 0, tzinfo=timezone.utc)
    parser.feed(_record("metal-HUD: 100,0,10.0", ts, process="OcarinaOfTime"))
    sample = parser.feed(
        _record("metal-HUD: 160,0,10.0", ts + timedelta(seconds=1), process="OcarinaOfTime")
    )
    assert sample is not None and sample["fps"] == pytest.approx(60.0, abs=0.1)


def test_stream_parser_adopts_real_process_when_filter_never_matches():
    """1114 现场：--game ott 对不上 OcarinaOfTime，一条数据都收不到。

    进程过滤已经不在内核谓词里，所以能看见「其实有进程在写 HUD」，
    攒够 3 行就自动改用那个进程，别让整场白录。
    """
    parser = fps.HudStreamParser("ott")
    t0 = datetime(2026, 11, 14, 19, 10, 0, tzinfo=timezone.utc)
    samples = [
        parser.feed(
            _record(
                f"metal-HUD: {1000 + index * 30},0,120.0",
                t0 + timedelta(seconds=index * 0.5),
                process="OcarinaOfTime",
            )
        )
        for index in range(6)
    ]
    status = parser.status()
    assert status["seen"] == {"OcarinaOfTime": 6}
    assert status["rejected"] == {"OcarinaOfTime": 3}   # 前 3 行被挡，第 3 行后改用
    assert status["adopted_from"] == "ott"
    assert status["filter"] == "OcarinaOfTime"
    # 第 3 行开始收下；它是新的基线，算不出帧率，所以从第 4 行起才有样本
    assert [s is not None for s in samples] == [False, False, False, True, True, True]
    assert samples[-1]["fps"] == pytest.approx(60.0, abs=0.5)


def test_stream_parser_requires_three_lines_before_adopting():
    """HUD 环境变量是全局的，别让别的应用几十毫秒就把「游戏」身份抢走。"""
    parser = fps.HudStreamParser("ott")
    t0 = datetime(2026, 11, 14, 19, 10, 0, tzinfo=timezone.utc)
    for index in range(2):
        parser.feed(_record(f"metal-HUD: {index},0,10.0",
                            t0 + timedelta(seconds=index), process="Safari"))
    assert parser.status()["adopted_from"] is None
    assert parser.status()["seen"] == {"Safari": 2}


def test_stream_parser_drops_duplicate_log_delivery():
    """同一条 HUD 记录会被 log stream 投递两份，帧号相同——不能变成两条样本。

    实测日志：一份 subsystem 为空、一份 com.apple.metal.hud，时间戳差 0.1ms。
    以前两份都落库，第二条算不出帧率，报告里一半的点是空值，折线全断。
    """
    parser = fps.HudStreamParser()
    t0 = datetime(2026, 11, 13, 14, 0, 0, tzinfo=timezone.utc)
    first = parser.feed(_record("metal-HUD: 1000,0,50.0", t0))
    assert first is None  # 第一条只用来立基线
    duplicated = parser.feed(_record("metal-HUD: 1000,0,50.0", t0 + timedelta(milliseconds=0.1)))
    assert duplicated is None
    second = parser.feed(_record("metal-HUD: 1030,0,50.0", t0 + timedelta(seconds=0.5)))
    assert second is not None
    assert second["fps"] == pytest.approx(60.0, abs=0.01)
    assert second["frames"] == 30
    assert second["window_s"] == pytest.approx(0.5, abs=0.01)
    assert second["interval_ms"] == pytest.approx(16.67, abs=0.05)


def test_stream_parser_rebaselines_after_frame_number_reset():
    """游戏重启后帧号归零：重建基线，不产出负的帧率。"""
    parser = fps.HudStreamParser()
    t0 = datetime(2026, 11, 13, 14, 0, 0, tzinfo=timezone.utc)
    parser.feed(_record("metal-HUD: 90000,0,50.0", t0))
    restarted = parser.feed(_record("metal-HUD: 3,0,50.0", t0 + timedelta(seconds=1)))
    assert restarted is None
    after = parser.feed(_record("metal-HUD: 33,0,50.0", t0 + timedelta(seconds=1.5)))
    assert after is not None and after["fps"] == pytest.approx(60.0, abs=0.1)


def test_real_hud_line_with_redacted_private_tail():
    """真机日志的后半段被脱敏成 <private>，只剩三个标量，帧率靠帧号差分。"""
    parser = fps.HudStreamParser()
    t0 = datetime(2026, 11, 13, 15, 18, 29, tzinfo=timezone(timedelta(hours=8)))
    parser.feed(_record("metal-HUD: 38,1635.59,3536.35<private>", t0))
    sample = parser.feed(
        _record("metal-HUD: 67,1635.59,3536.35<private>", t0 + timedelta(seconds=0.5))
    )
    assert sample is not None
    assert sample["fps"] == pytest.approx(58.0, abs=0.01)
    assert sample["fps_1pct"] is None  # 逐帧间隔拿不到，只能留空
    assert sample["memory_mb"] == pytest.approx(3536.35)
    assert sample["interval_ms"] == pytest.approx(17.24, abs=0.02)


def test_summarize_1pct_low_is_weighted_by_frames():
    """一次短暂卡顿只贡献少量帧，不能仅凭「最低值」就把它算成 1% low。"""
    samples = [{"fps": 60.0, "frames": 60, "process": "Game"} for _ in range(100)]
    samples.append({"fps": 5.0, "frames": 2, "process": "Game"})   # 一次 0.4s 的停顿
    samples += [{"fps": 40.0, "frames": 20, "process": "Game"} for _ in range(10)]
    result = fps.summarize(samples)
    assert result["fps_1pct_source"] == "window"
    # 总帧数 ≈ 6000+2+200，1% ≈ 62 帧：最慢的 5 FPS 段只占 2 帧，轮不到它
    assert result["fps_1pct_low"] == 40.0


def test_summarize_1pct_low_prefers_real_frame_intervals():
    samples = [
        {"fps": 60.0, "frames": 60, "fps_1pct": 50.0},
        {"fps": 55.0, "frames": 55, "fps_1pct": 30.0},
    ]
    result = fps.summarize(samples)
    assert result["fps_1pct_source"] == "frame"
    assert result["fps_1pct_low"] == 30.0


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
    predicate = cmd[cmd.index("--predicate") + 1]
    assert "metal-HUD" in predicate
    # 进程名不再进谓词：谓词是内核硬过滤，词写错就连「其实有 HUD」都看不见
    assert "processImagePath" not in predicate
    assert "Ryujinx" not in predicate


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


def test_merge_game_notes_records_fallback():
    """自动改用进程这件事必须落进 metadata，否则事后无从解释。"""
    from mac_thermal_lab.collector import Collector

    game: dict = {"enabled": True}
    Collector._merge_game_notes(
        game,
        {
            "seen": {"OcarinaOfTime": 5},
            "rejected": {"OcarinaOfTime": 3},
            "adopted_from": "ott",
            "filter": "OcarinaOfTime",
        },
    )
    assert game["filter_fallback"] == {"requested": "ott", "used": "OcarinaOfTime"}
    assert game["hud_seen_processes"] == {"OcarinaOfTime": 5}
    assert game["process"] == "OcarinaOfTime"


def test_recover_from_log_parses_history(monkeypatch):
    """log show 的历史行和 log stream 同格式，补录走同一条解析路径。"""
    from mac_thermal_lab import fps

    t0 = datetime(2026, 11, 14, 19, 10, 0, tzinfo=timezone(timedelta(hours=8)))
    lines = [
        json.dumps(_record(f"metal-HUD: {100 + index * 30},0,10.0",
                           t0 + timedelta(seconds=index * 0.5), process="OcarinaOfTime"))
        for index in range(4)
    ]

    class FakeCompleted:
        returncode = 0
        stdout = "\n".join(lines)
        stderr = ""

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeCompleted()

    monkeypatch.setattr(fps.subprocess, "run", fake_run)
    result = fps.recover_from_log(t0, t0 + timedelta(minutes=1), "Ocarina of Time")

    assert result["error"] is None
    assert result["raw_lines"] == 4
    assert [round(s["fps"], 1) for s in result["samples"]] == [60.0, 60.0, 60.0]
    assert result["status"]["adopted_from"] is None      # 归一化后直接命中
    assert "show" in captured["cmd"] and "--start" in captured["cmd"]


def test_recover_from_log_reports_unmatched_filter(monkeypatch):
    from mac_thermal_lab import fps

    t0 = datetime(2026, 11, 14, 19, 10, 0, tzinfo=timezone(timedelta(hours=8)))
    lines = [
        json.dumps(_record(f"metal-HUD: {100 + index * 30},0,10.0",
                           t0 + timedelta(seconds=index * 0.5), process="OcarinaOfTime"))
        for index in range(3)
    ]

    class FakeCompleted:
        returncode = 0
        stdout = "\n".join(lines)
        stderr = ""

    monkeypatch.setattr(fps.subprocess, "run", lambda *a, **k: FakeCompleted())
    result = fps.recover_from_log(t0, t0 + timedelta(minutes=1), "ott")
    assert result["status"]["seen"] == {"OcarinaOfTime": 3}
    assert result["status"]["adopted_from"] == "ott"


def test_report_explains_filter_mismatch(tmp_path):
    """1114 现场：收到的 HUD 都被过滤词挡掉了，画布上要把真实进程名说出来。"""
    from mac_thermal_lab.analysis import load_session
    from mac_thermal_lab.report import build_figure
    from mac_thermal_lab.storage import read_json, write_json

    paths = _fps_session(tmp_path)
    paths.fps_samples.write_text("", encoding="utf-8")
    meta = read_json(paths.metadata)
    meta["game"] = {
        "enabled": True,
        "filter": "ott",
        "process": None,
        "error": None,
        "hud_seen_processes": {"OcarinaOfTime": 420},
    }
    write_json(paths.metadata, meta)

    fig = build_figure(load_session(paths))
    text = " ".join(ann.text for ann in fig.layout.annotations if ann.text)
    assert "有进程在写 Metal HUD" in text
    assert "OcarinaOfTime（420 行）" in text
    assert "--game OcarinaOfTime" in text


def test_report_fps_curve_tolerates_samples_without_fps(tmp_path):
    """复现 1113 现场：重复投递导致一半样本算不出帧率，曲线必须照画。"""
    from mac_thermal_lab.analysis import load_session
    from mac_thermal_lab.report import build_figure

    paths = _fps_session(tmp_path)
    interleaved = []
    for line in paths.fps_samples.read_text(encoding="utf-8").splitlines():
        interleaved.append(line)
        blank = json.loads(line)
        blank["fps"] = None
        blank["fps_1pct"] = None
        interleaved.append(json.dumps(blank))
    paths.fps_samples.write_text("\n".join(interleaved) + "\n", encoding="utf-8")

    data = load_session(paths)
    fig = build_figure(data)
    curve = next(trace for trace in fig.data if trace.name == "帧率 (FPS)")
    ys = list(curve.y)
    assert len(ys) == 5
    assert all(value is not None for value in ys)
    assert curve.connectgaps is True
