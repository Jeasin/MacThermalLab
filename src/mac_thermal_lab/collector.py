"""采集守护进程：同时驱动 macmon 与 powermetrics，落盘原始数据。

关键设计：
- 每个子进程都在独立 session/pgid 中启动，停止时只 kill 自己的进程组，
  绝不会误伤用户其它 powermetrics / macmon 监控进程。
- 原始输出逐行 flush 落盘；解析失败也照样保留原文。
- 两个数据源互相独立：一路挂掉不影响另一路。
- 收到 SIGTERM/SIGINT 后先停子进程、再冲盘、最后写 metadata。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import fps as fps_mod
from . import macmon as macmon_mod
from .powermetrics import ThermalParser, build_command as build_powermetrics_command
from .storage import (
    SessionPaths,
    UTC,
    acquire_lock,
    append_jsonl,
    iso_utc,
    read_json,
    utc_now,
    write_json,
)

THERMAL_SAMPLES_FILE = "powermetrics.samples.jsonl"
MAX_MACMON_RESTARTS = 3


def _short_thermal_reason(tail: list[str], returncode: int | None) -> str:
    """把 sudo / powermetrics 的原始报错压缩成一句人能看懂的失败原因。"""
    blob = " ".join(tail).lower()
    # 先判「密码被拒」：这时 sudo 随后还会补一句 no password was provided，
    # 那是重试时读到 EOF 的连带输出，不是真正的原因
    if "sorry, try again" in blob or "incorrect password" in blob:
        return "sudo 密码错误（powermetrics 未启动）"
    if "a password is required" in blob or "no password was provided" in blob:
        return "sudo 未授权：powermetrics 需要管理员密码"
    if "operation not permitted" in blob:
        return "sudo 被系统拒绝，无法执行 powermetrics"
    if "not found" in blob:
        return "未找到 powermetrics 命令"
    text = tail[-1][:120] if tail else f"退出码 {returncode}"
    return f"powermetrics 退出：{text}"


def _short_hud_reason(tail: list[str]) -> str:
    """把 log stream 的报错压成一句人话。"""
    blob = " ".join(tail).lower()
    if "sandbox" in blob:
        return "log 命令被沙箱限制，无法读取系统日志"
    if "not permitted" in blob or "operation not permitted" in blob:
        return "没有读取系统日志的权限"
    if "predicate" in blob:
        return "log stream 的过滤条件被拒绝"
    text = tail[-1][:120] if tail else "未知错误"
    return f"log stream 退出：{text}"


class LineWriter:
    """带 flush 的追加写入器；出错只记一次，不中断采集。"""

    def __init__(self, path: Path, logger: "Logger") -> None:
        self.path = path
        self.logger = logger
        self.fh = path.open("a", encoding="utf-8", errors="replace", buffering=1)
        self._failed = False

    def write(self, line: str) -> None:
        if self._failed:
            return
        try:
            if not line.endswith("\n"):
                line += "\n"
            self.fh.write(line)
            self.fh.flush()
        except OSError as exc:
            self._failed = True
            self.logger.log(f"写入 {self.path.name} 失败：{exc}")

    def close(self) -> None:
        try:
            self.fh.flush()
            self.fh.close()
        except OSError:
            pass


class Logger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.fh = path.open("a", encoding="utf-8", errors="replace", buffering=1)

    def log(self, message: str) -> None:
        stamp = datetime.now(UTC).astimezone().strftime("%H:%M:%S")
        with self._lock:
            try:
                self.fh.write(f"[{stamp}] {message}\n")
                self.fh.flush()
            except OSError:
                pass

    def close(self) -> None:
        try:
            self.fh.close()
        except OSError:
            pass


class Collector:
    def __init__(
        self,
        paths: SessionPaths,
        interval_ms: int = 2000,
        thermal_enabled: bool = True,
        password_fd: int | None = None,
        soc_info_flag: bool = True,
        game_filter: str | None = None,
    ) -> None:
        self.paths = paths
        self.interval_ms = interval_ms
        self.thermal_enabled = thermal_enabled
        self.password_fd = password_fd
        self.soc_info_flag = soc_info_flag
        # None 表示不采帧率；空串表示自动识别是哪个进程的 HUD
        self.game_enabled = game_filter is not None
        self.game_filter = (game_filter or "").strip() or None

        self.stop_event = threading.Event()
        # state / runtime 分属 XDG 的另外两个目录，先建好再开日志与锁
        paths.ensure_runtime_dirs()
        self.logger = Logger(paths.daemon_log)
        self._lock = threading.Lock()
        self.counts = {
            "macmon_samples": 0,
            "macmon_bad_lines": 0,
            "thermal_samples": 0,
            "fps_samples": 0,
        }
        self.children: dict[str, dict[str, Any]] = {}
        self.errors: list[str] = []
        self.started_at = utc_now()
        self.last_sample_at: dict[str, str | None] = {
            "macmon": None, "powermetrics": None, "hud": None,
        }
        self.macmon_restarts = 0
        self.thermal_error: str | None = None
        self.game_error: str | None = None
        self._game_process: str | None = None
        self._soc_captured = False
        self._stop_reason = "unknown"
        self._current_macmon: subprocess.Popen | None = None
        self._current_thermal: subprocess.Popen | None = None
        self._current_hud: subprocess.Popen | None = None
        self._hud_parser = fps_mod.HudStreamParser(self.game_filter)
        self._lock_file = None

    # ------------------------------------------------------------------ #
    # 启动
    # ------------------------------------------------------------------ #
    def run(self) -> int:
        self._install_signals()
        self._lock_file = acquire_lock(self.paths.lock)
        if self._lock_file is None:
            self.logger.log("会话锁已被占用，可能已有采集进程在运行，退出")
            return 2
        self._write_pid_file()
        self.logger.log(
            f"collector 启动 pid={os.getpid()} interval={self.interval_ms}ms "
            f"data={self.paths.root} state={self.paths.state_dir}"
        )
        self._write_state(status="running")

        macmon_proc = self._spawn_macmon()
        self._current_macmon = macmon_proc
        if macmon_proc is None:
            self._record_error("macmon 启动失败，本次实验没有硬件指标")
        thermal_proc = self._spawn_powermetrics() if self.thermal_enabled else None
        self._current_thermal = thermal_proc
        hud_proc = self._spawn_hud() if self.game_enabled else None
        self._current_hud = hud_proc

        readers: list[threading.Thread] = []
        if macmon_proc is not None:
            readers.append(self._start_reader("macmon", macmon_proc))

        thermal_writer = None
        thermal_sidecar = None
        if thermal_proc is not None:
            thermal_writer = LineWriter(self.paths.powermetrics_raw, self.logger)
            thermal_sidecar = self.paths.path(THERMAL_SAMPLES_FILE).open(
                "a", encoding="utf-8", buffering=1
            )
            readers.append(
                self._start_thermal_reader(thermal_proc, thermal_writer, thermal_sidecar)
            )
            readers.append(self._start_stderr_reader("powermetrics.err", thermal_proc))

        hud_writer = None
        fps_sidecar = None
        if hud_proc is not None:
            hud_writer = LineWriter(self.paths.hud_raw, self.logger)
            fps_sidecar = self.paths.fps_samples.open("a", encoding="utf-8", buffering=1)
            readers.append(self._start_hud_reader(hud_proc, hud_writer, fps_sidecar))
            readers.append(self._start_hud_stderr_reader(hud_proc))

        try:
            while not self.stop_event.wait(1.0):
                self._supervise()
                self._write_state(status="running")
        except KeyboardInterrupt:
            self._stop_reason = "interrupted"
        finally:
            self._shutdown(thermal_writer, thermal_sidecar, readers, hud_writer, fps_sidecar)

        return 0

    def _install_signals(self) -> None:
        def handler(signum, _frame):
            self._stop_reason = signal.Signals(signum).name
            self.stop_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass

    # ------------------------------------------------------------------ #
    # 子进程
    # ------------------------------------------------------------------ #
    def _spawn_macmon(self) -> subprocess.Popen | None:
        if not macmon_mod.find_macmon():
            self._record_error("未找到 macmon，请先安装（brew install macmon）")
            return None
        cmd = macmon_mod.build_command(self.interval_ms, self.soc_info_flag)
        proc = self._popen(cmd, name="macmon")
        return proc

    def _spawn_powermetrics(self) -> subprocess.Popen | None:
        if not shutil.which("powermetrics"):
            self._record_error("未找到 powermetrics")
            self._mark_thermal_error("未找到 powermetrics 命令")
            return None
        use_stdin = self.password_fd is not None
        cmd = build_powermetrics_command(self.interval_ms, use_stdin)
        self.logger.log(f"启动 powermetrics：{' '.join(cmd)}")
        proc = self._popen(cmd, name="powermetrics", stdin_pipe=use_stdin)
        if proc is None:
            self._mark_thermal_error("powermetrics 进程无法启动（sudo 被拒绝或无执行权限）")
            return None
        if use_stdin and proc.stdin is not None:
            try:
                password = self._read_password()
                proc.stdin.write(password + "\n")
                proc.stdin.flush()
                password = ""
                proc.stdin.close()  # 密码错时让 sudo 立刻失败，而不是挂住
            except (OSError, ValueError) as exc:
                self._record_error(f"向 sudo 传递密码失败：{exc}")
                self._mark_thermal_error("向 sudo 传递密码失败")
        return proc

    def _mark_thermal_error(self, message: str) -> None:
        """把 Thermal 失败原因写进 metadata，报告和 CLI 都能直接说明为什么缺数据。"""
        if self.thermal_error is not None:
            return
        self.thermal_error = message
        meta = read_json(self.paths.metadata) or {}
        thermal = meta.get("thermal")
        if not isinstance(thermal, dict):
            thermal = {}
            meta["thermal"] = thermal
        thermal["error"] = message
        try:
            write_json(self.paths.metadata, meta)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # 帧率（Metal HUD）
    # ------------------------------------------------------------------ #
    def _spawn_hud(self) -> subprocess.Popen | None:
        if not os.path.exists(fps_mod.LOG_BIN):
            self._record_error("未找到 /usr/bin/log，无法读取 Metal HUD 的帧率日志")
            self._mark_game_error("系统缺少 log 命令")
            return None
        cmd = fps_mod.build_log_stream_command(self.game_filter)
        self.logger.log(f"启动 HUD 帧率采集：{' '.join(cmd)}")
        proc = self._popen(cmd, name="hud")
        if proc is None:
            self._mark_game_error("log stream 无法启动")
        return proc

    def _mark_game_error(self, message: str) -> None:
        if self.game_error is not None:
            return
        self.game_error = message
        meta = read_json(self.paths.metadata) or {}
        game = meta.get("game")
        if not isinstance(game, dict):
            game = {}
            meta["game"] = game
        game["error"] = message
        try:
            write_json(self.paths.metadata, meta)
        except OSError:
            pass

    def _capture_game_process(self, process: str | None) -> None:
        """记下第一个出现 HUD 的进程名，报告里用来标注是哪款游戏。"""
        if not process or process == self._game_process:
            return
        self._game_process = process
        meta = read_json(self.paths.metadata) or {}
        game = meta.get("game")
        if not isinstance(game, dict):
            game = {}
            meta["game"] = game
        if not game.get("process"):
            game["process"] = process
            try:
                write_json(self.paths.metadata, meta)
            except OSError:
                pass

    def _start_hud_reader(
        self, proc: subprocess.Popen, writer: LineWriter, sidecar
    ) -> threading.Thread:
        thread = threading.Thread(
            target=self._read_hud,
            args=(proc, writer, sidecar),
            name="hud-reader",
            daemon=True,
        )
        thread.start()
        return thread

    def _read_hud(self, proc: subprocess.Popen, writer: LineWriter, sidecar) -> None:
        try:
            if proc.stdout is None:
                return
            for line in proc.stdout:
                if not line.strip():
                    continue
                writer.write(line)          # 原始 ndjson 一律保留
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                sample = self._hud_parser.feed(record)
                if sample is None:
                    continue
                try:
                    append_jsonl(sidecar, sample)
                except OSError as exc:
                    self._record_error(f"写入帧率解析缓存失败：{exc}")
                with self._lock:
                    self.counts["fps_samples"] += 1
                    self.last_sample_at["hud"] = sample["timestamp"]
                self._capture_game_process(sample.get("process"))
        finally:
            writer.close()
            try:
                sidecar.close()
            except OSError:
                pass

    def _start_hud_stderr_reader(self, proc: subprocess.Popen) -> threading.Thread:
        def run() -> None:
            if proc.stderr is None:
                return
            writer = LineWriter(self.paths.hud_err, self.logger)
            tail: list[str] = []
            try:
                for line in proc.stderr:
                    writer.write(line)
                    if line.strip():
                        tail = (tail + [line.strip()])[-6:]
            finally:
                writer.close()
                if proc.poll() not in (0, None) and tail:
                    self._record_error("HUD 采集报错：" + " | ".join(tail[-3:]))
                    self._mark_game_error(_short_hud_reason(tail))

        thread = threading.Thread(target=run, name="hud-stderr", daemon=True)
        thread.start()
        return thread

    def _read_password(self) -> str:
        if self.password_fd is None:
            return ""
        try:
            data = os.read(self.password_fd, 4096)
        finally:
            try:
                os.close(self.password_fd)
            except OSError:
                pass
            self.password_fd = None
        return data.decode("utf-8", errors="replace").strip()

    def _popen(
        self, cmd: list[str], name: str, stdin_pipe: bool = False
    ) -> subprocess.Popen | None:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE if stdin_pipe else subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,  # 独立进程组，停止时只动自己
            )
        except OSError as exc:
            self._record_error(f"{name} 启动失败：{exc}")
            return None
        with self._lock:
            self.children[name] = {
                "pid": proc.pid,
                "pgid": proc.pid,
                "alive": True,
                "started_at": iso_utc(utc_now()),
                "restarts": self.children.get(name, {}).get("restarts", 0),
            }
        self.logger.log(f"{name} 已启动 pid={proc.pid}")
        return proc

    # ------------------------------------------------------------------ #
    # 读线程
    # ------------------------------------------------------------------ #
    def _start_reader(self, name: str, proc: subprocess.Popen) -> threading.Thread:
        writer = LineWriter(self.paths.macmon_raw, self.logger)
        thread = threading.Thread(
            target=self._read_macmon, args=(proc, writer), name=f"{name}-reader", daemon=True
        )
        thread.start()
        return thread

    def _read_macmon(self, proc: subprocess.Popen, writer: LineWriter) -> None:
        try:
            if proc.stdout is None:
                return
            for line in proc.stdout:
                if not line.strip():
                    continue
                writer.write(line)
                sample = macmon_mod.parse_line(line)
                with self._lock:
                    if sample is None:
                        self.counts["macmon_bad_lines"] += 1
                    else:
                        self.counts["macmon_samples"] += 1
                        self.last_sample_at["macmon"] = iso_utc(sample.ts)
                if sample is not None and not self._soc_captured:
                    soc = macmon_mod.soc_info(sample)
                    if soc:
                        self._capture_soc(soc)
        finally:
            writer.close()

    def _start_thermal_reader(
        self, proc: subprocess.Popen, writer: LineWriter, sidecar
    ) -> threading.Thread:
        thread = threading.Thread(
            target=self._read_thermal,
            args=(proc, writer, sidecar),
            name="powermetrics-reader",
            daemon=True,
        )
        thread.start()
        return thread

    def _read_thermal(self, proc: subprocess.Popen, writer: LineWriter, sidecar) -> None:
        parser = ThermalParser()
        try:
            if proc.stdout is None:
                return
            for line in proc.stdout:
                arrival = utc_now()  # 该行到达本机的时刻，仅在头部时间戳缺失时兜底
                writer.write(line)
                sample = parser.feed(line, arrival)
                if sample is None:
                    continue
                try:
                    append_jsonl(sidecar, sample.to_record())
                except OSError as exc:
                    self._record_error(f"写入 thermal 解析缓存失败：{exc}")
                with self._lock:
                    self.counts["thermal_samples"] += 1
                    if sample.ts:
                        self.last_sample_at["powermetrics"] = iso_utc(sample.ts)
        finally:
            writer.close()
            try:
                sidecar.close()
            except OSError:
                pass

    def _start_stderr_reader(self, label: str, proc: subprocess.Popen) -> threading.Thread:
        path = self.paths.powermetrics_err

        def run() -> None:
            if proc.stderr is None:
                return
            writer = LineWriter(path, self.logger)
            tail: list[str] = []
            try:
                for line in proc.stderr:
                    writer.write(line)
                    if line.strip():
                        tail = (tail + [line.strip()])[-6:]
            finally:
                writer.close()
                if proc.poll() not in (0, None) and tail:
                    detail = " | ".join(tail[-3:])
                    self._record_error("powermetrics 报错：" + detail)
                    self._mark_thermal_error(_short_thermal_reason(tail, proc.returncode))

        thread = threading.Thread(target=run, name=f"{label}-reader", daemon=True)
        thread.start()
        return thread

    # ------------------------------------------------------------------ #
    # 监管
    # ------------------------------------------------------------------ #
    def _supervise(self) -> None:
        # macmon 崩溃自愈
        proc = self._current_macmon
        if proc is not None and proc.poll() is not None:
            with self._lock:
                if "macmon" in self.children:
                    self.children["macmon"]["alive"] = False
            if self.macmon_restarts < MAX_MACMON_RESTARTS:
                self.macmon_restarts += 1
                self.logger.log(f"macmon 退出（code={proc.returncode}），第 {self.macmon_restarts} 次重启")
                self._record_error(f"macmon 意外退出 code={proc.returncode}，已尝试重启")
                time.sleep(1.0)
                new_proc = self._spawn_macmon()
                if new_proc is not None:
                    self._current_macmon = new_proc
                    with self._lock:
                        self.children["macmon"]["restarts"] = self.macmon_restarts
                    self._start_reader("macmon", new_proc)
            else:
                self.logger.log("macmon 重启次数超限，停止重启")

        thermal = self.children.get("powermetrics")
        if thermal and thermal.get("alive"):
            proc = self._current_thermal
            if proc is not None and proc.poll() is not None:
                with self._lock:
                    thermal["alive"] = False
                self.logger.log(f"powermetrics 退出 code={proc.returncode}")
                if proc.returncode not in (0, None):
                    self._record_error(
                        f"powermetrics 已退出（code={proc.returncode}），Thermal Pressure 不再更新"
                    )
                    self._mark_thermal_error(
                        f"powermetrics 中途退出（code={proc.returncode}）"
                    )

        hud = self.children.get("hud")
        if hud and hud.get("alive"):
            proc = self._current_hud
            if proc is not None and proc.poll() is not None:
                with self._lock:
                    hud["alive"] = False
                self.logger.log(f"hud 采集退出 code={proc.returncode}")
                self._record_error(
                    f"log stream 已退出（code={proc.returncode}），帧率不再更新"
                )

    def _capture_soc(self, soc: dict[str, Any]) -> None:
        self._soc_captured = True
        meta = read_json(self.paths.metadata) or {}
        meta["soc"] = soc
        try:
            write_json(self.paths.metadata, meta)
        except OSError:
            pass

    def _record_error(self, message: str) -> None:
        with self._lock:
            if message not in self.errors:
                self.errors.append(message)
        self.logger.log("ERROR " + message)
        self._write_state(status="running")

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def _write_state(self, status: str) -> None:
        with self._lock:
            payload = {
                "schema": 1,
                "status": status,
                "pid": os.getpid(),
                "pgid": os.getpgid(0),
                "started_at": iso_utc(self.started_at),
                "heartbeat_at": iso_utc(utc_now()),
                "interval_ms": self.interval_ms,
                "thermal_enabled": self.thermal_enabled,
                "game_enabled": self.game_enabled,
                "game_process": self._game_process,
                "counts": dict(self.counts),
                "last_sample_at": dict(self.last_sample_at),
                "children": {k: dict(v) for k, v in self.children.items()},
                "errors": list(self.errors),
                "macmon_restarts": self.macmon_restarts,
            }
        try:
            write_json(self.paths.state, payload)
        except OSError as exc:
            self.logger.log(f"写 state.json 失败：{exc}")

    # ------------------------------------------------------------------ #
    # 收尾
    # ------------------------------------------------------------------ #
    def _shutdown(
        self, thermal_writer, thermal_sidecar, readers, hud_writer=None, fps_sidecar=None
    ) -> None:
        self.logger.log(f"开始收尾（{self._stop_reason}）")
        procs: list[tuple[str, subprocess.Popen]] = []
        if self._current_macmon is not None:
            procs.append(("macmon", self._current_macmon))
        if self._current_thermal is not None:
            procs.append(("powermetrics", self._current_thermal))
        if self._current_hud is not None:
            procs.append(("hud", self._current_hud))

        for name, proc in procs:
            self._terminate(name, proc)

        for thread in readers:
            thread.join(timeout=3.0)

        for closer in (thermal_writer, thermal_sidecar, hud_writer, fps_sidecar):
            if closer is not None:
                try:
                    closer.close()
                except OSError:
                    pass

        ended_at = utc_now()
        meta = read_json(self.paths.metadata) or {}
        meta["ended_at"] = iso_utc(ended_at)
        meta["status"] = "stopped" if self._stop_reason != "unknown" else "aborted"
        meta["stop_reason"] = self._stop_reason
        meta["counts"] = dict(self.counts)
        if self.errors:
            existing = meta.get("errors") or []
            meta["errors"] = list(dict.fromkeys([*existing, *self.errors]))
        try:
            write_json(self.paths.metadata, meta)
        except OSError as exc:
            self.logger.log(f"写 metadata 失败：{exc}")

        self._write_state(status="stopped")
        self.logger.log(
            f"收尾完成：macmon={self.counts['macmon_samples']} "
            f"thermal={self.counts['thermal_samples']} fps={self.counts['fps_samples']}"
        )
        self._remove_pid_file()
        if self._lock_file is not None:
            try:
                self._lock_file.close()
            except OSError:
                pass
            # 锁文件本身没有内容价值，收尾时删掉，别在 runtime 里攒垃圾
            try:
                self.paths.lock.unlink(missing_ok=True)
            except OSError:
                pass
        self.logger.close()

    def _write_pid_file(self) -> None:
        try:
            self.paths.pid_file.write_text(str(os.getpid()), encoding="utf-8")
        except OSError as exc:
            self.logger.log(f"写 pid 文件失败：{exc}")

    def _remove_pid_file(self) -> None:
        try:
            self.paths.pid_file.unlink(missing_ok=True)
        except OSError:
            pass

    def _terminate(self, name: str, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            self.logger.log(f"停止 {name} pid={proc.pid}")
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.terminate()
                except OSError:
                    pass
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.logger.log(f"{name} 未响应 SIGTERM，强制结束")
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    try:
                        proc.kill()
                    except OSError:
                        pass
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.logger.log(f"{name} 仍未退出")
        with self._lock:
            if name in self.children:
                self.children[name]["alive"] = False


def run_collector(
    session_dir: Path,
    interval_ms: int = 2000,
    thermal_enabled: bool = True,
    password_fd: int | None = None,
    soc_info: bool = True,
    game_filter: str | None = None,
) -> int:
    collector = Collector(
        SessionPaths(session_dir),
        interval_ms=interval_ms,
        thermal_enabled=thermal_enabled,
        password_fd=password_fd,
        soc_info_flag=soc_info,
        game_filter=game_filter,
    )
    return collector.run()
