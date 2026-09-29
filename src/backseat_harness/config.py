"""Harness configuration and the ``.backseat/`` workspace layout.

Defaults live in code; ``.backseat/config.toml`` overrides them, and a task dir
may override sparingly. Nothing here reads global state at import time: every
path is derived from an explicit workspace root so the library stays
cwd-independent and orchestrator-friendly.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import tomllib

# One hour default managed-turn timeout, per the design. A one-shot turn should
# bail rather than hang, so this is a hard ceiling with a watchdog on top.
DEFAULT_TURN_TIMEOUT_S = 3600

# ~1MB transcript ~= 250-300k tokens: the preemptive compaction trigger.
DEFAULT_COMPACT_THRESHOLD_BYTES = 1_000_000

# Loop budget guards so the harness can never pink-sky into infinity.
DEFAULT_MAX_ITERATIONS = 50
DEFAULT_MAX_WALL_CLOCK_S = 24 * 3600
# Cap on compaction generations to guard against a fork that itself exceeds the
# threshold immediately (re-fork loop).
DEFAULT_MAX_GENERATIONS = 20


@dataclass(frozen=True)
class OperatorConfig:
    """Profile for the ephemeral operator agent.

    Same underlying CLI as the managed agent but a separate, often stronger
    model and its own agent definition. The harness grants it no permissions:
    ``agent`` and ``extra_args`` are the workspace owner's channel.
    """

    provider: str = "opencode"
    model: str | None = None
    agent: str | None = None  # maps to --agent / profile name where supported
    verify_commands: list[str] = field(default_factory=list)
    # Extra CLI flags for the operator's invocation (see Config.extra_args).
    extra_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    """Resolved harness configuration for a workspace."""

    workspace_root: Path
    provider: str = "opencode"
    model: str | None = None
    # Named provider agent for the managed side (e.g. opencode ``--agent``).
    # Unlike the operator, the harness writes no managed profile: this simply
    # forwards the flag so the workspace owner can run a task under their own
    # agent definition instead of the CLI default.
    agent: str | None = None
    turn_timeout_s: int = DEFAULT_TURN_TIMEOUT_S
    compact_threshold_bytes: int = DEFAULT_COMPACT_THRESHOLD_BYTES
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    max_wall_clock_s: int = DEFAULT_MAX_WALL_CLOCK_S
    max_generations: int = DEFAULT_MAX_GENERATIONS
    # Provider-specific flags passed through to every managed turn. This is how
    # the workspace owner grants the CLI its non-interactive permissions: the
    # harness deliberately picks no default, because the safe-vs-usable tradeoff
    # is the owner's to make, and a turn that blocks on an approval prompt just
    # burns the timeout.
    extra_args: tuple[str, ...] = ()
    operator: OperatorConfig = field(default_factory=OperatorConfig)

    # --- Path helpers: the entire .backseat/ contract lives here. ---

    @property
    def backseat_dir(self) -> Path:
        return self.workspace_root / ".backseat"

    @property
    def config_path(self) -> Path:
        return self.backseat_dir / "config.toml"

    @property
    def tasks_dir(self) -> Path:
        return self.backseat_dir / "tasks"

    def task_dir(self, slug: str) -> Path:
        return self.tasks_dir / slug

    def task_paths(self, slug: str) -> TaskPaths:
        return TaskPaths(self.task_dir(slug))


@dataclass(frozen=True)
class TaskPaths:
    """Resolved file paths for a single task lane."""

    root: Path

    @property
    def meta(self) -> Path:
        return self.root / "meta.md"

    @property
    def checklist(self) -> Path:
        return self.root / "checklist.json"

    @property
    def binding(self) -> Path:
        return self.root / "binding.json"

    @property
    def ledger(self) -> Path:
        return self.root / "ledger.jsonl"

    @property
    def docs(self) -> Path:
        """Handoff docs written by the managed agent (research, plan, reviews).

        The one place under ``.backseat`` the managed agent is asked to write.
        The harness creates it but never writes inside it.
        """
        return self.root / "docs"

    @property
    def transcript(self) -> Path:
        """Full session transcript the harness overwrites after each managed turn.

        The complete, untruncated (marker-redacted) record of the managed
        session, for operator and human review -- including tool-call results,
        which the compaction fork elides. One file, rewritten each turn;
        compaction forks a new session per generation, so this tracks the
        current generation's work.
        """
        return self.root / "transcript.md"

    @property
    def state(self) -> Path:
        return self.root / "state.json"


def _tomllib():
    # tomllib is stdlib on 3.11+; fall back gracefully on 3.10.
    if sys.version_info >= (3, 11):
        return tomllib
    try:
        import tomli  # type: ignore

        return tomli
    except ModuleNotFoundError as exc:  # pragma: no cover - env dependent
        raise RuntimeError(
            "Reading .backseat/config.toml on Python 3.10 requires the 'tomli' package."
        ) from exc


def load_config(workspace_root: Path | str) -> Config:
    """Load config from ``<workspace>/.backseat/config.toml`` over code defaults."""
    root = Path(workspace_root).resolve()
    cfg = Config(workspace_root=root)
    path = cfg.config_path
    if not path.exists():
        return cfg
    data: dict[str, Any] = _tomllib().loads(path.read_text(encoding="utf-8"))
    return _apply_overrides(cfg, data)


def _apply_overrides(cfg: Config, data: dict[str, Any]) -> Config:
    harness = data.get("harness", {})
    op_data = data.get("operator", {})
    operator = replace(
        cfg.operator,
        provider=op_data.get("provider", cfg.operator.provider),
        model=op_data.get("model", cfg.operator.model),
        agent=op_data.get("agent", cfg.operator.agent),
        verify_commands=list(op_data.get("verify_commands", cfg.operator.verify_commands)),
        extra_args=tuple(op_data.get("extra_args", cfg.operator.extra_args)),
    )
    return replace(
        cfg,
        provider=harness.get("provider", cfg.provider),
        model=harness.get("model", cfg.model),
        agent=harness.get("agent", cfg.agent),
        extra_args=tuple(harness.get("extra_args", cfg.extra_args)),
        turn_timeout_s=int(harness.get("turn_timeout_s", cfg.turn_timeout_s)),
        compact_threshold_bytes=int(
            harness.get("compact_threshold_bytes", cfg.compact_threshold_bytes)
        ),
        max_iterations=int(harness.get("max_iterations", cfg.max_iterations)),
        max_wall_clock_s=int(harness.get("max_wall_clock_s", cfg.max_wall_clock_s)),
        max_generations=int(harness.get("max_generations", cfg.max_generations)),
        operator=operator,
    )
