"""Thin CLI wrapper over the loop library.

Subcommands: init (scaffold a task lane), run (drive a task), takeover, status,
list. Every command supports ``--format json`` so a parent workflow can wait on
the process and parse machine-readable output. The CLI wires real subprocess
runners for the managed and operator agents; the loop itself stays injectable.

``run <slug> [GOAL|BRIEF]`` reads its second argument two ways. On a lane with no
goal-prompt yet it is the goal, and it is required. On a lane that already has
one it is a brief from the task owner for this run only: it rides the
plan-phase operator prompt, outranks the goal-prompt where they conflict, and
ends with the run. That is how an ``ask_human`` pause gets answered without a
separate verb -- and it is why a lane that is blocked or complete refuses a bare
resume.
"""

from __future__ import annotations

import argparse
import json
import sys

from backseat_harness import __version__, logs, progress, spawn, storage
from backseat_harness.config import Config, OperatorConfig, TaskPaths, load_config
from backseat_harness.loop import OperatorReply, run_task
from backseat_harness.models import TaskStatus
from backseat_harness.operator import load_persona
from backseat_harness.providers import get_provider
from backseat_harness.providers.base import Provider, SpawnSpec
from backseat_harness.spawn import run_turn


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    progress.set_debug(getattr(args, "debug", False))
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="backseat-harness", description=__doc__)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument(
        "-C",
        "--workspace",
        default=".",
        help="workspace root containing .backseat/ (default: cwd)",
    )
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.add_argument(
        "--debug",
        action="store_true",
        help="log each loop step with its elapsed time on stderr (provider call "
        "logs are always written to the task's logs/ dir)",
    )
    sub = p.add_subparsers(dest="command")

    init = sub.add_parser("init", help="scaffold a task lane under .backseat/tasks/")
    init.add_argument("slug", help="task slug (filesystem-safe)")
    init.add_argument(
        "goal", nargs="?", help="goal-prompt text; omit to create an empty goal.md"
    )
    init.set_defaults(func=_cmd_init)

    run = sub.add_parser(
        "run",
        help="drive a task lane (creates it when a goal-prompt is given for a new slug)",
    )
    run.add_argument("slug")
    run.add_argument(
        "goal",
        nargs="?",
        metavar="GOAL|BRIEF",
        help="the goal-prompt when the lane has none yet; on a lane that already "
        "has one, a brief from you added to this run's operator prompt. Required "
        "for a new task, and for one that is blocked or complete",
    )
    run.add_argument(
        "-p",
        "--prompt",
        help="override the first managed prompt (default: the operator plans first)",
    )
    run.set_defaults(func=_cmd_run)

    takeover = sub.add_parser(
        "takeover",
        help="open the managed session interactively to answer/unblock it, then resume",
    )
    takeover.add_argument("slug")
    takeover.add_argument(
        "--no-resume",
        action="store_true",
        help="just hand off interactively; don't run the loop again afterward",
    )
    takeover.set_defaults(func=_cmd_takeover)

    status = sub.add_parser("status", help="show a task's current state")
    status.add_argument("slug")
    status.set_defaults(func=_cmd_status)

    ls = sub.add_parser("list", help="list task lanes")
    ls.set_defaults(func=_cmd_list)

    return p


def _cmd_init(args) -> int:
    config = load_config(args.workspace, args.slug)
    paths = config.task_paths(args.slug)
    paths.root.mkdir(parents=True, exist_ok=True)
    if not paths.goal.exists():
        paths.goal.write_text((args.goal or "") + "\n", encoding="utf-8")
    if not paths.checklist.exists():
        paths.checklist.write_text('{\n  "steps": {},\n  "remarks": []\n}\n', encoding="utf-8")
    _emit(
        args,
        {"initialized": args.slug, "path": str(paths.root)},
        f"Initialized task '{args.slug}' at {paths.root}",
    )
    return 0


def _cmd_run(args) -> int:
    config = load_config(args.workspace, args.slug)
    paths = config.task_paths(args.slug)
    error, brief = _ensure_goal(paths, args.goal)
    if error:
        _emit(args, {"slug": args.slug, "error": error}, error)
        return 2
    if brief and args.prompt:
        error = (
            "a brief needs the operator, and --prompt skips it; drop --prompt, "
            "or put the instruction in --prompt itself"
        )
        _emit(args, {"slug": args.slug, "error": error}, error)
        return 2
    if not brief and not args.prompt:
        # No brief means "just carry on", which is only true of a lane already
        # carrying on. Anything else -- blocked on a question, or finished --
        # needs the owner to say something, and resuming anyway is how a
        # pending question gets silently dropped on the floor.
        #
        # --prompt is exempt: it is the explicit "skip the operator" override,
        # so it is already the owner saying what happens next. Gating it would
        # remove the only way to drive a blocked lane with the operator out of
        # the loop entirely.
        block = _resume_block(paths, args.slug)
        if block is not None:
            _emit(args, {"slug": args.slug, "error": block}, block)
            return 2

    provider = get_provider(config.provider)
    managed_runner = run_turn
    operator_invoker = _make_operator_invoker(config, config.operator)

    result = run_task(
        slug=args.slug,
        config=config,
        provider=provider,
        managed_runner=managed_runner,
        operator_invoker=operator_invoker,
        first_prompt=args.prompt,
        owner_brief=brief,
    )
    _emit(args, result.to_dict(), _result_line(result))
    return 0 if result.status.value in {"complete", "blocked"} else 1


def _resume_block(paths: TaskPaths, slug: str) -> str | None:
    """Why a bare ``run <slug>`` may not proceed, or None when it may.

    A lane is resumable only while it is ``continue``. ``blocked`` and
    ``complete`` both need the owner's word first, and the message says so with
    the exact command -- the question is quoted whole, because it is addressed
    to the person reading the terminal and ``progress`` truncated it to 110
    characters on the way out.
    """
    if not paths.state.exists():
        return None
    state = storage.load_state(paths, slug)
    if state.status is TaskStatus.CONTINUE:
        return None
    quoted = " ".join(state.reason.split()) or "(no reason recorded)"
    return (
        f"task '{slug}' is {state.status.value}, not continue, so it needs a "
        f"brief from you rather than a bare resume.\n"
        f"  reason: {quoted}\n"
        f"  resume it with: backseat-harness run {slug} \"<your answer or new instruction>\""
    )


def _result_line(result) -> str:
    """Final human-readable line for a finished ``run_task``.

    On ``complete`` the reason is the operator's last note, already echoed by
    the stderr progress line, so it is omitted. ``blocked`` keeps it (the
    question for the human, which progress truncated, or the budget guard, whose
    reason carries this run's turn count alongside the lane total because the two
    differ once a lane has been run more than once).
    """
    line = f"{result.slug}: {result.status.value} after {result.iterations} iterations"
    if result.status.value != "complete" and result.reason:
        line += f" ({result.reason})"
    return line


def _cmd_takeover(args) -> int:
    """Human takeover: drive the managed session by hand, then resume the loop.

    Complements ``run <slug> "brief"``, and the two are not interchangeable.
    Takeover mutates the *managed* session: the owner talks to the agent itself
    and the operator re-plans from the history that produced. A brief mutates the
    *operator's* brief and never reaches the agent. So a question about the
    agent's own work takes a takeover, while a decision the operator escalated
    -- a product call, a config choice, a ruling on a finding -- takes a brief,
    because the operator is the party that asked and the managed agent has no
    standing to answer.

    The pending question is printed before the handoff. The owner is about to be
    dropped into a chat window with an agent that has no idea a question is
    outstanding, and ``progress`` truncated that question to 110 characters on
    the way out; here it is whole.
    """
    config = load_config(args.workspace, args.slug)
    paths = config.task_paths(args.slug)
    if not paths.goal.exists():
        _emit(args, {"slug": args.slug, "error": "unknown task"}, f"No task '{args.slug}'")
        return 2
    if paths.state.exists():
        state = storage.load_state(paths, args.slug)
        if state.status is TaskStatus.BLOCKED and state.reason:
            print(f"This lane is blocked on a question from the operator:\n  {state.reason}")

    provider = get_provider(config.provider)
    binding = storage.load_binding(paths)
    native_id = binding.current.native_id if binding and binding.current else None

    where = f"session {native_id}" if native_id else "a fresh session"
    print(f"Handing you {provider.name} ({where}). Advance it, then quit to resume.")
    code = spawn.run_interactive(provider, native_id, config.workspace_root)
    if code != 0:
        _emit(
            args,
            {"slug": args.slug, "interactive_exit_code": code},
            f"Interactive session exited with code {code}; not resuming.",
        )
        return code
    if args.no_resume:
        _emit(args, {"slug": args.slug, "resumed": False}, "Handed off; not resuming.")
        return 0

    # Resume: no first_prompt, so the operator re-plans from the advanced session.
    result = run_task(
        slug=args.slug,
        config=config,
        provider=provider,
        managed_runner=run_turn,
        operator_invoker=_make_operator_invoker(config, config.operator),
        first_prompt=None,
    )
    _emit(args, result.to_dict(), _result_line(result))
    return 0 if result.status.value in {"complete", "blocked"} else 1


def _cmd_status(args) -> int:
    config = load_config(args.workspace, args.slug)
    paths = config.task_paths(args.slug)
    if not paths.state.exists():
        _emit(args, {"slug": args.slug, "status": "unknown"}, f"No state for task '{args.slug}'")
        return 1
    state = storage.load_state(paths, args.slug)
    binding = storage.load_binding(paths)
    payload = state.to_dict()
    if binding and binding.current:
        payload["native_session_id"] = binding.current.native_id
        payload["transcript_bytes"] = binding.current.transcript_bytes
    line = f"{state.slug}: {state.status.value} | iter {state.iteration} | gen {state.generation}"
    if state.reason:
        line += f" | {state.reason}"
    _emit(args, payload, line)
    return 0


def _cmd_list(args) -> int:
    config = load_config(args.workspace)
    tasks_dir = config.tasks_dir
    slugs = sorted(p.name for p in tasks_dir.iterdir()) if tasks_dir.exists() else []
    _emit(args, {"tasks": slugs}, "\n".join(slugs) if slugs else "(no tasks)")
    return 0


def _ensure_goal(paths: TaskPaths, text: str | None) -> tuple[str | None, str | None]:
    """Resolve the ``run``/``init`` positional into ``(error, brief)``.

The discriminator is whether ``goal.md`` has content, not whether this is the
first ``run``: a lane scaffolded by ``init`` has an empty ``goal.md`` and
still needs its goal, and a lane that has one is offering the slot as a brief
from the owner. A lane whose goal is the empty string and whose argument
differs is therefore a *goal*, not a brief, so the emptiness check has to
come first: `init` leaves the file empty, and testing a new lane's argument
against it for a difference first refuses the goal outright and then
dead-ends the alternative it suggests.

Text equal to the goal is ignored, not injected. Passing the goal back is
the shape of a wrapper that forwards its configuration blindly, and the
owner repeating their own brief back at the operator is noise, not signal.
"""
    if paths.goal.exists():
        existing = paths.goal.read_text(encoding="utf-8").strip()
        if not existing:
            # Scaffolded but never given a goal: the argument is the goal.
            if not text or not text.strip():
                return (
                    f"goal-prompt at {paths.goal} is empty; write the goal there "
                    "or pass it as the second argument",
                    None,
                )
            paths.goal.write_text(text.strip() + "\n", encoding="utf-8")
            return None, None
        if text is None or not text.strip() or text.strip() == existing:
            return None, None
        return None, text.strip()
    if not text or not text.strip():
        return f"new task: pass the goal as the second argument (no {paths.goal} yet)", None
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.goal.write_text(text.strip() + "\n", encoding="utf-8")
    return None, None


def _make_operator_invoker(config: Config, op_config: OperatorConfig):
    """Build the operator invoker: one-shot CLI call, capture the final text.

    Uses the operator provider (may differ from the managed provider) and runs
    its default agent -- or ``[operator].agent`` when set. The persona is
    prepended to every operator prompt, so one source of truth serves all
    providers and a workspace's own agent still gets the guidance. Permissions
    are the workspace owner's; the harness only forwards
    ``[operator].model``/``extra_args``. The persona is read once, here.

    The call is bounded by ``[operator].timeout_s``, not the managed turn's
    ceiling, and a timeout yields an empty reply flagged ``timed_out``
    (``OperatorReply``), so the loop tells a killed call from silence instead
    of inferring a kill from the emptiness. The loop owns what happens next;
    this layer only launches.

    The output goes to this call's log file, declared by the loop immediately
    before the call (``logs.set_current``), so an operator that hangs is
    tailable, and the verdict is parsed out of that file. That is the same text
    the CLI emitted, read a moment later rather than off a pipe; the loop
    redacts the file afterwards, after parsing. See logs.py.
    """
    provider: Provider = get_provider(op_config.provider)
    persona = load_persona()
    base_extra: tuple[str, ...] = ("--agent", op_config.agent) if op_config.agent else ()

    def invoke(prompt: str) -> str:
        prompt = f"{persona}\n\n{prompt}"
        extra: list[str] = list(base_extra)
        if op_config.model:
            extra += ["--model", op_config.model]
        extra += list(op_config.extra_args)
        spec = SpawnSpec(
            prompt=prompt,
            cwd=config.workspace_root,
            resume_native_id=None,
            timeout_s=op_config.timeout_s,
            extra_args=tuple(extra),
        )
        cmd = spawn.resolve_argv(provider.build_command(spec))
        call = spawn.run_with_timeout(
            cmd,
            cwd=config.workspace_root,
            input=prompt,
            timeout_s=op_config.timeout_s,
            log_path=logs.current(),
        )
        if call.timed_out:
            print(
                f"[warn] task: operator call hit its {op_config.timeout_s}s timeout "
                "and its process tree was terminated",
                file=sys.stderr,
            )
            return OperatorReply("", timed_out=True)
        # The operator's verdict JSON is inside its reply, so pull the assistant
        # text out of the event stream rather than handing back raw events.
        # Read off disk with the same substitution the stream was launched
        # with, so a character cut at a write boundary degrades rather than
        # raising -- which matters here, where an unreadable reply costs a whole
        # operator call out of the retry budget.
        return provider.extract_text(_read_output(logs.current(), call))

    return invoke


def _read_output(log_path, call) -> str:
    """The finished call's output text.

    Off the log file when the loop declared one; otherwise whatever the spawn
    captured in memory, so an invoker built by a frontend that never declares a
    log still works.

    ``errors="replace"`` matches the encoding the stream was launched with, so
    a multibyte character cut at a write boundary degrades exactly as it would
    have on the pipe. A log that cannot be read yields "", which the loop already
    treats as an unusable reply and answers with a re-ask -- an artifact the
    harness cannot produce must not end the run.
    """
    if log_path is None:
        return call.stdout or ""
    try:
        return log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _emit(args, payload: dict, text: str) -> None:
    if args.format == "json":
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(text)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
