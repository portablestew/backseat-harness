"""Checklist merge protocol: additive, whitelisted, free-status, no deletes."""

import json
import os

import pytest

from backseat_harness.checklist import Checklist, all_done, merge_update, save_checklist


def test_upsert_new_step():
    result = merge_update(Checklist(), {"steps": {"s1": {"title": "First", "status": "active"}}})
    assert result.steps["s1"]["title"] == "First"
    assert result.steps["s1"]["status"] == "active"


def test_unknown_fields_dropped():
    result = merge_update(Checklist(), {"steps": {"s1": {"status": "pending", "danger": "x"}}})
    assert "danger" not in result.steps["s1"]


def test_model_slot_survives_merge_and_roundtrip(tmp_path):
    delta = {"steps": {"s1": {"title": "Probe", "status": "active", "model": "thinker"}}}
    result = merge_update(Checklist(), delta)
    assert result.steps["s1"]["model"] == "thinker"
    # Round-trips through the on-disk JSON form unmolested.
    path = tmp_path / "checklist.json"
    save_checklist(path, result)
    assert json.loads(path.read_text(encoding="utf-8"))["steps"]["s1"]["model"] == "thinker"
    reloaded = Checklist.from_dict(json.loads(path.read_text(encoding="utf-8")))
    assert reloaded.steps["s1"]["model"] == "thinker"


def test_model_null_and_non_string_stripped():
    # nulls are stripped like every other field (deletes are forbidden, so a
    # slot is re-pointed, never removed); the value is loop-owned pass-through,
    # so a non-string is merged verbatim like any other whitelisted field.
    base = merge_update(Checklist(), {"steps": {"s1": {"model": "thinker", "status": "active"}}})
    result = merge_update(base, {"steps": {"s1": {"model": None}}})
    assert result.steps["s1"]["model"] == "thinker"  # null did not clear the slot
    result2 = merge_update(Checklist(), {"steps": {"s1": {"model": 7}}})
    assert result2.steps["s1"]["model"] == 7  # no semantics here; loop validates
    # Same rule for other fields: a null does not delete an existing value.
    base = merge_update(Checklist(), {"steps": {"s1": {"status": "active", "notes": "keep"}}})
    result3 = merge_update(base, {"steps": {"s1": {"notes": None, "status": "done"}}})
    assert result3.steps["s1"]["notes"] == "keep"  # null did not delete
    assert result3.steps["s1"]["status"] == "done"


def test_invalid_status_ignored():
    base = merge_update(Checklist(), {"steps": {"s1": {"status": "done"}}})
    result = merge_update(base, {"steps": {"s1": {"status": "banana"}}})
    assert result.steps["s1"]["status"] == "done"


def test_remarks_append():
    base = merge_update(Checklist(), {"remarks": ["a"]})
    result = merge_update(base, {"remarks": ["b"]})
    assert result.remarks == ["a", "b"]


def test_merge_does_not_mutate_input():
    base = Checklist(steps={"s1": {"status": "active"}})
    merge_update(base, {"steps": {"s1": {"status": "done"}}})
    assert base.steps["s1"]["status"] == "active"


def test_updated_at_is_stamped_when_a_step_changes():
    created = merge_update(
        Checklist(), {"steps": {"s1": {"title": "Probe", "status": "active"}}}, now=100.0
    )
    assert created.steps["s1"]["updated_at"] == 100.0

    # A no-op resend is not a change, so the stamp survives.
    unchanged = merge_update(
        created, {"steps": {"s1": {"title": "Probe", "status": "active"}}}, now=200.0
    )
    assert unchanged.steps["s1"]["updated_at"] == 100.0

    changed = merge_update(unchanged, {"steps": {"s1": {"status": "done"}}}, now=300.0)
    assert changed.steps["s1"]["updated_at"] == 300.0


def test_updated_at_is_harness_owned():
    created = merge_update(
        Checklist(), {"steps": {"s1": {"title": "Probe", "updated_at": 1.0}}}, now=100.0
    )
    assert created.steps["s1"]["updated_at"] == 100.0

    # A delta that only carries the harness-owned field changes nothing.
    touched = merge_update(created, {"steps": {"s1": {"updated_at": 999.0}}}, now=200.0)
    assert touched.steps["s1"]["updated_at"] == 100.0


def test_all_done():
    assert not all_done(Checklist())  # empty is not done
    assert not all_done(Checklist(steps={"s1": {"status": "active"}}))
    assert all_done(Checklist(steps={"s1": {"status": "done"}, "s2": {"status": "done"}}))


@pytest.mark.parametrize(
    "before, after",
    [
        (None, "cancelled"),  # cancelled is a valid status
        ("done", "pending"),  # no forward-only ordering: the operator may move a step back
        ("cancelled", "active"),  # ...and a declined step can be revived
    ],
)
def test_status_transitions_are_free(before, after):
    if before is None:
        base = Checklist()
    else:
        base = merge_update(Checklist(), {"steps": {"s1": {"status": before}}})
    result = merge_update(base, {"steps": {"s1": {"status": after}}})
    assert result.steps["s1"]["status"] == after


def test_cancelled_satisfies_the_completion_gate():
    """The owner declining a step settles it; it is not outstanding work."""
    done_and_cancelled = {"s1": {"status": "done"}, "s2": {"status": "cancelled"}}
    assert all_done(Checklist(steps=done_and_cancelled))
    # A lane whose every step was declined is finished; the answer is "don't build it".
    assert all_done(Checklist(steps={"s1": {"status": "cancelled"}}))
    # But one still-open step keeps it open, cancelled or not.
    for open_status in ("active", "pending"):
        steps = {"s1": {"status": "cancelled"}, "s2": {"status": open_status}}
        assert not all_done(Checklist(steps=steps))


def test_save_checklist_is_atomic_and_retries_a_contended_swap(tmp_path, monkeypatch):
    """The checklist rides the same door as state/binding: no torn file, no
    crashed run on a losing replace race."""
    from backseat_harness import storage

    real_replace = os.replace
    calls = []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) == 1:
            raise PermissionError(5, "Access is denied", str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(storage.os, "replace", flaky)
    monkeypatch.setattr(storage.time, "sleep", lambda _s: None)

    path = tmp_path / "checklist.json"
    result = merge_update(Checklist(), {"steps": {"s1": {"title": "First", "status": "active"}}})
    save_checklist(path, result)

    assert len(calls) == 2
    assert Checklist.from_dict(json.loads(path.read_text(encoding="utf-8"))).steps
    # The failed attempt's temp file is cleaned up rather than stranded.
    assert list(tmp_path.glob("*.tmp")) == []


def test_save_checklist_never_leaves_a_half_written_file(tmp_path, monkeypatch):
    """A write that fails outright must leave the previous checklist intact --
    ``load_checklist`` parses unconditionally, so a truncated tail would raise."""
    from backseat_harness import storage

    path = tmp_path / "checklist.json"
    save_checklist(path, merge_update(Checklist(), {"steps": {"s1": {"status": "active"}}}))
    before = path.read_text(encoding="utf-8")

    def boom(src, dst):
        raise PermissionError(5, "Access is denied", str(dst))

    monkeypatch.setattr(storage.os, "replace", boom)
    monkeypatch.setattr(storage.time, "sleep", lambda _s: None)

    with pytest.raises(PermissionError):
        save_checklist(path, merge_update(Checklist(), {"steps": {"s2": {"status": "done"}}}))

    assert path.read_text(encoding="utf-8") == before
    assert list(tmp_path.glob("*.tmp")) == []


def test_save_checklist_keeps_non_ascii_unescaped(tmp_path):
    """``ensure_ascii=False`` survives the switch: the operator writes titles and
    remarks that are not ASCII, and they stay readable on disk."""
    path = tmp_path / "checklist.json"
    delta = {"steps": {"s1": {"title": "café — naïve", "status": "active"}}}
    save_checklist(path, merge_update(Checklist(), delta))

    assert "café — naïve" in path.read_text(encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["steps"]["s1"]["title"] == "café — naïve"