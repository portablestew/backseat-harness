"""Checklist merge protocol: additive, whitelisted, forward-only, no deletes."""

from backseat_harness.checklist import Checklist, all_done, merge_update


def test_upsert_new_step():
    result = merge_update(Checklist(), {"steps": {"s1": {"title": "First", "status": "active"}}})
    assert result.steps["s1"]["title"] == "First"
    assert result.steps["s1"]["status"] == "active"


def test_unknown_fields_dropped():
    result = merge_update(Checklist(), {"steps": {"s1": {"status": "pending", "danger": "x"}}})
    assert "danger" not in result.steps["s1"]


def test_null_values_stripped_not_deleting():
    base = merge_update(Checklist(), {"steps": {"s1": {"status": "active", "notes": "keep"}}})
    result = merge_update(base, {"steps": {"s1": {"notes": None, "status": "done"}}})
    assert result.steps["s1"]["notes"] == "keep"  # null did not delete
    assert result.steps["s1"]["status"] == "done"


def test_status_is_forward_only():
    base = merge_update(Checklist(), {"steps": {"s1": {"status": "done"}}})
    result = merge_update(base, {"steps": {"s1": {"status": "pending"}}})
    assert result.steps["s1"]["status"] == "done"  # cannot regress


def test_reopen_allows_regress():
    base = merge_update(Checklist(), {"steps": {"s1": {"status": "done"}}})
    result = merge_update(base, {"steps": {"s1": {"status": "active", "reopen": True}}})
    assert result.steps["s1"]["status"] == "active"


def test_remarks_append():
    base = merge_update(Checklist(), {"remarks": ["a"]})
    result = merge_update(base, {"remarks": ["b"]})
    assert result.remarks == ["a", "b"]


def test_merge_does_not_mutate_input():
    base = Checklist(steps={"s1": {"status": "active"}})
    merge_update(base, {"steps": {"s1": {"status": "done"}}})
    assert base.steps["s1"]["status"] == "active"


def test_all_done():
    assert not all_done(Checklist())  # empty is not done
    assert not all_done(Checklist(steps={"s1": {"status": "active"}}))
    assert all_done(Checklist(steps={"s1": {"status": "done"}, "s2": {"status": "done"}}))
