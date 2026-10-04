"""Homepage workspace service (step 4.1).

Wraps the seams from 2-plan.md §4 so routes/templates stay thin:

- :func:`list_workspaces` — ``db.list_workspaces`` in registration order.
- :func:`register_workspace` — ``backseat-harness register-workspace <path>``
  via ``subprocess.run`` (V30: web invokes the CLI; validation of
  ``.backseat/`` lives in ``cli._cmd_register_workspace``).
- :func:`suggest_workspaces_from_chat_search` — chat-search parent dirs of
  matched sessions, minus already-registered paths.
- :func:`display_name` — trailing dir name for V4
  (``<strong>name</strong><br><small>full path</small>``).

History access is lazy (``history._load``) so the web package imports
without chat-mother-forker installed; failures return ``[]`` rather than
raising, mirroring ``history._retrying`` (reads are never fatal).
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def display_name(path: str | Path) -> str:
    """Trailing directory name for the workspace card (V4 bold line)."""
    p = Path(path).expanduser()
    name = p.resolve().name if p.is_absolute() else p.name
    return name or str(path)


def _normalize(path: str | Path) -> str:
    """Canonical form matching ``db._normalize`` (absolute, resolved)."""
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return str(path)


def list_workspaces() -> list[str]:
    """Every registered workspace path, in registration order."""
    from backseat_harness import db

    with db.open_db() as conn:
        return db.list_workspaces(conn)


def register_workspace(path: str | Path, timeout_s: float = 30.0) -> tuple[bool, str]:
    """Register ``path`` via the ``register-workspace`` CLI (V5/V30).

    Returns ``(ok, message)`` where ``ok`` is True iff the subprocess
    exited 0. The CLI validates ``.backseat/`` and normalizes the path,
    so this layer reports its output verbatim.
    """
    target = str(path)
    try:
        proc = subprocess.run(
            ["backseat-harness", "register-workspace", target],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except FileNotFoundError:
        return False, "backseat-harness executable not found on PATH"
    except subprocess.TimeoutExpired:
        return False, f"register-workspace timed out after {timeout_s:g}s"
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if out:
        message = out
    elif err:
        message = err
    elif proc.returncode == 0:
        message = f"Registered workspace {target}"
    else:
        message = "register failed"
    if proc.returncode != 0 and err and out and err not in out:
        message = f"{out} ({err})"
    return proc.returncode == 0, message


def _candidate_dirs(refs: object, registered: set[str]) -> list[str]:
    """Pure helper: unique existing dirs from chat refs, minus registered."""
    seen: dict[str, None] = {}
    for ref in refs or []:  # type: ignore[union-attr]
        project = getattr(ref, "project", None)
        candidates: list[Path] = []
        if project:
            candidates.append(Path(project).expanduser())
        session = getattr(ref, "session", "") or ""
        key = getattr(ref, "key", "") or ""
        native = session or (key.partition(":")[2] if ":" in key else "")
        if native:
            parent = Path(native).expanduser().parent
            if str(parent) not in (".", ""):
                candidates.append(parent)
        for cand in candidates:
            try:
                if not cand.is_dir():
                    continue
                norm = _normalize(cand)
            except OSError:
                continue
            if norm not in registered and norm not in seen:
                seen[norm] = None
    return sorted(seen)


def suggest_workspaces_from_chat_search(limit: int = 100) -> list[str]:
    """Unregistered dirs that have chat sessions (V6 suggestions).

    Scans all chat-mother-forker providers with an empty search, collects
    unique parent dirs of ``ChatRef.project`` (falling back to the parent
    of a path-shaped session id), and filters out already-registered
    workspaces. Returns ``[]`` when history is unavailable.
    """
    try:
        from backseat_harness import history
    except ImportError:
        return []
    try:
        find_chats = history._load("find_chats")
        default_providers = history._load("default_providers")
        providers = default_providers()
    except Exception:
        return []
    try:
        refs = find_chats("", providers=providers, limit=limit)
    except Exception:
        return []
    try:
        from backseat_harness import db as _db

        with _db.open_db() as conn:
            registered = set(_db.list_workspaces(conn))
    except Exception:
        registered = set()
    try:
        return _candidate_dirs(refs, registered)
    except Exception:
        return []
