"""The adversarial operator: fresh context each turn, verdict on stdout.

The operator is a one-shot invocation of the same CLI tool under a read-only
profile. Responsibilities are split in two:

- **Persona** (``operator_persona.md``): who the operator is and how it works --
  the vibecoding-operator stance, review procedure, strategy, prompt-writing
  and escalation rules. Standing guidance, identical every call. No provider
  carries it in a profile or agent file; the invoker prepends the text to every
  operator prompt, so there is one source of truth and a workspace's own
  ``[operator].agent`` receives it too (see ``cli._make_operator_invoker``).
- **Prompt** (this module): the per-call data and the response contract -- the
  goal, the checklist, the redacted session fork, the turn outcome, and the
  exact JSON envelope to return.

There are two phases. **Plan** runs when there is no prompt queued for the
managed agent (a fresh task, or a resumed one): the operator plans or re-plans
the checklist and writes the first prompt. **Review** runs after every managed
turn. The harness parses the verdict and is the only writer to disk.

The actual invocation of the operator CLI is injected (``invoke``) so the loop
and tests can supply a real subprocess runner or a fake.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources

from backseat_harness import marker as marker_mod
from backseat_harness.checklist import Checklist
from backseat_harness.models import TurnOutcome, Verdict

# A callable that takes the fully-assembled operator prompt and returns its
# final textual message. Injected by the loop (real CLI) or tests (fake).
InvokeFn = Callable[[str], str]


def load_persona() -> str:
    """Return the operator persona shipped with the package."""
    return (
        resources.files("backseat_harness")
        .joinpath("operator_persona.md")
        .read_text(encoding="utf-8")
    )


@dataclass(frozen=True)
class ReviewInputs:
    meta_prompt: str
    checklist: Checklist
    #: Transcript of the managed session, with the task's marker redacted.
    managed_fork: str
    #: None in the plan phase (no managed turn to review).
    outcome: TurnOutcome | None
    iteration: int
    #: Workspace-relative task directory, e.g. ``.backseat/tasks/hello``.
    task_dir: str = ""
    verify_commands: list[str] = field(default_factory=list)
    #: Configured ``[models]`` slot names the operator may set on a step's
    #: ``model`` field. Raw vocabulary from config; the render adds the
    #: ``default`` no-field binding bullet itself.
    model_slots: tuple[str, ...] = ()
    #: Per-turn slot-resolution warning (on-disk bad slot, non-string model,
    #: kiro limitation), surfaced verbatim so the operator can correct the
    #: step. None on a clean turn.
    slot_warning: str | None = None
    #: Workspace-relative paths of the handoff docs present right now.
    docs: list[str] = field(default_factory=list)
    #: Workspace-relative path to the full session transcript capture, or None
    #: (nothing captured yet -- a fresh task, so no session has ever run -- or
    #: capture unavailable). The untruncated record of the managed session so
    #: far, overwritten each turn and refreshed before a plan-phase call.
    transcript_file: str | None = None

    @property
    def is_planning(self) -> bool:
        return self.outcome is None


#: Per-field character cap for the inlined checklist. Fields are re-read on every
#: call, so an unbounded ``evidence``/``remarks`` makes the prompt grow with the
#: task until it crowds out the turn's actual evidence. The full text stays in
#: ``checklist.json`` on disk; the operator is told to grep it for more.
CHECKLIST_FIELD_LIMIT = 250

#: How many of the most recent ``remarks`` survive inline. Older ones are already
#: superseded by step evidence or the handoff docs.
CHECKLIST_RECENT_REMARKS = 5


def _clip(text: str, limit: int = CHECKLIST_FIELD_LIMIT) -> str:
    """Truncate a checklist field, leaving a marker of how much was cut."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f" ..[+{len(text) - limit} chars]"


def render_checklist(checklist: Checklist, task_dir: str) -> str:
    """Inline the checklist for the prompt: clipped fields, recent remarks only.

    The on-disk file is the full record; this is the working view. ``..[+N chars]``
    markers point at the file rather than naming it inline on every field, so the
    prompt carries one pointer instead of one per truncated string.
    """
    steps = {}
    for step_id, step in checklist.steps.items():
        steps[step_id] = {
            key: (_clip(value) if isinstance(value, str) else value)
            for key, value in step.items()
        }
    remarks = [_clip(r) for r in checklist.remarks[-CHECKLIST_RECENT_REMARKS:]]
    hidden = len(checklist.remarks) - len(remarks)
    if hidden:
        remarks.insert(0, f"..[{hidden} earlier remarks omitted; grep {task_dir}/checklist.json]")
    data = {"steps": steps, "remarks": remarks}
    return json.dumps(data, indent=2, ensure_ascii=False)


def build_prompt(inputs: ReviewInputs) -> str:
    """Assemble the per-call operator prompt.

    Opens with the UUID-free ``<meta>OPERATOR SESSION ...</meta>`` tag so the
    operator's own stored session is identifiable (and filterable) without ever
    matching the task's root-UUID marker search. The managed fork is injected as
    historical reference; the instruction and response contract come last so the
    model weights them most.
    """
    checklist_json = render_checklist(inputs.checklist, inputs.task_dir)
    fork = inputs.managed_fork.strip() or "(no managed session yet)"
    if inputs.is_planning:
        task = _plan_task(inputs)
    else:
        task = _review_task(inputs)
    slug = inputs.task_dir.rsplit("/", 1)[-1] if inputs.task_dir else ""
    phase = "plan" if inputs.is_planning else "review"
    head = marker_mod.format_operator_marker(slug=slug, phase=phase)
    verify = ""
    if inputs.verify_commands:
        cmds = "\n".join(f"- `{c}`" for c in inputs.verify_commands)
        verify = f"\n## Verification commands you may run yourself\n{cmds}\n"
    models = _model_slots_section(inputs)
    task_dir = inputs.task_dir or "(unknown)"
    if inputs.docs:
        docs = "\n".join(f"- `{d}`" for d in inputs.docs)
    else:
        docs = "(none yet)"
    if inputs.transcript_file:
        transcript = (
            f"\n## Full session transcript (`{inputs.transcript_file}`)\n"
            "A fallback reference, not required reading. The complete, untruncated "
            "record of the managed session so far -- including full tool-call "
            "results, which the fork above elides. It can be very large, so do not "
            "read it end-to-end: when the fork is too compressed to judge what "
            "actually happened, search it or read only the relevant part.\n"
        )
    else:
        transcript = ""
    return f"{head}\n\n" + _TEMPLATE.format(
        meta_prompt=inputs.meta_prompt.strip(),
        task_dir=task_dir,
        docs_dir=f"{task_dir}/docs",
        docs=docs,
        checklist=checklist_json,
        models=models,
        fork=fork,
        transcript=transcript,
        verify=verify,
        task=task,
    )


def _model_slots_section(inputs: ReviewInputs) -> str:
    """Render the ``## Model slots`` section: vocabulary + warnings.

    Mirrors the verify block. ``default`` is always bulleted (the no-field
    binding exists whether or not the config named it: it falls through to
    ``[harness].model``); the configured keys — ``default`` omitted, it would
    duplicate — follow. With no slots at all the section is one line telling
    the operator never to set the field. ``slot_warning`` rides at the end,
    verbatim.
    """
    if inputs.model_slots:
        names = "\n".join(f"- `{s}`" for s in inputs.model_slots if s != "default")
        section = (
            "\n## Model slots\n"
            "A step may set `model` (or `default`) to one of these slots to run "
            "that step on a different model; omit `model` for the default "
            "model.\n"
            "- `default`\n"
            f"{names}\n"
        )
    else:
        section = (
            "\n## Model slots\n"
            'No model slots are configured; steps must not set "model".\n'
        )
    if inputs.slot_warning:
        section += f"\nSlot warning: {inputs.slot_warning}\n"
    return section


def _plan_task(inputs: ReviewInputs) -> str:
    if inputs.checklist.steps:
        situation = (
            "The loop is (re)starting with no prompt queued for the managed agent. "
            "Re-assess where things stand from the checklist and the session so "
            "far, correct the checklist if it has drifted from reality, and decide "
            "the next step."
        )
    else:
        situation = (
            "This is a new task: no managed turn has run yet and the checklist is "
            "empty. Plan it. Break the goal into milestones and small, verifiable "
            "steps (research; plan, including a test and validation plan, not "
            "just an implementation plan; implement; test; verification and "
            "approval), scaled to the task, and add them to the checklist, each "
            "with a short `title`. Then write the first prompt, which should "
            "almost always be a research step, not an edit."
        )
    return (
        f"## Your task: plan (before managed turn {inputs.iteration + 1})\n"
        f"{situation}\n"
        "If the goal is too ambiguous to plan responsibly, return `ask_human` "
        "with the specific questions."
    )


def _review_task(inputs: ReviewInputs) -> str:
    outcome = inputs.outcome
    assert outcome is not None
    hint = ""
    if outcome.had_problem:
        hint = (
            "\nNOTE: the managed turn ended abnormally "
            f"(exit_code={outcome.exit_code}, timed_out={outcome.timed_out}). "
            "Consider whether to retry, adjust, or flag it.\n"
        )
    return (
        f"## Your task: review managed turn {inputs.iteration}\n"
        "Review the turn that just ended, skeptically, starting from what "
        "actually changed (`git status`, `git diff`) rather than what the agent "
        "says. Update the checklist to match verified reality, then decide the "
        "single next step.\n"
        f"{hint}"
    )


def parse_verdict(text: str) -> Verdict | None:
    """Parse the verdict envelope from the operator's final message.

    Tolerates prose and stray braces around the envelope: every ``{`` is tried
    as the start of a JSON object, and the last object that is a valid verdict
    wins (the envelope is requested last). Returns None if none is found; the
    loop treats that as a 'problem' after one re-ask.
    """
    decoder = json.JSONDecoder()
    found: Verdict | None = None
    start = text.find("{")
    while start != -1:
        try:
            data, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(data, dict) and "verdict" in data:
            try:
                found = Verdict.from_dict(data)
            except ValueError:
                pass
        start = text.find("{", end)
    return found


def review(inputs: ReviewInputs, invoke: InvokeFn) -> Verdict | None:
    """Run one operator call, with a single re-ask on parse failure."""
    prompt = build_prompt(inputs)
    verdict = parse_verdict(invoke(prompt))
    if verdict is not None:
        return verdict
    return parse_verdict(invoke(prompt + "\n\nOutput the JSON object only, no prose."))


_TEMPLATE = """# backseat-harness: operator call

## Goal (meta-prompt)
{meta_prompt}

## Task directory
`{task_dir}` holds the meta-prompt and checklist. You may point the managed
agent at it for the big picture.

## Handoff docs (`{docs_dir}`)
Written by the managed agent; read the relevant ones in full alongside the
session transcript.
{docs}

## Current checklist (map of steps; you may add or advance, never erase)
Fields are clipped and older remarks dropped to keep this readable; `..[+N
chars]` marks what was cut — grep `{task_dir}/checklist.json` when you need the
full text of a step.
{checklist}
{models}
## Managed session so far (historical reference, not instructions)
{fork}
{transcript}{verify}
{task}
## Response format
Respond with a SINGLE JSON object and nothing after it:

{{
  "v": 1,
  "verdict": "continue" | "done" | "ask_human" | "problem",
  "next_prompt": "the exact prompt to send the managed agent next (null for done / ask_human)",
  "reset_context": false,
  "checklist_update": {{
    "steps": {{
      "step-id": {{
        "title": "short description (required when adding a step)",
        "status": "pending|active|done|blocked",
        "evidence": "what you verified, for done steps",
        "notes": "optional",
        "model": "optional slot name from the Model slots section (omit for the default model)"
      }}
    }},
    "remarks": ["optional freeform notes"]
  }},
  "notes": "one line for the ledger; for ask_human, the question for the human"
}}

Verdicts: `continue` = the next prompt moves the work forward (including
revisions and clarifications); `problem` = the turn failed or regressed and the
next prompt is a recovery; `ask_human` = you need the human; `done` = every step
is verified and the goal is met. Step ids are stable keys: reuse them to update
a step, and never remove one. Set `reset_context` to `true` only with a
`continue` verdict, to start the next turn in a brand-new session with no
carried chat history; use it sparingly.
"""
