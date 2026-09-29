"""kiro adapter tests.

The event fixtures are trimmed from real ``kiro-cli chat --output-format
stream-json --no-interactive`` output (``kiro-cli-chat`` 2.24.1): ACP JSONL
with the session id under ``data.sessionId`` and the authoritative reply in
``runFinished.data.finalText``. Sources: the one-shot marker round-trip stream
(8 events), the same session's resume stream (8 events, same id, appended
reply), the no-trust stream trimmed to its meaningful events (runStarted,
session-bearing metadata, the write tool call, its failed permission update,
two message chunks, runFinished), and the verbatim 115-byte denial stderr.
"""

from pathlib import Path

from backseat_harness.providers.base import SpawnSpec
from backseat_harness.providers.kiro import KiroProvider

_SESSION = "c599a405-9382-4bb0-a157-9c2cc3d0cd02"
_UNTRUSTED_SESSION = "49693244-f76a-43d6-9594-149c3219bba8"

_ONESHOT = (
    '{"type":"runStarted","data":{"payloadSchema":"acp","acpProtocolVersion":1,"engine":"v2"}}'
    '\n'
    '{"type":"metadata","data":{"sessionId":"c599a405-9382-4bb0-a157-9c2cc3d0cd02","contextUsag'
    'ePercentage":3.5185000896453857,"reasoning":{"support":"unavailable","effortLevels":[]}}}'
    '\n'
    '{"type":"sessionUpdate","data":{"sessionId":"c599a405-9382-4bb0-a157-9c2cc3d0cd02","update'
    '":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"OK-9. Mar"}}}}'
    '\n'
    '{"type":"sessionUpdate","data":{"sessionId":"c599a405-9382-4bb0-a157-9c2cc3d0cd02","update'
    '":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"ker: KIROMARK"}}'
    '}}'
    '\n'
    '{"type":"sessionUpdate","data":{"sessionId":"c599a405-9382-4bb0-a157-9c2cc3d0cd02","update'
    '":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"9X42."}}}}'
    '\n'
    '{"type":"metadata","data":{"sessionId":"c599a405-9382-4bb0-a157-9c2cc3d0cd02","contextUsag'
    'ePercentage":1.6640000343322754,"reasoning":{"support":"unavailable","effortLevels":[]}}}'
    '\n'
    '{"type":"metadata","data":{"sessionId":"c599a405-9382-4bb0-a157-9c2cc3d0cd02","contextUsag'
    'ePercentage":1.6640000343322754,"meteringUsage":[{"value":0.054685097346600334,"unit":"cre'
    'dit","unitPlural":"credits"}],"turnDurationMs":2884,"reasoning":{"support":"unavailable","'
    'effortLevels":[]}}}'
    '\n'
    '{"type":"runFinished","data":{"sessionId":"c599a405-9382-4bb0-a157-9c2cc3d0cd02","status":'
    '"success","stopReason":"end_turn","finalText":"OK-9. Marker: KIROMARK9X42.","finalTextTrun'
    'cated":false}}'
)

_RESUME = (
    '{"type":"runStarted","data":{"payloadSchema":"acp","acpProtocolVersion":1,"engine":"v2"}}'
    '\n'
    '{"type":"metadata","data":{"sessionId":"c599a405-9382-4bb0-a157-9c2cc3d0cd02","contextUsag'
    'ePercentage":0.7078999876976013,"reasoning":{"support":"unavailable","effortLevels":[]}}}'
    '\n'
    '{"type":"sessionUpdate","data":{"sessionId":"c599a405-9382-4bb0-a157-9c2cc3d0cd02","update'
    '":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"K"}}}}'
    '\n'
    '{"type":"sessionUpdate","data":{"sessionId":"c599a405-9382-4bb0-a157-9c2cc3d0cd02","update'
    '":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"IROMARK9X42"}}}}'
    '\n'
    '{"type":"sessionUpdate","data":{"sessionId":"c599a405-9382-4bb0-a157-9c2cc3d0cd02","update'
    '":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"."}}}}'
    '\n'
    '{"type":"metadata","data":{"sessionId":"c599a405-9382-4bb0-a157-9c2cc3d0cd02","contextUsag'
    'ePercentage":1.678300142288208,"reasoning":{"support":"unavailable","effortLevels":[]}}}'
    '\n'
    '{"type":"metadata","data":{"sessionId":"c599a405-9382-4bb0-a157-9c2cc3d0cd02","contextUsag'
    'ePercentage":1.678300142288208,"meteringUsage":[{"value":0.054169712106135984,"unit":"cred'
    'it","unitPlural":"credits"}],"turnDurationMs":1642,"reasoning":{"support":"unavailable","e'
    'ffortLevels":[]}}}'
    '\n'
    '{"type":"runFinished","data":{"sessionId":"c599a405-9382-4bb0-a157-9c2cc3d0cd02","status":'
    '"success","stopReason":"end_turn","finalText":"KIROMARK9X42.","finalTextTruncated":false}}'
)

_UNTRUSTED = (
    '{"type":"runStarted","data":{"payloadSchema":"acp","acpProtocolVersion":1,"engine":"v2"}}'
    '\n'
    '{"type":"metadata","data":{"sessionId":"49693244-f76a-43d6-9594-149c3219bba8","contextUsag'
    'ePercentage":3.5185000896453857,"reasoning":{"support":"unavailable","effortLevels":[]}}}'
    '\n'
    '{"type":"sessionUpdate","data":{"sessionId":"49693244-f76a-43d6-9594-149c3219bba8","update'
    '":{"sessionUpdate":"tool_call","toolCallId":"toolu_bdrk_016qovUwFsAfNGhZyMnqkgWE","title":'
    '"Creating RO-PROBE.txt","kind":"edit","content":[{"type":"diff","path":"C:\\\\Users\\\\por'
    'ta\\\\AppData\\\\Local\\\\Temp\\\\opencode\\\\probe-kiro\\\\repo\\\\RO-PROBE.txt","oldText'
    '":null,"newText":"RO-OK"}],"locations":[{"path":"RO-PROBE.txt","line":1}],"rawInput":{"__t'
    'ool_use_purpose":"Create the requested file with exact content.","command":"create","path"'
    ':"RO-PROBE.txt","content":"RO-OK"},"_meta":{"kiro":{"toolName":"write"}}}}}'
    '\n'
    '{"type":"sessionUpdate","data":{"sessionId":"49693244-f76a-43d6-9594-149c3219bba8","update'
    '":{"sessionUpdate":"tool_call_update","toolCallId":"toolu_bdrk_016qovUwFsAfNGhZyMnqkgWE","'
    'kind":"edit","status":"failed","title":"write","content":[{"type":"content","content":{"ty'
    'pe":"text","text":"Permission request failed: Internal error: {\\n  \\"reason\\": \\"tool '
    'permission approval is not supported in non-interactive mode. Use --trust-all-tools to aut'
    'o-approve tool use, or drop --no-interactive.\\"\\n}"}}],"rawInput":{"__tool_use_purpose":'
    '"Create the requested file with exact content.","command":"create","path":"RO-PROBE.txt","'
    'content":"RO-OK"},"_meta":{"kiro":{"toolName":"write","toolCallFailureReason":"permission_'
    'denied"}}}}}'
    '\n'
    '{"type":"sessionUpdate","data":{"sessionId":"49693244-f76a-43d6-9594-149c3219bba8","update'
    '":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"The"}}}}'
    '\n'
    '{"type":"sessionUpdate","data":{"sessionId":"49693244-f76a-43d6-9594-149c3219bba8","update'
    '":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":" file cou"}}}}'
    '\n'
    '{"type":"runFinished","data":{"sessionId":"49693244-f76a-43d6-9594-149c3219bba8","status":'
    '"success","stopReason":"end_turn","finalText":"The file couldn\'t be created because I\'m '
    'running in non-interactive mode, which blocks the write operation that needs permission ap'
    'proval.\\n\\nTo create the file, you can either:\\n- Restart with `--trust-all-tools` to a'
    'uto-approve tool use, or\\n- Drop the `--no-interactive` flag so permission prompts can be'
    ' answered\\n\\nAlternatively, you can create it manually. On Windows PowerShell:\\n\\n```p'
    'owershell\\n\'RO-OK\' | Out-File -FilePath RO-PROBE.txt -NoNewline -Encoding ascii\\n```\\'
    'n\\nThe file should contain exactly `RO-OK` (no trailing newline).","finalTextTruncated":f'
    'alse}}'
)

_UNTRUSTED_STDERR = (
    '[denied] tool permission approval is not supported in non-interactive mode. Use --trust-al'
    'l-tools to auto-approve.\n'
)


def test_build_command_first_turn_omits_prompt():
    p = KiroProvider()
    cmd = p.build_command(SpawnSpec(prompt="do it", cwd=Path(".")))
    assert cmd[:2] == ["kiro-cli", "chat"]
    assert "--output-format" in cmd and "stream-json" in cmd
    assert "--no-interactive" in cmd
    # The prompt is piped on stdin: nothing positional, no '-' stdin marker.
    assert "do it" not in cmd
    assert "--resume-id" not in cmd
    assert "-" not in cmd


def test_build_command_resume_appends_resume_id():
    p = KiroProvider()
    cmd = p.build_command(SpawnSpec(prompt="next", cwd=Path("."), resume_native_id=_SESSION))
    assert "--resume-id" in cmd
    assert cmd[cmd.index("--resume-id") + 1] == _SESSION


def test_build_command_appends_extra_args():
    p = KiroProvider()
    cmd = p.build_command(SpawnSpec(prompt="go", cwd=Path("."), extra_args=("--mode", "default")))
    assert cmd[-2:] == ["--mode", "default"]


def test_build_command_trusts_all_tools_by_default():
    # Trust is the default on both sides; a non-interactive process cannot
    # answer a prompt, so the gate is open rather than silently denying writes.
    argv = KiroProvider().build_command(SpawnSpec(prompt="p", cwd=Path(".")))
    assert "--trust-all-tools" in argv
    assert "--no-interactive" in argv

def test_parse_result_reads_session_id():
    p = KiroProvider()
    assert p.parse_result(0, _ONESHOT, "") == _SESSION


def test_parse_result_last_wins_across_events():
    p = KiroProvider()
    stdout = (
        '{"type":"metadata","data":{"sessionId":"11111111-1111-1111-1111-111111111111"}}\n'
        '{"type":"runFinished","data":{"sessionId":"22222222-2222-2222-2222-222222222222",'
        '"status":"success","stopReason":"end_turn","finalText":"x"}}\n'
    )
    assert p.parse_result(0, stdout, "") == "22222222-2222-2222-2222-222222222222"


def test_parse_result_tolerates_noise_and_bad_lines():
    p = KiroProvider()
    stdout = (
        "Connecting...\n"
        "{not json at all\n"
        '{"type":"metadata","data":{"sessionId":"' + _SESSION + '"}}\n'
    )
    assert p.parse_result(0, stdout, "") == _SESSION


def test_parse_result_none_when_no_session():
    p = KiroProvider()
    assert p.parse_result(0, "plain text, no session", "") is None


def test_parse_result_reads_untrusted_session_and_ignores_denial_stderr():
    p = KiroProvider()
    assert p.parse_result(0, _UNTRUSTED, _UNTRUSTED_STDERR) == _UNTRUSTED_SESSION


def test_extract_text_full_stream_returns_run_finished_final_text():
    p = KiroProvider()
    assert p.extract_text(_ONESHOT) == "OK-9. Marker: KIROMARK9X42."


def test_resume_stream_parses_to_same_session_and_final_text():
    p = KiroProvider()
    assert p.parse_result(0, _RESUME, "") == _SESSION
    assert p.extract_text(_RESUME) == "KIROMARK9X42."


def test_extract_text_chunk_fallback_reassembles_the_message():
    p = KiroProvider()
    chunks_only = "\n".join(
        line for line in _ONESHOT.splitlines() if "agent_message_chunk" in line
    )
    # Fragments of one message: they join without a separator, matching the
    # finalText exactly.
    assert p.extract_text(chunks_only) == "OK-9. Marker: KIROMARK9X42."


def test_extract_text_untrusted_run_still_ends_in_final_text():
    p = KiroProvider()
    text = p.extract_text(_UNTRUSTED)
    assert text.startswith("The file couldn't be created")
    assert "`--trust-all-tools`" in text


def test_extract_text_falls_back_to_raw_output():
    p = KiroProvider()
    # No parseable events: raw output keeps a CLI error visible.
    assert p.extract_text("command failed: not authenticated") == (
        "command failed: not authenticated"
    )
def test_interactive_command_resumes_session():
    p = KiroProvider()
    assert p.interactive_command(_SESSION) == ["kiro-cli", "chat", "--resume-id", _SESSION]


def test_interactive_command_fresh_session_when_no_id():
    p = KiroProvider()
    assert p.interactive_command(None) == ["kiro-cli", "chat"]


def test_history_provider_matches_chat_mother_forker_name():
    # history.py scopes lookups by this name; a typo silently disables sizing.
    from chat_mother_forker import default_providers

    assert KiroProvider.history_provider == "kiro_cli"
    names = {p.name for p in default_providers()}
    assert KiroProvider.history_provider in names
