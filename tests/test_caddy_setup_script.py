"""The Windows caddy setup helper (scripts/setup-caddy-windows.ps1).

The elevated path changes the machine, so these tests only run it where it must
refuse: unelevated, with stub winget/caddy on PATH that record any call.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup-caddy-windows.ps1"
_WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="the setup helper is Windows-only"
)


def _is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _powershell() -> str:
    exe = shutil.which("powershell.exe") or shutil.which("powershell")
    assert exe, "Windows PowerShell is required for the setup helper test"
    return exe


@_WINDOWS_ONLY
def test_setup_script_parses_cleanly():
    """The script has no PowerShell parse errors; the check runs even when elevated."""
    command = (
        "$e = $null; $t = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{_SCRIPT}', [ref]$t, [ref]$e); "
        "$e | ForEach-Object { $_.Message }; exit $e.Count"
    )
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@_WINDOWS_ONLY
@pytest.mark.skipif(_is_elevated(), reason="the refusal path needs a non-elevated session")
def test_setup_script_refuses_unelevated_before_any_side_effect(tmp_path):
    """Unelevated, the script exits 2 with a message and touches nothing.

    Stub winget and caddy sit first on PATH and log every call; the home dir is
    a temp dir. A run that reached install, firewall or write would show up in
    the log or in the home dir.
    """
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for name in ("winget", "caddy"):
        (bin_dir / f"{name}.cmd").write_text(
            f'@echo off\r\necho {name} %* >> "{log}"\r\n', encoding="ascii"
        )

    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(bin_dir), env.get("PATH", "")])
    env["USERPROFILE"] = str(home)

    result = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(_SCRIPT),
        ],
        input="",
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "must run from an elevated" in result.stdout
    assert "nothing was installed, changed, or written" in result.stdout
    assert not log.exists(), "winget or caddy was called before the elevation check"
    # PowerShell itself may create profile folders such as AppData; the script must not.
    assert not (home / ".backseat").exists(), "the script wrote into ~/.backseat"
