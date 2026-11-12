"""powermetrics（thermal sampler）解析。

powermetrics 必须以 root 运行。真实输出形如：

    *** Sampled system activity (Wed Nov 11 19:43:21 2026 +0800) (2000.00ms elapsed) ***

    **** Thermal pressure ****
    Current pressure level: Nominal

注意：
- 时间戳是本机本地时间（带 +0800 这类偏移），必须按偏移解析后再转 UTC。
- 不同 macOS 版本字段措辞可能微调，解析器保持宽容：认不出状态也不丢样本。
- 与 macmon 完全独立，不假设采样行数/时刻对齐。
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator

from .storage import as_utc, parse_timestamp

# 官方已知状态；其它一律保留原始文本并归入 Unknown
KNOWN_LEVELS: tuple[str, ...] = ("Nominal", "Moderate", "Heavy", "Trapping")
LEVEL_RANK: dict[str, int] = {
    "Nominal": 0,
    "Moderate": 1,
    "Heavy": 2,
    "Trapping": 3,
}
UNKNOWN_RANK = 4

HEADER_RE = re.compile(
    r"\*{2,}\s*Sampled system activity\s*\((?P<stamp>[^)]*)\)\s*"
    r"(?:\((?P<elapsed>[\d.]+)\s*ms elapsed\))?",
    re.IGNORECASE,
)
LEVEL_RE = re.compile(r"Current\s+pressure\s+level\s*:\s*(?P<level>.+?)\s*$", re.IGNORECASE)
# 兜底：某些版本可能写成 "Thermal pressure: Nominal" 之类
LOOSE_LEVEL_RE = re.compile(
    r"thermal\s*pressure[^:\n]*:\s*(?P<level>Nominal|Moderate|Heavy|Trapping|Unknown)\b",
    re.IGNORECASE,
)


def canonical_level(raw: str | None) -> tuple[str, str]:
    """返回 (规范显示名, rank)。认不出的状态保留原文，归入 Unknown 展示。"""
    text = (raw or "").strip()
    if not text:
        return "Unknown", UNKNOWN_RANK
    for known in KNOWN_LEVELS:
        if text.lower() == known.lower():
            return known, LEVEL_RANK[known]
    low = text.lower()
    if low in {"unknown", "n/a", "na", "-"}:
        return "Unknown", UNKNOWN_RANK
    return text, UNKNOWN_RANK


@dataclass(slots=True)
class ThermalSample:
    ts: datetime | None
    level: str
    raw_level: str
    rank: int
    ts_source: str = "header"
    line: str = ""

    def to_record(self) -> dict[str, Any]:
        return {
            "ts": self.ts.isoformat() if self.ts else None,
            "level": self.level,
            "raw_level": self.raw_level,
            "rank": self.rank,
            "ts_source": self.ts_source,
            "line": self.line,
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "ThermalSample | None":
        ts = parse_timestamp(record.get("ts"))
        raw_level = str(record.get("raw_level") or record.get("level") or "")
        level, rank = canonical_level(raw_level)
        return cls(
            ts=ts,
            level=level,
            raw_level=raw_level,
            rank=rank,
            ts_source=str(record.get("ts_source") or "header"),
            line=str(record.get("line") or ""),
        )


class ThermalParser:
    """流式解析器：采集时逐行喂，报告时也可整段喂。

    时间戳优先取 `*** Sampled system activity (...)` 头部；
    头部缺失时用该行到达采集器的时间兜底，并在 ts_source 中如实标注。
    """

    def __init__(self) -> None:
        self._pending_ts: datetime | None = None
        self._unknown_headers = 0
        self._header_seen = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "headers": self._header_seen,
            "unparsed_headers": self._unknown_headers,
        }

    def feed(self, line: str, arrival: datetime | None = None) -> ThermalSample | None:
        header = HEADER_RE.search(line)
        if header:
            self._header_seen += 1
            parsed = parse_timestamp(header.group("stamp"))
            if parsed is None:
                self._unknown_headers += 1
            self._pending_ts = parsed
            # 头部行本身不含状态
            if not LEVEL_RE.search(line) and not LOOSE_LEVEL_RE.search(line):
                return None

        match = LEVEL_RE.search(line) or LOOSE_LEVEL_RE.search(line)
        if not match:
            return None

        raw_level = match.group("level").strip()
        level, rank = canonical_level(raw_level)
        ts = self._pending_ts
        source = "header"
        if ts is None:
            ts = as_utc(arrival) if arrival else None
            source = "arrival" if ts else "unknown"
        return ThermalSample(
            ts=ts, level=level, raw_level=raw_level, rank=rank, ts_source=source, line=line.rstrip()
        )


def parse_log_text(text: str) -> list[ThermalSample]:
    """整段解析（用于 `mtl report` 回填 / 测试）。"""
    parser = ThermalParser()
    out: list[ThermalSample] = []
    for line in text.splitlines():
        sample = parser.feed(line)
        if sample is not None:
            out.append(sample)
    return out


def parse_log_file(path) -> Iterator[ThermalSample]:
    if not path.exists():
        return iter(())
    parser = ThermalParser()

    def gen():
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                sample = parser.feed(line)
                if sample is not None:
                    yield sample

    return gen()


# --------------------------------------------------------------------------- #
# sudo 与命令行
# --------------------------------------------------------------------------- #
def sudo_nopasswd() -> bool:
    try:
        res = subprocess.run(
            ["sudo", "-n", "true"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def prime_sudo() -> bool:
    """在前台终端请求一次 sudo 授权（可能弹密码），不保存密码。"""
    try:
        res = subprocess.run(["sudo", "-v"], timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def build_command(interval_ms: int, use_password_stdin: bool) -> list[str]:
    cmd = ["sudo"]
    cmd += ["-S", "-p", ""] if use_password_stdin else ["-n"]
    cmd += ["powermetrics", "--samplers", "thermal", "-i", str(interval_ms)]
    return cmd


def thermal_selftest(interval_ms: int = 1000, timeout: float = 20.0) -> dict[str, Any]:
    """`mtl doctor --thermal` 用：跑一次真实 powermetrics 并回显解析结果。"""
    if not sys.platform == "darwin":
        return {"ok": False, "error": "仅支持 macOS"}
    if not sudo_nopasswd() and not prime_sudo():
        return {"ok": False, "error": "sudo 授权失败"}
    cmd = ["sudo", "-n", "powermetrics", "--samplers", "thermal", "-i", str(interval_ms), "-n", "2"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "error": f"超时：{exc}"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc)}

    samples = parse_log_text(res.stdout)
    return {
        "ok": bool(samples),
        "returncode": res.returncode,
        "raw_stdout": res.stdout,
        "stderr": res.stderr.strip(),
        "samples": [s.to_record() for s in samples],
    }
