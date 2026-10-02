"""Read/write helpers for the ``.backseat/tasks/<slug>/`` files.

The harness is the sole writer. Writes are atomic (temp file + replace) so a
crash mid-write cannot leave a half-written state.json or binding.json. The
ledger is append-only JSONL: one line per completed iteration.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

from backseat_harness.config import TaskPaths
from backseat_harness.models import Binding, LedgerEntry, TaskState


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_state(paths: TaskPaths, slug: str) -> TaskState:
    if not paths.state.exists():
        return TaskState(slug=slug)
    return TaskState.from_dict(json.loads(paths.state.read_text(encoding="utf-8")))


def save_state(paths: TaskPaths, state: TaskState) -> None:
    _atomic_write(paths.state, json.dumps(state.touch().to_dict(), indent=2) + "\n")


def load_binding(paths: TaskPaths) -> Binding | None:
    if not paths.binding.exists():
        return None
    return Binding.from_dict(json.loads(paths.binding.read_text(encoding="utf-8")))


def save_binding(paths: TaskPaths, binding: Binding) -> None:
    _atomic_write(paths.binding, json.dumps(binding.to_dict(), indent=2) + "\n")


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
