"""Human-facing progress lines on stderr.

A long-running loop is otherwise silent until it exits, so the launching
terminal has nothing to watch. This prints one compact line per event to
*stderr*, deliberately: stdout stays clean for a parent process consuming
``--format json``.

Each line is prefixed and (when the stream is a TTY) colored so the three
channels -- the iteration header, the operator's ledger note, and the exact
prompt sent to the managed agent -- are visually distinct. Notes and prompts
are truncated so a wall of text never floods the terminal.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from contextlib import contextmanager
from typing import TextIO

# Hard cap on how much of a note/prompt we show. The whole line -- prefix,
# content, and the "..." marker -- is also kept within the terminal width, so a
# line never wraps. Content is cut to the smaller of this cap and what the width
# allows; a plain "..." (no char count) marks that it was cut.
_MAX_FIELD_CHARS = 110
_ELLIPSIS = "..."

# ANSI styles, applied only when writing to a TTY.
_RESET = "\033[0m"
_STYLES = {
    "iter": "\033[1;36m",  # bold cyan  -- iteration header
    "note": "\033[33m",  # yellow      -- operator's ledger note
    "prompt": "\033[32m",  # green      -- prompt sent to managed agent
}


def _use_color(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def _content_budget(prefix: str) -> int:
    """How many content chars fit after ``prefix`` on one line.

    The budget is the smaller of the field cap and what the terminal width
    leaves once the prefix, its trailing space, and the ``...`` marker are
    reserved -- so ``prefix content...`` never exceeds the width. Floored so a
    narrow terminal still shows something.
    """
    width = shutil.get_terminal_size(fallback=(80, 24)).columns
    room = width - len(prefix) - 1 - len(_ELLIPSIS)
    return max(20, min(_MAX_FIELD_CHARS, room))


def _truncate(text: str, budget: int) -> str:
    """Collapse whitespace and cut to ``budget`` chars, appending ``...``."""
    flat = " ".join(text.split())
    if len(flat) <= budget:
        return flat
    return flat[:budget] + _ELLIPSIS


def _emit(stream: TextIO, channel: str, prefix: str, text: str) -> None:
    line = f"{prefix} {_truncate(text, _content_budget(prefix))}"
    if _use_color(stream):
        line = f"{_STYLES[channel]}{line}{_RESET}"
    print(line, file=stream, flush=True)


def iteration_start(
    iteration: int,
    generation: int,
    phase: str,
    *,
    stream: TextIO | None = None,
) -> None:
    """Announce the start of a loop iteration (or the plan phase)."""
    stream = stream or sys.stderr
    prefix = f"[iter {iteration} gen {generation} {phase}]"
    # No user text to truncate here; print the header verbatim.
    line = prefix
    if _use_color(stream):
        line = f"{_STYLES['iter']}{line}{_RESET}"
    print(line, file=stream, flush=True)


def operator_note(note: str, *, stream: TextIO | None = None) -> None:
    """Print the operator's one-line ledger note."""
    if not note:
        return
    _emit(stream or sys.stderr, "note", "  note:", note)


def next_prompt(prompt: str | None, *, stream: TextIO | None = None) -> None:
    """Print the exact prompt queued for the next managed turn."""
    if not prompt:
        return
    _emit(stream or sys.stderr, "prompt", "  prompt:", prompt)


# --- Debug mode: what the loop is doing, and for how long. ---

#: Off by default. A run prints one line per event (above) and is otherwise
#: silent, which is the right density for watching a task; the debug channel is
#: for the question the event lines cannot answer -- what is it doing right now,
#: and how long has it been not answering. Every provider call is an expected
#: silence bounded by a configured ceiling, so those print as one announce line
#: and the log path; everything else is a harness step that is normally fast and
#: becomes the suspect the moment it isn't.
_DEBUG = False

#: Style for debug lines, applied only when writing to a TTY. Dim rather than a
#: fourth channel color, so debug stays legible next to the three event lines.
_DEBUG_STYLE = "\033[2;37m"  # faint grey


def set_debug(enabled: bool) -> None:
    """Turn the debug step channel on or off for this process."""
    global _DEBUG
    _DEBUG = enabled


def debug_enabled() -> bool:
    return _DEBUG


def _debug_line(prefix: str, text: str, stream: TextIO) -> None:
    line = f"{prefix} {text}"
    if _use_color(stream):
        line = f"{_DEBUG_STYLE}{line}{_RESET}"
    print(line, file=stream, flush=True)


def debug_note(text: str, *, stream: TextIO | None = None) -> None:
    """One un-timed debug line, for a step with no meaningful duration.

    Used for the things a caller announces rather than performs: which provider
    is about to be called, at what ceiling, writing to which log. Those are the
    lines that answer "is it stuck in the provider or in the harness?" -- the
    provider call is an expected silence bounded by a configured timeout, so it
    is the one place in a run that is allowed to print nothing for minutes.
    """
    if not _DEBUG:
        return
    _debug_line("[dbg]", text, stream or sys.stderr)


@contextmanager
def step(what: str, *, stream: TextIO | None = None):
    """Time one loop step: announce it, then report how long it took.

    The elapsed figure is what makes a log readable after the fact: a run that
    stalled shows one step with a large number, not a uniform gap. Prints
    nothing unless debug is on, so the default output is unchanged.
    """
    if not _DEBUG:
        yield
        return
    out = stream or sys.stderr
    _debug_line("[dbg]", f"-> {what}", out)
    started = time.monotonic()
    try:
        yield
    finally:
        _debug_line("[dbg]", f"<- {what} ({time.monotonic() - started:.1f}s)", out)
