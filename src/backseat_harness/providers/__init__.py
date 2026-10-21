"""Provider adapters for the managed-agent CLIs.

Each adapter knows how to spawn a one-shot turn, resume by native id, pull the
native session id out of the CLI's output, and recover the agent's final text.
It does *not* know where transcripts live: sizing and forking go through
chat-mother-forker, scoped by the adapter's ``history_provider`` (see
history.py).

Supported providers: **opencode**, **claude** (Claude Code), **kiro**
(kiro-cli), and **commandcode** (Command Code ``cmdc``). All four are
registered. opencode, claude and kiro were validated live end to end: one-shot
and resume on a real install. commandcode was validated live for one-shot,
resume and the dead-session recovery only; takeover, fork and ``--model`` were
not run live. Every turn runs auto-approved (a non-interactive process cannot answer
a prompt), and the operator runs each CLI's default agent with the persona
prepended to its prompt. The harness writes no operator profile.
"""

from __future__ import annotations

import shutil

from backseat_harness.providers.base import (
    Provider,
    ProviderError,
    SpawnSpec,
    TurnResult,
    agent_args,
)
from backseat_harness.providers.claude import ClaudeProvider
from backseat_harness.providers.commandcode import CommandCodeProvider
from backseat_harness.providers.kiro import KiroProvider
from backseat_harness.providers.opencode import OpencodeProvider

# Registry keyed by the short name used in config (harness.provider).
_REGISTRY: dict[str, type[Provider]] = {
    "claude": ClaudeProvider,
    "commandcode": CommandCodeProvider,
    "kiro": KiroProvider,
    "opencode": OpencodeProvider,
}

# Executable each registered provider launches (its interactive argv[0]). The
# one place PATH checks read from: the fork picker and the workspace "New chat"
# buttons both go through on_path_providers().
_BINARY: dict[str, str] = {
    "claude": "claude",
    "commandcode": "cmdc",
    "kiro": "kiro-cli",
    "opencode": "opencode",
}


def get_provider(name: str) -> Provider:
    try:
        cls = _REGISTRY[name]
    except KeyError as exc:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise ProviderError(
            f"unknown provider {name!r}; available in this version: {known}"
        ) from exc
    return cls()


def available_providers() -> list[str]:
    return sorted(_REGISTRY)


def binary_for(name: str) -> str:
    try:
        return _BINARY[name]
    except KeyError as exc:
        raise ProviderError(f"no CLI binary known for provider {name!r}") from exc


def on_path_providers() -> list[str]:
    """Registered providers whose CLI resolves on PATH, in ``available_providers`` order."""
    return [name for name in available_providers() if shutil.which(binary_for(name))]


__all__ = [
    "Provider",
    "ProviderError",
    "SpawnSpec",
    "TurnResult",
    "agent_args",
    "ClaudeProvider",
    "CommandCodeProvider",
    "KiroProvider",
    "OpencodeProvider",
    "get_provider",
    "available_providers",
    "binary_for",
    "on_path_providers",
]
