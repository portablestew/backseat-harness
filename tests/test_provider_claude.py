"""Claude adapter tests.

Claude is a registered, live-validated provider (``providers/__init__.py``),
so these tests guard the adapter against ABC drift.

Provenance: the operator-recipe assertions below are transcribed verbatim from
the read-only recipe validated live against ``claude`` 2.1.283 (deny-by-default
validation runs; the version stamp lives here so traceability does not depend
on external notes). The one-shot parse/extract strings are synthetic contract
guards, not captures.
"""

from pathlib import Path

from backseat_harness.providers.base import SpawnSpec
from backseat_harness.providers.claude import ClaudeProvider


def test_build_command_first_turn_omits_prompt():
    p = ClaudeProvider()
    cmd = p.build_command(SpawnSpec(prompt="do it", cwd=Path(".")))
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "--output-format" in cmd
    # The prompt is piped on stdin, so -p takes no argument here.
    assert "do it" not in cmd
    assert "--resume" not in cmd


def test_build_command_appends_extra_args():
    """Where claude's permission mode reaches the managed turn."""
    spec = SpawnSpec(prompt="p", cwd=Path("."), extra_args=("--permission-mode", "auto"))
    argv = ClaudeProvider().build_command(spec)
    assert argv[-2:] == ["--permission-mode", "auto"]



def test_parse_result_reads_session_id():
    p = ClaudeProvider()
    stdout = (
        '{"type":"system","session_id":"early"}\n'
        "not json\n"
        '{"type":"result","is_error":false,"session_id":"final-id"}\n'
    )
    assert p.parse_result(0, stdout, "") == "final-id"


def test_parse_result_no_session():
    p = ClaudeProvider()
    assert p.parse_result(0, "plain text output", "") is None


def test_extract_text_prefers_terminal_result():
    p = ClaudeProvider()
    stdout = (
        '{"type":"assistant","message":{"role":"assistant",'
        '"content":[{"type":"text","text":"thinking out loud"}]}}\n'
        '{"type":"result","result":"final answer","session_id":"s1"}\n'
    )
    assert p.extract_text(stdout) == "final answer"


def test_extract_text_prefers_last_result_over_an_earlier_waiting_turn():
    # A multi-turn headless run (a wakeup wait, a subagent hand-back) emits
    # several result events; the earlier ones are status lines, and the final
    # one is the answer. Taking the first parked a real generation whose
    # envelope arrived on a later turn (see manager.py's envelope contract).
    p = ClaudeProvider()
    stdout = (
        '{"type":"result","result_index":0,'
        '"result":"Review is pending; waiting for the reviewer.",'
        '"session_id":"s1"}\n'
        '{"type":"result","result_index":1,'
        '"result":"{\\"v\\": 1, \\"phase\\": \\"generate\\", \\"approved\\": true}",'
        '"session_id":"s1"}\n'
    )
    assert p.extract_text(stdout) == '{"v": 1, "phase": "generate", "approved": true}'


def test_extract_text_empty_result_falls_back_to_assistant_blocks():
    p = ClaudeProvider()
    stdout = (
        '{"type":"assistant","message":{"role":"assistant",'
        '"content":[{"type":"text","text":"draft"}]}}\n'
        '{"type":"result","result":"","session_id":"s1"}\n'
    )
    # An empty result string is not terminal: the assistant blocks win,
    # matching kiro's finalText handling.
    assert p.extract_text(stdout) == "draft"


def test_interactive_command_resumes_session():
    p = ClaudeProvider()
    assert p.interactive_command("abc-123") == ["claude", "--resume", "abc-123"]


def test_interactive_command_fresh_session_when_no_id():
    p = ClaudeProvider()
    assert p.interactive_command(None) == ["claude"]


def test_history_provider_matches_chat_mother_forker_name():
    # history.py scopes lookups by this name; a typo silently disables sizing.
    from chat_mother_forker import default_providers

    assert ClaudeProvider.history_provider == "claude_code"
    names = {p.name for p in default_providers()}
    assert ClaudeProvider.history_provider in names
