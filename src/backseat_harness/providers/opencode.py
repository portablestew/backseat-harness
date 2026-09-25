"""opencode adapter (the v0.1 target provider).

Drives ``opencode run`` in non-interactive mode with ``--format json``, which
emits one JSON event per line on stdout. Two things we need are in there:

- ``sessionID`` (``ses_...``) on every event, so a turn's native session id is
  captured rather than minted. Identity is still marker-first; this is just the
  fast path, with a marker search through history as the fallback.
- ``text`` events whose ``part.text`` carries assistant output, which is how the
  system agent's verdict JSON gets recovered without parsing TUI decoration.

Resuming is ``--session <id>`` (opencode has no ``--resume``). ``--fork`` is
deliberately unused: the harness does its own compaction-by-fork through
chat-mother-forker, seeding a *new* session so the ``#n`` generation chain stays
explicit and searchable.

Permissions: opencode blocks on approval prompts by default, and ``--auto``
(auto-approve anything not explicitly denied) is flagged dangerous by opencode
itself. The harness will not pick that for you -- the workspace owner supplies
permission flags via config, and the turn timeout is the backstop.

Sessions live in SQLite (``opencode.db``), so there is no transcript file to
stat; sizing goes through chat-mother-forker's ``opencode`` provider.
"""

from __future__ import annotations

import json
import re

from backseat_harness.providers.base import Provider, SpawnSpec

# opencode session ids are prefixed, which makes a last-ditch text scan safe.
_SESSION_RE = re.compile(r"\bses_[A-Za-z0-9]+\b")


class OpencodeProvider(Provider):
    name = "opencode"
    history_provider = "opencode"

    def build_command(self, spec: SpawnSpec) -> list[str]:
        # The prompt is piped on stdin (see providers.base), so no positional
        # message here: a compacted first turn is far too large for argv.
        cmd = ["opencode", "run", "--format", "json"]
        if spec.resume_native_id:
            cmd += ["--session", spec.resume_native_id]
        cmd += list(spec.extra_args)
        return cmd

    def parse_result(self, exit_code: int, stdout: str, stderr: str) -> str | None:
        """Return the session id from the event stream, or None.

        Every event carries ``sessionID``; we take the last one seen. Falls back
        to a ``ses_`` scan so a partial or decorated stream (e.g. a turn killed
        mid-write) still yields an id when one was printed at all.
        """
        session_id: str | None = None
        for event in _iter_events(stdout):
            value = event.get("sessionID")
            if isinstance(value, str) and value:
                session_id = value
        if session_id:
            return session_id
        match = _SESSION_RE.search(stdout)
        return match.group(0) if match else None

    def extract_text(self, stdout: str) -> str:
        """Reassemble assistant text from the event stream.

        Text parts are keyed by part id and may be re-emitted as they stream, so
        the last value per part wins, joined in first-seen order. Reasoning
        events are skipped: the verdict is in the reply, not the thinking.
        """
        parts: dict[str, str] = {}
        order: list[str] = []
        for index, event in enumerate(_iter_events(stdout)):
            if event.get("type") != "text":
                continue
            part = event.get("part")
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue
            key = part.get("id") or f"_{index}"
            if key not in parts:
                order.append(key)
            parts[key] = text
        if not parts:
            # No parseable events: hand back the raw output rather than nothing,
            # so verdict parsing still has a chance (and errors stay visible).
            return stdout
        return "\n".join(parts[k] for k in order)


def _iter_events(stdout: str):
    """Yield each JSON object in a newline-delimited event stream.

    Tolerant by design: non-JSON lines (log noise, a truncated final line) are
    skipped rather than failing the whole parse.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event
