"""Thin CLI wrapper over the loop library.

Subcommands: init (scaffold a task lane), run (drive a task), takeover, status,
list, register-workspace, daemon, setup-caddy (write ~/.backseat/caddyfile).
Every command supports ``--format json`` so a
parent workflow can wait on the process and parse machine-readable output. The
CLI wires real subprocess runners for the managed and operator agents; the loop
itself stays injectable.

``run <slug> [GOAL|BRIEF]`` reads its second argument two ways. On a lane with no
goal-prompt yet it is the goal, and it is required. On a lane that already has
one it is a brief from the task owner for this run only: it rides every
operator prompt of the run, outranks the goal-prompt where they conflict, and
ends with the run. That is how an ``ask_human`` pause gets answered without a
separate verb -- and it is why a lane that is blocked or complete refuses a bare
resume.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import psutil

from backseat_harness import __version__, db, exitwatch, logs, manager, progress, spawn, storage
from backseat_harness import platform as platform_mod
from backseat_harness.config import Config, OperatorConfig, TaskPaths, is_task_lane, load_config
from backseat_harness.daemon import Daemon
from backseat_harness.loop import OperatorReply, run_task
from backseat_harness.models import Binding, TaskStatus
from backseat_harness.operator import load_persona
from backseat_harness.providers import get_provider
from backseat_harness.providers.base import Provider, ProviderError, SpawnSpec
from backseat_harness.spawn import run_turn


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    progress.set_debug(getattr(args, "debug", False))
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


#: Values ``daemon run --web`` accepts; ``none`` is decided here, before any web import.
_WEB_MODES = ("none", "auto", "direct", "caddy")


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
        "has one, a brief from you added to this run's operator prompts. Required "
        "for a new task, and for one that is blocked or complete",
    )
    run.add_argument(
        "-p",
        "--prompt",
        help="override the first managed prompt (default: the operator plans first)",
    )
    run.add_argument(
        "--interrupt",
        action="store_true",
        help="if this lane is already running, stop it (its harness and providers "
        "only) and run with the brief instead of refusing",
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

    register = sub.add_parser(
        "register-workspace",
        help="add a workspace dir to the registry the daemon scans",
    )
    register.add_argument(
        "path", help="workspace directory to register (or drop with --unregister)"
    )
    register.add_argument(
        "--unregister",
        action="store_true",
        help="remove this path from the registry instead of adding it",
    )
    register.set_defaults(func=_cmd_register_workspace)

    daemon_parser = sub.add_parser(
        "daemon",
        help="daemon lifecycle: run, install, restart, status, stop",
    )
    daemon_sub = daemon_parser.add_subparsers(dest="daemon_command")

    daemon_run = daemon_sub.add_parser(
        "run",
        help="run the background scheduler in the foreground until interrupted",
    )
    # SUPPRESS so the root --debug value survives when this one is absent;
    # argparse subparser defaults would otherwise overwrite it with False.
    daemon_run.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS,
        help="forward --debug to daemon-spawned runs ([dbg] step lines in "
        "each lane's daemon.log); also accepted before the daemon subcommand",
    )
    # Same SUPPRESS pattern as --debug: absent means simply "no web server",
    # so the root-level value is never clobbered by a subparser default.
    daemon_run.add_argument(
        "--web",
        nargs="?",
        const="auto",
        choices=_WEB_MODES,
        default=argparse.SUPPRESS,
        help="also serve the web interface in this process (host/port from the [web] "
        "table; a refused start exits 2 before the daemon starts). MODE: auto (caddy "
        "when ~/.backseat/caddyfile exists, else direct; bare --web), direct, caddy "
        "(needs the caddyfile), or none (same as no --web)",
    )
    daemon_run.set_defaults(func=_cmd_daemon)

    daemon_install = daemon_sub.add_parser(
        "install",
        help="register the daemon artifact and start it",
    )
    daemon_install.add_argument(
        "--uninstall",
        action="store_true",
        help="remove the artifact instead of installing",
    )
    daemon_install.add_argument(
        "--path-override",
        default=None,
        help="override the frozen PATH at install/reinstall time",
    )
    daemon_install.set_defaults(func=_cmd_daemon_install)

    daemon_restart = daemon_sub.add_parser(
        "restart",
        help="restart the registered daemon",
    )
    daemon_restart.set_defaults(func=_cmd_daemon_restart)

    daemon_status = daemon_sub.add_parser(
        "status",
        help="show the daemon's guard-row and registration status",
    )
    daemon_status.set_defaults(func=_cmd_daemon_status)

    daemon_stop = daemon_sub.add_parser(
        "stop",
        help="stop a registered daemon (does not kill unmanaged ones)",
    )
    daemon_stop.set_defaults(func=_cmd_daemon_stop)

    web = sub.add_parser("web", help="run the local web interface (localhost only)")
    web.add_argument(
        "--host", default=None, help="bind host (localhost only, default: config or 127.0.0.1)"
    )
    web.add_argument(
        "--port", type=int, default=None, help="bind port (default: config or 8090)"
    )
    web.add_argument(
        "--workspace",
        default=argparse.SUPPRESS,
        help="workspace root containing .backseat/ (default: cwd, or -C value)",
    )
    web.add_argument(
        "--mode",
        choices=("auto", "direct", "caddy"),
        default="auto",
        help="auto serves through caddy when ~/.backseat/caddyfile exists, "
        "direct serves plain uvicorn, caddy requires the caddyfile (default: auto)",
    )
    web.set_defaults(func=_cmd_web)

    setup = sub.add_parser(
        "setup-caddy",
        help="install caddy, open the site port, and write ~/.backseat/caddyfile",
    )
    setup.add_argument(
        "--host", default=None, help="site host name (skip the prompt)"
    )
    setup.add_argument(
        "--web-port",
        type=int,
        default=None,
        help="web app port reverse_proxy targets (skip the prompt; matches [web] port)",
    )
    setup.add_argument(
        "--user", default=None, help="basic-auth username (skip the prompt)"
    )
    setup.set_defaults(func=_cmd_setup_caddy)

    gen = sub.add_parser(
        "generate-task",
        help="draft a task's goal.md from a finished chat, then promote it to tasks/",
    )
    gen.add_argument("source_key", help="chat to distil, as provider:conversation_id")
    gen.add_argument(
        "--staging",
        default=None,
        help="an existing _gen-* staging dir under tasks/ (the web route creates it); "
        "default: create one",
    )
    gen.set_defaults(func=_cmd_generate_task)

    return p


def _managed_provider(config: Config, binding: Binding | None) -> Provider:
    """Resolve the managed provider, preferring an existing binding's own.

    A binding records which CLI owns its session chain (``Binding.provider``),
    and resuming that chain under a different CLI fails outright: an opencode
    ``ses_`` id means nothing to ``claude --resume``. So when a binding exists
    and this build still registers its provider, that provider wins -- config
    decides only for a fresh lane (no binding) or a binding whose provider has
    left the registry, where falling back keeps the lane drivable and the
    warning names the mismatch.
    """
    if binding is not None:
        try:
            return get_provider(binding.provider)
        except ProviderError:
            print(
                f"[warn] binding provider {binding.provider!r} is not registered; "
                f"using configured provider {config.provider!r}",
                file=sys.stderr,
            )
    return get_provider(config.provider)


def _cmd_init(args) -> int:
    config = load_config(args.workspace, args.slug)
    # Register the workspace first (goal:3-5): the row must exist before any
    # task lane lives under it, and add_workspace bumps workspaces-version
    # only when this insert actually changes the set -- a repeat init in an
    # already-listed workspace leaves the counter alone.
    with db.open_db() as conn:
        db.add_workspace(conn, config.workspace_root)
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

    provider = _managed_provider(config, storage.load_binding(paths))
    managed_runner = run_turn
    operator_invoker = _make_operator_invoker(config, config.operator)

    # Registry hooks (2-plan §2), all *after* the rc-2 validation gates above
    # so a refused run claims nothing: sweep orphans, then hold this lane's
    # claim for the run (see _lane_claim).
    task_relpath = paths.root.relative_to(config.workspace_root).as_posix()
    with db.open_db() as conn:
        db.sweep_orphans(conn)
    with contextlib.ExitStack() as stack:
        refusal = _claim_or_interrupt(stack, config, task_relpath, args.interrupt)
        if refusal:
            _emit(args, {"slug": args.slug, "error": refusal}, refusal)
            return 2
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


def _claim_or_interrupt(
    stack: contextlib.ExitStack, config: Config, task_relpath: str, interrupt: bool
) -> str | None:
    """Enter this lane's claim on ``stack``; with ``interrupt``, stop a live holder first.

    Returns None once the claim is held, else the refusal to print. A live
    claim without ``interrupt`` refuses at once and stops nothing. With it, the
    holder is stopped through :func:`_stop_lane` and the claim is taken again, so
    a second conflict (someone else claimed in the gap) is still a refusal.
    """
    try:
        stack.enter_context(_lane_claim(config, task_relpath))
        return None
    except db.ClaimConflict as exc:
        if not interrupt:
            return str(exc)
    with db.open_db() as conn:
        refusal = _stop_lane(conn, config.workspace_root, task_relpath)
    if refusal:
        return refusal
    try:
        stack.enter_context(_lane_claim(config, task_relpath))
    except db.ClaimConflict as exc:
        return str(exc)
    return None


@contextmanager
def _lane_claim(config: Config, task_relpath: str) -> Iterator[None]:
    """Hold this lane's registry claim for the body of the ``with``.

    Claims the row first, so a :class:`db.ClaimConflict` surfaces on entry before
    anything is installed. Then every provider spawn is recorded into the row,
    the heartbeat and exitwatch run, and the row is deleted on every exit path,
    exceptions and Ctrl-C included.
    """
    with db.open_db() as conn:
        db.claim_task(
            conn,
            config.workspace_root,
            task_relpath,
            os.getpid(),
        )

        def _note(line: str) -> None:
            print(f"[run] {line}", file=sys.stderr, flush=True)

        # Diagnostics from here to teardown land in stderr, which the daemon
        # redirects into the lane's daemon.log: the claim stamp, every exit
        # signal, and the 60s heartbeat prove which of pid/ppid/providers was
        # still alive right up to a silent death.
        restore_exitwatch = exitwatch.install(_note)
        _note(
            f"start pid={os.getpid()} ppid={os.getppid()} task={task_relpath} "
            f"debug={progress.debug_enabled()}"
        )
        stop_heartbeat = _start_run_heartbeat(config, task_relpath, _note)
        try:
            spawn.set_provider_recorder(
                lambda entries: db.record_provider(
                    conn,
                    config.workspace_root,
                    task_relpath,
                    [pid for pid, _ in entries],
                    [start for _, start in entries],
                )
            )
            yield
        finally:
            stop_heartbeat()
            # Clear first: a spawn during teardown must not write through a
            # connection that release_task is about to make stale.
            spawn.set_provider_recorder(None)
            _note("teardown: releasing task row")
            db.release_task(conn, config.workspace_root, task_relpath)
            _note("teardown: row released")
            restore_exitwatch()


def _start_run_heartbeat(
    config: Config, task_relpath: str, note: Callable[[str], None]
) -> Callable[[], None]:
    """Stamp liveness every 60s until the returned stop() is called.

    The run's log can legitimately go silent for the length of a provider
    call (up to the operator ceiling), so this is the run's half of the
    daemon's heartbeat: pid, parent (did the daemon go first?), and which
    recorded providers still answer as live.
    """
    stop = threading.Event()
    started = time.monotonic()

    def _beat() -> None:
        while not stop.wait(60.0):
            try:
                ppid = os.getppid()
                ppid_live = psutil.pid_exists(ppid)
                with db.open_db() as conn:
                    row = db.get_task(conn, config.workspace_root, task_relpath)
                live = (
                    [p for p, s in db.provider_entries(row) if db.pid_live(p, s)]
                    if row
                    else []
                )
            except Exception as exc:  # noqa: BLE001 - a failed beat still reports
                note(f"heartbeat: failed ({exc})")
                continue
            note(
                f"heartbeat uptime={int(time.monotonic() - started)}s "
                f"pid={os.getpid()} ppid={ppid} ppid_live={ppid_live} "
                f"providers_live={live}"
            )

    threading.Thread(target=_beat, daemon=True).start()
    return stop.set


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


def _stop_lane(conn, workspace: Path, task_relpath: str) -> str | None:
    """Stop this lane's harness and providers, then wait until they are dead.

    Returns None once the lane is stopped or was never running, else the refusal
    to print. The harness goes first so it cannot spawn another provider. The
    providers are the ones known before the harness kill, plus any the harness
    recorded before it died, re-read from the row. Only confirmed pid+start
    pairs are killed or waited on; :func:`db.kill_lane_entry` refuses the rest.
    A harness with no recorded start that still answers to its pid is waited on
    too, so an unidentifiable live harness refuses here rather than being taken
    over under.
    """
    grace = _KILL_GRACE_S
    row = db.get_task(conn, workspace, task_relpath)
    if row is None or not db.pid_live(row["harness_pid"], row["harness_start"]):
        return None
    harness = (row["harness_pid"], row["harness_start"])
    providers = db.provider_entries(row)
    db.kill_lane_entry(conn, *harness)
    after = db.get_task(conn, workspace, task_relpath)
    if after is not None:
        providers += [e for e in db.provider_entries(after) if e not in providers]
    for pid, start in providers:
        db.kill_lane_entry(conn, pid, start)

    def _stopped() -> bool:
        if db.pid_live(*harness):
            return False
        return not any(db.pid_live(pid, start) for pid, start in providers if start is not None)

    if _wait_for(_stopped, grace):
        return None
    slug = task_relpath.rsplit("/", 1)[-1]
    return f"lane {slug} still running after {grace:g}s; not taking over"


def _takeover_target(config: Config, binding: Binding | None) -> tuple[Provider, str | None]:
    """The provider and session a takeover hands the human.

    Walks back to the newest generation that captured a native id and opens that
    id under the provider that generation ran under, so a session is never paired
    with another CLI's id. A generation whose provider is no longer registered,
    and a lane with no id anywhere, open a fresh session under the configured
    provider; the unregistered case also warns. The resume that follows does not
    use this pairing: it keeps ``_managed_provider``.
    """
    if binding is not None:
        for gen in reversed(binding.generations):
            if gen.native_id:
                try:
                    return get_provider(gen.provider or binding.provider), gen.native_id
                except ProviderError:
                    print(
                        f"[warn] takeover: generation {gen.n} ran under {gen.provider!r}, "
                        "which is not registered; opening a fresh session under "
                        f"configured provider {config.provider!r}",
                        file=sys.stderr,
                    )
                    break
    return get_provider(config.provider), None


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

    task_relpath = paths.root.relative_to(config.workspace_root).as_posix()
    with db.open_db() as conn:
        msg = _stop_lane(conn, config.workspace_root, task_relpath)
    if msg:
        _emit(args, {"slug": args.slug, "error": msg}, msg)
        return 2

    # Hold the claim from here through the handover and the resume, so a daemon
    # check-in or a bare run sees a live lane and cannot start a second harness.
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(_lane_claim(config, task_relpath))
        except db.ClaimConflict:
            msg = "lane was restarted; retry"
            _emit(args, {"slug": args.slug, "error": msg}, msg)
            return 2

        binding = storage.load_binding(paths)
        provider = _managed_provider(config, binding)
        handoff, native_id = _takeover_target(config, binding)

        where = f"session {native_id}" if native_id else "a fresh session"
        print(f"Handing you {handoff.name} ({where}). Advance it, then quit to resume.")
        code = spawn.run_interactive(handoff, native_id, config.workspace_root)
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
    slugs = (
        sorted(p.name for p in tasks_dir.iterdir() if is_task_lane(p))
        if tasks_dir.exists()
        else []
    )
    _emit(args, {"tasks": slugs}, "\n".join(slugs) if slugs else "(no tasks)")
    return 0


def _cmd_daemon(args) -> int:
    """Run the background daemon until it is stopped (2-plan §3).

    The single-instance guard is claimed inside ``Daemon.run`` before anything
    is scheduled: a live daemon already out there is refused with exit 2, the
    same channel as the other pre-run refusals, and the running instance is
    left untouched. Ctrl-C is the normal way to stop — by the time it surfaces
    here the ``finally`` in ``run`` has already joined the watchers and
    released the guard, so both exits report the same stopped payload.

    ``--web`` (2-plan A/3a) also serves the web interface in this process, on
    a background thread started before the guard is claimed: a bad bind
    (non-localhost host, busy port) is refused with exit 2 and no daemon.
    uvicorn only installs signal handlers on the main thread, so Ctrl-C keeps
    reaching this handler chain and the web thread is joined and stopped here.
    """
    instance = Daemon(debug=getattr(args, "debug", False))
    web_handle = None
    caddy_handle = None
    requested = getattr(args, "web", "none")
    mode = "none"
    # What the post-claim try turns into exit 2; CaddyRefusal joins it once the
    # lazy web import below has run (only --web can raise it).
    refusals: tuple[type[Exception], ...] = (db.DaemonAlreadyRunning,)
    if requested != "none":
        # Lazily: the daemon process without --web never loads the web stack.
        from backseat_harness import config as config_mod
        from backseat_harness.web import caddy
        from backseat_harness.web.cli import start_web_in_thread

        refusals = (db.DaemonAlreadyRunning, caddy.CaddyRefusal)
        config = config_mod.load_config(args.workspace)
        caddyfile = caddy.caddyfile_path()
        extra_hosts: tuple[str, ...] = ()
        # Every check that can refuse runs here, before the guard is claimed.
        try:
            mode = caddy.resolve_mode(requested, caddyfile)
            if mode == "caddy":
                exe, extra_hosts = _caddy_checks(caddyfile)
            web_handle = start_web_in_thread(
                host=config.web.host, port=config.web.port, extra_hosts=extra_hosts
            )
        except (OSError, ValueError) as exc:
            _emit(args, {"error": str(exc)}, f"web: refused to start: {exc}")
            return 2

    def start_caddy() -> None:
        # Runs after the claim (Daemon.run's on_claimed), so a refused second
        # daemon never spawns a caddy that fights the live one.
        nonlocal caddy_handle
        caddy_handle = caddy.start(
            exe, caddyfile, log_path=caddyfile.parent / "caddy.log", on_exit=_caddy_exited
        )

    def _caddy_exited(rc: int) -> None:
        # Keep the daemon up: uvicorn is loopback-only, so losing caddy costs
        # remote access, not safety. The warning is the operator's signal.
        logging.getLogger("daemon").warning(
            "caddy exited rc=%s; the web interface keeps running without it", rc
        )

    def stop_caddy() -> None:
        # Runs in Daemon.run's finally, before the guard is cleared, so the site
        # port is free by the time a successor can claim the guard.
        if caddy_handle is not None:
            caddy_handle.stop()

    try:
        instance.run(
            on_claimed=start_caddy if mode == "caddy" else None,
            on_release=stop_caddy,
        )
    except refusals as exc:
        _emit(args, {"error": str(exc)}, str(exc))
        return 2
    except KeyboardInterrupt:
        pass
    finally:
        # uvicorn stops after caddy, as before.
        if web_handle is not None:
            web_handle.stop()
    _emit(args, {"daemon": "stopped"}, "daemon stopped")
    return 0


# The guard row is the only record of the daemon that is actually running, so
# both directions are confirmed against it rather than against a scheduler's
# return code: `schtasks /run` returns as soon as the request is accepted, and
# `schtasks /end` reports success even when the task has no running instance
# to end. A start only counts once something claimed the guard; a stop only
# counts once that pid stops being live.
_START_GRACE_S = 10.0
_STOP_GRACE_S = 5.0
#: Budget for the direct kill that follows an /end that reached no daemon.
_KILL_GRACE_S = 5.0


def _wait_for(check, grace_s: float):
    """Poll ``check`` until it returns a truthy answer, or None when time is up.

    Re-read rather than sampled once because both transitions land
    asynchronously: ``/end`` tears a tree down and the daemon's ``finally``
    clears the row a moment later, while a fresh daemon has to boot before it
    can claim. A check that raises reads as "no answer yet" rather than
    propagating, so an unreadable guard cannot wedge the command that exists
    to report on it.
    """
    deadline = time.monotonic() + grace_s
    while True:
        try:
            answer = check()
        except Exception:
            return None
        if answer:
            return answer
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


def _wait_new_guard_owner(pid, start, grace_s: float) -> dict | None:
    """The first live guard owner that is not ``(pid, start)``.

    Evidence of a real start, since a restart that leaves the same live daemon
    in place has restarted nothing: the fresh instance died refusing a guard
    it could not take (``DaemonAlreadyRunning``, exit 2) while ``/run`` still
    returned 0.
    """

    def check() -> dict | None:
        with db.open_db() as conn:
            guard = db._daemon_record(conn)
        if guard is None or not db.pid_live(guard["pid"], guard.get("start")):
            return None
        if (guard["pid"], guard.get("start")) == (pid, start):
            return None
        return guard

    return _wait_for(check, grace_s)


def _wait_guard_gone(pid, start, grace_s: float) -> bool:
    """True once ``(pid, start)`` no longer owns a live daemon."""
    return _wait_for(lambda: not db.pid_live(pid, start), grace_s) is not None


def _cmd_daemon_install(args) -> int:
    original_path = os.environ.get("PATH")
    if args.path_override:
        os.environ["PATH"] = args.path_override

    foreign_warning = None
    guard_before = None
    try:
        with db.open_db() as conn:
            guard = db._daemon_record(conn)
        guard_before = guard
        if guard is not None and db.pid_live(guard.get("pid"), guard.get("start")):
                registered = platform_mod.is_registered()
                foreign_warning = (
                    f"daemon is already live (pid={guard.get('pid')}); "
                    f"install does not kill a running daemon; "
                    f"ownership undeterminable (guard={guard.get('pid')}) — "
                    f"if it was not started by this registration, use the "
                    f"owning registration's `daemon restart` or `stop`; "
                    f"otherwise foreground Ctrl-C to reclaim"
                    f"{' (stale artifact exists)' if registered else ''}"
                )
    except Exception:
        pass

    try:
        if args.uninstall:
            platform_mod.uninstall()
            payload = {"uninstalled": True, "path_override": args.path_override}
            text = "daemon uninstalled"
        else:
            platform_mod.register()
            start_ok = True
            start_err = None
            try:
                platform_mod.start()
            except Exception as exc:
                start_ok = False
                start_err = str(exc)
            else:
                # The scheduler accepting the request says nothing about the
                # task's action running: the wrapper can be missing or blocked,
                # and the fresh instance can die refusing a guard someone else
                # holds, all long after /run returned 0. Only a new owner of
                # the guard row proves the daemon came up.
                owner = _wait_new_guard_owner(
                    guard_before.get("pid") if guard_before else None,
                    guard_before.get("start") if guard_before else None,
                    _START_GRACE_S,
                )
                if owner is None:
                    start_ok = False
                    start_err = (
                        "scheduler accepted the start but no daemon claimed the "
                        "guard; the task instance did not stay up"
                    )
            linger_text = None
            try:
                user = os.environ.get("USER") or os.environ.get("USERNAME", "user")
                result = subprocess.run(
                    ["loginctl", "show-user", user, "--property=Linger"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                output = (result.stdout or "")
                if "Linger=no" in output:
                    linger_text = (
                        f"linger is disabled for {user}; "
                        f"enable with: loginctl enable-linger {user}"
                    )
                elif "Linger=yes" in output:
                    linger_text = f"linger enabled for {user}"
            except Exception:
                pass

            installed_claim = start_ok and (foreign_warning is None)
            payload = {
                "installed": installed_claim,
                "path_override": args.path_override,
                "start_ok": start_ok,
            }
            text_parts = ["daemon installed" if installed_claim else "daemon install incomplete"]
            if start_err:
                payload["start_error"] = start_err
                text_parts.append(f"start failed: {start_err}")
            if linger_text:
                text_parts.append(f"[linger] {linger_text}")
            if foreign_warning:
                payload["warn"] = foreign_warning
                text_parts.append(f"[warn] {foreign_warning}")
                installed_claim = False
                payload["installed"] = installed_claim
            text = "; ".join(text_parts)
    finally:
        if original_path is not None:
            os.environ["PATH"] = original_path
        elif args.path_override:
            os.environ.pop("PATH", None)

    _emit(args, payload, text)
    return 0


def _cmd_daemon_restart(args) -> int:
    if not platform_mod.is_registered():
        payload = {"restarted": False, "installed": False, "error": "nothing installed"}
        _emit(args, payload, payload["error"])
        return 2
    with db.open_db() as conn:
        before = db._daemon_record(conn)
    old_pid = before["pid"] if before else None
    old_start = before.get("start") if before else None
    try:
        platform_mod.restart()
    except Exception as exc:
        payload = {"restarted": False, "installed": True, "error": str(exc)}
        _emit(args, payload, f"restart failed: {exc}")
        return 0

    # A restart that left the same live daemon in place has restarted nothing,
    # so the claim is made once a different pid owns the guard -- never on the
    # strength of /end and /run both returning 0.
    owner = _wait_new_guard_owner(old_pid, old_start, _START_GRACE_S)
    if owner is None and old_pid is not None:
        # /run could not start a second daemon while the old one still holds
        # the guard, and the old one survives /end whenever the task's python
        # launcher sits between the scheduler and the daemon. End it directly
        # and ask the task to start again.
        if _end_recorded_daemon(old_pid, old_start, _STOP_GRACE_S):
            try:
                platform_mod.start()
                owner = _wait_new_guard_owner(old_pid, old_start, _START_GRACE_S)
            except Exception as exc:
                payload = {"restarted": False, "installed": True, "error": str(exc)}
                _emit(args, payload, f"restart failed: {exc}")
                return 0
    if owner is None:
        payload = {"restarted": False, "installed": True, "pid": old_pid}
        text = (
            f"restart failed: the previous daemon (pid={old_pid}) could not be "
            f"ended, so the task could not start a new one -- if it was "
            f"installed as Administrator its daemon runs elevated, so re-run "
            f"this command from an Administrator shell"
            if old_pid is not None
            else "restart failed: no new daemon claimed the guard"
        )
        _emit(args, payload, text)
        return 0
    payload = {"restarted": True, "installed": True, "pid": owner["pid"]}
    _emit(args, payload, f"daemon restarted: pid={owner['pid']}")
    return 0


def _cmd_daemon_status(args) -> int:
    with db.open_db() as conn:
        guard = db._daemon_record(conn)
        registered = platform_mod.is_registered()
        artifact_exists = platform_mod.artifact_path().exists()

    live_guard = False
    pid = None
    start = None
    if guard is not None:
        pid = guard.get("pid")
        start = guard.get("start")
        live_guard = db.pid_live(pid, start)

    # Four situations from 2-plan §5 / goal.md line 20-23
    if live_guard:
        owns = registered and artifact_exists
        payload = {
            "running": True,
            "installed": registered or artifact_exists,
            "pid": pid,
            "start": start,
            "uptime": round(time.time() - (start or 0), 2) if start else None,
            "owns": owns,
        }
        if owns:
            text = f"daemon running: pid={pid} start={start} owns={owns}"
        else:
            text = (
                f"unmanaged daemon is live: pid={pid} start={start} "
                f"(not owned by this installation); stop with Ctrl-C"
            )
    elif registered or artifact_exists:
        payload = {"running": False, "installed": True}
        text = "daemon installed but not running"
    else:
        payload = {"running": False, "installed": False}
        text = "daemon not installed"
    _emit(args, payload, text)
    return 0


def _end_recorded_daemon(pid, start, grace_s: float) -> bool:
    """Make the daemon named by the guard row stop, and report whether it did.

    ``schtasks /end`` only ends the task's action tree, and on a uv-managed
    venv the action's python.exe is a launcher that re-execs the real
    interpreter as a *child*: the daemon itself is that grandchild, so ending
    the task leaves it running. The guard row holds the daemon's real pid, so
    that process is what has to go when /end did not reach it. It dies hard
    (no graceful release; the next claim overwrites the stale row), and a
    daemon this shell cannot terminate -- one the task started elevated --
    simply survives, which the caller reports rather than hides.
    """
    if _wait_guard_gone(pid, start, grace_s):
        return True
    spawn.kill_pid_tree(pid, budget_s=_KILL_GRACE_S)
    return _wait_guard_gone(pid, start, grace_s)


def _cmd_daemon_stop(args) -> int:
    registered = platform_mod.is_registered()
    with db.open_db() as conn:
        guard = db._daemon_record(conn)
    live_before = guard is not None and db.pid_live(guard["pid"], guard.get("start"))
    if registered:
        try:
            platform_mod.stop()
        except Exception as exc:
            payload = {"stopped": False, "error": str(exc)}
            _emit(args, payload, f"stop failed: {exc}")
            return 0

        # /end succeeding is not evidence the daemon died: it reports success
        # with no running instance at all, and it cannot end an instance that
        # never was the daemon's parent. The guard row is what has to move.
        if not live_before:
            payload = {"stopped": True, "was_running": False}
            _emit(args, payload, "no daemon running; nothing to stop")
            return 0
        if _end_recorded_daemon(guard["pid"], guard.get("start"), _STOP_GRACE_S):
            payload = {"stopped": True, "pid": guard["pid"]}
            _emit(args, payload, "daemon stopped")
            return 0

        payload = {"stopped": False, "pid": guard["pid"], "start": guard.get("start")}
        text = (
            f"stop failed: the daemon is still live (pid={guard['pid']}); "
            f"`schtasks /end` ended no instance owning it and this shell could "
            f"not terminate it either -- if the task was installed as "
            f"Administrator its daemon runs elevated, so re-run this command "
            f"from an Administrator shell"
        )
        _emit(args, payload, text)
        return 0

    if live_before and guard is not None:
        payload = {
            "stopped": False,
            "unmanaged": True,
            "pid": guard["pid"],
            "start": guard.get("start"),
        }
        text = (
            f"daemon is unmanaged (live pid={guard['pid']} with no "
            f"registration); stop with foreground Ctrl-C instead"
        )
    else:
        payload = {"stopped": False}
        text = "daemon not installed; nothing to stop"
    _emit(args, payload, text)
    return 0


def _cmd_register_workspace(args) -> int:
    """Register (or ``--unregister``) a workspace dir in ``~/.backseat/db.sqlite``.

    The path is resolved to an absolute form before it reaches the db so the
    same workspace can never be listed twice under two spellings — the exact
    normalization ``db.add_workspace``/``db.unregister_workspace`` apply, done
    here too so the emitted payload reports the stored string.

    The version bump is not this command's to manage: the db helpers move
    ``workspaces-version`` exactly when the workspace set changes (2-plan §1),
    so re-registering a listed path is a no-op and unregistering an unlisted
    one succeeds silently — both required invariants (goal:18-22).

    The workspace-shape guard (``.backseat/`` present, 2-plan §5) applies to
    the add form only: registering points at an existing workspace, while
    init/run auto-add covers one being created right now. ``--unregister``
    stays a pure registry edit — a dir whose ``.backseat/`` already vanished
    still needs its row dropped, and the task rows it leaves behind remain
    visible to the pre-run sweep (db.py:243-245).
    """
    target = Path(args.path).expanduser().resolve()
    if args.unregister:
        with db.open_db() as conn:
            removed = db.unregister_workspace(conn, target)
            version = db.workspaces_version(conn)
        text = (
            f"Unregistered workspace {target}"
            if removed
            else f"Workspace {target} is not registered"
        )
        _emit(
            args,
            {"path": str(target), "unregistered": removed, "workspaces_version": version},
            text,
        )
        return 0
    if not (target / ".backseat").is_dir():
        error = f"{target} is not a workspace (no .backseat/ directory there)"
        _emit(args, {"path": str(target), "error": error}, error)
        return 2
    with db.open_db() as conn:
        added = db.add_workspace(conn, target)
        version = db.workspaces_version(conn)
    text = (
        f"Registered workspace {target}"
        if added
        else f"Workspace {target} is already registered"
    )
    _emit(
        args,
        {"path": str(target), "registered": added, "workspaces_version": version},
        text,
    )
    return 0


def _caddy_checks(caddyfile: Path) -> tuple[str, tuple[str, ...]]:
    """Run every caddy pre-run check; return (caddy executable, extra Host/Origin strings).

    Shared by ``daemon run --web`` (before the guard is claimed) and ``web``.
    Raises CaddyRefusal, OSError or ValueError; each caller exits 2 with it.
    """
    from backseat_harness.web import caddy

    exe = caddy.find_caddy()
    hosts, ports = caddy.site_hosts(caddyfile.read_text(encoding="utf-8"))
    caddy.validate(exe, caddyfile)
    caddy.probe_ports(ports)
    return exe, tuple(sorted(hosts))


def _cmd_web(args) -> int:
    """Serve the web app in this process (3e): direct, or behind caddy.

    Caddy mode gets the same pre-run checks as ``daemon run --web``, and caddy
    is stopped before uvicorn. There is no guard here, so a dead caddy stops
    the server and exits 1 rather than leaving a daemon running without it.
    """
    from backseat_harness.web import caddy
    from backseat_harness.web.cli import run_web

    workspace = getattr(args, "workspace", ".")
    config = load_config(workspace)
    host = args.host if args.host is not None else config.web.host
    port = args.port if args.port is not None else config.web.port
    caddyfile = caddy.caddyfile_path()
    try:
        mode = caddy.resolve_mode(args.mode, caddyfile)
        exe, extra_hosts, sidecar = "", (), None
        if mode == "caddy":
            exe, extra_hosts = _caddy_checks(caddyfile)

            def sidecar(on_exit):
                def report(rc: int) -> None:
                    print(f"web: caddy exited rc={rc}; stopping the web server", file=sys.stderr)
                    on_exit(rc)

                return caddy.start(exe, caddyfile, log_path=None, on_exit=report)

        return run_web(
            host=host, port=port, workspace=workspace, extra_hosts=extra_hosts, sidecar=sidecar
        )
    except (caddy.CaddyRefusal, OSError, ValueError) as exc:
        _emit(args, {"error": str(exc)}, f"web: refused to start: {exc}")
        return 2


def _cmd_setup_caddy(args) -> int:
    """Interactive caddy setup: install, open the port, write ~/.backseat/caddyfile.

    Lazily imported so the web stack stays out of every other subcommand, and so
    an existing wheel without this module still runs the rest of the CLI. A
    refusal (unelevated Windows, no caddy/winget, a validation failure) exits 2
    with the message, like the other pre-run refusals.
    """
    from backseat_harness.web.caddy_setup import ConsolePrompter, SetupRefusal, run_setup

    try:
        result = run_setup(
            prompter=ConsolePrompter(),
            site_host=args.host,
            web_port=args.web_port,
            username=args.user,
        )
    except SetupRefusal as exc:
        _emit(args, {"error": str(exc)}, str(exc))
        return 2
    payload = {
        "caddyfile": str(result.caddyfile),
        "site_hosts": list(result.site_hosts),
        "url": result.url,
        "username": result.username,
        "caddy_installed": result.caddy_installed,
        "firewall_updated": result.firewall_updated,
    }
    text = (
        f"Caddy setup complete ({result.caddyfile}).\n"
        "Next:\n"
        "  backseat-harness daemon install\n"
        "  backseat-harness daemon restart\n"
        f"Then open {result.url} and sign in as {result.username}."
    )
    _emit(args, payload, text)
    return 0


def _cmd_generate_task(args) -> int:
    """Draft a task's goal.md from a finished chat, then promote it to ``tasks/<slug>/``.

    Exit 0 when promoted, 1 when parked (the reason is in the payload and in the
    staging manifest, and the staging dir is kept), 2 for bad arguments. An
    unregistered workspace is refused, not registered here: registering a
    workspace is the user's act.
    """
    config = load_config(args.workspace)
    source_key = args.source_key
    provider, _, conversation_id = source_key.partition(":")
    if not (provider and conversation_id):
        return _generate_refused(args, f"source key {source_key!r} is not provider:conversation_id")
    with db.open_db() as conn:
        registered = {Path(path).resolve() for path in db.list_workspaces(conn)}
    if config.workspace_root.resolve() not in registered:
        return _generate_refused(
            args, f"{config.workspace_root} is not a registered workspace; register it first"
        )
    if args.staging is None:
        staging = manager.new_staging(config.tasks_dir, source_key, str(config.workspace_root))
    else:
        staging = config.tasks_dir / args.staging
        if (
            staging.parent != config.tasks_dir
            or not staging.name.startswith(manager.STAGING_PREFIX)
            or manager.read_manifest(staging) is None
        ):
            return _generate_refused(args, f"{args.staging!r} is not a staging dir under tasks/")
    # The child is the sole writer of its pid after spawn , so the record
    # names the process that does the work, not the route that launched it.
    manifest = manager.read_manifest(staging) or {}
    manifest.update(pid=os.getpid(), pid_start=db.process_start(os.getpid()))
    manager.write_manifest(staging, manifest)

    invoker = _make_oneshot_invoker(
        config, config.manager_profile(), load_persona("manager_persona.md"), role="manager"
    )
    result = manager.run_generation(config, staging, source_key, invoker)
    slug: str | None = None
    reason = result.reason
    if result.approved:
        try:
            slug = manager.promote(staging, config.tasks_dir)
        except manager.PromotionParked as park:
            reason = park.reason
    if slug is None:
        _emit(
            args,
            {"status": "failed", "final_slug": None, "reason": reason, "staging": str(staging)},
            f"Generation parked: {reason}\nStaging kept at {staging}",
        )
        return 1
    _emit(
        args,
        {"status": "promoted", "final_slug": slug, "reason": "", "staging": None},
        f"Promoted task '{slug}' at {config.task_dir(slug)}",
    )
    return 0


def _generate_refused(args, error: str) -> int:
    _emit(args, {"status": "error", "error": error}, error)
    return 2


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
    """Build the operator invoker: the shared one-shot call, with the operator persona.

    The operator's persona is read once, here, and the invoker is the shared
    one-shot path (``_make_oneshot_invoker``) that the generate-task manager uses
    too, with its own persona and profile.
    """
    return _make_oneshot_invoker(config, op_config, load_persona(), role="operator")


def _make_oneshot_invoker(
    config: Config, profile: OperatorConfig, persona: str, *, role: str = "operator"
):
    """Build a one-shot CLI invoker: one call, capture the final text.

    Uses ``profile``'s provider (may differ from the managed provider) and runs
    its default agent -- or ``profile.agent`` when set. The persona is prepended
    to every prompt, so one source of truth serves all providers and a
    workspace's own agent still gets the guidance. Permissions are the
    workspace owner's; the harness only forwards ``profile.model``/
    ``extra_args``.

    The call is bounded by ``profile.timeout_s``, not the managed turn's
    ceiling, and a timeout yields an empty reply flagged ``timed_out``
    (``OperatorReply``), so the caller tells a killed call from silence instead
    of inferring a kill from the emptiness. The caller owns what happens next;
    this layer only launches. ``role`` names the caller in the timeout warning.

    The output goes to this call's log file, declared by the caller immediately
    before the call (``logs.set_current``), so a call that hangs is tailable, and
    the reply is parsed out of that file. That is the same text the CLI emitted,
    read a moment later rather than off a pipe. See logs.py.
    """
    provider: Provider = get_provider(profile.provider)
    base_extra: tuple[str, ...] = ("--agent", profile.agent) if profile.agent else ()

    def invoke(prompt: str) -> str:
        prompt = f"{persona}\n\n{prompt}"
        extra: list[str] = list(base_extra)
        if profile.model:
            extra += ["--model", profile.model]
        extra += list(profile.extra_args)
        spec = SpawnSpec(
            prompt=prompt,
            cwd=config.workspace_root,
            resume_native_id=None,
            timeout_s=profile.timeout_s,
            extra_args=tuple(extra),
        )
        cmd = spawn.resolve_argv(provider.build_command(spec))
        log_path = logs.current()
        call = spawn.run_with_timeout(
            cmd,
            cwd=config.workspace_root,
            input=prompt,
            timeout_s=profile.timeout_s,
            log_path=log_path,
        )
        # The stored session id is on the output we already hold (the adapter
        # parses it for a managed turn too). Report it before anything can park
        # or fail the call: a generate-task run that ends timed-out or
        # out-of-scope still leaves a readable manager transcript behind.
        output = _read_output(log_path, call)
        stdout, stderr = (
            (output, output) if log_path is not None else (call.stdout, call.stderr)
        )
        native = provider.parse_result(
            -1 if call.timed_out else call.returncode, stdout, stderr
        )
        if native:
            spawn.record_session(provider.history_provider, native)
        if call.timed_out:
            print(
                f"[warn] task: {role} call hit its {profile.timeout_s}s timeout "
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
        return provider.extract_text(output)

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
