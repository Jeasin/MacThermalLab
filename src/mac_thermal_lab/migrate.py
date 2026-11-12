"""从旧布局（源码目录下的 recordings/）迁移到 XDG 数据目录。

安全约束（顺序即保证）：
1. 目标已存在同名会话 → 记为冲突，跳过，绝不覆盖。
2. 先复制到目标目录下的临时目录 `.migrating-*`。
3. 复制后校验文件数量与字节总数，不一致就中止并删除临时目录。
4. 校验通过才改名成正式目录，再删除源目录。
5. 只有源目录里所有会话都处理完且源目录为空，才清理源目录本身。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .paths import session_runtime_dir, session_state_dir
from .storage import DAEMON_LOG, LOCK, METADATA, STATE


@dataclass
class SessionPlan:
    name: str
    source: Path
    target: Path
    status: str = "pending"  # pending / conflict / skip / done / failed / dry
    reason: str = ""


@dataclass
class MigrationReport:
    source: Path
    target: Path
    plans: list[SessionPlan] = field(default_factory=list)
    source_removed: bool = False
    note: str = ""
    errors: list[str] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for p in self.plans if p.status == status)

    @property
    def moved(self) -> int:
        return self.count("done") + self.count("dry")

    @property
    def ok(self) -> bool:
        return not self.errors and self.count("failed") == 0 and self.count("conflict") == 0


def _tree_stats(root: Path) -> tuple[int, int]:
    files = 0
    size = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            try:
                size += path.stat().st_size
                files += 1
            except OSError:
                continue
    return files, size


def plan_migration(source: Path, target: Path) -> MigrationReport:
    report = MigrationReport(source=source, target=target)
    if not source.exists():
        report.errors.append(f"旧目录不存在：{source}")
        return report
    for entry in sorted(source.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if not (entry / METADATA).exists():
            continue
        plan = SessionPlan(name=entry.name, source=entry, target=target / entry.name)
        if plan.target.exists():
            plan.status = "conflict"
            plan.reason = "目标已有同名会话，未覆盖"
        report.plans.append(plan)
    return report


def _move_runtime_files(session_dir: Path, migrated_dir: Path | None = None) -> None:
    """把旧布局里混在会话目录的运行期文件挪到 state / runtime。

    迁移时 migrate 目录是整目录复制的，所以还要把副本里的同类文件删掉，
    避免数据目录里继续混着 state.json / collector.log。
    """
    state_dir = session_state_dir(session_dir.name, create=True)
    runtime_dir = session_runtime_dir(create=True)
    pairs = (
        (STATE, state_dir, STATE),
        (DAEMON_LOG, state_dir, DAEMON_LOG),
        (LOCK, runtime_dir, f"{session_dir.name}.lock"),
    )
    for name, dest_dir, dest_name in pairs:
        src = session_dir / name
        dest = dest_dir / dest_name
        if src.exists():
            try:
                if dest.exists():
                    src.unlink()
                else:
                    shutil.move(str(src), str(dest))
            except OSError:
                pass  # 运行期文件挪不动不算致命
        copy = migrated_dir / name if migrated_dir is not None else None
        if copy is not None and copy.exists():
            try:
                copy.unlink()
            except OSError:
                pass


def run_migration(
    source: Path,
    target: Path,
    dry_run: bool = False,
    progress: Callable[[str], None] | None = None,
) -> MigrationReport:
    say = progress or (lambda _msg: None)
    target.mkdir(parents=True, exist_ok=True)
    report = plan_migration(source, target)

    for plan in report.plans:
        if plan.status == "conflict":
            say(f"跳过 {plan.name}：{plan.reason}")
            continue
        if dry_run:
            plan.status = "dry"
            say(f"[预演] 会迁移 {plan.name}")
            continue

        staging = target / f".migrating-{plan.name}-{os.getpid()}"
        try:
            shutil.copytree(plan.source, staging, symlinks=False)
        except OSError as exc:
            report.errors.append(f"{plan.name} 复制失败：{exc}")
            plan.status = "failed"
            plan.reason = str(exc)
            shutil.rmtree(staging, ignore_errors=True)
            continue

        src_files, src_size = _tree_stats(plan.source)
        dst_files, dst_size = _tree_stats(staging)
        if (src_files, src_size) != (dst_files, dst_size):
            plan.status = "failed"
            plan.reason = (
                f"校验不一致（源 {src_files} 个文件 {src_size}B，"
                f"目标 {dst_files} 个文件 {dst_size}B）"
            )
            report.errors.append(f"{plan.name} {plan.reason}")
            shutil.rmtree(staging, ignore_errors=True)
            continue

        try:
            staging.rename(plan.target)
        except OSError as exc:
            plan.status = "failed"
            plan.reason = f"落位失败：{exc}"
            report.errors.append(f"{plan.name} {plan.reason}")
            shutil.rmtree(staging, ignore_errors=True)
            continue

        # 复制已校验通过，此时才动源目录
        _move_runtime_files(plan.source, plan.target)
        try:
            shutil.rmtree(plan.source)
        except OSError as exc:
            plan.status = "done"
            plan.reason = f"已迁移，但旧目录清理失败：{exc}"
            report.errors.append(f"{plan.name} {plan.reason}")
            continue

        plan.status = "done"
        say(f"已迁移 {plan.name}")

    if not dry_run and not report.errors:
        _cleanup_source(source, report)
    return report


def _cleanup_source(source: Path, report: MigrationReport) -> None:
    """源目录空了才删；里面还有别的东西就留着。"""
    try:
        leftovers = [p.name for p in source.iterdir() if not p.name.startswith(".")]
    except OSError:
        return
    if leftovers:
        report.errors.append(f"旧目录仍有未迁移内容，已保留：{', '.join(leftovers[:5])}")
        return
    try:
        source.rmdir()
        report.source_removed = True
    except OSError:
        # 常见原因只剩 .DS_Store 之类的系统隐藏文件，不值得报错
        report.note = "旧目录里只剩隐藏文件（如 .DS_Store），已保留空目录"
