"""backseat-harness: drive an off-the-shelf CLI agent one small step at a time.

A meta-harness that spawns a managed CLI agent (Claude Code, opencode, kiro-cli),
lets it take a single turn, then hands a fresh adversarial *operator* the
transcript plus a durable checklist to review progress and decide the next prompt.

__version__ is the single source of truth for the package version. pyproject.toml
reads it from here (via tool.hatch.version), so this is the only place to edit
before a release.
"""

__version__ = "0.0.1"

from backseat_harness.models import (
    Binding,
    Generation,
    LedgerEntry,
    TaskResult,
    TaskState,
    TurnOutcome,
    Verdict,
    VerdictKind,
)

__all__ = [
    "__version__",
    "Binding",
    "Generation",
    "LedgerEntry",
    "TaskResult",
    "TaskState",
    "TurnOutcome",
    "Verdict",
    "VerdictKind",
]
