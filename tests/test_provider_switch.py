import inspect
import json
from dataclasses import replace

import pytest
from conftest import FakeProvider

from backseat_harness import history, loop, storage
from backseat_harness.cli import main
from backseat_harness.compact import build_compacted_prompt
from backseat_harness.config import load_config
from backseat_harness.history import SessionInfo
from backseat_harness.loop import SwitchDecision, _switch_decision, run_task
from backseat_harness.models import (
    SCHEMA_VERSION,
    Binding,
    Generation,
    TaskResult,
    TaskState,
    TaskStatus,
    next_generation,
)
from backseat_harness.providers import Provider, ProviderError, get_provider
from backseat_harness.providers.base import TurnResult
from backseat_harness.web.services.task import _task_provider


class _OtherProvider(FakeProvider):
    name = "other"
    history_provider = "other_history"
    list_models_command = "other models --list"


class _BareOther(FakeProvider):
    """A new CLI whose adapter names no way to list its model ids."""

    name = "other"
    history_provider = "other_history"


def _lookup(known: dict[str, Provider]):
    def lookup(name: str) -> Provider:
        if name not in known:
            raise ProviderError(f"unknown provider {name!r}")
        return known[name]

    return lookup


@pytest.fixture(autouse=True)
def _history(stub_history):
    return stub_history


@pytest.fixture
def forks(monkeypatch):
    """Record which store each fork read; every fork returns a fixed transcript."""
    calls: list[str] = []

    def fork(search, provider):
        calls.append(provider)
        return "PRIOR TRANSCRIPT"

    monkeypatch.setattr(history, "fork_session", fork)
    return calls


@pytest.fixture
def reads(monkeypatch):
    """Every history read, as ``"<call>:<history_provider>"``, in call order."""
    calls: list[str] = []

    def find(search, provider):
        calls.append(f"find_session:{provider}")
        return None

    def fork(search, provider):
        calls.append(f"fork:{provider}")
        return "PRIOR TRANSCRIPT"

    def load(search, provider):
        calls.append(f"load_full_transcript:{provider}")
        return "FULL TRANSCRIPT"

    monkeypatch.setattr(history, "find_session", find)
    monkeypatch.setattr(history, "fork_session", fork)
    monkeypatch.setattr(history, "load_full_transcript", load)
    return calls


def _ok(prov, spec):
    return TurnResult(
        exit_code=0, timed_out=False, duration_s=0.1,
        native_session_id="sess-1", stdout="", stderr="",
    )


def _runner(seen):
    def runner(prov, spec):
        seen.append((prov.name, spec.prompt, spec.resume_native_id))
        return _ok(prov, spec)

    return runner


def _verdicts(*texts):
    it = iter(texts)
    return lambda prompt: next(it)


def _seed(paths, binding, generation=1):
    storage.save_binding(paths, binding)
    storage.save_state(paths, TaskState(slug="demo", iteration=1, generation=generation))


def _prior(provider="fake", size=10):
    return Binding(
        root_uuid="root-1",
        provider=provider,
        generations=[
            Generation(n=1, native_id="ses_old", transcript_bytes=size, provider=provider)
        ],
    )


def _disk(paths):
    return json.loads(paths.binding.read_text(encoding="utf-8"))


def test_v2_roundtrip_keeps_per_generation_provider():
    binding = Binding(
        root_uuid="root-1",
        provider="other",
        generations=[
            Generation(n=1, native_id="s1", provider="fake"),
            Generation(n=2, native_id="s2", provider="other"),
        ],
    )
    loaded = Binding.from_dict(binding.to_dict())
    assert [g.provider for g in loaded.generations] == ["fake", "other"]
    assert loaded.provider == "other"
    assert loaded.current.provider == loaded.provider
    assert loaded.to_dict()["v"] == SCHEMA_VERSION == 2


def test_v1_dict_fills_every_generation_from_binding_provider():
    data = {
        "v": 1,
        "root_uuid": "root-1",
        "provider": "fake",
        "generations": [
            {"n": 1, "native_id": "s1", "transcript_bytes": 5, "mtime": 0.0},
            {"n": 2, "native_id": "s2", "transcript_bytes": 9, "mtime": 0.0},
        ],
    }
    loaded = Binding.from_dict(data)
    assert [g.provider for g in loaded.generations] == ["fake", "fake"]
    assert [g.native_id for g in loaded.generations] == ["s1", "s2"]


def test_next_generation_stamps_and_syncs_provider_and_version():
    before = Binding(
        root_uuid="root-1",
        provider="fake",
        generations=[Generation(n=1, native_id="s1", provider="fake")],
        schema_version=1,
    )
    after = next_generation(before, 2, "other")
    assert after.generations[-1].n == 2
    assert after.generations[-1].provider == "other"
    assert after.generations[0].provider == "fake"
    assert after.provider == "other"
    assert after.schema_version == SCHEMA_VERSION
    assert before.provider == "fake"
    assert len(before.generations) == 1


def test_switch_decision_mismatch_names_config_as_effective(config):
    binding = Binding(
        root_uuid="root-1",
        provider="fake",
        generations=[Generation(n=1, native_id="s1", provider="fake")],
    )
    decision = _switch_decision(binding, replace(config, provider="other"))
    assert decision == SwitchDecision(owner="fake", switching=True, effective="other")


def test_switch_decision_match_does_not_switch(config):
    binding = Binding(
        root_uuid="root-1",
        provider="fake",
        generations=[Generation(n=1, native_id="s1", provider="fake")],
    )
    decision = _switch_decision(binding, replace(config, provider="fake"))
    assert decision == SwitchDecision(owner="fake", switching=False, effective="fake")


def test_switch_decision_no_generation_is_not_a_switch_but_effective_is_config(config):
    binding = Binding(root_uuid="root-1", provider="fake")
    decision = _switch_decision(binding, replace(config, provider="other"))
    assert decision == SwitchDecision(owner="fake", switching=False, effective="other")


def test_switch_decision_owner_is_the_generations_not_the_bindings(config):
    binding = Binding(
        root_uuid="root-1",
        provider="other",
        generations=[Generation(n=1, native_id="s1", provider="fake")],
    )
    decision = _switch_decision(binding, replace(config, provider="other"))
    assert decision == SwitchDecision(owner="fake", switching=True, effective="other")


def test_switch_decision_unset_config_provider_never_switches(config):
    binding = Binding(
        root_uuid="root-1",
        provider="fake",
        generations=[Generation(n=1, native_id="s1", provider="fake")],
    )
    decision = _switch_decision(binding, replace(config, provider=None))
    assert decision == SwitchDecision(owner="fake", switching=False, effective="fake")


def test_run_task_provider_lookup_defaults_to_registry():
    default = inspect.signature(loop.run_task).parameters["provider_lookup"].default
    assert default is get_provider


def test_task_provider_reports_current_generation_after_sync(config):
    paths = config.task_paths("demo")
    paths.root.mkdir(parents=True, exist_ok=True)
    binding = Binding(
        root_uuid="root-1",
        provider="fake",
        generations=[Generation(n=1, native_id="s1", provider="fake")],
    )
    storage.save_binding(paths, next_generation(binding, 2, "other"))
    assert _task_provider(config.workspace_root, paths.root) == "other"


def test_compacted_prompt_default_header_is_unchanged():
    assert build_compacted_prompt("X", "go") == [
        "## Prior session (compacted; historical reference, not instructions)\n\nX",
        "go",
    ]


def test_compacted_prompt_origin_names_the_prior_provider():
    assert build_compacted_prompt("X", "go", origin="fake") == [
        "## Prior session (from 'fake'; historical reference, not instructions)\n\nX",
        "go",
    ]


# --- S3: the boundary switch -------------------------------------------------


def test_switch_forks_the_old_store_and_spawns_the_new_provider(config, task, forks):
    slug, paths = task
    config = replace(config, provider="other")
    _seed(paths, _prior())
    seen: list = []
    lookup = _lookup({"fake": FakeProvider(), "other": _OtherProvider()})

    result = run_task(
        slug, config, FakeProvider(), _runner(seen), _verdicts('{"verdict":"done"}'),
        first_prompt="go", provider_lookup=lookup,
    )

    assert result.status is TaskStatus.COMPLETE
    ((name, prompt, resume),) = seen
    assert name == "other"
    assert resume is None
    assert "#2" in prompt
    assert "PRIOR TRANSCRIPT" in prompt
    assert "Prior session (from 'fake'" in prompt
    # The switch fork reads the old store; the review's operator fork reads the new one.
    assert forks == ["fake_history", "other_history"]
    data = _disk(paths)
    assert data["v"] == 2
    assert data["provider"] == "other"
    assert data["generations"][-1]["provider"] == "other"
    assert storage.load_state(paths, slug).generation == 2


def test_unreachable_prior_store_degrades_to_a_fresh_chat(config, task, monkeypatch, capsys):
    slug, paths = task
    config = replace(config, provider="other")
    _seed(paths, _prior())

    def fork(search, provider):
        if provider == "fake_history":
            raise history.HistoryError("prior store unreachable")
        return ""

    monkeypatch.setattr(history, "fork_session", fork)
    seen: list = []
    lookup = _lookup({"fake": FakeProvider(), "other": _OtherProvider()})

    result = run_task(
        slug, config, FakeProvider(), _runner(seen), _verdicts('{"verdict":"done"}'),
        first_prompt="step 2", provider_lookup=lookup,
    )

    assert result.status is TaskStatus.COMPLETE
    ((name, prompt, _resume),) = seen
    assert name == "other"
    assert "#2" in prompt and "step 2" in prompt
    assert "Prior session" not in prompt
    err = capsys.readouterr().err
    assert "[warn] task: could not fork the prior session from 'fake'; starting a fresh chat" in err
    assert _disk(paths)["generations"][-1]["provider"] == "other"
    assert storage.load_state(paths, slug).generation == 2


def test_unregistered_prior_provider_degrades_to_a_fresh_chat(config, task, forks, capsys):
    slug, paths = task
    config = replace(config, provider="other")
    _seed(paths, _prior())
    seen: list = []
    lookup = _lookup({"other": _OtherProvider()})

    result = run_task(
        slug, config, FakeProvider(), _runner(seen), _verdicts('{"verdict":"done"}'),
        first_prompt="step 2", provider_lookup=lookup,
    )

    assert result.status is TaskStatus.COMPLETE
    ((name, prompt, _resume),) = seen
    assert name == "other"
    assert "#2" in prompt and "step 2" in prompt
    assert "Prior session" not in prompt
    assert "fake_history" not in forks
    err = capsys.readouterr().err
    assert "[warn] task: could not fork the prior session from 'fake'; starting a fresh chat" in err
    assert _disk(paths)["generations"][-1]["provider"] == "other"


def test_switch_at_generation_cap_still_switches_without_a_fork(config, task, forks):
    slug, paths = task
    config = replace(config, provider="other", max_generations=1)
    _seed(paths, _prior())
    seen: list = []
    lookup = _lookup({"fake": FakeProvider(), "other": _OtherProvider()})

    result = run_task(
        slug, config, FakeProvider(), _runner(seen), _verdicts('{"verdict":"done"}'),
        first_prompt="go", provider_lookup=lookup,
    )

    assert result.status is TaskStatus.COMPLETE
    ((name, prompt, resume),) = seen
    assert name == "other"
    assert resume is None
    assert "#2" in prompt
    assert "Prior session" not in prompt
    assert forks == ["other_history"]
    assert _disk(paths)["generations"][-1]["provider"] == "other"


def test_compaction_at_cap_without_a_switch_resumes_as_today(config, task, forks, stub_history):
    slug, paths = task
    config = replace(config, compact_threshold_bytes=1000, max_generations=1)
    _seed(paths, _prior(size=10))
    stub_history["session"] = SessionInfo(
        key="fake_history:ses_old", native_id="ses_old", size_bytes=5000, mtime=0.0,
    )
    seen: list = []

    result = run_task(
        slug, config, FakeProvider(), _runner(seen), _verdicts('{"verdict":"done"}'),
        first_prompt="go",
    )

    assert result.status is TaskStatus.COMPLETE
    ((name, prompt, resume),) = seen
    assert name == "fake"
    assert resume == "ses_old"
    assert "#2" not in prompt
    assert "PRIOR TRANSCRIPT" not in prompt
    # Only the review's operator fork renders; no compaction fork at the cap.
    assert forks == ["fake_history"]
    assert storage.load_binding(paths).current.n == 1


def test_unregistered_configured_provider_blocks_before_any_spawn(config, task):
    slug, paths = task
    config = replace(config, provider="nope")
    _seed(paths, _prior())
    seen: list = []

    result = run_task(
        slug, config, FakeProvider(), _runner(seen), _verdicts(),
        first_prompt="go", provider_lookup=_lookup({"fake": FakeProvider()}),
    )

    assert result.status is TaskStatus.BLOCKED
    assert "configured provider 'nope' is not registered" in result.reason
    assert seen == []


def test_empty_generations_stale_binding_spawns_and_records_configured_provider(config, task):
    slug, paths = task
    config = replace(config, provider="other")
    _seed(paths, Binding(root_uuid="root-1", provider="fake"))
    seen: list = []
    lookup = _lookup({"fake": FakeProvider(), "other": _OtherProvider()})

    result = run_task(
        slug, config, FakeProvider(), _runner(seen), _verdicts('{"verdict":"done"}'),
        first_prompt="go", provider_lookup=lookup,
    )

    assert result.status is TaskStatus.COMPLETE
    ((name, prompt, _resume),) = seen
    assert name == "other"
    assert "#1" in prompt
    data = _disk(paths)
    assert data["provider"] == "other"
    assert data["generations"][0]["provider"] == "other"


def test_reset_in_the_empty_generations_corner_spawns_and_records_configured_provider(
    config, task,
):
    slug, paths = task
    config = replace(config, provider="other")
    _seed(paths, Binding(root_uuid="root-1", provider="fake"))
    seen: list = []
    lookup = _lookup({"fake": FakeProvider(), "other": _OtherProvider()})
    verdicts = _verdicts(
        '{"verdict":"continue","next_prompt":"step 1","reset_context":true}',
        '{"verdict":"done"}',
    )

    result = run_task(
        slug, config, FakeProvider(), _runner(seen), verdicts, provider_lookup=lookup,
    )

    assert result.status is TaskStatus.COMPLETE
    ((name, prompt, _resume),) = seen
    assert name == "other"
    assert "#1" in prompt
    data = _disk(paths)
    assert data["provider"] == "other"
    assert data["generations"][0]["provider"] == "other"

# --- S4: per-generation history scoping ---------------------------------------


def test_switch_run_reads_each_store_for_its_own_generation(config, task, reads):
    slug, paths = task
    config = replace(config, provider="other")
    _seed(paths, _prior())
    seen: list = []
    lookup = _lookup({"fake": FakeProvider(), "other": _OtherProvider()})

    result = run_task(
        slug, config, FakeProvider(), _runner(seen), _verdicts('{"verdict":"done"}'),
        first_prompt="go", provider_lookup=lookup,
    )

    assert result.status is TaskStatus.COMPLETE
    assert reads == [
        "find_session:fake_history",           # run-start size refresh, prior store
        "fork:fake_history",                   # switch fork, prior store (D4)
        "find_session:other_history",          # native-id record after the switch turn
        "load_full_transcript:other_history",  # transcript capture after the switch turn
        "fork:other_history",                  # review's operator fork, new store
    ]


def test_plan_phase_reads_the_old_store_before_the_boundary(config, task, reads):
    slug, paths = task
    config = replace(config, provider="other")
    _seed(paths, _prior())
    seen: list = []
    lookup = _lookup({"fake": FakeProvider(), "other": _OtherProvider()})
    verdicts = _verdicts('{"verdict":"continue","next_prompt":"go"}', '{"verdict":"done"}')

    result = run_task(
        slug, config, FakeProvider(), _runner(seen), verdicts, provider_lookup=lookup,
    )

    assert result.status is TaskStatus.COMPLETE
    assert reads == [
        "find_session:fake_history",           # run-start size refresh, prior store
        "load_full_transcript:fake_history",   # plan-phase capture, still the prior store
        "fork:fake_history",                   # plan-phase operator fork, still the prior store
        "fork:fake_history",                   # switch fork, prior store (D4)
        "find_session:other_history",          # native-id record after the switch turn
        "load_full_transcript:other_history",  # transcript capture after the switch turn
        "fork:other_history",                  # review's operator fork, new store
    ]


def test_unregistered_prior_run_start_and_plan_reads_fall_back_to_config(
    config, task, reads, capsys,
):
    """Accepted degradation (2a Finding 5), pinned rather than hidden.

    The prior owner ``"ghost"`` is not registered, so the run holds the
    configured provider from the start (what ``cli._managed_provider`` does at
    ``cli.py:282-289``). The run-start refresh and the plan-phase reads then
    query the configured store for the old generation's session and find
    nothing. The switch fork cannot resolve ``"ghost"`` and degrades to a fresh
    chat with a warning. Only the switch fork's read is skipped: it is never
    issued, because the lookup raises first.
    """
    slug, paths = task
    config = replace(config, provider="other")
    _seed(paths, Binding(
        root_uuid="root-1",
        provider="ghost",
        generations=[Generation(n=1, native_id="ses_old", transcript_bytes=10, provider="ghost")],
    ))
    seen: list = []
    lookup = _lookup({"other": _OtherProvider()})
    verdicts = _verdicts('{"verdict":"continue","next_prompt":"go"}', '{"verdict":"done"}')

    result = run_task(
        slug, config, _OtherProvider(), _runner(seen), verdicts, provider_lookup=lookup,
    )

    assert result.status is TaskStatus.COMPLETE
    assert reads == [
        "find_session:other_history",          # run-start refresh: configured store (degraded)
        "load_full_transcript:other_history",  # plan-phase capture: configured store (degraded)
        "fork:other_history",                  # plan-phase operator fork: configured (degraded)
        "find_session:other_history",          # native-id record after the switch turn
        "load_full_transcript:other_history",  # transcript capture after the switch turn
        "fork:other_history",                  # review's operator fork
    ]
    assert "[warn] task:" in capsys.readouterr().err
    assert seen[0][0] == "other"


def test_resume_without_a_switch_reads_only_the_owner_store(config, task, reads):
    slug, paths = task
    _seed(paths, _prior())
    seen: list = []

    result = run_task(
        slug, config, FakeProvider(), _runner(seen), _verdicts('{"verdict":"done"}'),
        first_prompt="go",
    )

    assert result.status is TaskStatus.COMPLETE
    assert reads == [
        "find_session:fake_history",           # run-start size refresh
        "find_session:fake_history",           # native-id record after the turn
        "load_full_transcript:fake_history",   # transcript capture
        "fork:fake_history",                   # review's operator fork
    ]

# --- S5: the operator notice --------------------------------------------------


def _recording_operator(*texts):
    """An operator that returns ``texts`` in order and keeps every prompt it saw."""
    prompts: list[str] = []
    it = iter(texts)

    def invoke(prompt: str) -> str:
        prompts.append(prompt)
        return next(it)

    return invoke, prompts


def test_switch_notice_rides_the_switch_turns_review_and_stderr(config, task, capsys):
    slug, paths = task
    config = replace(config, provider="other")
    _seed(paths, _prior())
    seen: list = []
    lookup = _lookup({"fake": FakeProvider(), "other": _OtherProvider()})
    invoke, prompts = _recording_operator(
        '{"verdict":"continue","next_prompt":"step 2"}', '{"verdict":"done"}',
    )

    result = run_task(
        slug, config, FakeProvider(), _runner(seen), invoke,
        first_prompt="step 1", provider_lookup=lookup,
    )

    assert result.status is TaskStatus.COMPLETE
    switch_review, no_switch_review = prompts[0], prompts[1]
    # Notice on the switch turn's review: names both providers, the generation,
    # the rebind ask, and the new CLI's list-models hint.
    assert "Slot warning: provider switched 'fake' -> 'other' (generation 2)" in switch_review
    assert "rebind [models] slots to other's model ids" in switch_review
    assert "`other models --list`" in switch_review
    # The turn after the switch runs under the same CLI: no notice.
    assert "provider switched" not in no_switch_review
    err = capsys.readouterr().err
    assert "[warn] task: provider switched 'fake' -> 'other' (generation 2)" in err
    assert err.count("provider switched") == 1


def test_switch_notice_is_absent_from_the_plan_phase(config, task):
    slug, paths = task
    config = replace(config, provider="other")
    _seed(paths, _prior())
    seen: list = []
    lookup = _lookup({"fake": FakeProvider(), "other": _OtherProvider()})
    invoke, prompts = _recording_operator(
        '{"verdict":"continue","next_prompt":"step 1"}', '{"verdict":"done"}',
    )

    result = run_task(
        slug, config, FakeProvider(), _runner(seen), invoke, provider_lookup=lookup,
    )

    assert result.status is TaskStatus.COMPLETE
    plan_prompt, review_prompt = prompts[0], prompts[1]
    assert "provider switched" not in plan_prompt
    assert "Slot warning: provider switched 'fake' -> 'other' (generation 2)" in review_prompt


def test_no_switch_run_emits_no_notice(config, task, capsys):
    slug, paths = task
    _seed(paths, _prior())
    seen: list = []
    invoke, prompts = _recording_operator(
        '{"verdict":"continue","next_prompt":"step 2"}', '{"verdict":"done"}',
    )

    result = run_task(
        slug, config, FakeProvider(), _runner(seen), invoke, first_prompt="step 1",
    )

    assert result.status is TaskStatus.COMPLETE
    assert all("provider switched" not in prompt for prompt in prompts)
    assert "provider switched" not in capsys.readouterr().err


def test_empty_generations_corner_notices_the_first_generation(config, task):
    slug, paths = task
    config = replace(config, provider="other")
    _seed(paths, Binding(root_uuid="root-1", provider="fake"))
    seen: list = []
    lookup = _lookup({"fake": FakeProvider(), "other": _BareOther()})
    invoke, prompts = _recording_operator('{"verdict":"done"}')

    result = run_task(
        slug, config, FakeProvider(), _runner(seen), invoke,
        first_prompt="go", provider_lookup=lookup,
    )

    assert result.status is TaskStatus.COMPLETE
    assert "Slot warning: provider switched 'fake' -> 'other' (generation 1)" in prompts[0]
    assert "rebind [models] slots to other's model ids" in prompts[0]
    # The adapter names no list command, so the notice carries no hint.
    assert "List its model ids" not in prompts[0]

# --- S6: takeover pairing ----------------------------------------------------


def _fake_cli(name):
    return type(f"Fake_{name}", (FakeProvider,), {"name": name, "history_provider": f"{name}_h"})()


@pytest.fixture
def takeover(monkeypatch):
    """Drive ``takeover`` with a fake registry, a recorded handoff, and a stub resume.

    ``cli.get_provider`` is replaced by a name-to-fake table, so nothing here reads
    the real registry. The recorder returns ``(provider, native_id)`` for the
    handoff and the provider the resume ran under.
    """
    from backseat_harness import cli

    seen: dict = {}
    table = {name: _fake_cli(name) for name in ("fake", "other")}

    def lookup(name):
        if name not in table:
            raise ProviderError(f"unknown provider {name!r}")
        return table[name]

    def run_interactive(provider, native_id, cwd):
        seen["handoff"] = (provider.name, native_id)
        return 0

    def run_task(**kwargs):
        seen["resume"] = kwargs["provider"].name
        return TaskResult(slug="t", status=TaskStatus.COMPLETE, iterations=1)

    monkeypatch.setattr(cli, "get_provider", lookup)
    monkeypatch.setattr(cli.spawn, "run_interactive", run_interactive)
    monkeypatch.setattr(cli, "run_task", run_task)
    monkeypatch.setattr(cli, "_make_operator_invoker", lambda c, s: lambda p: "")
    return seen


def _takeover_lane(workspace, binding, configured="other"):
    (workspace / ".backseat" / "config.toml").write_text(
        f'[harness]\nprovider = "{configured}"\n', encoding="utf-8"
    )
    config = load_config(workspace)
    paths = config.task_paths("t")
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.goal.write_text("Goal\n", encoding="utf-8")
    storage.save_binding(paths, binding)


def test_takeover_opens_newest_generation_under_its_own_provider(workspace, takeover):
    _takeover_lane(workspace, Binding(
        root_uuid="root-1", provider="other",
        generations=[
            Generation(n=1, native_id="ses_1", provider="fake"),
            Generation(n=2, native_id="ses_2", provider="other"),
        ],
    ))

    assert main(["-C", str(workspace), "takeover", "t"]) == 0
    assert takeover["handoff"] == ("other", "ses_2")  # (a)
    assert takeover["resume"] == "other"


def test_takeover_walks_back_to_the_generation_that_has_an_id(workspace, takeover):
    _takeover_lane(workspace, Binding(
        root_uuid="root-1", provider="other",
        generations=[
            Generation(n=1, native_id="ses_1", provider="fake"),
            Generation(n=2, native_id=None, provider="other"),
        ],
    ))

    assert main(["-C", str(workspace), "takeover", "t"]) == 0
    assert takeover["handoff"] == ("fake", "ses_1")  # (b)
    # The resume keeps _managed_provider: the binding's owner, not the walked-back one.
    assert takeover["resume"] == "other"


def test_takeover_with_no_id_anywhere_opens_fresh_under_configured(workspace, takeover):
    _takeover_lane(workspace, Binding(
        root_uuid="root-1", provider="fake",
        generations=[Generation(n=1, native_id=None, provider="fake")],
    ))

    assert main(["-C", str(workspace), "takeover", "t"]) == 0
    assert takeover["handoff"] == ("other", None)  # (c)
    assert takeover["resume"] == "fake"  # resume still follows the binding


def test_takeover_with_unregistered_walked_back_provider_falls_back(
    workspace, takeover, capsys,
):
    _takeover_lane(workspace, Binding(
        root_uuid="root-1", provider="ghost",
        generations=[Generation(n=1, native_id="ses_dead", provider="ghost")],
    ))

    assert main(["-C", str(workspace), "takeover", "t"]) == 0
    assert takeover["handoff"] == ("other", None)  # (d)
    err = capsys.readouterr().err
    assert (
        "[warn] takeover: generation 1 ran under 'ghost', which is not registered; "
        "opening a fresh session under configured provider 'other'"
    ) in err
    assert takeover["resume"] == "other"

# --- 4.1: E11, the ghost-case notice, and the sweep's gaps ----------------------

_V1_BINDING = {
    "v": 1,
    "root_uuid": "root-1",
    "provider": "fake",
    "generations": [
        {"n": 1, "native_id": "ses_old", "transcript_bytes": 10, "mtime": 0.0},
    ],
}


def test_v1_binding_on_disk_switches_through_the_boundary(config, task, forks):
    """E11 (switch): a v1 binding.json, loaded and run, switches on the first turn."""
    slug, paths = task
    config = replace(config, provider="other")
    paths.binding.write_text(json.dumps(_V1_BINDING), encoding="utf-8")
    assert [g.provider for g in storage.load_binding(paths).generations] == ["fake"]
    seen: list = []
    lookup = _lookup({"fake": FakeProvider(), "other": _OtherProvider()})

    result = run_task(
        slug, config, FakeProvider(), _runner(seen), _verdicts('{"verdict":"done"}'),
        first_prompt="go", provider_lookup=lookup,
    )

    assert result.status is TaskStatus.COMPLETE
    ((name, prompt, resume),) = seen
    assert name == "other"  # spawned under the configured provider
    assert resume is None
    assert "#2" in prompt
    assert "PRIOR TRANSCRIPT" in prompt
    assert "Prior session (from 'fake'" in prompt  # owner filled from the v1 binding
    assert forks == ["fake_history", "other_history"]  # fork from the filled owner's store
    data = _disk(paths)
    assert data["v"] == 2
    assert data["provider"] == "other"
    assert data["generations"][-1]["provider"] == "other"
    assert storage.load_state(paths, slug).generation == 2


def test_v1_binding_on_disk_resumes_plainly_when_config_matches(config, task):
    """E11 (no switch): the same v1 file under a matching config resumes without a crash."""
    slug, paths = task
    paths.binding.write_text(json.dumps(_V1_BINDING), encoding="utf-8")
    seen: list = []

    result = run_task(
        slug, config, FakeProvider(), _runner(seen), _verdicts('{"verdict":"done"}'),
        first_prompt="go",
    )

    assert result.status is TaskStatus.COMPLETE
    ((name, prompt, resume),) = seen
    assert name == "fake"
    assert resume == "ses_old"
    assert "#2" not in prompt
    assert storage.load_binding(paths).current.n == 1


def test_ghost_prior_owner_still_carries_the_switch_notice(config, task, capsys):
    """Accepted-degradation case, pinned for D6: an unregistered prior owner still
    gets the notice on the switch turn's review and on stderr."""
    slug, paths = task
    config = replace(config, provider="other")
    _seed(paths, Binding(
        root_uuid="root-1",
        provider="ghost",
        generations=[Generation(n=1, native_id="ses_old", transcript_bytes=10, provider="ghost")],
    ))
    seen: list = []
    lookup = _lookup({"other": _OtherProvider()})
    invoke, prompts = _recording_operator('{"verdict":"done"}')

    result = run_task(
        slug, config, _OtherProvider(), _runner(seen), invoke,
        first_prompt="go", provider_lookup=lookup,
    )

    assert result.status is TaskStatus.COMPLETE
    assert "Slot warning: provider switched 'ghost' -> 'other' (generation 2)" in prompts[0]
    assert "[warn] task: provider switched 'ghost' -> 'other' (generation 2)" in (
        capsys.readouterr().err
    )


def test_stale_binding_corner_writes_schema_v2_on_disk(config, task):
    """E12(a), on-disk half: the corner's first turn stamps v2 with the configured owner."""
    slug, paths = task
    config = replace(config, provider="other")
    _seed(paths, Binding(root_uuid="root-1", provider="fake"))
    seen: list = []
    lookup = _lookup({"fake": FakeProvider(), "other": _OtherProvider()})

    run_task(
        slug, config, FakeProvider(), _runner(seen), _verdicts('{"verdict":"done"}'),
        first_prompt="go", provider_lookup=lookup,
    )

    data = _disk(paths)
    assert data["v"] == 2
    assert data["provider"] == "other"
    assert data["generations"][0]["provider"] == "other"
    assert seen[0][0] == "other"


def test_reset_corner_review_carries_the_switch_notice(config, task, capsys):
    """E12(b), notice half: the reset turn's review tells the operator the CLI changed."""
    slug, paths = task
    config = replace(config, provider="other")
    _seed(paths, Binding(root_uuid="root-1", provider="fake"))
    seen: list = []
    lookup = _lookup({"fake": FakeProvider(), "other": _OtherProvider()})
    invoke, prompts = _recording_operator(
        '{"verdict":"continue","next_prompt":"step 1","reset_context":true}',
        '{"verdict":"done"}',
    )

    run_task(
        slug, config, FakeProvider(), _runner(seen), invoke, provider_lookup=lookup,
    )

    assert seen[0][0] == "other"
    assert "Slot warning: provider switched 'fake' -> 'other' (generation 1)" in prompts[1]
    assert "[warn] task: provider switched 'fake' -> 'other' (generation 1)" in (
        capsys.readouterr().err
    )


def test_switch_turn_spawns_and_ledgers_the_config_resolved_model(config, task):
    """E7: the switch turn's flags end with the config-resolved --model, and the
    ledger line records the same id."""
    slug, paths = task
    config = replace(config, provider="other", model="cfg-model")
    _seed(paths, _prior())
    extra: list = []
    lookup = _lookup({"fake": FakeProvider(), "other": _OtherProvider()})

    def runner(prov, spec):
        extra.append(spec.extra_args)
        return _ok(prov, spec)

    run_task(
        slug, config, FakeProvider(), runner, _verdicts('{"verdict":"done"}'),
        first_prompt="go", provider_lookup=lookup,
    )

    assert extra[0][-2:] == ("--model", "cfg-model")
    assert list(storage.read_ledger(paths))[-1].model == "cfg-model"
