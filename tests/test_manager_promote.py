"""Promotion, the wrapper that turns an approved staging dir into a task lane.

The manager is a scripted fake, as in test_manager.py, so no provider CLI runs.
The CLI tests run ``backseat-harness generate-task`` through ``main`` with the
same fake, and a workspace registered in the hermetic db.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

import backseat_harness.cli as cli
from backseat_harness import db, logs, manager
from backseat_harness.config import Config

SOURCE_KEY = "claude_code:abc"
TRANSCRIPT = (
    "USER: keep it to the token budget guard, not a full quota system\n\n"
    "ASSISTANT: noted, a single guard per run\n\n"
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


def env(phase: str, **fields) -> str:
    """An envelope inside some prose, the way a real reply arrives."""
    return "Done.\n" + json.dumps({"v": 1, "phase": phase, **fields})


def only_staging(tasks_dir: Path) -> Path:
    """The one ``_gen-*`` dir under tasks/. The fakes find their dir at call time,
    so the CLI can create its own staging name."""
    found = sorted(tasks_dir.glob(f"{manager.STAGING_PREFIX}*"))
    assert len(found) == 1, found
    return found[0]


def approve_path(tasks_dir: Path, *, draft=GOOD_DRAFT, slug="token-guard", approved=True):
    """A fake manager on the approve path: one generate call that writes the draft
    and its approval.

    The call also writes a log file, the way the real invoker does, so the
    log-removal checks have something to find.
    """

    def generate_step():
        staging = only_staging(tasks_dir)
        (staging / manager.GOAL_DRAFT).write_text(draft, encoding="utf-8")
        data = manager.read_manifest(staging)
        data.update(approved=approved, suggested_slug=slug)
        manager.write_manifest(staging, data)
        return env("generate", approved=approved, suggested_slug=slug, reviews=1, notes="")

    steps = iter([generate_step])

    def invoke(prompt: str) -> str:
        log = logs.current()
        if log is not None:
            log.write_text("provider output\n", encoding="utf-8")
        return next(steps)()

    return invoke


def loader(keys):
    return {key: TRANSCRIPT for key in keys}


def make_approved_staging(config: Config, *, draft=GOOD_DRAFT, approved=True, status="running"):
    """A staging dir as a finished generation leaves it, for the promote tests.

    The manifest carries the keys a promoted lane must not keep (``source_title``,
    ``self_review``, the pids), so the whitelist test has something to drop.
    """
    staging = manager.new_staging(config.tasks_dir, SOURCE_KEY, str(config.workspace_root))
    (staging / manager.GOAL_DRAFT).write_text(draft, encoding="utf-8")
    data = manager.read_manifest(staging)
    data.update(
        status=status,
        approved=approved,
        suggested_slug="token-guard",
        rounds=1,
        source_title="A chat title",
        self_review="the chat said something",
        pid=1234,
        pid_start=1.0,
        provider_pids=[[1234, 1.0]],
    )
    manager.write_manifest(staging, data)
    return staging


@pytest.fixture
def tasks_dir(config: Config) -> Path:
    return config.tasks_dir


# --- promotion -------------------------------------------------------------------


def test_promotion_lands_goal_and_a_provenance_only_manifest(config, tasks_dir):
    staging = make_approved_staging(config)
    slug = manager.promote(staging, tasks_dir)

    lane = tasks_dir / slug
    assert slug == "token-guard"
    assert (lane / "goal.md").read_text(encoding="utf-8") == GOOD_DRAFT
    manifest = json.loads((lane / "manager.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "promoted"
    assert manifest["approved"] is True
    assert manifest["source_key"] == SOURCE_KEY
    assert manifest["final_slug"] == slug
    assert set(manifest) <= set(manager.PROVENANCE_KEYS)
    for dropped in ("source_title", "self_review", "pid", "pid_start", "provider_pids"):
        assert dropped not in manifest
    assert not staging.exists()
    assert sorted(p.name for p in tasks_dir.iterdir()) == [slug]


def test_promoted_lane_is_exactly_goal_and_manifest(config, tasks_dir):
    """Source, logs, and staging do not reach the promoted lane."""
    small = replace(config, compact_threshold_bytes=10)  # force source.md
    staging = manager.new_staging(small.tasks_dir, SOURCE_KEY, str(small.workspace_root))
    invoker = approve_path(tasks_dir)
    result = manager.run_generation(small, staging, SOURCE_KEY, invoker, load=loader)
    assert result.approved
    assert (staging / manager.SOURCE_FILE).is_file()
    assert any((staging / "logs").iterdir())

    slug = manager.promote(staging, tasks_dir)

    lane = tasks_dir / slug
    assert sorted(p.name for p in lane.iterdir()) == ["goal.md", "manager.json"]
    assert not staging.exists()


def test_parked_generation_keeps_its_source_and_logs(config, tasks_dir):
    """A parked generation is not promoted, so its staging dir keeps everything."""
    capped = replace(config, compact_threshold_bytes=10)
    staging = manager.new_staging(capped.tasks_dir, SOURCE_KEY, str(capped.workspace_root))

    def generate_unapproved():
        (staging / manager.GOAL_DRAFT).write_text(GOOD_DRAFT, encoding="utf-8")
        data = manager.read_manifest(staging)
        data.update(approved=False, suggested_slug="token-guard")
        manager.write_manifest(staging, data)
        return env("generate", approved=False, suggested_slug="token-guard", reviews=3, notes="")

    steps = iter([generate_unapproved])

    def invoke(prompt):
        log = logs.current()
        if log is not None:
            log.write_text("provider output\n", encoding="utf-8")
        return next(steps)()

    result = manager.run_generation(capped, staging, SOURCE_KEY, invoke, load=loader)

    assert not result.approved
    assert result.reason == "manager did not approve the draft: "
    assert (staging / manager.SOURCE_FILE).is_file()
    assert (staging / "logs" / "manager.generate.1.log").is_file()
    assert manager.read_manifest(staging)["status"] == "failed"
    assert not (tasks_dir / "token-guard").exists()


def test_promote_uniquifies_on_collision_and_leaves_the_existing_lane_alone(config, tasks_dir):
    existing = tasks_dir / "token-guard"
    existing.mkdir()
    (existing / "goal.md").write_text("an earlier task\n", encoding="utf-8")
    staging = make_approved_staging(config)

    slug = manager.promote(staging, tasks_dir)

    assert slug == "token-guard-2"
    assert (existing / "goal.md").read_text(encoding="utf-8") == "an earlier task\n"
    assert (tasks_dir / "token-guard-2" / "goal.md").read_text(encoding="utf-8") == GOOD_DRAFT


def test_promote_takes_the_next_suffix_when_mkdir_loses_a_race(config, tasks_dir, monkeypatch):
    """The first claim fails as if another promotion took the name in between."""
    real_mkdir = Path.mkdir
    lost = []

    def racing_mkdir(self, *args, **kwargs):
        if self.name == "token-guard" and not lost:
            lost.append(self)
            raise FileExistsError(str(self))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    staging = make_approved_staging(config)

    assert manager.promote(staging, tasks_dir) == "token-guard-2"
    assert lost


@pytest.mark.parametrize(
    ("suggested", "expected"),
    [
        ("../../evil", "evil"),
        ("!!!", "chat-task"),
        (None, "chat-task"),
        ("My Task!", "my-task"),
        ("a" * 200, "a" * manager.MAX_SLUG_LEN),
    ],
)
def test_unsanitary_suggested_slugs_are_sanitized(config, tasks_dir, suggested, expected):
    """The wrapper, not the model, decides the name, and it stays a direct child of tasks/."""
    staging = make_approved_staging(config)
    data = manager.read_manifest(staging)
    data["suggested_slug"] = suggested
    manager.write_manifest(staging, data)

    slug = manager.promote(staging, tasks_dir)

    assert slug == expected
    assert (tasks_dir / slug).parent == tasks_dir


def test_promotion_refuses_an_unapproved_generation(config, tasks_dir):
    staging = make_approved_staging(config, approved=False)

    with pytest.raises(manager.PromotionParked, match="not approved"):
        manager.promote(staging, tasks_dir)
    assert not (tasks_dir / "token-guard").exists()


def test_missing_user_decisions_parks_and_keeps_the_logs(config, tasks_dir):
    draft = GOOD_DRAFT.replace("## User decisions (hard)", "## Decisions")
    staging = make_approved_staging(config, draft=draft)
    (staging / "logs").mkdir()
    (staging / "logs" / "manager.generate.1.log").write_text("kept\n", encoding="utf-8")

    with pytest.raises(manager.PromotionParked) as caught:
        manager.promote(staging, tasks_dir)

    assert caught.value.reason == "draft missing required section: User decisions"
    manifest = manager.read_manifest(staging)
    assert manifest["status"] == "failed"
    assert manifest["reason"] == caught.value.reason
    assert (staging / "logs" / "manager.generate.1.log").is_file()
    assert not (tasks_dir / "token-guard").exists()


@pytest.mark.parametrize(
    ("draft", "expected"),
    [
        # Out of scope comes before Definition of done, so the last section is wrong.
        (
            GOOD_DRAFT.replace("## Definition of done", "## Tmp")
            .replace("## Out of scope", "## Definition of done")
            .replace("## Tmp", "## Out of scope"),
            "draft missing required section: Out of scope (last section)",
        ),
        # Out of scope is missing, so Definition of done is the last section.
        (
            GOOD_DRAFT[: GOOD_DRAFT.index("## Out of scope")],
            "draft missing required section: Out of scope (last section)",
        ),
        # Definition of done is missing, and Out of scope is still last.
        (
            GOOD_DRAFT.replace("## Definition of done\n\n- the guard runs once per run\n\n", ""),
            "draft missing required section: Definition of done (second-to-last section)",
        ),
    ],
)
def test_wrong_tail_sections_park(config, tasks_dir, draft, expected):
    staging = make_approved_staging(config, draft=draft)

    with pytest.raises(manager.PromotionParked) as caught:
        manager.promote(staging, tasks_dir)

    assert caught.value.reason == expected
    assert manager.read_manifest(staging)["reason"] == expected


def test_secret_like_token_parks_without_echoing_it(config, tasks_dir):
    secret = "AKIAIOSFODNN7EXAMPLE"  # AKIA plus 16 uppercase alphanumerics
    staging = make_approved_staging(config, draft=GOOD_DRAFT + f"\nkey {secret}\n")

    with pytest.raises(manager.PromotionParked) as caught:
        manager.promote(staging, tasks_dir)

    reason = caught.value.reason
    assert reason == "draft contains a secret-like token (aws-access-key)"
    assert secret not in reason
    assert secret not in (manager.read_manifest(staging)["reason"])
    assert staging.is_dir()
    assert not (tasks_dir / "token-guard").exists()


def test_words_that_end_in_sk_are_not_secrets(config, tasks_dir):
    """The lookbehind keeps ``risk-`` from matching the ``sk-`` key shape."""
    draft = GOOD_DRAFT + "\nrisk-management-process-overview-for-the-team\n"
    staging = make_approved_staging(config, draft=draft)

    assert manager.promote(staging, tasks_dir) == "token-guard"


def test_promote_is_refused_for_a_staging_dir_already_marked_failed(config, tasks_dir):
    staging = make_approved_staging(config, status="failed")

    with pytest.raises(manager.PromotionParked, match="not approved"):
        manager.promote(staging, tasks_dir)


# --- CLI ----------------------------------------------------------------------


@pytest.fixture
def registered(workspace):
    with db.open_db() as conn:
        db.add_workspace(conn, workspace)
    return workspace


@pytest.fixture
def fake_cli_manager(monkeypatch):
    """Route the CLI's manager through the fake, with the source loader injected."""
    real_run = manager.run_generation

    def invoker_for(config, profile, persona, *, role="operator"):
        return approve_path(config.tasks_dir)

    monkeypatch.setattr(cli, "_make_oneshot_invoker", invoker_for)
    monkeypatch.setattr(
        manager,
        "run_generation",
        lambda config, staging, key, invoker, **kwargs: real_run(
            config, staging, key, invoker, load=loader
        ),
    )


def run_cli(capsys, *argv):
    rc = cli.main(["--format", "json", *argv])
    return rc, json.loads(capsys.readouterr().out)


def test_generate_task_cli_parks_with_exit_1_and_keeps_the_staging_dir(
    registered, monkeypatch, capsys
):
    def invoker_for(config, profile, persona, *, role="operator"):
        return approve_path(config.tasks_dir, approved=False)

    monkeypatch.setattr(cli, "_make_oneshot_invoker", invoker_for)
    real_run = manager.run_generation
    monkeypatch.setattr(
        manager,
        "run_generation",
        lambda config, staging, key, invoker, **kwargs: real_run(
            config, staging, key, invoker, load=loader
        ),
    )

    rc, payload = run_cli(capsys, "-C", str(registered), "generate-task", SOURCE_KEY)

    assert rc == 1
    assert payload["status"] == "failed"
    assert payload["reason"].startswith("manager did not approve the draft")
    staging = Path(payload["staging"])
    assert staging.is_dir()
    manifest = manager.read_manifest(staging)
    assert manifest["pid"] == os.getpid()
    assert manifest["status"] == "failed"


def test_generate_task_cli_accepts_a_precreated_staging_dir(registered, fake_cli_manager, capsys):
    """The web route creates the staging dir before it spawns the CLI."""
    tasks = registered / ".backseat" / "tasks"
    staging = manager.new_staging(tasks, SOURCE_KEY, str(registered))

    rc, payload = run_cli(
        capsys, "-C", str(registered), "generate-task", SOURCE_KEY, "--staging", staging.name
    )

    assert rc == 0
    assert payload["final_slug"] == "token-guard"
    assert not staging.exists()


@pytest.mark.parametrize(
    "ws_fixture, tail, message",
    [
        ("workspace", [SOURCE_KEY], "not a registered workspace"),
        ("registered", ["notakey"], "provider:conversation_id"),
        ("registered", [SOURCE_KEY, "--staging", "../outside"], "not a staging dir under tasks/"),
        (
            "registered",
            [SOURCE_KEY, "--staging", "not-a-gen-dir"],
            "not a staging dir under tasks/",
        ),
    ],
    ids=["unregistered-workspace", "bad-source-key", "staging-escapes", "staging-not-gen-dir"],
)
def test_generate_task_cli_refuses_bad_input_and_writes_nothing(
    request, fake_cli_manager, capsys, ws_fixture, tail, message
):
    ws = request.getfixturevalue(ws_fixture)
    rc, payload = run_cli(capsys, "-C", str(ws), "generate-task", *tail)

    assert rc == 2
    assert message in payload["error"]
    assert not list((ws / ".backseat" / "tasks").glob("_gen-*"))
