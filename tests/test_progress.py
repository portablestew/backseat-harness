"""Progress lines go to stderr, are prefixed, and truncate long fields."""

import io

from backseat_harness import progress


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
