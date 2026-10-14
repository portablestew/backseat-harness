"""The generate-task manager core (config, persona, prompt, round protocol).

The manager runs through a scripted invoker, so no provider CLI is needed. Each
scripted step writes the files a real manager would and returns an envelope.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import FakeProvider

import backseat_harness.cli as cli
from backseat_harness import logs, manager, marker, spawn
from backseat_harness.config import (
    DEFAULT_MANAGER_PROVIDER,
    Config,
    ManagerConfig,
    load_config,
)
from backseat_harness.loop import OperatorReply
from backseat_harness.operator import load_persona

SOURCE_KEY = "claude_code:abc"
TRANSCRIPT = (
    "USER: keep it to the token budget guard, not a full quota system\n\n"
    "ASSISTANT: noted, a single guard per run\n\n"
    "USER: yes, that is right\n"
)
DRAFT = "# Token guard\n\nGuard each run with one token budget.\n"


def reply(phase: str, **fields) -> str:
    """An envelope inside some prose, the way a real reply arrives."""
    return "Done.\n" + json.dumps({"v": 1, "phase": phase, **fields})


class Script:
    """A scripted manager: each call pops the next step, which writes files and replies."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.steps:
            raise AssertionError("manager was called more times than scripted")
        return self.steps.pop(0)(prompt)


def generate_step(staging, text=DRAFT, slug="token-guard", approved=True, reviews=1, notes=""):
    """The one manager call: write the draft, record approval in the manifest, reply."""

    def step(prompt):
        (staging / manager.GOAL_DRAFT).write_text(text, encoding="utf-8")
        data = manager.read_manifest(staging)
        data.update(approved=approved, suggested_slug=slug)
        manager.write_manifest(staging, data)
        return reply(
            "generate", approved=approved, suggested_slug=slug, reviews=reviews, notes=notes
        )

    return step


def approving_script(staging):
    return Script(generate_step(staging))


@pytest.fixture
def staging(config):
    return manager.new_staging(config.tasks_dir, SOURCE_KEY, str(config.workspace_root))


@pytest.fixture
def loader():
    return lambda keys: {key: TRANSCRIPT for key in keys}


def with_manager(config: Config, **fields) -> Config:
    return replace(config, manager=ManagerConfig(**fields))


# --- config ------------------------------------------------------------------


def test_manager_profile_takes_its_own_provider_model_agent_and_timeout(config):
    cfg = replace(
        config,
        operator=replace(config.operator, provider="kiro_cli", model="op-model", agent="op-agent"),
    )
    profile = with_manager(
        cfg, provider="claude", model="mgr-model", timeout_s=120
    ).manager_profile()
    assert profile.provider == "claude"  # the manager's own, not the operator's
    assert profile.model == "mgr-model"
    assert profile.agent is None  # the operator's agent is not inherited
    assert profile.timeout_s == 120
    assert profile.verify_commands == []


def test_manager_profile_defaults_to_opencode_and_the_provider_model(config):
    cfg = replace(config, operator=replace(config.operator, model="op-model", timeout_s=77))
    profile = cfg.manager_profile()
    assert profile.provider == DEFAULT_MANAGER_PROVIDER == "opencode"
    assert profile.model is None  # the provider CLI's own default, not the operator's
    assert profile.timeout_s == ManagerConfig().timeout_s


def test_manager_table_parses_from_workspace_config(workspace, fake_home):
    (workspace / ".backseat" / "config.toml").write_text(
        '[manager]\nmodel = "m1"\nagent = "a1"\ntimeout_s = 300\nreview_rounds = 2\n'
        '[operator]\nmodel = "op"\n',
        encoding="utf-8",
    )
    cfg = load_config(workspace, home=fake_home)
    assert cfg.manager == ManagerConfig(
        provider="opencode", model="m1", agent="a1", timeout_s=300, review_rounds=2
    )
    assert cfg.operator.model == "op"


def test_review_rounds_below_one_is_clamped_with_a_warning(workspace, fake_home, capsys):
    (workspace / ".backseat" / "config.toml").write_text("[manager]\nreview_rounds = 0\n")
    cfg = load_config(workspace, home=fake_home)
    assert cfg.manager.review_rounds == 1
    assert "review_rounds = 0 must be at least 1" in capsys.readouterr().err


# --- persona, prompt, and shared invoker --------------------------------------


def test_load_persona_names_the_manager_persona_and_keeps_the_operator_default():
    manager_text = load_persona("manager_persona.md")
    assert "Definition of done" in manager_text
    assert load_persona() != manager_text
    assert load_persona() == load_persona("operator_persona.md")


def test_prompt_teaches_every_steering_rule():
    """Each R1–R10 rule and the three required headings are in the persona."""
    persona = load_persona("manager_persona.md")
    rules = {
        "R1": "Write requirements, not steps",
        "R2": "The user's words are the core",
        "R3": "Tag every decision hard or soft",
        "R4": 'Record rejected alternatives as "not X"',
        "R5": "Constraints keep their rationale",
        "R6": "Done means the user's acceptance",
        "R7": "Open questions stay open",
        "R8": "Attribute only what the user did",
        "R9": "Keep what is expensive to rediscover",
        "R10": "No filler",
    }
    for rule, phrase in rules.items():
        assert phrase in persona, rule
    for section in (
        "## User decisions",
        "## Definition of done",
        "## Out of scope",
    ):
        assert section in persona, section


def test_generate_prompt_states_the_review_limit_and_envelope(staging):
    prompt = manager.build_prompt(
        manager.ManagerInputs(phase="generate", staging=staging, source_text=TRANSCRIPT)
    )
    assert "after 3 reviews, whichever comes first" in prompt
    assert "start a fresh reviewer subagent" in prompt
    assert '"approved": true | false' in prompt
    assert "may write only these files" in prompt
    assert "`goal-draft.md`" in prompt and "`manager.json`" in prompt


def test_generate_prompt_uses_the_configured_review_limit(staging):
    prompt = manager.build_prompt(
        manager.ManagerInputs(
            phase="generate", staging=staging, source_text=TRANSCRIPT, review_rounds=2
        )
    )
    assert "after 2 reviews, whichever comes first" in prompt


def test_review_round_checks_user_attribution_and_hard_tags():
    """The reviewer stance (persona) keeps the four firm checks the subagent runs.

    Each quoted user attribution is tested against the source, and each hard tag
    against an explicit user decision, so a misattributed or over-hardened draft
    cannot be approved.
    """
    persona = load_persona("manager_persona.md")
    assert "Every quote under a decision is word for word from a user message" in persona
    assert "Every sentence saying the user decided, wanted, rejected, withdrew" in persona
    assert "Every hard tag is backed by an explicit user decision" in persona
    assert "No open question is described as settled or withdrawn" in persona
    assert "## Workflow" in persona


def test_manager_invoker_uses_the_manager_persona_and_profile(config, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "get_provider", lambda name: FakeProvider())
    monkeypatch.setattr(cli.spawn, "resolve_argv", lambda cmd: cmd)

    def fake_run(cmd, **kwargs):
        seen["input"] = kwargs["input"]
        seen["timeout_s"] = kwargs["timeout_s"]
        return spawn.CompletedCall(0, "ok", "", False)

    monkeypatch.setattr(cli.spawn, "run_with_timeout", fake_run)
    cfg = with_manager(config, timeout_s=45)
    invoke = cli._make_oneshot_invoker(
        cfg, cfg.manager_profile(), load_persona("manager_persona.md"), role="manager"
    )
    invoke("Draft it.")
    assert seen["input"] == f"{load_persona('manager_persona.md')}\n\nDraft it."
    assert seen["timeout_s"] == 45


def test_redact_all_removes_every_marker_uuid_and_bare_occurrences():
    a = "11111111-1111-1111-1111-111111111111"
    b = "22222222-2222-2222-2222-222222222222"
    text = f"{marker.format_marker(a, 1)}\n...\n{marker.format_marker(b, 2)}\nbare {a} again"
    out = marker.redact_all(text)
    assert a not in out and b not in out
    assert out.count(marker.REDACTED_ROOT) == 3


def test_manager_log_names_each_call_under_the_staging_logs(staging):
    path = logs.manager_log(staging, "review", 4)
    assert path == staging / "logs" / "manager.review.4.log"
    assert path.parent.is_dir()


# --- the round protocol --------------------------------------------------------


def test_full_generation_approves_and_leaves_the_draft_for_promotion(config, staging, loader):
    script = approving_script(staging)
    result = manager.run_generation(config, staging, SOURCE_KEY, script, load=loader)
    assert result == manager.GenerationResult(True, "", 1, "token-guard")
    assert len(script.prompts) == 1
    assert (staging / manager.GOAL_DRAFT).read_text(encoding="utf-8") == DRAFT
    data = manager.read_manifest(staging)
    assert data["approved"] is True
    assert data["status"] == "running"  # promotion moves it on
    assert data["source_key"] == SOURCE_KEY


def test_manager_reports_its_review_count_in_the_manifest(config, staging, loader):
    script = Script(generate_step(staging, reviews=2))
    result = manager.run_generation(config, staging, SOURCE_KEY, script, load=loader)
    assert result.rounds == 2
    assert manager.read_manifest(staging)["rounds"] == 2


def test_unapproved_draft_parks_with_the_managers_notes(config, staging, loader):
    script = Script(generate_step(staging, approved=False, notes="still has minutiae"))
    result = manager.run_generation(config, staging, SOURCE_KEY, script, load=loader)
    assert result.approved is False
    assert result.reason == "manager did not approve the draft: still has minutiae"
    data = manager.read_manifest(staging)
    assert data["status"] == "failed" and data["approved"] is False
    assert not list(config.tasks_dir.rglob("goal.md"))
    assert staging.is_dir()  # parked, not discarded


def test_unparsable_envelope_reasks_once_then_parks(config, staging, loader):
    script = Script(lambda p: "no envelope here", lambda p: "still none")
    result = manager.run_generation(config, staging, SOURCE_KEY, script, load=loader)
    assert len(script.prompts) == 2
    assert "no parsable envelope after re-ask" in result.reason
    assert manager.read_manifest(staging)["status"] == "failed"


def test_unparsable_envelope_recovers_on_the_reask(config, staging, loader):
    script = Script(lambda p: "no envelope here", generate_step(staging))
    result = manager.run_generation(config, staging, SOURCE_KEY, script, load=loader)
    assert result.approved is True
    assert manager.REASK_NOTE in script.prompts[1]


def test_timeout_parks_without_reask(config, staging, loader):
    script = Script(lambda p: OperatorReply("", timed_out=True))
    result = manager.run_generation(config, staging, SOURCE_KEY, script, load=loader)
    assert len(script.prompts) == 1
    assert result.reason == f"generate call timed out after {config.manager_profile().timeout_s}s"


def test_write_outside_allowed_set_parks(config, staging, loader):
    def generate_with_extra(prompt):
        (staging / "extra.txt").write_text("x", encoding="utf-8")
        (staging / manager.GOAL_DRAFT).write_text(DRAFT, encoding="utf-8")
        return reply("generate", approved=True, suggested_slug="t", reviews=1, notes="")

    script = Script(generate_with_extra)
    result = manager.run_generation(config, staging, SOURCE_KEY, script, load=loader)
    assert result.reason.startswith("wrote outside scope")
    assert "extra.txt" in result.reason


def test_a_stray_file_from_a_reviewer_is_a_scope_violation(config, staging, loader):
    """A reviewer subagent runs inside the same call, so its writes are checked too."""

    def generate_touches_scratch(prompt):
        (staging / manager.GOAL_DRAFT).write_text(DRAFT, encoding="utf-8")
        (staging / "review-notes.md").write_text("reviewer scratch", encoding="utf-8")
        return reply("generate", approved=True, suggested_slug="t", reviews=1, notes="")

    result = manager.run_generation(
        config, staging, SOURCE_KEY, Script(generate_touches_scratch), load=loader
    )
    assert result.reason == "wrote outside scope: review-notes.md"


def test_wrapper_owned_files_are_not_scope_violations(config, staging, loader):
    """A large source writes source.md, and the wrapper's logs are written every call."""
    config = replace(config, compact_threshold_bytes=10)
    script = approving_script(staging)
    result = manager.run_generation(config, staging, SOURCE_KEY, script, load=loader)
    assert result.approved is True
    assert (staging / manager.SOURCE_FILE).is_file()
    assert (staging / "logs").is_dir()


def test_approval_must_be_written_to_the_file_not_only_the_envelope(config, staging, loader):
    def generate_says_yes_but_writes_no(prompt):
        (staging / manager.GOAL_DRAFT).write_text(DRAFT, encoding="utf-8")
        return reply("generate", approved=True, suggested_slug="token-guard", reviews=1, notes="")

    script = Script(generate_says_yes_but_writes_no)
    result = manager.run_generation(config, staging, SOURCE_KEY, script, load=loader)
    assert result.reason.startswith("manager did not approve the draft")
    assert manager.read_manifest(staging)["approved"] is False


def test_wrapper_keys_are_reasserted_after_a_manager_edit(config, staging, loader):
    def generate_tampers(prompt):
        (staging / manager.GOAL_DRAFT).write_text(DRAFT, encoding="utf-8")
        data = manager.read_manifest(staging)
        data.update(approved=True, suggested_slug="token-guard", source_key="x:y")
        manager.write_manifest(staging, data)
        return reply("generate", approved=True, suggested_slug="token-guard", reviews=1, notes="")

    script = Script(generate_tampers)
    result = manager.run_generation(config, staging, SOURCE_KEY, script, load=loader)
    assert result.approved is True
    assert manager.read_manifest(staging)["source_key"] == SOURCE_KEY


def test_missing_source_transcript_parks_with_reason(config, staging):
    script = Script()
    result = manager.run_generation(
        config, staging, "claude_code:gone", script, load=lambda keys: {}
    )
    assert script.prompts == []
    assert result.reason == "source transcript unavailable: claude_code:gone"


def test_source_markers_are_redacted_in_the_prompt(config, staging):
    root = "33333333-3333-3333-3333-333333333333"
    text = f"{marker.format_marker(root, 1)}\nUSER: hi\n"
    script = approving_script(staging)
    manager.run_generation(
        config, staging, SOURCE_KEY, script, load=lambda keys: {k: text for k in keys}
    )
    assert root not in script.prompts[0]
    assert marker.REDACTED_ROOT in script.prompts[0]


def test_large_source_goes_to_source_md_and_the_prompt_points_at_it(config, staging, loader):
    config = replace(config, compact_threshold_bytes=10)
    script = approving_script(staging)
    manager.run_generation(config, staging, SOURCE_KEY, script, load=loader)
    source = staging / manager.SOURCE_FILE
    assert source.read_text(encoding="utf-8") == TRANSCRIPT
    assert "too long to inline" in script.prompts[0]
    assert "USER: keep it to the token budget guard" not in script.prompts[0]


def test_small_source_is_inlined_and_no_source_file_is_written(config, staging, loader):
    script = approving_script(staging)
    manager.run_generation(config, staging, SOURCE_KEY, script, load=loader)
    assert "USER: keep it to the token budget guard" in script.prompts[0]
    assert not (staging / manager.SOURCE_FILE).exists()


def test_nothing_is_written_outside_the_staging_dir(config, staging, loader):
    def tree():
        return {p: p.stat().st_mtime_ns for p in config.workspace_root.rglob("*") if p.is_file()}

    before = tree()
    manager.run_generation(config, staging, SOURCE_KEY, approving_script(staging), load=loader)
    after = tree()
    changed = {p for p in after if before.get(p) != after[p]} | (set(before) - set(after))
    assert changed, "the run should have written into the staging dir"
    assert all(staging in p.parents for p in changed)


def test_crash_mid_run_leaves_status_running(config, staging, loader):
    def explode(prompt):
        raise RuntimeError("boom")

    script = Script(explode)
    with pytest.raises(RuntimeError, match="boom"):
        manager.run_generation(config, staging, SOURCE_KEY, script, load=loader)
    assert manager.read_manifest(staging)["status"] == "running"
    assert not list(config.workspace_root.rglob("goal.md")), "a crash must leave no goal.md"


def test_run_requires_a_manifest_in_staging(config, loader, tmp_path):
    bare = tmp_path / "_gen-empty"
    bare.mkdir()
    with pytest.raises(ValueError, match="no readable manager.json"):
        manager.run_generation(config, bare, SOURCE_KEY, Script(), load=loader)


def test_parse_envelope_takes_the_last_object_of_the_requested_phase():
    text = (
        'first {"v":1,"phase":"generate","approved":false} '
        'then {"v":1,"phase":"other","suggested_slug":"x"} '
        'and last {"v":1,"phase":"generate","approved":true}'
    )
    assert manager.parse_envelope(text, "generate")["approved"] is True
    assert manager.parse_envelope(text, "other")["suggested_slug"] == "x"
    assert manager.parse_envelope(text, "review") is None


# --- the manager's operator-style tag -----------------------------------


def test_manager_tag_is_an_operator_session_with_no_lane(tmp_path):
    """The manager reuses the OPERATOR tag. Its phase lands in the slug position.

    What this checks: session classification hides the manager's session from
    "Other chats" (the tag is an operator tag), and the search's lane lookup
    returns no lane for it, because no ``tasks/manager-draft`` exists. The
    only way it resolves to a lane is a real task named ``manager-draft``.
    """
    from backseat_harness.web.services import chat_search

    text = marker.format_operator_marker(slug="", phase="manager-draft") + "\nhello"
    assert marker.parse_operator_marker(text) == "manager-draft"
    assert chat_search.classify_session(text) == "operator"

    hit = SimpleNamespace(project=str(tmp_path), operator_slug="manager-draft")
    assert chat_search._operator_lane(hit) is None

    (tmp_path / ".backseat" / "tasks" / "manager-draft").mkdir(parents=True)
    assert chat_search._operator_lane(hit) == tmp_path / ".backseat" / "tasks" / "manager-draft"
