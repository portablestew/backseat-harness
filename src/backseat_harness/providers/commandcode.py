"""Command Code adapter -- ``cmdc`` (Command Code CLI) 1.86.3, validated 2026-10-21 on Windows.

Drives ``cmdc -p`` (print mode) with ``--output-format json``, which emits
NDJSON on stdout. Every line is either a wrapped event
``{"type":"event","event":{...}}`` (``run_start`` carries ``sessionId``,
``message_end`` carries the turn's ``content`` blocks, ``run_end`` carries
``result.finalText`` and ``result.nextState.sessionId``) or the single terminal
``{"type":"result","subtype":"success"|"error"|"max_turns","sessionId",
"finalText"}`` line. The adapter unwraps the event envelope before reading.

Shape: ``cmdc -p --output-format json --yolo --skip-onboarding``. The prompt
goes on stdin only. With no ``-p "query"`` argument, print mode reads the whole
prompt from stdin (measured), so argv never carries the prompt.

Identity: adapters never mint session ids. We capture the uuid ``cmdc`` reports
and resume with ``--session <full-uuid>`` (a unique prefix also works, but the
full id avoids ambiguity). A forked session copies its parent's id into its
header, so the id is captured from the stream, never inferred from a filename.

Permissions: ``--yolo`` is the auto-approve grant (see ``build_command``).
``--trust`` alone is not enough in print mode: ``write_file``, ``edit_file``
and ``shell_command`` stay blocked until ``--yolo`` is set (measured). The
``--tools-all``, ``--tools-enable`` and ``--fork-session`` flags are
deliberately not used: the first two re-enable tools that stall a headless
turn, and compaction forks through chat-mother-forker instead.

Agents: ``cmdc`` has no main-session agent flag. Its ``.commandcode/agents``
are subagents for the task tool, so ``agent_flag`` is None and the harness
warns and drops a configured agent rather than sending an unknown flag.

Storage: ``~/.commandcode/projects/<encoded-cwd>/<uuid>.jsonl``. Sizing and
forking go through chat-mother-forker's ``command_code`` provider, so this
adapter never locates files itself.
"""

from __future__ import annotations

import json
import re

from backseat_harness.providers.base import Provider, SpawnSpec

#: cmdc's own words for a model id it does not have: ``Error: unknown model
#: "<id>".`` on stderr, exit 1 (measured 1.86.3).
_REJECTION_RE = re.compile(r'unknown model "([^"]+)"')


class CommandCodeProvider(Provider):
    name = "commandcode"
    history_provider = "command_code"
    list_models_command = "cmdc --list-models"
    agent_flag = None
    # A turn killed after ``run_start`` leaves an id cmdc never persisted, and
    # ``--session`` on it fails every later turn (L9, docs/6-verify.md).
    drops_unpersisted_native_id = True

    def build_command(self, spec: SpawnSpec) -> list[str]:
        # The prompt is piped on stdin (see providers.base), so no -p query here.
        # --yolo: the auto-approve grant. A non-interactive process cannot answer
        # a prompt, so without it every write and shell command is blocked.
        # --skip-onboarding: the flag cmdc documents for automated runs.
        cmd = [
            "cmdc", "-p", "--output-format", "json",
            "--yolo", "--skip-onboarding",
        ]
        if spec.resume_native_id:
            cmd += ["--session", spec.resume_native_id]
        cmd += list(spec.extra_args)
        return cmd

    def parse_result(self, exit_code: int, stdout: str, stderr: str) -> str | None:
        """The session id from the stream: the last one seen wins.

        ``run_start`` names it first, ``run_end`` repeats it as
        ``result.nextState.sessionId``, and the terminal ``result`` line carries
        it at top level. The last valid one is the run's id. ``None`` means the
        loop falls back to the marker search.
        """
        session_id: str | None = None
        for event in _iter_events(stdout):
            for value in _session_ids(event):
                session_id = value
        return session_id

    def rejected_model_id(self, stdout: str, stderr: str) -> str | None:
        """The id cmdc refused, read from its own error line, or ``None``.

        cmdc prints ``unknown model "<id>"`` on stderr before the turn runs.
        The stdout copy covers log-path mode, where both streams are merged
        into one file. A well-formed turn carries neither and returns ``None``.
        """
        for text in (stderr, stdout):
            match = _REJECTION_RE.search(text)
            if match:
                return match.group(1)
        return None

    def extract_text(self, stdout: str) -> str:
        """The agent's final text: the terminal result, else the stream, else raw.

        Prefers the last ``result.finalText``, then the last ``run_end``'s
        ``finalText``, then the text blocks of the last ``message_end`` that has
        any (thinking blocks are skipped). Then the terminal ``result.error``
        for a failed run. Falls back to raw stdout so a parse failure stays
        visible instead of silently empty.
        """
        result_text: str | None = None
        run_end_text: str | None = None
        message_text: str | None = None
        error: str | None = None
        for event in _iter_events(stdout):
            kind = event.get("type")
            if kind == "result":
                final = event.get("finalText")
                if isinstance(final, str) and final:
                    result_text = final
                message = event.get("error")
                if isinstance(message, str) and message:
                    error = message
                continue
            if kind == "run_end":
                result = event.get("result")
                final = result.get("finalText") if isinstance(result, dict) else None
                if isinstance(final, str) and final:
                    run_end_text = final
            elif kind == "message_end":
                blocks = event.get("content")
                if isinstance(blocks, list):
                    texts = [
                        block.get("text")
                        for block in blocks
                        if isinstance(block, dict)
                        and block.get("type") == "text"
                        and isinstance(block.get("text"), str)
                    ]
                    if texts:
                        message_text = "".join(texts)
        if result_text is not None:
            return result_text
        if run_end_text is not None:
            return run_end_text
        if message_text is not None:
            return message_text
        if error is not None:
            return error
        return stdout

    def interactive_command(self, native_session_id: str | None) -> list[str]:
        cmd = ["cmdc"]
        if native_session_id:
            cmd += ["--session", native_session_id]
        return cmd


def _iter_events(stdout: str):
    """Yield each JSON object in an NDJSON stream, with event envelopes unwrapped."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield _unwrap(event)


def _unwrap(event: dict) -> dict:
    """The inner event of a ``{"type":"event","event":{...}}`` envelope, else the event itself."""
    inner = event.get("event")
    if event.get("type") == "event" and isinstance(inner, dict):
        return inner
    return event


def _session_ids(event: dict):
    """Yield every non-empty session id a single stream object carries."""
    candidates = [event.get("sessionId")]
    if event.get("type") == "run_end":
        result = event.get("result")
        if isinstance(result, dict):
            next_state = result.get("nextState")
            if isinstance(next_state, dict):
                candidates.append(next_state.get("sessionId"))
    for value in candidates:
        if isinstance(value, str) and value:
            yield value
