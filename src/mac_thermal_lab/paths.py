"""XDG Base Directory 路径解析。

所有目录只在这里算一次，其它模块不要再拼 `~/.local/...` 之类的路径。

布局（APP = mac-thermal-lab）：

    配置    $XDG_CONFIG_HOME/mac-thermal-lab/config.toml
    数据    $XDG_DATA_HOME/mac-thermal-lab/recordings/<session>/
    状态    $XDG_STATE_HOME/mac-thermal-lab/sessions/<session>/{state.json,collector.log}
    缓存    $XDG_CACHE_HOME/mac-thermal-lab/
    运行期  $XDG_RUNTIME_DIR/mac-thermal-lab/<session>.{lock,pid}

未设置时按规范回退到 ~/.config、~/.local/share、~/.local/state、~/.cache。
XDG_RUNTIME_DIR 在 macOS 通常不存在，回退到按 uid 隔离的私有临时目录。
"""

from __future__ import annotations

import os
import stat
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

APP = "mac-thermal-lab"
CONFIG_FILE = "config.toml"
SESSIONS_SUBDIR = "sessions"
RECORDINGS_SUBDIR = "recordings"

# 旧版本把实验数据放在源码目录下，迁移时作为来源
LEGACY_RECORDINGS = Path.home() / ".local" / "src" / "MacThermalLab" / "recordings"


# --------------------------------------------------------------------------- #
# 基础解析
# --------------------------------------------------------------------------- #
def _expand(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value))))


def _first_set(env_names: tuple[str, ...]) -> Path | None:
    """按顺序取第一个非空环境变量（MTL_* 优先于 XDG_*，便于测试与隔离）。"""
    for name in env_names:
        raw = os.environ.get(name)
        if raw:
            return _expand(raw)
    return None


def config_home() -> Path:
    return _first_set(("MTL_CONFIG_HOME", "XDG_CONFIG_HOME")) or Path.home() / ".config"


def data_home() -> Path:
    return _first_set(("MTL_DATA_HOME", "XDG_DATA_HOME")) or Path.home() / ".local" / "share"


def state_home() -> Path:
    return _first_set(("MTL_STATE_HOME", "XDG_STATE_HOME")) or Path.home() / ".local" / "state"


def cache_home() -> Path:
    return _first_set(("MTL_CACHE_HOME", "XDG_CACHE_HOME")) or Path.home() / ".cache"


def runtime_home() -> Path:
    """$XDG_RUNTIME_DIR，不可用时回退到按 uid 隔离的私有临时目录。"""
    explicit = _first_set(("MTL_RUNTIME_DIR", "XDG_RUNTIME_DIR"))
    if explicit is not None and _usable_runtime(explicit):
        return explicit
    return _private_tmp_runtime()


def _usable_runtime(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        return False
    return path.is_dir() and os.access(path, os.W_OK)


def _private_tmp_runtime() -> Path:
    """临时目录回退：名字带 uid，权限收紧到 0700，避免被其它用户读写。"""
    uid = os.getuid() if hasattr(os, "getuid") else 0
    base = Path(tempfile.gettempdir()) / f"{APP}-runtime-{uid}"
    try:
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = base.stat()
        if info.st_uid != uid:
            # 目录不属于自己（多用户机器上的 /tmp 抢注），换个更私密的位置
            base = Path(tempfile.mkdtemp(prefix=f"{APP}-runtime-{uid}-"))
        elif stat.S_IMODE(info.st_mode) & 0o077:
            base.chmod(0o700)
    except OSError:
        base = Path(tempfile.mkdtemp(prefix=f"{APP}-runtime-{uid}-"))
    return base


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> tuple[dict[str, Any], str | None]:
    """读 config.toml；返回 (配置, 错误说明)。坏文件不致命，退化为默认值。"""
    if not path.exists():
        return {}, None
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {}, f"配置文件无法解析（{exc}），本次使用默认路径"
    return (data if isinstance(data, dict) else {}), None


CONFIG_TEMPLATE = """\
# Mac Thermal Lab 配置
# 所有键都可省略；省略时使用 XDG 默认路径。

# 实验数据目录。支持 ~ 与环境变量；相对路径相对于本文件所在目录。
# 默认：$XDG_DATA_HOME/mac-thermal-lab/recordings
# recordings_dir = "~/Documents/MacThermalLab"

# 默认采样间隔（毫秒）。命令行 -i 优先。
# interval_ms = 2000
"""


def write_config_template(path: Path) -> bool:
    """写入带注释的配置模板；已存在则不动。"""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return True


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AppPaths:
    config_dir: Path
    data_dir: Path
    state_dir: Path
    cache_dir: Path
    runtime_dir: Path
    recordings_dir: Path
    config_error: str | None = None

    @property
    def config_file(self) -> Path:
        return self.config_dir / CONFIG_FILE

    @property
    def sessions_state_dir(self) -> Path:
        return self.state_dir / SESSIONS_SUBDIR

    def describe(self) -> list[tuple[str, Path]]:
        return [
            ("配置", self.config_dir),
            ("数据", self.recordings_dir),
            ("状态", self.state_dir),
            ("缓存", self.cache_dir),
            ("运行期", self.runtime_dir),
        ]


_CACHE: dict[str, AppPaths] = {}


def resolve_paths(recordings_override: str | os.PathLike[str] | None = None) -> AppPaths:
    """解析一次全部目录。recordings 优先级：显式参数 > MTL_DATA_DIR > 配置 > XDG 默认。"""
    key = str(recordings_override) if recordings_override else ""
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    config_dir = config_home() / APP
    data_dir = data_home() / APP
    config_file = config_dir / CONFIG_FILE
    config, config_error = load_config(config_file)

    recordings: Path | None = None
    if recordings_override:
        recordings = _expand(recordings_override)
    elif os.environ.get("MTL_DATA_DIR"):
        recordings = _expand(os.environ["MTL_DATA_DIR"])
    elif config.get("recordings_dir"):
        raw = str(config["recordings_dir"])
        candidate = _expand(raw)
        if not candidate.is_absolute():
            candidate = (config_dir / candidate)
        recordings = candidate
    if recordings is None:
        recordings = data_dir / RECORDINGS_SUBDIR

    paths = AppPaths(
        config_dir=config_dir,
        data_dir=data_dir,
        state_dir=state_home() / APP,
        cache_dir=cache_home() / APP,
        runtime_dir=runtime_home() / APP,
        recordings_dir=recordings,
        config_error=config_error,
    )
    _CACHE[key] = paths
    return paths


def reset_cache() -> None:
    """清掉缓存（测试改环境变量后需要调用）。"""
    _CACHE.clear()


def session_state_dir(session_name: str, create: bool = False) -> Path:
    """每个会话的运行诊断目录：state.json、collector.log。

    默认只算路径不建目录——列会话是只读操作，不该在 STATE 下留一堆空目录。
    """
    path = resolve_paths().sessions_state_dir / session_name
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def session_runtime_dir(create: bool = False) -> Path:
    path = resolve_paths().runtime_dir
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def legacy_recordings() -> Path | None:
    """旧布局的 recordings 目录（存在且不是当前目录时才返回）。"""
    current = resolve_paths().recordings_dir
    try:
        if LEGACY_RECORDINGS.exists() and LEGACY_RECORDINGS.resolve() != current.resolve():
            return LEGACY_RECORDINGS
    except OSError:
        return None
    return None
