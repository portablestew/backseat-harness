"""The generate-task flow end to end, through the real CLI and loop.

These tests drive ``cli.main(["generate-task", ...])`` with a scripted manager
(files written and envelopes returned by phase, no provider call), then check the
promoted lane, what the workspace looks like afterwards, and what the first
operator prompt is allowed to contain.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import FakeProvider

from backseat_harness import cli, db, logs, manager
from backseat_harness.config import load_config
from backseat_harness.loop import run_task
from backseat_harness.providers.base import TurnResult
from backseat_harness.web.services import task as task_service

SOURCE_KEY = "claude_code:abc"
#: One assistant-turn sentinel and the full-transcript marker. Neither may reach
#: the first operator prompt. The user line is quoted into goal.md, which is
#: intended (R5), so it is not used as a leak marker.
TRANSCRIPT = (
    "USER: keep it to the token budget guard, not a full quota system\n\n"
    "ASSISTANT: noted, a single guard per run. ZEBRA-SOURCE-7\n\n"
    "USER: yes, that is right\n"
)
GOOD_DRAFT = (
    "# Token guard\n\n"
    "Guard each run with one token budget.\n\n"
    "## Why\n\nKeep spend bounded.\n\n"
    "## User decisions (hard)\n\n"
    "> keep it to the token budget guard, not a full quota system\n\n"
    "Rationale: the user said so.\n\n"
    "## Requirements (high level)\n\n- one guard per run\n\n"
    "## Unresolved\n\n- none\n\n"
    "## Definition of done\n\n- the guard runs once per run\n\n"
    "## Out of scope\n\n- quota system\n"
)


def _reply(phase: str, **fields) -> str:
    return "Done.\n" + json.dumps({"v": 1, "phase": phase, **fields})


def _staging(workspace: Path) -> Path:
    """The one ``_gen-*`` dir the CLI created for this run."""
    [staging] = (workspace / ".backseat" / "tasks").glob(f"{manager.STAGING_PREFIX}*")
    return staging


def _manager_call(workspace: Path, *, approved: bool = True, notes: str = ""):
    """The one manager call: write the draft and the manifest approval, then reply."""

    def step(prompt):
        staging = _staging(workspace)
        (staging / manager.GOAL_DRAFT).write_text(GOOD_DRAFT, encoding="utf-8")
        data = manager.read_manifest(staging)
        data.update(approved=approved, suggested_slug="token-guard")
        manager.write_manifest(staging, data)
        return _reply(
            "generate", approved=approved, suggested_slug="token-guard", reviews=1, notes=notes
        )

    return step


def _scripted(monkeypatch, workspace: Path, *steps) -> list[str]:
    """Route the CLI's manager through the scripted steps; returns the prompts seen."""
    queue = list(steps)
    prompts: list[str] = []

    def invoker(prompt: str) -> str:
        prompts.append(prompt)
        if not queue:
            raise AssertionError("manager was called more times than scripted")
        reply = queue.pop(0)(prompt)
        # The real one-shot invoker records each call's reply in the declared log.
        log = logs.current()
        if log is not None:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(reply, encoding="utf-8")
        return reply

    def make_invoker(config, profile, persona, **kwargs):
        return invoker

    monkeypatch.setattr(cli, "_make_oneshot_invoker", make_invoker)
    real_run = manager.run_generation
    monkeypatch.setattr(
        manager,
        "run_generation",
        lambda config, staging, key, inv, **kw: real_run(
            config, staging, key, inv, load=lambda keys: {k: TRANSCRIPT for k in keys}
        ),
    )
    return prompts


def _register(workspace: Path) -> None:
    with db.open_db() as conn:
        db.add_workspace(conn, workspace)


def _generate(capsys, workspace: Path) -> tuple[int, dict]:
    rc = cli.main(["--format", "json", "-C", str(workspace), "generate-task", SOURCE_KEY])
    return rc, json.loads(capsys.readouterr().out)


def _snapshot(workspace: Path) -> dict[Path, tuple[int, int]]:
    return {
        p: (p.stat().st_size, p.stat().st_mtime_ns)
        for p in workspace.rglob("*")
        if p.is_file()
    }


# --- confirm → generate → a lane with the goal, nothing left behind --------


def test_full_generation_promotes_a_lane_listed_as_pending(workspace, monkeypatch, capsys):
    _register(workspace)
    _scripted(
        monkeypatch, workspace,
        _manager_call(workspace),
    )

    rc, payload = _generate(capsys, workspace)

    assert rc == 0
    assert payload["status"] == "promoted" and payload["final_slug"] == "token-guard"
    lane = workspace / ".backseat" / "tasks" / "token-guard"
    assert (lane / "goal.md").read_text(encoding="utf-8") == GOOD_DRAFT
    assert not list((workspace / ".backseat" / "tasks").glob(f"{manager.STAGING_PREFIX}*"))
    listed = {info.slug: info.status for info in task_service.list_tasks(workspace)}
    assert listed == {"token-guard": "pending"}
    # End to end: the lane keeps the history-qualified session key and nothing else.
    provenance = json.loads((lane / "manager.json").read_text(encoding="utf-8"))
    assert provenance["source_key"] == SOURCE_KEY
    assert provenance["approved"] is True and provenance["status"] == "promoted"
    assert provenance["final_slug"] == "token-guard"
    assert set(provenance) <= set(manager.PROVENANCE_KEYS)


# --- the first operator plans from goal.md alone --------------------------


def test_first_operator_prompt_has_no_source_conversation(
    workspace, monkeypatch, capsys, stub_history
):
    _register(workspace)
    _scripted(
        monkeypatch, workspace,
        _manager_call(workspace),
    )
    rc, payload = _generate(capsys, workspace)
    assert rc == 0
    lane = workspace / ".backseat" / "tasks" / payload["final_slug"]
    assert not (lane / "source.md").exists(), "the large-source file must not be promoted"

    operator_prompts: list[str] = []

    def operator(prompt: str) -> str:
        operator_prompts.append(prompt)
        return '{"verdict":"done"}'

    def runner(provider, spec):
        return TurnResult(
            exit_code=0, timed_out=False, duration_s=0.1,
            native_session_id="sess-1", stdout="", stderr="",
        )

    run_task(payload["final_slug"], load_config(workspace), FakeProvider(), runner, operator)

    plan = operator_prompts[0]
    assert "(no managed session yet)" in plan
    assert "ZEBRA-SOURCE-7" not in plan
    assert SOURCE_KEY not in plan


# --- nothing is written outside the lane or the staging dir ---------------


def test_full_generation_writes_only_inside_its_lane_or_staging(
    workspace, monkeypatch, capsys
):
    _register(workspace)
    before = _snapshot(workspace)
    _scripted(
        monkeypatch, workspace,
        _manager_call(workspace),
    )

    rc, _ = _generate(capsys, workspace)

    assert rc == 0
    after = _snapshot(workspace)
    changed = {p for p in after if before.get(p) != after[p]} | (set(before) - set(after))
    assert changed, "the generation should have written something"
    tasks = workspace / ".backseat" / "tasks"
    allowed_roots = (tasks / "token-guard", *tasks.glob(f"{manager.STAGING_PREFIX}*"))
    outside = sorted(p for p in changed if not any(root in p.parents for root in allowed_roots))
    assert outside == [], f"written outside the lane and staging: {outside}"


# --- park path: manager does not approve, end to end ------------------------------------------


def test_unapproved_draft_parks_through_the_cli_and_keeps_the_staging_dir(
    workspace, monkeypatch, capsys
):
    _register(workspace)
    _scripted(
        monkeypatch, workspace,
        _manager_call(workspace, approved=False, notes="still has minutiae"),
    )

    rc, payload = _generate(capsys, workspace)

    assert rc == 1
    assert payload["status"] == "failed"
    assert payload["reason"] == "manager did not approve the draft: still has minutiae"
    tasks = workspace / ".backseat" / "tasks"
    assert not (tasks / "token-guard").exists()
    assert not list(workspace.rglob("goal.md"))
    staging = _staging(workspace)
    assert manager.read_manifest(staging)["status"] == "failed"
    assert list((staging / "logs").glob("manager.*.log")), "the park keeps its manager logs"
    [row] = task_service.list_generating(workspace)
    assert (row["status"], row["reason"]) == ("failed", payload["reason"])
