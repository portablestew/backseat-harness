"""Core data models and the on-disk contract for a task lane.

These dataclasses are the stable seam that a future orchestrator (e.g. a
workspace-templates tool) reads without importing the loop machinery. Every
model here round-trips to plain JSON via ``to_dict`` / ``from_dict`` so the
``.backseat/tasks/<slug>/`` files stay tool-agnostic.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any

# Bump when the on-disk shape of any task file changes incompatibly.
SCHEMA_VERSION = 1


class VerdictKind(str, Enum):
    """What the operator decided after reviewing a managed turn."""

    CONTINUE = "continue"
    DONE = "done"
    ASK_HUMAN = "ask_human"
    PROBLEM = "problem"


class TaskStatus(str, Enum):
    """Lifecycle of a single task lane.

    ``continue`` = keep running (safe to auto-resume if abandoned); ``blocked``
    = needs a human (``ask_human``, a takeover, or harness budget exhaustion);
    ``complete`` = terminal and implicitly approved. There is deliberately no
    ``failed``: the harness cannot fail a goal, it can only run out of budget,
    which is ``blocked``.
    """

    CONTINUE = "continue"
    BLOCKED = "blocked"
    COMPLETE = "complete"


@dataclass(frozen=True)
class Verdict:
    """The operator's structured output, parsed from its final message.

    The harness is the only writer to disk; ``checklist_update`` is a merge
    delta (see checklist.py) applied by the harness, never written verbatim.
    """

    kind: VerdictKind
    next_prompt: str | None = None
    reset_context: bool = False
    checklist_update: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Verdict:
        raw = data.get("verdict", "")
        try:
            kind = VerdictKind(raw)
        except ValueError as exc:
            raise ValueError(f"unknown verdict kind: {raw!r}") from exc
        update = data.get("checklist_update") or {}
        if not isinstance(update, dict):
            raise ValueError("checklist_update must be an object")
        return cls(
            kind=kind,
            next_prompt=data.get("next_prompt"),
            reset_context=bool(data.get("reset_context", False)),
            checklist_update=update,
            notes=data.get("notes", "") or "",
            schema_version=int(data.get("v", SCHEMA_VERSION)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": self.schema_version,
            "verdict": self.kind.value,
            "next_prompt": self.next_prompt,
            "reset_context": self.reset_context,
            "checklist_update": self.checklist_update,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class TurnOutcome:
    """Result of a single managed-agent turn (one CLI process invocation)."""

    exit_code: int
    timed_out: bool
    duration_s: float
    native_session_id: str | None = None
    transcript_bytes: int = 0

    @property
    def had_problem(self) -> bool:
        """Uniform-exit philosophy: any nonzero exit or timeout is a hint only."""
        return self.timed_out or self.exit_code != 0


@dataclass(frozen=True)
class Generation:
    """One managed-session generation within a task's compaction chain.

    A new generation begins when the transcript is compacted by re-forking into
    a fresh session (see compact.py). ``n`` is the ordinal in the ``<meta>``
    marker; ``native_id`` is captured from the provider, never minted by us.
    """

    n: int
    native_id: str | None = None
    transcript_bytes: int = 0
    mtime: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "native_id": self.native_id,
            "transcript_bytes": self.transcript_bytes,
            "mtime": self.mtime,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Generation:
        return cls(
            n=int(data["n"]),
            native_id=data.get("native_id"),
            transcript_bytes=int(data.get("transcript_bytes", 0)),
            mtime=float(data.get("mtime", 0.0)),
        )


@dataclass
class Binding:
    """Cache mapping a stable root UUID to its chain of generations.

    Identity is the ``<meta>START SESSION <root_uuid> #n</meta>`` marker in the
    managed session's first user message; this file is a recoverable cache, not
    the source of truth. If lost, the chain is reconstructible by searching for
    the marker.
    """

    root_uuid: str
    provider: str
    generations: list[Generation] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    @property
    def current(self) -> Generation | None:
        return self.generations[-1] if self.generations else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": self.schema_version,
            "root_uuid": self.root_uuid,
            "provider": self.provider,
            "generations": [g.to_dict() for g in self.generations],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Binding:
        return cls(
            root_uuid=data["root_uuid"],
            provider=data["provider"],
            generations=[Generation.from_dict(g) for g in data.get("generations", [])],
            schema_version=int(data.get("v", SCHEMA_VERSION)),
        )


@dataclass
class TaskState:
    """Mutable status of a task lane, persisted to state.json."""

    slug: str
    status: TaskStatus = TaskStatus.CONTINUE
    iteration: int = 0
    generation: int = 1
    #: Why the lane is in a terminal state (an ``ask_human`` question or a
    #: budget guard). Empty while the lane is ``continue``.
    reason: str = ""
    updated_at: float = field(default_factory=time.time)
    schema_version: int = SCHEMA_VERSION

    def touch(self) -> TaskState:
        self.updated_at = time.time()
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": self.schema_version,
            "slug": self.slug,
            "status": self.status.value,
            "iteration": self.iteration,
            "generation": self.generation,
            "updated_at": self.updated_at,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskState:
        return cls(
            slug=data["slug"],
            status=TaskStatus(data.get("status", TaskStatus.CONTINUE.value)),
            iteration=int(data.get("iteration", 0)),
            generation=int(data.get("generation", 1)),
            reason=data.get("reason", "") or "",
            updated_at=float(data.get("updated_at", time.time())),
            schema_version=int(data.get("v", SCHEMA_VERSION)),
        )


@dataclass(frozen=True)
class LedgerEntry:
    """One append-only record of a completed iteration (ledger.jsonl)."""

    iteration: int
    generation: int
    verdict: VerdictKind
    exit_code: int
    timed_out: bool
    duration_s: float
    notes: str = ""
    #: The exact prompt the operator sent the managed agent for the next turn
    #: (None for terminal verdicts that queue no prompt). Recording it makes the
    #: ledger a full audit trail of the conversation the operator drove.
    next_prompt: str | None = None
    #: True when the operator abandoned the session chat (fresh context next turn).
    reset_context: bool = False
    at: float = field(default_factory=time.time)
    #: "review" (after a managed turn) or "plan" (operator only, no turn).
    phase: str = "review"

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            # Derived, never read back: `at` stays the float so sorting and
            # arithmetic on the audit trail are unaffected, and this is what a
            # human reads. Written local-time with an offset rather than UTC so
            # the wall-clock in a log line matches the one in the terminal.
            "at_local": datetime.fromtimestamp(self.at).astimezone().isoformat(
                timespec="seconds"
            ),
            "phase": self.phase,
            "iteration": self.iteration,
            "generation": self.generation,
            "verdict": self.verdict.value,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_s": self.duration_s,
            "notes": self.notes,
            "next_prompt": self.next_prompt,
            "reset_context": self.reset_context,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LedgerEntry:
        return cls(
            iteration=int(data["iteration"]),
            generation=int(data.get("generation", 1)),
            verdict=VerdictKind(data["verdict"]),
            exit_code=int(data.get("exit_code", 0)),
            timed_out=bool(data.get("timed_out", False)),
            duration_s=float(data.get("duration_s", 0.0)),
            notes=data.get("notes", "") or "",
            next_prompt=data.get("next_prompt"),
            reset_context=bool(data.get("reset_context", False)),
            at=float(data.get("at", time.time())),
            phase=data.get("phase", "review") or "review",
        )


@dataclass(frozen=True)
class TaskResult:
    """Return value of run_task: the terminal outcome of a task lane."""

    slug: str
    status: TaskStatus
    iterations: int
    final_verdict: VerdictKind | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "status": self.status.value,
            "iterations": self.iterations,
            "final_verdict": self.final_verdict.value if self.final_verdict else None,
            "reason": self.reason,
        }


def next_generation(binding: Binding, n: int) -> Binding:
    """Return a copy of ``binding`` with a fresh generation appended."""
    gens = list(binding.generations)
    gens.append(Generation(n=n, mtime=time.time()))
    return replace(binding, generations=gens)
