"""Process registry: ``~/.backseat/db.sqlite`` (workspaces, tasks, kv).

The daemon's shared store, written only by the harness. Three tables:

* ``workspaces`` — every registered workspace dir path (absolute, resolved).
* ``tasks`` — one row per *running* task: harness pid+start plus the
  provider pid+start recorded at each provider CLI spawn. Claimed before a
  ``run`` starts, deleted before it exits; a row left behind by a crashed
  harness is reclaimed by the pre-run orphan sweep (goal.md).
* ``kv`` — plain key/value. Holds ``schema-version`` (the ``"v"`` convention
  from models.py carried as a kv row) and ``workspaces-version``, a counter
  that changes iff the workspace *set* changes — the daemon's discovery scan
  compares its cached copy to know when to re-read the workspaces table.

Liveliness is always pid *and* start time (``psutil.Process.create_time``):
a bare pid check would pass for an unrelated process that reused a dead
one's pid, which is exactly the case the stored start time exists to catch.

Home resolves through ``config._default_home`` on every call, so the
hermetic test pin (tests/conftest.py:55-65) isolates this file too; nothing
reads the real home at import time.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import psutil

from backseat_harness import config, spawn

#: Bumped only on an incompatible schema change, mirroring SCHEMA_VERSION in
#: models.py; stored in kv rather than a file field because the db has no
#: per-record dict to hang a "v" key on.
SCHEMA_VERSION = 1

DB_FILENAME = "db.sqlite"

#: Slack allowed between a stored start time and psutil's create_time() when
#: deciding a pid is "the same process". Both clocks are epoch seconds from
#: the same machine, so this only absorbs call-time skew, not real drift.
START_TOLERANCE_S = 2.0

#: How long a writer blocks on a contended db before sqlite errors. The file
#: is written by independent processes (a run, the daemon, register-workspace)
#: on the same machine, so contention is expected, not exceptional — the same
#: posture as storage.py's retry loop for the watched workspace files.
BUSY_TIMEOUT_MS = 5000

WORKSPACES_VERSION_KEY = "workspaces-version"
SCHEMA_VERSION_KEY = "schema-version"

#: Single-instance guard: the running daemon's pid+start as a JSON record.
#: Left behind by a killed daemon on purpose — the next starter reclaims it
#: once liveness fails (goal:15-16, 2-plan §3).
DAEMON_KEY = "daemon"


class RegistryError(RuntimeError):
    """A registry operation could not be performed; the message says why."""


class ClaimConflict(RegistryError):
    """A task row is already claimed by a harness that is still live."""


class DaemonAlreadyRunning(RegistryError):
    """Another daemon holds the guard row and is live at its recorded start."""


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workspaces (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS tasks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id   INTEGER NOT NULL REFERENCES workspaces(id),
    task_relpath   TEXT NOT NULL,
    harness_pid    INTEGER,
    harness_start  REAL,
    provider_pid   INTEGER,
    provider_start REAL,
    UNIQUE (workspace_id, task_relpath)
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def db_path() -> Path:
    """Where the registry lives: ``<home>/.backseat/db.sqlite``.

    ``config._default_home`` is called per invocation (never captured at
    import) precisely so the autouse home pin in tests/conftest.py applies
    to this path as well as to the user config layer.
    """
    return config._default_home() / ".backseat" / DB_FILENAME


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the registry at ``path`` or :func:`db_path`.

    Autocommit mode (``isolation_level=None``): transactions are begun
    explicitly by :func:`_txn` where atomicity is required, so a plain kv or
    row write lands immediately for other connections to see.
    """
    target = path if path is not None else db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.executescript(_SCHEMA_SQL)
    # INSERT OR IGNORE: first connect seeds schema-version, later connects
    # leave a possibly-migrated value alone.
    conn.execute(
        "INSERT OR IGNORE INTO kv (key, value) VALUES (?, ?)",
        (SCHEMA_VERSION_KEY, str(SCHEMA_VERSION)),
    )
    return conn


@contextmanager
def open_db(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """A connection that closes when the block exits."""
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _txn(conn: sqlite3.Connection) -> Iterator[None]:
    """One write transaction, immediate so claim races serialize.

    ``BEGIN IMMEDIATE`` takes the write lock up front: two processes racing
    to claim the same task both read only after the winner commits, so the
    loser sees the live row and gets ClaimConflict instead of a lost update.
    Rollback on any error keeps a failed claim from half-writing.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _process(pid: int) -> psutil.Process:
    """The psutil handle for ``pid``. The seam tests fake (no real pids)."""
    return psutil.Process(int(pid))


def pid_live(pid: int | None, start: float | None = None) -> bool:
    """Is ``pid`` running, and (when ``start`` given) still the *same* process?

    The start-time compare is the whole point: pids recycle, so a row
    recorded against a since-reused pid must read as dead, not live. With
    ``start=None`` only existence can be asked (used where no start was ever
    recorded). ``NoSuchProcess``/``AccessDenied`` mean the pid cannot be
    verified, which counts as dead — a false "live" blocks a claim forever,
    while a false "dead" is caught by the sweep's own re-check before any
    kill. ``ZombieProcess`` subclasses ``NoSuchProcess``.
    """
    if pid is None or pid <= 0:
        return False
    try:
        proc = _process(pid)
        if not proc.is_running():
            return False
        if start is None:
            return True
        return abs(float(proc.create_time()) - float(start)) <= START_TOLERANCE_S
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    except OSError:
        return False


def process_start(pid: int) -> float:
    """psutil's ``create_time()`` for ``pid`` — the start half of a pid+start pair.

    Used for the daemon's own pid at guard claim time: recording it is what
    lets the next starter tell this daemon from an unrelated process that
    later recycles the pid. psutil's failures propagate rather than degrade to
    ``time.time()`` — a guessed start would mint a guard row that no liveness
    check can ever confirm, which is worse than refusing to start.
    """
    return float(_process(pid).create_time())


# --- kv -------------------------------------------------------------------


def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else default


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def workspaces_version(conn: sqlite3.Connection) -> int:
    """Current ``workspaces-version``; 0 before any workspace was ever added."""
    return int(kv_get(conn, WORKSPACES_VERSION_KEY, "0"))


def _bump_workspaces_version_tx(conn: sqlite3.Connection) -> int:
    """Called only when the workspace set actually changed (see goal.md and
    2-plan §1: bump iff insert/delete lands — a no-op add never bumps)."""
    nxt = workspaces_version(conn) + 1
    kv_set(conn, WORKSPACES_VERSION_KEY, str(nxt))
    return nxt


# --- daemon guard -----------------------------------------------------------


def _daemon_record(conn: sqlite3.Connection) -> dict | None:
    """The ``kv["daemon"]`` record as ``{"pid", "start"}``, or None.

    A row that will not parse reads as absent: a corrupt guard must not wedge
    the daemon out of existence forever — the claim below simply overwrites
    it (2-plan §3).
    """
    raw = kv_get(conn, DAEMON_KEY)
    if raw is None:
        return None
    try:
        record = json.loads(raw)
        return {"pid": int(record["pid"]), "start": float(record["start"])}
    except (TypeError, ValueError, KeyError):
        return None


def claim_daemon(conn: sqlite3.Connection, pid: int, start: float) -> dict:
    """Atomically claim the single-instance guard; raise if a live one exists.

    One ``BEGIN IMMEDIATE`` transaction (2-plan §3): read, claim, verify. The
    write lock serializes racing starters, so exactly one reads an empty row
    and writes its record while the loser reads the winner's live pid and
    raises; the re-read after the write closes the door from the other
    direction — a claimant that raced past the first check finds a foreign
    live row instead of silently overwriting it. Losers roll back through
    ``_txn``, leaving the winner's record intact. Returns the record written.
    """
    mine = {"pid": int(pid), "start": float(start)}
    with _txn(conn):
        existing = _daemon_record(conn)
        if existing is not None and pid_live(existing["pid"], existing["start"]):
            raise DaemonAlreadyRunning(
                f"daemon already running: pid {existing['pid']} is live at its "
                "recorded start"
            )
        kv_set(conn, DAEMON_KEY, json.dumps(mine))
        current = _daemon_record(conn)
        if (
            current is not None
            and current != mine
            and pid_live(current["pid"], current["start"])
        ):
            raise DaemonAlreadyRunning(
                f"daemon already running: pid {current['pid']} claimed the guard "
                "first and is live at its recorded start"
            )
        return mine


def clear_daemon(conn: sqlite3.Connection, pid: int, start: float | None = None) -> bool:
    """Release the guard on a clean exit; True iff the row was ours to clear.

    The identity check (pid, plus start when given) keeps an exiting daemon
    from deleting a row that a newer daemon reclaimed after the old pid was
    recycled — the same pid+start rule every row here follows. A stale row
    from a killed daemon needs no clearing: the next starter's claim
    overwrites it.
    """
    with _txn(conn):
        current = _daemon_record(conn)
        if current is None or current["pid"] != int(pid):
            return False
        if start is not None and current["start"] != float(start):
            return False
        conn.execute("DELETE FROM kv WHERE key = ?", (DAEMON_KEY,))
        return True


# --- workspaces -----------------------------------------------------------


def _normalize(path: Path | str) -> str:
    """Canonical stored form: absolute, resolved, ``~`` expanded."""
    return str(Path(path).expanduser().resolve())


def _add_workspace_tx(conn: sqlite3.Connection, path: Path | str) -> bool:
    """Insert-if-missing inside an open transaction; True iff inserted."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO workspaces (path) VALUES (?)", (_normalize(path),)
    )
    if cur.rowcount != 1:
        return False
    _bump_workspaces_version_tx(conn)
    return True


def add_workspace(conn: sqlite3.Connection, path: Path | str) -> bool:
    """Register a workspace; True iff this changed the workspace set.

    The bump lives here so every caller (init/run auto-add,
    register-workspace, claim's implicit add) shares the same invariant:
    the counter moves exactly when a row appears.
    """
    with _txn(conn):
        return _add_workspace_tx(conn, path)


def unregister_workspace(conn: sqlite3.Connection, path: Path | str) -> bool:
    """Remove a workspace; True iff it was listed (and so the set changed).

    Task rows are deliberately left behind: the pre-run sweep scans *all*
    task records regardless of workspace (goal.md), and a provider orphaned
    under an unregistered workspace still has to be found and killed.
    """
    with _txn(conn):
        cur = conn.execute("DELETE FROM workspaces WHERE path = ?", (_normalize(path),))
        if cur.rowcount == 0:
            return False
        _bump_workspaces_version_tx(conn)
        return True


def list_workspaces(conn: sqlite3.Connection) -> list[str]:
    """Every registered workspace path, in registration order."""
    return [row["path"] for row in conn.execute("SELECT path FROM workspaces ORDER BY id")]


def _workspace_id(conn: sqlite3.Connection, path: Path | str) -> int | None:
    row = conn.execute(
        "SELECT id FROM workspaces WHERE path = ?", (_normalize(path),)
    ).fetchone()
    return row["id"] if row is not None else None


# --- tasks ----------------------------------------------------------------


def claim_task(
    conn: sqlite3.Connection,
    workspace: Path | str,
    task_relpath: str,
    harness_pid: int,
    harness_start: float | None = None,
) -> int:
    """Atomically claim a task row for this run; returns the row id.

    Auto-adds the workspace first (goal.md: workspaces are added before a
    task is initialized or run, bumping workspaces-version iff new). Then,
    in one immediate transaction: a row whose harness pid is live at its
    recorded start raises :class:`ClaimConflict` — only one harness may run
    a given task — while a dead harness's stale row is overwritten in place,
    its provider columns cleared (the new run starts with no provider yet).

    ``harness_start`` defaults to now; pass it explicitly in tests.
    """
    start = time.time() if harness_start is None else harness_start
    with _txn(conn):
        # Insert-or-nothing, then re-read: the id is needed either way, and
        # the bump happens only when the insert landed (set-change invariant).
        _add_workspace_tx(conn, workspace)
        ws_id = _workspace_id(conn, workspace)
        row = conn.execute(
            "SELECT harness_pid, harness_start FROM tasks "
            "WHERE workspace_id = ? AND task_relpath = ?",
            (ws_id, task_relpath),
        ).fetchone()
        if row is not None and pid_live(row["harness_pid"], row["harness_start"]):
            raise ClaimConflict(
                f"task {task_relpath!r} in {workspace} is already running: "
                f"harness pid {row['harness_pid']} is live at its recorded start"
            )
        if row is None:
            conn.execute(
                "INSERT INTO tasks (workspace_id, task_relpath, harness_pid, harness_start) "
                "VALUES (?, ?, ?, ?)",
                (ws_id, task_relpath, int(harness_pid), float(start)),
            )
        else:
            conn.execute(
                "UPDATE tasks SET harness_pid = ?, harness_start = ?, "
                "provider_pid = NULL, provider_start = NULL "
                "WHERE workspace_id = ? AND task_relpath = ?",
                (int(harness_pid), float(start), ws_id, task_relpath),
            )
        return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def record_provider(
    conn: sqlite3.Connection,
    workspace: Path | str,
    task_relpath: str,
    provider_pid: int,
    provider_start: float | None = None,
) -> None:
    """Record the provider CLI pid+start into the claimed row.

    Called once per provider spawn (managed turn or operator call): the most
    recent spawn wins. Loud when no row exists — a provider starting for an
    unclaimed task means the claim/release wiring upstream is broken.
    """
    start = time.time() if provider_start is None else provider_start
    with _txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET provider_pid = ?, provider_start = ? "
            "WHERE workspace_id = (SELECT id FROM workspaces WHERE path = ?) "
            "AND task_relpath = ?",
            (int(provider_pid), float(start), _normalize(workspace), task_relpath),
        )
        if cur.rowcount == 0:
            raise RegistryError(
                f"no claimed task row for {task_relpath!r} in {workspace}; "
                "claim_task must run before a provider spawns"
            )


def release_task(conn: sqlite3.Connection, workspace: Path | str, task_relpath: str) -> bool:
    """Delete the task row before the run exits; True iff one existed.

    Returns False (not an error) when the workspace was unregistered or the
    row already vanished — the invariant is "no row after exit", and the
    sweep is the backstop for any path that still leaks one.
    """
    with _txn(conn):
        cur = conn.execute(
            "DELETE FROM tasks WHERE workspace_id = "
            "(SELECT id FROM workspaces WHERE path = ?) AND task_relpath = ?",
            (_normalize(workspace), task_relpath),
        )
        return cur.rowcount > 0


def get_task(
    conn: sqlite3.Connection, workspace: Path | str, task_relpath: str
) -> dict | None:
    """The task row for this workspace-relative lane, or None.

    Resolves through the workspace path, so an unregistered workspace has no
    rows to see here (they remain visible to :func:`list_tasks`, which the
    sweep uses).
    """
    row = conn.execute(
        "SELECT t.id, t.task_relpath, t.harness_pid, t.harness_start, "
        "t.provider_pid, t.provider_start, w.path AS workspace_path "
        "FROM tasks t JOIN workspaces w ON w.id = t.workspace_id "
        "WHERE w.path = ? AND t.task_relpath = ?",
        (_normalize(workspace), task_relpath),
    ).fetchone()
    return dict(row) if row is not None else None


def list_tasks(conn: sqlite3.Connection) -> list[dict]:
    """Every task row across every workspace (LEFT JOIN: rows whose workspace
    was unregistered still appear, with ``workspace_path`` as None) — the
    sweep's worklist."""
    rows = conn.execute(
        "SELECT t.id, t.workspace_id, t.task_relpath, t.harness_pid, "
        "t.harness_start, t.provider_pid, t.provider_start, "
        "w.path AS workspace_path "
        "FROM tasks t LEFT JOIN workspaces w ON w.id = t.workspace_id ORDER BY t.id"
    )
    return [dict(row) for row in rows]


# --- orphan sweep ----------------------------------------------------------


def sweep_orphans(
    conn: sqlite3.Connection,
    *,
    kill: Callable[[int], object] | None = None,
) -> dict[str, int]:
    """Pre-run backstop (goal:11-12): delete rows whose harness is dead,
    killing any still-live orphaned provider first.

    Scans **every** tasks-table row regardless of workspace (2-plan §2
    amendment): liveliness is pid+start, so a live task in another workspace
    is untouched while a crashed harness anywhere is found. Per row whose
    harness pid is dead: when a provider pid was recorded and *confirms* live
    right now (pid+start re-checked here, never from an earlier read), its
    tree dies through :func:`spawn.kill_pid_tree` — the same
    ``taskkill /F /T`` reasoning as the timeout path — then the row is
    deleted either way.

    Two non-negotiables: a failing kill never raises (the sweep runs at the
    start of a run and must not take that run down), and the delete re-checks
    the harness pid it examined — a claim that raced in must not lose its
    fresh row to a sweep that decided on the old one.

    Returns ``{"scanned", "removed", "killed"}``. ``kill`` overrides the
    killer; tests pass a fake so nothing real is ever signalled.
    """
    killer = spawn.kill_pid_tree if kill is None else kill
    scanned = removed = killed = 0
    for row in list_tasks(conn):
        scanned += 1
        if pid_live(row["harness_pid"], row["harness_start"]):
            continue
        if row["provider_pid"] is not None and pid_live(
            row["provider_pid"], row["provider_start"]
        ):
            try:
                killer(int(row["provider_pid"]))
            except Exception:  # noqa: BLE001 - a dead sweep must not kill the run
                pass
            else:
                killed += 1
        cur = conn.execute(
            "DELETE FROM tasks WHERE id = ? AND harness_pid IS ?",
            (row["id"], row["harness_pid"]),
        )
        if cur.rowcount:
            removed += 1
    return {"scanned": scanned, "removed": removed, "killed": killed}
