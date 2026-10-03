"""Deadline scheduler: one pump, one semaphore, watcher threads.

Jobs (goal:25-29): ``schedule(job_id, deadline, callback)`` under a unique id,
with an absolute deadline on an injected clock. The pump runs on the thread
that calls :meth:`run` -- the daemon's main thread, per goal:26 -- and normally
sleeps on a semaphore until the earliest deadline (goal:28). The sleep is
``deadline - now``, so it can never outlast the nearest job: workspace
check-ins, cron fires (step 9 computes those deadlines; this module never
parses cron), and *next workspace discovery*, which joins the sleep math
deliberately so a far-off check-in cannot starve it (goal:53).

Every wake makes the pump recompute deadlines instead of resuming a stale
sleep (goal:29):

* ``schedule``/``reschedule``/``cancel`` from any thread -- a newly
  registered earlier deadline must shorten the current sleep;
* pid watchers, one per awaited live task and idempotent per (pid, start)
  because the check-in re-registers it every cycle (goal:37): process exit
  wakes the pump so the workspace is re-checked promptly rather than at the
  default cadence;
* file watchers -- stdlib mtime polling, no watchdog (research §4, ratified
  in 2-plan §4): the on-change callback reschedules that workspace's
  check-in or marks discovery due (goal:54-62), then the wake carries the
  recomputed deadline through.

Periodic work is an ordinary job re-registered from inside its own callback:
firing removes the job first (one-shot semantics), so a callback that
reschedules repeats and one that does nothing leaves no residue.

Time comes from ``clock`` (default ``time.monotonic``); tests inject a fake
and never sleep a real deadline. Pid liveness goes through ``db.pid_live``'s
seam, so no test needs a real pid, and nothing here ever signals a process --
killing is the sweep's job, not the scheduler's. ``_wait`` is the sleep seam
tests replace to script wakes.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from backseat_harness import db

#: A pid watcher re-checks liveness this often -- a poll, not a spin. The wake
#: only moves a check-in forward, and goal:37 asks for presence, not
#: microsecond latency.
PID_POLL_S = 1.0

#: File watchers re-stat this often (stdlib mtime polling, 2-plan §4).
FILE_POLL_S = 5.0

#: ``stop()`` waits this long for a watcher thread to exit. Watchers block on
#: the stop event rather than a bare sleep, so they notice immediately; the
#: timeout is a backstop against a thread stuck inside a stat/liveliness call.
STOP_JOIN_S = 5.0

JobCallback = Callable[[], None]
FileChangeCallback = Callable[[list[str]], None]


@dataclass(frozen=True)
class _Job:
    job_id: str
    deadline: float
    callback: JobCallback


def _mtimes(paths: Iterable[str]) -> dict[str, float | None]:
    """mtime per path, ``None`` when absent -- missing *is* a state, so a
    deletion registers as a change like any edit does."""
    snapshot: dict[str, float | None] = {}
    for path in paths:
        try:
            snapshot[path] = Path(path).stat().st_mtime
        except OSError:
            snapshot[path] = None
    return snapshot


class Scheduler:
    """Deadline jobs pumped from one thread, woken over a semaphore."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wait: Callable[[float | None], bool] | None = None,
    ) -> None:
        self._clock = clock
        self._wait = self._sleep if wait is None else wait
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.Lock()
        self._wake = threading.Semaphore(0)
        self._stop = threading.Event()
        self._watchers: list[threading.Thread] = []
        self._pid_watchers: dict[tuple[int, float | None], dict] = {}
        self._file_watchers: dict[str, dict] = {}

    def now(self) -> float:
        """The current time on the clock deadlines are expressed in."""
        return self._clock()

    def schedule(self, job_id: str, deadline: float, callback: JobCallback) -> None:
        """Register a job under a unique id and wake the pump to re-sleep.

        The id must be unique (goal:25): one pending job per id, so a caller
        that wants to move a deadline says so with :meth:`reschedule` instead
        of silently replacing a callback the daemon may still expect to run.
        """
        job = _Job(job_id, float(deadline), callback)
        with self._lock:
            if job_id in self._jobs:
                raise ValueError(
                    f"job {job_id!r} is already scheduled; use reschedule() to move it"
                )
            self._jobs[job_id] = job
        self.wake()

    def reschedule(self, job_id: str, deadline: float) -> bool:
        """Move a pending job's deadline; False when it is no longer pending.

        False is a race, not a bug: the job may have fired between the
        caller's decision and this call (or never existed), and re-arming a
        fired one-shot from here would resurrect it -- the callback that wants
        to repeat re-schedules itself. The wake rides along only when the
        move landed.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            self._jobs[job_id] = _Job(job_id, float(deadline), job.callback)
        self.wake()
        return True

    def cancel(self, job_id: str) -> bool:
        """Drop a pending job; True iff one existed."""
        with self._lock:
            return self._jobs.pop(job_id, None) is not None

    def wake(self) -> None:
        """Release the semaphore: the pump recomputes its deadlines now."""
        self._wake.release()

    def stop(self) -> None:
        """Stop the pump and join the watcher threads; idempotent.

        Safe to call from a job callback (the pump joins only threads, never
        itself) and from a signal handler on another thread.
        """
        self._stop.set()
        self._wake.release()
        for thread in list(self._watchers):
            if thread is not threading.current_thread():
                thread.join(timeout=STOP_JOIN_S)

    def run(self) -> None:
        """Pump jobs until :meth:`stop`; call it on the thread that owns the loop.

        Each pass: fire everything due, then sleep ``nearest deadline - now``
        on the semaphore -- or until a watcher/scheduler call releases it,
        whichever comes first. Both paths land on the same recompute, so an
        early wake can never make the pump skip or double-fire a deadline.
        """
        while self._wake.acquire(blocking=False):
            pass  # wakes posted before the pump started are subsumed by the first scan
        while not self._stop.is_set():
            now = self._clock()
            with self._lock:
                due = sorted(
                    (job for job in self._jobs.values() if job.deadline <= now),
                    key=lambda job: (job.deadline, job.job_id),
                )
                for job in due:
                    self._jobs.pop(job.job_id, None)
            for job in due:
                try:
                    job.callback()
                except Exception as exc:  # noqa: BLE001 - a dead job must not kill the pump
                    logging.getLogger("daemon").warning(
                        "scheduler: job %r raised %r", job.job_id, exc
                    )
            if self._stop.is_set():
                break
            with self._lock:
                next_deadline = min(
                    (job.deadline for job in self._jobs.values()), default=None
                )
            timeout = (
                None
                if next_deadline is None
                else max(0.0, next_deadline - self._clock())
            )
            self._wait(timeout)

    def _sleep(self, timeout: float | None) -> bool:
        """Block on the semaphore up to ``timeout``; True when woken early.

        A successful acquire drains any piled-up releases, so a burst of wakes
        (file change plus the reschedule it triggered, say) collapses into one
        recompute instead of a chain of no-op spins.
        """
        woke = self._wake.acquire(timeout=timeout)
        if woke:
            while self._wake.acquire(blocking=False):
                pass
        return woke

    # --- watchers ----------------------------------------------------------

    def watch_pid(
        self,
        pid: int,
        start: float | None = None,
        *,
        on_exit: JobCallback | None = None,
        interval: float = PID_POLL_S,
    ) -> bool:
        """Poll one process until it exits, then run ``on_exit`` and wake the
        pump (goal:37).

        Idempotent per ``(pid, start)`` -- the workspace check-in registers
        the same live task every cycle and must not stack watchers (the pair,
        never the pid alone: a reused pid is a different process, which is the
        whole reason start times are stored). Returns False when already
        watched or the scheduler is stopping; True when a new watcher started.
        """
        key = (int(pid), None if start is None else float(start))
        with self._lock:
            if self._stop.is_set() or key in self._pid_watchers:
                return False
            entry = {"pid": int(pid), "start": start, "on_exit": on_exit}
            self._pid_watchers[key] = entry
        self._start_watcher(self._pid_loop, key, entry, interval)
        return True

    def watch_files(
        self,
        key: str,
        paths: Iterable[Path | str],
        on_change: FileChangeCallback | None = None,
        *,
        interval: float = FILE_POLL_S,
    ) -> bool:
        """Poll ``paths``' mtimes; on any change run ``on_change(changed)``
        and wake the pump (goal:54-62).

        Stdlib mtime polling on purpose (2-plan §4): a period of detection lag
        is within the design's budget, and it adds no dependency and no
        second contention source over files ``storage.py`` already retries to
        write. Re-registering an existing ``key`` (discovery re-adopting a
        workspace) updates the watched paths and callback in place instead of
        stacking a second poll thread. Returns False when already watched or
        stopping; True when a new watcher started.
        """
        watched = [str(path) for path in paths]
        with self._lock:
            if self._stop.is_set():
                return False
            existing = self._file_watchers.get(key)
            if existing is not None:
                existing["paths"] = watched
                existing["on_change"] = on_change
                return False
            entry: dict = {"paths": watched, "on_change": on_change, "snapshot": None}
            self._file_watchers[key] = entry
        self._start_watcher(self._file_loop, key, entry, interval)
        return True

    def _start_watcher(self, target: Callable, *args) -> None:
        # C — durable: thread count = max unique workspace keys ever adopted,
        # not cycle count; same key updates in place, no accumulation.
        thread = threading.Thread(target=target, args=args, daemon=True)
        with self._lock:
            self._watchers.append(thread)
        thread.start()

    def _pid_loop(self, key: tuple, entry: dict, interval: float) -> None:
        # Liveliness resolves through db.pid_live at call time, so tests swap
        # that seam and no real pid is ever consulted.
        while not self._stop.is_set():
            if not db.pid_live(entry["pid"], entry["start"]):
                break
            self._stop.wait(interval)
        with self._lock:
            self._pid_watchers.pop(key, None)
        if self._stop.is_set():
            return  # shutdown: no callback, no wake -- stop() already woke the pump
        on_exit = entry["on_exit"]
        if on_exit is not None:
            try:
                on_exit()
            except Exception as exc:  # noqa: BLE001 - a dead callback must not kill the wake
                logging.getLogger("daemon").warning(
                    "scheduler: pid watcher for %s raised %r", key[0], exc
                )
        self.wake()

    def _file_loop(self, key: str, entry: dict, interval: float) -> None:
        while not self._stop.is_set():
            current = _mtimes(entry["paths"])
            previous, entry["snapshot"] = entry["snapshot"], current
            if previous is not None:
                changed = sorted(
                    path
                    for path in set(current) | set(previous)
                    if current.get(path) != previous.get(path)
                )
                if changed:
                    on_change = entry["on_change"]
                    if on_change is not None:
                        try:
                            on_change(changed)
                        except Exception as exc:  # noqa: BLE001 - same reasoning as above
                            logging.getLogger("daemon").warning(
                                "scheduler: file watcher %r raised %r", key, exc
                            )
                    self.wake()
            if self._stop.wait(interval):
                break
