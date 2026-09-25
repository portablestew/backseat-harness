from backseat_harness.marker import (
    format_marker,
    new_root_uuid,
    parse_marker,
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
