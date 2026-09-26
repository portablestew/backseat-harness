"""opencode adapter tests.

The event fixtures below are trimmed from real ``opencode run --format json``
output (opencode 1.18.32), which is why the field names look the way they do.
"""

from pathlib import Path

from backseat_harness.providers import get_provider
from backseat_harness.providers.base import ProviderError, SpawnSpec
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


def test_registry_serves_opencode_and_defers_claude():
    assert isinstance(get_provider("opencode"), OpencodeProvider)
    try:
        get_provider("claude")
    except ProviderError as exc:
        assert "not supported yet" in str(exc)
    else:  # pragma: no cover - registry regression
        raise AssertionError("claude should not be a registered v0.1 provider")


def test_history_provider_matches_chat_mother_forker_name():
    # history.py scopes lookups by this name; a typo silently disables sizing.
    from chat_mother_forker import default_providers

    names = {p.name for p in default_providers()}
    assert OpencodeProvider.history_provider in names


def test_operator_permissions_deny_by_default_and_never_ask():
    from backseat_harness.providers.opencode import operator_permissions

    perms = operator_permissions(["pytest -q"])
    assert perms["*"] == "deny"
    assert perms["bash"]["*"] == "deny"
    assert perms["bash"]["git diff*"] == "allow"
    assert perms["bash"]["pytest -q"] == "allow"
    # Last match wins in opencode, so the --output deny must come after the allows.
    assert list(perms["bash"])[-1] == "*--output*"
    assert perms["read"]["*binding.json"] == "deny"

    def actions(value):
        if isinstance(value, dict):
            for v in value.values():
                yield from actions(v)
        else:
            yield value

    # Nobody answers prompts in a non-interactive run.
    assert "ask" not in set(actions(perms))


def test_prepare_operator_writes_agent_and_returns_launch(tmp_path):
    import json

    from backseat_harness.providers.opencode import OPERATOR_AGENT

    p = OpencodeProvider()
    launch = p.prepare_operator(tmp_path / "prof", "You are the operator.", [])
    agent_file = tmp_path / "prof" / "agents" / f"{OPERATOR_AGENT}.md"
    text = agent_file.read_text(encoding="utf-8")

    assert launch.env == {"OPENCODE_CONFIG_DIR": str(tmp_path / "prof")}
    assert launch.extra_args == ("--agent", OPERATOR_AGENT)
    # Frontmatter values are JSON (valid YAML flow syntax).
    front, _, body = text.removeprefix("---\n").partition("\n---\n")
    fields = dict(line.split(": ", 1) for line in front.splitlines())
    assert json.loads(fields["mode"]) == "primary"
    assert json.loads(fields["permission"])["*"] == "deny"
    assert body.strip() == "You are the operator."

    # Rewritten (not appended) on every startup.
    p.prepare_operator(tmp_path / "prof", "Second persona.", [])
    assert "Second persona." in agent_file.read_text(encoding="utf-8")
    assert "You are the operator." not in agent_file.read_text(encoding="utf-8")


def test_interactive_command_resumes_session():
    p = OpencodeProvider()
    assert p.interactive_command(_SESSION) == ["opencode", "--session", _SESSION]


def test_interactive_command_fresh_session_when_no_id():
    p = OpencodeProvider()
    assert p.interactive_command(None) == ["opencode"]
