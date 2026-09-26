"""Thin CLI wrapper over the loop library.

Subcommands: init (scaffold a task lane), run (drive a task), status, list.
Every command supports ``--format json`` so a parent workflow can wait on the
process and parse machine-readable output. The CLI wires real subprocess
runners for the managed and system agents; the loop itself stays injectable.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from backseat_harness import __version__, spawn
from backseat_harness.config import Config, SystemAgentConfig, TaskPaths, load_config
from backseat_harness.loop import run_task
from backseat_harness.providers import get_provider
from backseat_harness.providers.base import OperatorLaunch, Provider, SpawnSpec
from backseat_harness.spawn import run_turn
from backseat_harness.system_agent import load_persona


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
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
    sub = p.add_subparsers(dest="command")

    init = sub.add_parser("init", help="scaffold a task lane under .backseat/tasks/")
    init.add_argument("slug", help="task slug (filesystem-safe)")
    init.add_argument("-m", "--meta", help="meta-prompt text; omit to create an empty meta.md")
    init.set_defaults(func=_cmd_init)

    run = sub.add_parser(
        "run",
        help="drive a task lane (creates it when --meta is given for a new slug)",
    )
    run.add_argument("slug")
    run.add_argument(
        "-m",
        "--meta",
        help="meta-prompt (the goal); required the first time a task is run",
    )
    run.add_argument(
        "-p",
        "--prompt",
        help="override the first managed prompt (default: the system agent plans first)",
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
    config = load_config(args.workspace)
    paths = config.task_paths(args.slug)
    paths.root.mkdir(parents=True, exist_ok=True)
    if not paths.meta.exists():
        paths.meta.write_text((args.meta or "") + "\n", encoding="utf-8")
    if not paths.checklist.exists():
        paths.checklist.write_text('{\n  "steps": {},\n  "remarks": []\n}\n', encoding="utf-8")
    _emit(
        args,
        {"initialized": args.slug, "path": str(paths.root)},
        f"Initialized task '{args.slug}' at {paths.root}",
    )
    return 0


def _cmd_run(args) -> int:
    config = load_config(args.workspace)
    paths = config.task_paths(args.slug)
    error = _ensure_meta(paths, args.meta)
    if error:
        _emit(args, {"slug": args.slug, "error": error}, error)
        return 2

    provider = get_provider(config.provider)
    managed_runner = run_turn
    system_invoker = _make_system_invoker(config, config.system_agent)

    result = run_task(
        slug=args.slug,
        config=config,
        provider=provider,
        managed_runner=managed_runner,
        system_invoker=system_invoker,
        first_prompt=args.prompt,
    )
    _emit(args, result.to_dict(), _result_line(result))
    return 0 if result.status.value in {"done", "paused"} else 1


def _result_line(result) -> str:
    """Final human-readable line for a finished ``run_task``.

    On ``done`` the reason is the operator's last note, already echoed by the
    stderr progress line, so it is omitted. ``paused`` keeps it (the question
    for the human, which progress truncated) and so does ``failed`` (budget
    guards like "max_iterations reached" are not printed anywhere else).
    """
    line = f"{result.slug}: {result.status.value} after {result.iterations} iterations"
    if result.status.value != "done" and result.reason:
        line += f" ({result.reason})"
    return line


def _cmd_takeover(args) -> int:
    """Human takeover: drive the managed session by hand, then resume the loop.

    This is how you answer an ``ask_human`` pause. The harness blocks on the
    interactive CLI; when you quit, the operator re-plans from the session you
    just advanced (unless --no-resume), so your clarifications land as the
    managed agent's own history rather than as harness text.
    """
    from backseat_harness import storage

    config = load_config(args.workspace)
    paths = config.task_paths(args.slug)
    if not paths.meta.exists():
        _emit(args, {"slug": args.slug, "error": "unknown task"}, f"No task '{args.slug}'")
        return 2

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
        system_invoker=_make_system_invoker(config, config.system_agent),
        first_prompt=None,
    )
    _emit(args, result.to_dict(), _result_line(result))
    return 0 if result.status.value in {"done", "paused"} else 1


def _cmd_status(args) -> int:
    from backseat_harness import storage

    config = load_config(args.workspace)
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
    _emit(
        args,
        payload,
        f"{state.slug}: {state.status.value} | iter {state.iteration} | gen {state.generation}",
    )
    return 0


def _cmd_list(args) -> int:
    config = load_config(args.workspace)
    tasks_dir = config.tasks_dir
    slugs = sorted(p.name for p in tasks_dir.iterdir()) if tasks_dir.exists() else []
    _emit(args, {"tasks": slugs}, "\n".join(slugs) if slugs else "(no tasks)")
    return 0


def _ensure_meta(paths: TaskPaths, meta: str | None) -> str | None:
    """Create the task lane from ``--meta`` if needed. Returns an error or None.

    A lane's goal is fixed once written: a different ``--meta`` for an existing
    lane is an error rather than a silent rewrite (edit meta.md deliberately).
    """
    if paths.meta.exists():
        existing = paths.meta.read_text(encoding="utf-8").strip()
        if meta is not None and meta.strip() != existing:
            return (
                f"task already has a different meta-prompt at {paths.meta}; "
                "edit that file or drop --meta"
            )
        if not existing:
            return f"meta-prompt at {paths.meta} is empty; write the goal there or pass --meta"
        return None
    if not meta or not meta.strip():
        return f"new task: pass the goal with --meta (no {paths.meta} yet)"
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.meta.write_text(meta.strip() + "\n", encoding="utf-8")
    return None


def _make_system_invoker(config: Config, sa_config: SystemAgentConfig):
    """Build the system-agent invoker: one-shot read-only CLI, capture final text.

    Uses the system-agent provider (may differ from the managed provider). Unless
    config names its own agent, the provider writes the harness's read-only
    operator profile (persona + permissions) under ``.backseat/agents/`` once,
    here at startup, and every call launches under it.
    """
    provider: Provider = get_provider(sa_config.provider)
    if sa_config.agent:
        launch = OperatorLaunch(env={}, extra_args=("--agent", sa_config.agent))
    else:
        launch = provider.prepare_operator(
            config.agents_dir / provider.name,
            load_persona(),
            list(sa_config.verify_commands),
        )
    env = {**os.environ, **launch.env} if launch.env else None

    def invoke(prompt: str) -> str:
        extra: list[str] = list(launch.extra_args)
        if sa_config.model:
            extra += ["--model", sa_config.model]
        extra += list(sa_config.extra_args)
        spec = SpawnSpec(
            prompt=prompt,
            cwd=config.workspace_root,
            resume_native_id=None,
            timeout_s=config.turn_timeout_s,
            extra_args=tuple(extra),
        )
        cmd = spawn.resolve_argv(provider.build_command(spec))
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(config.workspace_root),
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=config.turn_timeout_s,
            )
        except subprocess.TimeoutExpired:
            # An unparseable (here: empty) reply becomes a 'problem' verdict
            # upstream, which is the right outcome for an operator that hung.
            return ""
        # The operator's verdict JSON is inside its reply, so pull the assistant
        # text out of the event stream rather than handing back raw events.
        return provider.extract_text(proc.stdout or "")

    return invoke


def _emit(args, payload: dict, text: str) -> None:
    if args.format == "json":
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(text)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
