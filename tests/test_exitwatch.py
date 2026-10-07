"""Exit observability: note first, then let the platform's default proceed."""

import signal

import pytest

from backseat_harness import exitwatch


def test_install_wraps_sigint_with_a_note_and_restores():
    notes: list[str] = []
    before = signal.getsignal(signal.SIGINT)
    restore = exitwatch.install(notes.append)
    try:
        handler = signal.getsignal(signal.SIGINT)
        assert handler is not before
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGINT, None)
        assert notes == ["signal: SIGINT (console Ctrl-C)"]
    finally:
        restore()
    assert signal.getsignal(signal.SIGINT) is before


def test_restore_is_repeatable():
    before = signal.getsignal(signal.SIGINT)
    restore = exitwatch.install(lambda line: None)
    restore()
    restore()
    assert signal.getsignal(signal.SIGINT) is before


def test_ctrl_event_names_cover_the_platform_codes():
    assert exitwatch.CTRL_EVENT_NAMES == {
        0: "CTRL_C_EVENT",
        1: "CTRL_BREAK_EVENT",
        2: "CTRL_CLOSE_EVENT",
        3: "CTRL_LOGOFF_EVENT",
        4: "CTRL_SHUTDOWN_EVENT",
    }
