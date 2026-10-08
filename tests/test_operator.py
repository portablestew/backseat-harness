from backseat_harness.checklist import Checklist
from backseat_harness.models import TurnOutcome, VerdictKind
from backseat_harness.operator import ReviewInputs, build_prompt, parse_verdict


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
            goal_prompt="Goal", checklist=Checklist(), managed_fork="",
            outcome=None, iteration=0, task_dir=".backseat/tasks/demo",
        )
    )
    assert plan.startswith("<meta>OPERATOR SESSION demo plan</meta>\n\n")

    review = build_prompt(
        ReviewInputs(
            goal_prompt="Goal", checklist=Checklist(), managed_fork="fork",
            outcome=_outcome(), iteration=1, task_dir=".backseat/tasks/demo",
        )
    )
    assert review.startswith("<meta>OPERATOR SESSION demo review</meta>\n\n")


def test_prompt_meta_tag_omits_unknown_slug():
    inputs = ReviewInputs(
        goal_prompt="Goal", checklist=Checklist(), managed_fork="fork",
        outcome=_outcome(), iteration=1,
    )
    assert build_prompt(inputs).startswith("<meta>OPERATOR SESSION review</meta>\n\n")


def test_build_prompt_includes_problem_hint():
    inputs = ReviewInputs(
        goal_prompt="Goal", checklist=Checklist(), managed_fork="fork",
        outcome=_outcome(exit_code=1), iteration=2,
    )
    prompt = build_prompt(inputs)
    assert "abnormally" in prompt
    assert "exit_code=1" in prompt


def test_build_prompt_no_hint_on_clean_exit():
    inputs = ReviewInputs(
        goal_prompt="Goal", checklist=Checklist(), managed_fork="fork",
        outcome=_outcome(), iteration=1,
    )
    assert "abnormally" not in build_prompt(inputs)


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
        goal_prompt="Goal", checklist=Checklist(), managed_fork="",
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
        goal_prompt="Goal", checklist=cl, managed_fork="fork",
        outcome=None, iteration=3,
    )
    prompt = build_prompt(inputs)
    assert "Re-assess" in prompt
    assert "before managed turn 4" in prompt


def test_verify_commands_listed_when_configured():
    inputs = ReviewInputs(
        goal_prompt="Goal", checklist=Checklist(), managed_fork="fork",
        outcome=_outcome(), iteration=1, verify_commands=["pytest -q"],
    )
    assert "`pytest -q`" in build_prompt(inputs)


# --- Model slots section + slot warning (docs 2-plan §4) ---


def test_model_slots_rendered_with_default_binding_bullet():
    inputs = ReviewInputs(
        goal_prompt="Goal", checklist=Checklist(), managed_fork="fork",
        outcome=_outcome(), iteration=1,
        model_slots=("default", "thinker"),
    )
    prompt = build_prompt(inputs)
    assert "## Model slots" in prompt
    assert "- `default`" in prompt
    assert "- `thinker`" in prompt
    # 'default' renders once: the binding bullet, not duplicated from the keys.
    assert prompt.count("- `default`") == 1
    assert 'omit `model` for the default' in prompt


def test_model_slots_configured_key_skips_default_duplicate():
    inputs = ReviewInputs(
        goal_prompt="Goal", checklist=Checklist(), managed_fork="fork",
        outcome=_outcome(), iteration=1,
        model_slots=("default", "fast", "thinker"),
    )
    prompt = build_prompt(inputs)
    assert "- `fast`" in prompt and "- `thinker`" in prompt


def test_model_slots_empty_renders_single_line():
    inputs = ReviewInputs(
        goal_prompt="Goal", checklist=Checklist(), managed_fork="fork",
        outcome=_outcome(), iteration=1,
    )
    prompt = build_prompt(inputs)
    assert "## Model slots" in prompt  # the section itself always renders
    assert 'No model slots are configured; steps must not set "model".' in prompt
    assert "- `default`" not in prompt


def test_slot_warning_reaches_the_prompt_verbatim():
    warning = (
        "step 's1' names model slot 'ghost', which no [models] config "
        "defines; running the default model instead"
    )
    inputs = ReviewInputs(
        goal_prompt="Goal", checklist=Checklist(), managed_fork="fork",
        outcome=_outcome(), iteration=1,
        model_slots=("fast",), slot_warning=warning,
    )
    prompt = build_prompt(inputs)
    assert "Slot warning:" in prompt
    assert warning in prompt


def test_response_format_documents_the_model_field():
    inputs = ReviewInputs(
        goal_prompt="Goal", checklist=Checklist(), managed_fork="fork",
        outcome=_outcome(), iteration=1,
    )
    prompt = build_prompt(inputs)
    assert '"model": "optional slot name from the Model slots section' in prompt


def test_persona_ships_with_package():
    from backseat_harness.operator import load_persona

    persona = load_persona()
    assert "vibecoding" in persona
    assert "One small step per turn" in persona
    assert "Name the class, not the instances" in persona


def test_prompt_lists_handoff_docs():
    from backseat_harness.checklist import Checklist
    from backseat_harness.operator import ReviewInputs, build_prompt

    base = dict(
        goal_prompt="goal", checklist=Checklist(), managed_fork="", outcome=None,
        iteration=0, task_dir=".backseat/tasks/t",
    )
    empty = build_prompt(ReviewInputs(**base))
    assert "`.backseat/tasks/t/docs`" in empty
    assert "(none yet)" in empty
    listed = build_prompt(ReviewInputs(**base, docs=[".backseat/tasks/t/docs/1-research.md"]))
    assert "- `.backseat/tasks/t/docs/1-research.md`" in listed


def test_checklist_fields_are_clipped_with_a_marker():
    """Long evidence is cut, and the prompt says how much and where to look."""
    from backseat_harness.checklist import Checklist
    from backseat_harness.operator import CHECKLIST_FIELD_LIMIT, render_checklist

    limit = CHECKLIST_FIELD_LIMIT
    cl = Checklist(
        steps={"1-research": {"status": "active", "evidence": "E" * (limit + 500)}},
        remarks=[],
    )
    out = render_checklist(cl, ".backseat/tasks/t")
    assert "E" * limit in out
    assert "..[+500 chars]" in out
    assert "E" * (limit + 1) not in out


def test_done_steps_inline_as_a_map_entry_and_drop_their_evidence():
    """Closed work stays visible as a plan entry without its verified bytes."""
    from backseat_harness.checklist import Checklist
    from backseat_harness.operator import render_checklist

    cl = Checklist(
        steps={
            "1-research": {
                "title": "Research the flag",
                "status": "done",
                "evidence": "verified at 48a0da1, 134 passed",
                "notes": "close-out detail nobody needs again",
            }
        },
        remarks=[],
    )
    out = render_checklist(cl, ".backseat/tasks/t")
    assert "Research the flag" in out
    assert '"status": "done"' in out
    assert "134 passed" not in out
    assert "close-out detail" not in out


def test_non_done_steps_keep_every_field():
    from backseat_harness.checklist import Checklist
    from backseat_harness.operator import render_checklist

    cl = Checklist(
        steps={"11": {"title": "T", "status": "blocked", "notes": "n", "evidence": "e"}},
        remarks=[],
    )
    out = render_checklist(cl, ".backseat/tasks/t")
    assert all(v in out for v in ("T", "blocked", "n", "e"))


def test_short_checklist_fields_are_left_alone():
    from backseat_harness.checklist import Checklist
    from backseat_harness.operator import render_checklist

    cl = Checklist(steps={"1": {"evidence": "verified at 48a0da1, 134 passed"}}, remarks=[])
    out = render_checklist(cl, ".backseat/tasks/t")
    assert "..[" not in out
    assert "verified at 48a0da1, 134 passed" in out


def test_only_recent_remarks_are_inlined_and_older_ones_point_at_the_file():
    from backseat_harness.checklist import Checklist
    from backseat_harness.operator import CHECKLIST_RECENT_REMARKS, render_checklist

    total = CHECKLIST_RECENT_REMARKS + 10
    remarks = [f"remark-{i}" for i in range(total)]
    out = render_checklist(Checklist(steps={}, remarks=remarks), ".backseat/tasks/t")
    # The most recent CHECKLIST_RECENT_REMARKS survive; the rest are counted off.
    assert f"remark-{total - 1}" in out
    assert f"remark-{total - CHECKLIST_RECENT_REMARKS}" in out
    assert "remark-9" not in out
    assert "..[10 earlier remarks omitted" in out
    assert "checklist.json" in out


def test_prompt_renders_the_owner_brief_with_precedence():
    """A brief is the human speaking now, and the prompt says so out loud."""
    inputs = ReviewInputs(
        goal_prompt="Goal", checklist=Checklist(), managed_fork="",
        outcome=None, iteration=0, task_dir=".backseat/tasks/t",
        owner_brief="Keep the shared budget.",
    )
    out = build_prompt(inputs)
    assert "## Brief from the task owner" in out
    assert "Keep the shared budget." in out
    assert "it wins" in out
    # The plan instruction tells the operator to act on it, not just read it.
    assert "brief above is new input from this run" in out


def test_review_prompt_renders_the_brief_still_in_force():
    """Whole-run steering: a review call reads the brief too."""
    inputs = ReviewInputs(
        goal_prompt="Goal", checklist=Checklist(), managed_fork="fork",
        outcome=_outcome(), iteration=1, task_dir=".backseat/tasks/t",
        owner_brief="Keep the shared budget.",
    )
    out = build_prompt(inputs)
    assert "## Brief from the task owner" in out
    assert "Keep the shared budget." in out


def test_prompt_pairs_the_question_with_the_answer():
    """The operator has no memory of asking, so the question rides the prompt."""
    inputs = ReviewInputs(
        goal_prompt="Goal", checklist=Checklist(), managed_fork="",
        outcome=None, iteration=45, task_dir=".backseat/tasks/t",
        owner_brief="Keep the shared budget.",
        pending_question="D1: split the retry budget, or keep it shared?",
    )
    out = build_prompt(inputs)
    assert "## Your question, and the owner's answer" in out
    assert "### Your question" in out
    assert "D1: split the retry budget, or keep it shared?" in out
    assert "### Their answer" in out
    assert "Keep the shared budget." in out
    assert "close the step you left blocked" in out
    # The question precedes the answer, so the answer is legible as an answer.
    assert out.index("D1: split") < out.index("Keep the shared budget.")


def test_prompt_omits_the_note_section_when_there_is_no_note():
    inputs = ReviewInputs(
        goal_prompt="Goal", checklist=Checklist(), managed_fork="",
        outcome=None, iteration=0, task_dir=".backseat/tasks/t",
    )
    out = build_prompt(inputs)
    assert "Brief from the task owner" not in out
    assert "the owner's answer" not in out


def test_prompt_never_clips_the_question_or_the_note():
    """The question is 1,129 chars in practice; clipping it defeats the point."""
    long_question = "Which model id? " * 200
    long_brief = "Use anthropic. " * 200
    inputs = ReviewInputs(
        goal_prompt="Goal", checklist=Checklist(), managed_fork="",
        outcome=None, iteration=45, task_dir=".backseat/tasks/t",
        owner_brief=long_brief, pending_question=long_question,
    )
    out = build_prompt(inputs)
    assert long_question.strip() in out
    assert long_brief.strip() in out
