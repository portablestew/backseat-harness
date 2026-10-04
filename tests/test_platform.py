import os
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock

import pytest

from backseat_harness import platform


@pytest.fixture(autouse=True)
def fake_schtasks(monkeypatch):
    """Never invoke a real scheduler command."""
    calls = []
    def capture(cmd):
        calls.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(platform, "_run_schtasks", capture)
    import backseat_harness.platform as _mod
    _mod._SCHTASKS_CALLS = calls


def test_windows_artifact_is_valid_task_xml_with_wrapper(monkeypatch, tmp_path):
    win = platform._WindowsPlatform()
    monkeypatch.setattr(platform, "platform_impl", win)

    frozen_path = "/fake/frozen/path"
    original_get = os.environ.get
    def fake_get(k, default=""):
        return frozen_path if k == "PATH" else original_get(k, default)
    monkeypatch.setattr(os.environ, "get", fake_get)

    # Point artifacts to temp directory
    monkeypatch.setattr(win, "artifact_path", lambda: tmp_path / "backseat-daemon.xml")
    monkeypatch.setattr(win, "_wrapper_path", lambda: tmp_path / "backseat-daemon-wrapper.bat")

    win.register()

    # 1. Artifact XML assertions
    artifact = tmp_path / "backseat-daemon.xml"
    assert artifact.exists()
    content = artifact.read_bytes()
    # File is UTF-16 LE with BOM; decode for string assertions
    bom = content[:2] == b'\xff\xfe'
    text_content = content.decode('utf-16-le') if bom else content.decode('utf-8')
    # Namespace must be the valid MIT task namespace
    assert 'xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"' in text_content
    # Encoding declared as UTF-16
    assert '<?xml version="1.0" encoding="UTF-16"?>' in text_content
    # No invalid <Environment> tag
    assert "<Environment>" not in text_content
    # Schema-valid order verified against host-exported XML:
    # RegistrationInfo -> Principals -> Settings -> Triggers -> Actions
    # Parse XML (handle UTF-16 LE BOM)
    parse_bytes = content[2:] if content[:2] == b'\xff\xfe' else content
    root = ET.fromstring(parse_bytes)
    assert root.tag == "{http://schemas.microsoft.com/windows/2004/02/mit/task}Task"
    tags_in_order = [c.tag for c in root]
    expected_tags = [
        "{http://schemas.microsoft.com/windows/2004/02/mit/task}RegistrationInfo",
        "{http://schemas.microsoft.com/windows/2004/02/mit/task}Principals",
        "{http://schemas.microsoft.com/windows/2004/02/mit/task}Settings",
        "{http://schemas.microsoft.com/windows/2004/02/mit/task}Triggers",
        "{http://schemas.microsoft.com/windows/2004/02/mit/task}Actions",
    ]
    assert tags_in_order == expected_tags, f"XML element order invalid: {tags_in_order}"
    # RestartOnFailure uses schema-valid Count (not invalid Limit)
    assert "<Count>3</Count>" in text_content
    assert "<Limit>" not in text_content
    # InteractiveToken present
    assert "InteractiveToken" in text_content
    # Action points to wrapper .bat, not python directly
    has_wrapper = '.bat' in text_content or 'wrapper' in text_content or \
        'backseat-daemon-wrapper' in text_content
    assert has_wrapper

    # 2. Wrapper script assertions
    wrapper = tmp_path / "backseat-daemon-wrapper.bat"
    assert wrapper.exists(), "Wrapper .bat must exist with frozen PATH"
    wrapper_text = wrapper.read_text(encoding="utf-8")
    assert frozen_path in wrapper_text, "Wrapper must contain frozen PATH"
    assert "-m backseat_harness.cli daemon run" in wrapper_text, "Wrapper must invoke daemon run"

    # 3. Command-line assertions: no /SC /RL /IT alongside /XML
    calls = getattr(platform, "_SCHTASKS_CALLS", [])
    xml_call = [c for c in calls if "/XML" in c]
    assert len(xml_call) >= 1, "Expected /XML in schtasks call"
    for c in xml_call:
        assert "/SC" not in c, "Register command must not carry /SC when using /XML"
        assert "/RL" not in c, "Register command must not carry /RL when using /XML"
        assert "/IT" not in c, "Register command must not carry /IT when using /XML"
        assert "/F" in c, "Expected /F force flag"
        assert "/TN" in c and "backseat-daemon" in c, "Expected /TN backseat-daemon"
