"""会话读取、时间戳对齐合并与统计。

对齐原则：
- 两个数据源各自保留真实采样时刻，按时间戳对齐，绝不按行号合并。
- 缺失就是缺失：不插值、不伪造。
- 需要「某时刻的 thermal pressure」时使用 as-of（取不晚于该时刻的最近一次真实样本），
  并把 as-of 的「数据年龄」一并写出，方便判断可信度。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from . import fps as fps_mod
from . import macmon as macmon_mod
from .events import Event, load_events
from .macmon import CANONICAL_FIELDS, MacmonSample
from .powermetrics import UNKNOWN_RANK, ThermalSample, parse_log_file
from .storage import (
    EVENTS,
    SessionPaths,
    iter_jsonl,
    parse_timestamp,
    read_json,
    write_csv,
)

THERMAL_SAMPLES_FILE = "powermetrics.samples.jsonl"

MERGED_HEADER: tuple[str, ...] = (
    "row_ts_utc",
    "row_ts_local",
    "elapsed_s",
    "source",
    *CANONICAL_FIELDS,
    "thermal_pressure",
    "thermal_pressure_raw",
    "thermal_pressure_age_s",
    "fps",
    "fps_1pct",
    "fps_age_s",
    "game_process",
)


@dataclass(slots=True)
class SessionData:
    paths: SessionPaths
    metadata: dict[str, Any] = field(default_factory=dict)
    macmon: list[MacmonSample] = field(default_factory=list)
    thermal: list[ThermalSample] = field(default_factory=list)
    fps: list[dict[str, Any]] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # ---- 基本属性 ----
    @property
    def interval_ms(self) -> int:
        return int(self.metadata.get("interval_ms") or 2000)

    @property
    def interval_s(self) -> float:
        return self.interval_ms / 1000.0

    @property
    def started_at(self) -> datetime | None:
        return parse_timestamp(self.metadata.get("started_at"))

    @property
    def ended_at(self) -> datetime | None:
        ts = parse_timestamp(self.metadata.get("ended_at"))
        if ts:
            return ts
        if self.macmon:
            return self.macmon[-1].ts
        if self.thermal:
            last = [s.ts for s in self.thermal if s.ts]
            if last:
                return max(last)
        return None

    @property
    def span_seconds(self) -> float | None:
        start, end = self.started_at, self.ended_at
        if not start or not end:
            return None
        return max(0.0, (end - start).total_seconds())

    @property
    def name(self) -> str:
        return str(self.metadata.get("name") or self.paths.name)

    def thermal_sortable(self) -> list[ThermalSample]:
        return sorted((s for s in self.thermal if s.ts is not None), key=lambda s: s.ts)


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def _load_macmon(paths: SessionPaths, warnings: list[str]) -> list[MacmonSample]:
    samples: list[MacmonSample] = []
    bad = 0
    for record in iter_jsonl(paths.macmon_raw):
        sample = macmon_mod.parse_sample(record)
        if sample is None:
            bad += 1
            continue
        samples.append(sample)
    samples.sort(key=lambda s: s.ts)
    if bad:
        warnings.append(f"macmon.jsonl 中有 {bad} 行无法解析时间戳，已跳过（原始文件保留）")
    return samples


def _load_thermal(paths: SessionPaths, warnings: list[str]) -> list[ThermalSample]:
    """原始日志是唯一事实来源；采集时的解析缓存只用于补「无时间戳」样本的到达时刻。"""
    sidecar_records = [
        ThermalSample.from_record(rec) for rec in iter_jsonl(paths.path(THERMAL_SAMPLES_FILE))
    ]
    sidecar = [s for s in sidecar_records if s is not None]

    samples = list(parse_log_file(paths.powermetrics_raw)) if paths.powermetrics_raw.exists() else []
    if samples:
        missing = 0
        for index, sample in enumerate(samples):
            if sample.ts is None:
                if index < len(sidecar) and sidecar[index].ts is not None:
                    sample.ts = sidecar[index].ts
                    sample.ts_source = "arrival"
                else:
                    missing += 1
        if missing:
            warnings.append(f"有 {missing} 条 thermal 样本取不到时间戳，未进入时间轴（原始记录保留）")
    elif sidecar:
        warnings.append("原始 powermetrics 日志缺失，已回退到采集缓存")
        samples = sidecar

    located = [s for s in samples if s.ts is not None]
    if not samples:
        return []
    return located if len(located) != len(samples) else samples


def _load_fps(paths: SessionPaths, warnings: list[str]) -> list[dict[str, Any]]:
    """帧率样本来自采集时解析的 hud.log，每条带真实到达时刻。"""
    samples: list[dict[str, Any]] = []
    bad = 0
    for record in iter_jsonl(paths.fps_samples):
        ts = parse_timestamp(record.get("timestamp"))
        if ts is None:
            bad += 1
            continue
        samples.append({**record, "ts": ts})
    samples.sort(key=lambda s: s["ts"])
    if bad:
        warnings.append(f"fps.jsonl 中有 {bad} 条样本时间戳不可用，已跳过（原始日志保留）")
    return samples


def load_session(paths: SessionPaths) -> SessionData:
    metadata = read_json(paths.metadata) or {}
    warnings: list[str] = []
    data = SessionData(paths=paths, metadata=metadata, warnings=warnings)
    data.macmon = _load_macmon(paths, warnings)
    data.thermal = _load_thermal(paths, warnings)
    data.fps = _load_fps(paths, warnings)
    data.events = load_events(paths.path(EVENTS), data.started_at)
    return data


# --------------------------------------------------------------------------- #
# 合并
# --------------------------------------------------------------------------- #
def _asof_thermal(
    samples: list[ThermalSample], ts: datetime, max_age_s: float
) -> tuple[ThermalSample | None, float | None]:
    """取不晚于 ts 的最近一条 thermal 样本（不插值）。"""
    lo, hi = 0, len(samples) - 1
    found = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if samples[mid].ts <= ts:
            found = samples[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    if found is None:
        return None, None
    age = (ts - found.ts).total_seconds()
    if age > max_age_s:
        return None, age
    return found, age


def _asof_fps(
    samples: list[dict[str, Any]], ts: datetime, max_age_s: float
) -> tuple[dict[str, Any] | None, float | None]:
    """取不晚于 ts 的最近一条帧率样本（不插值）。"""
    lo, hi = 0, len(samples) - 1
    found = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if samples[mid]["ts"] <= ts:
            found = samples[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    if found is None:
        return None, None
    age = (ts - found["ts"]).total_seconds()
    if age > max_age_s:
        return None, age
    return found, age


def build_merged_rows(data: SessionData) -> list[dict[str, Any]]:
    """并集时间轴：macmon / thermal / fps 各自按真实时间戳占一行，再按时间排序。

    非本源的列一律留空，只有「某个采样时刻离该值足够近」时才填 as-of 值，
    并写出 age。缺失就是缺失，不插值。
    """
    start = data.started_at
    thermal = data.thermal_sortable()
    fps_samples = data.fps
    max_age = max(4 * data.interval_s, 10.0)
    fps_max_age = max(2 * data.interval_s, 4.0)
    rows: list[dict[str, Any]] = []

    def elapsed(ts: datetime) -> float | None:
        return None if start is None else round((ts - start).total_seconds(), 3)

    def fps_columns(ts: datetime) -> dict[str, Any]:
        match, age = _asof_fps(fps_samples, ts, fps_max_age)
        if match is None:
            return {"fps": "", "fps_1pct": "", "fps_age_s": "" if age is None else round(age, 3),
                    "game_process": ""}
        return {
            "fps": "" if match.get("fps") is None else round(float(match["fps"]), 2),
            "fps_1pct": "" if match.get("fps_1pct") is None else round(float(match["fps_1pct"]), 2),
            "fps_age_s": "" if age is None else round(age, 3),
            "game_process": match.get("process") or "",
        }

    for sample in data.macmon:
        matched, age = _asof_thermal(thermal, sample.ts, max_age)
        row: dict[str, Any] = {
            "row_ts_utc": sample.ts.isoformat(timespec="milliseconds"),
            "row_ts_local": sample.ts.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_s": elapsed(sample.ts),
            "source": "macmon",
            "thermal_pressure": matched.level if matched else "",
            "thermal_pressure_raw": "",
            "thermal_pressure_age_s": "" if age is None else round(age, 3),
            **fps_columns(sample.ts),
        }
        for name in CANONICAL_FIELDS:
            value = sample.get(name)
            row[name] = "" if value is None else round(value, 4)
        rows.append(row)

    for sample in thermal:
        rows.append(
            {
                "row_ts_utc": sample.ts.isoformat(timespec="milliseconds"),
                "row_ts_local": sample.ts.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                "elapsed_s": elapsed(sample.ts),
                "source": "powermetrics",
                "thermal_pressure": sample.level,
                "thermal_pressure_raw": sample.raw_level,
                "thermal_pressure_age_s": 0.0,
                **{name: "" for name in CANONICAL_FIELDS},
                **fps_columns(sample.ts),
            }
        )

    for sample in fps_samples:
        rows.append(
            {
                "row_ts_utc": sample["ts"].isoformat(timespec="milliseconds"),
                "row_ts_local": sample["ts"].astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                "elapsed_s": elapsed(sample["ts"]),
                "source": "metal-hud",
                **{name: "" for name in CANONICAL_FIELDS},
                "thermal_pressure": "",
                "thermal_pressure_raw": "",
                "thermal_pressure_age_s": "",
                "fps": "" if sample.get("fps") is None else round(float(sample["fps"]), 2),
                "fps_1pct": "" if sample.get("fps_1pct") is None else round(float(sample["fps_1pct"]), 2),
                "fps_age_s": 0.0,
                "game_process": sample.get("process") or "",
            }
        )

    rows.sort(key=lambda r: r["row_ts_utc"])
    return rows


def write_merged_csv(data: SessionData) -> Path:
    rows = build_merged_rows(data)
    write_csv(
        data.paths.merged_csv,
        MERGED_HEADER,
        ([row.get(col, "") for col in MERGED_HEADER] for row in rows),
    )
    return data.paths.merged_csv


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
def describe(values: Iterable[float | None]) -> dict[str, float] | None:
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return None
    clean.sort()
    n = len(clean)
    return {
        "count": n,
        "min": clean[0],
        "mean": sum(clean) / n,
        "max": clean[-1],
        "p95": clean[min(n - 1, int(round(0.95 * (n - 1))))],
        "sum": sum(clean),
    }


def energy_wh(mean_w: float | None, seconds: float | None) -> float | None:
    if mean_w is None or not seconds:
        return None
    return mean_w * seconds / 3600.0


def temperature_stats(data: SessionData) -> dict[str, dict[str, float] | None]:
    return {
        "cpu": describe(s.get("cpu_temp_c") for s in data.macmon),
        "gpu": describe(s.get("gpu_temp_c") for s in data.macmon),
    }


POWER_METRICS: tuple[str, ...] = (
    "cpu_power_w",
    "gpu_power_w",
    "all_power_w",
    "sys_power_w",
    "ane_power_w",
    "ram_power_w",
)


def power_stats(data: SessionData) -> dict[str, dict[str, float] | None]:
    span = data.span_seconds
    out: dict[str, dict[str, float] | None] = {}
    for key in POWER_METRICS:
        stats = describe(s.get(key) for s in data.macmon)
        if stats:
            stats["energy_wh"] = energy_wh(stats["mean"], span) or 0.0
        out[key] = stats
    return out


def thermal_durations(data: SessionData) -> dict[str, Any]:
    """每个状态持续多久（按相邻真实样本的间隔累积）。

    未知状态（macOS 新增或未收录的措辞）统一归入 Unknown 展示，
    但原始文本保留在 by_raw 里，绝不静默丢弃。
    """
    samples = data.thermal_sortable()
    if not samples:
        return {
            "total_s": 0.0,
            "by_level": {},
            "by_raw": {},
            "raw_labels": [],
            "changes": 0,
            "samples": 0,
        }

    end = data.ended_at or samples[-1].ts
    by_level: dict[str, float] = {}
    by_raw: dict[str, float] = {}
    changes = 0
    prev_level = None
    for index, sample in enumerate(samples):
        seg_end = samples[index + 1].ts if index + 1 < len(samples) else max(end, sample.ts)
        dur = max(0.0, (seg_end - sample.ts).total_seconds())
        key = "Unknown" if sample.rank >= UNKNOWN_RANK else sample.level
        by_level[key] = by_level.get(key, 0.0) + dur
        if key == "Unknown":
            by_raw[sample.raw_level] = by_raw.get(sample.raw_level, 0.0) + dur
        if prev_level is not None and key != prev_level:
            changes += 1
        prev_level = key

    total = sum(by_level.values())
    return {
        "total_s": total,
        "by_level": by_level,
        "by_raw": by_raw,
        "raw_labels": sorted(by_raw),
        "changes": changes,
        "samples": len(samples),
    }


def sampling_integrity(data: SessionData) -> dict[str, Any]:
    def analyze(timestamps: list[datetime], interval_s: float, label: str) -> dict[str, Any]:
        if not timestamps:
            return {"label": label, "count": 0, "missing": 0, "missing_pct": None, "gaps": []}
        timestamps = sorted(timestamps)
        gaps: list[dict[str, Any]] = []
        for prev, cur in zip(timestamps, timestamps[1:]):
            delta = (cur - prev).total_seconds()
            if delta > interval_s * 1.6:
                gaps.append(
                    {
                        "start": prev.isoformat(),
                        "end": cur.isoformat(),
                        "seconds": round(delta, 3),
                        "missed": max(0, int(round(delta / interval_s)) - 1),
                    }
                )
        span = (timestamps[-1] - timestamps[0]).total_seconds()
        expected = int(round(span / interval_s)) + 1 if interval_s > 0 else len(timestamps)
        missing = max(0, expected - len(timestamps))
        return {
            "label": label,
            "count": len(timestamps),
            "expected": expected,
            "missing": missing,
            "missing_pct": round(100.0 * missing / expected, 2) if expected else None,
            "gaps": gaps,
            "longest_gap_s": max((g["seconds"] for g in gaps), default=0.0),
        }

    return {
        "macmon": analyze([s.ts for s in data.macmon], data.interval_s, "macmon"),
        "thermal": analyze(
            [s.ts for s in data.thermal_sortable()], data.interval_s, "powermetrics"
        ),
    }


def summary(data: SessionData) -> dict[str, Any]:
    return {
        "name": data.name,
        "session": data.paths.name,
        "started_at": data.started_at,
        "ended_at": data.ended_at,
        "span_seconds": data.span_seconds,
        "interval_ms": data.interval_ms,
        "temperatures": temperature_stats(data),
        "power": power_stats(data),
        "thermal": thermal_durations(data),
        "fps": fps_mod.summarize(data.fps),
        "integrity": sampling_integrity(data),
        "events": data.events,
        "warnings": data.warnings,
        "raw_counts": {
            "macmon_lines": len(data.macmon),
            "thermal_samples": len(data.thermal),
            "fps_samples": len(data.fps),
        },
    }
