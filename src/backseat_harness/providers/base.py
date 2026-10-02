"""The Provider abstraction every managed-agent CLI adapter implements.

Two contracts worth stating up front, because they shape every adapter:

**The prompt goes on stdin, never in argv.** A compacted first turn carries a
forked transcript preamble that can run to hundreds of kilobytes, which blows
past the command-line length limit on Windows (~32k). Every supported CLI can
read its prompt from stdin, so ``build_command`` returns argv *without* the
prompt and ``spawn.run_turn`` pipes it in.

**Size lives in history, not on disk.** Adapters deliberately do not locate
transcript files: opencode stores sessions in SQLite (no file exists to
stat), and claude and kiro keep JSONL files that adapters still never
touch. ``history_provider`` names the chat-mother-forker provider that can
size and fork each CLI's sessions instead.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


class ProviderError(RuntimeError):
    """Raised for provider misuse or unsupported operations."""


@dataclass(frozen=True)
class SpawnSpec:
    """Everything needed to launch one managed-agent turn.

    ``resume_native_id`` is None for the first turn of a generation (a fresh
    session seeded by the marker) and set for subsequent same-generation turns.
    """

    prompt: str
    cwd: Path
    resume_native_id: str | None = None
    timeout_s: int = 3600
    # Extra provider-specific flags the workspace owner may inject.
    extra_args: tuple[str, ...] = ()
    # Where this turn's output goes as it is produced (see spawn.run_turn and
    # logs.py). None captures the streams in memory instead, which is what the
    # tests and any caller without a task lane do.
    log_path: Path | None = None


@dataclass(frozen=True)
class TurnResult:
    """Outcome of running the CLI process for one turn."""

    exit_code: int
    timed_out: bool
    duration_s: float
    native_session_id: str | None
    stdout: str = ""
    stderr: str = ""


class Provider(ABC):
    """Adapter contract. Adapters never mint session ids; they capture them.

    Every method is abstract: a provider must implement the full surface --
    one-shot spawning, id parsing, text extraction, and interactive takeover --
    before it can be instantiated and registered.

    There is no operator-profile method: the operator runs the CLI's own default
    agent (or ``[operator].agent``), with the persona prepended to its prompt and
    permissions left to the workspace owner. Because that makes ``build_command``
    shared by the managed turn and the operator turn alike, it must carry no
    permission or trust flags -- a workspace grants those through
    ``[harness] extra_args`` (managed) or ``[operator].extra_args`` (operator).
    """

    #: Short name used in config (e.g. "opencode").
    name: str

    #: Name of the chat-mother-forker provider backing this CLI's stored
    #: sessions. Used to scope size lookups and fork rendering (see history.py).
    history_provider: str

    #: Command that lists the model ids this CLI accepts, when the adapter
    #: knows one. Quoted by the loop inside the complaint it raises after a
    #: rejected id, so "which ids are valid?" has an answer in the same breath
    #: as the failure rather than a separate lookup the operator has to think
    #: up. None means no known command, and no hint is printed.
    list_models_command: str | None = None

    @abstractmethod
    def build_command(self, spec: SpawnSpec) -> list[str]:
        """Return the argv for a non-interactive one-shot turn.

        Must NOT include the prompt: it is piped on stdin by the caller.
        """

    @abstractmethod
    def parse_result(self, exit_code: int, stdout: str, stderr: str) -> str | None:
        """Extract the native session id from process output, if present.

        Best-effort. When it returns None the loop falls back to a marker
        search through history, which is the authoritative path anyway.
        """

    def rejected_model_id(self, stdout: str, stderr: str) -> str | None:
        """The model id the CLI refused before the turn could run, or ``None``.

        Optional observability, not part of the abstract contract: a CLI that
        says "I do not have that model" hands the id back so the loop can name
        the binding that asked for it, and a CLI with no such signal inherits
        ``None`` and the loop stays quiet. Implementations must key on the
        provider's own words for the refusal -- never on the exit code alone,
        which an ordinary failed turn shares.
        """
        return None

    @abstractmethod
    def extract_text(self, stdout: str) -> str:
        """Return the agent's final assistant text from process output.

        Used for the operator, whose verdict JSON has to be parsed out of
        its reply. Adapters that emit a structured event stream should
        reassemble the text parts rather than hand back raw events.
        """

    @abstractmethod
    def interactive_command(self, native_session_id: str | None) -> list[str]:
        """Return argv that opens the managed session in the CLI's interactive UI.

        Used for human takeover: the harness runs this attached to the real
        terminal and waits for the human to quit. ``native_session_id`` is None
        when no session exists yet (e.g. paused during planning); the CLI should
        then open a fresh interactive session in the workspace.
        """
