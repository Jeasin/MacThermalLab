"""powermetrics thermal 解析：时间戳、状态归一化、未知状态保留。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from mac_thermal_lab.powermetrics import (
    ThermalParser,
    canonical_level,
    parse_log_text,
)

FIXTURE = Path(__file__).parent / "fixtures" / "powermetrics.log"


def _samples():
    return parse_log_text(FIXTURE.read_text(encoding="utf-8"))


def test_parse_header_timestamps():
    samples = _samples()
    assert len(samples) == 6
    first = samples[0]
    # 头部的 +0800 是本地偏移，内部必须统一成 UTC
    assert first.ts is not None
    assert first.ts.utcoffset() == timezone.utc.utcoffset(None)
    assert first.ts.astimezone(timezone.utc).hour == 11
    assert first.ts.minute == 43


def test_levels_normalized():
    levels = [s.level for s in _samples()]
    assert levels[:5] == ["Nominal", "Nominal", "Moderate", "Heavy", "Serious"]
    assert levels[5] == "Trapping"


def test_unknown_state_is_preserved_not_dropped():
    samples = _samples()
    serious = [s for s in samples if s.level == "Serious"]
    assert len(serious) == 1
    assert serious[0].raw_level == "Serious"
    # 未知状态排到已知状态之后，但仍要画出来
    assert serious[0].rank > samples[3].rank


def test_canonical_level_case_insensitive():
    assert canonical_level("nominal")[0] == "Nominal"
    assert canonical_level("  HEAVY ")[0] == "Heavy"
    assert canonical_level("")[0] == "Unknown"


def test_missing_header_falls_back_to_arrival_time():
    parser = ThermalParser()
    arrival = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)
    sample = parser.feed("Current pressure level: Moderate", arrival)
    assert sample is not None
    assert sample.ts == arrival
    assert sample.ts_source == "arrival"


def test_garbage_lines_ignored():
    parser = ThermalParser()
    assert parser.feed("") is None
    assert parser.feed("random noise") is None
    assert parser.feed("**** Thermal pressure ****") is None


def test_unparsable_header_counted():
    parser = ThermalParser()
    parser.feed("*** Sampled system activity (плохое значение) (2000.00ms elapsed) ***")
    sample = parser.feed("Current pressure level: Heavy", datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert sample is not None
    assert sample.ts_source == "arrival"
    assert parser.stats["unparsed_headers"] == 1


def test_loose_format_supported():
    parser = ThermalParser()
    sample = parser.feed("Thermal pressure: Heavy")
    assert sample is not None
    assert sample.level == "Heavy"


def test_samples_are_ordered_and_spaced():
    samples = [s for s in _samples() if s.ts]
    deltas = [
        (b.ts - a.ts).total_seconds() for a, b in zip(samples, samples[1:])
    ]
    # 第三条与第四条之间刻意留了 4s 空档（模拟漏采），解析器不得补齐
    assert 4.0 in deltas
    assert all(d > 0 for d in deltas)
