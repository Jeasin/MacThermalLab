"""XDG 路径解析：默认值、覆盖优先级、runtime 回退。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mac_thermal_lab import paths as paths_mod


def _clear(monkeypatch):
    for name in (
        "MTL_CONFIG_HOME", "MTL_DATA_HOME", "MTL_STATE_HOME", "MTL_CACHE_HOME",
        "MTL_RUNTIME_DIR", "MTL_DATA_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
        "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    paths_mod.reset_cache()


def test_defaults_follow_xdg_spec(monkeypatch):
    _clear(monkeypatch)
    home = Path.home()
    assert paths_mod.config_home() == home / ".config"
    assert paths_mod.data_home() == home / ".local" / "share"
    assert paths_mod.state_home() == home / ".local" / "state"
    assert paths_mod.cache_home() == home / ".cache"

    paths_mod.reset_cache()
    app = paths_mod.resolve_paths()
    assert app.config_dir == home / ".config" / "mac-thermal-lab"
    assert app.recordings_dir == home / ".local" / "share" / "mac-thermal-lab" / "recordings"
    assert app.sessions_state_dir == home / ".local" / "state" / "mac-thermal-lab" / "sessions"


def test_xdg_env_wins_over_default(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    paths_mod.reset_cache()
    app = paths_mod.resolve_paths()
    assert app.recordings_dir == tmp_path / "d" / "mac-thermal-lab" / "recordings"
    assert app.config_dir == tmp_path / "c" / "mac-thermal-lab"


def test_recordings_precedence(monkeypatch, tmp_path):
    _clear(monkeypatch)
    config_dir = tmp_path / "config" / "mac-thermal-lab"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        f'recordings_dir = "{tmp_path / "from-config"}"\n', encoding="utf-8"
    )
    monkeypatch.setenv("MTL_CONFIG_HOME", str(tmp_path / "config"))
    paths_mod.reset_cache()
    assert paths_mod.resolve_paths().recordings_dir == tmp_path / "from-config"

    # MTL_DATA_DIR 覆盖配置
    monkeypatch.setenv("MTL_DATA_DIR", str(tmp_path / "from-env"))
    paths_mod.reset_cache()
    assert paths_mod.resolve_paths().recordings_dir == tmp_path / "from-env"

    # 显式参数优先级最高
    paths_mod.reset_cache()
    assert paths_mod.resolve_paths(tmp_path / "explicit").recordings_dir == tmp_path / "explicit"


def test_relative_recordings_dir_resolves_against_config_dir(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("MTL_CONFIG_HOME", str(tmp_path / "c"))
    config_dir = tmp_path / "c" / "mac-thermal-lab"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text('recordings_dir = "records"\n', encoding="utf-8")
    paths_mod.reset_cache()
    assert paths_mod.resolve_paths().recordings_dir == config_dir / "records"


def test_broken_config_falls_back(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("MTL_CONFIG_HOME", str(tmp_path / "c"))
    config_dir = tmp_path / "c" / "mac-thermal-lab"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text("recordings_dir = [broken", encoding="utf-8")
    paths_mod.reset_cache()
    app = paths_mod.resolve_paths()
    assert app.config_error
    assert app.recordings_dir.name == "recordings"


def test_runtime_fallback_is_private(monkeypatch, tmp_path):
    _clear(monkeypatch)
    runtime = paths_mod.runtime_home()
    assert str(os.getuid()) in runtime.name
    assert runtime.is_dir()
    assert runtime.stat().st_mode & 0o077 == 0


def test_runtime_prefers_xdg_when_usable(monkeypatch, tmp_path):
    _clear(monkeypatch)
    target = tmp_path / "run"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(target))
    assert paths_mod.runtime_home() == target
    assert target.exists()


def test_session_dirs_split_by_kind(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("MTL_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MTL_RUNTIME_DIR", str(tmp_path / "run"))
    paths_mod.reset_cache()
    from mac_thermal_lab.storage import SessionPaths

    session = SessionPaths(tmp_path / "data" / "2026-11-12_080000_x")
    assert tmp_path / "state" in session.state.parents
    assert session.lock.parent == tmp_path / "run" / "mac-thermal-lab"
    assert session.lock.name.endswith(".lock")
    assert session.state_dir.exists() is False  # 只读不建目录
    session.ensure_runtime_dirs()
    assert session.state_dir.exists()
    assert session.lock.parent.exists()


def test_init_config_template(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("MTL_CONFIG_HOME", str(tmp_path / "c"))
    paths_mod.reset_cache()
    target = paths_mod.resolve_paths().config_file
    assert paths_mod.write_config_template(target) is True
    text = target.read_text(encoding="utf-8")
    assert "recordings_dir" in text
    assert paths_mod.write_config_template(target) is False  # 已存在不覆盖
