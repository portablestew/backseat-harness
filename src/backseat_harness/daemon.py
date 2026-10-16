"""Background daemon: single-instance guard, discovery, check-ins, triggered jobs.

``backseat-harness daemon`` (goal:14-75, 2-plan §3) pumps one scheduler
(scheduler.py) with five concerns riding it:

* **Guard** — ``kv["daemon"]`` holds this process's pid+start, claimed
  atomically before anything is scheduled so a second ``daemon`` exits 2
  (goal:15-16), and released in a ``finally`` so a clean stop (Ctrl-C
  included) leaves nothing behind while a killed daemon leaves a stale row
  the next starter reclaims.

* **Discovery** — at startup (goal:48: its timestamp defaults to now) and
  every 30 minutes after, the workspaces table is re-read *iff*
  ``workspaces-version`` moved (goal:49): new paths are adopted with an
  immediate check-in, unlisted ones dropped (goal:50-52). The deadline is an
  ordinary scheduled job, so it joins the sleep math and cannot starve
  (goal:53).

* **Check-in** — per workspace, in goal order (goal:32-46): the orphan sweep
  runs first (a dead harness's live provider would otherwise pin the workspace
  busy forever, killing it here since the pre-run sweep is unreachable behind
  the busy gate), then gather the newest mtime under ``.backseat/tasks/**``
  (any file, goal:32) and the workspace's task rows; any pid live at its
  recorded start means the workspace is busy — single-threaded for
  auto-scheduled work (goal:34-38) — so register
  idempotent pid watchers (a completion wakes the pump for the next cycle,
  goal:37) and fall back to the 30-minute default; recent file activity means
  wait for quiescence (now+30s, goal:40-41); otherwise the oldest
  ``continue``/stateless lane (goal:42-43) is backgrounded as ``run`` with
  stdout+stderr appended to its ``daemon.log`` (goal:45), and the next scan
  lands at now+30s to spot its pid (goal:46).

* **File watching** — goal:54-62, over the scheduler's ``watch_files`` (stdlib
  mtime polling, the no-watchdog choice ratified in 2-plan §4): each adopted
  workspace contributes its ``goal.md``/``job.toml`` files plus the
  ``tasks``/``jobs`` directories holding them, and the sqlite registry
  contributes its own mtime. A workspace file change resets that
  workspace's check-in to now+30s — the path a web "add task" takes to
  actually starting work (goal:59) — and a registry change marks discovery
  due now; both land as ordinary jobs, so the wake rides the same semaphore.

* **Triggered jobs** — goal:63-75, 2-plan §6: every check-in reloads
  ``.backseat/jobs/[slug]/job.toml`` and arms each job's next occurrence
  (wall epochs, converted onto the scheduler's clock in ``_deadline``) as an
  ordinary deadline — missed intervals skip because the deadline is always the
  next future boundary. A fire runs jobs.py's guard/cleanup/copy protocol, the
  new ``job-[slug]-[timestamp]`` lane rides the normal check-in path, and a
  workspace drop cancels the pending fires.

Two clocks, split by purpose: scheduler deadlines use the scheduler's clock
(a fake one in tests), while wall epochs — the quiescence gate's file mtimes
and jobs' cron occurrences — ride ``self._wall`` (``time.time`` in tests
injected alongside the scheduler's fake). Nothing here signals a process
except the orphan sweep the check-in runs: watchers observe, the sweep kills.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from backseat_harness import config, db, exitwatch, jobs, storage
from backseat_harness.config import TaskPaths
from backseat_harness.models import TaskStatus
from backseat_harness.scheduler import FILE_POLL_S, Scheduler

#: Discovery cadence, and the default gap between workspace check-ins
#: (goal:31 "now + 30 minutes", goal:52 "now + 30 minutes").
DISCOVERY_S = 30 * 60
CHECKIN_DEFAULT_S = 30 * 60

#: No lane starts until the workspace has been quiet this long (goal:40).
QUIESCENCE_S = 30

#: Re-scan this soon after backgrounding a run: the next cycle sees its pid
#: and falls back to the default (goal:46).
AFTER_SPAWN_S = 30

#: How often the pump proves it is alive and shows what it sees. Without
#: this, a silent death is only bracketed by the last event and the next
#: check-in (up to 30 minutes later).
HEARTBEAT_S = 60

#: One pending job id for the workspace set; joins the sleep math (goal:53).
DISCOVERY_JOB = "discovery"

#: The scheduler watch key covering the registry's own mtime (goal:60-62).
DB_WATCH_KEY = "sqlite"

#: The scheduler job id carrying the periodic liveness stamp.
HEARTBEAT_JOB = "heartbeat"

#: The only filenames whose edits reset a check-in (goal:55-58). ``daemon.log``
#: and everything else under tasks/jobs is noise the filter drops.
_WATCHED_NAMES = frozenset({"goal.md", "job.toml"})

# Logging: durable log under ~/.backseat; setup deferred to Daemon.run so
# importing daemon.py is side-effect free. The path is resolved at setup
# time through the pinned home seam, so tests installing handlers write to
# the fake home and never touch the real file.
_logger = logging.getLogger("daemon")
_logger.setLevel(logging.INFO)


def _setup_daemon_logging() -> None:
    log_path = config._default_home() / ".backseat" / "daemon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _logger.handlers.clear()
    handler = logging.FileHandler(str(log_path), mode="a")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    _logger.addHandler(handler)


def checkin_job(workspace: str) -> str:
    """The scheduler job id carrying one workspace's next check-in."""
    return f"checkin:{workspace}"


def file_watch_job(workspace: str) -> str:
    """The scheduler watch key carrying one workspace's watched files."""
    return f"files:{workspace}"


def trigger_job(workspace: str, slug: str) -> str:
    """The scheduler job id carrying one triggered job's next fire (goal:66)."""
    return f"trigger:{workspace}:{slug}"


def _newest_mtime(root: Path) -> float | None:
    """Newest file mtime under ``root`` — any file, logs included (goal:32).

    Absent or unreadable paths count as absent: a lane vanishing mid-scan is
    an edge the daemon absorbs, not one it dies on (2-plan §7).
    """
    if not root.is_dir():
        return None
    newest: float | None = None
    for path in root.rglob("*"):
        try:
            if not path.is_file():
                continue
            stamp = path.stat().st_mtime
        except OSError:
            continue
        if newest is None or stamp > newest:
            newest = stamp
    return newest


def _rglob(root: Path, name: str) -> list[Path]:
    """Every ``name`` under ``root``, or an empty list when the walk dies.

    Best-effort like :func:`_newest_mtime`: a subtree vanishing mid-scan
    costs that subtree, not the watch (2-plan §7).
    """
    try:
        return sorted(root.rglob(name)) if root.is_dir() else []
    except OSError:
        return []


def _watch_paths(workspace: Path) -> list[Path]:
    """The mtimes worth polling for one workspace (goal:54-58).

    The two directories carry creations a file list would otherwise miss
    until its next refresh — a lane born between check-ins, a jobs dir
    appearing at all — and the files themselves carry the edits goal:55
    filters on. ``daemon.log`` and friends are deliberately absent.
    """
    tasks = workspace / ".backseat" / "tasks"
    jobs = workspace / ".backseat" / "jobs"
    return [tasks, jobs, *_rglob(tasks, "goal.md"), *_rglob(jobs, "job.toml")]


def _has_goal(goal: Path) -> bool:
    """Does this lane have a non-empty ``goal.md`` — the signal ``run`` needs?"""
    try:
        return goal.is_file() and bool(goal.read_text(encoding="utf-8", errors="replace").strip())
    except OSError:
        return False


def _lane_stamp(lane: Path) -> float | None:
    """Eligibility plus age for one lane: its stamp when it may run, else None.

    Eligible is "state says ``continue``, or there is no state at all"
    (goal:42) — *and* in either case a non-empty ``goal.md``, because ``run``
    refuses an empty goal with rc 2 (cli.py ``_ensure_goal``) and a
    backgrounded lane that can only refuse would respawn every 30 seconds
    forever. The stamp orders candidates: ``state.json`` mtime, or ``goal.md``
    mtime when there is no state (goal:42). A state file that will not parse
    is not confirmably ``continue``, so it is skipped rather than guessed.
    A ``_gen-*`` staging dir is never a lane, even with a ``goal.md`` in it
    (``config.is_task_lane``), so a generation can never be run by accident.
    """
    if not config.is_task_lane(lane):
        return None
    goal = lane / "goal.md"
    state = lane / "state.json"
    try:
        if state.exists():
            if not _has_goal(goal):
                return None
            loaded = storage.load_state(TaskPaths(lane), lane.name)
            if loaded.status is not TaskStatus.CONTINUE:
                return None
            return state.stat().st_mtime
        if not _has_goal(goal):
            return None
        return goal.stat().st_mtime
    except (OSError, ValueError, KeyError):
        return None


def _pick_lane(tasks_dir: Path) -> str | None:
    """The oldest eligible lane in a workspace (goal:42-43), or None.

    Oldest-first is the round-robin: a long lane times out or hits its step
    limit and yields to the next oldest (goal:43). Iteration is name-sorted so
    an mtime tie breaks deterministically.
    """
    if not tasks_dir.is_dir():
        return None
    best_slug: str | None = None
    best_stamp: float | None = None
    for lane in sorted(tasks_dir.iterdir()):
        if not lane.is_dir():
            continue
        stamp = _lane_stamp(lane)
        if stamp is None:
            continue
        if best_stamp is None or stamp < best_stamp:
            best_slug, best_stamp = lane.name, stamp
    return best_slug


def _live_pids(workspace: str) -> list[tuple[int, float | None]]:
    """Every pid in this workspace's task rows that is live at its recorded start.

    A harness that died mid-turn leaves a live provider, but the check-in's
    sweep has already removed that row (killing the provider), so what
    remains here is a live harness and its providers: that still counts as
    a busy workspace (goal:33-34). Rows of
    unregistered workspaces do not appear (``workspace_path`` is None), which
    matches a workspace discovery has already dropped.
    """
    target = str(Path(workspace).expanduser().resolve())
    live: list[tuple[int, float | None]] = []
    with db.open_db() as conn:
        rows = db.list_tasks(conn)
    for row in rows:
        if row["workspace_path"] != target:
            continue
        for pid_key, start_key in (
            ("harness_pid", "harness_start"),
        ):
            pid = row[pid_key]
            if pid is not None and db.pid_live(pid, row[start_key]):
                live.append((pid, row[start_key]))
        for pid, start in db.provider_entries(row):
            if db.pid_live(pid, start):
                live.append((pid, start))
    return live


def _run_argv(workspace: str, slug: str, *, debug: bool = False) -> list[str]:
    """argv for the backgrounded run: the console script when it is on PATH,
    else the very same CLI through the current interpreter.

    ``debug`` forwards this daemon's own --debug: with it, the lane's
    daemon.log carries the per-step timing lines, so a run that dies
    between two steps shows exactly which step it was in rather than only
    the last iteration header.
    """
    exe = shutil.which("backseat-harness")
    base = [exe] if exe else [sys.executable, "-m", "backseat_harness.cli"]
    flags = ["--debug"] if debug else []
    return [*base, "-C", workspace, *flags, "run", slug]


def _detach_kwargs() -> dict:
    """Popen kwargs that give a run its own session (POSIX) or process group (Windows).

    The same split the other launch sites inline (spawn.py, web/caddy.py,
    web/services/task.py). A takeover kills a lane's pids by group, so a run
    left in the daemon's group could take the daemon down with it.
    """
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


class Daemon:
    """One daemon instance: guard ownership plus the jobs it schedules."""

    def __init__(
        self,
        *,
        sched: Scheduler | None = None,
        pid: int | None = None,
        start: float | None = None,
        file_interval: float = FILE_POLL_S,
        wall: Callable[[], float] = time.time,
        debug: bool = False,
    ) -> None:
        self._sched = sched if sched is not None else Scheduler()
        self._pid = os.getpid() if pid is None else int(pid)
        self._start: float | None = None if start is None else float(start)
        # How often file watchers re-stat: the production cadence by default,
        # a test's 10ms cadence in tests/test_daemon.py's fixture.
        self._file_interval = float(file_interval)
        # Wall epochs: cron occurrences, fire-stamp names, quiescence. The one
        # seam tests fake beside the scheduler's clock (they run in lockstep).
        self._wall = wall
        # Forwarded to daemon-spawned runs as --debug ([dbg] step lines in the
        # lane's daemon.log) iff this daemon itself was started with --debug.
        self._debug = bool(debug)
        # None means "never scanned"; 0 (no workspaces ever) is a real answer.
        self._version: int | None = None
        self._workspaces: set[str] = set()
        # Pending trigger ids per workspace, so a drop can cancel them (goal:50).
        self._job_ids: dict[str, set[str]] = {}

    # --- lifecycle ---------------------------------------------------------

    def claim(self) -> None:
        """Take the single-instance guard; a live holder raises DaemonAlreadyRunning.

        ``start`` defaults to psutil's create_time for our own pid, recorded
        beside it precisely so a recycled pid cannot impersonate this daemon
        (goal:16: same liveliness rule as a run's task row).
        """
        if self._start is None:
            self._start = db.process_start(self._pid)
        with db.open_db() as conn:
            db.claim_daemon(conn, self._pid, self._start)
        _logger.info("guard claimed pid=%s start=%s", self._pid, self._start)

    def run(
        self,
        on_claimed: Callable[[], None] | None = None,
        on_release: Callable[[], None] | None = None,
    ) -> None:
        """Claim the guard, then pump until :meth:`stop`; release it on the way out.

        The claim sits outside the ``try``: a refusal (or a Ctrl-C during the
        claim) must not clear a guard this instance never took. ``on_claimed``
        runs inside the ``try`` right after the claim, so a callback that raises
        still releases the guard (caddy mode starts its child here).
        ``on_release`` runs in the ``finally`` just before the guard is cleared,
        so whatever it stops (caddy holding the site port) is down before a
        successor can claim the guard. It runs even if ``on_claimed`` raised.
        """
        self.claim()
        _setup_daemon_logging()
        # Claim/start logged here, not inside claim(): handlers do not exist
        # until this point, so claim()'s own line was always dropped.
        _logger.info("guard claimed pid=%s start=%s", self._pid, self._start)
        _logger.info("daemon start argv=%s", " ".join(sys.argv))
        restore_exitwatch = exitwatch.install(
            lambda line: _logger.warning("exit: %s", line)
        )

        def _shutdown(signum, frame):
            _logger.warning("exit: signal SIGBREAK (Ctrl-Break); stopping pump")
            self.stop()

        if os.name == "nt":
            signal.signal(signal.SIGBREAK, _shutdown)
        else:
            signal.signal(signal.SIGTERM, _shutdown)

        try:
            if on_claimed is not None:
                on_claimed()
            # Startup discovery: goal:48 sets its timestamp to now, so the
            # first pump pass adopts the workspace set before sleeping.
            self._sched.schedule(DISCOVERY_JOB, self._sched.now(), self.discover)
            self._sched.schedule(
                HEARTBEAT_JOB, self._sched.now() + HEARTBEAT_S, self._heartbeat
            )
            self._sched.run()
        finally:
            self._sched.stop()
            try:
                if on_release is not None:
                    on_release()
            finally:
                with db.open_db() as conn:
                    db.clear_daemon(conn, self._pid, self._start)
            _logger.info("guard released pid=%s start=%s", self._pid, self._start)
            _logger.info("shutdown")
            restore_exitwatch()

    def stop(self) -> None:
        """Stop the pump and join the watcher threads (idempotent)."""
        self._sched.stop()

    # --- liveness -----------------------------------------------------------

    def _heartbeat(self) -> None:
        """One stamp per HEARTBEAT_S: this pid, the armed jobs, the live pids.

        The death of a silent process is otherwise only bracketed by its last
        log line and the next scheduled wake. Exceptions are logged and
        swallowed -- a heartbeat that can crash is one that stops proving
        liveness -- and the re-arm happens on every path.
        """
        try:
            live: list[int] = []
            for workspace in sorted(self._workspaces):
                try:
                    live.extend(pid for pid, _ in _live_pids(workspace))
                except Exception as exc:  # noqa: BLE001 - one bad scan, not a dead beat
                    _logger.warning(
                        "heartbeat: liveness scan failed workspace=%s err=%s",
                        workspace,
                        exc,
                    )
            jobs = getattr(self._sched, "_jobs", {})
            next_in: float | None = None
            if jobs:
                next_in = min(job.deadline for job in jobs.values()) - self._sched.now()
            _logger.info(
                "heartbeat pid=%s armed=%d next_in=%s live_pids=%s",
                self._pid,
                len(jobs),
                "n/a" if next_in is None else f"{next_in:.0f}s",
                sorted(live),
            )
        except Exception as exc:  # noqa: BLE001 - log, re-arm, keep proving liveness
            _logger.warning("heartbeat: failed err=%s", exc)
        finally:
            self._arm(HEARTBEAT_JOB, self._sched.now() + HEARTBEAT_S, self._heartbeat)

    # --- discovery ---------------------------------------------------------

    def discover(self) -> None:
        """One workspace-discovery pass (goal:47-53), then re-arm at now+30min.

        The re-arm lives in ``finally``: the fired one-shot job is already
        gone, and a pass that raised must not strand discovery for the rest of
        the process's life. Equal version means no work (goal:49), never no
        next pass. The registry watch registers inside ``try`` (goal:60-62):
        re-registering the key is in-place, so repeats start no second thread,
        and a scan that raises still leaves sqlite edits able to bring
        discovery back.
        """
        try:
            self._watch_db()
            self._scan()
        finally:
            self._arm(DISCOVERY_JOB, self._sched.now() + DISCOVERY_S, self.discover)

    def _scan(self) -> None:
        with db.open_db() as conn:
            version = db.workspaces_version(conn)
            if version == self._version:
                return
            paths = db.list_workspaces(conn)
            # Cached only after ingest succeeds: a half-adopted pass must
            # retry next period, not early-out against a version it never
            # finished applying.
            self._ingest(paths)
            self._version = version

    def _ingest(self, paths: list[str]) -> None:
        """Adopt new paths with an immediate check-in; drop unregistered ones.

        Drops cancel the pending check-in and retire the workspace's file
        watch outright (goal:50): the workspace is no longer the daemon's to
        schedule, though any run already launched there finishes under its
        own task-row lifecycle (2-plan §5).
        """
        current = set(paths)
        for workspace in sorted(current - self._workspaces):
            self._workspaces.add(workspace)
            _logger.info("discovery adopted workspace=%s", workspace)
            # Arm first, then watch (the busy branch's order): a file change
            # racing the adoption must find a pending job to move. Immediate:
            # a fresh task's own 30s gate supplies the delay (goal:51), so no
            # extra timer is needed here.
            self._arm(checkin_job(workspace), self._sched.now(), self._checkin(workspace))
            self._watch_workspace(workspace)
        for workspace in sorted(self._workspaces - current):
            self._workspaces.discard(workspace)
            _logger.info("discovery dropped workspace=%s", workspace)
            self._sched.cancel(checkin_job(workspace))
            # The pending fires go with it: the workspace is no longer ours to
            # trigger in (goal:50, 2-plan §6).
            for job_id in self._job_ids.pop(workspace, set()):
                self._sched.cancel(job_id)
            self._unwatch_workspace(workspace)

    # --- check-in ----------------------------------------------------------

    def check_in(self, workspace: str) -> None:
        """One workspace check-in (goal:32-46), always leaving a next scan armed.

        The except clause is the error policy: a transient failure re-arms at
        the default cadence and re-raises (the pump warns), because a check-in
        that dies un-armed retires the workspace until a discovery that may
        never *re*-adopt an already-listed path.
        """
        try:
            self._do_check_in(workspace)
        except Exception:  # noqa: BLE001 - one bad pass must not retire the workspace
            self._arm(
                checkin_job(workspace),
                self._sched.now() + CHECKIN_DEFAULT_S,
                self._checkin(workspace),
            )
            raise

    def _do_check_in(self, workspace: str) -> None:
        # Refresh the watch first (goal:54-58): paths only enter the set on
        # registration, so this is how a lane born since the last cycle gets
        # polled — and a workspace a racing discovery dropped must not get
        # its watch re-registered.
        if workspace in self._workspaces:
            self._watch_workspace(workspace)
        # Jobs ride every check-in too (goal:64-66): adoption reaches here via
        # the immediate check-in, and a job.toml edit reaches here via the file
        # watcher's now+30s reset, so this one call is both the first load and
        # the refresh — _arm moves a pending deadline in place.
        self._register_jobs(workspace)
        # Sweep before any gate: a harness that died mid-turn leaves a live
        # provider whose row would pin this workspace busy forever, and the
        # pre-run sweep is exactly what the busy gate below blocks.
        with db.open_db() as conn:
            swept = db.sweep_orphans(conn)
        if swept["removed"] or swept["killed"]:
            _logger.info("orphan sweep workspace=%s %s", workspace, swept)
        tasks_dir = Path(workspace) / ".backseat" / "tasks"
        newest = _newest_mtime(tasks_dir)

        busy = _live_pids(workspace)
        if busy:
            _logger.info(
                "check-in workspace=%s branch=busy pids=%s",
                workspace,
                [p for p, _ in busy],
            )
            # Arm first, then watch: a pid dying between the two must find a
            # pending job to move (watchers only ever move pending jobs), and
            # the default is the fallback when the pid outlives a full period.
            self._arm(
                checkin_job(workspace),
                self._sched.now() + CHECKIN_DEFAULT_S,
                self._checkin(workspace),
            )
            for pid, start in busy:
                self._sched.watch_pid(
                    pid, start, on_exit=self._pid_exited(workspace)
                )
            return

        now = self._sched.now()
        if newest is not None and self._wall() - newest < QUIESCENCE_S:
            _logger.info("check-in workspace=%s branch=quiescence newest=%.1f", workspace, newest)
            # Files are still moving: wait for quiescence before starting
            # anything (goal:40-41).
            self._arm(checkin_job(workspace), now + QUIESCENCE_S, self._checkin(workspace))
            return

        slug = _pick_lane(tasks_dir)
        if slug is None:
            _logger.info("check-in workspace=%s branch=no_lane", workspace)
            # Nothing runnable: back to the default cadence (goal:31).
            self._arm(
                checkin_job(workspace), now + CHECKIN_DEFAULT_S, self._checkin(workspace)
            )
            return

        _logger.info("lane picked workspace=%s slug=%s", workspace, slug)
        # Success re-scans in 30s to spot the new pid (goal:46); a spawn that
        # raises falls through to check_in's default-and-re-raise.
        _logger.info("run workspace=%s slug=%s", workspace, slug)
        self._spawn(workspace, slug)
        self._arm(checkin_job(workspace), now + AFTER_SPAWN_S, self._checkin(workspace))

    def _pid_exited(self, workspace: str):
        """A watched process finished: check this workspace in again now (goal:37)."""

        def on_exit() -> None:
            if workspace in self._workspaces:
                self._arm(checkin_job(workspace), self._sched.now(), self._checkin(workspace))

        return on_exit

    def _checkin(self, workspace: str):
        """A check-in callback for a workspace's scheduled job.

        A fresh closure per arm; ``_arm`` may keep an older one when the job
        is already pending, and every instance does the same thing, so the
        swap is invisible.
        """
        return lambda: self.check_in(workspace)

    def _arm(self, job_id: str, deadline: float, callback) -> None:
        """Register or move a job: reschedule when pending, schedule when not.

        Both orders race safely against a watcher thread doing the same: the
        loser's ``schedule`` trips the unique-id rule and falls back to
        rescheduling whatever won, so the pair lands on one pending job.
        """
        if self._sched.reschedule(job_id, deadline):
            return
        try:
            self._sched.schedule(job_id, deadline, callback)
        except ValueError:
            self._sched.reschedule(job_id, deadline)

    # --- triggered jobs (goal:63-75) -------------------------------------------

    def _register_jobs(self, workspace: str) -> None:
        """Arm every job under ``.backseat/jobs/`` (goal:64-66); drop the gone ones.

        Called from each check-in, so adoption, a job.toml edit (which the file
        watcher routes into a check-in), and a schedule change all land here.
        ``load_jobs`` is best-effort per directory: a spec it cannot parse is
        warned about and skipped, costing that job only — never the workspace's
        check-in or its other jobs.
        """
        specs = jobs.load_jobs(workspace)
        wanted = {trigger_job(workspace, slug): slug for slug in specs}
        for job_id in set(self._job_ids.get(workspace, set())) - set(wanted):
            self._sched.cancel(job_id)
        for job_id, slug in wanted.items():
            _logger.info("arm workspace=%s job=%s", workspace, slug)
            self._arm(job_id, self._deadline(specs[slug]), self._fire(workspace, slug))
        if wanted:
            self._job_ids[workspace] = set(wanted)
        else:
            self._job_ids.pop(workspace, None)

    def _deadline(self, job: jobs.Job) -> float:
        """Convert a job's next wall-clock occurrence into a scheduler deadline.

        Occurrences are wall epochs by definition (cron semantics); the pump's
        clock is its own (a fake in tests), so the gap ``next_fire - wall``
        rides on top of ``sched.now()`` rather than being used raw — with both
        clocks faked in lockstep the deadline reads as the epoch itself.
        """
        now = self._wall()
        return self._sched.now() + (jobs.next_fire(job, now) - now)

    def _fire(self, workspace: str, slug: str):
        """A fire callback: run the protocol now, then arm the next occurrence.

        The spec is re-read inside the callback, so an edited job.toml applies
        even where ``_arm`` kept an older closure (reschedule never swaps the
        callback). The pump pops this id before calling, so the re-arm is a
        plain schedule — one-shot semantics, exactly like ``discovery``
        re-arming itself. A guard-skipped fire still re-arms: the next
        occurrence retries (goal:70 is a hold-off, not a cancel). A deleted
        job.toml stops the re-arm, and a fire that raises is healed by the
        next check-in's reload.
        """

        def on_fire() -> None:
            if workspace not in self._workspaces:
                return  # dropped while pending; _ingest cancelled the id anyway
            job = jobs.load_job(workspace, slug)
            if job is None:
                return
            result = jobs.fire(workspace, slug, now=self._wall())
            if result is not None:
                _logger.info("fire workspace=%s slug=%s result=%s", workspace, slug, result)
            else:
                _logger.info("fire workspace=%s slug=%s held_off (guard held)", workspace, slug)
            self._arm(
                trigger_job(workspace, slug),
                self._deadline(job),
                self._fire(workspace, slug),
            )

        return on_fire

    # --- file watchers (goal:54-62) ------------------------------------------

    def _watch_workspace(self, workspace: str) -> None:
        """Register (or refresh) one workspace's mtime watch (goal:54-58).

        Called at adoption and again at every check-in: the path list only
        moves on registration, so the per-cycle refresh is what makes a lane
        created between check-ins polled. Re-registering the key updates the
        paths and callback in place — one poll thread per workspace.
        """
        self._sched.watch_files(
            file_watch_job(workspace),
            _watch_paths(Path(workspace)),
            self._workspace_changed(workspace),
            interval=self._file_interval,
        )

    def _unwatch_workspace(self, workspace: str) -> None:
        """Retire a dropped workspace's watch: same key, nothing left to poll.

        The scheduler has no unwatch (2-plan §4), so the thread parks on an
        empty path list until ``stop()`` joins it — and the callback goes
        with the paths, so a re-adoption starts from a clean entry rather
        than a stale closure over a workspace the daemon no longer serves.
        """
        self._sched.watch_files(
            file_watch_job(workspace), [], None, interval=self._file_interval
        )

    def _workspace_changed(self, workspace: str):
        """A workspace watch callback: filter, then re-check-in at now+30s.

        The filter is goal:55's — only ``goal.md``/``job.toml`` edits and the
        two directories holding them count, so a ``daemon.log`` append cannot
        reset the cadence. Membership is checked first, matching the pid
        watcher: a callback racing its workspace's drop must not re-arm the
        check-in the discovery pass just cancelled.
        """
        tasks_dir = Path(workspace) / ".backseat" / "tasks"
        jobs_dir = Path(workspace) / ".backseat" / "jobs"
        relevant = {tasks_dir, jobs_dir}

        def on_change(changed: list[str]) -> None:
            if workspace not in self._workspaces:
                return
            if not any(
                Path(path).name in _WATCHED_NAMES or Path(path) in relevant
                for path in changed
            ):
                return
            self._arm(
                checkin_job(workspace),
                self._sched.now() + QUIESCENCE_S,
                self._checkin(workspace),
            )

        return on_change

    def _watch_db(self) -> None:
        """Poll the registry's own mtime (goal:60-62); idempotent per key.

        F — durable: WAL mode means inserts go to -wal sidecar; the main
        db.sqlite st_mtime does not change until checkpoint, so this poll
        depends on the registry NOT using WAL.
        """
        self._sched.watch_files(
            DB_WATCH_KEY, [db.db_path()], self._db_changed, interval=self._file_interval
        )

    def _db_changed(self, changed: list[str]) -> None:
        """The registry moved: discovery is due now (goal:60-62).

        ``changed`` goes unread — the watch covers exactly one file, and the
        registration-write that bumps the workspaces version is precisely the
        edit this exists to catch.
        """
        self._arm(DISCOVERY_JOB, self._sched.now(), self.discover)

    # --- spawn -------------------------------------------------------------

    def _spawn(self, workspace: str, slug: str) -> None:
        """Background ``backseat-harness run <slug>``, appended to daemon.log (goal:45).

        The child runs in the workspace (``-C`` defaults to cwd anyway, but
        spelled out so the argv reads as the command it is) with stdin closed:
        a backgrounded lane gets no brief, and inheriting the daemon's stdin
        would let an absent human block a run.
        """
        log_path = Path(workspace) / ".backseat" / "tasks" / slug / "daemon.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("ab")
        try:
            proc = subprocess.Popen(
                _run_argv(workspace, slug, debug=self._debug),
                cwd=str(workspace),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                **_detach_kwargs(),
            )
            _logger.info(
                "spawned run workspace=%s slug=%s child_pid=%s", workspace, slug, proc.pid
            )
        finally:
            # The child holds its own inherited copy; ours closes either way.
            log.close()
