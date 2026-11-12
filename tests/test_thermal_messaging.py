"""Thermal 缺数据时的解释性文案，避免报告只显示一句「无数据」。"""

from __future__ import annotations

import json
from datetime import timedelta

from mac_thermal_lab.collector import _short_thermal_reason
from mac_thermal_lab.report import _thermal_missing_text
from mac_thermal_lab.storage import SessionPaths, write_json

from conftest import START


def _make(tmp_path, thermal: dict, raw_text: str | None):
    d = tmp_path / "s"
    d.mkdir()
    paths = SessionPaths(d)
    write_json(
        paths.metadata,
        {
            "schema": 1,
            "session_id": "s",
            "name": "t",
            "started_at": (START - timedelta(seconds=5)).isoformat(),
            "ended_at": START.isoformat(),
            "interval_ms": 1000,
            "status": "stopped",
            "thermal": thermal,
        },
    )
    if raw_text is not None:
        paths.powermetrics_raw.write_text(raw_text, encoding="utf-8")
    from mac_thermal_lab.analysis import load_session

    return load_session(paths)


def test_missing_text_explains_no_thermal_flag(tmp_path):
    data = _make(tmp_path, {"enabled": False, "sudo_mode": "disabled"}, None)
    text = _thermal_missing_text(data)
    assert "--no-thermal" in text


def test_missing_text_shows_recorded_error(tmp_path):
    data = _make(
        tmp_path,
        {"enabled": True, "sudo_mode": "password", "error": "sudo 密码错误"},
        "",
    )
    text = _thermal_missing_text(data)
    assert "sudo 密码错误" in text
    assert "mtl doctor --thermal" in text


def test_missing_text_distinguishes_empty_output(tmp_path):
    data = _make(tmp_path, {"enabled": True, "sudo_mode": "password", "error": None}, "")
    assert "没有返回任何样本" in _thermal_missing_text(data)


def test_missing_text_unparsed_output(tmp_path):
    data = _make(
        tmp_path,
        {"enabled": True, "sudo_mode": "password", "error": None},
        "*** Sampled system activity (Wed Nov 11 22:50:00 2026) (1002.11ms elapsed) ***\n",
    )
    assert "未解析到有效样本" in _thermal_missing_text(data)


def test_short_reason_maps_sudo_errors():
    assert "密码" in _short_thermal_reason(["sudo: a password is required"], 1)
    assert "拒绝" in _short_thermal_reason(["sudo: Operation not permitted"], 1)
    assert "退出码 9" in _short_thermal_reason([], 9)
