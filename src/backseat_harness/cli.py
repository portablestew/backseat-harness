"""Thin CLI wrapper over the loop library.

Subcommands: init (scaffold a task lane), run (drive a task), status, list.
Every command supports ``--format json`` so a parent workflow can wait on the
process and parse machine-readable output. The CLI wires real subprocess
runners for the managed and system agents; the loop itself stays injectable.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from backseat_harness import __version__
from backseat_harness.config import Config, SystemAgentConfig, load_config
from backseat_harness.providers import get_provider
from backseat_harness.providers.base import Provider, SpawnSpec
from backseat_harness.spawn import run_turn


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

    run = sub.add_parser("run", help="drive a task lane")
    run.add_argument("slug")
    run.add_argument("-p", "--prompt", help="seed prompt for iteration 1")
    run.set_defaults(func=_cmd_run)

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
    # Imported here so `init`/`status`/`list` work even without chat-mother-forker.
    from backseat_harness.loop import run_task

    config = load_config(args.workspace)
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
    _emit(
        args,
        result.to_dict(),
        f"{result.slug}: {result.status.value} after "
        f"{result.iterations} iterations ({result.reason})",
    )
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


def _make_system_invoker(config: Config, sa_config: SystemAgentConfig):
    """Build the system-agent invoker: one-shot read-only CLI, capture final text.

    Uses the system-agent provider (may differ from the managed provider). The
    read-only profile / model / agent flags come from .backseat/agents config;
    here we pass them through as extra args on a fresh one-shot invocation.
    """
    provider: Provider = get_provider(sa_config.provider)

    def invoke(prompt: str) -> str:
        extra: list[str] = []
        if sa_config.model:
            extra += ["--model", sa_config.model]
        if sa_config.agent:
            extra += ["--agent", sa_config.agent]
        # The reviewer is read-only by profile, but still needs whatever
        # permission flags let it run without blocking on a prompt.
        extra += list(sa_config.extra_args)
        spec = SpawnSpec(
            prompt=prompt,
            cwd=config.workspace_root,
            resume_native_id=None,
            timeout_s=config.turn_timeout_s,
            extra_args=tuple(extra),
        )
        cmd = provider.build_command(spec)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(config.workspace_root),
                input=prompt,
                capture_output=True,
                text=True,
                timeout=config.turn_timeout_s,
            )
        except subprocess.TimeoutExpired:
            # An unparseable (here: empty) reply becomes a 'problem' verdict
            # upstream, which is the right outcome for a reviewer that hung.
            return ""
        # The reviewer's verdict JSON is inside its reply, so pull the assistant
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
