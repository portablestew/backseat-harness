"""Triggered jobs: ``job.toml`` schedules, next-fire math, and the fire protocol.

goal:63-75, 2-plan §6. A job lives at ``.backseat/jobs/[slug]/job.toml`` and is
loaded during the daemon's workspace scan (``Daemon._register_jobs``), which
arms its next occurrence as an ordinary scheduler deadline — scheduler.py never
parses a schedule itself. The vocabulary is a stdlib enum instead of cron:
``hourly`` / ``daily`` / ``weekly`` with an ``at`` time (machine-local, or a
``tz`` zone name where zoneinfo can resolve it), plus epoch-anchored
``interval`` periods (``"45m"`` / ``"2h"`` / ``"1d"`` — boundaries at exact
multiples of the period, so restarts cannot drift). "Missed intervals are
skipped" (goal:67) falls out of :func:`next_fire` always answering the first
occurrence strictly after *now*: there is no catch-up policy to get wrong, and
a daemon that was down registers the next future boundary instead of replaying
the ones it slept through.

Firing (goal:68-75) is a file protocol with no scheduling inside it:

1. Any destination ``job-[slug]-*`` lane whose ``state.json`` is not
   ``"complete"`` skips the fire (goal:70) — a fresh copy has no state yet, so
   the lane a fire just made holds off the next one until it finishes.
2. The oldest completed destinations beyond the newest three are deleted
   (goal:71).
3. ``jobs/[slug]/`` is copied into ``tasks/job-[slug]-[timestamp]/`` with
   ``goal.md`` copied **last** (goal:72-73): it is the validity signal the
   picker scans for, so a half-copied lane never looks runnable, and
   ``config.toml`` rides along for the task config layer (goal:75). A
   same-second re-fire gets a ``-2``, ``-3``, … suffix rather than ever
   overwriting an existing lane (2-plan §6/§8).

The new lane needs no special scheduling: the normal check-in path picks it up
after the 30s quiescence gate (2-plan §3).
"""

from __future__ import annotations

import math
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

from backseat_harness import storage
from backseat_harness.config import TaskPaths, _tomllib
from backseat_harness.models import TaskStatus

#: Completed destinations kept per job before the oldest are deleted (goal:71).
KEEP_COMPLETED = 3

#: One timestamped lane name per fire: ``job-[slug]-[timestamp]``, with a
#: counter appended on collision (2-plan §6/§8).
_STAMP_FORMAT = "%Y%m%d-%H%M%S"
_STAMP_RE = r"\d{8}-\d{6}(?:-\d+)?"

_GOAL_NAME = "goal.md"

_PERIOD_S = {"m": 60.0, "h": 3600.0, "d": 86400.0}
_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


@dataclass(frozen=True)
class Job:
    """One parsed ``job.toml``: enough to compute occurrences, nothing more."""

    slug: str
    #: ``hourly`` / ``daily`` / ``weekly`` / ``interval``.
    schedule: str
    #: Hour and minute of ``at`` (goal:65); hourly honors the minute only and
    #: fires at it of every hour, defaulting to ``:00``.
    hour: int = 0
    minute: int = 0
    #: Weekly only: 0=Monday … 6=Sunday.
    day: int = 0
    #: ``interval`` only, in seconds.
    period_s: float = 0.0
    tz: tzinfo = field(default_factory=lambda: datetime.now().astimezone().tzinfo)


def _parse_at(raw: object) -> tuple[int, int]:
    """``"HH:MM"`` (or toml's own ``07:30:00`` time literal) → (hour, minute)."""
    if isinstance(raw, dt_time):
        return raw.hour, raw.minute
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(raw).strip())
    if match is None:
        raise ValueError(f"at must be HH:MM, got {raw!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"at out of range: {raw!r}")
    return hour, minute


def _parse_day(raw: object) -> int:
    text = str(raw).strip().lower()
    if text in _DAYS:
        return _DAYS[text]
    if text.isdigit() and int(text) <= 6:
        return int(text)
    raise ValueError(f"day must be mon..sun or 0..6, got {raw!r}")


def _parse_period(raw: object) -> float:
    match = re.fullmatch(r"(\d+)([mhd])", str(raw).strip())
    if match is None:
        raise ValueError(f'interval must look like "45m", "2h", or "1d", got {raw!r}')
    seconds = int(match.group(1)) * _PERIOD_S[match.group(2)]
    if seconds <= 0:
        raise ValueError(f"interval must be positive, got {raw!r}")
    return seconds


def _parse_tz(raw: object) -> tzinfo:
    """A ``tz`` zone name through zoneinfo; unresolvable zones are a load error.

    zoneinfo needs a tz database — the ``tzdata`` package where the platform
    ships none (Windows) — so a name it cannot resolve fails the whole job at
    parse time, where ``load_jobs`` reports it, rather than at 3am on the fire.
    """
    try:
        return ZoneInfo(str(raw))
    except Exception as exc:  # noqa: BLE001 - ZoneInfoNotFoundError's bases vary by platform
        raise ValueError(f"tz {raw!r} is not resolvable (is tzdata installed?): {exc}") from exc


def parse_job(slug: str, text: str) -> Job:
    """Parse one ``job.toml`` body; raises ``ValueError`` on anything malformed.

    Malformed is deliberately loud: ``load_jobs`` turns the raise into a
    warning and a skip, so a typo costs its own job and nothing else.
    """
    data = _tomllib().loads(text)
    if "interval" in data:
        # ``interval = "2h"`` alone, or spelled out as ``schedule = "interval"``
        # beside it; only a *conflicting* schedule is an error.
        stated = str(data.get("schedule", "interval")).strip().lower()
        if stated != "interval":
            raise ValueError('set either "schedule" or "interval", not both')
        return Job(slug=slug, schedule="interval", period_s=_parse_period(data["interval"]))
    schedule = str(data.get("schedule", "")).strip().lower()
    if schedule not in ("hourly", "daily", "weekly"):
        raise ValueError(f'unknown schedule {schedule!r}; want hourly, daily, or weekly')
    hour, minute = _parse_at(data.get("at", "00:00"))
    day = _parse_day(data.get("day", "mon")) if schedule == "weekly" else 0
    tz = _parse_tz(data["tz"]) if "tz" in data else datetime.now().astimezone().tzinfo
    return Job(slug=slug, schedule=schedule, hour=hour, minute=minute, day=day, tz=tz)


def load_job(workspace: Path | str, slug: str) -> Job | None:
    """One job's spec, or None when it is gone (deleted since registration)."""
    spec = Path(workspace) / ".backseat" / "jobs" / slug / "job.toml"
    try:
        if not spec.is_file():
            return None
        return parse_job(slug, spec.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError) as exc:
        print(f"[warn] jobs: cannot load {spec}: {exc}", file=sys.stderr)
        return None


def load_jobs(workspace: Path | str) -> dict[str, Job]:
    """Every well-formed job under ``.backseat/jobs/`` (goal:64-65).

    Best-effort per directory: a job.toml that will not parse is warned about
    and skipped — one typo must not cost the workspace its check-in, its other
    jobs, or the daemon its pump.
    """
    root = Path(workspace) / ".backseat" / "jobs"
    if not root.is_dir():
        return {}
    found: dict[str, Job] = {}
    for job_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        job = load_job(workspace, job_dir.name)
        if job is not None:
            found[job_dir.name] = job
    return found


def next_fire(job: Job, now: float) -> float:
    """The first occurrence of ``job`` strictly after ``now`` (wall epochs).

    Strictly after is the whole missed-interval story (goal:67): a daemon down
    across ten occurrences registers the next future one, and a fire landing
    exactly on its timestamp re-arms for the following period rather than
    re-firing the one it just ran.
    """
    if job.schedule == "interval":
        return (math.floor(now / job.period_s) + 1) * job.period_s
    current = datetime.fromtimestamp(now, job.tz)
    if job.schedule == "hourly":
        candidate = current.replace(minute=job.minute, second=0, microsecond=0)
        while candidate.timestamp() <= now:
            candidate = candidate + timedelta(hours=1)
        return candidate.timestamp()
    offset = (job.day - current.weekday()) % 7 if job.schedule == "weekly" else 0
    day = current.date() + timedelta(days=offset)
    candidate = datetime(day.year, day.month, day.day, job.hour, job.minute, tzinfo=job.tz)
    step = timedelta(days=7 if job.schedule == "weekly" else 1)
    while candidate.timestamp() <= now:
        candidate = candidate + step
    return candidate.timestamp()


def destinations(workspace: Path | str, slug: str) -> list[Path]:
    """Every destination lane of ``slug``, newest first by name.

    The name carries the creation stamp, so lexicographic order is creation
    order; the shape check (``-[timestamp]``) keeps a slug from claiming its
    prefixes' lanes — ``job-triage-`` must not match ``job-triage-tickets-…``.
    """
    tasks = Path(workspace) / ".backseat" / "tasks"
    if not tasks.is_dir():
        return []
    pattern = re.compile(rf"^{re.escape('job-' + slug)}-{_STAMP_RE}$")
    lanes = [path for path in tasks.iterdir() if path.is_dir() and pattern.match(path.name)]
    return sorted(lanes, key=lambda path: path.name, reverse=True)


def _complete(lane: Path) -> bool:
    """Is this destination finished (goal:70's ``state.json`` == ``"complete"``)?

    Missing or unreadable state is *not* confirmably complete — a fresh copy
    has no state yet, which is exactly what holds the next fire off.
    """
    try:
        state = storage.load_state(TaskPaths(lane), lane.name)
    except (OSError, ValueError, KeyError):
        return False
    return state.status is TaskStatus.COMPLETE


def _fresh_lane(tasks: Path, base: str) -> Path:
    """Create ``base`` or its ``-2``, ``-3``, … sibling; never reuse a lane."""
    candidate = tasks / base
    counter = 1
    while candidate.exists():
        counter += 1
        candidate = tasks / f"{base}-{counter}"
    # exist_ok off on purpose: losing a creation race must raise, not reuse.
    candidate.mkdir(parents=True)
    return candidate


def fire(workspace: Path | str, slug: str, *, now: float | None = None) -> Path | None:
    """Run one job's fire protocol (goal:68-75); the new lane, or None to skip.

    Skip means the guard held — some destination is still running — or there
    was nothing to copy. The copy itself puts ``goal.md`` down last, so the
    lane only becomes visible to the picker once it is complete on disk.
    """
    src = Path(workspace) / ".backseat" / "jobs" / slug
    tasks = Path(workspace) / ".backseat" / "tasks"
    if not src.is_dir():
        return None
    lanes = destinations(workspace, slug)
    if any(not _complete(lane) for lane in lanes):  # goal:70
        return None
    for stale in lanes[KEEP_COMPLETED:]:  # goal:71
        try:
            shutil.rmtree(stale)
        except OSError as exc:
            print(f"[warn] jobs: cannot delete {stale}: {exc}", file=sys.stderr)
    stamp = time.strftime(_STAMP_FORMAT, time.localtime(now if now is not None else time.time()))
    dest = _fresh_lane(tasks, f"job-{slug}-{stamp}")
    goal = src / _GOAL_NAME
    for entry in sorted(src.iterdir()):
        if entry.name == _GOAL_NAME and entry.is_file():
            continue  # goal.md lands last, below
        if entry.is_dir():
            shutil.copytree(entry, dest / entry.name)
        else:
            shutil.copy2(entry, dest / entry.name)
    if goal.is_file():
        # A plain copy, not copy2: goal.md is the validity signal, and a fresh
        # mtime is what marks the lane as born now (goal:73; cf. _lane_stamp).
        shutil.copy(goal, dest / _GOAL_NAME)
    return dest
