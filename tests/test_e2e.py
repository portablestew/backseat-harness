"""End-to-end daemon validation: real CLI surfaces (subprocesses), isolated HOME,
real workspace, file-triggered check-in, and daemon termination assertions.

The daemon, register-workspace, and the registry all run as real
subprocesses over a real workspace under a hermetic temp HOME. The daemon's
spawned ``run`` picks up a stub provider CLI injected on PATH (no parent
monkeypatch reaches the daemon subprocess). Termination uses
CREATE_NEW_PROCESS_GROUP + CTRL_BREAK_EVENT; the daemon installs a
SIGBREAK (Windows) / SIGTERM (POSIX) graceful-shutdown handler that calls
self.stop(), so finally releases the guard; the assertions require that
release.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backseat_harness import db
from backseat_harness.cli import main


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A workspace root with a single seeded lane ready for daemon adoption."""
    ws = tmp_path / "e2e_ws"
    tasks_dir = ws / ".backseat" / "tasks" / "demo"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "goal.md").write_text("Build the demo.\n", encoding="utf-8")
    (tasks_dir / "checklist.json").write_text(
        '{"steps": {}, "remarks": []}\n', encoding="utf-8"
    )
    return ws


def _make_stub_provider_bin(stub_bin: Path) -> None:
    """Write a stub `opencode` CLI that writes a marker JSON event and exits 0."""
    stub_bin.mkdir(parents=True, exist_ok=True)
    bat = stub_bin / "opencode.bat"
    # The adapter expects newline-delimited JSON events (or raw output).
    # Emit one event with sessionID so parse_result succeeds and extract_text
    # returns our marker.
    bat.write_text(
        '@echo off\r\n'
        'echo {"sessionID":"stub-ses-e2e",'
        '"type":"text","part":'
        '{"id":"1","text":"STUB_PROVIDER_RAN"}}\r\n'
        'exit /b 0\r\n',
        encoding="utf-8",
    )


@pytest.mark.slow
def test_e2e_daemon_picks_up_new_goal_and_spawns_run(
    workspace: Path, fake_home: Path, capsys
):
    """Real daemon subprocess picks up file change, spawns stub provider run,
    terminates cleanly (guard released, no orphan rows), and proves stub
    provider reached daemon's spawned process via daemon.log marker."""

    # --- Provider stub via PATH (reaches daemon's spawned interpreter) ---
    stub_bin = fake_home / "stub_bin"
    _make_stub_provider_bin(stub_bin)

    # --- 1. Register workspace via CLI ---
    rc = main(
        [
            "-C",
            str(workspace),
            "register-workspace",
            str(workspace),
        ]
    )
    assert rc == 0, f"register-workspace failed rc={rc}"

    # --- 2. Start daemon with CREATE_NEW_PROCESS_GROUP ---
    daemon_stdout_path = workspace / ".backseat" / "daemon_stdout.log"
    daemon_stdout_path.parent.mkdir(parents=True, exist_ok=True)

    # Inject stub provider PATH so daemon's spawned `run` resolves it.
    daemon_env = {
        **os.environ,
        "HOME": str(fake_home),
        "USERPROFILE": str(fake_home),
        "PATH": f"{stub_bin};{os.environ.get('PATH', '')}",
    }

    with open(str(daemon_stdout_path), "wb") as out:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "backseat_harness.cli",
                "-C",
                str(workspace),
                "daemon",
                "run",
            ],
            stdout=out,
            stderr=out,
            stdin=subprocess.DEVNULL,
            env=daemon_env,
            cwd=str(workspace),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )

    # --- 3. Wait for daemon claim ---
    guard_found = False
    for _ in range(10):
        time.sleep(0.5)
        with db.open_db() as conn:
            guard = db.kv_get(conn, db.DAEMON_KEY)
            workspaces = db.list_workspaces(conn)
        if guard is not None:
            assert str(workspace.resolve()) in workspaces, (
                f"workspace adopted but not in registry: workspaces={workspaces}"
            )
            guard_found = True
            break

    def _tail(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="ignore")[-2000:]
        except Exception:
            return "(unreadable)"

    assert guard_found, (
        f"daemon guard never appeared (workspace={workspace.resolve()})\n"
        f"daemon stdout tail:\n{_tail(daemon_stdout_path)}"
    )

    # --- 4. Trigger file change ---
    time.sleep(0.5)
    (workspace / ".backseat" / "tasks" / "demo" / "goal.md").write_text(
        "Build the updated demo.\n", encoding="utf-8"
    )

    # --- 5. Wait for spawn evidence (db row OR daemon.log marker) ---
    workspace_daemon_log = workspace / ".backseat" / "tasks" / "demo" / "daemon.log"
    spawned = False
    task_running = False
    for _ in range(90):
        time.sleep(0.5)
        state_path = workspace / ".backseat" / "tasks" / "demo" / "state.json"
        with db.open_db() as conn:
            rows = db.list_tasks(conn)
        task_running = any(
            r.get("task_relpath") == ".backseat/tasks/demo"
            and r.get("provider_pid") is not None
            for r in rows
        )
        # The stub provider writes to daemon.log via the harness stdout redirect.
        log_has_marker = False
        try:
            if workspace_daemon_log.exists():
                content = workspace_daemon_log.read_text(
                    encoding="utf-8", errors="ignore"
                )
                log_has_marker = "STUB_PROVIDER_RAN" in content
        except Exception:
            pass
        if state_path.exists() or task_running or log_has_marker:
            spawned = True
            break

    has_state = (workspace / ".backseat" / "tasks" / "demo" / "state.json").exists()
    msg = (
        f"daemon did not spawn within 45s: state.json={has_state}, "
        f"provider_pid_row={task_running}, daemon.log_marker="
        f"{'yes' if log_has_marker else 'no'}, log_size="
        f"{workspace_daemon_log.stat().st_size if workspace_daemon_log.exists() else 0}\n"
        f"daemon stdout tail:\n{_tail(daemon_stdout_path)}"
    )
    assert spawned, msg

    # Prove stub provider reached daemon's spawned process (not parent
    # monkeypatch). The operator log files are written by the daemon's spawned
    # `run` harness; check them for the stub marker (no adapter turn needed
    # for this proof — the stub is resolved by the harness CLI inside the daemon).
    op_logs = list(
        (workspace / ".backseat" / "tasks" / "demo" / "logs").glob("*.operator.*.log")
    ) if (workspace / ".backseat" / "tasks" / "demo" / "logs").exists() else []
    op_has_marker = False
    op_text = ""
    for log_path in op_logs:
        text = log_path.read_text(encoding="utf-8", errors="ignore")
        op_text += text
        if "STUB_PROVIDER_RAN" in text:
            op_has_marker = True
    daemon_has_error = False
    if workspace_daemon_log.exists():
        d_text = workspace_daemon_log.read_text(encoding="utf-8", errors="ignore")
        daemon_has_error = (
            "opencode" in d_text.lower()
            and ("not found" in d_text.lower() or "not recognized" in d_text.lower())
        )
    assert op_has_marker, (
        "stub provider evidence missing: operator log (daemon's spawned harness) "
        "has no STUB_PROVIDER_RAN marker from PATH-injected stub. op_logs="
        f"{op_logs}, op_text_snippet={op_text[:500]}, daemon_has_error="
        f"{daemon_has_error}, daemon_log_snippet={_tail(workspace_daemon_log)[:500]}"
    )

    # --- 6. Terminate cleanly (CTRL_BREAK_EVENT delivers finally) ---
    # Send to the process group (negative pid) because the daemon runs in its
    # own group via CREATE_NEW_PROCESS_GROUP.
    try:
        os.kill(-proc.pid, signal.CTRL_BREAK_EVENT)
    except Exception:
        # Fallback to direct pid if group kill fails.
        os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
    proc.wait(timeout=15)

    # --- 7. Assert clean termination (finally runs, guard released). ---
    with db.open_db() as conn:
        guard_after = db.kv_get(conn, db.DAEMON_KEY)
        rows_after = db.list_tasks(conn)
    assert guard_after is None, (
        f"daemon guard not released after CTRL_BREAK_EVENT: {guard_after}; "
        f"expected finally to clear it"
    )
    orphan_rows = [
        r for r in rows_after
        if r.get("workspace_path") == str(workspace.resolve())
        and r.get("harness_pid") is not None
    ]
    assert not orphan_rows, (
        f"orphan harness rows remain after termination: {orphan_rows}"
    )

    # --- 8. Final verification: workspace registered ---
    with db.open_db() as conn:
        paths_registered = db.list_workspaces(conn)
    assert str(workspace.resolve()) in paths_registered, (
        f"workspace unregistered: {paths_registered}"
    )
