"""Restart a running Loom web process without touching agent tmux sessions.

The web server cannot replace itself in-request (the listen port is still
bound). Instead it spawns a short-lived supervisor that SIGKILLs this
process — skipping shutdown handlers that would tear down an independently
started Turbogate tunnel — waits for the port to free, and execs the same
``loom web`` argv from the (possibly updated) source checkout.

Agent panes live in tmux and are not children of Loom, so they keep running.
In-flight AR one-shot jobs (arxiv mine / idea gen / reviewer) are server
subprocesses and *do* die; the API refuses while those are running unless
``allow_active_jobs`` is set.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from loom import __version__

_UPDATE_PREFIX = "LOOM_UPDATE_"


def source_checkout() -> Path:
    """Repo root when running from a git checkout; the package parent otherwise."""
    return Path(__file__).resolve().parent.parent


def git_info(source: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(source), *args],
                capture_output=True,
                text=True,
                timeout=8,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    head = run("rev-parse", "--short", "HEAD")
    return {
        "head": head,
        "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(run("status", "--porcelain")),
        "has_git": bool(head),
    }


def git_pull(source: Path) -> dict[str, Any]:
    """Fast-forward only. Never writes secrets; never force-resets."""
    try:
        result = subprocess.run(
            ["git", "-C", str(source), "pull", "--ff-only", "--stat"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}
    out = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        return {"ok": False, "error": out or f"git pull exited {result.returncode}"}
    return {"ok": True, "output": out, **git_info(source)}


def launch_argv(source: Path, current_argv: list[str] | None = None) -> list[str]:
    """Rebuild ``python -m loom web …`` pointing at *source*'s venv if present."""
    argv = list(current_argv if current_argv is not None else sys.argv)
    python = source / ".venv" / "bin" / "python"
    exe = str(python) if python.is_file() else sys.executable
    if not argv:
        return [exe, "-m", "loom", "web"]
    script = Path(argv[0]).name
    if script == "loom":
        return [exe, "-m", "loom", *argv[1:]]
    if script.startswith("__main__"):
        rest = argv[1:]
        if rest[:1] != ["web"]:
            rest = ["web", *rest]
        return [exe, "-m", "loom", *rest]
    rebuilt = list(argv)
    rebuilt[0] = exe
    if "-m" not in rebuilt[:4]:
        rest = argv[1:]
        if rest[:1] != ["web"]:
            rest = ["web", *rest]
        return [exe, "-m", "loom", *rest]
    return rebuilt


def server_status(port: int) -> dict[str, Any]:
    source = source_checkout()
    return {
        "ok": True,
        "version": __version__,
        "source": str(source),
        "port": port,
        "python": sys.executable,
        "git": git_info(source),
        "platform": sys.platform,
    }


def active_one_shot_jobs(projects: list[tuple[str, Path]]) -> list[str]:
    """AR mine/ideas/review jobs that cannot survive a process replacement."""
    from loom import ar_task as ar
    from loom.rud_task import list_tasks

    active: list[str] = []
    for project_id, root in projects:
        try:
            tasks = list_tasks(root)
        except OSError:
            continue
        for meta in tasks:
            if not ar.is_ar_kind(meta.kind):
                continue
            try:
                state = ar.read_ar_state(root, meta.slug) or {}
            except OSError:
                continue
            for job in ("papers", "ideas", "review"):
                if str(state.get(f"{job}_status") or "") == "running":
                    active.append(f"{project_id}/{meta.slug}: {job}")
    return active


def _port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def supervisor_main() -> None:
    """Entry for the detached supervisor (``python -c`` / ``-m loom.self_update``)."""
    parent = int(os.environ.get("LOOM_UPDATE_PARENT") or "0")
    port = int(os.environ.get("LOOM_UPDATE_PORT") or "0")
    source = Path(os.environ.get("LOOM_UPDATE_SOURCE") or ".")
    raw_argv = os.environ.get("LOOM_UPDATE_ARGV") or "[]"
    argv = json.loads(raw_argv)
    if not parent or not port or not isinstance(argv, list) or not argv:
        sys.stderr.write("loom.self_update: missing supervisor env\n")
        sys.exit(2)
    time.sleep(0.6)
    try:
        os.kill(parent, signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 8.0
    while _port_open(port) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _port_open(port):
        sys.stderr.write(f"loom.self_update: port {port} did not become free\n")
        sys.exit(3)
    clean = {key: value for key, value in os.environ.items() if not key.startswith(_UPDATE_PREFIX)}
    try:
        os.chdir(source)
    except OSError:
        pass
    os.execve(str(argv[0]), [str(part) for part in argv], clean)


def schedule_restart(
    port: int,
    *,
    pull: bool = False,
    allow_active_jobs: bool = False,
    projects: list[tuple[str, Path]] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    source = source_checkout()
    jobs = active_one_shot_jobs(projects or [])
    if jobs and not allow_active_jobs:
        return {
            "ok": False,
            "error": (
                "refusing to interrupt non-resumable AR jobs: "
                + ", ".join(jobs)
                + " (wait, or pass allow_active_jobs)"
            ),
            "active_one_shot_jobs": jobs,
        }
    pull_result: dict[str, Any] | None = None
    if pull:
        pull_result = git_pull(source)
        if not pull_result.get("ok"):
            return {"ok": False, "error": pull_result.get("error"), "pull": pull_result}
    argv = launch_argv(source)
    python = Path(argv[0])
    if not python.is_file():
        return {"ok": False, "error": f"updated interpreter does not exist: {python}"}
    result: dict[str, Any] = {
        "ok": True,
        "version": __version__,
        "source": str(source),
        "port": port,
        "command": argv,
        "active_one_shot_jobs": jobs,
        "dry_run": dry_run,
        "pull": pull_result,
        "git": git_info(source),
    }
    if dry_run:
        return result

    log_path = source / ".RUD" / f"loom-{port}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["LOOM_UPDATE_PARENT"] = str(os.getpid())
    env["LOOM_UPDATE_PORT"] = str(port)
    env["LOOM_UPDATE_SOURCE"] = str(source)
    env["LOOM_UPDATE_ARGV"] = json.dumps(argv)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(
            f"\n# loom update scheduled {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
            f"parent={os.getpid()} port={port}\n"
        )
        log.flush()
        proc = subprocess.Popen(
            [argv[0], "-c", "from loom.self_update import supervisor_main; supervisor_main()"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=str(source),
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    result["scheduled"] = True
    result["supervisor_pid"] = proc.pid
    return result


if __name__ == "__main__":
    supervisor_main()
