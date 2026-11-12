"""时间戳对齐与统计：不许按行号合并，不许补齐缺失。"""

from __future__ import annotations

from mac_thermal_lab.analysis import (
    build_merged_rows,
    load_session,
    sampling_integrity,
    summary,
    thermal_durations,
    write_merged_csv,
)


def test_sources_loaded_independently(session):
    data = load_session(session)
    assert len(data.macmon) == 10
    assert len(data.thermal) == 12
    assert len(data.events) == 1


def test_merge_is_by_timestamp_not_row_index(session):
    data = load_session(session)
    rows = build_merged_rows(data)
    # 两个数据源的样本数不同，合并后应是并集而不是较短一方截断
    assert len(rows) == len(data.macmon) + len(data.thermal)
    timestamps = [row["row_ts_utc"] for row in rows]
    assert timestamps == sorted(timestamps)


def test_thermal_asof_never_looks_into_the_future(session):
    data = load_session(session)
    thermal = data.thermal_sortable()
    for row in build_merged_rows(data):
        if row["source"] != "macmon" or not row["thermal_pressure"]:
            continue
        ts = row["row_ts_utc"]
        candidates = [s for s in thermal if s.ts.isoformat() <= ts]
        assert candidates, "as-of 必须存在不晚于当前行的样本"
        assert candidates[-1].level == row["thermal_pressure"]


def test_missing_thermal_reported_as_empty_not_filled(session):
    data = load_session(session)
    rows = [r for r in build_merged_rows(data) if r["source"] == "macmon"]
    # 空档之后 macmon 恢复采样的第一行，距上一条 thermal 样本可能超龄 → 留空
    blanks = [r for r in rows if not r["thermal_pressure"]]
    assert blanks, "空档区的 thermal 应留空而不是沿用旧值或插值"


def test_gap_detected_in_integrity(session):
    data = load_session(session)
    integrity = sampling_integrity(data)
    macmon = integrity["macmon"]
    assert macmon["count"] == 10
    assert macmon["missing"] >= 1
    assert macmon["longest_gap_s"] >= 20
    assert len(macmon["gaps"]) == 1


def test_thermal_durations_cover_all_states(session):
    data = load_session(session)
    durations = thermal_durations(data)
    assert durations["total_s"] > 0
    assert set(durations["by_level"]) >= {"Nominal", "Moderate", "Heavy", "Unknown", "Trapping"}
    assert durations["changes"] >= 4


def test_unknown_thermal_state_not_dropped(session):
    """macOS 新增/未收录的状态必须进入统计，而不是被静默丢弃。"""
    data = load_session(session)
    durations = thermal_durations(data)
    assert "Unknown" in durations["by_level"]
    assert durations["by_raw"]["Serious"] > 0
    assert durations["raw_labels"] == ["Serious"]
    # 各状态时长之和应覆盖整段实验
    assert abs(sum(durations["by_level"].values()) - durations["total_s"]) < 0.01


def test_csv_has_union_rows_and_both_sources(tmp_path, session):
    data = load_session(session)
    path = write_merged_csv(data)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(data.macmon) + len(data.thermal) + 1
    assert lines[0].startswith("row_ts_utc,row_ts_local,elapsed_s,source")
    body = "\n".join(lines[1:])
    assert "powermetrics" in body and "macmon" in body
    assert "thermal_pressure_raw" in lines[0]


def test_summary_numbers_are_consistent(session):
    data = load_session(session)
    smy = summary(data)
    cpu = smy["temperatures"]["cpu"]
    assert cpu["min"] == 40.0
    assert cpu["max"] == 58.0  # 40 + 9*2
    assert cpu["count"] == 10
    assert smy["power"]["cpu_power_w"]["max"] == 5.5
    assert smy["span_seconds"] == 32.0


def test_report_figure_builds_with_mixed_data(session):
    from mac_thermal_lab.report import build_figure

    data = load_session(session)
    fig = build_figure(data)
    names = [t.name for t in fig.data]
    assert "CPU 温度 (°C)" in names
    assert "All Power" in names
    assert "Thermal Pressure" in names
    # 有 thermal 数据时不应出现「无数据」提示
    assert not any(
        "未采集到 Thermal Pressure" in (a.text or "") for a in fig.layout.annotations
    )


def test_report_is_offline_self_contained(session):
    import re

    from mac_thermal_lab.report import generate_report

    path = generate_report(session)
    html = path.read_text(encoding="utf-8")
    # 不允许任何外部脚本/样式引用（plotly.js 必须内嵌）
    assert not re.search(r"<script[^>]+src=", html)
    assert not re.search(r'<link[^>]+href="https?:', html)
    assert "Plotly.newPlot" in html
    assert "风扇对比" in html
