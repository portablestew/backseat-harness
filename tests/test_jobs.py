"""Triggered jobs (jobs.py + the daemon wiring): schedule math, guard, cleanup, copy.

Hermetic like its siblings: schedule math runs on fake epochs (never a real
wait), one fake clock serves both the scheduler's deadlines and the daemon's
wall seam so the two stay in lockstep, the pump is driven by a scripted
``_wait`` that advances that clock instead of sleeping, every psutil consult
fails loudly, and nothing is ever spawned — the fire protocol is pure files.
Each test file is self-contained (tests/test_scheduler.py's convention), so
the clock, pump helpers, and registry helper are restated here.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backseat_harness import db, jobs, storage
from backseat_harness.config import TaskPaths
from backseat_harness.daemon import Daemon, trigger_job
from backseat_harness.models import TaskState, TaskStatus
from backseat_harness.scheduler import Scheduler

GOAL_TEXT = "Do the thing.\n"


class FakeClock:
    """A clock nothing advances unless a test says so."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


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
    """A daemon whose wall seam shares the scheduler's fake clock (lockstep)."""
    instance = Daemon(
        sched=Scheduler(clock=clock),
        pid=777,
        start=5000.0,
        file_interval=0.01,
        wall=clock,
    )
    yield instance
    instance.stop()
    assert not any(t.is_alive() for t in instance._sched._watchers), "watcher leaked"


def _register(workspace: Path) -> str:
    """List the workspace in the registry; returns its stored (resolved) form."""
    with db.open_db() as conn:
        db.add_workspace(conn, workspace)
    return str(workspace.resolve())


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()


def _write_job(workspace: Path, slug: str, *, spec: str | None = None) -> Path:
    job_dir = workspace / ".backseat" / "jobs" / slug
    job_dir.mkdir(parents=True)
    (job_dir / "goal.md").write_text(GOAL_TEXT, encoding="utf-8")
    if spec is not None:
        (job_dir / "job.toml").write_text(spec, encoding="utf-8")
    return job_dir


def _lane(tasks: Path, name: str, *, status: TaskStatus | None = None) -> Path:
    """A destination lane; ``status=None`` leaves no state.json at all."""
    lane = tasks / name
    lane.mkdir(parents=True)
    (lane / "goal.md").write_text(GOAL_TEXT, encoding="utf-8")
    if status is not None:
        _complete(lane, status=status)
    return lane


def _complete(lane: Path, *, status: TaskStatus = TaskStatus.COMPLETE) -> None:
    storage.save_state(TaskPaths(lane), TaskState(slug=lane.name, status=status))


def _spy_fire(monkeypatch, daemon: Daemon) -> list[Path]:
    """Record each fire as it happens and stop the pump on the first one."""
    fired: list[Path] = []
    real_fire = jobs.fire

    def spy(workspace, slug, **kwargs):
        lane = real_fire(workspace, slug, **kwargs)
        fired.append(lane)
        daemon.stop()
        return lane

    monkeypatch.setattr(jobs, "fire", spy)
    return fired


# --- schedule registration and firing (goal:64-67, 2-plan §6) ------------------


def test_a_schedule_fires_once_and_rearms_for_the_next_occurrence(
    workspace: Path, daemon: Daemon, clock: FakeClock, monkeypatch
):
    _write_job(workspace, "nightly", spec='schedule = "interval"\ninterval = "1h"\n')
    ws = _register(workspace)
    daemon.discover()  # adopt, with its immediate check-in
    daemon.check_in(ws)  # goal:64-66: the job's next fire lands on the pump
    job_id = trigger_job(ws, "nightly")
    # Wall 1000 is inside the 0..3600 interval, so the first boundary is 3600.
    assert daemon._sched._jobs[job_id].deadline == 3600.0

    fired = _spy_fire(monkeypatch, daemon)
    timeouts: list[float | None] = []

    def advancing_wait(timeout: float | None) -> bool:
        # The pump's sleep is always deadline - now; advance by exactly that.
        assert timeout is not None, "pump idled with deadlines pending"
        timeouts.append(timeout)
        clock.advance(timeout)
        return False

    daemon._sched._wait = advancing_wait
    daemon._sched.run()

    assert len(fired) == 1 and fired[0] is not None
    tasks = workspace / ".backseat" / "tasks"
    assert sorted(tasks.glob("job-nightly-*")) == [fired[0]]
    # One fire, then the re-arm is the NEXT boundary: the pump would sleep
    # 3600 more rather than re-fire at the timestamp it just ran.
    assert daemon._sched._jobs[job_id].deadline == 7200.0
    assert timeouts == [1800.0, 800.0]  # to the check-in, then to the fire


def test_a_downtime_gap_fires_once_instead_of_catching_up(
    workspace: Path, daemon: Daemon, clock: FakeClock, monkeypatch
):
    """goal:67 — the daemon was down across two intervals; neither is replayed."""
    _write_job(workspace, "nightly", spec='schedule = "interval"\ninterval = "1h"\n')
    ws = _register(workspace)
    daemon.discover()
    daemon.check_in(ws)
    job_id = trigger_job(ws, "nightly")
    assert daemon._sched._jobs[job_id].deadline == 3600.0

    fired = _spy_fire(monkeypatch, daemon)
    clock.advance(10000)  # downtime: 3600 and 7200 both pass with no pump

    def no_sleep(timeout: float | None) -> bool:
        raise AssertionError("the due fire must run before the pump sleeps")

    daemon._sched._wait = no_sleep
    daemon._sched.run()

    assert len(fired) == 1 and fired[0] is not None
    tasks = workspace / ".backseat" / "tasks"
    assert sorted(tasks.glob("job-nightly-*")) == [fired[0]]
    # The re-arm is the next future boundary (4 * 3600), never 7200/10800.
    assert daemon._sched._jobs[job_id].deadline == 14400.0


def test_next_fire_skips_missed_occurrences_rather_than_catching_up():
    quoted = jobs.parse_job("daily", 'schedule = "daily"\nat = "06:00"\n')
    native = jobs.parse_job("daily", 'schedule = "daily"\nat = 06:00:00\n')

    def at(day: int, hour: int) -> float:
        # Expectations ride the parsed job's own tz, so they hold on any box.
        return datetime(2026, 1, day, hour, 0, tzinfo=quoted.tz).timestamp()

    assert jobs.next_fire(quoted, at(1, 0)) == at(1, 6)
    assert jobs.next_fire(native, at(1, 0)) == at(1, 6)  # toml's time literal too

    # Down across four 06:00s: the answer is the next one, not the four passed.
    down = at(4, 12)
    assert jobs.next_fire(quoted, down) == at(5, 6)
    assert jobs.next_fire(quoted, down) > down
    # Landing exactly on an occurrence re-arms for the following day (goal:67).
    assert jobs.next_fire(quoted, at(5, 6)) == at(6, 6)


# --- the fire protocol (goal:70-75) -------------------------------------------


def test_a_non_complete_destination_blocks_the_fire(workspace: Path):
    _write_job(workspace, "report")
    tasks = workspace / ".backseat" / "tasks"
    inflight = _lane(tasks, "job-report-20260101-060000")  # no state.json yet
    started = _lane(tasks, "job-report-20260102-060000", status=TaskStatus.CONTINUE)
    moment = _utc(2026, 1, 3, 6)

    assert jobs.fire(workspace, "report", now=moment) is None
    assert sorted(p.name for p in tasks.glob("job-report-*")) == [
        inflight.name,
        started.name,
    ]

    # Completing one is not enough: any single non-complete lane holds the gate.
    _complete(inflight)
    assert jobs.fire(workspace, "report", now=moment) is None

    # All complete -> the fire lands (goal:70 is a hold-off, not a cancel).
    _complete(started)
    assert jobs.fire(workspace, "report", now=moment) is not None
    assert len(list(tasks.glob("job-report-*"))) == 3


def test_fire_keeps_only_the_newest_three_completed_destinations(workspace: Path):
    _write_job(workspace, "report")
    tasks = workspace / ".backseat" / "tasks"
    old = [
        _lane(tasks, f"job-report-2026010{i}-060000", status=TaskStatus.COMPLETE)
        for i in range(1, 6)
    ]

    new = jobs.fire(workspace, "report", now=_utc(2026, 1, 6, 6))

    assert new is not None
    assert not old[0].exists() and not old[1].exists()  # the oldest two, deleted
    assert all(lane.exists() for lane in old[2:])  # newest three kept (goal:71)
    assert new.exists()
    assert len(list(tasks.glob("job-report-*"))) == 4


def test_goal_md_is_copied_last_and_lands_with_the_newest_mtime(workspace: Path):
    job_dir = _write_job(workspace, "nightly")
    (job_dir / "config.toml").write_text('[models]\nprimary = "opus"\n', encoding="utf-8")
    (job_dir / "checklist.json").write_text('{"steps": {}, "remarks": []}', encoding="utf-8")
    for entry in job_dir.iterdir():
        os.utime(entry, (1000, 1000))  # every source equally old

    dest = jobs.fire(workspace, "nightly", now=_utc(2026, 1, 6, 6))

    assert dest is not None
    assert dest.parent == workspace / ".backseat" / "tasks"
    mtimes = {path.name: path.stat().st_mtime for path in dest.iterdir()}
    assert set(mtimes) == {"goal.md", "config.toml", "checklist.json"}
    # copy2 kept the siblings' pinned mtimes; goal.md, copied last with a plain
    # copy, is the fresh one — the validity signal the picker's scan reads.
    assert mtimes["config.toml"] == 1000
    assert mtimes["checklist.json"] == 1000
    assert mtimes["goal.md"] > 1000
    assert mtimes["goal.md"] == max(mtimes.values())
    # config.toml rides along byte-for-byte (goal:75: the task layer applies it).
    assert (dest / "config.toml").read_text(encoding="utf-8") == (
        '[models]\nprimary = "opus"\n'
    )
    assert (dest / "goal.md").read_text(encoding="utf-8") == GOAL_TEXT


def test_guard_blocks_legitimate_fresh_lane(workspace: Path):
    """A fresh legitimate lane (goal.md present, no state.json) must block."""
    _write_job(workspace, "report")
    tasks = workspace / ".backseat" / "tasks"
    fresh_lane = _lane(tasks, "job-report-20260105-060000")  # no state.json
    # The partial/debris case: a lane with no goal.md should NOT block.
    debris = tasks / "job-report-20260104-060000-debris"
    debris.mkdir(parents=True)
    # No goal.md, no state.json => interrupted debris; must be ignored.
    # A legitimate unfinished lane (fresh) still holds the guard.
    assert jobs.fire(workspace, "report", now=_utc(2026, 1, 6, 6)) is None
    # After the fresh lane completes, the guard releases.
    _complete(fresh_lane)
    # The debris lane (no goal.md) does not block a new fire.
    result = jobs.fire(workspace, "report", now=_utc(2026, 1, 7, 6))
    assert result is not None


def test_partial_lane_without_goal_md_ignored_by_guard(workspace: Path):
    """An interrupted partial copy (dir present, goal.md absent) is debris
    and must not permanently block future fires."""
    _write_job(workspace, "report")
    tasks = workspace / ".backseat" / "tasks"
    # Partial/interrupted copy: dir exists but no goal.md (copied last).
    partial = tasks / "job-report-partial"
    partial.mkdir(parents=True)
    # No goal.md, no state.json => interrupted debris; ignored by guard.
    # A completed lane exists alongside, so fire should succeed.
    complete_lane = _lane(tasks, "job-report-20260101-060000", status=TaskStatus.COMPLETE)
    result = jobs.fire(workspace, "report", now=_utc(2026, 1, 3, 6))
    assert result is not None
    # The partial debris remains but does not block.
    assert partial.exists() and not (partial / "goal.md").exists()


def test_a_timestamp_collision_gets_a_counter_suffix_instead_of_overwriting(
    workspace: Path,
):
    _write_job(workspace, "nightly")
    tasks = workspace / ".backseat" / "tasks"
    now = time.time()  # both fires name themselves from this same second

    first = jobs.fire(workspace, "nightly", now=now)
    assert first is not None and first.exists()
    # The fresh copy has no state yet, so the guard holds until it completes.
    assert jobs.fire(workspace, "nightly", now=now) is None

    _complete(first)
    second = jobs.fire(workspace, "nightly", now=now)
    assert second is not None
    assert second.name == f"{first.name}-2"
    assert first.exists() and (first / "goal.md").read_text(encoding="utf-8") == GOAL_TEXT

    _complete(second)
    third = jobs.fire(workspace, "nightly", now=now)
    assert third is not None and third.name == f"{first.name}-3"
    assert len(list(tasks.glob("job-nightly-*"))) == 3
