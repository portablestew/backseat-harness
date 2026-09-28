"""Kiro Code adapter -- registered and live-validated (``kiro-cli-chat`` 2.24.1).

This is one of the providers registered in ``providers/__init__.py``. It
implements the full Provider contract against a live ``kiro-cli`` install
(``kiro-cli-chat`` 2.24.1): one-shot and same-id resume through
``--resume-id``, the operator's deny-by-omission behavior, and the
interactive takeover were all validated live.

Shape: ``kiro-cli chat --output-format stream-json --no-interactive``. The
prompt goes on stdin only -- never a positional argument. The CLI does offer a
positional ``[INPUT]`` (and a ``-`` marker meaning "read stdin"), but a live
probe proved the piped prompt is consumed as the prompt itself, so argv stays
clean as the stdin contract in ``providers.base`` requires. Events are
newline-delimited ACP JSON: the session-bearing events (``metadata``,
``sessionUpdate``, ``runFinished``) carry the session id under
``data.sessionId``, and the run ends with ``runFinished`` whose
``data.finalText`` is the authoritative reply.

Identity: adapters never mint session ids. We capture the bare uuid kiro
reports and resume with ``--resume-id <id>``: resume keeps the same id and
appends to the same session file (it never forks), so no fork handling exists
in this adapter.

Storage: ``~/.kiro/sessions/cli/<uuid>.jsonl``
(``{"version":"v1","kind":"Prompt"|"AssistantMessage","data":...}``) plus a
``<uuid>.json`` sidecar (``cwd``, ``session_id``). Sizing and forking go
through chat-mother-forker's ``kiro_cli`` provider, so this adapter never
locates files itself.

Read-only operator: omitting trust flags *is* the profile, but what it buys
is split (live-validated against ``kiro-cli-chat`` 2.24.1): with no
``-a/--trust-all-tools`` (or ``--trust-tools``) and ``--no-interactive``,
writes and shell commands are denied at the permission layer -- fast, exit 0,
no file written, no prompt surface that could hang the operator -- while file
*reads* run unattended. Read-only git sits with the shell side: it is denied
on this binary, so the operator is file-reads-only. Trust flags therefore stay
out of ``build_command`` (it feeds the operator too): an agent workspace grants
tool trust through the workspace config's ``[harness] extra_args`` instead, or
its edits are silently denied.
"""

from __future__ import annotations

import json
from pathlib import Path

from backseat_harness.providers.base import OperatorLaunch, Provider, SpawnSpec


class KiroProvider(Provider):
    name = "kiro"
    history_provider = "kiro_cli"

    def build_command(self, spec: SpawnSpec) -> list[str]:
        # Bare `chat`: the prompt is piped on stdin (see providers.base) --
        # no positional INPUT and no `-` marker. Trust flags stay out: this
        # argv is reused for the read-only operator (deny-by-omission).
        cmd = ["kiro-cli", "chat", "--output-format", "stream-json", "--no-interactive"]
        if spec.resume_native_id:
            cmd += ["--resume-id", spec.resume_native_id]
        cmd += list(spec.extra_args)
        return cmd

    def parse_result(self, exit_code: int, stdout: str, stderr: str) -> str | None:
        """Scan ACP JSONL events for ``data.sessionId``; the last one wins.

        ``runFinished`` is the terminal event of a well-formed run, so
        last-wins yields that run's id. Noise and non-JSON lines are skipped
        per-line. ``None`` means the loop falls back to the marker search.
        """
        session_id: str | None = None
        for event in _iter_events(stdout):
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            value = data.get("sessionId")
            if isinstance(value, str) and value:
                session_id = value
        return session_id

    def extract_text(self, stdout: str) -> str:
        """Reassemble the reply from the stream: finalText, else the chunks.

        Prefers ``runFinished.data.finalText`` (present even on runs whose
        tool calls were denied). Otherwise concatenates the
        ``agent_message_chunk`` text parts in order -- they are fragments of
        one message, so they join without a separator. Falls back to raw
        stdout so parse failures stay visible instead of silently empty.
        """
        chunks: list[str] = []
        for event in _iter_events(stdout):
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            if event.get("type") == "runFinished":
                final = data.get("finalText")
                if isinstance(final, str) and final:
                    return final
            update = data.get("update")
            if not isinstance(update, dict):
                continue
            if update.get("sessionUpdate") != "agent_message_chunk":
                continue
            content = update.get("content")
            if not isinstance(content, dict) or content.get("type") != "text":
                continue
            text = content.get("text")
            if isinstance(text, str):
                chunks.append(text)
        return "".join(chunks) if chunks else stdout

    def prepare_operator(
        self, profile_dir: Path, persona: str, verify_commands: list[str]
    ) -> OperatorLaunch:
        """Read-only by omission: no trust flags, no profile file.

        The trust flags are absent from ``build_command`` (never present) and
        ``--no-interactive`` rejects permission requests at the permission
        layer without prompting: on ``kiro-cli-chat`` 2.24.1 writes and shell
        commands are denied instantly (exit 0, no hang, no file), file reads
        run, and read-only git is denied -- it rides the shell tool, whose only
        grants are category-wide, so no git-scoped exception exists. The
        operator is therefore file-reads-only. ``profile_dir`` and ``persona``
        are unused by design -- there is nothing to write there;
        ``verify_commands`` are read-only git by convention and cannot run
        unattended on this binary, so they are accepted but not used.
        """
        return OperatorLaunch(env={}, extra_args=())

    def interactive_command(self, native_session_id: str | None) -> list[str]:
        cmd = ["kiro-cli", "chat"]
        if native_session_id:
            cmd += ["--resume-id", native_session_id]
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
