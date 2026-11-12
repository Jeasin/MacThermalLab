"""旧目录迁移：不覆盖、不丢数据、校验通过才清理。"""

from __future__ import annotations

import json
from pathlib import Path

from mac_thermal_lab.migrate import plan_migration, run_migration


def _make_session(root: Path, name: str, payload: bytes = b"x" * 100) -> Path:
    session = root / name
    session.mkdir(parents=True)
    (session / "metadata.json").write_text(
        json.dumps({"schema": 1, "name": name}, ensure_ascii=False), encoding="utf-8"
    )
    (session / "macmon.jsonl").write_bytes(payload)
    (session / "state.json").write_text("{}", encoding="utf-8")
    (session / "collector.log").write_text("[log]\n", encoding="utf-8")
    return session


def test_plan_detects_conflict(tmp_path):
    source = tmp_path / "old"
    target = tmp_path / "new"
    _make_session(source, "s1")
    _make_session(target, "s1")

    report = plan_migration(source, target)
    assert len(report.plans) == 1
    assert report.plans[0].status == "conflict"


def test_dry_run_changes_nothing(tmp_path):
    source = tmp_path / "old"
    target = tmp_path / "new"
    _make_session(source, "s1")

    report = run_migration(source, target, dry_run=True)
    assert report.count("dry") == 1
    assert (source / "s1").exists()
    assert not (target / "s1").exists()
    assert not source.name == ""


def test_migrate_moves_data_and_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MTL_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MTL_RUNTIME_DIR", str(tmp_path / "run"))
    from mac_thermal_lab import paths as paths_mod

    paths_mod.reset_cache()

    source = tmp_path / "old"
    target = tmp_path / "new"
    payload = b"sample-line\n" * 500
    _make_session(source, "s1", payload)

    report = run_migration(source, target)
    assert report.count("done") == 1
    assert report.errors == []
    assert (target / "s1" / "macmon.jsonl").read_bytes() == payload
    assert not (source / "s1").exists()
    assert report.source_removed is True

    # 运行期文件被移到 state，不再留在数据目录里
    state_dir = tmp_path / "state" / "mac-thermal-lab" / "sessions" / "s1"
    assert (state_dir / "state.json").exists()
    assert (state_dir / "collector.log").exists()
    assert not (target / "s1" / "state.json").exists()


def test_conflict_is_never_overwritten(tmp_path):
    source = tmp_path / "old"
    target = tmp_path / "new"
    _make_session(source, "s1", b"old")
    _make_session(target, "s1", b"new")

    report = run_migration(source, target)
    assert report.count("conflict") == 1
    assert (target / "s1" / "macmon.jsonl").read_bytes() == b"new"
    assert (source / "s1").exists()  # 源保留
    assert not report.ok


def test_source_kept_when_leftovers_remain(tmp_path):
    source = tmp_path / "old"
    target = tmp_path / "new"
    _make_session(source, "s1")
    (source / "notes.txt").write_text("手写备注", encoding="utf-8")

    report = run_migration(source, target)
    assert report.count("done") == 1
    assert source.exists()  # 还有别的东西，不删
    assert any("未迁移内容" in e for e in report.errors)


def test_missing_source_reports_error(tmp_path):
    report = run_migration(tmp_path / "nope", tmp_path / "new")
    assert report.errors


def test_no_partial_target_left_after_failure(tmp_path, monkeypatch):
    source = tmp_path / "old"
    target = tmp_path / "new"
    _make_session(source, "s1")

    import mac_thermal_lab.migrate as migrate_mod

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(migrate_mod.shutil, "copytree", boom)
    report = run_migration(source, target)
    assert report.count("failed") == 1
    assert not any(p.name.startswith(".migrating-") for p in target.iterdir())
    assert (source / "s1").exists()
