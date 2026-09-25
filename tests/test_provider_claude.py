"""Claude adapter tests.

Claude is not a registered v0.1 provider (opencode is), but the adapter is kept
contract-conformant for v0.2, so these tests guard it against ABC drift.
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


def test_build_command_resume():
    p = ClaudeProvider()
    cmd = p.build_command(SpawnSpec(prompt="next", cwd=Path("."), resume_native_id="abc-123"))
    assert "--resume" in cmd
    assert cmd[cmd.index("--resume") + 1] == "abc-123"


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


def test_extract_text_falls_back_to_assistant_blocks():
    p = ClaudeProvider()
    stdout = (
        '{"type":"assistant","message":{"role":"assistant",'
        '"content":[{"type":"text","text":"part one"},{"type":"tool_use","name":"x"}]}}\n'
        '{"type":"assistant","message":{"role":"assistant","content":"part two"}}\n'
    )
    assert p.extract_text(stdout) == "part one\npart two"
