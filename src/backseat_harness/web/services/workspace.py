"""Homepage workspace service (step 4.1).

Wraps the seams from 2-plan.md §4 so routes/templates stay thin:

- :func:`list_workspaces` — ``db.list_workspaces`` in registration order.
- :func:`register_workspace` — ``backseat-harness register-workspace <path>``
  via ``subprocess.run`` (V30: web invokes the CLI; validation of
  ``.backseat/`` lives in ``cli._cmd_register_workspace``).
- :func:`suggest_workspaces_from_chat_search` — absolute dirs recovered from
  the provider stores behind ``history.list_session_refs``' cheap (unparsed)
  refs, minus already-registered paths (parity with the ``chat_search`` tool;
  plan B).
- :func:`display_name` — trailing dir name for V4
  (``<strong>name</strong><br><small>full path</small>``).

History access is lazy (``history.list_session_refs`` reads through
``history._load``) so the web package imports without chat-mother-forker
installed; failures return ``[]`` rather than raising, mirroring
``history._retrying`` (reads are never fatal).
"""

from __future__ import annotations

import json
import re
import sqlite3
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


def list_workspaces_with_ids() -> list[tuple[int, str]]:
    """Every registered workspace as ``(id, path)``, in registration order.

    The id is the URL key the ``/ws/{id}`` route resolves, so the homepage
    can link each card to its workspace page.
    """
    from backseat_harness import db

    with db.open_db() as conn:
        return db.list_workspaces_with_ids(conn)


def initialize_workspace_marker(path: str | Path) -> bool:
    """Create a bare ``<path>/.backseat/`` marker for an existing plain dir.

    Plan Step C/F1: the ``+`` dropdown surfaces un-initialized chat dirs, but
    ``register-workspace`` refuses anything without ``.backseat/``. Adding such
    a dir therefore creates the marker first. Returns True iff this call
    created it; raises ``OSError`` when ``path`` is not an existing directory
    (the caller leaves that to the CLI to report). Deliberately narrower than
    the CLI's ``init`` (``cli._cmd_init``), which also scaffolds a task lane.
    """
    p = Path(path).expanduser()
    marker = p / ".backseat"
    if marker.is_dir():
        return False
    if not p.is_dir():
        raise NotADirectoryError(str(p))
    marker.mkdir()
    return True


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


#: Provider names whose sessions live in a shared SQLite DB; the absolute
#: workspace path is the ``session.directory`` column (opencode.py:148-153,
#: kilo.py:114-119).
_SQLITE_PROVIDERS = frozenset({"opencode", "kilo"})

#: Kiro IDE's legacy execution log embeds the workspace path only inside the
#: rendered ``<fileTree>`` blob (kiro_ide.py:56,134-161).
_FILE_TREE_ENTRY_RE = re.compile(r"<(?:folder|file) name='([^']+)'")


def _provider_of(key: str) -> str:
    """Exact provider name from a ``provider:conversation_id`` ChatRef key.

    ``split(":", 1)[0]`` keeps ``kiro_ide`` and ``kiro_ide_v2`` distinct (a
    ``startswith`` test would conflate them; plan F3).
    """
    return key.split(":", 1)[0]


#: ``{database: ((mtime, size), {session_id: directory})}`` -- one read of a
#: shared SQLite store per file version, not one query per session id.
_SESSION_DIR_CACHE: dict[str, tuple[tuple[float, int], dict[str, str]]] = {}


def _session_dirs(database: str) -> dict[str, str]:
    """``{session_id: directory}`` from a shared store, read once per version.

    A listing probes one directory per candidate row -- up to a hundred rows
    per store -- and querying them one id at a time re-opens and re-scans the
    table each time: 0.8s across opencode's store alone. One
    ``SELECT id, directory`` covers every row the page will ask about, and
    keying the result on the file's ``(mtime, size)`` rebuilds it after a
    write instead of serving a stale path.
    """
    path = Path(database)
    try:
        st = path.stat()
    except OSError:
        return {}
    stamp = (st.st_mtime, st.st_size)
    cached = _SESSION_DIR_CACHE.get(database)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    mapping: dict[str, str] = {}
    try:
        conn = sqlite3.connect(
            f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=5.0
        )
        try:
            rows = conn.execute("SELECT id, directory FROM session").fetchall()
        finally:
            conn.close()
        mapping = {
            session_id: directory
            for session_id, directory in ((row[0], row[1]) for row in rows)
            if isinstance(session_id, str)
            and isinstance(directory, str)
            and directory
        }
    except (sqlite3.Error, OSError, ValueError, TypeError):
        mapping = {}
    _SESSION_DIR_CACHE[database] = (stamp, mapping)
    return mapping


def _dir_from_session_db(database: str, session: str | None) -> str | None:
    """``session.directory`` for an opencode/kilo session id, read-only."""
    if not session:
        return None
    return _session_dirs(database).get(session)


def _dir_from_claude_jsonl(locator: str) -> str | None:
    """First ``cwd`` in a Claude Code ``.jsonl`` (claude_code.py:132-145)."""
    with open(locator, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            cwd = event.get("cwd")
            if isinstance(cwd, str) and cwd:
                return cwd
    return None


def _dir_from_command_code_jsonl(locator: str) -> str | None:
    """``cwd`` from a Command Code session's header, the first line of ``<uuid>.jsonl``.

    Only that line is read: the header is the one record that names the
    workspace, and message bodies are never scanned. A first record that is not
    a ``session`` header ends the search with ``None``.
    """
    with open(locator, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                return None
            if not isinstance(event, dict) or event.get("type") != "session":
                return None
            cwd = event.get("cwd")
            return cwd if isinstance(cwd, str) and cwd else None
    return None


def _dir_from_kiro_cli(locator: str) -> str | None:
    """``cwd`` from the Kiro CLI sidecar ``<uuid>.json`` (kiro_cli.py:119-132)."""
    meta_path = Path(locator).with_suffix(".json")
    meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
    cwd = meta.get("cwd")
    return cwd if isinstance(cwd, str) and cwd else None


def _dir_from_cline(locator: str) -> str | None:
    """``workspace_root``/``cwd`` from a Cline ``<id>.json`` (cline.py:98-104)."""
    session_dir = Path(locator)
    meta_path = session_dir / f"{session_dir.name}.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
    cwd = meta.get("workspace_root") or meta.get("cwd")
    return cwd if isinstance(cwd, str) and cwd else None


def _dir_from_kiro_v2(locator: str) -> str | None:
    """``workspacePaths[0]`` from Kiro IDE v2 ``session.json`` (kiro_ide_v2.py:135-151)."""
    meta_path = Path(locator) / "session.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
    paths = meta.get("workspacePaths")
    if isinstance(paths, list) and paths and isinstance(paths[0], str) and paths[0]:
        return paths[0]
    return None


def _dir_from_kiro_legacy(locator: str) -> str | None:
    """Workspace dir scraped from a Kiro IDE execution log (kiro_ide.py:134-161).

    Best-effort and a known parity gap: a ``running`` session whose context is
    empty has no ``<fileTree>`` here (the provider would stitch a previous
    terminal execution; this helper does not), so such a ref is skipped.
    """
    data = json.loads(Path(locator).read_text(encoding="utf-8", errors="replace"))
    for ctx_msg in data.get("context", {}).get("messages", []):
        for entry in ctx_msg.get("entries", []):
            if not isinstance(entry, dict) or entry.get("type") != "document":
                continue
            document = entry.get("document")
            if not isinstance(document, dict):
                continue
            sdv = document.get("staticDirectoryView", "")
            if not isinstance(sdv, str):
                continue
            match = _FILE_TREE_ENTRY_RE.search(sdv)
            if not match:
                continue
            raw = match.group(1)
            parts = re.split(r"[\\/]+", raw)
            if len(parts) >= 2:
                sep = "\\" if "\\" in raw else "/"
                return sep.join(parts[:-1])
    return None


def _workspace_dir_for(ref: object) -> str | None:
    """Absolute workspace dir for a chat ref, or ``None`` when unrecoverable.

    chat-mother-forker exposes only a directory *basename* in
    ``ChatRef.project`` (models.py:91-94), so the absolute path is recovered
    from the provider's own store using the ``database``/``session``/``locator``
    fields ``find_chats`` sets on each ref (api.py:160-171). Never guesses:
    any missing/unreadable field returns ``None`` and the candidate is dropped.
    """
    provider = _provider_of(getattr(ref, "key", "") or "")
    locator = getattr(ref, "locator", None)
    database = getattr(ref, "database", None)
    session = getattr(ref, "session", None)
    try:
        if database:
            if provider in _SQLITE_PROVIDERS:
                return _dir_from_session_db(database, session)
            return None
        if not locator:
            return None
        recover = {
            "claude_code": _dir_from_claude_jsonl,
            "command_code": _dir_from_command_code_jsonl,
            "kiro_cli": _dir_from_kiro_cli,
            "cline": _dir_from_cline,
            "kiro_ide_v2": _dir_from_kiro_v2,
            "kiro_ide": _dir_from_kiro_legacy,
        }.get(provider)
        if recover is None:
            return None
        return recover(locator)
    except (OSError, sqlite3.Error, json.JSONDecodeError, ValueError, TypeError):
        return None


def _project_fallback(ref: object) -> str | None:
    """Last-resort: accept ``project`` only when it is already absolute.

    Back-compat for refs/tests that predate store recovery; real providers put
    a bare basename here, which is exactly the old bug (plan D2).
    """
    project = getattr(ref, "project", None)
    if not project:
        return None
    p = Path(project).expanduser()
    return str(p) if p.is_absolute() else None


def _candidate_dirs(refs: object, registered: set[str]) -> list[str]:
    """Unique existing dirs recovered from chat refs, minus registered.

    Each ref's absolute dir comes from :func:`_workspace_dir_for`; refs whose
    dir cannot be recovered fall back to ``project``, which for a
    ``history.SessionRef`` means one parse of that one session (its
    ``project`` resolves on first access) -- so the parse is paid only for the
    refs whose store recovery already came up empty. Per plan D1 there is
    **no** ``.backseat/`` filter — every existing chat dir is a potential
    workspace, matching ``chat_search``'s result set.
    """
    seen: dict[str, None] = {}
    for ref in refs or []:  # type: ignore[union-attr]
        raw = _workspace_dir_for(ref) or _project_fallback(ref)
        if raw is None:
            continue
        try:
            cand = Path(raw).expanduser()
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

    Scans all chat-mother-forker providers with the cheap listing (one
    unparsed ``list_candidates`` per provider -- no message bodies: opening
    the dropdown is a directory question, not a transcript one), recovers each
    ref's absolute workspace dir from its provider store (plan D2), and
    filters out already-registered workspaces. No ``.backseat/`` filter
    (plan D1): every existing chat dir is a potential workspace, comparable to
    ``chat_search``. Returns ``[]`` when history is unavailable.
    """
    try:
        from backseat_harness import history
    except ImportError:
        return []
    try:
        refs = history.list_session_refs(limit)
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
