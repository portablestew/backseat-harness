"""Daemon (daemon.py): single-instance guard, discovery scan, workspace check-in.

Hermetic by construction: the autouse home pin lands the registry under
fake_home, an autouse guard makes any consult of the real psutil fail the
test, every spawn is a spy (nothing is ever launched), and deadlines ride a
fake clock. Discovery, check-in, and the file watches (goal:54-62) are driven
directly rather than through the pump, so no test sleeps an interval; the
watcher threads that do start (a workspace's files, the registry, a live
task's pid) are joined by the fixtures, with their seams faked or polled at
a 10ms test cadence.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from backseat_harness import db
from backseat_harness.daemon import DB_WATCH_KEY, Daemon, checkin_job, file_watch_job
from backseat_harness.scheduler import Scheduler


class FakeClock:
    """A clock nothing advances unless a test says so."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


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
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def daemon(clock: FakeClock) -> Daemon:
    """A daemon on a fake clock that never claims a guard unless asked to.

    ``file_interval`` drops the file watchers to a 10ms cadence so a test can
    observe a poll turn-around without waiting out the production 5s.
    """
    instance = Daemon(
        sched=Scheduler(clock=clock), pid=777, start=5000.0, file_interval=0.01
    )
    yield instance
    instance.stop()
    assert not any(t.is_alive() for t in instance._sched._watchers), "watcher leaked"


@pytest.fixture
def spawns(monkeypatch):
    """A Popen spy: records ``(argv, kwargs)``; no process is ever launched."""
    records: list[tuple[list[str], dict]] = []

    def fake_popen(argv, **kwargs):
        records.append((list(argv), kwargs))
        return object()

    monkeypatch.setattr("backseat_harness.daemon.subprocess.Popen", fake_popen)
    return records


def _register(workspace: Path) -> str:
    """List the workspace in the registry; returns its stored (resolved) form."""
    with db.open_db() as conn:
        db.add_workspace(conn, workspace)
    return str(workspace.resolve())


def _lane(tasks: Path, slug: str, *, state: dict | None, mtime: float) -> Path:
    """One lane, all of its files pinned to ``mtime`` (old enough to be quiet)."""
    lane = tasks / slug
    lane.mkdir(parents=True)
    (lane / "goal.md").write_text("Do the thing.\n", encoding="utf-8")
    if state is not None:
        (lane / "state.json").write_text(json.dumps(state), encoding="utf-8")
    for path in lane.iterdir():
        os.utime(path, (mtime, mtime))
    return lane


def _wait_until(predicate, what: str, timeout: float = 5.0) -> None:
    """Poll for something only a watcher thread (on its own schedule) does."""
    limit = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < limit, f"timed out waiting for {what}"
        time.sleep(0.005)


class PumpHarness:
    """The daemon's pump on a background thread, with recorded (capped) waits.

    Mirrors tests/test_scheduler.py's harness (each test file here is
    self-contained): entering waits for the pump's first sleep — the sync
    point a test arranges its wake against — ``timeouts`` records the timeout
    the pump *asked* for (before the cap), and the exit always stops the pump
    and asserts nothing leaked.
    """

    WAKE_CAP_S = 30.0

    def __init__(self, clock: FakeClock) -> None:
        self.sched = Scheduler(clock=clock)
        self.timeouts: list[float | None] = []
        self._entries = 0
        self.first_sleep = threading.Event()
        self.woke = threading.Event()
        inner = self.sched._sleep

        def wait(timeout: float | None) -> bool:
            self.timeouts.append(timeout)
            self._entries += 1
            (self.first_sleep if self._entries == 1 else self.woke).set()
            if timeout is None:
                return inner(None)
            return inner(min(timeout, self.WAKE_CAP_S))

        self.sched._wait = wait
        self.thread = threading.Thread(target=self.sched.run, daemon=True)

    def __enter__(self) -> PumpHarness:
        self.thread.start()
        assert self.first_sleep.wait(5.0), "pump never reached its first sleep"
        return self

    def __exit__(self, *exc) -> None:
        self.sched.stop()
        self.thread.join(timeout=5.0)
        assert not self.thread.is_alive(), "pump ignored stop()"
        assert not any(t.is_alive() for t in self.sched._watchers), "watcher leaked"


# --- single-instance guard (goal:15-16, 2-plan §3) ---------------------------


def test_a_second_daemon_start_exits_2(monkeypatch, capsys):
    from backseat_harness.cli import main

    with db.open_db() as conn:
        db.claim_daemon(conn, 111, 1000.0)
    # Pid 111 is live at its recorded start: that is the running daemon. Our
    # own start is faked so the claim never needs psutil before it refuses.
    monkeypatch.setattr(db, "_process", lambda pid: FakeProc(1000.0))
    monkeypatch.setattr(db, "process_start", lambda pid: 5000.0)

    assert main(["daemon", "run"]) == 2
    assert "already running" in capsys.readouterr().out
    # The refusal touched nothing: the running instance's record survives.
    with db.open_db() as conn:
        record = json.loads(db.kv_get(conn, db.DAEMON_KEY))
    assert record == {"pid": 111, "start": 1000.0}


def test_two_racing_daemon_starts_leave_exactly_one_winner(monkeypatch):
    procs = {111: FakeProc(1000.0), 222: FakeProc(2000.0)}
    monkeypatch.setattr(db, "_process", lambda pid: procs[pid])
    daemons = [
        Daemon(sched=Scheduler(clock=FakeClock()), pid=111, start=1000.0),
        Daemon(sched=Scheduler(clock=FakeClock()), pid=222, start=2000.0),
    ]
    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[BaseException] = []

    def try_claim(d: Daemon) -> None:
        try:
            barrier.wait(timeout=5)
            d.claim()
        except db.DaemonAlreadyRunning:
            results.append("lost")
        except BaseException as exc:  # noqa: BLE001 - surfaced via errors below
            errors.append(exc)
        else:
            results.append("won")

    threads = [threading.Thread(target=try_claim, args=(d,)) for d in daemons]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not any(t.is_alive() for t in threads), "claim threads hung"
    assert errors == []
    # Exactly one claim lands; the loser rolled back rather than clobbering it.
    assert sorted(results) == ["lost", "won"]
    with db.open_db() as conn:
        record = json.loads(db.kv_get(conn, db.DAEMON_KEY))
    assert record["pid"] in (111, 222)


def test_a_stale_guard_row_is_reclaimed_by_the_next_starter(monkeypatch):
    with db.open_db() as conn:
        db.claim_daemon(conn, 999, 1000.0)

    def _dead(pid):
        raise db.psutil.NoSuchProcess(pid)

    monkeypatch.setattr(db, "_process", _dead)
    Daemon(sched=Scheduler(clock=FakeClock()), pid=111, start=2000.0).claim()

    with db.open_db() as conn:
        record = json.loads(db.kv_get(conn, db.DAEMON_KEY))
    assert record == {"pid": 111, "start": 2000.0}


def test_run_claims_before_pumping_and_clears_the_guard_on_the_way_out():
    """The ``finally``-clear (2-plan §3): claimed while pumping, gone after."""
    seen: list[dict | None] = []

    def wait(timeout: float | None) -> bool:
        # Inside the pump's first sleep the claim must already be on disk...
        with db.open_db() as conn:
            raw = db.kv_get(conn, db.DAEMON_KEY)
        seen.append(json.loads(raw) if raw else None)
        daemon.stop()
        return True

    daemon = Daemon(sched=Scheduler(clock=FakeClock(), wait=wait), pid=777, start=5000.0)
    daemon.run()

    assert seen == [{"pid": 777, "start": 5000.0}]
    # ...and after run() returns, nothing is left holding the guard.
    with db.open_db() as conn:
        assert db.kv_get(conn, db.DAEMON_KEY) is None


# --- discovery (goal:47-53) ---------------------------------------------------


def test_discovery_early_outs_when_the_version_is_unchanged(
    workspace: Path, daemon: Daemon, clock: FakeClock, monkeypatch
):
    ws = _register(workspace)
    scanned: list[str] = []
    real_list = db.list_workspaces

    def spy(conn):
        scanned.append(ws)
        return real_list(conn)

    monkeypatch.setattr(db, "list_workspaces", spy)

    daemon.discover()
    assert scanned == [ws]
    assert daemon._sched._jobs["discovery"].deadline == clock() + 1800
    # Adopting queues the workspace for an immediate check-in (goal:51).
    assert daemon._sched._jobs[checkin_job(ws)].deadline == clock()

    clock.advance(60)
    daemon.discover()
    # Equal version: no rescan, no re-adoption — but the next pass is armed
    # all the same (a fired one-shot must never strand discovery, goal:53).
    assert scanned == [ws]
    assert daemon._sched._jobs["discovery"].deadline == clock() + 1800
    assert daemon._sched._jobs[checkin_job(ws)].deadline == 1000.0


def test_discovery_adopts_new_and_drops_unregistered_workspaces(
    workspace: Path, daemon: Daemon, clock: FakeClock
):
    daemon.discover()
    assert daemon._workspaces == set()

    ws = _register(workspace)
    daemon.discover()
    assert daemon._workspaces == {ws}
    assert daemon._sched._jobs[checkin_job(ws)].deadline == clock()
    # Adoption also starts the workspace's file watch (goal:54-58).
    adopted = daemon._sched._file_watchers[file_watch_job(ws)]
    assert adopted["paths"] and adopted["on_change"] is not None

    with db.open_db() as conn:
        assert db.unregister_workspace(conn, workspace) is True
    daemon.discover()
    # Dropped: no further scheduling for it (goal:50); an in-flight run under
    # it would finish on its own task-row lifecycle (2-plan §5).
    assert daemon._workspaces == set()
    assert checkin_job(ws) not in daemon._sched._jobs
    # The watch retires with it: empty path list, callback gone.
    retired = daemon._sched._file_watchers[file_watch_job(ws)]
    assert retired["paths"] == []
    assert retired["on_change"] is None


# --- workspace check-in (goal:32-46) -------------------------------------------


def test_a_quiet_workspace_backgrounds_the_oldest_runnable_lane(
    workspace: Path, daemon: Daemon, clock: FakeClock, spawns
):
    ws = _register(workspace)
    tasks = workspace / ".backseat" / "tasks"
    base = time.time()
    # Oldest of all, and ineligible: blocked lanes are never picked (goal:42).
    _lane(tasks, "blocked", state={"slug": "blocked", "status": "blocked"}, mtime=base - 600)
    # The winner: no state yet, goal.md the oldest stamp (goal:42).
    _lane(tasks, "old", state=None, mtime=base - 500)
    # Eligible but newer: continue-state lanes are evaluated, then lose on age.
    _lane(tasks, "mid", state={"slug": "mid", "status": "continue"}, mtime=base - 450)
    _lane(tasks, "new", state={"slug": "new", "status": "continue"}, mtime=base - 100)
    # goal:45 appends: a prior daemon.log must survive the spawn untouched.
    prior = tasks / "old" / "daemon.log"
    prior.write_text("prior\n", encoding="utf-8")
    os.utime(prior, (base - 400, base - 400))

    daemon.discover()
    assert checkin_job(ws) in daemon._sched._jobs
    daemon.check_in(ws)

    assert len(spawns) == 1
    argv, kwargs = spawns[0]
    assert argv[-2:] == ["run", "old"]
    assert "-C" in argv and ws in argv
    assert kwargs["cwd"] == ws
    assert kwargs["stderr"] == subprocess.STDOUT
    assert Path(kwargs["stdout"].name) == prior
    assert prior.read_text(encoding="utf-8") == "prior\n"
    # goal:46: next scan at now+30s, when the new run's pid should be visible.
    assert daemon._sched._jobs[checkin_job(ws)].deadline == clock() + 30


def test_recent_file_activity_defers_the_start_by_thirty_seconds(
    workspace: Path, daemon: Daemon, clock: FakeClock, spawns
):
    ws = _register(workspace)
    tasks = workspace / ".backseat" / "tasks"
    _lane(tasks, "fresh", state=None, mtime=time.time())

    daemon.discover()
    daemon.check_in(ws)

    # Quiescence gate (goal:40-41): files moved a moment ago, so nothing is
    # launched and the workspace re-checks in now+30s instead.
    assert spawns == []
    assert daemon._sched._jobs[checkin_job(ws)].deadline == clock() + 30


def test_a_live_task_earlys_out_and_registers_one_pid_watcher(
    workspace: Path, daemon: Daemon, clock: FakeClock, spawns, monkeypatch
):
    ws = _register(workspace)
    with db.open_db() as conn:
        db.claim_task(conn, workspace, ".backseat/tasks/busy", 4242, 1000.0)
    monkeypatch.setattr(db, "_process", lambda pid: FakeProc(1000.0))

    daemon.discover()
    daemon.check_in(ws)
    daemon.check_in(ws)  # the watcher registration is idempotent (goal:37)

    # Busy workspaces never auto-start anything (goal:34-38)...
    assert spawns == []
    # ...the live pid is watched exactly once, at its recorded start...
    assert list(daemon._sched._pid_watchers) == [(4242, 1000.0)]
    # ...and the check-in falls back to the default cadence (goal:31); the
    # watcher's wake, not this deadline, brings the workspace back early.
    assert daemon._sched._jobs[checkin_job(ws)].deadline == clock() + 1800
    daemon.stop()  # join the watcher while its pid seam is still faked


def test_an_orphaned_provider_is_swept_so_the_lane_can_start(
    workspace: Path, daemon: Daemon, clock: FakeClock, spawns, monkeypatch
):
    """A dead harness's live provider must not pin the workspace busy forever.

    The row would otherwise read as busy and the pre-run sweep (the only
    killer) is unreachable behind that gate: the check-in sweeps first, so
    the orphan dies, the row goes, and the lane starts.
    """
    ws = _register(workspace)
    tasks = workspace / ".backseat" / "tasks"
    _lane(
        tasks,
        "resume",
        state={"slug": "resume", "status": "continue"},
        mtime=time.time() - 500,
    )
    with db.open_db() as conn:
        db.claim_task(conn, workspace, ".backseat/tasks/resume", 4242, 1000.0)
        db.record_provider(conn, workspace, ".backseat/tasks/resume", [9999])

    def _process(pid):
        if pid == 4242:
            raise db.psutil.NoSuchProcess(pid)
        return FakeProc(1000.0)

    monkeypatch.setattr(db, "_process", _process)
    killed: list[int] = []
    monkeypatch.setattr(db.spawn, "kill_pid_tree", killed.append)

    daemon.discover()
    daemon.check_in(ws)

    assert killed == [9999]
    with db.open_db() as conn:
        assert db.list_tasks(conn) == []
    assert len(spawns) == 1
    assert spawns[0][0][-2:] == ["run", "resume"]
    assert daemon._sched._jobs[checkin_job(ws)].deadline == clock() + 30


def test_a_recycled_provider_pid_is_swept_without_killing(
    workspace: Path, daemon: Daemon, clock: FakeClock, spawns, monkeypatch
):
    """The row must not pin the workspace busy, and the stranger that now
    owns the recorded pid must survive: pid+start reads it dead."""
    ws = _register(workspace)
    tasks = workspace / ".backseat" / "tasks"
    _lane(
        tasks,
        "resume",
        state={"slug": "resume", "status": "continue"},
        mtime=time.time() - 500,
    )
    with db.open_db() as conn:
        db.claim_task(conn, workspace, ".backseat/tasks/resume", 4242, 1000.0)
        db.record_provider(conn, workspace, ".backseat/tasks/resume", [9999], [1000.0])

    def _process(pid):
        if pid == 4242:
            raise db.psutil.NoSuchProcess(pid)
        return FakeProc(9999.0)  # same pid, different start: recycled

    monkeypatch.setattr(db, "_process", _process)
    killed: list[int] = []
    monkeypatch.setattr(db.spawn, "kill_pid_tree", killed.append)

    daemon.discover()
    daemon.check_in(ws)

    assert killed == []
    with db.open_db() as conn:
        assert db.list_tasks(conn) == []
    assert len(spawns) == 1
    assert spawns[0][0][-2:] == ["run", "resume"]


# --- file watching (goal:54-62, 2-plan §4) --------------------------------------


def test_a_goal_edit_resets_the_workspace_check_in_and_wakes_the_pump(
    workspace: Path, clock: FakeClock, spawns
):
    """goal:55-58 — a ``goal.md`` mtime move resets that workspace's check-in
    to now+30s, and the wake recomputes the pump's sleep to exactly that."""
    ws = _register(workspace)
    tasks = workspace / ".backseat" / "tasks"
    base = time.time()
    # The shape of a lane but an empty goal: the first check-in falls back to
    # the default cadence instead of spawning, so the only wake under test is
    # the file watcher's.
    scaffold = tasks / "scaffold"
    scaffold.mkdir(parents=True)
    goal = scaffold / "goal.md"
    goal.write_text("", encoding="utf-8")
    os.utime(goal, (base - 500, base - 500))

    h = PumpHarness(clock)
    daemon = Daemon(sched=h.sched, pid=777, start=5000.0, file_interval=0.01)
    try:
        daemon.discover()
        daemon.check_in(ws)  # run the adoption's immediate check-in up front
        entry = h.sched._file_watchers[file_watch_job(ws)]
        _wait_until(lambda: entry["snapshot"] is not None, "the watch baseline")

        with h:
            # Nearest deadline is 30 minutes out (discovery sits at the same
            # mark): the pump sleeps toward it with nothing due.
            assert h.timeouts == [1800.0]
            baseline = goal.stat().st_mtime
            # One pinned utime — a single move, so the watcher reports exactly
            # one change and the recorded sleeps stay deterministic.
            os.utime(goal, (baseline + 10, baseline + 10))
            assert h.woke.wait(5.0), "the goal edit did not wake the pump"

        assert h.timeouts[0] == 1800.0
        assert h.timeouts[1] == 30.0  # recomputed from now+30, not resumed
        assert daemon._sched._jobs[checkin_job(ws)].deadline == clock() + 30
        assert spawns == []
    finally:
        daemon.stop()


def test_a_registry_edit_marks_discovery_due_now(daemon: Daemon, clock: FakeClock):
    """goal:60-62 — a ``db.sqlite`` mtime move brings the next discovery to
    now; re-registering the watch across passes starts no second thread."""
    with db.open_db():  # the registry exists before the watch baselines it
        pass

    daemon.discover()
    assert daemon._sched._jobs["discovery"].deadline == clock() + 1800
    assert len(daemon._sched._watchers) == 1
    daemon.discover()  # same key: updated in place, still one thread
    assert daemon._sched._jobs["discovery"].deadline == clock() + 1800
    assert len(daemon._sched._watchers) == 1

    entry = daemon._sched._file_watchers[DB_WATCH_KEY]
    _wait_until(lambda: entry["snapshot"] is not None, "the registry baseline")
    path = db.db_path()
    baseline = path.stat().st_mtime
    os.utime(path, (baseline + 10, baseline + 10))
    _wait_until(
        lambda: daemon._sched._jobs["discovery"].deadline == clock(),
        "discovery to be marked due",
    )
    assert daemon._sched._jobs["discovery"].deadline == clock()


def test_the_workspace_watch_is_filtered_to_goal_and_job_files(
    workspace: Path, daemon: Daemon, clock: FakeClock
):
    """goal:55 — only ``goal.md``/``job.toml`` edits (and the directories
    holding them) reset the cadence; a ``daemon.log`` append is dropped."""
    ws = _register(workspace)
    tasks = Path(ws) / ".backseat" / "tasks"
    scaffold = tasks / "scaffold"
    scaffold.mkdir(parents=True)
    goal = scaffold / "goal.md"
    goal.write_text("", encoding="utf-8")
    log = scaffold / "daemon.log"
    log.write_text("noisy\n", encoding="utf-8")

    daemon.discover()
    entry = daemon._sched._file_watchers[file_watch_job(ws)]
    names = {Path(path).name for path in entry["paths"]}
    # The set is goal.md/job.toml plus the two directories — never the logs.
    assert "goal.md" in names
    assert "daemon.log" not in names
    assert str(tasks) in entry["paths"]
    assert str(Path(ws) / ".backseat" / "jobs") in entry["paths"]

    # Adoption already armed the immediate check-in (goal:51)...
    assert daemon._sched._jobs[checkin_job(ws)].deadline == clock()
    on_change = entry["on_change"]
    # ...a filtered change leaves it exactly where adoption put it...
    on_change([str(log)])
    assert daemon._sched._jobs[checkin_job(ws)].deadline == clock()
    # ...and a goal.md move resets it to now+30s (goal:55-58).
    on_change([str(goal)])
    assert daemon._sched._jobs[checkin_job(ws)].deadline == clock() + 30
