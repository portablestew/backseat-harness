"""The ``<meta>`` session markers: portable, provider-independent identity.

Every managed generation's first user message begins with a marker so the
session chain is reconstructible by search even if the binding cache is lost. A
stable root UUID identifies the whole task chain; ``#n`` is the generation
ordinal that increments on compaction.

Operator sessions carry their own, UUID-free tag: ``<meta>OPERATOR SESSION
<slug> <phase></meta>``. It marks a stored session as an operator's so session
search can filter them out, without ever matching the task's root-UUID marker
search (see ``redact`` for why that matters).
"""

from __future__ import annotations

import re
import uuid

_MARKER_RE = re.compile(r"<meta>START SESSION\s+([0-9a-fA-F-]{36})\s+#(\d+)</meta>")

# Operator session tag: the constant prefix is the identity signal; the slug and
# phase that may follow are human context only. Never contains a root UUID.
#
# The token requirement is what keeps the tag from matching *prose about* the
# tag. ``format_operator_marker`` only ever emits real tokens -- a sanitized
# task slug (``[a-z0-9_-]``, see config.sanitize_slug) or a phase
# (``plan``/``review``/``manager-<phase>``) -- so a tag with no token at all
# (the bare formatter shape) or with a non-token such as the ``...`` elision the
# docs use when quoting this syntax is a description of a marker, not a marker.
_OPERATOR_TOKEN = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
OPERATOR_SESSION_RE = re.compile(
    rf"<meta>OPERATOR SESSION(?:[ \t]+{_OPERATOR_TOKEN}){{1,2}}[ \t]*</meta>"
)

# First token after the tag: the task slug for a task-scoped operator session.
_OPERATOR_SLUG_RE = re.compile(rf"<meta>OPERATOR SESSION[ \t]+({_OPERATOR_TOKEN})")


def parse_operator_marker(text: str) -> str | None:
    """Extract the task slug from an operator session marker, or None.

    The tag reads ``<meta>OPERATOR SESSION <slug> <phase></meta>``. A task-less
    operator session (slug empty) leaves the phase token in the slug position;
    callers disambiguate that against the filesystem, since a phase name is
    never a valid lane slug in practice.

    Only a real token parses: the tag must carry at least one slug/phase token,
    so a quoted or elided form such as ``<meta>OPERATOR SESSION ...</meta>``
    returns None rather than the ``...``. See ``OPERATOR_SESSION_RE``.
    """
    m = _OPERATOR_SLUG_RE.search(text)
    return m.group(1) if m else None


def new_root_uuid() -> str:
    return str(uuid.uuid4())


def format_marker(root_uuid: str, generation: int) -> str:
    return f"<meta>START SESSION {root_uuid} #{generation}</meta>"


def prepend_marker(root_uuid: str, generation: int, prompt: str) -> str:
    """Return ``prompt`` with the marker as its first line."""
    return f"{format_marker(root_uuid, generation)}\n\n{prompt}"


def format_operator_marker(slug: str = "", phase: str = "") -> str:
    """Return the operator session tag: ``<meta>OPERATOR SESSION [slug] [phase]</meta>``.

    UUID-free by contract: the operator's own stored session must never match
    the task's root-UUID marker search, while still being recognizable as an
    operator's for session filtering.
    """
    parts = ["<meta>OPERATOR SESSION"]
    if slug:
        parts.append(slug)
    if phase:
        parts.append(phase)
    return " ".join(parts) + "</meta>"


REDACTED_ROOT = "<task-root>"


def redact(text: str, root_uuid: str) -> str:
    """Remove the task's root UUID from ``text``.

    Used on the managed-session fork before it goes into an operator prompt.
    The operator's own session is stored by the same CLI, so an unredacted fork
    would make every review a second session matching the marker search -- and
    the newest match would win, pointing sizing and compaction at the operator
    instead of the managed agent. The generation ordinal stays visible.
    """
    return text.replace(root_uuid, REDACTED_ROOT)


def redact_all(text: str) -> str:
    """Remove every task root UUID that a ``START SESSION`` marker in ``text`` names.

    For a source transcript that is itself a backseat session. Every marker's
    UUID is replaced, not only the first, so a second chain's marker cannot
    survive into a prompt and match that task's session search. A UUID that
    appears bare, outside any marker, is replaced too: it is the same identity.
    """
    for root_uuid in {m.group(1) for m in _MARKER_RE.finditer(text)}:
        text = text.replace(root_uuid, REDACTED_ROOT)
    return text


def parse_marker(text: str) -> tuple[str, int] | None:
    """Extract ``(root_uuid, generation)`` from text, or None if absent."""
    m = _MARKER_RE.search(text)
    if not m:
        return None
    return m.group(1), int(m.group(2))
