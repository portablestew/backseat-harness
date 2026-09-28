"""Claude Code adapter -- registered and live-validated (``claude`` 2.1.283).

This is one of the providers registered in ``providers/__init__.py``. Its
one-shot path, same-id resume, and read-only operator recipe were all
validated live against ``claude`` 2.1.283: the prompt arrives on stdin,
``session_id`` is captured from the stream, ``--resume`` appends to the same
project JSONL, and under the recipe a file write is auto-denied while a file
read and ``git status --porcelain`` run unattended.

Shape: headless ``-p`` with newline-delimited ``stream-json`` so the terminal
``result`` object (carrying ``session_id``) is readable. The prompt arrives on
stdin, not as a ``-p`` argument, per the Provider contract. We never mint
``--session-id``; identity is the ``<meta>`` marker and we capture whatever
native id Claude reports.

Claude Code stores transcripts as JSONL under ``~/.claude/projects/``; sizing
goes through chat-mother-forker's ``claude_code`` provider, so this adapter does
not locate files itself.

Residuals of the read-only recipe, documented by decision:

- R-C1 -- ``--setting-sources project,local`` isolates the *user* scope only;
  workspace project/local settings still load. That is by design (the workspace
  owner owns them) but must be stated wherever the recipe is handed out.
- R-C2 -- ``build_command`` passes no permission flags, so an agent turn that
  needs approval blocks until the spawn watchdog fires. Grants are structurally
  the workspace owner's channel (``[harness] extra_args``); operator turns never
  receive them (cli.py composes only launch + operator extra_args).
"""

from __future__ import annotations

import json
from pathlib import Path

from backseat_harness.providers.base import OperatorLaunch, Provider, SpawnSpec


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

    def prepare_operator(
        self, profile_dir: Path, persona: str, verify_commands: list[str]
    ) -> OperatorLaunch:
        """Read-only operator expressed purely as CLI flags -- no files.

        Three stacked cuts make it read-only: mutating tools are removed
        outright (``--disallowedTools Write Edit NotebookEdit PowerShell``),
        every remaining approval prompt auto-denies (``--permission-prompts
        none`` -- nothing in the recipe can prompt), and ambient user-scope
        settings/MCP/allow rules are excluded (``--setting-sources
        project,local``; user scope only, see the module docstring R-C1).
        Reads, Glob/Grep, and read-only git run unattended and never prompt.

        ``profile_dir`` and ``persona`` are unused by design: claude needs no
        settings file there (``env={}`` -- read-only git needs no allow rule).
        ``verify_commands`` is accepted but never passed through: by convention
        they are read-only git commands, which need no allow rule either.
        """
        return OperatorLaunch(
            env={},
            extra_args=(
                "--permission-prompts", "none",
                "--disallowedTools", "Write", "Edit", "NotebookEdit", "PowerShell",
                "--setting-sources", "project,local",
            ),
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
