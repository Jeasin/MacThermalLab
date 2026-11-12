"""macmon 采集与解析。

基于 macmon 0.11.0 的真实输出校准（`macmon pipe -s 0 -i 2000 --soc-info`）：

    {
      "all_power": 14.88, "cpu_power": 14.65, "gpu_power": 0.23,
      "ane_power": 0.0, "ram_power": 0.58, "gpu_ram_power": 0.0,
      "sys_power": 33.12,
      "cpu_active_ratio": 0.32, "cpu_scaled_ratio": 0.30, "cpu_usage_pct": 0.30,
      "ecpu_freq_mhz": 2227, "ecpu_active_ratio": 0.11,
      "pcpu_freq_mhz": 4425, "pcpu_active_ratio": 0.63,   # S 核在 macmon 里叫 pcpu
      "gpu_freq_mhz": 338, "gpu_active_ratio": 0.19,
      "temp": {"cpu_temp_avg": 72.4, "gpu_temp_avg": 70.3},
      "memory": {"pressure_level": 0, "pressure_percent": 35,
                 "ram_total": ..., "ram_usage": ..., "swap_total": ..., "swap_usage": ...},
      "soc": {"chip_name": "Apple M5", "mac_model": "Mac17,3", ...},
      "timestamp": "2026-11-11T11:43:35.377979+00:00"   # UTC
    }

单位：功耗 W（不要除 1000），温度 °C，频率 MHz，内存字节。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .storage import parse_timestamp

# merged.csv / 报告使用的规范列顺序
CANONICAL_FIELDS: tuple[str, ...] = (
    "cpu_temp_c",
    "gpu_temp_c",
    "cpu_power_w",
    "gpu_power_w",
    "all_power_w",
    "sys_power_w",
    "ane_power_w",
    "ram_power_w",
    "gpu_ram_power_w",
    "ecpu_freq_mhz",
    "pcpu_freq_mhz",
    "gpu_freq_mhz",
    "cpu_usage_pct",
    "ecpu_active_pct",
    "pcpu_active_pct",
    "gpu_active_pct",
    "mem_pressure_level",
    "mem_pressure_pct",
    "ram_used_gb",
    "swap_used_gb",
)

# 人读标签
FIELD_LABELS: dict[str, str] = {
    "cpu_temp_c": "CPU 温度 (°C)",
    "gpu_temp_c": "GPU 温度 (°C)",
    "cpu_power_w": "CPU 功耗 (W)",
    "gpu_power_w": "GPU 功耗 (W)",
    "all_power_w": "All Power (W)",
    "sys_power_w": "Sys Power (W)",
    "ane_power_w": "ANE 功耗 (W)",
    "ram_power_w": "RAM 功耗 (W)",
    "cpu_usage_pct": "CPU 使用率 (%)",
    "gpu_usage_pct": "GPU 使用率 (%)",
}


def _dig(data: Any, path: str) -> Any:
    """按 "temp.cpu_temp_avg" 取值。"""
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _pct(ratio: Any) -> float | None:
    val = _num(ratio)
    return None if val is None else val * 100.0


def _gb(value: Any) -> float | None:
    val = _num(value)
    return None if val is None else val / (1024**3)


@dataclass(slots=True)
class MacmonSample:
    ts: datetime
    values: dict[str, float | None] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str) -> float | None:
        return self.values.get(key)

    @property
    def soc(self) -> dict[str, Any]:
        soc = self.raw.get("soc")
        return soc if isinstance(soc, dict) else {}


def parse_sample(payload: dict[str, Any]) -> MacmonSample | None:
    """把一行 macmon JSON 转成规范样本。时间戳不可用的行直接丢弃（调用方需计数）。"""
    ts = parse_timestamp(_dig(payload, "timestamp"))
    if ts is None:
        return None

    # S 核：macmon 用 pcpu_*，旧版本/文档里叫 scpu_*；优先按 cpu_tiers 标签取
    ecpu_freq = _num(_dig(payload, "ecpu_freq_mhz"))
    pcpu_freq = _num(_dig(payload, "pcpu_freq_mhz")) or _num(_dig(payload, "scpu_freq_mhz"))
    ecpu_active = _num(_dig(payload, "ecpu_active_ratio"))
    pcpu_active = _num(_dig(payload, "pcpu_active_ratio")) or _num(
        _dig(payload, "scpu_active_ratio")
    )

    tiers = payload.get("cpu_tiers")
    if isinstance(tiers, list):
        for tier in tiers:
            if not isinstance(tier, dict):
                continue
            label = str(tier.get("label") or "").upper()
            if label == "E":
                ecpu_freq = ecpu_freq if ecpu_freq is not None else _num(tier.get("freq_mhz"))
                ecpu_active = (
                    ecpu_active if ecpu_active is not None else _num(tier.get("active_ratio"))
                )
            elif label in {"S", "P"}:
                pcpu_freq = pcpu_freq if pcpu_freq is not None else _num(tier.get("freq_mhz"))
                pcpu_active = (
                    pcpu_active if pcpu_active is not None else _num(tier.get("active_ratio"))
                )

    # cpu_usage_pct 在 0.11.0 里是 0~1 的比例（没有乘 100），统一从 ratio 计算
    cpu_usage = _pct(_dig(payload, "cpu_scaled_ratio"))
    if cpu_usage is None:
        cpu_usage = _pct(_dig(payload, "cpu_usage_pct"))

    values: dict[str, float | None] = {
        "cpu_temp_c": _num(_dig(payload, "temp.cpu_temp_avg")),
        "gpu_temp_c": _num(_dig(payload, "temp.gpu_temp_avg")),
        "cpu_power_w": _num(_dig(payload, "cpu_power")),
        "gpu_power_w": _num(_dig(payload, "gpu_power")),
        "all_power_w": _num(_dig(payload, "all_power")),
        "sys_power_w": _num(_dig(payload, "sys_power")),
        "ane_power_w": _num(_dig(payload, "ane_power")),
        "ram_power_w": _num(_dig(payload, "ram_power")),
        "gpu_ram_power_w": _num(_dig(payload, "gpu_ram_power")),
        "ecpu_freq_mhz": ecpu_freq,
        "pcpu_freq_mhz": pcpu_freq,
        "gpu_freq_mhz": _num(_dig(payload, "gpu_freq_mhz")),
        "cpu_usage_pct": cpu_usage,
        "ecpu_active_pct": _pct(ecpu_active),
        "pcpu_active_pct": _pct(pcpu_active),
        "gpu_active_pct": _pct(_dig(payload, "gpu_active_ratio")),
        "mem_pressure_level": _num(_dig(payload, "memory.pressure_level")),
        "mem_pressure_pct": _num(_dig(payload, "memory.pressure_percent")),
        "ram_used_gb": _gb(_dig(payload, "memory.ram_usage")),
        "swap_used_gb": _gb(_dig(payload, "memory.swap_usage")),
    }
    values["gpu_usage_pct"] = values["gpu_active_pct"]
    return MacmonSample(ts=ts, values=values, raw=payload)


def parse_line(line: str) -> MacmonSample | None:
    line = line.strip()
    if not line or not line.startswith("{"):
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return parse_sample(payload)


def soc_info(sample: MacmonSample) -> dict[str, Any]:
    """取一次 SoC 元数据，用于 metadata.json（不必每行重复保存）。"""
    soc = sample.soc
    if not soc:
        return {}
    return {
        "chip_name": soc.get("chip_name"),
        "mac_model": soc.get("mac_model"),
        "memory_gb": soc.get("memory_gb"),
        "ecpu_cores": soc.get("ecpu_cores"),
        "pcpu_cores": soc.get("pcpu_cores"),
        "gpu_cores": soc.get("gpu_cores"),
        "ecpu_label": soc.get("ecpu_label"),
        "pcpu_label": soc.get("pcpu_label"),
        "is_vm": soc.get("vm"),
    }


# --------------------------------------------------------------------------- #
# 可执行文件
# --------------------------------------------------------------------------- #
def find_macmon() -> str | None:
    return shutil.which("macmon")


def macmon_version() -> str | None:
    exe = find_macmon()
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


def build_command(interval_ms: int, with_soc_info: bool = True) -> list[str]:
    exe = find_macmon() or "macmon"
    cmd = [exe, "pipe", "-s", "0", "-i", str(interval_ms)]
    if with_soc_info:
        cmd.append("--soc-info")
    return cmd
