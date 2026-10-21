"""commandcode (``cmdc`` 1.86.3) adapter tests.

The stream fixtures are trimmed from a live ``cmdc -p --output-format json
--yolo --skip-onboarding`` run (2026-10-21): wrapped events
``{"type":"event","event":{...}}`` for run_start / message_end / run_end, then
the terminal ``{"type":"result",...}`` line.
"""

import json
from pathlib import Path

import pytest

from backseat_harness.providers import base
from backseat_harness.providers.base import SpawnSpec, agent_args
from backseat_harness.providers.claude import ClaudeProvider
from backseat_harness.providers.commandcode import CommandCodeProvider
from backseat_harness.providers.opencode import OpencodeProvider

_SESSION = "7ade29d8-4622-4fca-ad37-c4e41dc5806d"
_OTHER = "0d1c2b3a-1111-4222-8333-944455556666"


def _event(inner: dict) -> str:
    return json.dumps({"type": "event", "event": inner})


def _terminal(**fields) -> str:
    return json.dumps({"type": "result", **fields})


def _success_stream(session: str = _SESSION, text: str = "PONG") -> str:
    return "\n".join(
        [
            _event({"type": "run_start", "sessionId": session}),
            _event({"type": "turn_start", "turnNumber": 1}),
            _event({"type": "text_delta", "delta": text}),
            _event({"type": "message_end", "content": [{"type": "text", "text": text}]}),
            _event({
                "type": "run_end",
                "result": {
                    "finalText": text,
                    "stopReason": "end_turn",
                    "nextState": {"sessionId": session},
                },
            }),
            _terminal(
                subtype="success", sessionId=session, stopReason="end_turn", finalText=text
            ),
        ]
    ) + "\n"


def _spec(**overrides) -> SpawnSpec:
    return SpawnSpec(prompt="PROMPT-BODY", cwd=Path("."), **overrides)


@pytest.fixture(autouse=True)
def _fresh_agent_warnings(monkeypatch):
    monkeypatch.setattr(base, "_AGENT_WARNED", set())


# --- identity and argv -------------------------------------------------------


def test_identity_attributes():
    provider = CommandCodeProvider()
    assert provider.name == "commandcode"
    assert provider.history_provider == "command_code"
    assert provider.list_models_command == "cmdc --list-models"
    assert provider.agent_flag is None
    assert provider.drops_unpersisted_native_id is True


def test_history_provider_is_in_chat_mother_forker_defaults():
    # history.py scopes every lookup by this name; a typo silently disables sizing.
    from chat_mother_forker import default_providers

    assert CommandCodeProvider.history_provider in {p.name for p in default_providers()}


def test_build_command_first_turn_has_grant_and_no_prompt():
    argv = CommandCodeProvider().build_command(_spec())
    assert argv == [
        "cmdc", "-p", "--output-format", "json", "--yolo", "--skip-onboarding",
    ]
    assert "--trust" not in argv
    assert "PROMPT-BODY" not in argv


def test_build_command_resume_then_extra_args():
    argv = CommandCodeProvider().build_command(
        _spec(resume_native_id=_SESSION, extra_args=("--model", "m"))
    )
    assert argv[-4:] == ["--session", _SESSION, "--model", "m"]


def test_interactive_command():
    provider = CommandCodeProvider()
    assert provider.interactive_command(None) == ["cmdc"]
    assert provider.interactive_command(_SESSION) == ["cmdc", "--session", _SESSION]


# --- session id ----------------------------------------------------------------


def test_parse_result_takes_last_session_id_from_wrapped_stream():
    # run_start, run_end.nextState and the terminal result all carry the same id.
    assert CommandCodeProvider().parse_result(0, _success_stream(), "") == _SESSION


def test_parse_result_last_wins_when_the_run_forks_id():
    stream = _success_stream(session=_SESSION) + _terminal(
        subtype="success", sessionId=_OTHER, finalText="x"
    ) + "\n"
    assert CommandCodeProvider().parse_result(0, stream, "") == _OTHER


def test_parse_result_reads_run_start_alone():
    stream = _event({"type": "run_start", "sessionId": _SESSION}) + "\n"
    assert CommandCodeProvider().parse_result(-1, stream, "") == _SESSION


def test_parse_result_missing_id_returns_none():
    stream = "\n".join(
        [
            "not json at all",
            _event({"type": "run_start", "sessionId": ""}),
            _terminal(subtype="error", sessionId="", error="boom"),
        ]
    )
    assert CommandCodeProvider().parse_result(1, stream, "") is None


# --- reply text -------------------------------------------------------------------


def test_extract_text_prefers_terminal_final_text():
    assert CommandCodeProvider().extract_text(_success_stream(text="OK-9")) == "OK-9"


def test_extract_text_falls_back_to_run_end_then_message_end():
    no_result = "\n".join(
        [
            _event({"type": "message_end", "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "from message"},
            ]}),
        ]
    )
    assert CommandCodeProvider().extract_text(no_result) == "from message"

    run_end_only = _event({"type": "run_end", "result": {"finalText": "from run_end"}})
    assert CommandCodeProvider().extract_text(run_end_only) == "from run_end"


def test_extract_text_skips_thinking_blocks():
    stream = _event({"type": "message_end", "content": [
        {"type": "thinking", "thinking": "secret"},
        {"type": "text", "text": "visible"},
    ]})
    assert "secret" not in CommandCodeProvider().extract_text(stream)


def test_extract_text_final_text_wins_and_drops_thinking():
    # With a terminal finalText present, the message_end thinking block is never read.
    stream = "\n".join([
        _event({"type": "message_end", "content": [
            {"type": "thinking", "thinking": "secret"},
            {"type": "text", "text": "visible"},
        ]}),
        _terminal(subtype="success", sessionId=_SESSION, finalText="visible"),
    ])
    text = CommandCodeProvider().extract_text(stream)
    assert text == "visible"
    assert "secret" not in text


def test_extract_text_error_result_returns_error_and_keeps_session_id():
    stream = _event({"type": "run_start", "sessionId": _SESSION}) + "\n" + _terminal(
        subtype="error", sessionId=_SESSION, error="model call failed"
    )
    provider = CommandCodeProvider()
    assert provider.extract_text(stream) == "model call failed"
    assert provider.parse_result(1, stream, "") == _SESSION


def test_max_turns_result_keeps_its_text_and_id():
    # Exit code 8 is the cap; the adapter reads the stream, not the exit code.
    stream = _terminal(
        subtype="max_turns", sessionId=_SESSION, finalText="partial reply"
    ) + "\n"
    provider = CommandCodeProvider()
    assert provider.extract_text(stream) == "partial reply"
    assert provider.parse_result(8, stream, "") == _SESSION


def test_extract_text_raw_fallback_for_non_json():
    assert CommandCodeProvider().extract_text("plain text\n") == "plain text\n"


# --- rejected model ----------------------------------------------------------------


def test_rejected_model_id_from_stderr():
    stderr = 'Error: unknown model "nope/x".\nRun "cmdc --list-models" to see ids.\n'
    assert CommandCodeProvider().rejected_model_id("", stderr) == "nope/x"


def test_rejected_model_id_from_merged_log_stdout():
    merged = 'Error: unknown model "nope/x".\n'
    assert CommandCodeProvider().rejected_model_id(merged, merged) == "nope/x"


def test_rejected_model_id_none_for_a_clean_turn():
    assert CommandCodeProvider().rejected_model_id(_success_stream(), "") is None


# --- agent flag guard -------------------------------------------------------------


def test_agent_args_passes_flag_for_providers_that_have_one(capsys):
    assert agent_args(OpencodeProvider(), "a") == ("--agent", "a")
    assert agent_args(ClaudeProvider(), "a") == ("--agent", "a")
    assert capsys.readouterr().err == ""


def test_agent_args_empty_agent_is_silent(capsys):
    assert agent_args(CommandCodeProvider(), None) == ()
    assert agent_args(CommandCodeProvider(), "") == ()
    assert capsys.readouterr().err == ""


def test_agent_args_warns_once_per_provider_and_agent(capsys):
    provider = CommandCodeProvider()
    assert agent_args(provider, "a") == ()
    assert agent_args(provider, "a") == ()
    err = capsys.readouterr().err
    assert err.count("no agent flag") == 1
    assert '"a"' in err

    assert agent_args(provider, "b") == ()
    assert capsys.readouterr().err.count("no agent flag") == 1
