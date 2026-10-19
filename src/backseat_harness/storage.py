"""Read/write helpers for the ``.backseat/tasks/<slug>/`` files.

The harness is the sole writer. Writes are atomic (temp file + replace) so a
crash mid-write cannot leave a half-written state.json or binding.json, and the
replace is retried because these files sit inside the watched workspace, where a
concurrent reader can briefly deny the swap. The ledger is append-only JSONL: one
line per completed iteration.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from backseat_harness.config import TaskPaths
from backseat_harness.models import Binding, LedgerEntry, TaskState

#: A contended write gets this many attempts, ``RETRY_DELAY_S`` apart. On Windows
#: ``os.replace`` is ``MoveFileEx``, which fails with ERROR_ACCESS_DENIED
#: (WinError 5) when the destination is open in another process that did not share
#: delete -- an editor, a file watcher, an indexer, or a virus scanner reacting to
#: the write we just made. These files live inside the workspace, so something is
#: nearly always watching them, and ``_persist`` writes ``state.json`` and then
#: ``_finish`` writes it again with nothing in between: the second write lands
#: exactly in the window where that watcher still holds the first result open.
#: A losing race is a blip, not a verdict, and is worth a couple of seconds of
#: waiting rather than a crashed run -- the same reasoning, and deliberately the
#: same constants, as ``history._retrying``.
RETRY_ATTEMPTS = 3
RETRY_DELAY_S = 1.0


def atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path``, retrying a Windows-contended replace.

    Exhausting the attempts re-raises the original error. A write that cannot
    land is reported, never swallowed: losing one silently would leave the lane
    claiming a commit that never reached disk, which is the one outcome worse
    than stopping.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
            return
        except OSError as exc:
            if attempt == RETRY_ATTEMPTS:
                raise
            print(
                f"[warn] task: write {path.name} failed ({exc}); retrying in "
                f"{RETRY_DELAY_S:g}s (attempt {attempt}/{RETRY_ATTEMPTS})",
                file=sys.stderr,
            )
            time.sleep(RETRY_DELAY_S)
        finally:
            # Gone once the replace succeeds, so the unlink is a no-op there. On a
            # failed attempt it clears our own temp file -- and it can hit the very
            # scanner that blocked the replace, which must not displace the error
            # we are about to raise.
            try:
                os.unlink(tmp)
            except OSError:
                pass


def load_state(paths: TaskPaths, slug: str) -> TaskState:
    if not paths.state.exists():
        return TaskState(slug=slug)
    return TaskState.from_dict(json.loads(paths.state.read_text(encoding="utf-8")))


def save_state(paths: TaskPaths, state: TaskState) -> None:
    atomic_write(paths.state, json.dumps(state.touch().to_dict(), indent=2) + "\n")


def load_run_request(paths: TaskPaths) -> dict | None:
    """The run journal, or None when it is absent or unreadable.

    An unreadable journal counts as absent: the next run replaces it anyway.
    """
    try:
        data = json.loads(paths.run_request.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_run_request(paths: TaskPaths, brief: str | None, started_at: float) -> None:
    atomic_write(
        paths.run_request,
        json.dumps({"v": 1, "started_at": started_at, "brief": brief}, indent=2) + "\n",
    )


def clear_run_request(paths: TaskPaths) -> None:
    paths.run_request.unlink(missing_ok=True)


def load_binding(paths: TaskPaths) -> Binding | None:
    if not paths.binding.exists():
        return None
    return Binding.from_dict(json.loads(paths.binding.read_text(encoding="utf-8")))


def save_binding(paths: TaskPaths, binding: Binding) -> None:
    atomic_write(paths.binding, json.dumps(binding.to_dict(), indent=2) + "\n")


def append_ledger(paths: TaskPaths, entry: LedgerEntry) -> None:
    paths.ledger.parent.mkdir(parents=True, exist_ok=True)
    with paths.ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry.to_dict()) + "\n")


def read_ledger(paths: TaskPaths) -> Iterator[LedgerEntry]:
    if not paths.ledger.exists():
        return
    for line in paths.ledger.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            yield LedgerEntry.from_dict(json.loads(line))


def read_goal(paths: TaskPaths) -> str:
    if not paths.goal.exists():
        raise FileNotFoundError(f"missing goal-prompt: {paths.goal}")
    return paths.goal.read_text(encoding="utf-8")
