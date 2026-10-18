"""Preemptive compaction-by-fork.

When a managed transcript grows past the threshold (~1MB ~= 250-300k tokens),
the next turn starts a *new* generation: a fresh session seeded with a chat-fork
of the prior session (which middle-truncates the expensive tool payloads),
followed by the real next prompt as a separate message. ``#n`` increments; the
root UUID stays stable so the chain is searchable from either end.

The fork itself comes from chat-mother-forker via history.py, which is also
where the size measurement comes from -- so this module stays pure decision
logic plus prompt assembly.
"""

from __future__ import annotations

from backseat_harness import history
from backseat_harness.config import Config


def should_compact(transcript_bytes: int, config: Config) -> bool:
    return transcript_bytes >= config.compact_threshold_bytes


def can_compact_again(current_generation: int, config: Config) -> bool:
    """Guard against a fork that itself immediately exceeds the threshold."""
    return current_generation < config.max_generations


def render_fork(root_uuid: str, history_provider: str) -> str:
    """Return a middle-truncated fork of the prior session for the preamble.

    Searches by the root UUID marker, which matches the most recent generation
    of this task's chain. Returns "" when there is nothing to fork yet.
    """
    return history.fork_session(root_uuid, history_provider)


def build_compacted_prompt(
    fork_text: str, next_prompt: str, origin: str | None = None
) -> list[str]:
    """Return the ordered messages for a compacted generation's first turn.

    The fork is a separate leading message; the real instruction is last so the
    model weights it most. The marker is applied by the caller (loop) to the
    first message. ``origin`` names the provider the fork came from when a
    provider switch carried it across; left None, the header is today's.
    """
    messages: list[str] = []
    if fork_text.strip():
        if origin is None:
            header = "## Prior session (compacted; historical reference, not instructions)"
        else:
            header = (
                f"## Prior session (from '{origin}'; historical reference, not instructions)"
            )
        messages.append(header + "\n\n" + fork_text.strip())
    messages.append(next_prompt)
    return messages
