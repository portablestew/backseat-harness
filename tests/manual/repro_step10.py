"""Step 10 integration reproducers for the per-step-model-slot work.

Each section drives the real ``backseat_harness.loop.run_task`` over a real
``Config`` and the real ``OpencodeProvider``. The only stub is the managed
runner -- the callable that would spawn a live CLI -- which records the
``SpawnSpec`` the loop built and returns a canned ``TurnResult``. Live provider
turns are step 10a; everything asserted by the unit suite is re-derivable here
from what a run actually produced.

Nothing in here asserts a claim. It prints artifacts, so the output can be
diffed against what a doc says happened.

Reproducers for docs/10-validation.md. Run all three:

    uv run python tests/manual/repro_step10.py

Run one:

    uv run python tests/manual/repro_step10.py bad-slot     # (a)
    uv run python tests/manual/repro_step10.py slot-argv    # (b)
    uv run python tests/manual/repro_step10.py problem-cap  # (c)
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path

from backseat_harness import history, storage
from backseat_harness.config import Config
from backseat_harness.loop import run_task
from backseat_harness.providers import get_provider
from backseat_harness.providers.base import TurnResult

PROMPT_FIRST = "first prompt for the managed agent"


def _stub_history() -> None:
    """Replace the chat-mother-forker reads with fixed values.

    The loop reads history for the operator fork, the session size, and the
    transcript capture. Reading a real SQLite store would make the repro depend
    on whatever sessions this machine happens to have, so all three are pinned.
    Session size 0 keeps compaction off unless a case asks for it.
    """
    history.fork_session = lambda search, provider: "PRIOR MANAGED TRANSCRIPT (stubbed)"
    history.find_session = lambda search, provider: None
    history.load_full_transcript = lambda search, provider: ""


def _lane(ws: Path, slug: str, steps: dict, **cfg_kwargs) -> tuple[Config, str, object]:
    """Create a real task lane on disk and return its resolved config."""
    config = replace(Config(workspace_root=ws), **cfg_kwargs)
    paths = config.task_paths(slug)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.meta.write_text("Prove the per-step model slot behaviors.\n", encoding="utf-8")
    paths.checklist.write_text(
        json.dumps({"steps": steps, "remarks": []}), encoding="utf-8"
    )
    return config, slug, paths


def _recording_runner(seen: list):
    """A managed runner that records each SpawnSpec and spawns nothing."""

    def runner(provider, spec):
        seen.append(spec)
        return TurnResult(
            exit_code=0,
            timed_out=False,
            duration_s=0.1,
            native_session_id="ses_repro",
            stdout="",
            stderr="",
        )

    return runner


def _banner(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _show_prompt_fragment(prompt: str, needle: str, before: int = 2, after: int = 3) -> None:
    """Print the assembled operator prompt around the first line matching needle.

    Each line is prefixed with two spaces and a ``>>``/``  `` marker so the match
    stands out. An *empty* prompt line prints as an empty line instead: the
    prefix is a rendering aid, and letting it stand in for a blank line wrote
    five spaces of trailing whitespace into the capture, which is invisible in
    the doc and stripped by editors anyway. Only a truly empty line takes this
    path, so no non-empty line's bytes change, and the matched line always
    keeps its ``>>`` marker (it necessarily contains the needle).
    """
    lines = prompt.splitlines()
    hit = next((i for i, line in enumerate(lines) if needle in line), None)
    if hit is None:
        print(f"    !! no line containing {needle!r} in the assembled prompt")
        return
    lo = max(0, hit - before)
    hi = min(len(lines), hit + after + 1)
    for i in range(lo, hi):
        if lines[i] == "":
            print()
            continue
        marker = ">>" if i == hit else "  "
        print(f"  {marker} {lines[i]}")


def bad_slot(ws: Path) -> None:
    """(a) An on-disk bad slot surfaces in the operator prompt and on stderr.

    §5b: config changed or a bad slot was left behind, so the step names a slot
    no ``[models]`` entry defines. The turn must fall back to the default model
    and *tell both* the human (stderr) and the operator (prompt), never silently.
    """
    _banner("(a) on-disk bad slot -> stderr warn + 'Slot warning:' in the prompt")
    config, slug, _ = _lane(
        ws,
        "bad-slot",
        {"s1": {"status": "active", "model": "ghost"}},
        provider="opencode",
        model="cfg-default-model",
        models={"fast": "cfg-fast-model"},
    )
    print(f"[models] on disk      : {config.models}")
    print(f"[harness].model      : {config.model}")
    print("checklist step s1     : model = 'ghost'  (no such slot)")
    print()

    prompts: list[str] = []
    err = io.StringIO()
    with redirect_stderr(err):
        run_task(
            slug,
            config,
            get_provider("opencode"),
            _recording_runner([]),
            lambda p: prompts.append(p) or '{"verdict":"done"}',
            first_prompt=PROMPT_FIRST,
        )

    print("--- stderr emitted by the run -------------------------------------")
    for line in err.getvalue().splitlines():
        print(f"  {line}")
    print()
    print("--- assembled operator prompt, around the Slot warning line --------")
    _show_prompt_fragment(prompts[0], "Slot warning:")
    print()
    print("--- configured slot vocabulary as the operator sees it -------------")
    _show_prompt_fragment(prompts[0], "Model slots", before=1, after=2)


def slot_argv(ws: Path) -> None:
    """(b) A good slot's model reaches the managed turn's spawned argv.

    The argv is produced by the real ``OpencodeProvider.build_command`` from the
    exact ``SpawnSpec`` the loop handed the runner -- the same list that would be
    exec'd -- so this is the command line, not a re-derivation of it.
    """
    _banner("(b) slot -> --model on the spawned argv, and the no-slot fallbacks")
    provider = get_provider("opencode")

    cases = [
        (
            "active step names slot 'fast'",
            {"s1": {"status": "active", "model": "fast"}},
            {"model": "cfg-default-model", "models": {"fast": "cfg-fast-model"}},
        ),
        (
            "active step names no slot (config.model set)",
            {"s1": {"status": "active"}},
            {"model": "cfg-default-model", "models": {"fast": "cfg-fast-model"}},
        ),
        (
            "no slot, no config.model, no [models].default",
            {"s1": {"status": "active"}},
            {"model": None, "models": {}},
        ),
    ]

    for index, (label, steps, cfg_kwargs) in enumerate(cases):
        config, slug, _ = _lane(
            ws, f"argv-{index}", steps, provider="opencode", **cfg_kwargs
        )
        seen: list = []
        with redirect_stderr(io.StringIO()):
            run_task(
                slug,
                config,
                provider,
                _recording_runner(seen),
                lambda p: '{"verdict":"done"}',
                first_prompt=PROMPT_FIRST,
            )
        spec = seen[0]
        print(f"--- {label}")
        print(f"  [harness].model     : {config.model!r}")
        print(f"  [models]            : {config.models}")
        print(f"  checklist s1        : {json.dumps(steps['s1'])}")
        print(f"  spec.extra_args     : {list(spec.extra_args)}")
        print(f"  build_command(spec) : {provider.build_command(spec)}")
        print()


def problem_cap(ws: Path) -> None:
    """(c) Identical-prompt PROBLEMs are capped before max_iterations.

    §6: an operator answering ``problem`` with ``next_prompt: null`` leaves the
    prompt unchanged, so the same prompt re-sends. The cap stops the run; the
    reason string and iteration count are printed as the run produced them.
    """
    _banner("(c) identical-prompt PROBLEMs -> capped run, exact reason")
    config, slug, paths = _lane(
        ws,
        "problem-cap",
        {"s1": {"status": "active"}},
        provider="opencode",
        max_iterations=50,
    )
    print(f"[harness].max_iterations     : {config.max_iterations}")
    print(f"[harness].problem_repeat_cap : {config.problem_repeat_cap}")
    print()

    seen: list = []
    err = io.StringIO()
    with redirect_stderr(err):
        result = run_task(
            slug,
            config,
            get_provider("opencode"),
            _recording_runner(seen),
            lambda p: '{"verdict":"problem","next_prompt":null,"notes":"still broken"}',
            first_prompt=PROMPT_FIRST,
        )

    print("--- terminal result -------------------------------------------------")
    print(f"  status                  : {result.status.value}")
    print(f"  reason                  : {result.reason}")
    print(f"  iterations              : {result.iterations}")
    print(f"  final_verdict           : {result.final_verdict}")
    print()
    print("--- prompts actually sent, one per turn ------------------------------")
    # The cap counts the *effective* prompt, so that is what is compared here.
    # The spawned text is not identical: turn 1 carries the session marker the
    # loop prepends to establish identity, so the two differ by that line alone.
    for index, spec in enumerate(seen, start=1):
        head = spec.prompt.split("\n\n")[0]
        body = spec.prompt.rsplit("\n\n", 1)[-1]
        print(f"  turn {index} effective : {body!r}")
        print(f"  turn {index} first line: {head!r}")
    bodies = [s.prompt.rsplit("\n\n", 1)[-1] for s in seen]
    print(f"  distinct EFFECTIVE prompts : {len(set(bodies))}  <- what the cap counts")
    print(f"  distinct SPAWNED  prompts  : {len({s.prompt for s in seen})}  (marker on turn 1)")
    print()
    print("--- what the run left on disk ----------------------------------------")
    state = storage.load_state(paths, slug)
    print(f"  state.status            : {state.status.value}")
    print(f"  state.reason            : {state.reason}")
    print(f"  ledger lines            : {len(list(storage.read_ledger(paths)))}")


SECTIONS = {"bad-slot": bad_slot, "slot-argv": slot_argv, "problem-cap": problem_cap}


def main(argv: list[str]) -> int:
    wanted = argv[1:] or list(SECTIONS)
    unknown = [name for name in wanted if name not in SECTIONS]
    if unknown:
        print(f"unknown section(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"choose from: {', '.join(SECTIONS)}", file=sys.stderr)
        return 2
    _stub_history()
    ws = Path(tempfile.mkdtemp(prefix="repro-step10-"))
    for name in wanted:
        SECTIONS[name](ws)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
