from backseat_harness.checklist import Checklist
from backseat_harness.models import TurnOutcome, VerdictKind
from backseat_harness.system_agent import ReviewInputs, build_prompt, parse_verdict, review


def _outcome(exit_code=0, timed_out=False):
    return TurnOutcome(exit_code=exit_code, timed_out=timed_out, duration_s=1.0)


def test_parse_plain_json():
    v = parse_verdict('{"v":1,"verdict":"continue","next_prompt":"go"}')
    assert v is not None
    assert v.kind is VerdictKind.CONTINUE
    assert v.next_prompt == "go"


def test_parse_json_embedded_in_prose():
    text = 'Here is my assessment.\n{"verdict":"done","notes":"finished"}\nThanks!'
    v = parse_verdict(text)
    assert v.kind is VerdictKind.DONE


def test_parse_unknown_verdict_returns_none():
    assert parse_verdict('{"verdict":"explode"}') is None


def test_parse_garbage_returns_none():
    assert parse_verdict("no json at all") is None


def test_build_prompt_includes_problem_hint():
    inputs = ReviewInputs(
        meta_prompt="Goal", checklist=Checklist(), managed_fork="fork",
        outcome=_outcome(exit_code=1), iteration=2,
    )
    prompt = build_prompt(inputs)
    assert "abnormally" in prompt
    assert "exit_code=1" in prompt


def test_build_prompt_no_hint_on_clean_exit():
    inputs = ReviewInputs(
        meta_prompt="Goal", checklist=Checklist(), managed_fork="fork",
        outcome=_outcome(), iteration=1,
    )
    assert "abnormally" not in build_prompt(inputs)


def test_review_reasks_once_on_bad_output():
    calls = []

    def invoke(prompt: str) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            return "sorry no json"
        return '{"verdict":"continue","next_prompt":"retry"}'

    inputs = ReviewInputs(
        meta_prompt="Goal", checklist=Checklist(), managed_fork="fork",
        outcome=_outcome(), iteration=1,
    )
    v = review(inputs, invoke)
    assert len(calls) == 2
    assert v.kind is VerdictKind.CONTINUE
