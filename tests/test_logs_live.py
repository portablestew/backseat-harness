"""The real subprocess path, end to end through the loop.

Every other loop test injects a fake managed runner, which means none of them
exercises what this feature actually changed: the CLI's output being handed to a
file descriptor instead of a pipe, written to the lane's log, read back off disk
to recover the session id, and redacted before the operator can read it. Here
the runner is ``spawn.run_turn`` and the "CLI" is a real Python process that
speaks the newline-delimited JSON a provider emits.
"""

import json
import sys
from dataclasses import replace

from conftest import FakeProvider

from backseat_harness import marker as marker_mod
from backseat_harness import spawn, storage
from backseat_harness.loop import run_task
from backseat_harness.models import TaskStatus
from backseat_harness.providers.base import TurnResult


class _StreamingCLI(FakeProvider):
    """A provider whose command is a real process emitting an event stream.

    Prints the session id the way a provider does, and echoes back the marker it
    was given on stdin -- so the log contains the task's root UUID, exactly as a
    real turn's does.
    """

    name = "streaming"
    history_provider = "streaming_history"

    def __init__(self):
        super().__init__()
        self._uuid = None

    def build_command(self, spec):
        self._uuid = marker_mod.parse_marker(spec.prompt)[0] if spec.prompt else None
        program = (
            "import json, sys\n"
            "prompt = sys.stdin.read()\n"
            "print(json.dumps({'type': 'text', 'sessionID': 'ses_real'}))\n"
            "print(json.dumps({'type': 'text', 'echo': prompt}))\n"
        )
        return [sys.executable, "-c", program]

    def parse_result(self, exit_code, stdout, stderr):
        for line in stdout.splitlines():
            if not line.startswith("{"):
                continue
            event = json.loads(line)
            if isinstance(event.get("sessionID"), str):
                return event["sessionID"]
        return None


def _lane(tmp_path, slug="live"):
    """A real lane directory, as ``TaskPaths`` so the test can ask for ``logs/``."""
    from backseat_harness.config import Config, TaskPaths

    paths = TaskPaths(tmp_path / ".backseat" / "tasks" / slug)
    paths.root.mkdir(parents=True)
    paths.goal.write_text("Prove the log path.\n", encoding="utf-8")
    paths.checklist.write_text('{"steps": {}, "remarks": []}', encoding="utf-8")
    config = Config(workspace_root=tmp_path, provider="streaming")
    return slug, paths, config


def test_a_real_turn_writes_a_redacted_log_and_recovers_the_session_id(
    tmp_path, stub_history
):
    slug, paths, config = _lane(tmp_path)
    provider = _StreamingCLI()

    result = run_task(
        slug,
        config,
        provider,
        spawn.run_turn,
        lambda p: '{"verdict":"done"}',
        first_prompt="go",
    )

    assert result.status is TaskStatus.COMPLETE

    # The log exists, is named for the iteration, and holds the CLI's own stream.
    log = paths.logs / "iter-0001.managed.log"
    assert log.exists()
    text = log.read_text(encoding="utf-8")
    assert '"sessionID": "ses_real"' in text

    # The session id made it out of the log and onto the binding, which is the
    # part that would break if read-back were not byte-identical to the pipe.
    binding = storage.load_binding(paths)
    assert binding.current.native_id == "ses_real"

    # And the root UUID is gone from disk, even though the CLI echoed the prompt
    # -- which carries the marker -- straight into the log. The redacted
    # placeholder is present in its place, so redaction ran on this file rather
    # than on some other copy of the stream.
    root_uuid = binding.root_uuid
    assert root_uuid not in text
    assert marker_mod.REDACTED_ROOT in text


def test_a_real_turn_degrades_rather_than_dying_when_the_log_is_unwritable(
    tmp_path, stub_history, monkeypatch
):
    """An injected runner that never writes a log must not break the loop.

    The log is an artifact, not a precondition: a run whose managed runner is a
    fake (or whose launch failed before the file was opened) still has to reach a
    verdict. Redaction returns rather than raising in exactly that case.
    """
    slug, paths, config = _lane(tmp_path)
    config = replace(config, provider="fake")

    def fake_runner(prov, spec):
        return TurnResult(
            exit_code=0, timed_out=False, duration_s=0.1,
            native_session_id="sess-1", stdout="", stderr="",
        )

    result = run_task(
        slug, config, FakeProvider(), fake_runner,
        lambda p: '{"verdict":"done"}', first_prompt="go",
    )
    assert result.status is TaskStatus.COMPLETE