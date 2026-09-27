"""opencode adapter (the v0.1 target provider).

Drives ``opencode run`` in non-interactive mode with ``--format json``, which
emits one JSON event per line on stdout. Two things we need are in there:

- ``sessionID`` (``ses_...``) on every event, so a turn's native session id is
  captured rather than minted. Identity is still marker-first; this is just the
  fast path, with a marker search through history as the fallback.
- ``text`` events whose ``part.text`` carries assistant output, which is how the
  operator's verdict JSON gets recovered without parsing TUI decoration.

Resuming is ``--session <id>`` (opencode has no ``--resume``). ``--fork`` is
deliberately unused: the harness does its own compaction-by-fork through
chat-mother-forker, seeding a *new* session so the ``#n`` generation chain stays
explicit and searchable.

Permissions: the managed agent runs as opencode's default agent with whatever
flags the workspace owner supplies via config (``--auto`` is never assumed). The
operator runs as a harness-written ``backseat-operator`` agent that denies
everything except reads, search, and read-only git (see ``prepare_operator``).

Sessions live in SQLite (``opencode.db``), so there is no transcript file to
stat; sizing goes through chat-mother-forker's ``opencode`` provider.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from backseat_harness.providers.base import OperatorLaunch, Provider, SpawnSpec

# opencode session ids are prefixed, which makes a last-ditch text scan safe.
_SESSION_RE = re.compile(r"\bses_[A-Za-z0-9]+\b")

#: Name of the harness-managed operator agent (the markdown file's stem).
OPERATOR_AGENT = "backseat-operator"

# Read-only git inspection the operator may run itself. Everything else is the
# managed agent's job. ``--output`` is denied after the allows (last match wins)
# because ``git diff --output=<file>`` writes to disk.
_OPERATOR_GIT = ("git status*", "git diff*", "git log*", "git show*")


def operator_permissions(verify_commands: list[str]) -> dict:
    """Permission map for the operator: deny by default, allow read-only tools.

    Nothing is left at ``ask``: a non-interactive run has nobody to answer, so an
    ``ask`` would stall the operator until the turn timeout.
    """
    bash: dict[str, str] = {"*": "deny"}
    bash.update({pattern: "allow" for pattern in _OPERATOR_GIT})
    bash.update({cmd: "allow" for cmd in verify_commands})
    bash["*--output*"] = "deny"
    return {
        "*": "deny",
        "read": {
            "*": "allow",
            "*.env": "deny",
            "*.env.*": "deny",
            "*.env.example": "allow",
            # Holds the task's root UUID. If the operator read it, its own
            # stored session would match the marker search (see marker.redact).
            "*binding.json": "deny",
        },
        "grep": "allow",
        "glob": "allow",
        "list": "allow",
        "bash": bash,
        "external_directory": "deny",
        "doom_loop": "deny",
        "question": "deny",
    }


def render_operator_agent(persona: str, verify_commands: list[str]) -> str:
    """Render the operator as an opencode markdown agent.

    Frontmatter values are JSON, which is valid YAML flow syntax, so no YAML
    library is needed and quoting is always correct.
    """
    front = {
        "description": "backseat-harness operator: read-only user of the managed agent",
        "mode": "primary",
        "permission": operator_permissions(verify_commands),
    }
    lines = ["---"]
    lines += [f"{key}: {json.dumps(value)}" for key, value in front.items()]
    lines += ["---", "", persona.strip(), ""]
    return "\n".join(lines)


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

    def prepare_operator(
        self, profile_dir: Path, persona: str, verify_commands: list[str]
    ) -> OperatorLaunch:
        """Write ``<profile_dir>/agents/backseat-operator.md`` and point opencode at it.

        ``OPENCODE_CONFIG_DIR`` is an extra config directory that opencode loads
        after the global config and project ``.opencode`` dirs, so this adds the
        agent without touching the user's own opencode config. It is set on the
        operator process only; the managed agent never sees this profile.
        """
        agents = profile_dir / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / f"{OPERATOR_AGENT}.md").write_text(
            render_operator_agent(persona, verify_commands), encoding="utf-8"
        )
        return OperatorLaunch(
            env={"OPENCODE_CONFIG_DIR": str(profile_dir)},
            extra_args=("--agent", OPERATOR_AGENT),
        )

    def interactive_command(self, native_session_id: str | None) -> list[str]:
        """Open the managed session in opencode's TUI.

        The bare ``opencode`` command starts the TUI; ``--session <id>`` drops
        the human straight into the managed session so they can advance it, then
        quit to hand control back. With no id yet, a fresh TUI session opens.
        """
        cmd = ["opencode"]
        if native_session_id:
            cmd += ["--session", native_session_id]
        return cmd


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
