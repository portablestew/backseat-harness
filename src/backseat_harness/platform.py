"""Platform abstraction: artifact, scheduler, and PATH freeze per OS."""

from __future__ import annotations

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


class _WindowsPlatform(_PlatformImpl):
    def artifact_path(self) -> Path:
        return Path.home() / ".backseat" / "daemon" / "backseat-daemon.xml"

    def register(self) -> None:
        raise NotImplementedError("Windows: implement schtasks /create /XML")

    def start(self) -> None:
        raise NotImplementedError("Windows: implement schtasks /run")

    def stop(self) -> None:
        raise NotImplementedError("Windows: implement schtasks /end")

    def restart(self) -> None:
        self.stop()
        self.start()

    def uninstall(self) -> None:
        raise NotImplementedError("Windows: implement schtasks /delete")

    def is_registered(self) -> bool:
        raise NotImplementedError("Windows: implement schtasks /query")

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
