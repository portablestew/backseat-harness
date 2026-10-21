"""opencode adapter tests.

The event fixtures below are trimmed from real ``opencode run --format json``
output (opencode 1.18.32), which is why the field names look the way they do.
"""

from pathlib import Path

import pytest

from backseat_harness.providers import available_providers, binary_for, get_provider
from backseat_harness.providers.base import ProviderError, SpawnSpec
from backseat_harness.providers.claude import ClaudeProvider
from backseat_harness.providers.kiro import KiroProvider
from backseat_harness.providers.opencode import OpencodeProvider

_SESSION = "ses_f2912cb3bffew2JrdmxTShIbSX"

_STREAM = (
    f'{{"type":"step_start","timestamp":1,"sessionID":"{_SESSION}",'
    '"part":{"id":"prt_a","type":"step-start"}}\n'
    f'{{"type":"text","timestamp":2,"sessionID":"{_SESSION}",'
    '"part":{"id":"prt_b","type":"text","text":"OK"}}\n'
    f'{{"type":"step_finish","timestamp":3,"sessionID":"{_SESSION}",'
    '"part":{"id":"prt_c","type":"step-finish"}}\n'
)


def test_build_command_first_turn_omits_prompt():
    p = OpencodeProvider()
    cmd = p.build_command(SpawnSpec(prompt="do it", cwd=Path(".")))
    assert cmd[:2] == ["opencode", "run"]
    assert "--format" in cmd and "json" in cmd
    # The prompt is piped on stdin, so it must never appear in argv.
    assert "do it" not in cmd
    assert "--session" not in cmd


def test_build_command_resumes_with_session_flag():
    p = OpencodeProvider()
    cmd = p.build_command(
        SpawnSpec(prompt="next", cwd=Path("."), resume_native_id=_SESSION)
    )
    # opencode resumes with --session, not --resume.
    assert "--session" in cmd
    assert cmd[cmd.index("--session") + 1] == _SESSION
    assert "--resume" not in cmd


def test_build_command_appends_extra_args():
    p = OpencodeProvider()
    cmd = p.build_command(
        SpawnSpec(prompt="go", cwd=Path("."), extra_args=("--agent", "builder"))
    )
    assert cmd[-2:] == ["--agent", "builder"]


def test_parse_result_reads_session_id():
    p = OpencodeProvider()
    assert p.parse_result(0, _STREAM, "") == _SESSION


def test_parse_result_tolerates_noise_and_falls_back_to_scan():
    p = OpencodeProvider()
    # Log noise plus a truncated final line: no complete event survives, so the
    # ses_ scan is what recovers the id.
    stdout = "INFO starting\n{not json at all\n  ses_abc123 \n"
    assert p.parse_result(1, stdout, "") == "ses_abc123"


def test_parse_result_none_when_no_session():
    p = OpencodeProvider()
    assert p.parse_result(0, "plain text, no session", "") is None


def test_extract_text_returns_assistant_text_only():
    p = OpencodeProvider()
    assert p.extract_text(_STREAM) == "OK"


def test_extract_text_keeps_last_value_per_part_in_order():
    p = OpencodeProvider()
    # A streamed part is re-emitted as it grows; the final value must win, and
    # distinct parts must stay in first-seen order.
    stdout = (
        '{"type":"text","part":{"id":"prt_b","type":"text","text":"par"}}\n'
        '{"type":"text","part":{"id":"prt_b","type":"text","text":"partial"}}\n'
        '{"type":"text","part":{"id":"prt_c","type":"text","text":"second"}}\n'
    )
    assert p.extract_text(stdout) == "partial\nsecond"


def test_extract_text_falls_back_to_raw_output():
    p = OpencodeProvider()
    # No parseable events: return raw output so a CLI error stays visible.
    assert p.extract_text("command failed: not authenticated") == (
        "command failed: not authenticated"
    )


def test_registry_serves_all_three_providers():
    # opencode, claude, and kiro are all registered and live-validated;
    # nothing is deferred, so an unknown name is simply unknown.
    assert isinstance(get_provider("opencode"), OpencodeProvider)
    assert isinstance(get_provider("claude"), ClaudeProvider)
    assert isinstance(get_provider("kiro"), KiroProvider)
    assert available_providers() == ["claude", "commandcode", "kiro", "opencode"]
    assert binary_for("commandcode") == "cmdc"
    with pytest.raises(ProviderError, match="unknown provider"):
        get_provider("nope")


def test_history_provider_matches_chat_mother_forker_name():
    # history.py scopes lookups by this name; a typo silently disables sizing.
    from chat_mother_forker import default_providers

    names = {p.name for p in default_providers()}
    assert OpencodeProvider.history_provider in names
@pytest.mark.parametrize(
    "session, argv",
    [
        (_SESSION, ["opencode", "--session", _SESSION]),
        (None, ["opencode"]),  # no id: a fresh session
    ],
    ids=["resumes-session", "fresh-session"],
)
def test_interactive_command_resumes_session_or_starts_fresh(session, argv):
    p = OpencodeProvider()
    assert p.interactive_command(session) == argv


def test_every_provider_defaults_to_auto_approve():
    """No provider may leave a non-interactive process able to prompt or silently deny.

    build_command feeds the managed turn and the operator alike, so a missing
    grant is a 1h block on claude/opencode and a silent write-denial on kiro.
    """
    from backseat_harness.providers import available_providers, get_provider

    grants = {
        "opencode": "--auto",
        "claude": "--permission-mode",
        "commandcode": "--yolo",
        "kiro": "--trust-all-tools",
    }
    for name in available_providers():
        argv = get_provider(name).build_command(
            SpawnSpec(prompt="p", cwd=Path("."), resume_native_id="s1")
        )
        assert grants[name] in argv, f"{name} build_command lacks {grants[name]}"
