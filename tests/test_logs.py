"""Per-call provider logs: the file is the stream, and it is redacted.

Two properties carry the design. The child's stdout is handed to it as a file
descriptor, so a call that is still running -- or has hung, the case a log
buffered to the end of the call cannot explain -- is already on disk and
tailable. And stderr goes to the same file, so one file per call holds the two
streams in the order they were produced, rather than in an order reconstructed
after the fact.

The redaction is not a nicety. The log carries the task's root UUID, and an
operator that reads a log file and quotes it makes its own stored session match
the task's marker search -- where the newest match wins, pointing sizing and
compaction at the operator instead of the managed agent.
"""

import subprocess
import sys
import time

import pytest

from backseat_harness import logs, marker, spawn
from backseat_harness.config import Config
from backseat_harness.providers.base import SpawnSpec, TurnResult


def _config(tmp_path) -> Config:
    return Config(workspace_root=tmp_path, provider="fake")


def _paths(config: Config, slug: str = "demo"):
    return config.task_paths(slug)


def test_call_log_names_one_file_per_call_and_creates_the_dir(tmp_path):
    paths = _paths(_config(tmp_path))

    managed = logs.call_log(paths, 7, logs.ROLE_MANAGED)
    first = logs.call_log(paths, 7, logs.ROLE_OPERATOR, 1)
    second = logs.call_log(paths, 7, logs.ROLE_OPERATOR, 2)

    assert paths.logs.is_dir()
    assert managed.name == "iter-0007.managed.log"
    # A re-asked verdict must leave the answer that came back unusable on disk,
    # not overwrite it: each attempt is its own file.
    assert first.name == "iter-0007.operator.1.log"
    assert second.name == "iter-0007.operator.2.log"
    assert len({managed, first, second}) == 3


def test_call_log_names_sort_chronologically(tmp_path):
    paths = _paths(_config(tmp_path))

    names = [
        logs.call_log(paths, i, logs.ROLE_MANAGED).name
        for i in (1, 2, 10, 11, 100)
    ]
    assert names == sorted(names), "zero padding exists so ls order is run order"


def test_redact_file_removes_the_root_uuid_and_keeps_the_generation(tmp_path):
    paths = _paths(_config(tmp_path))
    uuid = "d2ea8f1f-1674-4fec-b4e1-8235d2963fe6"
    log = logs.call_log(paths, 1, logs.ROLE_MANAGED)
    log.write_text(
        f'{{"text":"<meta>START SESSION {uuid} #2</meta>"}}\n', encoding="utf-8"
    )

    logs.redact_file(log, uuid)

    text = log.read_text(encoding="utf-8")
    assert uuid not in text
    assert marker.REDACTED_ROOT in text
    assert "#2" in text  # the ordinal is not identity; it stays readable


def test_redact_file_on_a_log_that_was_never_written_is_a_no_op(tmp_path):
    """A call that never produced a log has no UUID to leak.

    An injected runner in a test, or a launch that failed before the file was
    opened, leaves nothing to redact -- that must not be an error, or every
    caller would need to guard it.
    """
    paths = _paths(_config(tmp_path))
    logs.redact_file(logs.call_log(paths, 1, logs.ROLE_MANAGED), "some-uuid")


def test_redact_file_does_not_raise_on_a_multibyte_truncation(tmp_path):
    """errors="replace" is what makes a killed call's last line survivable.

    The stream was launched with that substitution, so reading the log back must
    use it too: a multibyte character cut at a write boundary is exactly what a
    killed provider leaves behind, and a strict decode would turn a diagnostic
    artifact into a failed run.
    """
    paths = _paths(_config(tmp_path))
    log = paths.logs
    log.mkdir(parents=True, exist_ok=True)
    path = log / "iter-0001.managed.log"
    path.write_bytes(b'{"text":"caf\xc3')  # a half-written é

    logs.redact_file(path, "some-uuid")  # must not raise

    assert "caf" in path.read_text(encoding="utf-8", errors="replace")


def test_current_declares_the_path_for_a_caller_that_cannot_be_told(tmp_path):
    """The operator invoker is handed a one-argument callable, so the loop
    declares the path out of band instead of threading it through."""
    paths = _paths(_config(tmp_path))
    log = logs.call_log(paths, 1, logs.ROLE_OPERATOR, 1)

    logs.set_current(log)
    assert logs.current() == log

    logs.set_current(None)
    assert logs.current() is None


def test_run_with_timeout_writes_the_streams_to_the_log_file(tmp_path):
    log = tmp_path / "logs" / "call.log"

    call = spawn.run_with_timeout(
        [
            sys.executable,
            "-c",
            "import sys; print('ses_from_log'); print('a warning', file=sys.stderr)",
        ],
        cwd=tmp_path,
        input="",
        timeout_s=60,
        log_path=log,
    )

    assert call.timed_out is False
    # The pipes are gone: the streams went to the descriptor, and the log file
    # is the read-back's job rather than the spawn's.
    assert call.stdout == "" and call.stderr == ""
    text = log.read_text(encoding="utf-8")
    assert "ses_from_log" in text
    # stderr shares the file, so one log per call holds both streams.
    assert "a warning" in text


def test_the_log_is_on_disk_while_the_call_is_still_running(tmp_path):
    """The whole reason the output is a descriptor rather than a pipe.

    A log assembled at the end of the call cannot answer "what was the last
    thing it did before it stopped answering", which is the only question a
    tailed log exists to answer.
    """
    log = tmp_path / "logs" / "live.log"
    log.parent.mkdir()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            "import time; print('event-1', flush=True); time.sleep(30)",
        ],
        stdin=subprocess.PIPE,
        stdout=log.open("wb"),
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if "event-1" in log.read_text(encoding="utf-8", errors="replace"):
                break
            time.sleep(0.05)
        else:  # pragma: no cover - the point is that the file had it already
            raise AssertionError("child's output never reached the log")
    finally:
        proc.kill()
        proc.wait(timeout=20)


def test_run_turn_parses_the_session_id_out_of_the_log(tmp_path):
    """With a log path the id comes off disk, not off the captured stream.

    Same text either way, so this must be indistinguishable to the adapter --
    which is what makes redirecting the output free for the parse path.
    """

    class _Provider:
        name = "fake"

        def build_command(self, spec):
            return [sys.executable, "-c", "print('ses_from_log')"]

        def parse_result(self, exit_code, stdout, stderr):
            for line in stdout.splitlines():
                if line.startswith("ses_"):
                    return line
            return None

    result = spawn.run_turn(
        _Provider(),
        SpawnSpec(
            prompt="p",
            cwd=tmp_path,
            timeout_s=60,
            log_path=tmp_path / "logs" / "turn.log",
        ),
    )

    assert isinstance(result, TurnResult)
    assert result.native_session_id == "ses_from_log"
    assert "ses_from_log" in result.stdout


def test_a_log_path_that_cannot_be_opened_ends_the_run(tmp_path):
    """A log the harness cannot create is a broken disk, and the run dies.

    The tempting alternative -- fall back to capturing in memory -- buys
    nothing: the task dir is already load-bearing for _persist, so a run that
    cannot write its logs cannot commit a verdict either, seconds later. Dying
    here at least names the real cause.
    """
    (tmp_path / "logs").mkdir()
    with pytest.raises(OSError):
        spawn.run_with_timeout(
            [sys.executable, "-c", "print('hi')"],
            cwd=tmp_path,
            input="",
            timeout_s=60,
            log_path=tmp_path / "logs",
        )