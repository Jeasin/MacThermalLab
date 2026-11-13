"""游戏帧率采集：解析 macOS Metal Performance HUD 写到统一日志里的数据。

Apple 的 Metal HUD 在有 `MTL_HUD_ENABLED=1` 时显示浮层；再加上
`MTL_HUD_LOG_ENABLED=1` 后，它每秒往统一日志（Console.app 那个）写一行：

    metal-HUD: <首帧号>,<估计丢帧>,<进程内存 MB>,<帧间隔 ms>,<GPU 时间 ms>,...

前三个是标量，后面是每帧一对「呈现间隔, GPU 时间」，一行的帧对数不固定。
因此这里同时算两种口径，互为交叉校验：

- ``fps``       由帧间隔均值换算（1000 / 平均间隔），精度取决于 HUD 的毫秒值
- ``frames``    这一秒实际出现的帧对数，对格式细节不敏感
- ``fps_1pct``  按最差 1% 的帧间隔换算，即常说的 1% low

数据来源是 `log stream`，不是截屏 OCR：HUD 本来就把这些数字送去统一日志，
所以既不需要屏幕录制权限，也不用识别浮层文字。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from datetime import datetime

from .storage import iso_utc

HUD_LOG_KEY = "metal-HUD"

# HUD 需要的环境变量。Apple 文档写 MTL_HUD_LOG_ENABLED，早期文档/讲座里
# 出现过 MTL_HUD_LOGGING_ENABLED，两个都设上没有坏处。
HUD_ENV_KEYS = ("MTL_HUD_ENABLED", "MTL_HUD_LOG_ENABLED", "MTL_HUD_LOGGING_ENABLED")

LOG_BIN = "/usr/bin/log"

_NUM_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


# --------------------------------------------------------------------------- #
# 环境变量（launchctl）
# --------------------------------------------------------------------------- #
def read_launchd_env(key: str) -> str | None:
    """读 launchd 用户域里的环境变量；未设置返回 None。"""
    try:
        out = subprocess.run(
            ["launchctl", "getenv", key],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = (out.stdout or "").strip()
    return value or None


def write_launchd_env(key: str, value: str | None) -> bool:
    """设置/清除 launchd 用户域环境变量。失败返回 False（权限或沙箱限制）。"""
    cmd = ["launchctl", "setenv", key, value] if value is not None else ["launchctl", "unsetenv", key]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def enable_hud_env() -> tuple[dict[str, str | None], list[str], list[str]]:
    """打开 HUD 与日志，返回（原值快照, 设置成功的键, 设置失败的键）。

    快照用于停止时精确还原——不假设用户之前没设过这些变量。
    只要有一个键失败（例如没有图形登录会话），调用方应视为「帧率不可用」。
    """
    snapshot = {key: read_launchd_env(key) for key in HUD_ENV_KEYS}
    applied: list[str] = []
    failed: list[str] = []
    for key in HUD_ENV_KEYS:
        (applied if write_launchd_env(key, "1") else failed).append(key)
    return snapshot, applied, failed


def restore_hud_env(
    snapshot: dict[str, str | None] | None, only: list[str] | None = None
) -> list[str]:
    """按快照还原。``only`` 限定只还原当初真的设成功的键。返回还原失败的键。"""
    if not snapshot:
        return []
    failed: list[str] = []
    for key in HUD_ENV_KEYS:
        if key not in snapshot:
            continue
        if only is not None and key not in only:
            continue
        if not write_launchd_env(key, snapshot[key]):
            failed.append(key)
    return failed


def hud_env_snapshot() -> dict[str, str | None]:
    return {key: read_launchd_env(key) for key in HUD_ENV_KEYS}


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def build_log_stream_command(process_name: str | None = None) -> list[str]:
    """构造 log stream 命令。

    用 ndjson 而不是默认的文本格式：字段能直接取，不必猜分隔符。
    谓词只筛 HUD 行；进程过滤在解析阶段做（日志里的进程字段比谓词可靠）。
    """
    predicate = f'eventMessage CONTAINS "{HUD_LOG_KEY}"'
    if process_name:
        # 进程名放进谓词可以让内核层就过滤掉其它应用的 HUD
        escaped = process_name.replace('"', '\\"')
        predicate += f' AND processImagePath CONTAINS "{escaped}"'
    return [LOG_BIN, "stream", "--style", "ndjson", "--predicate", predicate]


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def parse_log_timestamp(text: str | None) -> datetime | None:
    """解析 log stream ndjson 里的时间戳，例如 2026-11-13 14:00:00.123456+0800。"""
    if not text:
        return None
    raw = str(text).strip().replace(" ", "T", 1)
    # Python 的 fromisoformat 只认 +08:00，日志里是 +0800
    fixed = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", raw)
    for candidate in (fixed, raw):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue
    return None


def _to_float(token: str) -> float | None:
    text = token.strip()
    if not text:
        return None
    if text.endswith("ms"):
        text = text[:-2]
    try:
        return float(text)
    except ValueError:
        return None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def parse_hud_message(message: str | None) -> dict | None:
    """解析一行 `metal-HUD: ...`。不是 HUD 行或字段不够时返回 None。"""
    if not message:
        return None
    index = message.find(HUD_LOG_KEY)
    if index < 0:
        return None
    body = message[index + len(HUD_LOG_KEY):].lstrip()
    if body.startswith(":"):
        body = body[1:]

    tokens = [token.strip() for token in body.split(",")]
    while tokens and not tokens[-1]:
        tokens.pop()  # 行尾常有空字段
    # 文档里的三个标量（首帧号、丢帧、内存）必须齐；帧明细缺失时
    # 仍可以靠帧号差分算帧率，所以不要求后半段存在。
    if len(tokens) < 3:
        return None

    values = [_to_float(token) for token in tokens]
    if values[0] is None:
        return None

    # 前三列是标量：首帧号、丢帧数、进程内存
    frame_no = values[0]
    missed = values[1] if len(values) > 1 else None
    memory_mb = values[2] if len(values) > 2 else None

    rest = [value for value in values[3:] if value is not None]
    intervals = rest[0::2]          # 呈现间隔 ms
    gpu_times = rest[1::2]          # 对应 GPU 时间 ms
    intervals = [value for value in intervals if value > 0]

    mean_interval = _mean(intervals)
    fps = 1000.0 / mean_interval if mean_interval else None

    fps_1pct = None
    if intervals:
        ordered = sorted(intervals)
        worst_index = min(len(ordered) - 1, int(round(0.99 * (len(ordered) - 1))))
        worst = ordered[worst_index]
        fps_1pct = 1000.0 / worst if worst > 0 else None

    return {
        "frame_no": frame_no,
        "missed": missed,
        "memory_mb": memory_mb,
        "frames": len(intervals),
        "interval_ms": mean_interval,
        "gpu_ms": _mean(gpu_times),
        "fps": fps,
        "fps_1pct": fps_1pct,
    }


def _process_name(record: dict) -> str | None:
    path = record.get("processImagePath") or ""
    if path:
        return os.path.basename(path)
    name = record.get("process") or record.get("processImagePath")
    return str(name) if name else None


class HudStreamParser:
    """把 log stream 的 ndjson 行转成帧率样本。"""

    def __init__(self, process_filter: str | None = None) -> None:
        self.process_filter = (process_filter or "").strip().lower() or None
        self._last_frame: float | None = None
        self._last_ts: datetime | None = None
        self.seen_processes: dict[str, int] = {}

    def _matches(self, record: dict, process: str | None) -> bool:
        if not self.process_filter:
            return True
        haystacks = [
            (process or "").lower(),
            str(record.get("processImagePath") or "").lower(),
        ]
        return any(self.process_filter in item for item in haystacks)

    def feed(self, record: dict) -> dict | None:
        parsed = parse_hud_message(record.get("eventMessage"))
        if parsed is None:
            return None
        process = _process_name(record)
        if not self._matches(record, process):
            return None
        ts = parse_log_timestamp(record.get("timestamp"))
        if ts is None:
            return None

        if process:
            self.seen_processes[process] = self.seen_processes.get(process, 0) + 1

        # 帧号差分是格式变化时的兜底：两行之间涨了多少帧，就是这段时间的帧数
        fps_by_count = None
        frame_no = parsed.get("frame_no")
        if frame_no is not None and self._last_frame is not None and self._last_ts is not None:
            delta_frames = frame_no - self._last_frame
            delta_s = (ts - self._last_ts).total_seconds()
            if 0 < delta_s < 10 and delta_frames > 0:
                fps_by_count = delta_frames / delta_s
        if frame_no is not None:
            self._last_frame = frame_no
            self._last_ts = ts

        fps = parsed.get("fps")
        if fps is None:
            fps = fps_by_count

        return {
            "timestamp": iso_utc(ts),
            "process": process,
            "fps": round(fps, 3) if fps is not None else None,
            "fps_1pct": round(parsed["fps_1pct"], 3) if parsed.get("fps_1pct") else None,
            "frames": parsed.get("frames"),
            "interval_ms": round(parsed["interval_ms"], 3) if parsed.get("interval_ms") else None,
            "gpu_ms": round(parsed["gpu_ms"], 3) if parsed.get("gpu_ms") else None,
            "missed": parsed.get("missed"),
            "memory_mb": parsed.get("memory_mb"),
            "fps_by_count": round(fps_by_count, 3) if fps_by_count else None,
        }


def dominant_process(seen: dict[str, int]) -> str | None:
    if not seen:
        return None
    return max(seen.items(), key=lambda item: item[1])[0]


def summarize(samples: list[dict]) -> dict:
    fps_values = [s["fps"] for s in samples if s.get("fps") is not None]
    if not fps_values:
        return {"count": len(samples), "fps_samples": 0}
    ordered = sorted(fps_values)
    low_index = min(len(ordered) - 1, int(round(0.01 * (len(ordered) - 1))))
    return {
        "count": len(samples),
        "fps_samples": len(fps_values),
        "fps_avg": sum(fps_values) / len(fps_values),
        "fps_min": ordered[0],
        "fps_max": ordered[-1],
        "fps_1pct_low": ordered[low_index],
        "processes": sorted({s.get("process") for s in samples if s.get("process")}),
    }


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #
def hud_selftest(seconds: float = 3.0, process_name: str | None = None) -> dict:
    """实测一次「开 HUD → 读日志 → 解析」，测完把环境变量还原。

    只验证链路通不通，不要求此刻真有游戏在跑；没有样本是正常结果。
    """
    before = hud_env_snapshot()
    snapshot, applied, failed = enable_hud_env()
    result: dict = {
        "env_before": before,
        "applied": applied,
        "can_set": not failed,
        "failed_keys": failed,
        "raw_lines": 0,
        "samples": [],
        "error": None,
        "stderr": "",
    }

    cmd = build_log_stream_command(process_name)
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", start_new_session=True,
        )
    except OSError as exc:
        restore_hud_env(snapshot, applied)
        result["error"] = f"无法启动 log stream：{exc}"
        return result

    def stop() -> None:
        try:
            proc.terminate()
        except OSError:
            pass

    timer = threading.Timer(seconds, stop)
    timer.start()
    try:
        out, err = proc.communicate(timeout=seconds + 6)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    finally:
        timer.cancel()
        restore_hud_env(snapshot, applied)

    parser = HudStreamParser(process_name)
    samples: list[dict] = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line:
            continue
        result["raw_lines"] += 1
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        sample = parser.feed(record)
        if sample is not None:
            samples.append(sample)
    result["samples"] = samples[:5]
    result["sample_count"] = len(samples)
    stderr_text = (err or "").strip()
    if stderr_text:
        result["stderr"] = stderr_text[-400:]
        if not result["can_set"]:
            result["error"] = "launchctl 无法设置环境变量，Metal HUD 打不开"
    return result
