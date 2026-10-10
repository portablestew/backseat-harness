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


def test_windows_artifact_is_valid_task_xml_with_vbs_wrapper(monkeypatch, tmp_path):
    win = platform._WindowsPlatform()
    monkeypatch.setattr(platform, "platform_impl", win)

    frozen_path = "/fake/frozen/path"
    original_get = os.environ.get
    def fake_get(k, default=""):
        return frozen_path if k == "PATH" else original_get(k, default)
    monkeypatch.setattr(os.environ, "get", fake_get)

    # Point artifacts to temp directory
    monkeypatch.setattr(win, "artifact_path", lambda: tmp_path / "backseat-daemon.xml")
    monkeypatch.setattr(win, "_wrapper_path", lambda: tmp_path / "backseat-daemon-wrapper.vbs")

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
    # The daemon runs as the ordinary logged-on user, unelevated. HighestAvailable
    # would hand a model-driven agent -- and every provider CLI it spawns -- a full
    # admin token at every logon, silently, and make the daemon un-stoppable from
    # a normal shell.
    assert "<RunLevel>LeastPrivilege</RunLevel>" in text_content
    assert "HighestAvailable" not in text_content
    # The logon trigger is scoped to this account. Unscoped, it means "any user
    # logging on": the daemon would start under whichever account logs in first,
    # and registering that machine-wide trigger is an administrative write (the
    # XML import is denied without elevation, where the plain one is not).
    trigger = root.find(
        "{http://schemas.microsoft.com/windows/2004/02/mit/task}Triggers/"
        "{http://schemas.microsoft.com/windows/2004/02/mit/task}LogonTrigger"
    )
    assert trigger is not None
    trigger_user = trigger.find(
        "{http://schemas.microsoft.com/windows/2004/02/mit/task}UserId"
    )
    assert trigger_user is not None, "the logon trigger must name the account it runs for"
    assert trigger_user.text == win._user_id()
    # Action runs wscript.exe (a windowless host) on the wrapper .vbs, never a
    # console app; naming the host directly also keeps a CScript file
    # association from putting a console window back.
    action = root.find("{http://schemas.microsoft.com/windows/2004/02/mit/task}Actions/"
                       "{http://schemas.microsoft.com/windows/2004/02/mit/task}Exec")
    command_el = action.find("{http://schemas.microsoft.com/windows/2004/02/mit/task}Command")
    arguments_el = action.find("{http://schemas.microsoft.com/windows/2004/02/mit/task}Arguments")
    assert command_el.text.endswith("wscript.exe")
    assert (arguments_el.text or "").strip('"') == str(tmp_path / "backseat-daemon-wrapper.vbs")

    # 2. Wrapper script assertions
    wrapper = tmp_path / "backseat-daemon-wrapper.vbs"
    assert wrapper.exists(), "Wrapper .vbs must exist with frozen PATH"
    raw = wrapper.read_bytes()
    # Written Unicode (UTF-16 LE BOM) so non-ASCII paths survive, like the XML.
    assert raw[:2] == b'\xff\xfe'
    wrapper_text = raw.decode('utf-16-le')
    assert 'sh.Environment("PROCESS")("PATH") = ' in wrapper_text
    assert platform._vbs_string(frozen_path) in wrapper_text, (
        "Wrapper must contain frozen PATH"
    )
    # The launch command is daemon run --web, launched hidden (window style 0)
    # and waited on, so the task instance stays Running and `schtasks /end`
    # (daemon stop/restart) can still terminate the whole tree.
    assert wrapper_text.count("sh.Run(") == 1
    assert platform._vbs_string(
        f'"{sys.executable}" -m backseat_harness.cli daemon run --web'
    ) in wrapper_text
    assert ", 0, True)" in wrapper_text
    # The daemon's exit code is re-raised, so a nonzero death still trips the
    # task's RestartOnFailure exactly as the old .bat's exit code did.
    assert "WScript.Quit rc" in wrapper_text

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
    monkeypatch.setattr(win, "_wrapper_path", lambda: tmp_path / "backseat-daemon-wrapper.vbs")

    win.register()
    win.register()

    wrapper_text = (tmp_path / "backseat-daemon-wrapper.vbs").read_bytes().decode("utf-16-le")
    assert platform._vbs_string(
        f'"{sys.executable}" -m backseat_harness.cli daemon run --web'
    ) in wrapper_text


def test_windows_register_leaves_legacy_bat_for_uninstall(monkeypatch, tmp_path):
    """A pre-VBS install's .bat survives a reinstall and is cleaned on uninstall.

    Deleting it inside register() would be wrong: until schtasks /create has
    re-pointed the task at the .vbs, the registered task still launches that
    .bat, so a failed /create (non-elevated install, say) would leave the task
    pointing at a deleted file. It is inert once the task is re-pointed, and
    uninstall removes it either way.
    """
    win = platform._WindowsPlatform()
    monkeypatch.setattr(win, "artifact_path", lambda: tmp_path / "backseat-daemon.xml")
    monkeypatch.setattr(win, "_wrapper_path", lambda: tmp_path / "backseat-daemon-wrapper.vbs")
    legacy = tmp_path / "backseat-daemon-wrapper.bat"
    legacy.write_text("@echo off\n", encoding="utf-8")

    win.register()
    assert legacy.exists(), "register must not delete the launcher still in use"

    win.uninstall()
    assert not legacy.exists(), "uninstall must drop the legacy .bat launcher"
    assert not (tmp_path / "backseat-daemon-wrapper.vbs").exists()
    assert not (tmp_path / "backseat-daemon.xml").exists()


def _vbs_chunk_contents(literal: str) -> list[str]:
    """The unescaped content of each quoted chunk in a wrapped literal.

    Splitting on the continuation leaves one quoted literal per piece; every
    piece must be a complete string literal on its own, and concatenating
    their unescaped contents must rebuild the original value -- which is what
    catches a quote straddling a chunk boundary.
    """
    pieces = literal.split(" & _\n    ")
    contents = []
    for piece in pieces:
        assert piece.startswith('"') and piece.endswith('"'), f"not a literal: {piece!r}"
        contents.append(piece[1:-1].replace('""', '"'))
    return contents


def test_vbs_string_wraps_long_literals_with_continuations():
    """A PATH-sized literal is chunked over short lines the host accepts."""
    assert platform._vbs_string("abc") == '"abc"'
    assert platform._vbs_string("") == '""'
    assert platform._vbs_string('a"b') == '"a""b"', "quotes are doubled, not split"

    long_value = "x" * 250
    literal = platform._vbs_string(long_value)
    assert platform._VBS_LITERAL_CHUNK < len(long_value)
    # Chunks carry their own quotes with the continuation in between, and the
    # VBScript `&` between them rebuilds the original value exactly.
    assert " & _\n    " in literal
    assert _vbs_chunk_contents(literal) == [long_value[i : i + platform._VBS_LITERAL_CHUNK]
                                            for i in range(0, len(long_value),
                                                           platform._VBS_LITERAL_CHUNK)]

    # A quote landing exactly on a chunk boundary stays escaped inside its own
    # chunk rather than being split into a stray doubled quote.
    straddling = "a" * platform._VBS_LITERAL_CHUNK + '"b'
    wrapped = platform._vbs_string(straddling)
    assert wrapped.count(" & _\n    ") == 1
    assert "".join(_vbs_chunk_contents(wrapped)) == straddling


def test_linux_register_twice_keeps_web_flag(monkeypatch, tmp_path):
    """A second register (reinstall) rewrites the unit and keeps --web."""
    linux = platform._LinuxPlatform()
    monkeypatch.setattr(linux, "artifact_path", lambda: tmp_path / "backseat-daemon.service")

    linux.register()
    linux.register()

    content = (tmp_path / "backseat-daemon.service").read_text(encoding="utf-8")
    exec_start = f"ExecStart={sys.executable} -m backseat_harness.cli daemon run --web"
    assert exec_start in content.splitlines()
