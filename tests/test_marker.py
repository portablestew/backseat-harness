from backseat_harness.marker import (
    OPERATOR_SESSION_RE,
    format_marker,
    format_operator_marker,
    new_root_uuid,
    parse_marker,
    parse_operator_marker,
    prepend_marker,
)


def test_roundtrip():
    root = new_root_uuid()
    text = prepend_marker(root, 3, "do the thing")
    parsed = parse_marker(text)
    assert parsed == (root, 3)


def test_format():
    root = "12345678-1234-1234-1234-123456789abc"
    assert format_marker(root, 1) == f"<meta>START SESSION {root} #1</meta>"


def test_parse_absent():
    assert parse_marker("no marker here") is None


def test_prepend_puts_marker_first():
    root = new_root_uuid()
    text = prepend_marker(root, 2, "prompt body")
    assert text.startswith("<meta>START SESSION")
    assert text.rstrip().endswith("prompt body")


def test_redact_removes_root_uuid_but_keeps_generation():
    from backseat_harness.marker import REDACTED_ROOT, redact

    root = new_root_uuid()
    text = prepend_marker(root, 2, f"see {root} again")
    out = redact(text, root)
    assert root not in out
    assert f"<meta>START SESSION {REDACTED_ROOT} #2</meta>" in out
    # A redacted marker no longer parses, so it can't be mistaken for identity.
    assert parse_marker(out) is None


def test_operator_marker_bare():
    assert format_operator_marker() == "<meta>OPERATOR SESSION</meta>"


def test_operator_marker_with_slug_and_phase():
    assert (
        format_operator_marker(slug="demo", phase="review")
        == "<meta>OPERATOR SESSION demo review</meta>"
    )


def test_operator_marker_matches_and_never_holds_a_uuid():
    root = new_root_uuid()
    text = format_operator_marker(slug="demo", phase="plan")
    assert OPERATOR_SESSION_RE.search(text)
    # The operator tag is UUID-free by contract: an operator session must never
    # match the task's root-UUID marker search even if the slug were a UUID.
    assert root not in text
    assert parse_marker(text) is None


def test_operator_marker_requires_a_real_token():
    """A tag-shaped string with no slug/phase token is a description of the tag.

    The docs quote the syntax with an elision (``...``); the sanitized-slug and
    phase vocabularies never produce it, so neither form is a real marker --
    which is what keeps a chat *about* the harness from classifying as one.
    """
    for quoted in (
        "<meta>OPERATOR SESSION</meta>",
        "<meta>OPERATOR SESSION ...</meta>",
        "<meta>OPERATOR SESSION <slug> <phase></meta>",
    ):
        assert OPERATOR_SESSION_RE.search(quoted) is None, quoted
        assert parse_operator_marker(quoted) is None, quoted


def test_operator_marker_accepts_the_slug_phase_and_phase_only_forms():
    # Task-scoped: slug then phase.
    assert OPERATOR_SESSION_RE.search(format_operator_marker(slug="demo", phase="plan"))
    # Task-less: the phase token sits in the slug position.
    phase_only = format_operator_marker(phase="review")
    assert OPERATOR_SESSION_RE.search(phase_only)
    assert parse_operator_marker(phase_only) == "review"
    # The manager tags with ``manager-<phase>`` in the slug position.
    manager = format_operator_marker(phase="manager-draft")
    assert OPERATOR_SESSION_RE.search(manager)
    assert parse_operator_marker(manager) == "manager-draft"
