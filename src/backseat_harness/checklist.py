"""Durable, append-only-in-spirit checklist with a safe merge protocol.

The operator emits a ``checklist_update`` delta; the harness is the only
writer. The rules (locked in design) exist so an agent can add steps or advance
their status but can *never* erase history:

* Shape is a **map** (``steps.<id>``), not an array, so updates upsert by key
  instead of replacing wholesale (arrays are how agents accidentally erase).
* ``null`` values are stripped before merge (RFC 7396 would treat them as
  deletes; we forbid deletes).
* Per-step fields are whitelisted; unknown fields are dropped.
* Status may be set to any valid value (``pending``, ``active``, ``blocked``,
  ``done``, ``cancelled``); there is no forward-only ordering, so the operator
  can move a step back to redo it. Invalid statuses are ignored.
* A freeform ``remarks`` channel appends narrative without touching structure.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backseat_harness import storage

# Allowed per-step fields. Anything else in a delta is ignored. ``model`` is a
# pure pass-through: it names a model *slot* for the harness to resolve against
# the ``[models]`` config (loop-side, per docs 2-plan §2/§3); this module stores
# and merges it without any semantics of its own. ``updated_at`` is
# harness-owned: ``merge_update`` stamps it from the harness clock whenever a
# step's content actually changes, and an incoming value is ignored.
STEP_FIELDS = {"title", "status", "notes", "evidence", "updated_at", "model"}

# Valid per-step statuses. There is no ordering: the operator may move a step
# to any of these freely, including back to redo it. ``cancelled`` is the
# vocabulary for work the owner decided against -- a brief that de-scopes a step
# needs somewhere to put it, and the alternatives were both wrong: ``done`` is a
# lie that corrupts the completion gate, and ``pending`` forever leaves the lane
# waiting on work nobody wants.
STEP_STATUSES = {"pending", "active", "blocked", "done", "cancelled"}
DEFAULT_STATUS = "pending"

#: Statuses that need no further work. ``cancelled`` counts because a step the
#: owner declined is finished, not outstanding -- otherwise a de-scoped step
#: keeps the lane open forever.
TERMINAL_STATUSES = frozenset({"done", "cancelled"})


@dataclass
class Checklist:
    """In-memory checklist. ``steps`` maps step id -> field dict."""

    steps: dict[str, dict[str, Any]] = field(default_factory=dict)
    remarks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"steps": self.steps, "remarks": self.remarks}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Checklist:
        steps = data.get("steps") or {}
        if not isinstance(steps, dict):
            raise ValueError("checklist 'steps' must be an object keyed by step id")
        remarks = list(data.get("remarks") or [])
        return cls(steps={k: dict(v) for k, v in steps.items()}, remarks=remarks)


def _strip_nulls(obj: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in obj.items() if v is not None}


def merge_update(
    current: Checklist, update: dict[str, Any], *, now: float | None = None
) -> Checklist:
    """Merge a validated ``checklist_update`` delta into ``current``.

    Returns a new Checklist; never mutates ``current`` in place. Rejects nothing
    hard here (the loop treats a bad verdict as a 'problem'); instead it applies
    only the safe subset so a malformed delta cannot corrupt the file.

    ``updated_at`` belongs to the harness, not the operator: every step whose
    content this delta actually changes gets the current harness time, while a
    no-op resend (or a delta that only carries ``updated_at``) keeps the old
    stamp. ``now`` is injectable so tests can pin the clock.
    """
    stamp = time.time() if now is None else now
    steps = {k: dict(v) for k, v in current.steps.items()}
    remarks = list(current.remarks)

    delta_steps = update.get("steps") or {}
    if isinstance(delta_steps, dict):
        for step_id, raw in delta_steps.items():
            if not isinstance(raw, dict):
                continue
            incoming = _strip_nulls(
                {
                    k: v
                    for k, v in raw.items()
                    if k in STEP_FIELDS and k != "updated_at"
                }
            )
            existing = steps.get(step_id, {})
            merged = dict(existing)
            merged.update(incoming)
            new_status = merged.get("status", existing.get("status", DEFAULT_STATUS))
            if new_status in STEP_STATUSES:
                merged["status"] = new_status
            else:
                merged["status"] = existing.get("status", DEFAULT_STATUS)
            if merged != existing:
                merged["updated_at"] = stamp
            steps[step_id] = merged

    delta_remarks = update.get("remarks")
    if isinstance(delta_remarks, list):
        remarks.extend(str(r) for r in delta_remarks if r is not None)

    return Checklist(steps=steps, remarks=remarks)


def load_checklist(path: Path) -> Checklist:
    if not path.exists():
        return Checklist()
    return Checklist.from_dict(json.loads(path.read_text(encoding="utf-8")))


def save_checklist(path: Path, checklist: Checklist) -> None:
    """Write the checklist through the same atomic, retried door as state/binding.

    This is the *first* write in ``loop._persist``'s crash-safe ordering, so a
    torn file here costs the most of any: ``load_checklist`` parses JSON
    unconditionally, and a truncated tail raises rather than degrading, which
    bricks the lane on the next read. ``ensure_ascii=False`` is kept -- the
    operator writes non-ASCII titles and remarks -- and a bare ``write_text``
    would also have left this the one write in the run that neither survives a
    partial write nor retries a contended swap.
    """
    storage.atomic_write(
        path, json.dumps(checklist.to_dict(), indent=2, ensure_ascii=False) + "\n"
    )


def all_done(checklist: Checklist) -> bool:
    """True when there is at least one step and none is still outstanding.

    ``cancelled`` satisfies the gate alongside ``done``: the owner declining a
    step settles it. A lane whose every step was cancelled is therefore
    complete, which is the honest reading -- the work is finished and the answer
    is "don't build it". The operator is still the one who set those statuses,
    and this is the same trust the gate already extends to an all-``done``
    checklist completing the lane on a non-``done`` verdict.
    """
    if not checklist.steps:
        return False
    return all(s.get("status") in TERMINAL_STATUSES for s in checklist.steps.values())
