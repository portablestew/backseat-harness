"""The ``<meta>`` session marker: portable, provider-independent identity.

Every generation's first user message begins with a marker so the session chain
is reconstructible by search even if the binding cache is lost. A stable root
UUID identifies the whole task chain; ``#n`` is the generation ordinal that
increments on compaction.
"""

from __future__ import annotations

import re
import uuid

_MARKER_RE = re.compile(r"<meta>START SESSION\s+([0-9a-fA-F-]{36})\s+#(\d+)</meta>")


def new_root_uuid() -> str:
    return str(uuid.uuid4())


def format_marker(root_uuid: str, generation: int) -> str:
    return f"<meta>START SESSION {root_uuid} #{generation}</meta>"


def prepend_marker(root_uuid: str, generation: int, prompt: str) -> str:
    """Return ``prompt`` with the marker as its first line."""
    return f"{format_marker(root_uuid, generation)}\n\n{prompt}"


def parse_marker(text: str) -> tuple[str, int] | None:
    """Extract ``(root_uuid, generation)`` from text, or None if absent."""
    m = _MARKER_RE.search(text)
    if not m:
        return None
    return m.group(1), int(m.group(2))
