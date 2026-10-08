import os
import sys
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
    # The launch line is the wrapper's last non-empty line: --web must be on it.
    launch_line = [line for line in wrapper_text.splitlines() if line.strip()][-1]
    assert launch_line == f'"{sys.executable}" -m backseat_harness.cli daemon run --web', (
        "Wrapper must launch daemon run --web"
    )

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


@pytest.fixture(autouse=True)
def fake_systemctl(monkeypatch):
    """Never invoke a real systemctl command."""
    calls = []
    def capture(cmd):
        calls.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(platform, "_run_systemctl", capture)
    import backseat_harness.platform as _mod
    _mod._SYSTEMCTL_CALLS = calls


def test_linux_service_artifact_and_systemctl_calls(monkeypatch, tmp_path):
    linux = platform._LinuxPlatform()

    frozen_path = "/fake/linux/path"
    original_get = os.environ.get
    def fake_get(k, default=""):
        return frozen_path if k == "PATH" else original_get(k, default)
    monkeypatch.setattr(os.environ, "get", fake_get)

    monkeypatch.setattr(linux, "artifact_path", lambda: tmp_path / "backseat-daemon.service")

    # Capture all systemctl calls locally
    systemctl_calls = []
    def capture(cmd):
        systemctl_calls.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(platform, "_run_systemctl", capture)

    linux.register()

    # Artifact assertions
    artifact = tmp_path / "backseat-daemon.service"
    assert artifact.exists(), ".service file must exist after register"
    content = artifact.read_text(encoding="utf-8")
    assert "Restart=always" in content
    assert f"Environment=PATH={frozen_path}" in content
    exec_start = f"ExecStart={sys.executable} -m backseat_harness.cli daemon run --web"
    assert exec_start in content.splitlines()
    assert "[Unit]" in content
    assert "[Service]" in content
    assert "[Install]" in content
    assert "WantedBy=default.target" in content

    assert any("daemon-reload" in c for c in systemctl_calls), "Expected daemon-reload call"

    linux.start()
    assert any("start" in c for c in systemctl_calls), "Expected start call"

    linux.stop()
    assert any("stop" in c for c in systemctl_calls), "Expected stop call"

    linux.restart()
    assert any("restart" in c for c in systemctl_calls), \
        "Expected restart call (not stop+start sequence)"


def test_macos_plist_artifact_and_launchctl_calls(monkeypatch, tmp_path):
    mac = platform._MacOSPlatform()

    frozen_path = "/fake/macos/path"
    original_get = os.environ.get
    def fake_get(k, default=""):
        return frozen_path if k == "PATH" else original_get(k, default)
    monkeypatch.setattr(os.environ, "get", fake_get)

    monkeypatch.setattr(mac, "artifact_path", lambda: tmp_path / "backseat-daemon.plist")

    launchctl_calls = []
    def capture(cmd):
        launchctl_calls.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(platform, "_run_launchctl", capture)

    mac.register()

    artifact = tmp_path / "backseat-daemon.plist"
    assert artifact.exists(), ".plist file must exist after register"
    content = artifact.read_text(encoding="utf-8")
    assert "KeepAlive" in content
    assert "<true/>" in content
    assert f"<string>{frozen_path}</string>" in content
    assert "backseat-daemon" in content
    assert "ProgramArguments" in content
    # ProgramArguments must be exactly the daemon run --web invocation.
    root = ET.fromstring(content)
    top = list(root.find("dict"))
    values = dict(zip((k.text for k in top[::2]), top[1::2], strict=True))
    program_args = [s.text for s in values["ProgramArguments"].findall("string")]
    assert program_args == [
        sys.executable,
        "-m",
        "backseat_harness.cli",
        "daemon",
        "run",
        "--web",
    ]

    assert any("bootstrap" in c for c in launchctl_calls), \
        "Expected bootstrap call"
    # Verify domain is resolved (numeric uid, not literal $UID)
    for c in launchctl_calls:
        cmd_str = " ".join(str(x) for x in c)
        assert "gui/$UID" not in cmd_str, \
            "Domain must be resolved to numeric uid, not literal $UID"
        assert "gui/" in cmd_str, "Resolved gui/ domain expected"

    mac.start()
    assert any("kickstart" in c for c in launchctl_calls)

    mac.stop()
    assert any("bootout" in c for c in launchctl_calls)

    mac.restart()
    # Restart is non-atomic: bootout unregisters, bootstrap re-registers (not kickstart)
    restart_verb = launchctl_calls[-2:]  # last two calls: bootout, bootstrap
    assert "bootout" in " ".join(str(x) for x in restart_verb), \
        "Restart must begin with bootout"
    assert "bootstrap" in " ".join(str(x) for x in restart_verb), \
        "Restart must end with bootstrap (not kickstart)"
    restart_last = restart_verb[-1]
    assert "kickstart" not in " ".join(str(x) for x in [restart_last]), \
        "Restart must not use kickstart"


def test_windows_register_twice_keeps_web_flag(monkeypatch, tmp_path):
    """A second register (reinstall) rewrites the wrapper and keeps --web."""
    win = platform._WindowsPlatform()
    monkeypatch.setattr(win, "artifact_path", lambda: tmp_path / "backseat-daemon.xml")
    monkeypatch.setattr(win, "_wrapper_path", lambda: tmp_path / "backseat-daemon-wrapper.bat")

    win.register()
    win.register()

    wrapper_text = (tmp_path / "backseat-daemon-wrapper.bat").read_text(encoding="utf-8")
    launch_line = [line for line in wrapper_text.splitlines() if line.strip()][-1]
    assert launch_line.endswith(" -m backseat_harness.cli daemon run --web")


def test_linux_register_twice_keeps_web_flag(monkeypatch, tmp_path):
    """A second register (reinstall) rewrites the unit and keeps --web."""
    linux = platform._LinuxPlatform()
    monkeypatch.setattr(linux, "artifact_path", lambda: tmp_path / "backseat-daemon.service")

    linux.register()
    linux.register()

    content = (tmp_path / "backseat-daemon.service").read_text(encoding="utf-8")
    exec_start = f"ExecStart={sys.executable} -m backseat_harness.cli daemon run --web"
    assert exec_start in content.splitlines()
