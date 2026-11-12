"""构造一个可复用的合成会话：包含真实的 macmon 样本结构 + 真实的 powermetrics 文本格式。

两个数据源刻意错开时间、并各自留出空档，用来验证「按时间戳对齐」与「缺口不补齐」。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mac_thermal_lab import paths as paths_mod
from mac_thermal_lab.storage import SessionPaths, iso_utc, write_json

FIXTURES = Path(__file__).parent / "fixtures"
START = datetime(2026, 11, 11, 11, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolate_xdg(tmp_path_factory, monkeypatch):
    """把 XDG 目录锁在临时沙箱里，测试绝不碰用户真实的 ~/.local 与 ~/.config。"""
    base = tmp_path_factory.mktemp("xdg")
    for name, sub in (
        ("MTL_CONFIG_HOME", "config"),
        ("MTL_DATA_HOME", "data"),
        ("MTL_STATE_HOME", "state"),
        ("MTL_CACHE_HOME", "cache"),
        ("MTL_RUNTIME_DIR", "runtime"),
    ):
        monkeypatch.setenv(name, str(base / sub))
    for name in ("MTL_DATA_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
                 "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        monkeypatch.delenv(name, raising=False)
    paths_mod.reset_cache()
    yield
    paths_mod.reset_cache()


def _base_sample() -> dict:
    line = (FIXTURES / "macmon_samples.jsonl").read_text(encoding="utf-8").splitlines()[0]
    return json.loads(line)


def build_macmon_lines(count: int = 10, interval_s: int = 2, gap_at: int = 5) -> list[str]:
    """在第 gap_at 个样本之后插入 20s 空档，并让温度/功耗随样本变化。"""
    base = _base_sample()
    lines = []
    t = START
    for i in range(count):
        sample = json.loads(json.dumps(base))
        sample["timestamp"] = t.isoformat()
        sample["temp"] = {
            "cpu_temp_avg": 40.0 + i * 2,
            "gpu_temp_avg": 38.0 + i * 2,
        }
        sample["cpu_power"] = 1.0 + i * 0.5
        sample["all_power"] = 2.0 + i * 0.6
        lines.append(json.dumps(sample, ensure_ascii=False))
        t += timedelta(seconds=interval_s)
        if i + 1 == gap_at:
            t += timedelta(seconds=20)
    return lines


def build_powermetrics_log(count: int = 12, interval_s: int = 2) -> str:
    stamps = ["Nominal"] * 4 + ["Moderate"] * 3 + ["Heavy"] * 2 + ["Serious"] + ["Trapping"] * 2
    parts = []
    # 从 START + 1s 开始，刻意与 macmon 错开 1s
    t = START + timedelta(seconds=1)
    for i in range(count):
        level = stamps[i % len(stamps)]
        local = t.astimezone(timezone(timedelta(hours=8)))
        header = local.strftime("%a %b %d %H:%M:%S %Y %z")
        parts.append(
            f"*** Sampled system activity ({header}) ({interval_s * 1000:.2f}ms elapsed) ***\n"
            f"\n**** Thermal pressure ****\nCurrent pressure level: {level}\n"
        )
        t += timedelta(seconds=interval_s)
    return "\n".join(parts)


@pytest.fixture
def session(tmp_path: Path) -> SessionPaths:
    root = tmp_path / "2026-11-11_190000_test"
    root.mkdir()
    paths = SessionPaths(root)

    (root / "macmon.jsonl").write_text(
        "\n".join(build_macmon_lines()) + "\n", encoding="utf-8"
    )
    (root / "powermetrics.log").write_text(build_powermetrics_log(), encoding="utf-8")
    (root / "events.jsonl").write_text(
        json.dumps(
            {
                "timestamp": iso_utc(START + timedelta(seconds=12)),
                "elapsed_seconds": 12.0,
                "event": "桌面风扇开启",
                "note": "",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    write_json(
        paths.metadata,
        {
            "schema": 1,
            "session_id": root.name,
            "name": "风扇对比",
            "started_at": iso_utc(START),
            "ended_at": iso_utc(START + timedelta(seconds=32)),
            "interval_ms": 2000,
            "status": "stopped",
            "host": {"os_version": "26.5.2"},
            "soc": {"chip_name": "Apple M5", "mac_model": "Mac17,3", "memory_gb": 16},
            "tools": {"macmon": "macmon 0.11.0"},
            "thermal": {"enabled": True, "sudo_mode": "nopassword", "error": None},
        },
    )
    return paths
