"""会话目录布局、时间戳处理与 JSON/JSONL 读写。

设计要点：
- 内部一律使用「带时区的 datetime」，比较与对齐前统一转到 UTC。
- 展示给用户时才转成本机本地时间。
- 所有写盘操作都 flush，避免异常退出丢数据。
"""

from __future__ import annotations

import json
import os
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .paths import resolve_paths, session_runtime_dir, session_state_dir

UTC = timezone.utc
SCHEMA_VERSION = 1

METADATA = "metadata.json"
STATE = "state.json"
LOCK = "daemon.lock"
PID = "pid"
MACMON_RAW = "macmon.jsonl"
POWERMETRICS_RAW = "powermetrics.log"
POWERMETRICS_ERR = "powermetrics.err.log"
EVENTS = "events.jsonl"
MERGED_CSV = "merged.csv"
DASHBOARD = "dashboard.html"
DAEMON_LOG = "collector.log"
HUD_RAW = "hud.log"
HUD_ERR = "hud.err.log"
FPS_SAMPLES = "fps.jsonl"
METRICS_CLEAN = "metrics.jsonl"

# 会话目录里属于「实验数据」的文件（会随迁移一起搬走）
DATA_FILES = (
    METADATA,
    MACMON_RAW,
    POWERMETRICS_RAW,
    POWERMETRICS_ERR,
    EVENTS,
    MERGED_CSV,
    DASHBOARD,
    "powermetrics.samples.jsonl",
    HUD_RAW,
    HUD_ERR,
    FPS_SAMPLES,
    METRICS_CLEAN,
)

# 瘦身时挪去缓存归档的原始文件（体积大头，且都能从精简数据重建）
RAW_ARCHIVE_FILES = (
    MACMON_RAW,
    HUD_RAW,
    HUD_ERR,
    POWERMETRICS_RAW,
    POWERMETRICS_ERR,
)

# 旧布局里混在会话目录中的运行期文件，迁移时挪去 state/runtime
LEGACY_RUNTIME_FILES = (STATE, DAEMON_LOG, LOCK)


# --------------------------------------------------------------------------- #
# 时间
# --------------------------------------------------------------------------- #
def utc_now() -> datetime:
    return datetime.now(UTC)


def local_tz():
    return datetime.now().astimezone().tzinfo


def as_utc(dt: datetime) -> datetime:
    """把 datetime 变成带时区的 UTC。naive 视为本机本地时间。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz())
    return dt.astimezone(UTC)


def iso_utc(dt: datetime) -> str:
    return as_utc(dt).isoformat(timespec="milliseconds")


def parse_timestamp(value: str | None) -> datetime | None:
    """尽最大努力解析时间戳，永远返回带时区的 datetime 或 None。

    支持 ISO 8601（带/不帯偏移、Z 后缀、空格分隔）以及
    powermetrics 的 `Wed Nov 11 19:43:21 2026 +0800` 形式。
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None

    # ISO 8601，含微秒与偏移
    candidate = text.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        return as_utc(datetime.fromisoformat(candidate))
    except ValueError:
        pass

    # 只有日期时间、没有偏移（视为本地时间）
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return as_utc(datetime.strptime(text, fmt))
        except ValueError:
            continue

    # C 风格时间：day 可能是空格补齐的（"Nov  5"）
    normalized = " ".join(text.split())
    for fmt in (
        "%a %b %d %H:%M:%S %Y %z",
        "%a %b %d %H:%M:%S %Z %Y",
        "%a %b %d %H:%M:%S %Y",
        "%b %d %H:%M:%S %Y",
    ):
        try:
            return as_utc(datetime.strptime(normalized, fmt))
        except ValueError:
            continue
    return None


def fmt_local(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    if dt is None:
        return "-"
    return as_utc(dt).astimezone(local_tz()).strftime(fmt)


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


# --------------------------------------------------------------------------- #
# 文件名
# --------------------------------------------------------------------------- #
def slugify(name: str, max_len: int = 32) -> str:
    """把实验名变成安全的目录名片段，保留中文。"""
    text = unicodedata.normalize("NFKC", name).strip()
    out = []
    for ch in text:
        if ch.isalnum() or ch in "-_":
            out.append(ch)
        elif ch.isspace() or ch in "/\\:*?\"<>|":
            out.append("-")
    slug = "".join(out).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return (slug[:max_len].strip("-") or "session")


# --------------------------------------------------------------------------- #
# 数据根目录
# --------------------------------------------------------------------------- #
def default_data_root() -> Path:
    """当前生效的 recordings 目录（XDG 解析结果，可由配置覆盖）。"""
    return resolve_paths().recordings_dir


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SessionPaths:
    """一个实验的路径集合。

    root 是实验数据目录（XDG_DATA_HOME 下的 recordings/<session>）；
    运行诊断与锁不放在这里，分别落到 XDG_STATE 和 XDG_RUNTIME。
    """

    root: Path

    @property
    def dir(self) -> Path:
        return self.root

    def path(self, name: str) -> Path:
        return self.root / name

    @property
    def metadata(self) -> Path:
        return self.root / METADATA

    # ---- 运行诊断（XDG_STATE_HOME）----
    @property
    def state_dir(self) -> Path:
        return session_state_dir(self.name)

    @property
    def state(self) -> Path:
        return self.state_dir / STATE

    @property
    def daemon_log(self) -> Path:
        return self.state_dir / DAEMON_LOG

    # ---- 运行期（XDG_RUNTIME_DIR）----
    @property
    def runtime_dir(self) -> Path:
        return session_runtime_dir()

    @property
    def lock(self) -> Path:
        return self.runtime_dir / f"{self.name}.lock"

    @property
    def pid_file(self) -> Path:
        return self.runtime_dir / f"{self.name}.pid"

    # ---- 实验数据（XDG_DATA_HOME）----
    @property
    def macmon_raw(self) -> Path:
        return self.root / MACMON_RAW

    @property
    def powermetrics_raw(self) -> Path:
        return self.root / POWERMETRICS_RAW

    @property
    def powermetrics_err(self) -> Path:
        return self.root / POWERMETRICS_ERR

    @property
    def events(self) -> Path:
        return self.root / EVENTS

    @property
    def merged_csv(self) -> Path:
        return self.root / MERGED_CSV

    @property
    def dashboard(self) -> Path:
        return self.root / DASHBOARD

    @property
    def hud_raw(self) -> Path:
        return self.root / HUD_RAW

    @property
    def hud_err(self) -> Path:
        return self.root / HUD_ERR

    @property
    def fps_samples(self) -> Path:
        return self.root / FPS_SAMPLES

    @property
    def metrics_clean(self) -> Path:
        """精简后的 macmon 采样：同一套字段，去掉每行重复的 SoC 频率表与逐核数组。"""
        return self.root / METRICS_CLEAN

    def raw_archive_targets(self) -> list[Path]:
        """当前仍留在会话目录里的原始文件。"""
        return [self.root / name for name in RAW_ARCHIVE_FILES if (self.root / name).exists()]

    @property
    def legacy_runtime_files(self) -> list[Path]:
        """旧布局遗留在会话目录里的 state.json / collector.log / daemon.lock。"""
        return [self.root / name for name in LEGACY_RUNTIME_FILES if (self.root / name).exists()]

    @property
    def name(self) -> str:
        return self.root.name

    def ensure_runtime_dirs(self) -> None:
        """建好 state / runtime 目录。只读操作不会调用它。"""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)


def create_session_dir(root: Path, label: str | None = None) -> SessionPaths:
    root.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().astimezone(local_tz()).strftime("%Y-%m-%d_%H%M%S")
    base = f"{stamp}_{slugify(label)}" if label else stamp
    candidate = root / base
    index = 2
    while candidate.exists():
        candidate = root / f"{base}-{index}"
        index += 1
    candidate.mkdir(parents=True)
    return SessionPaths(candidate)


def list_sessions(root: Path) -> list[SessionPaths]:
    """列出某个 recordings 目录下的会话（跳过迁移中的临时目录）。"""
    if not root.exists():
        return []
    dirs = [
        p
        for p in root.iterdir()
        if p.is_dir() and not p.name.startswith(".") and (p / METADATA).exists()
    ]
    return [SessionPaths(p) for p in sorted(dirs, key=lambda p: p.name)]


def list_sessions_multi(roots: Iterable[Path]) -> list[SessionPaths]:
    """按 roots 顺序合并会话；同名会话以靠前的目录为准。"""
    seen: dict[str, SessionPaths] = {}
    for root in roots:
        for session in list_sessions(root):
            seen.setdefault(session.name, session)
    return [seen[name] for name in sorted(seen)]


def resolve_session(root: Path, token: str | None) -> SessionPaths:
    """按目录名 / 名字前缀 / latest 定位会话。"""
    sessions = list_sessions(root)
    if not sessions:
        raise LookupError(f"{root} 下还没有任何实验记录")
    if token in (None, "", "latest"):
        return sessions[-1]
    token = token.strip()

    direct = SessionPaths(root / token)
    if direct.metadata.exists():
        return direct

    matches = [s for s in sessions if s.name == token]
    if not matches:
        matches = [s for s in sessions if s.name.startswith(token)]
    if not matches:
        # 允许用实验的显示名（metadata.name）查找
        matches = [s for s in sessions if _meta_name(s) == token]
    if not matches:
        raise LookupError(f"找不到实验：{token}")
    return matches[-1]


def resolve_session_multi(roots: Iterable[Path], token: str | None) -> SessionPaths:
    """在多个 recordings 目录（当前 + 旧布局）里定位会话。"""
    roots = [r for r in roots]
    sessions = list_sessions_multi(roots)
    if not sessions:
        hint = "、".join(str(r) for r in roots) or "-"
        raise LookupError(f"{hint} 下还没有任何实验记录")
    if token in (None, "", "latest"):
        return sessions[-1]
    token = token.strip()

    for root in roots:
        direct = SessionPaths(root / token)
        if direct.metadata.exists():
            return direct

    matches = [s for s in sessions if s.name == token]
    if not matches:
        matches = [s for s in sessions if s.name.startswith(token)]
    if not matches:
        matches = [s for s in sessions if _meta_name(s) == token]
    if not matches:
        raise LookupError(f"找不到实验：{token}")
    return matches[-1]


def _meta_name(session: SessionPaths) -> str:
    meta = read_json(session.metadata) or {}
    return str(meta.get("name") or "")


# --------------------------------------------------------------------------- #
# JSON / JSONL
# --------------------------------------------------------------------------- #
def read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path: Path, data: dict[str, Any]) -> None:
    """原子替换写入，避免读到写了一半的 JSON。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def append_jsonl(fh, record: dict[str, Any]) -> None:
    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    fh.flush()


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """逐行读取 JSONL，跳过坏行（原始文件不动）。"""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return list(iter_jsonl(path))


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            total += chunk.count(b"\n")
    return total


def tail_lines(path: Path, count: int = 5) -> list[str]:
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [ln for ln in text.splitlines() if ln.strip()][-count:]


def write_csv(path: Path, header: Iterable[str], rows: Iterable[Iterable[Any]]) -> None:
    import csv

    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(list(header))
        writer.writerows(rows)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# 守护进程存活判定（flock，避免 PID 复用与 ps 依赖）
# --------------------------------------------------------------------------- #
def acquire_lock(path: Path):
    """守护进程持锁：返回打开的文件对象，进程退出/被杀时内核自动释放。"""
    import fcntl

    fh = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()) + "\n")
        fh.flush()
    except OSError:
        pass
    return fh


def lock_is_held(path: Path) -> bool:
    """True 表示仍有进程持有该会话的锁（即守护进程还活着）。"""
    import fcntl

    if not path.exists():
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)
