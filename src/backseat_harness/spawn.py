"""Managed-turn subprocess lifecycle: launch, watchdog timeout, result assembly.

Turn end is process exit (the design's contract). Any nonzero exit or timeout is
recorded uniformly and surfaced to the operator only as an optional hint;
the harness does not branch its control flow on exit codes.

The prompt is written to the child's stdin rather than passed in argv, because a
compacted first turn carries a forked transcript that would exceed the platform
command-line limit (see providers.base).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from typing import NamedTuple

from backseat_harness.providers.base import Provider, ProviderError, SpawnSpec, TurnResult

#: One grace budget, started the moment the deadline fires, covering every
#: teardown step that follows -- the tree kill, the final drain, and the pipe
#: close. Short on purpose, and shared rather than per step: the deadline is
#: the point of the helper, so a survivor that still holds a pipe must not
#: turn a bounded wait back into an unbounded one.
DEFAULT_KILL_GRACE_S = 5


class CompletedCall(NamedTuple):
    """Result of one bounded subprocess call."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


def resolve_argv(cmd: list[str]) -> list[str]:
    """Resolve the executable in ``cmd`` against PATH (honoring PATHEXT).

    Needed on Windows: CLIs installed through npm are ``.cmd`` shims, and
    ``subprocess`` without a shell only finds ``.exe`` files. Prompts never go in
    argv (see providers.base), so running a shim this way stays quoting-safe.
    """
    found = shutil.which(cmd[0])
    if found is None:
        raise ProviderError(f"{cmd[0]!r} not found on PATH; is the CLI installed?")
    return [found, *cmd[1:]]


def _kill_tree(proc: subprocess.Popen, budget_s: float) -> None:
    """Terminate the child and everything it spawned, inside the grace budget.

    ``Popen.kill`` is ``TerminateProcess`` on the direct child alone, and npm
    CLIs reach us through a ``.cmd`` shim -- so the direct child is ``cmd.exe``
    and the real process survives it. That survivor is what makes a plain
    timeout unenforceable: it keeps the inherited stdout pipe open, and
    CPython's Windows timeout path re-reads that pipe to EOF after killing, so
    the caller blocks for as long as the orphan lives and never sees
    ``TimeoutExpired``. Killing the tree is what makes the deadline mean
    anything; the bare ``kill()`` afterwards is only a race fallback for when
    the tree walk finds nothing left to kill. ``taskkill`` itself draws on the
    remaining budget: a helper that hangs must not be the thing that blows the
    ceiling, so it is skipped outright when the budget is already spent (the
    syscall-level ``kill()`` below still runs either way).
    """
    if os.name == "nt":
        if budget_s > 0:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    check=False,
                    timeout=budget_s,
                )
            except (subprocess.TimeoutExpired, OSError):
                pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _close_pipes(proc: subprocess.Popen, budget_s: float) -> None:
    """Close the child's pipes without letting a survivor own the return.

    In text mode CPython reads each output pipe on a daemon thread that closes
    the stream at EOF (``subprocess._readerthread``), so a foreground
    ``pipe.close()`` waits on that reader -- and the reader waits for EOF,
    which a survivor holding the inherited write end postpones until *it*
    exits (measured: 45s blocked against a claimed 6s ceiling). The closes
    therefore run on their own daemon thread, joined only for the budget that
    remains; at the budget's end the pipes are abandoned to that thread, which
    still closes them at EOF. Handles live exactly as long as the survivor
    either way -- the reader frees them at EOF regardless of who asked first
    -- so this buys back the ceiling without extending handle lifetime, and
    nothing in this process waits on the survivor.
    """
    pipes = [p for p in (proc.stdin, proc.stdout, proc.stderr) if p is not None]
    if not pipes:
        return

    def close_all() -> None:
        for pipe in pipes:
            try:
                pipe.close()
            except OSError:
                pass

    worker = threading.Thread(target=close_all, daemon=True)
    worker.start()
    worker.join(timeout=max(budget_s, 0.0))


def run_with_timeout(
    cmd: list[str],
    *,
    cwd,
    input: str,
    timeout_s: int,
    grace_s: int = DEFAULT_KILL_GRACE_S,
) -> CompletedCall:
    """Run a command to completion, to a deadline, or to neither.

    The one place the harness launches a provider CLI, so the deadline is
    enforced the same way for a managed turn and an operator call. It never
    blocks past ``timeout_s`` by more than ``grace_s``: the deadline starts
    one grace budget that covers the whole teardown -- killing the tree, the
    final drain, closing the pipes, waiting for the child -- and a pipe a
    survivor still holds (one the tree walk missed) is abandoned at the
    budget's end rather than waited on to EOF. Text mode throughout, so
    prompts and transcripts containing non-ASCII do not fall back to the ANSI
    code page.
    """
    # Platform by platform: on Windows `taskkill /T` walks parentage, so the
    # tree is reachable with or without a new process group -- the flag buys
    # isolation only. On POSIX `start_new_session` is load-bearing:
    # `_kill_tree`'s `os.killpg` would otherwise signal this process's own
    # group and take the harness down with the child. What holds on both: a
    # console Ctrl-C lands in the new group/session, not on a child whose
    # lifetime this call owns.
    group_kwargs = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **group_kwargs,
    )
    try:
        stdout, stderr = proc.communicate(input, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        partial_out, partial_err = _as_text(exc.stdout), _as_text(exc.stderr)
        # One budget for everything below: kill, drain, close, wait. Giving
        # each step its own grace would let the chain reach timeout_s +
        # 2 * grace_s before _close_pipes is even reached, and the close used
        # to be unbounded on top of that; a shared deadline keeps the entire
        # teardown inside timeout_s + grace_s.
        deadline = time.monotonic() + grace_s

        def budget() -> float:
            return deadline - time.monotonic()

        _kill_tree(proc, budget())
        try:
            stdout, stderr = proc.communicate(timeout=max(budget(), 0.0))
        except subprocess.TimeoutExpired:
            _close_pipes(proc, budget())
            try:
                proc.wait(timeout=max(budget(), 0.0))
            except subprocess.TimeoutExpired:
                pass
            stdout, stderr = partial_out, partial_err
        return CompletedCall(-1, stdout, stderr, True)
    return CompletedCall(proc.returncode, stdout, stderr, False)


def run_turn(provider: Provider, spec: SpawnSpec) -> TurnResult:
    """Run one managed-agent turn to completion or timeout.

    On timeout the tree is terminated and ``timed_out`` is set. The
    non-interactive permission flags in ``spec.extra_args`` are what let a turn
    actually finish instead of blocking on an approval prompt; the watchdog is
    the backstop.
    """
    cmd = resolve_argv(provider.build_command(spec))
    start = time.monotonic()
    call = run_with_timeout(cmd, cwd=spec.cwd, input=spec.prompt, timeout_s=spec.timeout_s)
    duration = time.monotonic() - start
    # Still try for the session id even after a timeout: a timed-out turn did
    # real work, and the next turn needs to resume the same session.
    exit_code = -1 if call.timed_out else call.returncode
    native = provider.parse_result(exit_code, call.stdout, call.stderr)
    return TurnResult(
        exit_code=exit_code,
        timed_out=call.timed_out,
        duration_s=duration,
        native_session_id=native,
        stdout=call.stdout,
        stderr=call.stderr,
    )


def run_interactive(provider: Provider, native_session_id: str | None, cwd) -> int:
    """Hand the terminal to the CLI's interactive UI and block until it exits.

    Unlike ``run_turn``, this inherits the parent's stdin/stdout/stderr so the
    human drives a real TUI, and there is no timeout: the human quits when the
    conversation is unambiguous, and the harness resumes on exit. Returns the
    process exit code.
    """
    cmd = resolve_argv(provider.interactive_command(native_session_id))
    return subprocess.run(cmd, cwd=str(cwd)).returncode


def _as_text(data: object) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)
