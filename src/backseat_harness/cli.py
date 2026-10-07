"""Thin CLI wrapper over the loop library.

Subcommands: init (scaffold a task lane), run (drive a task), takeover, status,
list, register-workspace, daemon. Every command supports ``--format json`` so a
parent workflow can wait on the process and parse machine-readable output. The
CLI wires real subprocess runners for the managed and operator agents; the loop
itself stays injectable.

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
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import psutil

from backseat_harness import __version__, db, exitwatch, logs, progress, spawn, storage
from backseat_harness import platform as platform_mod
from backseat_harness.config import Config, OperatorConfig, TaskPaths, load_config
from backseat_harness.daemon import Daemon
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
        "--port", type=int, default=None, help="bind port (default: config or 8080)"
    )
    web.add_argument(
        "--workspace",
        default=argparse.SUPPRESS,
        help="workspace root containing .backseat/ (default: cwd, or -C value)",
    )
    web.set_defaults(func=_cmd_web)

    return p


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

    provider = get_provider(config.provider)
    managed_runner = run_turn
    operator_invoker = _make_operator_invoker(config, config.operator)

    # Registry hooks (2-plan §2), all *after* the rc-2 validation gates above
    # so a refused run claims nothing: sweep orphans, claim this lane, record
    # every provider spawn into the claimed row, and delete the row before
    # leaving -- on every path, exceptions and Ctrl-C included.
    task_relpath = paths.root.relative_to(config.workspace_root).as_posix()
    with db.open_db() as conn:
        db.sweep_orphans(conn)
        try:
            db.claim_task(
                conn,
                config.workspace_root,
                task_relpath,
                os.getpid(),
            )
        except db.ClaimConflict as exc:
            _emit(args, {"slug": args.slug, "error": str(exc)}, str(exc))
            return 2

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
            result = run_task(
                slug=args.slug,
                config=config,
                provider=provider,
                managed_runner=managed_runner,
                operator_invoker=operator_invoker,
                first_prompt=args.prompt,
                owner_brief=brief,
            )
        finally:
            stop_heartbeat()
            # Clear first: a spawn during teardown must not write through a
            # connection that release_task is about to make stale.
            spawn.set_provider_recorder(None)
            _note("teardown: releasing task row")
            db.release_task(conn, config.workspace_root, task_relpath)
            _note("teardown: row released")
            restore_exitwatch()
    _emit(args, result.to_dict(), _result_line(result))
    return 0 if result.status.value in {"complete", "blocked"} else 1


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

    with db.open_db() as conn:
        for row in db.list_tasks(conn):
            for pid, start in db.provider_entries(row):
                if db.pid_live(pid, start):
                    spawn.kill_pid_tree(pid)

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


def _cmd_daemon(args) -> int:
    """Run the background daemon until it is stopped (2-plan §3).

    The single-instance guard is claimed inside ``Daemon.run`` before anything
    is scheduled: a live daemon already out there is refused with exit 2, the
    same channel as the other pre-run refusals, and the running instance is
    left untouched. Ctrl-C is the normal way to stop — by the time it surfaces
    here the ``finally`` in ``run`` has already joined the watchers and
    released the guard, so both exits report the same stopped payload.
    """
    instance = Daemon(debug=getattr(args, "debug", False))
    try:
        instance.run()
    except db.DaemonAlreadyRunning as exc:
        _emit(args, {"error": str(exc)}, str(exc))
        return 2
    except KeyboardInterrupt:
        pass
    _emit(args, {"daemon": "stopped"}, "daemon stopped")
    return 0


def _cmd_daemon_install(args) -> int:
    original_path = os.environ.get("PATH")
    if args.path_override:
        os.environ["PATH"] = args.path_override

    foreign_warning = None
    try:
        with db.open_db() as conn:
            guard = db._daemon_record(conn)
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
    try:
        platform_mod.restart()
        payload = {"restarted": True, "installed": True}
        text = "daemon restarted"
    except Exception as exc:
        payload = {"restarted": False, "installed": True, "error": str(exc)}
        text = f"restart failed: {exc}"
    _emit(args, payload, text)
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


def _cmd_daemon_stop(args) -> int:
    registered = platform_mod.is_registered()
    with db.open_db() as conn:
        guard = db._daemon_record(conn)
        foreign_live = (
            guard is not None
            and db.pid_live(guard["pid"], guard.get("start"))
        )
    if registered:
        try:
            platform_mod.stop()
            payload = {"stopped": True}
            text = "daemon stopped"
        except Exception as exc:
            payload = {"stopped": False, "error": str(exc)}
            text = f"stop failed: {exc}"
    else:
        if foreign_live and guard is not None:
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


def _cmd_web(args) -> int:
    from backseat_harness.web.cli import run_web

    workspace = getattr(args, "workspace", ".")
    config = load_config(workspace)
    host = args.host if args.host is not None else config.web.host
    port = args.port if args.port is not None else config.web.port
    run_web(host=host, port=port, workspace=workspace)
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
