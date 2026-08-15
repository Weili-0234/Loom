"""Tests for in-process Loom update / hot restart helpers."""

from __future__ import annotations

from pathlib import Path

from loom.self_update import git_info, launch_argv, schedule_restart, server_status


def test_launch_argv_prefers_checkout_venv(tmp_path: Path) -> None:
    source = tmp_path / "Loom"
    python = source / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    argv = launch_argv(
        source,
        ["/old/.venv/bin/python", "-m", "loom", "web", "--port", "8765"],
    )
    assert argv[0] == str(python)
    assert argv[1:4] == ["-m", "loom", "web"]
    assert "--port" in argv


def test_launch_argv_from_dash_m_main(tmp_path: Path) -> None:
    source = tmp_path / "Loom"
    python = source / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    argv = launch_argv(
        source,
        [
            str(source / "loom" / "__main__.py"),
            "web",
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
            "--project",
            "/data/wxu",
        ],
    )
    assert argv[:4] == [str(python), "-m", "loom", "web"]
    assert argv.count("web") == 1
    assert "--project" in argv


def test_launch_argv_from_loom_script(tmp_path: Path) -> None:
    source = tmp_path / "Loom"
    python = source / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    argv = launch_argv(source, ["/usr/bin/loom", "web", "--project", "/data/wxu"])
    assert argv[0] == str(python)
    assert argv[1:4] == ["-m", "loom", "web"]
    assert "--project" in argv


def test_dry_run_schedule_does_not_spawn(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "Loom"
    (source / "loom").mkdir(parents=True)
    python = source / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")

    monkeypatch.setattr("loom.self_update.source_checkout", lambda: source)
    monkeypatch.setattr("loom.self_update.active_one_shot_jobs", lambda projects: [])
    monkeypatch.setattr(
        "loom.self_update.git_info",
        lambda src: {"head": "abc", "branch": "main", "dirty": False, "has_git": True},
    )
    spawned: list[list[str]] = []

    def fake_popen(*args, **kwargs):
        spawned.append(list(args[0]))
        raise AssertionError("dry-run must not spawn")

    monkeypatch.setattr("loom.self_update.subprocess.Popen", fake_popen)
    result = schedule_restart(8765, dry_run=True)
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert spawned == []
    assert result["command"][0] == str(python)


def test_schedule_refuses_active_jobs(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "Loom"
    (source / ".venv" / "bin").mkdir(parents=True)
    (source / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    monkeypatch.setattr("loom.self_update.source_checkout", lambda: source)
    monkeypatch.setattr(
        "loom.self_update.active_one_shot_jobs",
        lambda projects: ["proj/task: review"],
    )
    result = schedule_restart(8765, dry_run=True)
    assert result["ok"] is False
    assert "review" in result["error"]
    allowed = schedule_restart(8765, dry_run=True, allow_active_jobs=True)
    assert allowed["ok"] is True


def test_server_status_includes_version() -> None:
    payload = server_status(8765)
    assert payload["ok"] is True
    assert payload["port"] == 8765
    assert payload["version"]
    assert "git" in payload


def test_git_info_on_this_checkout() -> None:
    info = git_info(Path(__file__).resolve().parents[1])
    assert info["has_git"] is True
    assert info["head"]
