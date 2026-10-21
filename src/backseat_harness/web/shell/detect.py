"""Plain-shell detection for the workspace "New chat" panel's shell button.

Every lookup goes through ``shutil.which`` so tests monkeypatch one seam. The
argv is bare (the name as found, or the ``$SHELL`` value); the route still
runs it through ``resolve_argv``, so PATH resolution stays in one place.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def _windows_shell() -> tuple[str, list[str]] | None:
    """``pwsh`` when installed, else Windows PowerShell; None when neither resolves."""
    for name in ("pwsh", "powershell"):
        if shutil.which(name) is not None:
            return name, [name]
    return None


def _posix_shell() -> tuple[str, list[str]] | None:
    """``$SHELL`` (labelled by its basename), else ``bash``, else ``sh``."""
    shell = os.environ.get("SHELL", "")
    if shell and shutil.which(shell) is not None:
        return Path(shell).name, [shell]
    for name in ("bash", "sh"):
        if shutil.which(name) is not None:
            return name, [name]
    return None


def default_shell() -> tuple[str, list[str]] | None:
    """``(label, argv)`` for the platform's plain shell, or None when none resolves.

    The label is the text the button shows; no button is rendered on None.
    """
    return _windows_shell() if os.name == "nt" else _posix_shell()
