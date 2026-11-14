"""`mtl` 命令行入口。"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from . import fps as fps_mod
from .analysis import load_session, summary as build_summary
from .collector import run_collector
from .events import append_event
from .macmon import find_macmon, macmon_version
from .migrate import run_migration
from .paths import (
    AppPaths,
    legacy_recordings,
    resolve_paths,
    write_config_template,
)
from .powermetrics import sudo_nopasswd, thermal_selftest
from .report import generate_report
from .storage import (
    SessionPaths,
    create_session_dir,
    fmt_duration,
    fmt_local,
    list_sessions,
    list_sessions_multi,
    lock_is_held,
    parse_timestamp,
    read_json,
    resolve_session_multi,
    utc_now,
    write_json,
)

C_OK = "\033[32m"
C_WARN = "\033[33m"
C_ERR = "\033[31m"
C_DIM = "\033[2m"
C_BOLD = "\033[1m"
C_OFF = "\033[0m"


def _supports_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(text: str, color: str) -> str:
    return f"{color}{text}{C_OFF}" if _supports_color() else text


def ok(text: str) -> str:
    return f"{c('✓', C_OK)} {text}"


def warn(text: str) -> str:
    return f"{c('!', C_WARN)} {text}"


def err(text: str) -> str:
    return f"{c('✗', C_ERR)} {text}"


def heading(text: str) -> str:
    return c(text, C_BOLD)


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def _width(text: str) -> int:
    """按终端显示宽度计算（CJK 占两列）。"""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _width(cell))
    lines = ["  ".join(_pad(h, widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append("  ".join(_pad(cell, widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


def _describe_counts(counts: dict[str, Any], limit: int = 4) -> str:
    """把 {"进程": 行数} 排成一行可读文本，多的时候只列前几个。"""
    ordered = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    shown = "、".join(f"{name}（{count} 行）" for name, count in ordered[:limit])
    if len(ordered) > limit:
        shown += f" 等 {len(ordered)} 个"
    return shown or "-"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cmdline(pid: int) -> str:
    try:
        res = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout.strip()


def _is_our_daemon(pid: int, session_dir: Path) -> bool:
    cmd = _cmdline(pid)
    return "mac_thermal_lab" in cmd and str(session_dir) in cmd


def _child_verdict(pid: int, pgid: int, name: str) -> str:
    """判断某个残留进程是不是我们启动的采集子进程。

    返回 ours / unverified / foreign。ps 不可用时退化为「进程组归属」判断，
    因为我们启动子进程时用了 start_new_session，pgid 必然等于自身 pid。
    """
    keyword = "macmon" if name == "macmon" else "powermetrics"
    cmd = _cmdline(pid)
    if cmd:
        try:
            same_group = os.getpgid(pid) == pgid
        except OSError:
            return "foreign"
        if keyword in cmd and same_group:
            return "ours"
        return "foreign"
    try:
        if pgid == pid and os.getpgid(pid) == pgid:
            return "unverified"
    except OSError:
        return "foreign"
    return "foreign"


def _session_status(session: SessionPaths) -> dict[str, Any]:
    state = read_json(session.state) or {}
    meta = read_json(session.metadata) or {}
    pid = int(state.get("pid") or 0)

    # 首选 flock 判活：内核保证进程退出（含被杀）即释放，不受 PID 复用影响
    if lock_is_held(session.lock):
        alive = True
    elif pid and _pid_alive(pid):
        alive = _is_our_daemon(pid, session.root)  # 兼容没有锁文件的旧会话
    else:
        alive = False
    return {"state": state, "meta": meta, "pid": pid, "alive": alive}


def _app_paths(args) -> AppPaths:
    """解析本次调用生效的 XDG 目录；--data-dir 可临时覆盖 recordings。"""
    return resolve_paths(getattr(args, "data_dir", None))


def _records_roots(paths: AppPaths) -> list[Path]:
    """先看当前 recordings，再看旧布局，避免迁移前「历史实验消失」。"""
    roots = [paths.recordings_dir]
    legacy = legacy_recordings()
    if legacy is not None:
        roots.append(legacy)
    return roots


def _active_session(roots: list[Path]) -> SessionPaths | None:
    best: SessionPaths | None = None
    for session in list_sessions_multi(roots):
        info = _session_status(session)
        if info["alive"]:
            best = session
    return best


def _parse_interval(text: str) -> int:
    raw = str(text).strip().lower()
    try:
        if raw.endswith("ms"):
            ms = float(raw[:-2])
        elif raw.endswith("s"):
            ms = float(raw[:-1]) * 1000
        else:
            ms = float(raw)
    except ValueError as exc:
        raise SystemExit(f"无法解析采样间隔：{text}") from exc
    if not 200 <= ms <= 60_000:
        raise SystemExit("采样间隔需在 200ms ~ 60000ms 之间")
    return int(round(ms))


def _host_info() -> dict[str, Any]:
    info: dict[str, Any] = {}
    try:
        out = subprocess.run(
            ["sw_vers", "-productVersion"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        info["os_version"] = out or None
        out = subprocess.run(
            ["sysctl", "-n", "hw.model"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        info["hw_model"] = out or None
        out = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        info["chip"] = out or None
    except (OSError, subprocess.SubprocessError):
        pass
    return info


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_start(args) -> int:
    paths = _app_paths(args)
    roots = _records_roots(paths)
    running = _active_session(roots)
    if running:
        print(err(f"已有实验在进行中：{running.name}"))
        print(f"  先执行 {c('mtl stop', C_BOLD)} 结束它，或用 {c('mtl status', C_BOLD)} 查看进度。")
        return 1
    if not find_macmon():
        print(err("未找到 macmon，请先安装：brew install macmon"))
        return 1

    interval_ms = _parse_interval(args.interval)

    thermal_enabled = not args.no_thermal
    sudo_mode = "disabled"
    password: str | None = None
    if thermal_enabled:
        if sudo_nopasswd():
            sudo_mode = "nopasswd"
        elif sys.stdin.isatty():
            print("采集 macOS Thermal Pressure 需要管理员权限（仅本次使用，不会保存密码）。")
            try:
                password = getpass.getpass("sudo 密码：")
            except KeyboardInterrupt:
                print()
                print(warn("已取消，未开始实验"))
                return 1
            except EOFError:
                password = None
            if password:
                sudo_mode = "password"
            else:
                thermal_enabled = False
                print(warn("未取得 sudo 授权，本次不采集 Thermal Pressure"))
        else:
            thermal_enabled = False
            print(warn("非交互终端无法请求 sudo，本次不采集 Thermal Pressure"))

    # 认证通过后再建目录：中途取消不会留下空会话
    session = create_session_dir(paths.recordings_dir, args.name)

    # 帧率：打开 Metal HUD 的日志开关。必须在游戏启动之前设置——
    # launchctl 设的是用户域环境变量，只对之后新启动的 GUI 应用生效。
    game_filter = getattr(args, "game", None)
    game_enabled = game_filter is not None
    game_snapshot: dict[str, str | None] | None = None
    game_applied: list[str] = []
    game_error: str | None = None
    if game_enabled:
        game_snapshot, game_applied, failed = fps_mod.enable_hud_env()
        if failed:
            game_error = "无法设置 " + "/".join(failed) + "（需要图形登录会话）"

    started_at = utc_now()
    metadata: dict[str, Any] = {
        "schema": 1,
        "session_id": session.name,
        "name": args.name or session.name,
        "started_at": started_at.isoformat(timespec="milliseconds"),
        "ended_at": None,
        "interval_ms": interval_ms,
        "status": "starting",
        "host": _host_info(),
        "soc": {},
        "tools": {
            "mtl": __version__,
            "macmon": macmon_version(),
            "powermetrics": shutil.which("powermetrics"),
        },
        "thermal": {"enabled": thermal_enabled, "sudo_mode": sudo_mode, "error": None},
        "game": {
            "enabled": game_enabled,
            "filter": game_filter or None,
            "env_snapshot": game_snapshot,
            "env_applied": game_applied,
            "error": game_error,
            "process": None,
        },
        "counts": {},
        "errors": [],
    }
    write_json(session.metadata, metadata)

    read_fd: int | None = None
    if password:
        read_fd, write_fd = os.pipe()
        os.write(write_fd, password.encode("utf-8"))
        os.close(write_fd)
        password = None

    cmd = [
        sys.executable, "-m", "mac_thermal_lab", "_collect",
        "--session-dir", str(session.root),
        "--interval-ms", str(interval_ms),
    ]
    if not thermal_enabled:
        cmd.append("--no-thermal")
    if read_fd is not None:
        cmd += ["--password-fd", str(read_fd)]
    if args.no_soc_info:
        cmd.append("--no-soc-info")
    if game_enabled:
        cmd += ["--game-filter", game_filter or ""]

    pass_fds = (read_fd,) if read_fd is not None else ()
    session.ensure_runtime_dirs()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=session.daemon_log.open("a", encoding="utf-8"),
            start_new_session=True,
            pass_fds=pass_fds,
        )
    except OSError as exc:
        print(err(f"启动采集进程失败：{exc}"))
        return 1
    finally:
        if read_fd is not None:
            try:
                os.close(read_fd)
            except OSError:
                pass

    # 等守护进程写出 state.json
    deadline = time.monotonic() + 8.0
    state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = read_json(session.state) or {}
        if state.get("pid") or proc.poll() is not None:
            break
        time.sleep(0.15)

    if proc.poll() is not None and not state:
        print(err("采集进程启动后立即退出，日志如下："))
        log = session.daemon_log
        if log.exists():
            print(c(log.read_text(encoding="utf-8", errors="replace")[-1500:], C_DIM))
        return 1

    print(ok(f"实验已开始：{c(metadata['name'], C_BOLD)}"))
    print(f"  会话目录  {session.name}")
    print(f"  采样间隔  {interval_ms / 1000:.1f}s")
    if thermal_enabled:
        verdict, reason = _thermal_startup_state(session)
        if verdict == "ok":
            print(f"  Thermal   已启用（sudo {sudo_mode}）")
        elif verdict == "fail":
            print(warn(f"Thermal 采集未能启动：{reason}"))
            print(f"  {c('macmon 仍在正常采集', C_DIM)}；"
                  f"结束后可运行 {c('mtl doctor --thermal', C_BOLD)} 排查")
        else:
            print(f"  Thermal   已启用（sudo {sudo_mode}），启动确认超时，用 "
                  f"{c('mtl status', C_BOLD)} 复查")
    else:
        print("  Thermal   未启用（--no-thermal）")

    if game_enabled:
        if game_error:
            print(warn(f"帧率采集可能不可用：{game_error}"))
            print(f"  {c('macmon 与 Thermal 不受影响', C_DIM)}")
        else:
            label = game_filter or "自动识别"
            print(f"  帧率      Metal HUD 已开启（{label}）")
            if game_filter:
                print(f"            {c('过滤词按进程名/路径匹配（大小写与空格不敏感）；一条都对不上时会自动改用真正在写 HUD 的进程', C_DIM)}")
            else:
                print(f"            {c('现在启动游戏即可；HUD 每秒写一行到系统日志', C_DIM)}")
    else:
        print(c("  帧率      未采集（加 --game 可记录）", C_DIM))
    print()
    print(f"  {c('mtl mark \"游戏开始\"', C_BOLD)}   在时间轴打一个事件标记")
    print(f"  {c('mtl status', C_BOLD)}             查看采集状态")
    print(f"  {c('mtl stop', C_BOLD)}               结束实验并生成报告")
    return 0


def _thermal_startup_state(session: SessionPaths, timeout: float = 3.0) -> tuple[str, str]:
    """看守护进程有没有真的把 powermetrics 拉起来，避免「以为采了其实没有」。"""
    deadline = time.monotonic() + timeout
    state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = read_json(session.state) or {}
        child = (state.get("children") or {}).get("powermetrics")
        if child and child.get("alive"):
            return "ok", ""
        for message in state.get("errors") or []:
            if "powermetrics" in message or "sudo" in message:
                return "fail", message
        time.sleep(0.2)
    return "unknown", ""


def cmd_stop(args) -> int:
    paths = _app_paths(args)
    roots = _records_roots(paths)
    force = bool(getattr(args, "force", False))
    active = _active_session(roots)
    session: SessionPaths | None = None

    if args.session:
        try:
            target = resolve_session_multi(roots, args.session)
        except LookupError as exc:
            print(err(str(exc)))
            return 1
        target_meta = read_json(target.metadata) or {}
        target_alive = _session_status(target)["alive"]

        # 名字解析是模糊的（目录名 / 前缀 / 显示名都算），所以 stop 必须显式确认
        # 目标就是正在跑的那个，否则很容易「以为结束了当前实验，其实动的是旧会话」。
        if active is not None and target.root != active.root:
            print(err(f"「{args.session}」不是当前正在进行的实验，未做任何改动"))
            print(f"  当前实验  {active.name}"
                  f"（{fmt_local(parse_timestamp((read_json(active.metadata) or {}).get('started_at')))} 开始）")
            ended = parse_timestamp(target_meta.get("ended_at"))
            print(f"  你指的是  {target.name}"
                  + (f"（已于 {fmt_local(ended)} 结束）" if ended else ""))
            print()
            print(f"  结束当前实验：{c('mtl stop', C_BOLD)}")
            print(f"  只想重出报告：{c(f'mtl report {args.session}', C_BOLD)}")
            return 1

        if not target_alive and target_meta.get("ended_at") and not force:
            ended = parse_timestamp(target_meta.get("ended_at"))
            print(err(f"实验「{target.name}」已经结束过了"))
            print(f"  结束于    {fmt_local(ended)}")
            print()
            print(f"  重新生成报告：{c(f'mtl report {args.session}', C_BOLD)}")
            print(f"  确实要再收尾一次：{c(f'mtl stop {args.session} --force', C_BOLD)}")
            return 1
        session = target
    else:
        session = active
        if session is None:
            # 采集进程已消失（崩溃 / 终端被关）：找最近一个没有 ended_at 的会话继续收尾
            unfinished = [
                s
                for s in list_sessions_multi(roots)
                if not (read_json(s.metadata) or {}).get("ended_at")
            ]
            if unfinished:
                session = unfinished[-1]
                if len(unfinished) > 1:
                    print(warn(f"有 {len(unfinished)} 个未收尾的实验，将处理最近的一个：{session.name}"))
                    print(c("  其余可逐个指定：mtl stop " +
                            " / mtl stop ".join(s.name for s in unfinished[:-1]), C_DIM))
                else:
                    print(warn(f"采集进程已不在，将从已落盘数据恢复：{session.name}"))
    if session is None:
        print(err("当前没有正在进行的实验"))
        return 1

    info = _session_status(session)
    state = info["state"]
    pid = info["pid"]

    if info["alive"]:
        print(f"停止采集进程 pid={pid}（{session.name}） ...")
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            print(warn(f"发送 SIGTERM 失败：{exc}"))
        if not _wait_exit(pid, timeout=25.0):
            print(warn("采集进程未在 25s 内退出，发送 SIGKILL"))
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            _wait_exit(pid, timeout=5.0)
    else:
        print(warn("采集进程已不在运行，将直接从已落盘数据生成报告"))

    # 清理可能残留的、确认属于本次实验的子进程
    _reap_orphans(session, state)

    # 还原帧率采集改过的环境变量：不还原的话 HUD 会一直跟着所有 GUI 应用
    game_meta = (read_json(session.metadata) or {}).get("game") or {}
    if game_meta.get("enabled") and game_meta.get("env_applied"):
        failed = fps_mod.restore_hud_env(
            game_meta.get("env_snapshot"), game_meta.get("env_applied")
        )
        if failed:
            print(warn("Metal HUD 环境变量还原失败：" + "/".join(failed)))
            print(f"  手动关闭：{c('launchctl unsetenv MTL_HUD_ENABLED', C_BOLD)}")
        else:
            print(c("已关闭 Metal HUD（环境变量已还原）", C_DIM))

    meta = read_json(session.metadata) or {}
    if info["alive"]:
        # 本次由命令主动终止，守护进程已优雅收尾
        if not meta.get("ended_at"):
            meta["ended_at"] = utc_now().isoformat(timespec="milliseconds")
            meta["ended_at_source"] = "stop_command"
        meta["stop_reason"] = "manual"
        meta["status"] = "stopped"
    elif not meta.get("ended_at"):
        # 守护进程已消失：用最后一次心跳作结束时刻，别把中断后的空档算进时长
        recovered = (
            parse_timestamp(state.get("heartbeat_at"))
            or parse_timestamp((state.get("last_sample_at") or {}).get("macmon"))
            or utc_now()
        )
        meta["ended_at"] = recovered.isoformat(timespec="milliseconds")
        meta["ended_at_source"] = "recovered"
        meta["stop_reason"] = "recovered"
        meta["status"] = "aborted"
    else:
        reason = str(meta.get("stop_reason") or "")
        if reason.startswith("SIG") or not reason:
            meta["stop_reason"] = "manual"
        meta["status"] = "stopped"
    write_json(session.metadata, meta)

    try:
        dashboard = generate_report(session)
    except Exception as exc:  # noqa: BLE001 - 报告失败不能掩盖原始数据
        print(err(f"生成报告失败：{exc}"))
        print(f"  原始数据完好：{session.root}")
        return 1

    data = load_session(session)
    smy = build_summary(data)
    print(ok("实验已结束"))
    print(f"  会话      {session.name}")
    print(f"  时长      {fmt_duration(smy.get('span_seconds'))}")
    print(f"  样本      macmon {smy['integrity']['macmon']['count']} · "
          f"thermal {smy['integrity']['thermal']['count']} · "
          f"fps {smy['raw_counts'].get('fps_samples', 0)}")
    print(f"  报告      {c(str(dashboard), C_BOLD)}")
    thermal_meta = (read_json(session.metadata) or {}).get("thermal") or {}
    if thermal_meta.get("enabled") and smy["integrity"]["thermal"]["count"] == 0:
        print()
        print(warn("本次启用了 Thermal 采集，但没有拿到任何样本，报告里该面板会是空的"))
        if thermal_meta.get("error"):
            print(f"  原因：{thermal_meta['error']}")
        print(f"  排查：{c('mtl doctor --thermal', C_BOLD)}")
    game_meta = (read_json(session.metadata) or {}).get("game") or {}
    fallback = game_meta.get("filter_fallback") or {}
    if fallback:
        used = str(fallback.get("used"))
        print()
        print(warn(f"--game 过滤词「{fallback.get('requested')}」没有匹配到任何 HUD 进程"))
        print(f"  已自动改用 {c(used, C_BOLD)}，下次直接写 {c('--game ' + used, C_BOLD)} 更省事")
    if game_meta.get("enabled") and smy["raw_counts"].get("fps_samples", 0) == 0:
        print()
        print(warn("本次启用了帧率采集，但没有捕获到 Metal HUD 数据"))
        seen = game_meta.get("hud_seen_processes") or {}
        if game_meta.get("error"):
            print(f"  原因：{game_meta['error']}")
        elif seen:
            print(f"  记录期间有进程在写 HUD，但都被过滤词挡掉了：{_describe_counts(seen)}")
        else:
            print("  常见原因：游戏在设置环境变量之前就启动了；或该游戏不走 Metal 呈现")
        print(f"  排查：{c('mtl doctor --game', C_BOLD)}")
    if smy["warnings"]:
        print()
        for message in smy["warnings"]:
            print(warn(message))
    if args.open:
        subprocess.run(["open", str(dashboard)], check=False)
    return 0


def _wait_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    return not _pid_alive(pid)


def _reap_orphans(session: SessionPaths, state: dict[str, Any]) -> None:
    children = state.get("children") or {}
    for name, child in children.items():
        try:
            pid = int(child.get("pid") or 0)
            pgid = int(child.get("pgid") or pid)
        except (TypeError, ValueError):
            continue
        if pid <= 0 or not _pid_alive(pid):
            continue
        verdict = _child_verdict(pid, pgid, name)
        if verdict == "foreign":
            print(warn(f"发现 pid={pid}（疑似 {name}）不属于本次实验，已跳过"))
            continue
        note = "（ps 不可用，按进程组归属判断）" if verdict == "unverified" else ""
        print(f"清理残留 {name} pid={pid}{note}")
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
        if not _wait_exit(pid, timeout=6.0):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def cmd_status(args) -> int:
    paths = _app_paths(args)
    roots = _records_roots(paths)
    try:
        session = resolve_session_multi(roots, args.session) if args.session else None
    except LookupError as exc:
        print(err(str(exc)))
        return 1
    if session is None:
        session = _active_session(roots) or (
            resolve_session_multi(roots, "latest") if list_sessions_multi(roots) else None
        )
    if session is None:
        print(err("还没有任何实验记录"))
        print(f"  用 {c('mtl start 名称', C_BOLD)} 开始一个")
        return 1

    info = _session_status(session)
    state, meta = info["state"], info["meta"]
    started = parse_timestamp(state.get("started_at")) or parse_timestamp(meta.get("started_at"))

    if info["alive"]:
        print(ok(f"正在采集：{c(meta.get('name') or session.name, C_BOLD)}"))
    else:
        print(c(f"未在采集：{meta.get('name') or session.name}", C_DIM))

    elapsed = None
    last_ts = parse_timestamp((state.get("last_sample_at") or {}).get("macmon"))
    if started:
        if info["alive"]:
            end = utc_now()
        else:
            end = parse_timestamp(meta.get("ended_at")) or last_ts or utc_now()
        elapsed = (end - started).total_seconds()
    print(f"  会话      {session.name}")
    print(f"  开始      {fmt_local(started)}")
    print(f"  时长      {fmt_duration(elapsed)}")
    if not info["alive"] and not meta.get("ended_at"):
        print(warn("采集进程已退出且未正常收尾：数据可能不完整"))
        print(f"           执行 {c('mtl stop', C_BOLD)} 可从已有原始数据生成报告")
    print(f"  采样间隔  {(state.get('interval_ms') or meta.get('interval_ms') or 0) / 1000:.1f}s")
    if info["alive"]:
        print(f"  进程      pid={info['pid']} (pgid={state.get('pgid')})")

    counts = state.get("counts") or meta.get("counts") or {}
    if counts:
        print(f"  样本      macmon {counts.get('macmon_samples', 0)} · "
              f"thermal {counts.get('thermal_samples', 0)} · "
              f"fps {counts.get('fps_samples', 0)}"
              + (f" · 坏行 {counts['macmon_bad_lines']}" if counts.get("macmon_bad_lines") else ""))

    last = state.get("last_sample_at") or {}
    if info["alive"] and last.get("macmon"):
        ts = parse_timestamp(last["macmon"])
        if ts:
            age = (utc_now() - ts).total_seconds()
            flag = "" if age < 15 else c(f"（已 {age:.0f}s 无新样本）", C_WARN)
            print(f"  最近样本  {fmt_local(ts, '%H:%M:%S')} {flag}")

    children = state.get("children") or {}
    for name, child in children.items():
        alive = _pid_alive(int(child.get("pid") or 0))
        mark = c("运行中", C_OK) if alive else c("已退出", C_WARN)
        print(f"  {name:<13} {mark}  pid={child.get('pid')}")

    thermal_meta = meta.get("thermal") or {}
    if thermal_meta.get("enabled") is False:
        print(warn("本次实验未采集 Thermal Pressure"))
    elif thermal_meta.get("error"):
        print(warn(f"Thermal 采集异常：{thermal_meta['error']}"))

    game_meta = meta.get("game") or {}
    hud_state = state.get("hud") or {}
    if game_meta.get("enabled"):
        if game_meta.get("error"):
            print(warn(f"帧率采集异常：{game_meta['error']}"))
        elif game_meta.get("process"):
            print(f"  HUD 进程  {game_meta['process']}")
            if (hud_state.get("adopted_from") or (game_meta.get("filter_fallback") or {})):
                requested = (hud_state.get("adopted_from")
                             or (game_meta.get("filter_fallback") or {}).get("requested"))
                print(f"            {c(f'过滤词「{requested}」没匹配到，已自动改用该进程', C_DIM)}")
        elif counts.get("fps_samples", 0) == 0:
            seen = hud_state.get("seen") or {}
            if seen:
                print(warn("收到 Metal HUD 日志，但没有一行匹配过滤词"
                           f"「{hud_state.get('requested_filter')}」"))
                print(f"  实际在写 HUD：{_describe_counts(seen)}")
                print(f"  已自动改用 {c(str(hud_state.get('filter')), C_BOLD)}，等它出第一条样本"
                      if hud_state.get("adopted_from")
                      else f"  建议：{c('mtl stop', C_BOLD)} 后用 --game <上面的进程名> 重来")
            else:
                print(warn("帧率采集已开启，但还没有收到 Metal HUD 日志（游戏启动了吗？）"))

    errors = state.get("errors") or []
    if errors:
        print()
        for message in errors:
            print(warn(message))

    raw_bytes = 0
    for name in ("macmon.jsonl", "powermetrics.log", "events.jsonl"):
        path = session.path(name)
        if path.exists():
            raw_bytes += path.stat().st_size
    print(f"  已落盘    {raw_bytes / 1024:.0f} KB  →  {session.root}")

    if not info["alive"] and session.dashboard.exists():
        print(f"  报告      {session.dashboard}")
    return 0


def cmd_mark(args) -> int:
    paths = _app_paths(args)
    session = _active_session(_records_roots(paths))
    if session is None:
        print(err("当前没有正在进行的实验，先执行 mtl start"))
        return 1
    started = parse_timestamp((read_json(session.metadata) or {}).get("started_at"))
    # nargs="+" 收到的是词表，直接拼回一句话：mtl mark 风扇 开到最大 也不用引号
    label = " ".join(args.event).strip()
    event = append_event(session.events, label, args.note or "", started)
    elapsed = fmt_duration(event.elapsed_seconds)
    print(ok(f"已标记「{event.event}」 @ {fmt_local(event.ts, '%H:%M:%S')}（+{elapsed}）"))
    return 0


def cmd_report(args) -> int:
    paths = _app_paths(args)
    try:
        session = resolve_session_multi(_records_roots(paths), args.session)
    except LookupError as exc:
        print(err(str(exc)))
        return 1
    if _session_status(session)["alive"]:
        print(warn("该实验仍在采集中，生成的是当前时刻的临时报告"))
    path = generate_report(session)
    print(ok(f"报告已生成：{path}"))
    print(f"  merged.csv  {session.merged_csv}")
    if args.open:
        subprocess.run(["open", str(path)], check=False)
    return 0


def cmd_list(args) -> int:
    paths = _app_paths(args)
    roots = _records_roots(paths)
    sessions = list_sessions(paths.recordings_dir)
    legacy_sessions = list_sessions(roots[-1]) if len(roots) > 1 else []
    all_sessions = list_sessions_multi(roots)
    if not all_sessions:
        print(f"{paths.recordings_dir} 下还没有实验记录")
        return 0

    current_names = {s.name for s in sessions}
    legacy_names = {s.name for s in legacy_sessions}

    rows: list[list[str]] = []
    for session in reversed(all_sessions):
        meta = read_json(session.metadata) or {}
        state = read_json(session.state) or {}
        started = parse_timestamp(meta.get("started_at"))
        ended = parse_timestamp(meta.get("ended_at"))
        alive = _pid_alive(int(state.get("pid") or 0)) and _is_our_daemon(
            int(state.get("pid") or 0), session.root
        )
        if alive:
            span = (utc_now() - started).total_seconds() if started else None
            status = c("采集中", C_OK)
        else:
            span = (ended - started).total_seconds() if started and ended else None
            status = "已结束" if ended else c("未完成", C_WARN)
        counts = state.get("counts") or meta.get("counts") or {}
        label = status
        if session.name not in current_names and session.name in legacy_names:
            label = f"{status} {c('(旧目录)', C_DIM)}"
        # start 没写名称时，name 就是时间戳本身，再列一遍没有信息量；
        # 这时把 HUD 进程填进「游戏」列，那场实验才认得出是哪次。
        display = meta.get("name") or ""
        if not display or display == session.name:
            display = "-"
        game_meta = meta.get("game") or {}
        game = game_meta.get("process")
        if not game:
            seen = game_meta.get("hud_seen_processes") or {}
            game = next(iter(sorted(seen, key=seen.get, reverse=True)), None)
        rows.append(
            [
                session.name,
                display[:24],
                (game or "-")[:22],
                fmt_local(started, "%m-%d %H:%M"),
                fmt_duration(span),
                str(counts.get("macmon_samples", "-")),
                str(counts.get("thermal_samples", "-")),
                label,
            ]
        )

    print(_table(
        ["会话", "名称", "游戏", "开始", "时长", "macmon", "thermal", "状态"], rows
    ))
    print()
    print(f"数据目录：{paths.recordings_dir}")
    if legacy_sessions:
        print(
            warn(f"仍有 {len(legacy_sessions)} 个实验留在旧目录：{roots[-1]}")
        )
        print(f"  执行 {c('mtl migrate', C_BOLD)} 可迁移到新位置（会先复制校验，再清理旧目录）")
    return 0


def _probe_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".mtl_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def cmd_doctor(args) -> int:
    print(heading("环境检查"))
    checks: list[tuple[str, bool, str]] = []

    exe = find_macmon()
    version = (macmon_version() or "").replace("macmon ", "").strip()
    checks.append(("macmon", bool(exe), exe or "未安装：brew install macmon"))
    checks.append(("macmon 版本", bool(exe), version or "-"))
    checks.append(("powermetrics", bool(shutil.which("powermetrics")), shutil.which("powermetrics") or "-"))
    checks.append(("Python", True, sys.version.split()[0]))
    try:
        import plotly

        checks.append(("plotly", True, plotly.__version__))
    except ImportError:
        checks.append(("plotly", False, "未安装"))

    nopass = sudo_nopasswd() if sys.platform == "darwin" else False
    checks.append(("sudo 免密", nopass, "是" if nopass else "否（mtl start 时会询问密码）"))

    paths = _app_paths(args)
    for label, path in (
        ("数据目录", paths.recordings_dir),
        ("状态目录", paths.state_dir),
        ("运行期目录", paths.runtime_dir),
    ):
        checks.append((f"{label}可写", _probe_writable(path), str(path)))

    checks.append(
        (
            "配置文件",
            paths.config_file.exists(),
            str(paths.config_file) if paths.config_file.exists() else "未创建（用 mtl paths --init-config）",
        )
    )
    if paths.config_error:
        checks.append(("配置解析", False, paths.config_error))

    for name, passed, detail in checks:
        mark = c("✓", C_OK) if passed else c("✗", C_ERR)
        print(f"  {mark} {name:<14}{detail}")

    active = _active_session(_records_roots(paths))
    print()
    print(heading("当前实验"))
    print(f"  {active.name if active else '无'}")

    if args.thermal:
        print()
        print(heading("Thermal Pressure 实测"))
        result = thermal_selftest(interval_ms=_parse_interval(args.interval))
        if not result.get("ok"):
            print(err(f"失败：{result.get('error') or '未解析到样本'}"))
            raw = (result.get("raw_stdout") or "").strip()
            if raw:
                print(c("原始输出（前 40 行）：", C_DIM))
                print("\n".join(raw.splitlines()[:40]))
            if result.get("stderr"):
                print(c(result["stderr"], C_DIM))
            return 1
        for sample in result["samples"]:
            print(f"  {sample['ts']}  {sample['level']}  (来源: {sample['ts_source']})")
        print(ok(f"解析到 {len(result['samples'])} 条样本"))

    if getattr(args, "game", False):
        print()
        print(heading("帧率（Metal HUD）实测"))
        hud = fps_mod.hud_selftest(seconds=3.0)
        before = hud["env_before"].get("MTL_HUD_ENABLED")
        print(f"  环境变量   测试前{'已开启' if before else '未开启'}，测试后已还原")
        if not hud["can_set"]:
            print(err("launchctl 无法设置环境变量：" + "/".join(hud["failed_keys"])))
            print(c("  需要在图形登录会话的终端里运行；沙箱或远程会话里会被拒绝", C_DIM))
            return 1
        print(ok("launchctl 环境变量可设置"))
        print(f"  log stream 3s 内收到 {hud['raw_lines']} 行")
        if hud["error"]:
            print(err(hud["error"]))
        if hud.get("sample_count"):
            for sample in hud["samples"]:
                ts = parse_timestamp(sample.get("timestamp"))
                print(f"  {fmt_local(ts, '%H:%M:%S')}  {sample.get('fps')} FPS  "
                      f"{sample.get('process') or '-'}")
            print(ok(f"解析到 {hud['sample_count']} 条帧率样本"))
        else:
            print(warn("没有捕获到 metal-HUD 日志行"))
            seen = (hud.get("status") or {}).get("seen") or {}
            if seen:
                print(f"  收到过 HUD 行的进程：{_describe_counts(seen)}")
                print(c("  有 HUD 但算不出帧率，通常是只有一帧或帧号没变", C_DIM))
            else:
                print(c("  如果此刻没有游戏在跑，这是正常的。", C_DIM))
                print(c("  先 mtl start 名称 --game，再启动游戏，然后重跑本命令。", C_DIM))
        if hud.get("stderr"):
            print(c("  log stream: " + hud["stderr"], C_DIM))
    return 0


def cmd_paths(args) -> int:
    paths = _app_paths(args)
    if args.init_config:
        created = write_config_template(paths.config_file)
        if created:
            print(ok(f"已写入配置模板：{paths.config_file}"))
        else:
            print(warn(f"配置已存在，未改动：{paths.config_file}"))
        print()

    print(heading("目录布局"))
    rows = list(paths.describe()) + [
        ("配置文件", paths.config_file),
        ("会话状态", paths.sessions_state_dir),
    ]
    for label, path in rows:
        note = "" if path.exists() else c("（未创建）", C_DIM)
        print(f"  {_pad(label, 10)}{path} {note}".rstrip())
    if paths.config_error:
        print(warn(paths.config_error))

    legacy = legacy_recordings()
    if legacy is not None:
        count = len(list_sessions(legacy))
        if count:
            print()
            print(warn(f"旧数据目录仍有 {count} 个实验：{legacy}"))
            print(f"  执行 {c('mtl migrate', C_BOLD)} 迁移，或用 "
                  f"{c('mtl migrate --dry-run', C_BOLD)} 先看会做什么")
    return 0


def cmd_recover_fps(args) -> int:
    """从系统日志历史里补回某场实验的帧率（录制时过滤词写错的救援通道）。"""
    paths = _app_paths(args)
    try:
        session = resolve_session_multi(_records_roots(paths), args.session)
    except LookupError as exc:
        print(err(str(exc)))
        return 1

    meta = read_json(session.metadata) or {}
    started = parse_timestamp(meta.get("started_at"))
    ended = parse_timestamp(meta.get("ended_at")) or utc_now()
    if started is None:
        print(err("metadata 里没有 started_at，无法确定要从哪段时间捞日志"))
        return 1

    game = meta.get("game") or {}
    process = (args.process or game.get("process") or game.get("filter") or "").strip() or None

    existing = 0
    if session.fps_samples.exists():
        existing = sum(1 for line in session.fps_samples.read_text(encoding="utf-8").splitlines() if line.strip())
    if existing and not args.force:
        print(err(f"该实验已有 {existing} 条帧率样本；确实要覆盖就加 --force"))
        return 1

    print(heading("从系统日志补回帧率"))
    print(f"  会话      {session.name}")
    print(f"  时间窗    {fmt_local(started, '%Y-%m-%d %H:%M:%S')} → "
          f"{fmt_local(ended, '%Y-%m-%d %H:%M:%S')}")
    print(f"  过滤词    {process or '自动识别'}")
    print(c("  正在翻 log show 历史，40 分钟的窗口通常要几秒到十几秒……", C_DIM))

    result = fps_mod.recover_from_log(started, ended, process)
    if result["error"]:
        print(err(result["error"]))
        return 1
    samples = result["samples"]
    status = result.get("status") or {}
    if not samples:
        print(warn(f"这段时间里没有解析出帧率样本（读到 {result['raw_lines']} 行日志）"))
        seen = status.get("seen") or {}
        if seen:
            print(f"  有 HUD 日志的进程：{_describe_counts(seen)}")
        else:
            print(c("  系统日志只保留有限天数；当时没开 HUD 日志、或游戏不在 Metal 上呈现，也会是空的", C_DIM))
        return 1

    session.fps_samples.write_text(
        "".join(json.dumps(sample, ensure_ascii=False) + "\n" for sample in samples),
        encoding="utf-8",
    )
    if status.get("adopted_from"):
        process = status.get("filter")
        game["filter_fallback"] = {
            "requested": status.get("requested_filter"),
            "used": status.get("filter"),
        }
    game["process"] = process or game.get("process")
    game["hud_seen_processes"] = dict(status.get("seen") or {})
    game["recovered_from_log"] = {
        "samples": len(samples),
        "raw_lines": result["raw_lines"],
    }
    meta["game"] = game
    write_json(session.metadata, meta)

    fps_summary = fps_mod.summarize(samples)
    print(ok(f"补回 {len(samples)} 条帧率样本"
             f"（{fps_summary.get('processes') and '、'.join(fps_summary['processes'])}）"))
    print(f"  平均 {_fmt_num(fps_summary.get('fps_avg'), 1)} FPS · "
          f"1% low ≈ {_fmt_num(fps_summary.get('fps_1pct_low'), 1)} · "
          f"最低 {_fmt_num(fps_summary.get('fps_min'), 1)}")
    path = generate_report(session)
    print(ok(f"报告已重新生成：{path}"))
    return 0


def _fmt_num(value: Any, digits: int) -> str:
    return "-" if value is None else f"{float(value):.{digits}f}"


def cmd_migrate(args) -> int:
    paths = _app_paths(args)
    source = Path(args.source).expanduser() if args.source else legacy_recordings()
    if source is None:
        print(ok(f"没有需要迁移的旧目录，数据已在 {paths.recordings_dir}"))
        return 0
    if not source.exists():
        print(err(f"旧目录不存在：{source}"))
        return 1

    target = paths.recordings_dir
    print(heading("迁移旧实验数据"))
    print(f"  旧目录  {source}")
    print(f"  新目录  {target}")
    if args.dry_run:
        print(c("  （预演模式，不会改动任何文件）", C_DIM))
    print()

    report = run_migration(
        source, target, dry_run=args.dry_run, progress=lambda msg: print(f"  {msg}")
    )

    if not report.plans:
        print(warn("旧目录里没有可识别的实验（缺 metadata.json）"))
        return 0

    print()
    print(f"  迁移  {report.count('done')} 个"
          + (f" · 预演 {report.count('dry')} 个" if args.dry_run else "")
          + f" · 跳过 {report.count('conflict')} 个"
          + f" · 失败 {report.count('failed')} 个")
    if report.source_removed:
        print(ok("旧目录已清理"))
    else:
        print(c(report.note or "旧目录保留（仍有未迁移内容）", C_DIM))
    for message in report.errors:
        print(warn(message))
    if args.dry_run:
        print()
        print(f"  确认无误后执行 {c('mtl migrate', C_BOLD)}")
    return 0 if report.ok or args.dry_run else 1


def cmd_collect(args) -> int:
    return run_collector(
        Path(args.session_dir),
        interval_ms=args.interval_ms,
        thermal_enabled=not args.no_thermal,
        password_fd=args.password_fd,
        soc_info=not args.no_soc_info,
        game_filter=getattr(args, "game_filter", None),
    )


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mtl",
        description="Mac Thermal Lab — macOS Apple Silicon 热状态与功耗实验记录",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""常用流程:
  mtl start 游戏测试       开始采集
  mtl mark "桌面风扇开启"   打点
  mtl stop                 结束并生成报告
  mtl report --open        重新生成并打开报告

记帧率:
  mtl start 名称 --game      同时记录游戏 FPS（Metal HUD）
  mtl doctor --game          实测帧率采集链路
  mtl recover-fps 会话       从系统日志历史补回帧率（过滤词写错时救场）

数据位置:
  mtl paths                查看配置/数据/状态目录（XDG）
  mtl migrate              把旧目录里的实验迁到新位置
""",
    )
    parser.add_argument("--version", action="version", version=f"mtl {__version__}")
    parser.add_argument(
        "--data-dir",
        default=None,
        help="临时覆盖实验数据目录（默认 $XDG_DATA_HOME/mac-thermal-lab/recordings）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="开始新实验并启动采集")
    p.add_argument("name", nargs="?", help="实验名称（= 目录名后缀，如 oot；留空则只有时间戳）")
    p.add_argument("-i", "--interval", default="2000",
                   help="采样间隔，默认 2000（毫秒），也可写 2s / 500ms")
    p.add_argument("--no-thermal", action="store_true", help="不采集 Thermal Pressure（免 sudo）")
    p.add_argument("--no-soc-info", action="store_true", help="不在每行附带 SoC 信息")
    p.add_argument(
        "--game", nargs="?", const="", default=None, metavar="进程名",
        help="记录游戏帧率（Metal HUD）。留空自动识别；也可指定进程名（不是实验名），"
             "如 --game OcarinaOfTime",
    )
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("stop", help="停止实验、整理数据并生成报告")
    p.add_argument("session", nargs="?", help="实验目录名（默认当前进行中的）")
    p.add_argument("--open", action="store_true", help="结束后自动打开报告")
    p.add_argument("--force", action="store_true", help="允许再次收尾一个已经结束的实验")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("status", help="查看当前采集状态")
    p.add_argument("session", nargs="?", help="指定实验（默认当前或最近一次）")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("mark", help="在时间轴上添加事件")
    p.add_argument("event", nargs="+", help="事件名，中间有空格也不用加引号")
    p.add_argument("-n", "--note", default="", help="可选备注")
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("report", help="重新生成 HTML 报告")
    p.add_argument("session", nargs="?", help="实验目录名（默认最近一次）")
    p.add_argument("--open", action="store_true", help="生成后自动打开")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("recover-fps", help="从系统日志历史里补回某场实验的帧率")
    p.add_argument("session", nargs="?", help="实验目录名（默认最近一次）")
    p.add_argument("--process", help="HUD 进程名；默认用 metadata 里记录的")
    p.add_argument("--force", action="store_true", help="已有帧率样本时也覆盖")
    p.set_defaults(func=cmd_recover_fps)

    p = sub.add_parser("list", help="列出历史实验")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("doctor", help="环境自检")
    p.add_argument("--thermal", action="store_true", help="实跑一次 powermetrics 校验解析")
    p.add_argument("--game", action="store_true", help="实测 Metal HUD 帧率链路（会临时开关环境变量并还原）")
    p.add_argument("--interval", default="1000", help="自检采样间隔")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("paths", help="显示 XDG 目录布局")
    p.add_argument("--init-config", action="store_true", help="写入带注释的 config.toml 模板")
    p.set_defaults(func=cmd_paths)

    p = sub.add_parser("migrate", help="把旧目录里的实验迁移到 XDG 数据目录")
    p.add_argument("--from", dest="source", default=None, help="旧目录（默认源码目录下的 recordings/）")
    p.add_argument("--dry-run", action="store_true", help="只显示会做什么，不改动文件")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("_collect")
    p.add_argument("--session-dir", required=True)
    p.add_argument("--interval-ms", type=int, default=2000)
    p.add_argument("--no-thermal", action="store_true")
    p.add_argument("--no-soc-info", action="store_true")
    p.add_argument("--password-fd", type=int, default=None, help=argparse.SUPPRESS)
    p.add_argument("--game-filter", default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_collect)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        print("\n已取消")
        return 130
    except LookupError as exc:
        print(err(str(exc)))
        return 1
    except Exception as exc:  # noqa: BLE001
        if os.environ.get("MTL_DEBUG"):
            raise
        print(err(f"{type(exc).__name__}: {exc}"))
        print(c("设置 MTL_DEBUG=1 可查看完整堆栈", C_DIM))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
