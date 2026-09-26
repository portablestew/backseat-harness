"""Full-session transcript capture: a reviewable artifact of the managed work.

After each managed turn the harness overwrites ``.backseat/tasks/<slug>/transcript.md``
so the operator (and a human) can inspect what the agent actually did --
including full, untruncated tool-call results, which the middle-truncated
compaction fork deliberately drops.

The transcript comes from chat-mother-forker's ``load_chat`` (via history.py):
the *complete* session for this task's marker, already annotated with turn
headers and ``TOOL_CALL``/``TOOL_RESULT`` labels. It is a single file rewritten
each turn; because compaction forks each generation into a fresh session, the
loaded session naturally tracks the current generation's work.

The captured text is run through ``marker.redact`` before it is written, for the
same reason the operator fork is: if the operator reads and quotes the file, an
unredacted root UUID would make the operator's own stored session match the
task's marker search -- and the newest match wins, pointing sizing and
compaction at the operator instead of the managed agent. The generation ordinal
stays visible.
"""

from __future__ import annotations

from backseat_harness import history
from backseat_harness import marker as marker_mod


def capture(search: str, history_provider: str, root_uuid: str) -> str | None:
    """Load the managed session's full transcript, redacted and ready to write.

    Returns the string to write, or ``None`` when there is nothing to capture
    yet (no matching session, or an empty one). Callers treat ``None`` as "skip
    writing a file this turn".
    """
    transcript = history.load_full_transcript(search, history_provider)
    if not transcript.strip():
        return None
    return marker_mod.redact(transcript, root_uuid)
