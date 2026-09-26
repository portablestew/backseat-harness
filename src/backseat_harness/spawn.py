"""Managed-turn subprocess lifecycle: launch, watchdog timeout, result assembly.

Turn end is process exit (the design's contract). Any nonzero exit or timeout is
recorded uniformly and surfaced to the system agent only as an optional hint;
the harness does not branch its control flow on exit codes.

The prompt is written to the child's stdin rather than passed in argv, because a
compacted first turn carries a forked transcript that would exceed the platform
command-line limit (see providers.base).
"""

from __future__ import annotations

import shutil
import subprocess
import time

from backseat_harness.providers.base import Provider, ProviderError, SpawnSpec, TurnResult


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


def run_turn(provider: Provider, spec: SpawnSpec) -> TurnResult:
    """Run one managed-agent turn to completion or timeout.

    On timeout the process is terminated and ``timed_out`` is set. The
    non-interactive permission flags in ``spec.extra_args`` are what let a turn
    actually finish instead of blocking on an approval prompt; the watchdog is
    the backstop.
    """
    cmd = resolve_argv(provider.build_command(spec))
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(spec.cwd),
            input=spec.prompt,
            capture_output=True,
            text=True,
            # Explicit, so Windows doesn't fall back to the ANSI code page for
            # prompts and transcripts that routinely contain non-ASCII text.
            encoding="utf-8",
            errors="replace",
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
