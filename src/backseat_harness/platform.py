"""Platform abstraction: artifact, scheduler, and PATH freeze per OS."""

from __future__ import annotations

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


class _WindowsPlatform(_PlatformImpl):
    def artifact_path(self) -> Path:
        return Path.home() / ".backseat" / "daemon" / "backseat-daemon.xml"

    def _wrapper_path(self) -> Path:
        return self.artifact_path().with_suffix(".bat")

    def _command_line(self) -> str:
        return f'"{sys.executable}" -m backseat_harness.cli daemon run'

    def _build_xml(self) -> str:
        wrapper = self._wrapper_path()
        xml_content = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>backseat-harness</Author>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
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
      <Delay>PT30S</Delay>
    </LogonTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>{wrapper}</Command>
      <Arguments></Arguments>
    </Exec>
  </Actions>
</Task>"""
        return xml_content

    def register(self) -> None:
        artifact = self.artifact_path()
        artifact.parent.mkdir(parents=True, exist_ok=True)
        frozen_path = os.environ.get("PATH", "")
        wrapper_content = f"@echo off\nset PATH={frozen_path}\n{self._command_line()}\n"
        wrapper_path = self._wrapper_path()
        wrapper_path.write_text(wrapper_content, encoding="utf-8")
        xml_content = self._build_xml()
        artifact.write_bytes(b'\xff\xfe' + xml_content.encode('utf-16-le'))
        cmd = [
            "schtasks",
            "/create",
            "/XML", str(artifact),
            "/TN", "backseat-daemon",
            "/F",
        ]
        result = _run_schtasks(cmd)
        if result.returncode != 0:
            raise RuntimeError(
                f"schtasks /create failed (rc={result.returncode}): {result.stderr}"
            )

    def start(self) -> None:
        cmd = ["schtasks", "/run", "/tn", "backseat-daemon"]
        result = _run_schtasks(cmd)
        if result.returncode != 0:
            raise RuntimeError(
                f"schtasks /run failed (rc={result.returncode}): {result.stderr}"
            )

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
        wrapper = self._wrapper_path()
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

    def register(self) -> None:
        raise NotImplementedError("Linux: implement .service + daemon-reload")

    def start(self) -> None:
        raise NotImplementedError("Linux: implement systemctl --user start")

    def stop(self) -> None:
        raise NotImplementedError("Linux: implement systemctl --user stop")

    def restart(self) -> None:
        self.stop()
        self.start()

    def uninstall(self) -> None:
        raise NotImplementedError("Linux: implement systemctl --user disable/remove")

    def is_registered(self) -> bool:
        raise NotImplementedError("Linux: implement systemctl --user status")

    def platform_name(self) -> str:
        return "Linux systemd user unit"


class _MacOSPlatform(_PlatformImpl):
    def artifact_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / "backseat-daemon.plist"

    def register(self) -> None:
        raise NotImplementedError("macOS: implement launchctl bootstrap")

    def start(self) -> None:
        raise NotImplementedError("macOS: implement launchctl kickstart")

    def stop(self) -> None:
        raise NotImplementedError("macOS: implement launchctl bootout")

    def restart(self) -> None:
        self.stop()
        self.start()

    def uninstall(self) -> None:
        raise NotImplementedError("macOS: implement launchctl bootout + rm plist")

    def is_registered(self) -> bool:
        raise NotImplementedError("macOS: implement launchctl list")

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
