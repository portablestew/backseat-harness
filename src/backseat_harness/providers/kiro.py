"""Kiro Code adapter -- registered and live-validated (``kiro-cli-chat`` 2.26.1).

This is one of the providers registered in ``providers/__init__.py``. It
implements the full Provider contract against a live ``kiro-cli`` install
(``kiro-cli-chat`` 2.26.1): one-shot and same-id resume through ``--resume-id``,
the trust/deny boundary, and the interactive takeover were all validated live.

Model flags are version-dependent, and 2.26.1 changed what this adapter has to
defend against. Measured 2026-09-30 (``docs/1-research-model-switch.md``,
"Re-test on kiro-cli 2.26.1"):

* ``--model`` is now *applied* on fresh and resumed turns -- no stderr warn,
  and the sidecar's per-turn ``user_turn_metadatas[].model`` records the
  requested id -- where 2.24.1/2.25.0 rejected the flag with
  ``[warn] failed to set model '<id>': Method not found`` and ran the turn on
  ``auto``. The adapter passes the flag through and is otherwise
  fire-and-forget: no claim is made about which model served a turn.
* An id the catalog does not have is refused outright on both paths:
  ``runError`` at stage ``prompt`` whose message names the id, exit 1, empty
  stderr. ``rejected_model_id`` reads that back so the loop can surface the
  binding that asked for it instead of an unnamed nonzero exit.

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

Permissions: ``--trust-all-tools`` is the default (see ``build_command``). The
alternative is a silent auto-deny of every write and shell command, which fails
loudly in the wrong way. Narrow the tool set with a repo-defined agent selected
via ``--agent <name>``, whose tool-permission block is the command-scoped axis;
``--trust-tools`` is category-level.
"""

from __future__ import annotations

import json
import re

from backseat_harness.providers.base import Provider, SpawnSpec

#: kiro's own words for an id it does not have, as seen in a ``runError`` on
#: both fresh and ``--resume-id`` turns (2.26.1): "The model '<id>' is not
#: available. Please use '/model' to select a different model and try again."
_REJECTION_RE = re.compile(r"The model '([^']+)' is not available")


class KiroProvider(Provider):
    name = "kiro"
    history_provider = "kiro_cli"
    list_models_command = "kiro-cli chat --list-models"

    def build_command(self, spec: SpawnSpec) -> list[str]:
        # Bare `chat`: the prompt is piped on stdin (see providers.base) --
        # no positional INPUT and no `-` marker.
        # --trust-all-tools: a non-interactive process cannot answer a prompt,
        # so approve rather than silently deny every write. This argv also runs
        # the operator, which is intentional -- both sides are trusted.
        cmd = [
            "kiro-cli", "chat", "--output-format", "stream-json",
            "--no-interactive", "--trust-all-tools",
        ]
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

    def rejected_model_id(self, stdout: str, stderr: str) -> str | None:
        """The id kiro refused, read from its own ``runError``, or ``None``.

        kiro quotes the id back in the message -- ``The model '<id>' is not
        available`` at stage ``prompt`` -- so the id is the CLI's own words
        rather than a guess, and it is the one thing the loop cannot get from
        the exit code (a 1 is just a failed turn). Fresh turns and resumed
        turns emit the identical event (live-checked on 2.26.1, see
        ``docs/12-kiro-loud-path-live.md``), so one read covers both. The
        message is also scanned on stderr in case a later version moves it; a
        well-formed turn carries neither and returns ``None``.
        """
        for event in _iter_events(stdout):
            if event.get("type") != "runError":
                continue
            data = event.get("data")
            message = data.get("message") if isinstance(data, dict) else None
            if isinstance(message, str):
                match = _REJECTION_RE.search(message)
                if match:
                    return match.group(1)
        match = _REJECTION_RE.search(stderr)
        return match.group(1) if match else None

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
