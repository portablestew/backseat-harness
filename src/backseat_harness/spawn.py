"""Managed-turn subprocess lifecycle: launch, watchdog timeout, result assembly.

Turn end is process exit (the design's contract). Any nonzero exit or timeout is
recorded uniformly and surfaced to the system agent only as an optional hint;
the harness does not branch its control flow on exit codes.

The prompt is written to the child's stdin rather than passed in argv, because a
compacted first turn carries a forked transcript that would exceed the platform
command-line limit (see providers.base).
"""

from __future__ import annotations

import subprocess
import time

from backseat_harness.providers.base import Provider, SpawnSpec, TurnResult


def run_turn(provider: Provider, spec: SpawnSpec) -> TurnResult:
    """Run one managed-agent turn to completion or timeout.

    On timeout the process is terminated and ``timed_out`` is set. The
    non-interactive permission flags in ``spec.extra_args`` are what let a turn
    actually finish instead of blocking on an approval prompt; the watchdog is
    the backstop.
    """
    cmd = provider.build_command(spec)
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(spec.cwd),
            input=spec.prompt,
            capture_output=True,
            text=True,
            timeout=spec.timeout_s,
        )
        duration = time.monotonic() - start
        native = provider.parse_result(proc.returncode, proc.stdout, proc.stderr)
        return TurnResult(
            exit_code=proc.returncode,
            timed_out=False,
            duration_s=duration,
            native_session_id=native,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - start
        stdout = _as_text(exc.stdout)
        stderr = _as_text(exc.stderr)
        # Still try for the session id: a timed-out turn did real work, and the
        # next turn needs to resume the same session.
        native = provider.parse_result(-1, stdout, stderr)
        return TurnResult(
            exit_code=-1,
            timed_out=True,
            duration_s=duration,
            native_session_id=native,
            stdout=stdout,
            stderr=stderr,
        )


def _as_text(data: object) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)
