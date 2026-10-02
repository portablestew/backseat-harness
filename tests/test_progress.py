"""Progress lines go to stderr, are prefixed, and truncate long fields."""

import io

import pytest

from backseat_harness import progress


@pytest.fixture(autouse=True)
def _debug_off():
    """The debug flag is process-wide, so it must not leak between tests."""
    progress.set_debug(False)
    yield
    progress.set_debug(False)


def test_iteration_start_header():
    out = io.StringIO()
    progress.iteration_start(3, 2, "review", stream=out)
    assert out.getvalue().strip() == "[iter 3 gen 2 review]"


def test_note_and_prompt_have_distinct_prefixes():
    out = io.StringIO()
    progress.operator_note("looks fine", stream=out)
    progress.next_prompt("do the next thing", stream=out)
    lines = out.getvalue().splitlines()
    assert lines[0].startswith("  note:")
    assert lines[1].startswith("  prompt:")
    assert "looks fine" in lines[0]
    assert "do the next thing" in lines[1]


def test_long_field_is_truncated_with_plain_ellipsis():
    out = io.StringIO()
    long = "x" * 500
    progress.next_prompt(long, stream=out)
    line = out.getvalue().strip()
    # Plain "..." marks the cut; no char count.
    assert line.endswith("...")
    assert "chars" not in line
    # The whole line (prefix + content + "...") stays within the fallback width.
    assert len(line) <= 120


def test_line_fits_terminal_width(monkeypatch):
    import os

    monkeypatch.setattr(
        progress.shutil,
        "get_terminal_size",
        lambda fallback=(80, 24): os.terminal_size((60, 24)),
    )
    out = io.StringIO()
    progress.next_prompt("y" * 500, stream=out)
    line = out.getvalue().rstrip("\n")
    assert len(line) <= 60
    assert line.startswith("  prompt: ")
    assert line.endswith("...")


def test_whitespace_is_collapsed():
    out = io.StringIO()
    progress.operator_note("first\n\n  second\tthird", stream=out)
    assert "first second third" in out.getvalue()


def test_empty_note_and_prompt_print_nothing():
    out = io.StringIO()
    progress.operator_note("", stream=out)
    progress.next_prompt(None, stream=out)
    assert out.getvalue() == ""


def test_non_tty_stream_has_no_ansi_color():
    out = io.StringIO()  # StringIO is not a TTY
    progress.iteration_start(1, 1, "plan", stream=out)
    progress.operator_note("hello", stream=out)
    assert "\033[" not in out.getvalue()


def test_debug_channel_is_silent_by_default():
    """The event lines above are the normal density for watching a task; the
    step channel is the answer to a different question, and it must not add
    noise to an ordinary run."""
    out = io.StringIO()
    progress.set_debug(False)
    with progress.step("capture transcript", stream=out):
        pass
    progress.debug_note("calling provider", stream=out)
    assert out.getvalue() == ""


def test_a_step_announces_itself_then_reports_elapsed():
    out = io.StringIO()
    progress.set_debug(True)
    with progress.step("capture transcript", stream=out):
        pass
    lines = out.getvalue().splitlines()

    assert len(lines) == 2
    assert lines[0].startswith("[dbg]") and "-> capture transcript" in lines[0]
    assert lines[1].startswith("[dbg]") and "<- capture transcript" in lines[1]
    assert "s)" in lines[1], "the elapsed figure is what makes a log readable"


def test_a_step_reports_even_when_it_raises():
    """A step that blew up is exactly the one worth timing -- the failure mode
    being debugged is a step that takes too long, hangs, or dies, and all three
    read the same without this."""
    out = io.StringIO()
    progress.set_debug(True)
    with pytest.raises(RuntimeError), progress.step("history read", stream=out):
        raise RuntimeError("boom")

    lines = out.getvalue().splitlines()
    assert len(lines) == 2
    assert "<- history read" in lines[1]


def test_debug_note_is_one_line_with_no_timing():
    out = io.StringIO()
    progress.set_debug(True)
    progress.debug_note("calling managed provider opencode", stream=out)
    lines = out.getvalue().splitlines()
    assert len(lines) == 1
    assert "opencode" in lines[0]
    assert "<-" not in lines[0]


def test_debug_lines_go_to_stderr_by_default():
    """Same channel discipline as the event lines: stdout stays clean for a
    parent process consuming --format json."""
    progress.set_debug(True)
    import sys

    original = sys.stderr
    captured = io.StringIO()
    sys.stderr = captured
    try:
        with progress.step("persist verdict"):
            pass
    finally:
        sys.stderr = original

    assert "persist verdict" in captured.getvalue()


def test_debug_lines_have_no_ansi_when_the_stream_is_not_a_tty():
    out = io.StringIO()  # StringIO is not a TTY
    progress.set_debug(True)
    with progress.step("capture transcript", stream=out):
        pass
    progress.debug_note("note", stream=out)
    assert "\033[" not in out.getvalue()