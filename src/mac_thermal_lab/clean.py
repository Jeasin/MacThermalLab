"""会话瘦身：原始日志归档到缓存，数据目录只留「能重建报告」的部分。

为什么能瘦身：

- `macmon.jsonl` 每行都重发一份 SoC 频率表与逐核数组（`soc.cpu_tiers`、
  `ecpu_cores`、`pcpu_cores`），这些是硬件描述、整场不变，占了约 3/4 的体积；
  实测数值都在顶层标量里。`metrics.jsonl` 只去掉这些，一个数都不改。
- `hud.log` 是 `log stream --style ndjson` 的原文，每行还带
  `backtrace` / `senderImageUUID` / `threadID` 等日志元数据，比 HUD 本身大两个数量级；
  解析后的 `fps.jsonl` 已经够画图和统计。
- `powermetrics.log` 同理，`powermetrics.samples.jsonl` 是解析结果。

所以归档这三样，报告和数据仍能完整重建；要原样留档的人用 `--restore` 取回。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import macmon as macmon_mod
from .paths import AppPaths
from .powermetrics import parse_log_file
from .storage import (
    SessionPaths,
    lock_is_held,
    read_json,
    write_json,
)

CHUNK = 1 << 20
THERMAL_SAMPLES_FILE = "powermetrics.samples.jsonl"


def dir_size(path: Path) -> int:
    """会话目录里所有文件的总字节数（不递归进子目录以外的东西）。"""
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def archive_dir(app: AppPaths, session: SessionPaths) -> Path:
    return app.raw_archive_dir / session.name


def is_running(session: SessionPaths) -> bool:
    """还在采集里的会话不动：文件正被写入，搬走就把守护进程写崩了。"""
    if lock_is_held(session.lock):
        return True
    meta = read_json(session.metadata) or {}
    return meta.get("ended_at") is None


def build_clean_metrics(session: SessionPaths) -> tuple[int, int]:
    """从 macmon.jsonl 生成 metrics.jsonl，返回 (源字节, 产物字节)。

    原始文件已归档时，就对现有的 metrics.jsonl 再瘦一遍——这样 `mtl clean`
    可以重复执行，精简规则升级后也不用先把归档搬回来。
    """
    source = session.macmon_raw
    in_place = False
    if not source.exists():
        source = session.metrics_clean
        in_place = True
    if not source.exists():
        return 0, 0

    target = session.metrics_clean
    tmp = target.with_suffix(".jsonl.tmp")
    src_bytes = out_bytes = 0
    with source.open("r", encoding="utf-8", errors="replace") as fh, tmp.open(
        "w", encoding="utf-8"
    ) as out:
        for line in fh:
            src_bytes += len(line)
            line = line.strip()
            if not line:
                continue
            record = macmon_mod.load_line(line)
            if record is None:
                continue
            text = macmon_mod.dump_line(macmon_mod.slim_record(record))
            out.write(text)
            out_bytes += len(text)
    if in_place and src_bytes and out_bytes >= src_bytes:
        # 已经是最精简形态，别把文件改成同样的内容
        tmp.unlink(missing_ok=True)
        return src_bytes, src_bytes
    tmp.replace(target)
    return src_bytes, out_bytes


def build_thermal_sidecar(session: SessionPaths) -> int:
    """原始日志归档前，确认解析缓存存在；缺失就用原始日志重建。

    报告读不到 `powermetrics.log` 时会退回 `powermetrics.samples.jsonl`。
    归档前不补这一步，Thermal Pressure 就会跟着原始日志一起「消失」。
    已有缓存不动：采集时写的那份可能含「无头部时间戳」样本的到达时刻。
    """
    raw = session.powermetrics_raw
    sidecar = session.path(THERMAL_SAMPLES_FILE)
    if not raw.exists():
        return 0
    if sidecar.exists() and any(line.strip() for line in sidecar.open(encoding="utf-8")):
        return 0

    samples = [s for s in parse_log_file(raw) if s.ts is not None]
    if not samples:
        return 0
    texts = "".join(
        json.dumps(sample.to_record(), ensure_ascii=False) + "\n" for sample in samples
    )
    sidecar.write_text(texts, encoding="utf-8")
    return len(samples)


def archive_raw(session: SessionPaths, app: AppPaths, dry_run: bool = False) -> dict[str, int]:
    """把原始文件挪进缓存归档目录，返回 {文件名: 字节数}。"""
    targets = session.raw_archive_targets()
    moved: dict[str, int] = {}
    if not targets:
        return moved
    dest_dir = archive_dir(app, session)
    for path in targets:
        size = path.stat().st_size
        moved[path.name] = size
        if dry_run:
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest_dir / path.name))

    if not dry_run:
        meta = read_json(session.metadata) or {}
        meta["raw_archived"] = {
            "dir": str(dest_dir),
            "files": {name: moved[name] for name in moved},
            "archived_at": _now_iso(),
        }
        write_json(session.metadata, meta)
    return moved


def restore_raw(session: SessionPaths, app: AppPaths) -> list[Path]:
    """把归档的原始文件搬回会话目录（已有的不覆盖，避免盖掉新录的数据）。"""
    src_dir = archive_dir(app, session)
    if not src_dir.exists():
        return []
    restored: list[Path] = []
    for path in sorted(src_dir.iterdir()):
        if not path.is_file():
            continue
        target = session.root / path.name
        if target.exists():
            continue
        shutil.move(str(path), str(target))
        restored.append(target)
    if restored:
        meta = read_json(session.metadata) or {}
        if meta.pop("raw_archived", None) is not None:
            write_json(session.metadata, meta)
    try:
        src_dir.rmdir()
    except OSError:
        pass
    return restored


def purge_archive(session: SessionPaths, app: AppPaths) -> tuple[int, int]:
    """删掉缓存里的归档，返回 (文件数, 字节数)。"""
    src_dir = archive_dir(app, session)
    if not src_dir.exists():
        return 0, 0
    count = size = 0
    for path in sorted(src_dir.rglob("*")):
        if not path.is_file():
            continue
        size += path.stat().st_size
        path.unlink()
        count += 1
    shutil.rmtree(src_dir, ignore_errors=True)
    meta = read_json(session.metadata) or {}
    if meta.pop("raw_archived", None) is not None:
        meta["raw_purged_at"] = _now_iso()
        write_json(session.metadata, meta)
    return count, size


def archive_summary(app: AppPaths) -> tuple[int, int]:
    """整个归档目录的 (文件数, 字节数)。"""
    root = app.raw_archive_dir
    count = size = 0
    if not root.exists():
        return 0, 0
    for path in root.rglob("*"):
        if path.is_file():
            count += 1
            size += path.stat().st_size
    return count, size


@dataclass(slots=True)
class CleanResult:
    session: SessionPaths
    before_bytes: int = 0
    after_bytes: int = 0
    archived: dict[str, int] = field(default_factory=dict)
    slim_before: int = 0
    slim_after: int = 0
    report_written: bool = False
    skipped: str | None = None

    @property
    def saved(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


def _now_iso() -> str:
    from .storage import iso_utc, utc_now

    return iso_utc(utc_now())


def needs_report(session: SessionPaths) -> bool:
    """报告是否该重出：不存在，或比任何输入文件都旧。"""
    dashboard = session.dashboard
    if not dashboard.exists():
        return True
    newest = dashboard.stat().st_mtime
    for name in ("macmon.jsonl", "metrics.jsonl", "fps.jsonl", "powermetrics.samples.jsonl",
                 "events.jsonl", "metadata.json"):
        path = session.path(name)
        if not path.exists():
            continue
        if path.stat().st_mtime > newest + 1:
            return True
    return False


def clean_session(
    session: SessionPaths, app: AppPaths, *, dry_run: bool = False, make_report: bool = True
) -> CleanResult:
    """瘦身一场实验：精简 macmon → 刷新报告 → 原始日志归档。"""
    result = CleanResult(session=session, before_bytes=dir_size(session.root))

    if is_running(session):
        result.skipped = "仍在采集中"
        result.after_bytes = result.before_bytes
        return result

    if dry_run:
        result.slim_before = session.macmon_raw.stat().st_size if session.macmon_raw.exists() else 0
        result.archived = {
            p.name: p.stat().st_size for p in session.raw_archive_targets()
        }
        projected = result.before_bytes - sum(result.archived.values()) + result.slim_before // 4
        result.after_bytes = max(0, projected)
        return result

    result.slim_before, result.slim_after = build_clean_metrics(session)
    build_thermal_sidecar(session)

    if make_report and needs_report(session):
        from .report import generate_report

        generate_report(session)
        result.report_written = True

    result.archived = archive_raw(session, app, dry_run=dry_run)
    result.after_bytes = dir_size(session.root)
    return result


def sessions_with_archive(app: AppPaths) -> list[str]:
    root = app.raw_archive_dir
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024 or unit == "GB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return f"{num:.1f} GB"


__all__ = [
    "CleanResult",
    "archive_dir",
    "archive_raw",
    "archive_summary",
    "build_clean_metrics",
    "clean_session",
    "dir_size",
    "human_size",
    "is_running",
    "needs_report",
    "purge_archive",
    "restore_raw",
    "sessions_with_archive",
]
