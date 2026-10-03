"""Scheduler (scheduler.py): deadline math, semaphore wakes, watcher threads.

A fake clock drives every deadline and scripted waits stand in for real
sleeps, so no test waits an interval out. The semaphore itself is real where
the wake is under test -- another thread or a watcher interrupting the sleep
-- and the pid seam is monkeypatched, so no test consults a real pid. The
scheduler never signals a process, so no test can kill one.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from backseat_harness import db
from backseat_harness.scheduler import Scheduler

#: A real block in the threaded tests is capped at this many seconds: when a
#: wake breaks the test fails in seconds instead of hanging on a 1000s
#: deadline, while a working wake lands far below the cap.
WAKE_CAP_S = 30.0


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


class PumpHarness:
    """The pump on a background thread, with recorded (and capped) waits.

    Entering waits for the pump's first sleep -- the sync point a test uses
    before arranging a wake. ``timeouts`` records the timeout the pump *asked*
    for (before the cap), which is what the deadline assertions read, and the
    context manager always stops the pump and asserts nothing leaked.
    """

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
            return inner(min(timeout, WAKE_CAP_S))

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


def test_a_job_fires_exactly_at_its_deadline():
    """Sleep is ``deadline - now``; the job runs the moment that elapses."""
    clock = FakeClock()
    timeouts: list[float | None] = []

    def advancing_wait(timeout: float | None) -> bool:
        assert timeout is not None, "pump idled with a job still pending"
        timeouts.append(timeout)
        clock.advance(timeout)  # pretend the full sleep elapsed
        return False

    sched = Scheduler(clock=clock, wait=advancing_wait)
    fired: list[float] = []
    sched.schedule("job", clock() + 300, lambda: (fired.append(clock()), sched.stop()))

    sched.run()

    assert fired == [1300.0]
    assert clock.now == 1300.0
    assert timeouts == [300.0]


def test_a_woken_semaphore_fires_early_work_without_sleeping_the_interval():
    """A ``schedule()`` from outside interrupts the sleep: the near job runs
    now, and the far deadline is never waited out."""
    clock = FakeClock()
    far_ran: list[float] = []
    early_ran: list[float] = []
    done = threading.Event()

    h = PumpHarness(clock)
    h.sched.schedule("far", clock() + 300, lambda: far_ran.append(clock()))
    with h:
        # The pump is inside its 300s sleep; a new due job must cut it short.
        h.sched.schedule(
            "early",
            clock(),
            lambda: (early_ran.append(clock()), done.set(), h.sched.stop()),
        )
        assert done.wait(5.0), "the woken pump never ran the early job"

    assert early_ran == [1000.0]
    assert far_ran == []
    assert clock.now == 1000.0  # the 300s interval never elapsed
    assert h.timeouts == [300.0]  # and was never re-entered


def test_pid_completion_wakes_the_pump(monkeypatch):
    """goal:37 -- the awaited process exiting interrupts the sleep, and the
    recomputed deadline is unchanged: the wake ended it, not the clock."""
    clock = FakeClock()
    alive = {"on": True}
    monkeypatch.setattr(db, "pid_live", lambda pid, start=None: alive["on"])

    h = PumpHarness(clock)
    sched = h.sched
    sched.schedule("far", clock() + 1000, lambda: None)
    exited = threading.Event()
    # Idempotent per (pid, start): the check-in re-registers every cycle.
    assert sched.watch_pid(4242, 500.0, on_exit=exited.set, interval=0.01) is True
    assert sched.watch_pid(4242, 500.0, on_exit=exited.set, interval=0.01) is False
    assert len(sched._pid_watchers) == 1

    with h:
        alive["on"] = False  # the watched harness exits
        assert h.woke.wait(5.0), "pid completion did not wake the pump"
        assert exited.is_set()

    assert h.timeouts == [1000.0, 1000.0]
    assert len(sched._pid_watchers) == 0  # the watcher removed itself on exit


def test_sleep_never_exceeds_the_nearest_deadline_including_discovery():
    """goal:53 -- discovery joins the sleep math: with discovery at +60 and a
    check-in at +1000, the first sleep is 60, not 1000; every sleep after is
    exactly the distance to whatever is then nearest."""
    clock = FakeClock()
    timeouts: list[float | None] = []

    def advancing_wait(timeout: float | None) -> bool:
        assert timeout is not None, "pump idled with deadlines pending"
        timeouts.append(timeout)
        clock.advance(timeout)
        return False

    sched = Scheduler(clock=clock, wait=advancing_wait)
    discovery_fires: list[float] = []
    checkin_fires: list[float] = []

    def discovery() -> None:
        discovery_fires.append(clock())
        if len(discovery_fires) == 1:
            sched.schedule("discovery", clock() + 1800, discovery)  # daemon pushes it out
        else:
            sched.stop()

    sched.schedule("discovery", clock() + 60, discovery)
    sched.schedule("checkin", clock() + 1000, lambda: checkin_fires.append(clock()))

    sched.run()

    assert timeouts == [60.0, 940.0, 860.0]
    assert discovery_fires == [1060.0, 2860.0]
    assert checkin_fires == [2000.0]


def test_a_file_change_reschedules_the_deadline_and_wakes_the_pump(tmp_path):
    """goal:54-58 -- a ``goal.md`` edit runs the on-change callback, which
    reschedules the check-in to now+30; the wake recomputes the sleep to
    exactly that instead of the old deadline."""
    clock = FakeClock()
    goal = tmp_path / "goal.md"
    goal.write_text("one\n", encoding="utf-8")

    h = PumpHarness(clock)
    sched = h.sched
    rescheduled_to: list[float] = []
    changed_paths: list[list[str]] = []

    def on_change(changed: list[str]) -> None:
        changed_paths.append(changed)
        assert sched.reschedule("checkin", clock() + 30) is True
        rescheduled_to.append(clock())

    sched.schedule("checkin", clock() + 300, lambda: None)
    assert sched.watch_files("ws", [goal], on_change, interval=0.01) is True
    # Discovery re-registering the same key updates in place, no 2nd thread.
    assert sched.watch_files("ws", [goal], on_change, interval=0.01) is False
    assert len(sched._watchers) == 1

    baseline_by = time.monotonic() + 5.0
    while sched._file_watchers["ws"]["snapshot"] is None:
        assert time.monotonic() < baseline_by, "file watcher never took its baseline"
        time.sleep(0.005)

    with h:
        assert h.timeouts[0] == 300.0  # sleeping toward the original deadline
        baseline = goal.stat().st_mtime
        goal.write_text("two\n", encoding="utf-8")
        # Pin the edit past the baseline so the assertion cannot hinge on the
        # filesystem's timestamp granularity.
        os.utime(goal, (baseline + 10, baseline + 10))
        assert h.woke.wait(5.0), "file change did not wake the pump"

    assert changed_paths == [[str(goal)]]
    assert rescheduled_to == [1000.0]
    assert h.timeouts[1] == 30.0  # recomputed: the rescheduled check-in


def test_duplicate_job_ids_are_rejected_and_cancel_is_a_plain_unsubscribe():
    """goal:25's uniqueness, plus the moves a caller has instead of a
    duplicate: reschedule (pending) and cancel (any)."""
    sched = Scheduler(clock=FakeClock())
    sched.schedule("j", 1000.0, lambda: None)
    with pytest.raises(ValueError, match="already scheduled"):
        sched.schedule("j", 1200.0, lambda: None)
    assert sched.reschedule("j", 1300.0) is True
    assert sched.cancel("j") is True
    assert sched.cancel("j") is False
    assert sched.reschedule("j", 1400.0) is False
