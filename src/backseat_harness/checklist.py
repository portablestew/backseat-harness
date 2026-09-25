"""Durable, append-only-in-spirit checklist with a safe merge protocol.

The system agent emits a ``checklist_update`` delta; the harness is the only
writer. The rules (locked in design) exist so an agent can add steps or advance
their status but can *never* erase history:

* Shape is a **map** (``steps.<id>``), not an array, so updates upsert by key
  instead of replacing wholesale (arrays are how agents accidentally erase).
* ``null`` values are stripped before merge (RFC 7396 would treat them as
  deletes; we forbid deletes).
* Per-step fields are whitelisted; unknown fields are dropped.
* Status is forward-only (``pending -> active -> done``; ``blocked`` is a
  sidecar) unless the delta explicitly sets ``reopen: true``.
* A freeform ``remarks`` channel appends narrative without touching structure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Allowed per-step fields. Anything else in a delta is ignored.
STEP_FIELDS = {"title", "status", "notes", "evidence", "updated_at"}

# Forward-only status ordering. Higher rank cannot regress to lower unless the
# delta carries reopen=true for that step.
STATUS_RANK = {"pending": 0, "active": 1, "blocked": 1, "done": 2}
DEFAULT_STATUS = "pending"


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


def merge_update(current: Checklist, update: dict[str, Any]) -> Checklist:
    """Merge a validated ``checklist_update`` delta into ``current``.

    Returns a new Checklist; never mutates ``current`` in place. Rejects nothing
    hard here (the loop treats a bad verdict as a 'problem'); instead it applies
    only the safe subset so a malformed delta cannot corrupt the file.
    """
    steps = {k: dict(v) for k, v in current.steps.items()}
    remarks = list(current.remarks)

    delta_steps = update.get("steps") or {}
    if isinstance(delta_steps, dict):
        for step_id, raw in delta_steps.items():
            if not isinstance(raw, dict):
                continue
            incoming = _strip_nulls({k: v for k, v in raw.items() if k in STEP_FIELDS})
            reopen = bool(raw.get("reopen"))
            existing = steps.get(step_id, {})
            merged = dict(existing)
            merged.update(incoming)
            # Enforce forward-only status unless reopening.
            new_status = merged.get("status", existing.get("status", DEFAULT_STATUS))
            old_status = existing.get("status", DEFAULT_STATUS)
            if not reopen and _rank(new_status) < _rank(old_status):
                merged["status"] = old_status
            else:
                merged["status"] = new_status
            steps[step_id] = merged

    delta_remarks = update.get("remarks")
    if isinstance(delta_remarks, list):
        remarks.extend(str(r) for r in delta_remarks if r is not None)

    return Checklist(steps=steps, remarks=remarks)


def _rank(status: str) -> int:
    return STATUS_RANK.get(status, 0)


def load_checklist(path: Path) -> Checklist:
    if not path.exists():
        return Checklist()
    return Checklist.from_dict(json.loads(path.read_text(encoding="utf-8")))


def save_checklist(path: Path, checklist: Checklist) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(checklist.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def all_done(checklist: Checklist) -> bool:
    """True when there is at least one step and every step is done."""
    if not checklist.steps:
        return False
    return all(s.get("status") == "done" for s in checklist.steps.values())
