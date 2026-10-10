"""Platform abstraction: artifact, scheduler, and PATH freeze per OS."""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
from pathlib import Path


class PlatformNotSupported(Exception):
    """Raised when sys.platform is not one of the three supported."""

    def __init__(self, message: str = "") -> None:
        super().__init__(
            message
            or "Platform not supported. Supported mechanisms: "
            "Windows Task Scheduler, Linux systemd user unit, "
            "macOS LaunchAgent (launchd)."
        )


def _select() -> _PlatformImpl:
    platform = sys.platform
    if platform == "win32":
        return _WindowsPlatform()
    if platform.startswith("linux"):
        return _LinuxPlatform()
    if platform == "darwin":
        return _MacOSPlatform()
    raise PlatformNotSupported()


class _PlatformImpl:
    def artifact_path(self) -> Path:
        raise NotImplementedError

    def register(self) -> None:
        raise NotImplementedError

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def restart(self) -> None:
        raise NotImplementedError

    def uninstall(self) -> None:
        raise NotImplementedError

    def is_registered(self) -> bool:
        raise NotImplementedError

    def platform_name(self) -> str:
        raise NotImplementedError


def _run_schtasks(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _xml_text(value: str) -> str:
    """Escape ``value`` for use as a Task Scheduler XML element text."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# VBScript rejects an arbitrarily long statement line, so a PATH-sized string
# literal is emitted as adjacent chunks joined by ``& _`` continuations.
_VBS_LITERAL_CHUNK = 96


def _vbs_string(value: str, chunk: int = _VBS_LITERAL_CHUNK) -> str:
    """``value`` as a VBScript string literal, wrapped over short lines.

    Each chunk carries its own quotes, so the separator holds only the
    concatenation and the line continuation::

        "first chunk" & _
        "second chunk"

    Escaping happens per chunk and chunking splits between characters, so a
    doubled quote is never cut in half by a line continuation.
    """
    pieces = [value[i : i + chunk] for i in range(0, len(value), chunk)] or [""]
    literals = ['"' + piece.replace('"', '""') + '"' for piece in pieces]
    if len(literals) == 1:
        return literals[0]
    return ' & _\n    '.join(literals)


def _run_systemctl(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _run_launchctl(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


class _WindowsPlatform(_PlatformImpl):
    def artifact_path(self) -> Path:
        return Path.home() / ".backseat" / "daemon" / "backseat-daemon.xml"

    def _wrapper_path(self) -> Path:
        return self.artifact_path().with_suffix(".vbs")

    def _legacy_wrapper_path(self) -> Path:
        """The pre-VBS ``.bat`` launcher, still around on an upgraded install."""
        return self._wrapper_path().with_suffix(".bat")

    def _command_line(self) -> str:
        return f'"{sys.executable}" -m backseat_harness.cli daemon run --web'

    def _wscript_path(self) -> str:
        """Absolute ``wscript.exe``: the windowless host that runs the wrapper.

        It is named in ``<Command>`` rather than the ``.vbs`` itself so the
        launch does not depend on the user's ``.vbs`` file association -- a
        CScript default would put the console window straight back.
        """
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        return str(system_root / "System32" / "wscript.exe")

    def _wrapper_content(self) -> str:
        """The launcher: freeze PATH, then run the daemon without a window.

        ``Environment("PROCESS")`` hands the frozen PATH down exactly the way
        the old ``set PATH=`` line did: this process and its descendants, and
        nothing persisted. ``WshShell.Run`` window style 0 is ``SW_HIDE``, so
        python still gets a console -- stdout/stderr stay real handles and the
        CLI's prints keep working -- but it is never shown. ``bWaitOnReturn``
        is true so wscript outlives the daemon and the task instance stays
        Running, which is what lets ``schtasks /end`` (daemon stop/restart)
        terminate the whole tree; the return code is then re-raised so a daemon
        that died nonzero still trips the task's RestartOnFailure, exactly as
        the old ``.bat`` did by exiting with python's code. wscript.exe is
        itself a windowless host, so no visible window is created at any point
        in the chain.
        """
        frozen = os.environ.get("PATH", "")
        return (
            'Set sh = CreateObject("WScript.Shell")\n'
            'sh.Environment("PROCESS")("PATH") = ' + _vbs_string(frozen) + '\n'
            'rc = sh.Run(' + _vbs_string(self._command_line()) + ', 0, True)\n'
            'WScript.Quit rc\n'
        )

    def _user_id(self) -> str:
        """``DOMAIN\\user`` for the account installing this task.

        A ``LogonTrigger`` with no ``UserId`` fires for *any* user logging on.
        That is a machine-wide trigger: registering one is an administrative
        write (plain ``schtasks /create`` needs no elevation, the XML import is
        denied without it), and the daemon would then start under whichever
        account logs in first rather than this one. Scoping the trigger to the
        installing account is what makes the registration an ordinary,
        unelevated one -- and the task itself already runs as that user via
        ``InteractiveToken``.
        """
        user = os.environ.get("USERNAME") or getpass.getuser()
        domain = os.environ.get("USERDOMAIN")
        return f"{domain}\\{user}" if domain else user

    def _build_xml(self) -> str:
        wrapper = self._wrapper_path()
        wscript = self._wscript_path()
        arguments = _xml_text(f'"{wrapper}"')
        user_id = _xml_text(self._user_id())
        xml_content = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>backseat-harness</Author>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user_id}</UserId>
      <Delay>PT30S</Delay>
    </LogonTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>{_xml_text(wscript)}</Command>
      <Arguments>{arguments}</Arguments>
    </Exec>
  </Actions>
</Task>"""
        return xml_content

    def register(self) -> None:
        artifact = self.artifact_path()
        artifact.parent.mkdir(parents=True, exist_ok=True)
        # A pre-VBS install's .bat is left alone here: until schtasks /create has
        # re-pointed the task at the .vbs it is still the registered task's
        # launcher. It stops mattering the moment that call succeeds, and
        # uninstall removes it either way, so deleting it by hand after a
        # successful install is enough.
        wrapper_path = self._wrapper_path()
        # UTF-16 LE with BOM, like the XML beside it: the Windows Script Host
        # reads that as Unicode, so a non-ASCII profile path survives the write.
        wrapper_path.write_bytes(b"\xff\xfe" + self._wrapper_content().encode("utf-16-le"))
        xml_content = self._build_xml()
        artifact.write_bytes(b"\xff\xfe" + xml_content.encode("utf-16-le"))
        cmd = [
            "schtasks",
            "/create",
            "/XML",
            str(artifact),
            "/TN",
            "backseat-daemon",
            "/F",
        ]
        result = _run_schtasks(cmd)
        if result.returncode != 0:
            stderr = result.stderr or ""
            if "Access is denied" in stderr or result.returncode == 5:
                raise RuntimeError(
                    "schtasks /create failed: Access is denied. The task is "
                    "registered for this user with LeastPrivilege, so this is "
                    "not an elevation requirement -- check whether another "
                    "policy or session is denying this account a write to the "
                    "Task Scheduler store."
                )
            raise RuntimeError(f"schtasks /create failed (rc={result.returncode}): {stderr}")

    def start(self) -> None:
        cmd = ["schtasks", "/run", "/tn", "backseat-daemon"]
        result = _run_schtasks(cmd)
        if result.returncode != 0:
            raise RuntimeError(f"schtasks /run failed (rc={result.returncode}): {result.stderr}")

    def stop(self) -> None:
        cmd = ["schtasks", "/end", "/tn", "backseat-daemon"]
        _run_schtasks(cmd)
        # /end may fail if task is not running; ignore non-zero when task absent

    def restart(self) -> None:
        self.stop()
        self.start()

    def uninstall(self) -> None:
        cmd = ["schtasks", "/delete", "/f", "/tn", "backseat-daemon"]
        _run_schtasks(cmd)
        artifact = self.artifact_path()
        if artifact.exists():
            artifact.unlink()
        for wrapper in (self._wrapper_path(), self._legacy_wrapper_path()):
            if wrapper.exists():
                wrapper.unlink()

    def is_registered(self) -> bool:
        cmd = ["schtasks", "/query", "/tn", "backseat-daemon", "/fo", "LIST"]
        result = _run_schtasks(cmd)
        return result.returncode == 0 and "backseat-daemon" in (result.stdout or "")

    def platform_name(self) -> str:
        return "Windows Task Scheduler"


class _LinuxPlatform(_PlatformImpl):
    def artifact_path(self) -> Path:
        return Path.home() / ".config" / "systemd" / "user" / "backseat-daemon.service"

    def _service_content(self) -> str:
        frozen_path = os.environ.get("PATH", "")
        return f"""[Unit]
Description=backseat-harness daemon
[Service]
ExecStart={sys.executable} -m backseat_harness.cli daemon run --web
Restart=always
Environment=PATH={frozen_path}
[Install]
WantedBy=default.target
"""

    def register(self) -> None:
        artifact = self.artifact_path()
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(self._service_content(), encoding="utf-8")
        cmd = ["systemctl", "--user", "daemon-reload"]
        result = _run_systemctl(cmd)
        if result.returncode != 0:
            raise RuntimeError(
                f"systemctl daemon-reload failed (rc={result.returncode}): {result.stderr}"
            )

    def start(self) -> None:
        cmd = ["systemctl", "--user", "start", "backseat-daemon"]
        result = _run_systemctl(cmd)
        if result.returncode != 0:
            raise RuntimeError(f"systemctl start failed (rc={result.returncode}): {result.stderr}")

    def stop(self) -> None:
        cmd = ["systemctl", "--user", "stop", "backseat-daemon"]
        _run_systemctl(cmd)

    def restart(self) -> None:
        cmd = ["systemctl", "--user", "restart", "backseat-daemon"]
        result = _run_systemctl(cmd)
        if result.returncode != 0:
            raise RuntimeError(
                f"systemctl restart failed (rc={result.returncode}): {result.stderr}"
            )

    def uninstall(self) -> None:
        cmd_stop = ["systemctl", "--user", "stop", "backseat-daemon"]
        _run_systemctl(cmd_stop)
        cmd_reload = ["systemctl", "--user", "daemon-reload"]
        _run_systemctl(cmd_reload)
        artifact = self.artifact_path()
        if artifact.exists():
            artifact.unlink()

    def is_registered(self) -> bool:
        return self.artifact_path().exists()

    def platform_name(self) -> str:
        return "Linux systemd user unit"


def _uid_domain() -> str:
    try:
        return f"gui/{os.getuid()}"
    except AttributeError:
        # Fallback for hosts without getuid (e.g. Windows test hosts)
        return "gui/501"


class _MacOSPlatform(_PlatformImpl):
    def artifact_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / "backseat-daemon.plist"

    def _plist_content(self) -> str:
        frozen_path = os.environ.get("PATH", "")
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>backseat-daemon</string>
  <key>ProgramArguments</key>
  <array>
    <string>{sys.executable}</string>
    <string>-m</string>
    <string>backseat_harness.cli</string>
    <string>daemon</string>
    <string>run</string>
    <string>--web</string>
  </array>
  <key>KeepAlive</key>
  <true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>{frozen_path}</string>
  </dict>
</dict>
</plist>"""

    def register(self) -> None:
        artifact = self.artifact_path()
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(self._plist_content(), encoding="utf-8")
        cmd = [
            "launchctl",
            "bootstrap",
            _uid_domain(),
            str(artifact),
        ]
        result = _run_launchctl(cmd)
        if result.returncode != 0:
            raise RuntimeError(
                f"launchctl bootstrap failed (rc={result.returncode}): {result.stderr}"
            )

    def start(self) -> None:
        cmd = ["launchctl", "kickstart", "-k", f"{_uid_domain()}/backseat-daemon"]
        result = _run_launchctl(cmd)
        if result.returncode != 0:
            raise RuntimeError(
                f"launchctl kickstart failed (rc={result.returncode}): {result.stderr}"
            )

    def stop(self) -> None:
        cmd = ["launchctl", "bootout", f"{_uid_domain()}/backseat-daemon"]
        _run_launchctl(cmd)

    def restart(self) -> None:
        cmd_stop = ["launchctl", "bootout", f"{_uid_domain()}/backseat-daemon"]
        _run_launchctl(cmd_stop)
        artifact = self.artifact_path()
        cmd_start = [
            "launchctl",
            "bootstrap",
            _uid_domain(),
            str(artifact),
        ]
        result = _run_launchctl(cmd_start)
        if result.returncode != 0:
            raise RuntimeError(
                f"launchctl bootstrap (restart) failed (rc={result.returncode}): {result.stderr}"
            )

    def uninstall(self) -> None:
        cmd = ["launchctl", "bootout", f"{_uid_domain()}/backseat-daemon"]
        _run_launchctl(cmd)
        artifact = self.artifact_path()
        if artifact.exists():
            artifact.unlink()

    def is_registered(self) -> bool:
        return self.artifact_path().exists()

    def platform_name(self) -> str:
        return "macOS LaunchAgent (launchd)"


# Public interface used by CLI
platform_impl = _select()
artifact_path = platform_impl.artifact_path
register = platform_impl.register
start = platform_impl.start
stop = platform_impl.stop
restart = platform_impl.restart
uninstall = platform_impl.uninstall
is_registered = platform_impl.is_registered
platform_name = platform_impl.platform_name
