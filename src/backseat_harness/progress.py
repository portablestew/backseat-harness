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
