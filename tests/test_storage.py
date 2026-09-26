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
