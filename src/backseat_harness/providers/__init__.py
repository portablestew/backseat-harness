"""Provider adapters for the managed-agent CLIs.

Each adapter knows how to spawn a one-shot turn, resume by native id, pull the
native session id out of the CLI's output, and recover the agent's final text.
It does *not* know where transcripts live: sizing and forking go through
chat-mother-forker, scoped by the adapter's ``history_provider`` (see
history.py).

Supported providers: **opencode**, **claude** (Claude Code), and **kiro**
(kiro-cli). All three are registered and were validated live end to end:
one-shot and resume on a real install. Every turn runs auto-approved (a
non-interactive process cannot answer a prompt), and the operator runs each
CLI's default agent with the persona prepended to its prompt. The harness writes
no operator profile.
"""

from __future__ import annotations

from backseat_harness.providers.base import Provider, ProviderError, SpawnSpec, TurnResult
from backseat_harness.providers.claude import ClaudeProvider
from backseat_harness.providers.kiro import KiroProvider
from backseat_harness.providers.opencode import OpencodeProvider

# Registry keyed by the short name used in config (harness.provider).
_REGISTRY: dict[str, type[Provider]] = {
    "claude": ClaudeProvider,
    "kiro": KiroProvider,
    "opencode": OpencodeProvider,
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


__all__ = [
    "Provider",
    "ProviderError",
    "SpawnSpec",
    "TurnResult",
    "ClaudeProvider",
    "KiroProvider",
    "OpencodeProvider",
    "get_provider",
    "available_providers",
]
