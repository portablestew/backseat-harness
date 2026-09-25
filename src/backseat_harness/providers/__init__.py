"""Provider adapters for the managed-agent CLIs.

Each adapter knows how to spawn a one-shot turn, resume by native id, pull the
native session id out of the CLI's output, and recover the agent's final text.
It does *not* know where transcripts live: sizing and forking go through
chat-mother-forker, scoped by the adapter's ``history_provider`` (see
history.py).

v0.1 supports **opencode** only. The Claude Code adapter exists
(providers/claude.py) and conforms to the contract, but is unvalidated against a
live install, so it is not registered -- asking for it fails loudly instead of
half-working. Registering it is the bulk of v0.2, alongside kiro-cli.
"""

from __future__ import annotations

from backseat_harness.providers.base import Provider, ProviderError, SpawnSpec, TurnResult
from backseat_harness.providers.opencode import OpencodeProvider

# Registry keyed by the short name used in config (harness.provider).
_REGISTRY: dict[str, type[Provider]] = {
    "opencode": OpencodeProvider,
}

# Adapters that exist but are not yet validated end-to-end. Named here so the
# error message can say "coming, not missing" instead of just "unknown".
_DEFERRED: dict[str, str] = {
    "claude": "v0.2",
    "kiro": "v0.2",
}


def get_provider(name: str) -> Provider:
    try:
        cls = _REGISTRY[name]
    except KeyError as exc:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        detail = f"unknown provider {name!r}; available in this version: {known}"
        if name in _DEFERRED:
            detail = (
                f"provider {name!r} is not supported yet (planned for "
                f"{_DEFERRED[name]}); available in this version: {known}"
            )
        raise ProviderError(detail) from exc
    return cls()


def available_providers() -> list[str]:
    return sorted(_REGISTRY)


__all__ = [
    "Provider",
    "ProviderError",
    "SpawnSpec",
    "TurnResult",
    "OpencodeProvider",
    "get_provider",
    "available_providers",
]
