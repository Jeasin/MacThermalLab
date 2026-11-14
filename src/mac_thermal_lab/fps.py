"""游戏帧率采集：解析 macOS Metal Performance HUD 写到统一日志里的数据。

Apple 的 Metal HUD 在有 `MTL_HUD_ENABLED=1` 时显示浮层；再加上
`MTL_HUD_LOG_ENABLED=1` 后，它每秒往统一日志（Console.app 那个）写一行：

    metal-HUD: <首帧号>,<估计丢帧>,<进程内存 MB>,<帧间隔 ms>,<GPU 时间 ms>,...

前三个是标量，后面是每帧一对「呈现间隔, GPU 时间」。但后半段在实际日志里
是以单个 ``%s`` 输出的，`log stream` 会把它整段脱敏成 ``<private>``：

    metal-HUD: 38,1635.59,3536.35<private>

所以逐帧间隔通常拿不到，只有帧号是可靠的。于是主口径是**帧号差分**：

- ``fps``        两行之间涨了多少帧 ÷ 两行的真实时间差
- ``frames``     该窗口内呈现的帧数（帧号差）
- ``interval_ms`` 该窗口的平均呈现间隔（1000 × 窗口秒数 / 帧数）
- ``fps_1pct``   仅当逐帧间隔没被脱敏时才有值，否则为 None（由汇总层做近似）

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
# 日志脱敏占位符，例如 <private>、<mask.hash: 'abc'>
_REDACTION_RE = re.compile(r"<[^<>]*>")


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
def build_log_stream_command() -> list[str]:
    """构造 log stream 命令。

    用 ndjson 而不是默认的文本格式：字段能直接取，不必猜分隔符。
    谓词**只**筛 HUD 行，不再把进程名塞进去：谓词是系统内核层的硬过滤，
    词写错（比如 --game ott 对上 OcarinaOfTime）就一行都收不到，
    连「其实收到了别的进程的 HUD」都无从得知。进程选择改由解析器做。
    """
    predicate = f'eventMessage CONTAINS "{HUD_LOG_KEY}"'
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
    # 行尾那段逐帧明细是一个 %s 参数，日志里常被脱敏成 <private> 或
    # <mask.hash: ...>；它不是数据，删掉才不会把它黏在最后一个数字上。
    body = _REDACTION_RE.sub("", body)

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

    # 前三列是标量：首帧号、预计丢帧、进程内存（Apple 技术讲座的口径）。
    # 实测日志里第二、三列都随内存增长、且第二列只在内存变化时才变，
    # 语义可能和讲座里的描述对不上，所以这两个值只原样保留，不参与任何计算。
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


ADOPT_MIN_LINES = 3
"""过滤词一条都对不上时，同一个进程要累计多少行 HUD 才敢改用它。

设个门槛是因为 HUD 的环境变量是全局的：录制期间其它 GUI 应用也可能
顺手往日志里写几行，不能让它们抢走「游戏」的身份。
"""


def _norm(text: str | None) -> str:
    """归一化后再比：忽略大小写、空格和标点。

    这样 ``--game "Ocarina of Time"`` 能对上进程名 ``OcarinaOfTime``。
    """
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


class HudStreamParser:
    """把 log stream 的 ndjson 行转成帧率样本。"""

    def __init__(self, process_filter: str | None = None) -> None:
        self.process_filter = (process_filter or "").strip() or None
        self.requested_filter = self.process_filter   # 用户原话，报告里要回显
        self.adopted_from: str | None = None          # 自动改用时的原过滤词
        self._filter_norm = _norm(self.process_filter) or None
        self._last_frame: float | None = None
        self._last_ts: datetime | None = None
        self._accepted = 0
        self.seen_processes: dict[str, int] = {}      # 所有写过 HUD 的进程
        self.rejected_processes: dict[str, int] = {}  # 被过滤词挡掉的那些

    def _matches(self, record: dict, process: str | None) -> bool:
        if not self._filter_norm:
            return True
        haystacks = [_norm(process), _norm(str(record.get("processImagePath") or ""))]
        return any(self._filter_norm in item for item in haystacks if item)

    def _maybe_adopt(self, process: str | None) -> None:
        """过滤词谁也对不上时，改用真正在写 HUD 的进程，别让整场白录。

        只在「一条都还没接受过」时触发，且要同一进程攒够 ADOPT_MIN_LINES 行。
        """
        if not process or self._filter_norm is None or self._accepted or self.adopted_from:
            return
        if self.rejected_processes.get(process, 0) < ADOPT_MIN_LINES:
            return
        self.adopted_from = self.process_filter
        self.process_filter = process
        self._filter_norm = _norm(process)

    def status(self) -> dict:
        """给 CLI / 元数据用的现场信息：谁在写 HUD、过滤词有没有落空。"""
        return {
            "requested_filter": self.requested_filter,
            "filter": self.process_filter,
            "adopted_from": self.adopted_from,
            "seen": dict(self.seen_processes),
            "rejected": dict(self.rejected_processes),
            "accepted": self._accepted,
        }

    def feed(self, record: dict) -> dict | None:
        parsed = parse_hud_message(record.get("eventMessage"))
        if parsed is None:
            return None
        process = _process_name(record)
        if process:
            self.seen_processes[process] = self.seen_processes.get(process, 0) + 1
        if not self._matches(record, process):
            if process:
                self.rejected_processes[process] = (
                    self.rejected_processes.get(process, 0) + 1
                )
            self._maybe_adopt(process)
            if not self._matches(record, process):
                return None
        ts = parse_log_timestamp(record.get("timestamp"))
        if ts is None:
            return None

        # 帧号差分是主口径：两行之间涨了多少帧，就是这段时间的帧数。
        # 同一条 HUD 记录会被 log stream 投递两次（一份 subsystem 为空、
        # 一份 com.apple.metal.hud），帧号必然相同——这类重复要丢掉，
        # 否则每秒会多出一条算不出帧率的空记录，报告里的曲线会被切成碎片。
        fps_by_count = None
        window_s = None
        counted_frames = parsed.get("frames")
        frame_no = parsed.get("frame_no")
        if frame_no is not None:
            if self._last_frame is not None:
                if frame_no == self._last_frame:
                    return None
                if frame_no < self._last_frame:
                    # 游戏重启，帧号归零：重建基线，这一条算不出帧率
                    self._last_frame, self._last_ts = frame_no, ts
                    return None
                delta_frames = frame_no - self._last_frame
                delta_s = (ts - self._last_ts).total_seconds() if self._last_ts else 0.0
                if 0 < delta_s < 10 and delta_frames > 0:
                    fps_by_count = delta_frames / delta_s
                    window_s = delta_s
                    counted_frames = int(delta_frames)
            self._last_frame = frame_no
            self._last_ts = ts

        fps = parsed.get("fps")
        if fps is None:
            fps = fps_by_count
        if fps is None:
            # 第一条（或游戏重启后的第一条）没有参照，算不出帧率。
            # 这种行不带任何可用信息，不写进 fps.jsonl，下游也就不必到处判空。
            return None

        interval_ms = parsed.get("interval_ms")
        if interval_ms is None and window_s and counted_frames:
            interval_ms = window_s * 1000.0 / counted_frames

        self._accepted += 1
        return {
            "timestamp": iso_utc(ts),
            "process": process,
            "fps": round(fps, 3) if fps is not None else None,
            "fps_1pct": round(parsed["fps_1pct"], 3) if parsed.get("fps_1pct") else None,
            "frames": counted_frames,
            "window_s": round(window_s, 3) if window_s else None,
            "interval_ms": round(interval_ms, 3) if interval_ms else None,
            "gpu_ms": round(parsed["gpu_ms"], 3) if parsed.get("gpu_ms") else None,
            "missed": parsed.get("missed"),
            "memory_mb": parsed.get("memory_mb"),
            "fps_by_count": round(fps_by_count, 3) if fps_by_count else None,
        }


def dominant_process(seen: dict[str, int]) -> str | None:
    if not seen:
        return None
    return max(seen.items(), key=lambda item: item[1])[0]


def _window_weights(samples: list[dict]) -> list[tuple[float, float]]:
    """每个采样窗口的 (帧率, 权重)：权重是该窗口贡献了多少帧。

    优先用 HUD 的帧号差；早期版本抓的 fps.jsonl 没有帧数，就用
    「时间跨度 × 帧率」估出来，好过直接对窗口帧率取分位。
    """
    items: list[tuple[float, float]] = []
    prev_ts = None
    for sample in samples:
        fps = sample.get("fps")
        ts = sample.get("ts")
        weight = sample.get("frames") or 0
        if not weight and fps is not None and ts is not None and prev_ts is not None:
            span = (ts - prev_ts).total_seconds()
            if 0 < span < 10:
                weight = fps * span
        if fps is not None and weight:
            items.append((fps, weight))
        if ts is not None:
            prev_ts = ts
    return items


def _weighted_low(samples: list[dict], share: float = 0.01) -> float | None:
    """按帧数加权的低帧率分位：帧率从低到高累计帧数，取到占总帧数 ``share`` 处的帧率。

    逐帧间隔拿不到时，这是「最慢的 1% 帧有多慢」最接近的近似——采样窗口越慢，
    贡献的帧也越少，所以不能对窗口帧率简单取分位（那样会明显偏乐观）。
    """
    weighted = _window_weights(samples)
    total = sum(frames for _, frames in weighted)
    if total <= 0:
        return None
    need = total * share
    accumulated = 0
    for fps, frames in sorted(weighted):
        accumulated += frames
        if accumulated >= need:
            return fps
    return min(fps for fps, _ in weighted)


def recover_from_log(
    start: datetime, end: datetime, process_filter: str | None = None, timeout: float = 300.0
) -> dict:
    """从统一日志的历史里补回帧率。

    `log stream` 只能读实时流；已经过去的 HUD 行要用 `log show` 翻历史。
    录制时过滤词写错、事后才发现的场景靠这个救回来（受系统日志保留期限制）。
    """
    cmd = [
        LOG_BIN,
        "show",
        "--start", start.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "--end", end.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "--style", "ndjson",
        "--predicate", f'eventMessage CONTAINS "{HUD_LOG_KEY}"',
    ]
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"samples": [], "status": {}, "raw_lines": 0, "command": cmd,
                "error": f"无法读取系统日志历史：{exc}"}

    parser = HudStreamParser(process_filter)
    samples: list[dict] = []
    raw_lines = 0
    for line in (completed.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        raw_lines += 1
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        sample = parser.feed(record)
        if sample is not None:
            samples.append(sample)
    error = None
    if completed.returncode not in (0, 1):     # log show 没匹配到时会返回 1
        error = (completed.stderr or "").strip()[-300:] or f"log show 退出码 {completed.returncode}"
    return {
        "samples": samples,
        "status": parser.status(),
        "raw_lines": raw_lines,
        "command": cmd,
        "error": error,
    }


def summarize(samples: list[dict]) -> dict:
    """汇总帧率。

    1% low 有两个来源：逐帧间隔没被脱敏时用真实的最差 1% 帧间隔；
    拿不到时退化为按帧数加权的窗口近似，并在 ``fps_1pct_source``
    里标明，报告据此决定要不要写「近似」。
    """
    fps_values = [s["fps"] for s in samples if s.get("fps") is not None]
    if not fps_values:
        return {"count": len(samples), "fps_samples": 0}
    ordered = sorted(fps_values)
    low_index = min(len(ordered) - 1, int(round(0.01 * (len(ordered) - 1))))
    frame_lows = sorted(s["fps_1pct"] for s in samples if s.get("fps_1pct") is not None)
    if frame_lows:
        frame_low_index = min(len(frame_lows) - 1, int(round(0.01 * (len(frame_lows) - 1))))
        fps_1pct_low = frame_lows[frame_low_index]
        fps_1pct_source = "frame"
    else:
        weighted = _weighted_low(samples)
        fps_1pct_low = weighted if weighted is not None else ordered[low_index]
        fps_1pct_source = "window" if weighted is not None else "percentile"
    return {
        "count": len(samples),
        "fps_samples": len(fps_values),
        "fps_avg": sum(fps_values) / len(fps_values),
        "fps_min": ordered[0],
        "fps_max": ordered[-1],
        "fps_1pct_low": fps_1pct_low,
        "fps_1pct_source": fps_1pct_source,
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

    cmd = build_log_stream_command()
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
    result["status"] = parser.status()
    stderr_text = (err or "").strip()
    if stderr_text:
        result["stderr"] = stderr_text[-400:]
        if not result["can_set"]:
            result["error"] = "launchctl 无法设置环境变量，Metal HUD 打不开"
    return result
