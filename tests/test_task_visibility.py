"""What the workspace listings show, and what they hide.

A lane is listed only when it has a ``goal.md``. A generation's staging dir shows
as one row while it generates, after a park (with the reason), or once its
generator is gone (interrupted). A promoted lane lists like a hand-made one.
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from backseat_harness import db, manager
from backseat_harness.cli import main
from backseat_harness.web.main import create_app
from backseat_harness.web.services import task as task_service

PORT = 8080
SOURCE_KEY = "claude_code:visibility-test"
DRAFT = (
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


def make_staging(workspace: Path, *, status: str, draft: bool = True, **fields) -> Path:
    """A ``_gen-*`` dir as the route or the wrapper leaves it, with ``fields`` merged in."""
    staging = manager.new_staging(workspace / ".backseat" / "tasks", SOURCE_KEY, str(workspace))
    if draft:
        (staging / manager.GOAL_DRAFT).write_text(DRAFT, encoding="utf-8")
    data = manager.read_manifest(staging)
    data.update(status=status, **fields)
    manager.write_manifest(staging, data)
    return staging


def tasks_dir(workspace: Path) -> Path:
    return workspace / ".backseat" / "tasks"


def _client() -> TestClient:
    return TestClient(create_app(port=PORT), base_url=f"http://localhost:{PORT}")


def _register(workspace: Path) -> int:
    with db.open_db() as conn:
        assert db.add_workspace(conn, workspace) is True
        ws_id = db._workspace_id(conn, workspace)
    assert ws_id is not None
    return ws_id


def _no_live_pids(monkeypatch) -> None:
    monkeypatch.setattr(db, "pid_live", lambda *args, **kwargs: False)


# --- hidden: draft-only dirs ----------------------------------------------------


def test_list_tasks_skips_dirs_without_goal_md(workspace):
    make_staging(workspace, status="queued")
    (tasks_dir(workspace) / "half-made").mkdir()

    assert task_service.list_tasks(workspace) == []
    assert task_service.list_incomplete(workspace) == []
    assert task_service.list_recent_task_dirs(workspace) == ([], 0)


def test_a_staging_dir_holding_a_goal_md_is_never_a_lane(workspace):
    # A misbehaving manager could leave a goal.md in a parked dir. It must not list.
    staging = make_staging(workspace, status="failed", reason="review cap reached")
    (staging / "goal.md").write_text("Do the thing.\n", encoding="utf-8")

    assert task_service.list_tasks(workspace) == []
    assert task_service.list_recent_task_dirs(workspace) == ([], 0)


def test_an_empty_goal_md_lane_is_still_listed(workspace):
    # `init` scaffolds an empty goal.md, and that lane must keep listing.
    lane = tasks_dir(workspace) / "fresh"
    lane.mkdir()
    (lane / "goal.md").write_text("", encoding="utf-8")

    assert [info.slug for info in task_service.list_tasks(workspace)] == ["fresh"]
    assert task_service.list_recent_task_dirs(workspace) == (["fresh"], 0)


def test_cli_list_skips_staging_dirs_and_draft_only_lanes(workspace, capsys):
    make_staging(workspace, status="failed", reason="review cap reached")
    (tasks_dir(workspace) / "half-made").mkdir()
    task_service.create_task(workspace, "real", "Do the thing.")

    assert main(["-C", str(workspace), "list"]) == 0
    assert capsys.readouterr().out.strip() == "real"


# --- visible: a promoted lane lists like a hand-made one -------------------------


def test_a_promoted_lane_lists_exactly_like_a_hand_made_one(workspace):
    task_service.create_task(workspace, "hand-made", "Do the thing.")
    staging = make_staging(
        workspace, status="running", approved=True, suggested_slug="token-guard"
    )

    slug = manager.promote(staging, tasks_dir(workspace))

    by_slug = {info.slug: info for info in task_service.list_tasks(workspace)}
    promoted, hand_made = by_slug[slug], by_slug["hand-made"]
    assert (promoted.status, promoted.badge) == (hand_made.status, hand_made.badge)
    assert task_service.list_generating(workspace) == []
    assert sorted(p.name for p in (tasks_dir(workspace) / slug).iterdir()) == [
        "goal.md",
        "manager.json",
    ]


# --- generation rows (orphan display) -----------------------------


def test_a_live_generator_reads_as_generating(workspace, monkeypatch):
    monkeypatch.setattr(db, "pid_live", lambda *args, **kwargs: True)
    make_staging(workspace, status="running", pid=4321, pid_start=1.0)

    [row] = task_service.list_generating(workspace)
    assert (row["status"], row["badge"], row["reason"]) == ("generating", "generating", "")
    assert row["source_key"] == SOURCE_KEY


def test_a_queued_dir_without_a_pid_is_generating_only_inside_the_grace(workspace):
    make_staging(workspace, status="queued", created_at=time.time() - 10)
    make_staging(workspace, status="queued", created_at=time.time() - 3600)

    statuses = sorted(row["status"] for row in task_service.list_generating(workspace))
    assert statuses == ["generating", "interrupted"]


def test_dead_generator_reads_as_interrupted_and_is_kept(workspace, monkeypatch):
    # U3: the wrapper died mid-way. The row says so, and the dir is not deleted.
    _no_live_pids(monkeypatch)
    staging = make_staging(workspace, status="running", pid=999999, pid_start=1.0)

    [row] = task_service.list_generating(workspace)
    assert (row["status"], row["badge"]) == ("interrupted", "interrupted")
    assert row["reason"] == task_service.INTERRUPTED_REASON
    assert (staging / manager.GOAL_DRAFT).is_file()
    assert task_service.list_tasks(workspace) == []


def test_a_parked_generation_reads_as_failed_with_its_reason(workspace):
    make_staging(workspace, status="failed", reason="review cap reached after 3 rounds")

    [row] = task_service.list_generating(workspace)
    assert (row["status"], row["badge"]) == ("failed", "generation failed")
    assert row["reason"] == "review cap reached after 3 rounds"


def test_other_statuses_and_manifestless_dirs_are_not_listed(workspace):
    make_staging(workspace, status="promoted")
    (tasks_dir(workspace) / "_gen-bare").mkdir()

    assert task_service.list_generating(workspace) == []


# --- workspace page -------------------------------------------------------------


def test_workspace_page_shows_generating_row_without_task_link(workspace):
    make_staging(workspace, status="queued", created_at=time.time())
    ws_id = _register(workspace)

    html = _client().get(f"/ws/{ws_id}").text

    assert 'data-status="generating"' in html
    assert "/tasks/_gen-" not in html
    assert "No active tasks." not in html


def test_generating_row_links_its_source_session_to_the_transcript(workspace):
    make_staging(workspace, status="queued", created_at=time.time())
    ws_id = _register(workspace)

    html = _client().get(f"/ws/{ws_id}").text

    assert f'href="/chat/claude_code/visibility-test?ws={ws_id}"' in html


def test_generating_row_without_a_provider_key_shows_the_key_unlinked(workspace):
    make_staging(workspace, status="queued", created_at=time.time(), source_key="no-colon")
    ws_id = _register(workspace)

    html = _client().get(f"/ws/{ws_id}").text

    assert "From chat <code>no-colon</code>" in html
    assert "/chat/no-colon" not in html


def test_workspace_page_shows_failed_generation_reason(workspace):
    make_staging(workspace, status="failed", reason="review cap reached after 3 rounds")
    ws_id = _register(workspace)

    html = _client().get(f"/ws/{ws_id}").text

    assert 'data-status="failed"' in html
    assert "generation failed" in html
    assert "review cap reached after 3 rounds" in html


def test_workspace_page_shows_interrupted_row(workspace, monkeypatch):
    _no_live_pids(monkeypatch)
    make_staging(workspace, status="running", pid=999999, pid_start=1.0)
    ws_id = _register(workspace)

    html = _client().get(f"/ws/{ws_id}").text

    assert 'data-status="interrupted"' in html
    assert task_service.INTERRUPTED_REASON in html


def test_workspace_page_hides_a_draft_only_dir_and_says_no_active_tasks(workspace):
    (tasks_dir(workspace) / "_gen-draft-only").mkdir()
    (tasks_dir(workspace) / "_gen-draft-only" / manager.GOAL_DRAFT).write_text(
        DRAFT, encoding="utf-8"
    )
    ws_id = _register(workspace)

    html = _client().get(f"/ws/{ws_id}").text

    assert "_gen-draft-only" not in html
    assert "No active tasks." in html


def test_workspace_page_lists_a_promoted_lane_with_its_link(workspace):
    staging = make_staging(
        workspace, status="running", approved=True, suggested_slug="token-guard"
    )
    slug = manager.promote(staging, tasks_dir(workspace))
    ws_id = _register(workspace)

    html = _client().get(f"/ws/{ws_id}").text

    assert f'href="/ws/{ws_id}/tasks/{slug}"' in html
    assert "generating" not in html
