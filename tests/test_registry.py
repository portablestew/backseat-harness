"""Registry (db.py): workspace set/version invariant, atomic claim, pid seam.

Hermetic by construction: the autouse home pin makes db_path() land under
fake_home, and an autouse guard makes any consult of the real psutil fail
the test — liveliness is exercised through the _process seam only, so no
test ever asks the OS whether a real pid is alive (and no test can kill
anything; the sweep's killer is always a fake).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from backseat_harness import db, spawn


class FakeProc:
    """A stand-in psutil.Process with a controllable create_time."""

    def __init__(self, create_time: float, running: bool = True):
        self._create_time = create_time
        self._running = running

    def is_running(self) -> bool:
        return self._running

    def create_time(self) -> float:
        return self._create_time


@pytest.fixture(autouse=True)
def no_real_psutil(monkeypatch):
    """Any code path that would ask the real OS about a pid fails loudly."""

    def _boom(pid):
        raise AssertionError(f"test consulted the real psutil for pid {pid!r}")

    monkeypatch.setattr(db, "_process", _boom)


@pytest.fixture
def conn(fake_home: Path):
    """A registry connection whose file lives under the pinned fake home."""
    connection = db.connect()
    yield connection
    connection.close()


# --- hermeticity & schema -------------------------------------------------


def test_db_path_lands_under_the_pinned_home(fake_home: Path):
    """The fake_home pin must actually isolate the db — this is the guard."""
    assert db.db_path() == fake_home / ".backseat" / "db.sqlite"
    assert fake_home in db.db_path().parents


def test_connect_creates_schema_and_seeds_schema_version(conn):
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"workspaces", "tasks", "kv"} <= tables
    # The models.py "v" convention, carried as a kv row (2-plan §1).
    assert db.kv_get(conn, db.SCHEMA_VERSION_KEY) == str(db.SCHEMA_VERSION)
    # Reconnect is idempotent: schema re-created, version value left alone.
    again = db.connect()
    try:
        assert db.kv_get(again, db.SCHEMA_VERSION_KEY) == str(db.SCHEMA_VERSION)
    finally:
        again.close()


def test_connect_upgrades_a_registry_that_predates_provider_starts(conn):
    """CREATE TABLE IF NOT EXISTS never widens an existing table; connect does."""
    conn.execute("ALTER TABLE tasks DROP COLUMN provider_starts")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "provider_starts" not in cols

    again = db.connect()
    try:
        cols = {row[1] for row in again.execute("PRAGMA table_info(tasks)")}
        assert "provider_starts" in cols
    finally:
        again.close()


def test_workspaces_version_starts_at_zero(conn):
    assert db.workspaces_version(conn) == 0
    assert db.kv_get(conn, "no-such-key") is None
    assert db.kv_get(conn, "no-such-key", "fallback") == "fallback"


# --- workspace set / workspaces-version invariant -------------------------


def test_add_workspace_bumps_only_when_the_set_changes(conn, workspace: Path):
    assert db.add_workspace(conn, workspace) is True
    assert db.workspaces_version(conn) == 1
    # Re-adding a listed path: no set change, no bump (2-plan §1 amendment).
    assert db.add_workspace(conn, workspace) is False
    assert db.workspaces_version(conn) == 1
    assert db.list_workspaces(conn) == [str(workspace.resolve())]
    # A genuinely new path bumps exactly once.
    assert db.add_workspace(conn, workspace.parent / "elsewhere") is True
    assert db.workspaces_version(conn) == 2


def test_unregister_bumps_only_for_a_listed_path(conn, workspace: Path):
    db.add_workspace(conn, workspace)
    assert db.unregister_workspace(conn, workspace) is True
    assert db.workspaces_version(conn) == 2
    assert db.list_workspaces(conn) == []
    # Unregistering again (or a never-listed path) changes nothing.
    assert db.unregister_workspace(conn, workspace) is False
    assert db.unregister_workspace(conn, workspace.parent / "ghost") is False
    assert db.workspaces_version(conn) == 2


# --- claim semantics -------------------------------------------------------


def test_claim_auto_adds_the_workspace_then_never_bumps_again(conn, workspace: Path):
    tid = db.claim_task(conn, workspace, "tasks/x", 100, 1000.0)
    # Auto-add before the task runs (goal:4-5) — bumped once.
    assert db.workspaces_version(conn) == 1
    row = db.get_task(conn, workspace, "tasks/x")
    assert row["id"] == tid
    assert row["harness_pid"] == 100
    assert row["harness_start"] == 1000.0
    assert row["provider_pids"] is None
    # A second task claim in the same workspace is not a set change.
    db.claim_task(conn, workspace, "tasks/y", 101, 1001.0)
    assert db.workspaces_version(conn) == 1
    assert len(db.list_tasks(conn)) == 2


def test_claim_conflicts_while_the_existing_harness_is_live(
    conn, workspace: Path, monkeypatch
):
    db.claim_task(conn, workspace, "tasks/x", 100, 1000.0)
    monkeypatch.setattr(db, "_process", lambda pid: FakeProc(1000.0))
    with pytest.raises(db.ClaimConflict, match="already running") as exc:
        db.claim_task(conn, workspace, "tasks/x", 200, 2000.0)
    assert "100" in str(exc.value)
    # Atomic: the losing claim rolled back entirely — original row intact.
    row = db.get_task(conn, workspace, "tasks/x")
    assert row["harness_pid"] == 100
    assert row["harness_start"] == 1000.0


def test_claim_replaces_a_row_whose_harness_died(conn, workspace: Path, monkeypatch):
    db.claim_task(conn, workspace, "tasks/x", 100, 1000.0)
    db.record_provider(conn, workspace, "tasks/x", [555])

    def _dead(pid):
        raise db.psutil.NoSuchProcess(pid)

    monkeypatch.setattr(db, "_process", _dead)
    tid = db.claim_task(conn, workspace, "tasks/x", 200, 2000.0)
    row = db.get_task(conn, workspace, "tasks/x")
    # Contract: claim_task returns the actual claimed/updated row id (not
    # coincidentally matching by connection state via last_insert_rowid).
    assert tid == row["id"]
    assert row["harness_pid"] == 200
    assert row["harness_start"] == 2000.0
    # The new run starts with no provider recorded.
    assert row["provider_pids"] is None
    # Workspace already listed: claims never move the version.
    assert db.workspaces_version(conn) == 1


def test_claim_replaces_a_row_after_pid_reuse(conn, workspace: Path, monkeypatch):
    """A live pid at a *different* start is a different process: dead."""
    db.claim_task(conn, workspace, "tasks/x", 100, 1000.0)
    monkeypatch.setattr(db, "_process", lambda pid: FakeProc(99999.0))
    db.claim_task(conn, workspace, "tasks/x", 200, 2000.0)
    assert db.get_task(conn, workspace, "tasks/x")["harness_pid"] == 200


def test_claim_task_returns_actual_row_id_on_update(conn, workspace: Path, monkeypatch):
    """Update path: return value equals SELECT id, not coincidental last_insert_rowid.

    The None-when-row-vanished path is unreachable in normal operation (no
    concurrent deletion of the same workspace/task by design); the contract
    allows it for defensive completeness.
    """
    # First claim.
    tid1 = db.claim_task(conn, workspace, "tasks/x", 100, 1000.0)
    assert tid1 is not None and isinstance(tid1, int)
    # Replacement claim on a new connection eliminates coincidence.
    monkeypatch.setattr(db, "_process", lambda pid: FakeProc(99999.0))
    with db.connect(path=db.db_path()) as fresh_conn:
        tid2 = db.claim_task(fresh_conn, workspace, "tasks/x", 200, 2000.0)
    row = db.get_task(conn, workspace, "tasks/x")
    assert tid2 == row["id"]
    assert tid2 == tid1  # same row id (update, not new insert)


def test_different_tasks_claim_concurrently_even_while_both_are_live(
    conn, workspace: Path, monkeypatch
):
    monkeypatch.setattr(db, "pid_live", lambda pid, start=None: True)
    db.claim_task(conn, workspace, "tasks/a", 100, 1000.0)
    db.claim_task(conn, workspace, "tasks/b", 200, 2000.0)
    assert len(db.list_tasks(conn)) == 2


# --- provider record / release ---------------------------------------------


def test_record_provider_requires_a_claimed_row(conn, workspace: Path):
    with pytest.raises(db.RegistryError, match="no claimed task row"):
        db.record_provider(conn, workspace, "tasks/none", [555])


def test_record_provider_and_release_roundtrip(conn, workspace: Path):
    db.claim_task(conn, workspace, "tasks/x", 100, 1000.0)
    db.record_provider(conn, workspace, "tasks/x", [555])
    row = db.get_task(conn, workspace, "tasks/x")
    assert row["provider_pids"] == "[555]"
    # Release-before-exit semantics: idempotent, no error on a second call.
    assert db.release_task(conn, workspace, "tasks/x") is True
    assert db.get_task(conn, workspace, "tasks/x") is None
    assert db.release_task(conn, workspace, "tasks/x") is False


def test_list_tasks_covers_every_workspace_and_survives_unregister(
    conn, workspace: Path, tmp_path: Path
):
    """The sweep's worklist is the whole table, per goal:11 (2-plan amendment):
    rows under an unregistered workspace still show up so their provider
    orphan can be found and killed."""
    other = tmp_path / "other-ws"
    db.claim_task(conn, workspace, "tasks/x", 100, 1000.0)
    db.claim_task(conn, other, "tasks/y", 200, 2000.0)
    assert db.unregister_workspace(conn, workspace) is True

    rows = db.list_tasks(conn)
    assert len(rows) == 2
    by_rel = {row["task_relpath"]: row for row in rows}
    assert by_rel["tasks/x"]["workspace_path"] is None
    assert by_rel["tasks/y"]["workspace_path"] == str(other.resolve())


# --- pid liveliness seam ----------------------------------------------------


def test_pid_live_requires_a_matching_start(monkeypatch):
    monkeypatch.setattr(db, "_process", lambda pid: FakeProc(1000.0))
    assert db.pid_live(7, 1000.0) is True
    assert db.pid_live(7, 1000.0 + db.START_TOLERANCE_S) is True  # boundary
    assert db.pid_live(7, 1500.0) is False  # pid reused by a different proc
    assert db.pid_live(7) is True  # no start recorded: existence only


def test_pid_live_is_false_for_dead_or_unverifiable_pids(monkeypatch):
    assert db.pid_live(None) is False
    assert db.pid_live(0) is False
    assert db.pid_live(-1) is False

    monkeypatch.setattr(db, "_process", lambda pid: FakeProc(1000.0, running=False))
    assert db.pid_live(7, 1000.0) is False

    def _nsp(pid):
        raise db.psutil.NoSuchProcess(pid)

    monkeypatch.setattr(db, "_process", _nsp)
    assert db.pid_live(7, 1000.0) is False

    def _denied(pid):
        raise db.psutil.AccessDenied(pid)

    monkeypatch.setattr(db, "_process", _denied)
    # Unverifiable counts as dead: a false "live" would block claims forever.
    assert db.pid_live(7, 1000.0) is False


# --- init / run wiring (cli.py) ---------------------------------------------
#
# The full _cmd_run window per 2-plan §2: gates first (validation failures
# claim nothing), then sweep, claim, provider recording, release. All of it
# through main(), so the wiring itself is what is under test -- pids are faked
# through the _process seam and the sweep's killer is always a fake.

REL = ".backseat/tasks/t"


def _complete(**kwargs):
    from backseat_harness.models import TaskResult, TaskStatus

    return TaskResult(slug="t", status=TaskStatus.COMPLETE, iterations=1)


def test_run_claims_before_run_task_releases_after_and_leaves_no_row(
    workspace: Path, monkeypatch
):
    import backseat_harness.cli as cli
    from backseat_harness.cli import main

    assert main(["-C", str(workspace), "init", "t", "Goal"]) == 0
    seen = {}

    def fake_run_task(**kwargs):
        with db.open_db() as conn:
            seen["row"] = db.get_task(conn, workspace, REL)
        return _complete()

    monkeypatch.setattr(cli, "run_task", fake_run_task)
    assert main(["-C", str(workspace), "run", "t"]) == 0

    row = seen["row"]
    assert row is not None
    # The claim is *this* harness, pid + start (goal:6).
    assert row["harness_pid"] == os.getpid()
    assert row["harness_start"] > 0
    # Released before exit: no row survives a completed run (goal:10).
    with db.open_db() as conn:
        assert db.get_task(conn, workspace, REL) is None


def test_run_conflict_with_a_live_claim_exits_2_and_claims_nothing(
    workspace: Path, monkeypatch, capsys
):
    import backseat_harness.cli as cli
    from backseat_harness.cli import main

    assert main(["-C", str(workspace), "init", "t", "Goal"]) == 0
    with db.open_db() as conn:
        db.claim_task(conn, workspace, REL, 100, 1000.0)

    # Pid 100 is live at its recorded start; that is the only live pid.
    monkeypatch.setattr(db, "_process", lambda pid: FakeProc(1000.0))

    def _no_run(**kwargs):
        raise AssertionError("run_task must not start after a ClaimConflict")

    monkeypatch.setattr(cli, "run_task", _no_run)
    assert main(["-C", str(workspace), "run", "t"]) == 2
    assert "already running" in capsys.readouterr().out
    # The losing run touched nothing: same claim, and no provider recorded.
    with db.open_db() as conn:
        row = db.get_task(conn, workspace, REL)
    assert row["harness_pid"] == 100
    assert row["harness_start"] == 1000.0
    assert row["provider_pids"] is None


def test_validation_failure_before_the_hooks_never_touches_the_registry(
    workspace: Path,
):
    from backseat_harness.cli import main

    # No goal for a fresh lane: the gate returns 2 before any db work.
    assert main(["-C", str(workspace), "run", "fresh"]) == 2
    with db.open_db() as conn:
        assert db.list_tasks(conn) == []
        assert db.list_workspaces(conn) == []
        assert db.workspaces_version(conn) == 0


def test_sweep_scans_every_workspace_kills_live_orphans_and_spares_live_tasks(
    workspace: Path, tmp_path: Path, monkeypatch
):
    """The amendment (2-plan §2): the sweep is table-wide, not per-workspace."""
    import backseat_harness.cli as cli
    from backseat_harness.cli import main

    assert main(["-C", str(workspace), "init", "t", "Goal"]) == 0
    other = tmp_path / "elsewhere"
    with db.open_db() as conn:
        # Orphaned harness, provider still alive -> confirm and kill (goal:12).
        db.claim_task(conn, other, ".backseat/tasks/a", 111, 111.0)
        db.record_provider(conn, other, ".backseat/tasks/a", [999])
        # Orphaned harness, no provider ever spawned -> row deleted only.
        db.claim_task(conn, other, ".backseat/tasks/b", 300, 300.0)
        # Orphaned harness, provider also dead -> row deleted, nothing killed.
        db.claim_task(conn, other, ".backseat/tasks/c", 500, 500.0)
        db.record_provider(conn, other, ".backseat/tasks/c", [777])
        # Live harness elsewhere -> untouched, live provider included.
        db.claim_task(conn, other, ".backseat/tasks/d", 400, 4000.0)
        db.record_provider(conn, other, ".backseat/tasks/d", [888])

    # Absent from the map = NoSuchProcess = dead; a FakeProc = live at its
    # recorded start. Exactly the pids above and no others are consulted.
    procs = {400: FakeProc(4000.0), 888: FakeProc(888.0), 999: FakeProc(999.0)}

    def _process(pid):
        if pid in procs:
            return procs[pid]
        raise db.psutil.NoSuchProcess(pid)

    monkeypatch.setattr(db, "_process", _process)
    killed: list[int] = []
    monkeypatch.setattr(db.spawn, "kill_pid_tree", killed.append)
    monkeypatch.setattr(cli, "run_task", _complete)

    assert main(["-C", str(workspace), "run", "t"]) == 0

    with db.open_db() as conn:
        assert db.get_task(conn, other, ".backseat/tasks/a") is None
        assert db.get_task(conn, other, ".backseat/tasks/b") is None
        assert db.get_task(conn, other, ".backseat/tasks/c") is None
        alive = db.get_task(conn, other, ".backseat/tasks/d")
    assert alive is not None
    assert alive["harness_pid"] == 400
    assert alive["provider_pids"] == "[888]"
    assert killed == [999]


def test_the_sweep_spares_a_recycled_provider_pid(
    workspace: Path, tmp_path: Path, monkeypatch
):
    """pid+start on the provider side: a pid that came back as a different
    process reads dead, so the row is deleted without killing the stranger."""
    import backseat_harness.cli as cli
    from backseat_harness.cli import main

    assert main(["-C", str(workspace), "init", "t", "Goal"]) == 0
    other = tmp_path / "elsewhere"
    with db.open_db() as conn:
        # Recycled: the pid is live, but not at the recorded start.
        db.claim_task(conn, other, ".backseat/tasks/a", 111, 111.0)
        db.record_provider(conn, other, ".backseat/tasks/a", [999], [1000.0])
        # The real thing: pid live at its recorded start -> confirm and kill.
        db.claim_task(conn, other, ".backseat/tasks/b", 222, 222.0)
        db.record_provider(conn, other, ".backseat/tasks/b", [888], [777.0])

    procs = {999: FakeProc(9999.0), 888: FakeProc(777.0)}

    def _process(pid):
        if pid in procs:
            return procs[pid]
        raise db.psutil.NoSuchProcess(pid)

    monkeypatch.setattr(db, "_process", _process)
    killed: list[int] = []
    monkeypatch.setattr(db.spawn, "kill_pid_tree", killed.append)
    monkeypatch.setattr(cli, "run_task", _complete)

    assert main(["-C", str(workspace), "run", "t"]) == 0

    with db.open_db() as conn:
        assert db.get_task(conn, other, ".backseat/tasks/a") is None
        assert db.get_task(conn, other, ".backseat/tasks/b") is None
    assert killed == [888]


def test_record_provider_keeps_starts_aligned_with_pids(conn, workspace: Path):
    db.claim_task(conn, workspace, "tasks/x", 10, 1000.0)
    db.record_provider(conn, workspace, "tasks/x", [1, 2], [100.5, None])
    row = db.get_task(conn, workspace, "tasks/x")
    assert row["provider_pids"] == "[1, 2]"
    assert row["provider_starts"] == "[100.5, null]"

    # A misaligned start list is worse than none: store NULL, never a pair
    # that would make pid_live vouch for the wrong process.
    db.record_provider(conn, workspace, "tasks/x", [3], [1.0, 2.0])
    row = db.get_task(conn, workspace, "tasks/x")
    assert row["provider_pids"] == "[3]"
    assert row["provider_starts"] is None


def test_provider_spawn_during_the_run_lands_in_the_claimed_row(
    workspace: Path, monkeypatch
):
    """The spawn.py hook: a real Popen during run_task records pid+start."""
    import backseat_harness.cli as cli
    from backseat_harness.cli import main

    assert main(["-C", str(workspace), "init", "t", "Goal"]) == 0
    seen = {}

    def fake_run_task(**kwargs):
        # _cmd_run installed the recorder before entering the loop, so this
        # spawn (a trivial child, test_spawn-style) must reach the row.
        spawn.run_with_timeout(
            [sys.executable, "-c", "print('ok')"],
            cwd=workspace,
            input="",
            timeout_s=30,
        )
        with db.open_db() as conn:
            seen["row"] = db.get_task(conn, workspace, REL)
        return _complete()

    monkeypatch.setattr(cli, "run_task", fake_run_task)
    assert main(["-C", str(workspace), "run", "t"]) == 0

    row = seen["row"]
    assert row is not None
    assert row["provider_pids"] is not None
    pids = json.loads(row["provider_pids"])
    assert len(pids) > 0 and all(p > 0 for p in pids)
    # Starts ride the same shape: one entry per pid (None only if the
    # process was gone before psutil could read it back).
    starts = json.loads(row["provider_starts"])
    assert len(starts) == len(pids)
    # The recorder is cleared on the way out -- no leak into the next run.
    assert spawn._provider_recorder.get() is None


def test_release_runs_even_when_run_task_raises(workspace: Path, monkeypatch):
    import backseat_harness.cli as cli
    from backseat_harness.cli import main

    assert main(["-C", str(workspace), "init", "t", "Goal"]) == 0
    seen = {}

    def boom(**kwargs):
        with db.open_db() as conn:
            seen["row"] = db.get_task(conn, workspace, REL)
        raise RuntimeError("loop exploded")

    monkeypatch.setattr(cli, "run_task", boom)
    with pytest.raises(RuntimeError, match="loop exploded"):
        main(["-C", str(workspace), "run", "t"])
    # Claimed before the crash, released despite it, recorder cleared.
    assert seen["row"] is not None
    assert seen["row"]["harness_pid"] == os.getpid()
    with db.open_db() as conn:
        assert db.get_task(conn, workspace, REL) is None
    assert spawn._provider_recorder.get() is None


# --- register-workspace (cli.py) ---------------------------------------------
#
# Goal:18-22 through main([...]): path positional in both forms, the
# bump-iff-set-change invariant end to end (2-plan §1/§5), the add-form
# workspace guard, and _emit's text/json. Hermetic via the autouse pins: the
# db lands under fake_home and this command never consults a pid at all.


@pytest.mark.parametrize(
    "first, repeat, first_msg, repeat_msg",
    [
        (
            ["register-workspace", "{ws}"],
            ["register-workspace", "{ws}"],
            "Registered workspace",
            "already registered",
        ),
        # init registers through the same path as register-workspace.
        (["init", "t", "Goal"], ["init", "t"], None, None),
    ],
    ids=["register-workspace", "init"],
)
def test_register_workspace_adds_once_and_bumps_only_for_a_new_path(
    workspace: Path, capsys, first, repeat, first_msg, repeat_msg
):
    from backseat_harness.cli import main

    def argv(args):
        return ["-C", str(workspace), *[a.replace("{ws}", str(workspace)) for a in args]]

    assert main(argv(first)) == 0
    if first_msg:
        assert first_msg in capsys.readouterr().out
    with db.open_db() as conn:
        assert len(db.list_workspaces(conn)) == 1
        # Stored exactly as the db helpers normalize it: absolute + resolved.
        assert db.list_workspaces(conn) == [str(workspace.resolve())]
        assert db.workspaces_version(conn) == 1
    # Repeat add of a listed path: the set did not change, so no bump.
    assert main(argv(repeat)) == 0
    if repeat_msg:
        assert repeat_msg in capsys.readouterr().out
    with db.open_db() as conn:
        assert db.workspaces_version(conn) == 1
        assert len(db.list_workspaces(conn)) == 1


def test_register_workspace_unregister_bumps_and_keeps_task_rows(workspace: Path, capsys):
    from backseat_harness.cli import main

    assert main(["register-workspace", str(workspace)]) == 0
    # A claimed lane outlives its workspace's registration: the pre-run sweep
    # must still reach its orphaned provider (db.py:243-245, 2-plan §5).
    with db.open_db() as conn:
        db.claim_task(conn, workspace, "tasks/x", 100, 1000.0)

    assert main(["register-workspace", "--unregister", str(workspace)]) == 0
    with db.open_db() as conn:
        assert db.list_workspaces(conn) == []
        assert db.workspaces_version(conn) == 2
        rows = db.list_tasks(conn)
    assert [row["task_relpath"] for row in rows] == ["tasks/x"]

    # A path that was never listed (and does not exist) is a no-op, not an error.
    ghost = workspace / "ghost"
    assert main(["register-workspace", "--unregister", str(ghost)]) == 0
    assert "is not registered" in capsys.readouterr().out
    with db.open_db() as conn:
        assert db.workspaces_version(conn) == 2


def test_register_workspace_rejects_a_path_that_is_not_a_workspace(
    tmp_path: Path, capsys
):
    from backseat_harness.cli import main

    bare = tmp_path / "bare"
    bare.mkdir()
    assert main(["register-workspace", str(bare)]) == 2
    assert "not a workspace" in capsys.readouterr().out
    # Refused before the registry was written at all (2-plan §5 guard).
    with db.open_db() as conn:
        assert db.list_workspaces(conn) == []
        assert db.workspaces_version(conn) == 0


def test_register_workspace_emits_json(workspace: Path, capsys):
    from backseat_harness.cli import main

    assert main(["--format", "json", "register-workspace", str(workspace)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "path": str(workspace.resolve()),
        "registered": True,
        "workspaces_version": 1,
    }
