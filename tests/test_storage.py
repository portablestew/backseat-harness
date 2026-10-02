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
