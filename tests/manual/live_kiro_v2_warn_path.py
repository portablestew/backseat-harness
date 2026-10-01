"""Step 10a4 -- one live two-turn kiro (v2) run, two model slots.

The kiro half of plan row 9 (``docs/2-plan.md``), and the last of the three live
runs the research gate demanded. Same shape as
``tests/manual/live_opencode_slots.py``: real ``loop.run_task``, real
``KiroProvider``, real ``spawn.run_turn``, exactly two managed turns, the slot
flipped by the scripted operator's ``checklist_update``. Run in a throwaway temp
workspace so the live turns cannot touch a working tree.

**The target here is detection, not switching.** Nothing in this run is expected
to show a model changing, and a verdict that implied one would be wrong.
``providers/kiro.py`` does not pass ``--v3``, so the engine is the default v2,
and research section 3a (``1-research-model-switch.md:149-173``) established that
v2 never applies ``--model``: it accepts the flag, prints
``[warn] failed to set model '<id>': Method not found`` on **stderr**, exits 0,
and serves the turn from its ``auto`` router. So what this run has to prove is
that the harness *notices* and *surfaces* that rejection rather than passing the
flag and reporting success. Four things are recorded per turn:

    (i)   the argv the harness built carries the slot's concrete model id
    (ii)  kiro's own ``[warn] failed to set model`` on that turn's CLI stderr
    (iii) the harness's own loop-side ``[warn]`` on that turn's loop stderr
    (iv)  the slot warning the loop surfaces into that turn's operator prompt

(iv) is the one that reaches a human: ``operator.py`` appends it to the prompt
as ``Slot warning: ...``, and a silent no-op that nobody is told about is the
failure mode the design exists to prevent.

Because the prompts exist only to make each turn a real turn, they are kept to a
single short word -- kiro credits are metered and the reply text is *not* the
evidence here. The same two model ids as research 3a are used
(``claude-sonnet-4.6`` / ``glm-5``) so this run is comparable to that probe.

The kiro session sidecar (``~/.kiro/sessions/cli/<sid>.json``) is read after the
run, read-only, to show kiro's own record of which model served the turn.

    uv run python tests/manual/live_kiro_v2_warn_path.py [raw_capture_path]
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path

from backseat_harness import storage
from backseat_harness.config import Config
from backseat_harness.loop import run_task
from backseat_harness.providers import get_provider
from backseat_harness.providers.base import Provider, SpawnSpec
from backseat_harness.spawn import resolve_argv, run_turn

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW = (
    REPO_ROOT
    / ".backseat"
    / "tasks"
    / "per-step-models"
    / "docs"
    / "10a-live-kiro-v2-raw.txt"
)
SIDECAR_DIR = Path(os.path.expanduser(r"~\.kiro\sessions\cli"))

# The same two ids research 3a probed, in the same order, so the warn text this
# run produces can be compared to that probe line for line. Research 3b showed
# v2 cannot distinguish valid from invalid ids -- both produce the identical
# warn -- so these are not being validated here, only carried through.
SLOT_A = "claude-sonnet-4.6"
SLOT_B = "glm-5"

# Minimal on purpose. The reply text is not the evidence for this slice; the
# warn lines are. Each turn gets a distinct one-word sentinel purely so the two
# turns are distinguishable in the transcript.
FIRST_PROMPT = "Reply with the single word: OK-ONE"
SECOND_PROMPT = "Reply with the single word: OK-TWO"

KIRO_WARN = "[warn] failed to set model"
LOOP_WARN = "[warn] provider kiro does not honor --model"
SLOT_WARNING_PREFIX = "Slot warning:"


def _banner(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _slot_warning_from_prompt(prompt: str) -> str | None:
    """The ``Slot warning: ...`` line the loop put in this operator prompt.

    ``operator.py`` renders it as ``f"\\nSlot warning: {warning}\\n"`` at the end
    of the model-slots section, so it is one line; the ``[warn] `` prefix that
    the same string carries on stderr is *not* part of it.
    """
    for line in prompt.splitlines():
        if line.startswith(SLOT_WARNING_PREFIX):
            return line.strip()
    return None


class Turn:
    """One managed turn: argv, CLI stderr, loop stderr, and the operator warning."""

    def __init__(self, index: int, spec: SpawnSpec, result, provider: Provider):
        self.index = index
        self.spec = spec
        self.result = result
        self.argv = resolve_argv(provider.build_command(spec))
        self.reply = provider.extract_text(result.stdout)
        self.loop_stderr = ""        # filled in by main(), from the stderr window
        self.slot_warning: str | None = None   # filled in from the operator prompt
        self.sidecar: dict | None = None

    @property
    def model_flag(self) -> str | None:
        argv = self.argv
        return argv[argv.index("--model") + 1] if "--model" in argv else None

    @property
    def resume_flag(self) -> str | None:
        """kiro resumes with --resume-id (providers/kiro.py:58)."""
        argv = self.argv
        return argv[argv.index("--resume-id") + 1] if "--resume-id" in argv else None

    @property
    def kiro_model_warn(self) -> str | None:
        """kiro's own stderr line rejecting --model, if it emitted one."""
        for line in self.result.stderr.splitlines():
            if KIRO_WARN in line:
                return line.strip()
        return None

    @property
    def sidecar_model(self) -> str | None:
        if not self.sidecar:
            return None
        for key in ("model", "rts_model_state.model_info.model_id"):
            value = self.sidecar
            for part in key.split("."):
                if not isinstance(value, dict) or part not in value:
                    value = None
                    break
                value = value[part]
            if isinstance(value, str):
                return value
        return None


def _recording_runner(turns: list[Turn], err: io.StringIO, marks: list[int]):
    """Spawn for real, recording the stderr-window boundaries around each turn.

    The loop prints its kiro ``[warn]`` *before* calling the runner
    (``loop.py:234`` precedes ``loop.py:251``), so the stderr between the previous
    runner call and this one is exactly this turn's loop-side output.
    """

    def runner(provider, spec):
        marks.append(len(err.getvalue()))
        result = run_turn(provider, spec)
        turns.append(Turn(len(turns) + 1, spec, result, provider))
        return result

    return runner


def _scripted_operator(prompts: list[str], calls: list[int]):
    """Exactly two answers: continue (flipping the slot), then done."""

    def operator(prompt: str) -> str:
        prompts.append(prompt)
        calls.append(1)
        n = len(calls)
        if n == 1:
            return json.dumps(
                {
                    "verdict": "continue",
                    "notes": "turn 1 done on slot A; re-pointing the step at slot B",
                    "next_prompt": SECOND_PROMPT,
                    "checklist_update": {
                        "steps": {"s1": {"status": "active", "model": "b"}}
                    },
                }
            )
        return json.dumps(
            {
                "verdict": "done",
                "notes": "turn 2 done on slot B",
                "checklist_update": {"steps": {"s1": {"status": "done"}}},
            }
        )

    return operator


def _read_sidecar(session_id: str | None) -> dict | None:
    """kiro's own per-session sidecar, read-only, after the run. None if absent."""
    if not session_id:
        return None
    path = SIDECAR_DIR / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def main(argv: list[str]) -> int:
    raw_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_RAW

    ws = Path(tempfile.mkdtemp(prefix="live-kiro-v2-warn-"))
    config = replace(
        Config(workspace_root=ws),
        provider="kiro",
        models={"a": SLOT_A, "b": SLOT_B},
    )
    paths = config.task_paths("live-kiro-v2")
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.meta.write_text(
        "Prove the harness detects and surfaces kiro v2's refusal of --model.\n",
        encoding="utf-8",
    )
    paths.checklist.write_text(
        json.dumps(
            {"steps": {"s1": {"status": "active", "model": "a"}}, "remarks": []}
        ),
        encoding="utf-8",
    )

    _banner("setup")
    print(f"workspace_root (disposable temp) : {ws}")
    print(f"provider                         : {config.provider}")
    print(f"[models]                         : {config.models}")
    print("checklist step s1                : model='a' -> operator flips it to 'b'")
    print("operator invoker                 : scripted, 2 answers (continue, done)")
    print(f"expected kiro warn               : {KIRO_WARN} '<id>': Method not found")
    print(f"expected loop warn               : {LOOP_WARN} ...")
    print(f"sidecar dir (read-only, after)   : {SIDECAR_DIR}")

    turns: list[Turn] = []
    prompts: list[str] = []
    calls: list[int] = []
    marks: list[int] = []
    err = io.StringIO()
    with redirect_stderr(err):
        result = run_task(
            "live-kiro-v2",
            config,
            get_provider("kiro"),
            _recording_runner(turns, err, marks),
            _scripted_operator(prompts, calls),
            first_prompt=FIRST_PROMPT,
        )

    stderr_text = err.getvalue()
    # Attribute each turn's loop-side stderr: the window between the previous
    # runner call's entry mark and this one's.
    for i, turn in enumerate(turns):
        lo = marks[i - 1] if i else 0
        turn.loop_stderr = stderr_text[lo : marks[i]].strip()
    # Operator call N reviews turn N, so it carries turn N's slot warning.
    for i, turn in enumerate(turns):
        if i < len(prompts):
            turn.slot_warning = _slot_warning_from_prompt(prompts[i])
    for turn in turns:
        turn.sidecar = _read_sidecar(turn.result.native_session_id)

    print(f"managed turns spawned            : {len(turns)}  (operator calls: {len(calls)})")
    print(f"operator prompts captured        : {len(prompts)}")

    _banner("(a) argv the harness built, per turn")
    for turn in turns:
        print(f"--- turn {turn.index}")
        print(f"  argv                 : {turn.argv}")
        print(f"  --model              : {turn.model_flag}")
        print(f"  --resume-id          : {turn.resume_flag}")
        print(f"  spec.cwd             : {turn.spec.cwd}")
        print(f"  exit_code            : {turn.result.exit_code}")
        print(f"  native_session_id    : {turn.result.native_session_id}")
        print(f"  duration_s           : {turn.result.duration_s:.2f}")
    sids = [t.result.native_session_id for t in turns]
    print()
    print(f"  session ids          : {sids}")
    print(f"  same id on both turns: {len(set(sids)) == 1 and sids[0] is not None}")

    _banner("(b) kiro's own stderr: --model rejection, per turn")
    for turn in turns:
        print(f"--- turn {turn.index}  (--model {turn.model_flag})")
        if turn.kiro_model_warn:
            print(f"  {turn.kiro_model_warn}")
        else:
            print("  (no 'failed to set model' line on this turn's stderr)")
        print(f"  full stderr          : {turn.result.stderr.strip() or '(empty)'}")

    _banner("(c) the harness's own loop-side warning, per turn")
    for turn in turns:
        print(f"--- turn {turn.index}  (slot {turn.model_flag})")
        for line in turn.loop_stderr.splitlines() or ["(no loop stderr in this window)"]:
            print(f"  {line}")

    _banner("(d) slot warning surfaced into the operator prompt, per turn")
    for turn in turns:
        print(f"--- turn {turn.index}")
        print(f"  {turn.slot_warning or '(no Slot warning: line in this prompt)'}")

    _banner("(e) kiro's own record: which model served the turn")
    for turn in turns:
        print(
            f"  turn {turn.index}: requested {turn.model_flag!r}  ->  "
            f"sidecar model {turn.sidecar_model!r}   (sidecar found: "
            f"{turn.sidecar is not None})"
        )

    _banner("(f) verdict")
    kiro_warned = [t.kiro_model_warn is not None for t in turns]
    loop_warned = [LOOP_WARN in t.loop_stderr for t in turns]
    surfaced = [t.slot_warning is not None for t in turns]
    models = [t.model_flag for t in turns]
    served = [t.sidecar_model for t in turns]
    print(f"  turn 1 slot 'a' -> argv --model {models[0] if models else None}")
    print(f"  turn 2 slot 'b' -> argv --model {models[1] if len(models) > 1 else None}")
    print()
    print("  DID THE SERVING MODEL CHANGE? NO")
    print(
        "  expected: kiro v2 never applies --model (research 3a). No switch is "
        "the correct outcome, not a failure of this run."
    )
    print(f"  kiro rejected the flag on both turns : {all(kiro_warned)}  {kiro_warned}")
    print(f"  harness warned on both turns        : {all(loop_warned)}  {loop_warned}")
    print(f"  operator told on both turns         : {all(surfaced)}  {surfaced}")
    print(f"  sidecar model, turn 1 / turn 2      : {served[0] if served else None} / "
          f"{served[1] if len(served) > 1 else None}")
    detected = all(kiro_warned) and all(loop_warned) and all(surfaced)
    print()
    print(f"  DID THE HARNESS DETECT AND SURFACE THE REFUSAL? {'YES' if detected else 'NO'}")
    print("  evidence: per-turn CLI stderr (kiro's own line), per-turn loop stderr")
    print("            ([warn] provider kiro does not honor --model), and the")
    print("            'Slot warning:' line in that turn's operator prompt")
    print("  NOT from the reply text: the replies are not consulted for any of this")
    print()
    print(f"  run status           : {result.status.value}  ({result.reason})")
    print(f"  iterations           : {result.iterations}")
    print(f"  final_verdict        : {result.final_verdict}")
    print(f"  state.status on disk : {storage.load_state(paths, 'live-kiro-v2').status.value}")

    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with raw_path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(f"# sidecar dir: {SIDECAR_DIR}\n")
        fh.write(f"# requested slot a: {SLOT_A}\n")
        fh.write(f"# requested slot b: {SLOT_B}\n")
        for turn in turns:
            fh.write(f"########## TURN {turn.index} ##########\n")
            fh.write(f"# argv: {json.dumps(turn.argv)}\n")
            fh.write(f"# --model: {turn.model_flag}\n")
            fh.write(f"# --resume-id: {turn.resume_flag}\n")
            fh.write(f"# exit_code: {turn.result.exit_code}\n")
            fh.write(f"# native_session_id: {turn.result.native_session_id}\n")
            fh.write(f"# kiro model warn: {turn.kiro_model_warn}\n")
            fh.write(f"# loop stderr window: {turn.loop_stderr or '(empty)'}\n")
            fh.write(f"# operator Slot warning: {turn.slot_warning or '(none)'}\n")
            fh.write(f"# sidecar model: {turn.sidecar_model}\n")
            fh.write(f"# reply: {json.dumps(turn.reply)}\n")
            fh.write("########## CLI STDERR ##########\n")
            fh.write((turn.result.stderr or "(empty)") + "\n")
            fh.write("########## CLI STDOUT ##########\n")
            fh.write(turn.result.stdout)
            if not turn.result.stdout.endswith("\n"):
                fh.write("\n")
    print()
    print(f"raw capture written to: {raw_path}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
