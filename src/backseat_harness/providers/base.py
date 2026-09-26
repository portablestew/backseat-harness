"""The Provider abstraction every managed-agent CLI adapter implements.

Two contracts worth stating up front, because they shape every adapter:

**The prompt goes on stdin, never in argv.** A compacted first turn carries a
forked transcript preamble that can run to hundreds of kilobytes, which blows
past the command-line length limit on Windows (~32k). Every supported CLI can
read its prompt from stdin, so ``build_command`` returns argv *without* the
prompt and ``spawn.run_turn`` pipes it in.

**Size lives in history, not on disk.** Adapters deliberately do not locate
transcript files. The v0.1 provider (opencode) stores sessions in SQLite, so
there is no file to stat; ``history_provider`` names the chat-mother-forker
provider that can size and fork this CLI's sessions instead.
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


@dataclass(frozen=True)
class TurnResult:
    """Outcome of running the CLI process for one turn."""

    exit_code: int
    timed_out: bool
    duration_s: float
    native_session_id: str | None
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class OperatorLaunch:
    """How to launch the system agent under a harness-managed operator profile.

    ``env`` is merged over the parent environment for the operator process only;
    ``extra_args`` are appended to its command line. Both empty means the
    provider has no profile support and the operator runs with CLI defaults.
    """

    env: dict[str, str]
    extra_args: tuple[str, ...]


class Provider(ABC):
    """Adapter contract. Adapters never mint session ids; they capture them."""

    #: Short name used in config (e.g. "opencode").
    name: str

    #: Name of the chat-mother-forker provider backing this CLI's stored
    #: sessions. Used to scope size lookups and fork rendering (see history.py).
    history_provider: str

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

    @abstractmethod
    def extract_text(self, stdout: str) -> str:
        """Return the agent's final assistant text from process output.

        Used for the system agent, whose verdict JSON has to be parsed out of
        its reply. Adapters that emit a structured event stream should
        reassemble the text parts rather than hand back raw events.
        """

    def prepare_operator(
        self, profile_dir: Path, persona: str, verify_commands: list[str]
    ) -> OperatorLaunch:
        """Write (or overwrite) a read-only operator profile and say how to use it.

        ``profile_dir`` is harness-owned scratch space under ``.backseat/``.
        The default is a no-op: a provider without profile support runs the
        operator with CLI defaults, which are NOT read-only.
        """
        return OperatorLaunch(env={}, extra_args=())

    def interactive_command(self, native_session_id: str | None) -> list[str]:
        """Return argv that opens the managed session in the CLI's interactive UI.

        Used for human takeover: the harness runs this attached to the real
        terminal and waits for the human to quit. ``native_session_id`` is None
        when no session exists yet (e.g. paused during planning); the CLI should
        then open a fresh interactive session in the workspace.
        """
        raise ProviderError(f"{self.name} does not support interactive takeover")
