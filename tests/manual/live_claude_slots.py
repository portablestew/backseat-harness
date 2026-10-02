"""Step 10a slice 2a -- one live two-turn claude run, two model slots.

Drives the **real** ``loop.run_task`` over a **real** ``ClaudeProvider`` and the
**real** ``spawn.run_turn``, so both managed turns are actual ``claude``
processes. Two managed turns, no more: the operator is scripted (not
hand-driven) to answer ``continue`` on the first call -- flipping the active
checklist step's slot from A to B in the same verdict -- and ``done`` on the
second. The loop runs exactly one managed turn per operator call, so a
two-answer script is a two-turn run by construction.

The run happens in a throwaway temp workspace, never in the repo it is invoked
from, so the live turns cannot touch a working tree.

What this exists to establish: whether a per-step ``[models]`` slot actually
changes the model that *serves* a resumed turn, or only the id on the command
line. Research 1-research-model-switch.md:130-135 is explicit that the only
machine-checkable proof for claude is ``modelUsage.<id>.canonicalModel`` in the
stream -- ``events[*].model`` is the request-time model and can differ from what
ran. Both are extracted here, per turn, from the real stream, and the verdict is
computed from ``canonicalModel`` alone.

    uv run python tests/manual/live_claude_slots.py [raw_capture_path]

Costs two real API turns on the operator's own claude account. Re-running starts
a fresh session (new root uuid, new claude session id) but must produce the same
argv shape and the same canonicalModel pair.
"""

from __future__ import annotations

import io
import json
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
    / "10a-live-claude-raw.txt"
)

# Two slots, two genuinely different concrete models. These are the aliases
# `claude --help` documents ("an alias for the latest model (e.g. 'fable',
# 'opus', or 'sonnet')"), which is the robust spelling: an alias always resolves
# to whatever that family currently serves, so the run cannot fail on a stale
# full name. Research §2 recorded this account serving both, resolving to
# claude-sonnet-5 and claude-opus-5-5. The live run is the real check -- if a
# model were not served, the turn would fail or canonicalModel would say so.
SLOT_A = "sonnet"
SLOT_B = "opus"

FIRST_PROMPT = (
    "Reply with exactly this text and nothing else: TURN-OK. "
    "Do not use any tools and do not read or write any files."
)
SECOND_PROMPT = (
    "Reply with exactly this text and nothing else: TURN-OK-AGAIN. "
    "Do not use any tools and do not read or write any files."
)


def _banner(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


class Turn:
    """Everything one managed turn produced: what we asked for, what came back."""

    def __init__(self, index: int, spec: SpawnSpec, result, provider: Provider):
        self.index = index
        self.spec = spec
        self.result = result
        # The argv is rebuilt by the real provider from the spec the loop built,
        # so what is printed below is the command line that was actually exec'd.
        self.argv = resolve_argv(provider.build_command(spec))
        self.stream = [e for e in _json_lines(result.stdout) if isinstance(e, dict)]

    @property
    def model_flag(self) -> str | None:
        argv = self.argv
        return argv[argv.index("--model") + 1] if "--model" in argv else None

    @property
    def resume_id(self) -> str | None:
        argv = self.argv
        return argv[argv.index("--resume") + 1] if "--resume" in argv else None

    @property
    def request_models(self) -> list[tuple[str, str]]:
        """(event index, model) for every `model` field in the stream."""
        found = []
        for i, event in enumerate(self.stream):
            if isinstance(event.get("model"), str):
                found.append((f"events[{i}].model", event["model"]))
            msg = event.get("message")
            if isinstance(msg, dict) and isinstance(msg.get("model"), str):
                found.append((f"events[{i}].message.model", msg["model"]))
        return found

    @property
    def canonical_models(self) -> list[tuple[str, str]]:
        """(event index, canonicalModel) from modelUsage -- the ground truth."""
        found = []
        for i, event in enumerate(self.stream):
            usage = event.get("modelUsage")
            if not isinstance(usage, dict):
                continue
            for model_id, entry in usage.items():
                if isinstance(entry, dict) and isinstance(
                    entry.get("canonicalModel"), str
                ):
                    found.append(
                        (f"events[{i}].modelUsage['{model_id}'].canonicalModel",
                         entry["canonicalModel"])
                    )
        return found

    @property
    def unique_canonical(self) -> list[str]:
        return sorted({v for _, v in self.canonical_models})


def _json_lines(text: str) -> list[object]:
    """Parse stream-json, tolerating interleaved non-JSON noise (as the adapter does)."""
    out: list[object] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append(line)
    return out


def _recording_runner(turns: list[Turn]):
    """The real spawn, wrapped only to record the spec and the raw stream."""

    def runner(provider, spec):
        result = run_turn(provider, spec)
        turns.append(Turn(len(turns) + 1, spec, result, provider))
        return result

    return runner


def _scripted_operator(calls: list[int]):
    """Exactly two answers: continue (flipping the slot), then done."""

    def operator(_prompt: str) -> str:
        calls.append(1)
        n = len(calls)
        if n == 1:
            return json.dumps(
                {
                    "verdict": "continue",
                    "notes": "turn 1 done on slot A; switching the step to slot B",
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


def main(argv: list[str]) -> int:
    raw_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_RAW

    ws = Path(tempfile.mkdtemp(prefix="live-claude-slots-"))
    config = replace(
        Config(workspace_root=ws),
        provider="claude",
        models={"a": SLOT_A, "b": SLOT_B},
    )
    paths = config.task_paths("live-claude")
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.goal.write_text(
        "Prove a per-step model slot changes the serving claude model.\n",
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
    print(f"[models]                         : {config.models}")
    print("checklist step s1                : model='a'  -> operator flips it to 'b'")
    print("operator invoker                 : scripted, 2 answers (continue, done)")

    turns: list[Turn] = []
    calls: list[int] = []
    err = io.StringIO()
    with redirect_stderr(err):
        result = run_task(
            "live-claude",
            config,
            get_provider("claude"),
            _recording_runner(turns),
            _scripted_operator(calls),
            first_prompt=FIRST_PROMPT,
        )

    print(f"managed turns spawned            : {len(turns)}  (operator calls: {len(calls)})")

    _banner("(a) full argv the harness built, per turn")
    for turn in turns:
        print(f"--- turn {turn.index}")
        print(f"  argv                 : {turn.argv}")
        print(f"  --model              : {turn.model_flag}")
        print(f"  --resume             : {turn.resume_id}")
        print(f"  spec.cwd             : {turn.spec.cwd}")
        print(f"  exit_code            : {turn.result.exit_code}")
        print(f"  native_session_id    : {turn.result.native_session_id}")
        print(f"  duration_s           : {turn.result.duration_s:.2f}")
        print(f"  stderr               : {turn.result.stderr.strip() or '(empty)'}")
    sids = [t.result.native_session_id for t in turns]
    print()
    print(f"  session ids          : {sids}")
    print(f"  same id on both turns: {len(set(sids)) == 1 and sids[0] is not None}")

    _banner("(b) claude's own stream evidence, per turn")
    for turn in turns:
        print(f"--- turn {turn.index}")
        print(f"  request-time model fields ({len(turn.request_models)}):")
        for where, value in turn.request_models:
            print(f"    {where:<52} = {value}")
        print(f"  modelUsage canonicalModel ({len(turn.canonical_models)}):")
        for where, value in turn.canonical_models:
            print(f"    {where:<52} = {value}")
        if not turn.canonical_models:
            print("    (none -- this is the finding, not a formatting problem)")

    _banner("(c) verdict")
    a, b = (turns[0] if turns else None), (turns[1] if len(turns) > 1 else None)
    if a and b and a.canonical_models and b.canonical_models:
        ca, cb = a.unique_canonical, b.unique_canonical
        print(f"  turn 1 slot '{SLOT_A}' -> --model {a.model_flag} -> canonicalModel {ca}")
        print(f"  turn 2 slot '{SLOT_B}' -> --model {b.model_flag} -> canonicalModel {cb}")
        switched = set(ca) != set(cb)
        print()
        print(f"  DID THE SLOT CHANGE THE SERVING MODEL? {'YES' if switched else 'NO'}")
        print("  basis: modelUsage.<id>.canonicalModel, the field research §2 names as truth")
    else:
        print("  COULD NOT DECIDE -- a turn reported no canonicalModel; see (b).")
    print()
    print(f"  run status           : {result.status.value}  ({result.reason})")
    print(f"  iterations           : {result.iterations}")
    print(f"  final_verdict        : {result.final_verdict}")
    print(f"  state.status on disk : {storage.load_state(paths, 'live-claude').status.value}")

    _banner("stderr emitted by the run")
    for line in err.getvalue().splitlines():
        print(f"  {line}")
    if not err.getvalue().strip():
        print("  (empty)")

    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with raw_path.open("w", encoding="utf-8", newline="") as fh:
        for turn in turns:
            fh.write(f"########## TURN {turn.index} ##########\n")
            fh.write(f"# argv: {json.dumps(turn.argv)}\n")
            fh.write(f"# --model: {turn.model_flag}\n")
            fh.write(f"# exit_code: {turn.result.exit_code}\n")
            fh.write(
                f"# native_session_id: {turn.result.native_session_id}\n"
            )
            fh.write(
                f"# canonicalModel: {json.dumps(turn.unique_canonical)}\n"
            )
            fh.write("########## STDOUT ##########\n")
            fh.write(turn.result.stdout)
            if not turn.result.stdout.endswith("\n"):
                fh.write("\n")
            fh.write("########## STDERR ##########\n")
            fh.write(turn.result.stderr or "(empty)\n")
            if turn.result.stderr and not turn.result.stderr.endswith("\n"):
                fh.write("\n")
    print()
    print(f"raw capture written to: {raw_path}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
