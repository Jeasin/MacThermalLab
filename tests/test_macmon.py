"""macmon 解析：单位与字段名必须与真实输出一致。"""

from __future__ import annotations

from datetime import timezone
from pathlib import Path

from mac_thermal_lab import macmon

FIXTURE = Path(__file__).parent / "fixtures" / "macmon_samples.jsonl"


def _samples():
    lines = FIXTURE.read_text(encoding="utf-8").splitlines()
    return [macmon.parse_line(line) for line in lines if line.strip()]


def test_parse_real_macmon_output():
    samples = [s for s in _samples() if s is not None]
    assert len(samples) >= 3
    sample = samples[0]
    assert sample.ts.tzinfo is not None
    assert sample.ts.utcoffset() == timezone.utc.utcoffset(None)  # macmon 输出 UTC
    # 温度：摄氏度原值，不换算
    assert 20 < sample.get("cpu_temp_c") < 120
    assert 20 < sample.get("gpu_temp_c") < 120
    # 功耗：W 原值，绝不除 1000
    assert 0 < sample.get("cpu_power_w") < 200
    assert 0 < sample.get("all_power_w") < 200
    # 频率：MHz
    assert 500 < sample.get("pcpu_freq_mhz") < 6000
    assert 500 < sample.get("ecpu_freq_mhz") < 6000
    # 使用率百分比已乘 100
    assert sample.get("cpu_usage_pct") is not None
    assert 0 <= sample.get("cpu_usage_pct") <= 100


def test_s_core_mapped_from_pcpu():
    """macmon 0.11 用 pcpu_* 表示 S 核，不能因为找不到 scpu_* 就留空。"""
    sample = _samples()[0]
    assert sample is not None
    assert sample.get("pcpu_freq_mhz") is not None
    assert sample.get("pcpu_active_pct") is not None


def test_soc_info_extracted():
    sample = _samples()[0]
    assert sample is not None
    soc = macmon.soc_info(sample)
    assert soc["chip_name"]
    assert soc["ecpu_cores"] and soc["pcpu_cores"]


def test_all_power_is_independent_of_parts():
    """all_power 不应等于 cpu+gpu，代码里也没有把它们相加的逻辑。"""
    sample = _samples()[0]
    assert sample is not None
    total = sample.get("cpu_power_w") + sample.get("gpu_power_w")
    assert sample.get("all_power_w") != total


def test_bad_json_is_ignored():
    assert macmon.parse_line("not json") is None
    assert macmon.parse_line("") is None
    assert macmon.parse_line('{"no_timestamp": 1}') is None


def test_missing_fields_degrade_to_none():
    sample = macmon.parse_sample({"timestamp": "2026-11-11T11:43:35+00:00"})
    assert sample is not None
    assert sample.get("cpu_temp_c") is None
    assert sample.get("cpu_power_w") is None


def test_build_command_uses_interval_and_soc_flag():
    cmd = macmon.build_command(2000, True)
    assert "pipe" in cmd and "-i" in cmd and "2000" in cmd
    assert "--soc-info" in cmd
    assert "-s" in cmd and "0" in cmd


def test_legacy_scpu_keys_supported():
    payload = {
        "timestamp": "2026-11-11T11:43:35+00:00",
        "scpu_freq_mhz": 3200,
        "scpu_active_ratio": 0.5,
    }
    sample = macmon.parse_sample(payload)
    assert sample is not None
    assert sample.get("pcpu_freq_mhz") == 3200
    assert sample.get("pcpu_active_pct") == 50.0
