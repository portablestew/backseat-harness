import os
from pathlib import Path

import pytest

from backseat_harness import storage
from backseat_harness.models import Binding, Generation, LedgerEntry, TaskState, VerdictKind


def test_state_roundtrip(config):
    paths = config.task_paths("t1")
    state = TaskState(slug="t1", iteration=5, generation=2)
    storage.save_state(paths, state)
    loaded = storage.load_state(paths, "t1")
    assert loaded.iteration == 5
    assert loaded.generation == 2
    assert loaded.slug == "t1"


def test_binding_roundtrip(config):
    paths = config.task_paths("t1")
    binding = Binding(
        root_uuid="root-1",
        provider="fake",
        generations=[Generation(n=1, native_id="s1", transcript_bytes=42)],
    )
    storage.save_binding(paths, binding)
    loaded = storage.load_binding(paths)
    assert loaded.root_uuid == "root-1"
    assert loaded.current.native_id == "s1"
    assert loaded.current.transcript_bytes == 42


def test_ledger_append_only(config):
    paths = config.task_paths("t1")
    storage.append_ledger(
        paths,
        LedgerEntry(
            iteration=1, generation=1, verdict=VerdictKind.CONTINUE,
            exit_code=0, timed_out=False, duration_s=1.0,
        ),
    )
    storage.append_ledger(
        paths,
        LedgerEntry(
            iteration=2, generation=1, verdict=VerdictKind.DONE,
            exit_code=0, timed_out=False, duration_s=2.0,
        ),
    )
    entries = list(storage.read_ledger(paths))
    assert len(entries) == 2
    assert entries[0].iteration == 1
    assert entries[1].verdict is VerdictKind.DONE


def test_ledger_records_next_prompt(config):
    paths = config.task_paths("t1")
    storage.append_ledger(
        paths,
        LedgerEntry(
            iteration=1, generation=1, verdict=VerdictKind.CONTINUE,
            exit_code=0, timed_out=False, duration_s=1.0,
            notes="planned", next_prompt="research the router",
        ),
    )
    (entry,) = list(storage.read_ledger(paths))
    assert entry.next_prompt == "research the router"
    # Terminal entries carry no prompt; the field round-trips as None.
    assert LedgerEntry.from_dict(
        LedgerEntry(
            iteration=2, generation=1, verdict=VerdictKind.DONE,
            exit_code=0, timed_out=False, duration_s=0.0,
        ).to_dict()
    ).next_prompt is None


def test_load_missing_returns_defaults(config):
    paths = config.task_paths("missing")
    assert storage.load_binding(paths) is None
    state = storage.load_state(paths, "missing")
    assert state.iteration == 0


def test_ledger_entry_carries_a_readable_timestamp(config):
    """Every entry gets `at` (epoch, for sorting) and `at_local` (for a human)."""
    import datetime
    import json

    paths = config.task_paths("t1")
    storage.append_ledger(
        paths,
        LedgerEntry(
            iteration=1, generation=1, verdict=VerdictKind.CONTINUE,
            exit_code=0, timed_out=False, duration_s=1.0, at=1_750_000_000.0,
        ),
    )
    row = json.loads(paths.ledger.read_text(encoding="utf-8").splitlines()[0])
    assert row["at"] == 1_750_000_000.0
    assert "at_local" in row
    # Derived from `at`, local time with an offset, second resolution.
    local = datetime.datetime.fromisoformat(row["at_local"])
    assert local.utcoffset() is not None
    assert abs(local.timestamp() - 1_750_000_000.0) < 1.0
    assert len(row["at_local"]) == len("2025-06-15T09:46:40-07:00")


def test_ledger_reads_back_without_the_derived_field(config):
    """`at_local` is write-only: a row written before it existed still loads."""
    paths = config.task_paths("t1")
    paths.ledger.parent.mkdir(parents=True, exist_ok=True)
    paths.ledger.write_text(
        '{"at": 1750000000.0, "iteration": 1, "generation": 1, '
        '"verdict": "continue", "exit_code": 0, "timed_out": false, '
        '"duration_s": 1.0}\n',
        encoding="utf-8",
    )
    entries = list(storage.read_ledger(paths))
    assert [e.iteration for e in entries] == [1]


def test_ledger_reads_back_a_line_written_before_the_model_field(config):
    """`model` is absent from every line written before it existed; those
    lines must keep loading, as null -- this lane's own ledger is all of them."""
    paths = config.task_paths("t1")
    paths.ledger.parent.mkdir(parents=True, exist_ok=True)
    paths.ledger.write_text(
        '{"at": 1790664771.0685432, "at_local": "2026-09-29T10:00:00-07:00", '
        '"phase": "plan", "iteration": 0, "generation": 1, '
        '"verdict": "continue", "exit_code": 0, "timed_out": false, '
        '"duration_s": 0.0, "notes": "planned", "next_prompt": "step 1", '
        '"reset_context": false}\n',
        encoding="utf-8",
    )
    (entry,) = list(storage.read_ledger(paths))
    assert entry.model is None
    assert entry.phase == "plan"
    assert entry.next_prompt == "step 1"
    # Round-tripping the old line back out writes the field as null.
    assert entry.to_dict()["model"] is None


def test_ledger_round_trips_owner_brief(config):
    """The run's brief lands on the plan line; the field round-trips."""
    paths = config.task_paths("t1")
    storage.append_ledger(
        paths,
        LedgerEntry(
            iteration=0, generation=1, verdict=VerdictKind.CONTINUE,
            exit_code=0, timed_out=False, duration_s=0.0,
            phase="plan", owner_brief="Keep the shared budget.",
        ),
    )
    (entry,) = list(storage.read_ledger(paths))
    assert entry.owner_brief == "Keep the shared budget."
    assert entry.to_dict()["owner_brief"] == "Keep the shared budget."


def test_ledger_reads_back_a_line_written_before_the_owner_brief_field(config):
    """`owner_brief` is absent from every line written before it existed;
    those lines must keep loading, as null -- this lane's own ledger is all of
    them."""
    paths = config.task_paths("t1")
    paths.ledger.parent.mkdir(parents=True, exist_ok=True)
    paths.ledger.write_text(
        '{"at": 1790664771.0685432, "at_local": "2026-09-29T10:00:00-07:00", '
        '"phase": "plan", "model": null, "iteration": 0, "generation": 1, '
        '"verdict": "continue", "exit_code": 0, "timed_out": false, '
        '"duration_s": 0.0, "notes": "planned", "next_prompt": "step 1", '
        '"reset_context": false}\n',
        encoding="utf-8",
    )
    (entry,) = list(storage.read_ledger(paths))
    assert entry.owner_brief is None
    assert entry.to_dict()["owner_brief"] is None


def test_atomic_write_retries_a_contended_replace(config, monkeypatch, capsys):
    """WinError 5 on the swap is a losing race, not a verdict: wait it out."""
    real_replace = os.replace
    calls = []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) == 1:
            raise PermissionError(5, "Access is denied", str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(storage.os, "replace", flaky)
    monkeypatch.setattr(storage.time, "sleep", lambda _s: None)

    paths = config.task_paths("t1")
    storage.save_state(paths, TaskState(slug="t1", iteration=7))

    assert len(calls) == 2
    assert storage.load_state(paths, "t1").iteration == 7
    assert "retrying" in capsys.readouterr().err


def test_atomic_write_gives_up_and_reraises(config, monkeypatch, capsys):
    """Exhausting the attempts reports the failure; it never swallows it."""
    monkeypatch.setattr(
        storage.os,
        "replace",
        lambda src, dst: (_ for _ in ()).throw(PermissionError(5, "denied", str(dst))),
    )
    monkeypatch.setattr(storage.time, "sleep", lambda _s: None)

    paths = config.task_paths("t1")
    with pytest.raises(PermissionError):
        storage.save_state(paths, TaskState(slug="t1", iteration=7))

    assert not paths.state.exists()
    assert list(paths.root.glob("*.tmp")) == []


def test_atomic_write_leaves_no_temp_file_behind(config, monkeypatch):
    """Each attempt cleans up its own temp file, so a retry cannot strand one."""
    real_replace = os.replace
    seen = []

    def flaky(src, dst):
        seen.append(src)
        if len(seen) == 1:
            raise PermissionError(5, "denied", str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(storage.os, "replace", flaky)
    monkeypatch.setattr(storage.time, "sleep", lambda _s: None)

    paths = config.task_paths("t1")
    storage.save_state(paths, TaskState(slug="t1", iteration=1))

    assert not Path(seen[0]).exists()
    assert list(paths.root.glob("*.tmp")) == []