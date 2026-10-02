"""Harness configuration and the ``.backseat/`` workspace layout.

Defaults live in code, then config resolves in layers of increasing precedence:
``~/.backseat/config.toml`` (user) → ``.backseat/config.toml`` (workspace) →
``.backseat/tasks/<slug>/config.toml`` (task, when a slug is given). Higher
layers win; arrays are replaced wholesale; ``[models]`` slot names merge per
key instead, since slots are shared vocabulary and a layer rebinds a name
rather than restating all of them; the task layer may override anything,
``[operator]`` included. The user layer derives from the home dir via
``_default_home``; the rest comes from an explicit workspace root, so the library
stays cwd-independent and orchestrator-friendly. Nothing here reads global state
at import time.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import tomllib

# One hour default managed-turn timeout, per the design. A one-shot turn should
# bail rather than hang, so this is a hard ceiling enforced by spawn.run_with_timeout:
# it kills the whole process tree on expiry (a bare kill leaves an npm shim's
# child holding the output pipe) and bounds every teardown step after the
# deadline -- the pipe close included, for a holder the tree walk misses -- to
# one grace budget, so the ceiling lands either way.
DEFAULT_TURN_TIMEOUT_S = 3600

# The operator's only job is to read a turn and emit one JSON verdict, so it gets
# a tighter ceiling than a managed turn that may run a test suite. Separate
# because the two are not the same kind of work: raise this per task when the
# operator runs expensive validation, and lower operator_retry_budget to keep the
# worst case bounded, since the budget multiplies this -- each call costs at most
# this plus one grace budget (spawn's enforced bound).
DEFAULT_OPERATOR_TIMEOUT_S = 1800

# ~1MB transcript ~= 250-300k tokens: the preemptive compaction trigger.
DEFAULT_COMPACT_THRESHOLD_BYTES = 1_000_000

# Loop budget guards, all scoped to one run_task call so no single invocation can
# pink-sky into infinity: a lane is bounded per run, and the owner's next `run` is
# a fresh budget. 30 rather than 50 to keep chunks small and let progress arrive in
# more, shorter runs instead of one long unattended one.
DEFAULT_MAX_ITERATIONS = 30
DEFAULT_MAX_WALL_CLOCK_S = 24 * 3600
# Cap on compaction generations to guard against a fork that itself exceeds the
# threshold immediately (re-fork loop).
DEFAULT_MAX_GENERATIONS = 20
# Consecutive identical-prompt PROBLEM verdicts before the loop gives up. Tighter
# than max_iterations on purpose: a stuck loop re-sends one prompt forever, and
# the interesting signal is the repetition, not the count. 0 disables the cap and
# leaves max_iterations governing.
DEFAULT_PROBLEM_REPEAT_CAP = 3
# Provider invocations allowed for one operator verdict, counting the first. A
# clean answer never touches it; an unparsable envelope and a bad model slot
# draw on the same budget, because to the loop they are one failure -- the
# operator's answer was not usable. Matches problem_repeat_cap's convention: a
# count of occurrences, not of retries on top. 1 means no re-ask.
DEFAULT_OPERATOR_RETRY_BUDGET = 3


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
    # Ceiling on one operator call, independent of the managed turn's: the
    # operator reads and decides, the agent builds and runs things, so the same
    # hour is generous for one and a stall for the other.
    timeout_s: int = DEFAULT_OPERATOR_TIMEOUT_S


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
    # Managed turns one run_task call may spend. Per run, not per lane:
    # TaskState.iteration keeps counting across runs for the ledger and the log
    # names, and this bounds a fresh budget against it.
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    max_wall_clock_s: int = DEFAULT_MAX_WALL_CLOCK_S
    max_generations: int = DEFAULT_MAX_GENERATIONS
    # Max consecutive PROBLEM verdicts that re-send the same prompt before the
    # run is BLOCKED as stuck. Counts identical-prompt PROBLEMs only, so a
    # recovery that keeps changing the prompt is bounded by max_iterations as
    # before. 0 disables the cap.
    problem_repeat_cap: int = DEFAULT_PROBLEM_REPEAT_CAP
    # Provider invocations allowed per operator verdict, counting the first.
    # Bounds the worst case of a wedged operator at budget * operator.timeout_s;
    # lower it when raising that timeout for a validation-heavy task.
    operator_retry_budget: int = DEFAULT_OPERATOR_RETRY_BUDGET
    # Provider-specific flags passed through to every managed turn. This is how
    # the workspace owner grants the CLI its non-interactive permissions: the
    # harness deliberately picks no default, because the safe-vs-usable tradeoff
    # is the owner's to make, and a turn that blocks on an approval prompt just
    # burns the timeout.
    extra_args: tuple[str, ...] = ()
    # Per-step model slots: names the operator may set on a checklist step to
    # route that step's turns through a concrete ``--model``. Keys are slots by
    # virtue of existing; ``default`` is the only reserved key (the binding for
    # a step naming no slot). Values are opaque model ids, passed verbatim to
    # the managed CLI. ``[operator].model`` is deliberately not a slot: it
    # profiles the operator itself, which is never routed per step.
    models: dict[str, str] = field(default_factory=dict)
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
    def goal(self) -> Path:
        """The task's goal-prompt: what the owner asked for, fixed once written."""
        return self.root / "goal.md"

    @property
    def config(self) -> Path:
        return self.root / "config.toml"

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
    def logs(self) -> Path:
        """One log file per provider call: managed turns and operator attempts.

        The child's stdout *is* the file, handed to it as a descriptor, so a
        call that is still running -- or has hung -- is tailable while it
        happens. Written by the harness, read by whoever is diagnosing a run.
        """
        return self.root / "logs"

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


def _default_home() -> Path:
    """The home dir for the user config layer.

    A single, private seam so tests can pin it and stay hermetic; nothing reads
    global state until ``load_config`` actually runs.
    """
    return Path.home()


def load_config(
    workspace_root: Path | str,
    slug: str | None = None,
    *,
    home: Path | str | None = None,
) -> Config:
    """Resolve config as code defaults ← user ← workspace ← task.

    Layers, lowest precedence first: ``~/.backseat/config.toml``,
    ``<workspace>/.backseat/config.toml``, and
    ``<workspace>/.backseat/tasks/<slug>/config.toml`` when ``slug`` is given.
    Missing layers are skipped; arrays replace wholesale, while ``[models]``
    slot names merge per key (higher layer wins). ``home`` overrides the
    user-layer directory (defaults to the real home dir).
    """
    root = Path(workspace_root).resolve()
    cfg = Config(workspace_root=root)
    home_dir = Path(home) if home is not None else _default_home()
    layers = (
        home_dir / ".backseat" / "config.toml",
        cfg.config_path,
        cfg.task_paths(slug).config if slug is not None else None,
    )
    for path in layers:
        if path is not None and path.exists():
            data: dict[str, Any] = _tomllib().loads(path.read_text(encoding="utf-8"))
            cfg = _apply_overrides(cfg, data)
    # Warn once, here, on the value that actually resolved -- not per layer in
    # _apply_overrides, which would name a value a higher layer may immediately
    # override (user sets -1, task sets 5) and so report a cap that is not in
    # effect. A negative cap silently disables the PROBLEM re-send bound, which
    # is the same silent-no-op class §5b refuses for a bad slot: a typo'd guard
    # must not quietly un-guard. 0 is the documented way to disable it, and is
    # deliberate, so it stays quiet.
    if cfg.problem_repeat_cap < 0:
        print(
            f"[warn] config: [harness] problem_repeat_cap = "
            f"{cfg.problem_repeat_cap} is negative, so the PROBLEM re-send cap "
            "is disabled and max_iterations governs; use 0 to disable it "
            "deliberately",
            file=sys.stderr,
        )
    return cfg


def _merge_models(cfg: Config, data: dict[str, Any]) -> dict[str, str]:
    """Merge this layer's ``[models]`` slots into the inherited vocabulary.

    Slot names are shared, owner-configured vocabulary, so layers merge per key
    (higher layer wins) instead of replacing wholesale: a task layer rebinds
    one name and keeps the rest, exactly like the scalar fields above merge via
    their per-field ``.get`` fall-backs. Values are opaque model-id strings;
    any pair that is not ``non-empty-str: non-empty-str`` is dropped with a
    stderr warning (never silent), so a typo'd slot can never silently reroute
    a step or fall through to the wrong model. An empty value is dropped for
    the same reason, one step later: it would resolve, be advertised to the
    operator as an available slot, and then compose no ``--model`` at all
    (``""`` is falsy at the spawn site), so the step would silently run the
    CLI's own default under a name that promised otherwise. Dropping the pair
    also stops the prompt advertising a slot that cannot produce a flag.
    ``default`` is honored like any other key here — reservation is a
    resolution-time rule, not a parse-time one.
    """
    raw = data.get("models", {})
    merged = dict(cfg.models)
    if not isinstance(raw, dict):
        print(
            "[warn] config: [models] must be a table of slot = \"model-id\" "
            f"pairs; got {type(raw).__name__}, so this layer adds no slots",
            file=sys.stderr,
        )
        return merged
    for key, value in raw.items():
        if not isinstance(key, str) or not key or not isinstance(value, str) or not value:
            print(
                f"[warn] config: dropped [models] slot {key!r} = {value!r}: "
                "slot names and model ids must be non-empty strings",
                file=sys.stderr,
            )
            continue
        merged[key] = value
    return merged


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
        timeout_s=int(op_data.get("timeout_s", cfg.operator.timeout_s)),
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
        problem_repeat_cap=int(
            harness.get("problem_repeat_cap", cfg.problem_repeat_cap)
        ),
        operator_retry_budget=int(
            harness.get("operator_retry_budget", cfg.operator_retry_budget)
        ),
        models=_merge_models(cfg, data),
        operator=operator,
    )
