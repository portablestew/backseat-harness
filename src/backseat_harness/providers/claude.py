"""Claude Code adapter -- registered and live-validated (``claude`` 2.1.283).

This is one of the providers registered in ``providers/__init__.py``. Its
one-shot path and same-id resume were validated live against ``claude``
2.1.283: the prompt arrives on stdin, ``session_id`` is captured from the
stream, and ``--resume`` appends to the same project JSONL.

Shape: headless ``-p`` with newline-delimited ``stream-json`` so the terminal
``result`` object (carrying ``session_id``) is readable. The prompt arrives on
stdin, not as a ``-p`` argument, per the Provider contract. We never mint
``--session-id``; identity is the ``<meta>`` marker and we capture whatever
native id Claude reports.

Claude Code stores transcripts as JSONL under ``~/.claude/projects/``; sizing
goes through chat-mother-forker's ``claude_code`` provider, so this adapter does
not locate files itself.

Permission mode: ``auto`` (see ``build_command``), because a non-interactive
process cannot answer a prompt and would otherwise block until the watchdog.
Restrict the tool set with ``--allowedTools``/``--disallowedTools``,
``--setting-sources``, or a repo-defined agent.
"""

from __future__ import annotations

import json

from backseat_harness.providers.base import Provider, SpawnSpec


class ClaudeProvider(Provider):
    name = "claude"
    history_provider = "claude_code"

    def build_command(self, spec: SpawnSpec) -> list[str]:
        # Bare -p: the prompt is piped on stdin (see providers.base).
        # --permission-mode auto: this process cannot answer a prompt, so approve
        # rather than block until the watchdog. Narrow tools with
        # --allowedTools/--disallowedTools or a repo-defined agent.
        cmd = [
            "claude", "-p", "--output-format", "stream-json", "--verbose",
            "--permission-mode", "auto",
        ]
        if spec.resume_native_id:
            cmd += ["--resume", spec.resume_native_id]
        cmd += list(spec.extra_args)
        return cmd

    def parse_result(self, exit_code: int, stdout: str, stderr: str) -> str | None:
        """Scan stream-json lines for the terminal result's ``session_id``.

        Robust to interleaved non-JSON noise: each line is parsed independently
        and the last ``session_id`` seen wins.
        """
        session_id: str | None = None
        for event in _iter_events(stdout):
            value = event.get("session_id")
            if isinstance(value, str) and value:
                session_id = value
        return session_id

    def extract_text(self, stdout: str) -> str:
        """Reassemble assistant text from the stream-json events.

        Prefers the terminal ``result`` object's ``result`` field; otherwise
        concatenates assistant text blocks. Falls back to raw stdout so parse
        failures stay visible instead of silently empty.
        """
        chunks: list[str] = []
        for event in _iter_events(stdout):
            value = event.get("result")
            # An empty ``result`` is not terminal: fall through to the
            # assistant blocks below, like kiro does with ``finalText``.
            if event.get("type") == "result" and isinstance(value, str) and value:
                return value
            message = event.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content")
            if isinstance(content, str):
                chunks.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str):
                            chunks.append(text)
        return "\n".join(chunks) if chunks else stdout

    def interactive_command(self, native_session_id: str | None) -> list[str]:
        cmd = ["claude"]
        if native_session_id:
            cmd += ["--resume", native_session_id]
        return cmd


def _iter_events(stdout: str):
    """Yield each JSON object in a newline-delimited stream, skipping noise."""
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
