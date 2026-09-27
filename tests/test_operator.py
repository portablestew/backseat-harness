from backseat_harness.checklist import Checklist
from backseat_harness.models import TurnOutcome, VerdictKind
from backseat_harness.operator import ReviewInputs, build_prompt, parse_verdict, review


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


def test_prompt_opens_with_operator_meta_tag():
    """Every operator prompt starts with the UUID-free operator session tag."""
    plan = build_prompt(
        ReviewInputs(
            meta_prompt="Goal", checklist=Checklist(), managed_fork="",
            outcome=None, iteration=0, task_dir=".backseat/tasks/demo",
        )
    )
    assert plan.startswith("<meta>OPERATOR SESSION demo plan</meta>\n\n")

    review = build_prompt(
        ReviewInputs(
            meta_prompt="Goal", checklist=Checklist(), managed_fork="fork",
            outcome=_outcome(), iteration=1, task_dir=".backseat/tasks/demo",
        )
    )
    assert review.startswith("<meta>OPERATOR SESSION demo review</meta>\n\n")


def test_prompt_meta_tag_omits_unknown_slug():
    inputs = ReviewInputs(
        meta_prompt="Goal", checklist=Checklist(), managed_fork="fork",
        outcome=_outcome(), iteration=1,
    )
    assert build_prompt(inputs).startswith("<meta>OPERATOR SESSION review</meta>\n\n")


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


def test_parse_ignores_trailing_prose_with_braces():
    # A greedy {.*} match would swallow the trailing braces and fail to parse.
    text = 'Checked {the diff}.\n{"verdict":"continue","next_prompt":"x"}\nSee {notes}.'
    v = parse_verdict(text)
    assert v is not None and v.next_prompt == "x"


def test_parse_takes_last_verdict_object():
    text = '{"verdict":"continue","next_prompt":"draft"}\n{"verdict":"done"}'
    assert parse_verdict(text).kind is VerdictKind.DONE


def test_plan_prompt_for_empty_checklist_asks_for_titled_steps():
    inputs = ReviewInputs(
        meta_prompt="Goal", checklist=Checklist(), managed_fork="",
        outcome=None, iteration=0, task_dir=".backseat/tasks/demo",
    )
    prompt = build_prompt(inputs)
    assert "Your task: plan" in prompt
    assert "new task" in prompt
    assert "`title`" in prompt
    assert ".backseat/tasks/demo" in prompt
    assert "(no managed session yet)" in prompt


def test_plan_prompt_with_existing_checklist_replans():
    cl = Checklist(steps={"a": {"title": "A", "status": "active"}})
    inputs = ReviewInputs(
        meta_prompt="Goal", checklist=cl, managed_fork="fork",
        outcome=None, iteration=3,
    )
    prompt = build_prompt(inputs)
    assert "Re-assess" in prompt
    assert "before managed turn 4" in prompt


def test_verify_commands_listed_when_configured():
    inputs = ReviewInputs(
        meta_prompt="Goal", checklist=Checklist(), managed_fork="fork",
        outcome=_outcome(), iteration=1, verify_commands=["pytest -q"],
    )
    assert "`pytest -q`" in build_prompt(inputs)


def test_persona_ships_with_package():
    from backseat_harness.operator import load_persona

    persona = load_persona()
    assert "vibecoding" in persona
    assert "one small" in persona


def test_prompt_lists_handoff_docs():
    from backseat_harness.checklist import Checklist
    from backseat_harness.operator import ReviewInputs, build_prompt

    base = dict(
        meta_prompt="goal", checklist=Checklist(), managed_fork="", outcome=None,
        iteration=0, task_dir=".backseat/tasks/t",
    )
    empty = build_prompt(ReviewInputs(**base))
    assert "`.backseat/tasks/t/docs`" in empty
    assert "(none yet)" in empty
    listed = build_prompt(ReviewInputs(**base, docs=[".backseat/tasks/t/docs/1-research.md"]))
    assert "- `.backseat/tasks/t/docs/1-research.md`" in listed
