"""Per-call provider logs, one file per provider invocation.

Every provider CLI already runs in a machine-readable mode (opencode
``--format json``, claude and kiro ``--output-format stream-json``, Command Code
``--output-format json``), and none of
them has a TTY here, so a call's output is newline-delimited JSON with no spinner
or box-drawing to strip. That makes the log worth keeping verbatim: it is the raw
event stream, re-parseable later, rather than a rendering of one.

The child's output *is* the log file, handed to it as a file descriptor rather
than drained through a pipe. Two things follow. The kernel writes each line as
the child emits it, so ``tail -f`` on a log shows a call that is still running --
including one that has hung, which is the case a log buffered to the end of the
call cannot explain. And a survivor of the tree kill can only append to its own
abandoned file, where a surviving writer on an inherited pipe holds the caller
past its deadline; that pipe is what made the deadline hard to enforce in the
first place (see spawn.py).

Reading the text back needs ``errors="replace"``, the same as the stream it
replaced, or a multibyte character cut at a buffer boundary raises instead of
degrading. And :func:`redact_file` runs *after* the caller has parsed what it
needed: the log carries the task's root UUID, and an operator that reads and
quotes a log file makes its own stored session match the task's marker search,
where the newest match wins and points sizing and compaction at the operator
instead of the managed agent (see marker.redact).

Nothing here prunes. Files are unique per call, so a retention policy is purely
additive later, and a run that died mid-call leaves its partial log behind rather
than losing it to a buffer that was never flushed.
"""

from __future__ import annotations

import contextvars
from pathlib import Path

from backseat_harness import marker as marker_mod
from backseat_harness.config import TaskPaths

#: How a call is labeled in its filename. The managed side and the operator side
#: are the two roles that spawn a process; the operator is numbered per attempt
#: so a re-asked verdict leaves one file per thing that was tried rather than
#: overwriting the answer that came back unusable.
ROLE_MANAGED = "managed"
ROLE_OPERATOR = "operator"

#: The log path of the call about to be made, for a caller handed a one-argument
#: callable and so unable to be told the path (see cli._make_operator_invoker).
#: Set by the loop immediately before the call, consumed once by the invoker. A
#: ContextVar rather than a module global so a future concurrent lane cannot pick
#: up another lane's path.
_current: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "backseat_harness_call_log", default=None
)


def call_log(paths: TaskPaths, iteration: int, role: str, attempt: int = 0) -> Path:
    """Return the log path for one call, creating the directory.

    Zero-padded so ``ls`` sorts chronologically, and unique per call so a process
    that outlives its deadline can only ever append to a file the loop has already
    moved on from.
    """
    paths.logs.mkdir(parents=True, exist_ok=True)
    if role == ROLE_MANAGED:
        return paths.logs / f"iter-{iteration:04d}.{role}.log"
    return paths.logs / f"iter-{iteration:04d}.{role}.{attempt}.log"


def manager_log(root: Path, phase: str, n: int) -> Path:
    """Log path for one generate-task call: ``<root>/logs/manager.<phase>.<n>.log``.

    Numbered per call, so a re-asked call keeps the reply that came back
    unusable, the same reason the operator's attempts are numbered. ``root`` is
    the staging dir, which is removed with the rest of the staging at promotion.
    """
    logs_dir = root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir / f"manager.{phase}.{n}.log"


def set_current(path: Path | None) -> None:
    """Declare the log path of the call the next invoked callable will make."""
    _current.set(path)


def current() -> Path | None:
    """The log path declared by the most recent :func:`set_current`."""
    return _current.get()


def redact_file(path: Path, root_uuid: str) -> None:
    """Rewrite a finished call log with the task's root UUID removed.

    Called by the loop, which owns the binding, after the caller has parsed what
    it needed. The file rather than the string the caller held: the on-disk log
    is the artifact an operator (or a human) may read, so this is the one place
    that guarantees what lands there, and it runs after parsing rather than
    before so redaction cannot touch a verdict.

    A log that is not there is nothing to redact: a call whose output never went
    to disk (an injected runner in a test, a launch that failed before the file
    was written) carries no UUID to leak, so this returns rather than raising.
    A log that exists but cannot be rewritten *does* raise -- that is a broken
    disk, which ends the run anyway one commit later, and the root UUID would
    otherwise sit on disk unredacted.
    """
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    path.write_text(marker_mod.redact(text, root_uuid), encoding="utf-8")