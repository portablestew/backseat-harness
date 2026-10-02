"""Subprocess lifecycle: the deadline must mean something.

The failure these cover is not hypothetical. An npm-installed CLI reaches the
harness through a ``.cmd`` shim, so the direct child is ``cmd.exe`` and the real
process is its child. ``Popen.kill`` ends ``cmd.exe`` and leaves that grandchild
running, still holding the inherited stdout pipe -- and CPython's Windows timeout
path re-reads that pipe to EOF after killing, so the caller blocks for as long as
the orphan lives and never sees ``TimeoutExpired``. That is a one-hour ceiling
that cost 78 minutes of a run before anyone noticed.
"""

import os
import signal
import subprocess
import sys
import time

from backseat_harness import spawn
from backseat_harness.providers.base import ProviderError, SpawnSpec, TurnResult


def _sleeper(seconds: float) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def test_resolve_argv_rejects_a_missing_cli():
    try:
        spawn.resolve_argv(["definitely-not-a-real-cli-xyzzy"])
    except ProviderError as exc:
        assert "not found on PATH" in str(exc)
    else:  # pragma: no cover - the point is that it raises
        raise AssertionError("expected ProviderError")


def test_a_command_that_finishes_is_not_timed_out(tmp_path):
    call = spawn.run_with_timeout(
        [sys.executable, "-c", "print('hello')"],
        cwd=tmp_path,
        input="",
        timeout_s=60,
    )
    assert call.timed_out is False
    assert call.returncode == 0
    assert "hello" in call.stdout


def test_prompt_arrives_on_stdin_not_argv(tmp_path):
    """A compacted first prompt can exceed the platform command-line limit, so
    it goes in on stdin. A provider that lost that would break on long forks."""
    call = spawn.run_with_timeout(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
        cwd=tmp_path,
        input="x" * 100_000,
        timeout_s=60,
    )
    assert call.stdout == "x" * 100_000


def test_a_command_that_outruns_the_deadline_is_killed_and_reported(tmp_path):
    """The ceiling lands: the call returns at the deadline, not when the child
    happens to exit."""
    start = time.monotonic()
    call = spawn.run_with_timeout(
        _sleeper(30), cwd=tmp_path, input="", timeout_s=1, grace_s=2
    )
    elapsed = time.monotonic() - start

    assert call.timed_out is True
    assert call.returncode == -1
    assert elapsed < 20, f"waited {elapsed:.1f}s past a 1s deadline"


def test_an_orphaned_grandchild_cannot_outlive_the_deadline(tmp_path):
    """The regression this helper exists for.

    The child spawns a grandchild and both sleep. The grandchild inherits the
    stdout pipe, so killing only the direct child leaves a writer on it and the
    caller waits for that writer instead of the deadline. The whole tree has to
    go, or the ceiling is decorative.
    """
    program = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "time.sleep(30)\n"
    )
    start = time.monotonic()
    call = spawn.run_with_timeout(
        [sys.executable, "-c", program], cwd=tmp_path, input="", timeout_s=1, grace_s=2
    )
    elapsed = time.monotonic() - start

    assert call.timed_out is True
    # The grandchild sleeps 30s. Returning near the deadline is the only way
    # this passes, and it is exactly what a bare kill fails to do.
    assert elapsed < 20, f"blocked {elapsed:.1f}s on a pipe held by a survivor"


def test_a_pipe_holder_outside_the_tree_cannot_outlive_the_grace_budget(tmp_path):
    """Scenario B: the direct child exits *before* the deadline, so the tree
    kill walks a dead target and never reaches the grandchild it orphaned --
    which still holds the inherited stdout pipe. Waiting for that pipe's EOF
    means waiting for the survivor (measured 45.12s against a claimed 6s
    ceiling), so the teardown must burn at most the one shared grace budget
    and abandon the close to its background thread instead.
    """
    program = (
        "import subprocess, sys\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(45)'])\n"
        "open('survivor.pid', 'w').write(str(g.pid))\n"
    )
    pid_file = tmp_path / "survivor.pid"
    start = time.monotonic()
    try:
        call = spawn.run_with_timeout(
            [sys.executable, "-c", program], cwd=tmp_path, input="", timeout_s=1, grace_s=2
        )
        elapsed = time.monotonic() - start

        assert call.timed_out is True
        assert call.returncode == -1
        # 1s deadline + 2s shared teardown budget + 1s slack for scheduling
        # and thread start. Pre-fix this path returned at 45s, when the
        # survivor exited and finally released the pipe.
        assert elapsed <= 1 + 2 + 1, f"waited {elapsed:.1f}s against a 3s bound"
    finally:
        if pid_file.exists():
            pid = pid_file.read_text().strip()
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/PID", pid], capture_output=True, check=False
                )
            else:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except OSError:
                    pass


def test_run_turn_reports_a_timeout_with_the_session_id_it_can_still_read(tmp_path):
    """A timed-out turn did real work, and the next turn resumes that session,
    so the id is still read out of the partial output rather than discarded."""

    class _Provider:
        name = "fake"

        def build_command(self, spec):
            # Prints a session id, then overruns the deadline.
            return [
                sys.executable,
                "-c",
                "import sys, time; print('ses_from_partial'); sys.stdout.flush();"
                " time.sleep(30)",
            ]

        def parse_result(self, exit_code, stdout, stderr):
            for line in stdout.splitlines():
                if line.startswith("ses_"):
                    return line
            return None

    result = spawn.run_turn(
        _Provider(),
        SpawnSpec(prompt="p", cwd=tmp_path, timeout_s=1),
    )

    assert isinstance(result, TurnResult)
    assert result.timed_out is True
    assert result.exit_code == -1
    assert result.native_session_id == "ses_from_partial"
    assert result.duration_s < 20


def test_run_turn_passes_a_clean_result_through_untouched(tmp_path):
    class _Provider:
        name = "fake"

        def build_command(self, spec):
            return [sys.executable, "-c", "print('ses_ok')"]

        def parse_result(self, exit_code, stdout, stderr):
            return stdout.strip() or None

    result = spawn.run_turn(_Provider(), SpawnSpec(prompt="p", cwd=tmp_path, timeout_s=60))

    assert result.timed_out is False
    assert result.exit_code == 0
    assert result.native_session_id == "ses_ok"


def test_a_nonzero_exit_is_carried_not_raised(tmp_path):
    """Exit codes are a hint to the operator, never a branch in the harness."""

    class _Provider:
        name = "fake"

        def build_command(self, spec):
            return [sys.executable, "-c", "import sys; sys.exit(3)"]

        def parse_result(self, exit_code, stdout, stderr):
            return None

    result = spawn.run_turn(_Provider(), SpawnSpec(prompt="p", cwd=tmp_path, timeout_s=60))

    assert result.exit_code == 3
    assert result.timed_out is False


def test_run_interactive_still_defers_to_the_human(monkeypatch):
    """The takeover path hands over the terminal and has no deadline: the human
    quits when the conversation is unambiguous. It must not acquire one by
    accident when the managed and operator paths gain theirs."""

    class _Provider:
        name = "fake"

        def interactive_command(self, native_session_id):
            return ["fake-cli", "session", native_session_id or "fresh"]

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        seen["timeout"] = kwargs.get("timeout")

        class _Proc:
            returncode = 0

        return _Proc()

    monkeypatch.setattr(spawn, "resolve_argv", lambda cmd: cmd)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert spawn.run_interactive(_Provider(), "ses_1", ".") == 0
    assert seen["cmd"] == ["fake-cli", "session", "ses_1"]
    assert seen["timeout"] is None  # no deadline is imposed on the human
