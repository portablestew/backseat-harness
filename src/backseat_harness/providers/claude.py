"""Claude Code adapter -- written, but NOT a supported provider in v0.1.

v0.1 ships opencode only (see providers/__init__.py). This adapter conforms to
the current Provider contract and is kept as the head start on v0.2, but it has
not been validated against a live ``claude`` install, so it is deliberately left
out of the registry rather than offered as if it worked.

Shape: headless ``-p`` with newline-delimited ``stream-json`` so the terminal
``result`` object (carrying ``session_id``) is readable. The prompt arrives on
stdin, not as a ``-p`` argument, per the Provider contract. We never mint
``--session-id``; identity is the ``<meta>`` marker and we capture whatever
native id Claude reports.

Claude Code stores transcripts as JSONL under ``~/.claude/projects/``; sizing
goes through chat-mother-forker's ``claude_code`` provider, so this adapter does
not locate files itself.
"""

from __future__ import annotations

import json
from pathlib import Path

from backseat_harness.providers.base import OperatorLaunch, Provider, ProviderError, SpawnSpec


class ClaudeProvider(Provider):
    name = "claude"
    history_provider = "claude_code"

    def build_command(self, spec: SpawnSpec) -> list[str]:
        # Bare -p: the prompt is piped on stdin (see providers.base).
        cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
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
            if event.get("type") == "result" and isinstance(event.get("result"), str):
                return event["result"]
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

    def prepare_operator(
        self, profile_dir: Path, persona: str, verify_commands: list[str]
    ) -> OperatorLaunch:
        """Not yet defined: a read-only Claude Code operator profile is still to
        be designed (see the v0.2 work). Fail loudly rather than run a
        non-read-only operator.
        """
        raise ProviderError(
            "claude does not yet define a read-only operator profile; "
            "not registered until it does"
        )

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
