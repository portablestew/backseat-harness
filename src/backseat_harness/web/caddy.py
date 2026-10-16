"""Caddy web mode: where the caddyfile lives, how ``--web`` resolves, which
Host/Origin strings its site addresses produce, and the caddy child process
(find, validate, probe the site ports, start, stop, report an unexpected exit).

The daemon never imports this module (``tests/test_daemon.py`` pins that), so
the caller in ``cli.py`` owns the lazy import.
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from backseat_harness import config as config_mod
from backseat_harness.spawn import kill_pid_tree

#: Modes the resolver understands. ``none`` is decided by the CLI before this
#: module is reached, so it is not accepted here.
MODES = ("auto", "direct", "caddy")

_HOST_RE = re.compile(
    r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)
_PORT_RE = re.compile(r"[0-9]{1,5}")


class CaddyRefusal(ValueError):
    """A caddy-mode precondition failed; the caller exits 2 with this message."""


def caddyfile_path() -> Path:
    """``~/.backseat/caddyfile``, resolved through the config home seam (tests pin it)."""
    return config_mod._default_home() / ".backseat" / "caddyfile"


def resolve_mode(requested: str, path: Path) -> str:
    """Turn a requested ``--web`` / ``--mode`` value into ``"direct"`` or ``"caddy"``.

    ``auto`` picks caddy exactly when the caddyfile exists. An explicit
    ``caddy`` with no caddyfile is refused rather than silently served without
    auth.
    """
    if requested not in MODES:
        raise ValueError(f"unknown web mode {requested!r}; expected one of {', '.join(MODES)}")
    if requested == "direct":
        return "direct"
    present = path.exists()
    if requested == "caddy":
        if not present:
            raise CaddyRefusal(f"web mode caddy needs a caddyfile at {path}")
        return "caddy"
    return "caddy" if present else "direct"


def _strip_comments(text: str) -> str:
    """Drop ``#`` comments. Caddy only starts one at the beginning of a word."""
    return "\n".join(re.sub(r"(^|\s)#.*$", "", line) for line in text.splitlines())


def _block_headers(text: str) -> list[str]:
    """The text before each top-level ``{``, in order. Block bodies are skipped."""
    headers: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in _strip_comments(text):
        if depth == 0:
            if ch == "{":
                headers.append("".join(buf).strip())
                buf = []
                depth = 1
            elif ch == "}":
                raise CaddyRefusal("caddyfile has a '}' with no matching '{'")
            else:
                buf.append(ch)
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
    if depth != 0:
        raise CaddyRefusal("caddyfile has a '{' that is never closed")
    if "".join(buf).strip():
        raise CaddyRefusal("caddyfile has text outside a block; site addresses need '{ }'")
    return headers


def _port(text: str, token: str) -> int:
    if not _PORT_RE.fullmatch(text) or not 1 <= int(text) <= 65535:
        raise CaddyRefusal(f"site address {token!r} has no valid port (1-65535)")
    return int(text)


def _address(token: str) -> tuple[set[str], set[int]]:
    """Host/Origin strings and probe ports for one site address."""
    addr = token.lower()
    scheme = None
    if "://" in addr:
        scheme, _, addr = addr.partition("://")
        if scheme not in ("http", "https"):
            raise CaddyRefusal(f"site address {token!r} has an unsupported scheme")
    if "*" in addr:
        raise CaddyRefusal(f"site address {token!r} is a wildcard; list the host explicitly")
    if "/" in addr:
        raise CaddyRefusal(f"site address {token!r} has a path; only host and port are allowed")
    if addr.startswith("["):
        raise CaddyRefusal(f"site address {token!r} is an IPv6 literal, which is not supported")

    port: int | None = None
    host = addr
    if ":" in addr:
        host, _, port_text = addr.rpartition(":")
        port = _port(port_text, token)

    if not host:
        # ``:9443`` answers on any local name; browsers can reach it as either.
        assert port is not None  # ":" was present, so a port was parsed
        return {f"localhost:{port}", f"127.0.0.1:{port}"}, {port}
    if not _HOST_RE.fullmatch(host):
        raise CaddyRefusal(f"site address {token!r} is not a valid host name")
    if port is not None:
        return {f"{host}:{port}"}, {port}
    # Browsers leave the default port out of Host and Origin, so both forms are allowed.
    default = 80 if scheme == "http" else 443
    return {host, f"{host}:{default}"}, {default}


def site_hosts(text: str) -> tuple[set[str], set[int]]:
    """Allowlist strings and ports for every site address in a caddyfile.

    Global options blocks and ``(snippet)`` blocks are skipped; their bodies
    are never read. Addresses are lowercased, because the middleware lowercases
    the incoming Host and Origin (``web/security.py``). Raises
    :class:`CaddyRefusal` for anything it does not handle, so an unsupported
    form fails loudly instead of quietly dropping a site.
    """
    hosts: set[str] = set()
    ports: set[int] = set()
    for header in _block_headers(text):
        if not header or header.startswith("("):
            continue
        for token in re.split(r"[\s,]+", header):
            if token:
                token_hosts, token_ports = _address(token)
                hosts |= token_hosts
                ports |= token_ports
    if not hosts:
        raise CaddyRefusal("caddyfile has no site address")
    return hosts, ports


# --- the caddy child -----------------------------------------------------------

#: How long ``CaddyHandle.stop`` waits for a graceful exit before the tree-kill.
_STOP_GRACE_S = 5.0

#: How often the watch thread polls the child for an unexpected exit.
_POLL_S = 1.0

#: Ports never probed: 80 is the HTTP redirect listener (a busy 80 surfaces as
#: caddy exiting at startup), and 2019 is caddy's admin API.
_UNPROBED_PORTS = frozenset({80, 2019})


def find_caddy() -> str:
    """Absolute path of the ``caddy`` executable on PATH, or :class:`CaddyRefusal`."""
    found = shutil.which("caddy")
    if found is None:
        raise CaddyRefusal(
            "caddy is not on PATH; install it from https://caddyserver.com/docs/install"
        )
    return found


def validate(exe: str, path: Path) -> None:
    """Run ``caddy validate`` on the caddyfile; a non-zero exit is a refusal."""
    try:
        proc = subprocess.run(
            [exe, "validate", "--config", str(path), "--adapter", "caddyfile"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_STOP_GRACE_S * 6,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CaddyRefusal(f"caddy validate did not finish for {path}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise CaddyRefusal(f"caddy validate rejected {path}: {detail}")


def probe_ports(ports: Iterable[int]) -> None:
    """Refuse when a site port is already bound; the caller has not claimed the guard yet.

    Each port is bound on three addresses, because a listener on one of them
    hides from a bind on another (checked on Windows; see the commit that added
    this). ``0.0.0.0`` catches a wildcard listener, ``127.0.0.1`` a loopback
    one, and dual-stack ``::`` the ``[::]`` listener that Go opens for ``:port``.
    ``SO_REUSEADDR`` is deliberately not set: on Windows it lets a bind succeed
    on a port that is in use. A ``PermissionError`` (a privileged port on POSIX,
    a reserved range on Windows) means that probe cannot tell, so it is skipped.
    """
    probes = (
        (socket.AF_INET, "", 1),
        (socket.AF_INET, "127.0.0.1", 1),
        (socket.AF_INET6, "::", 0),
    )
    for port in sorted(set(ports) - _UNPROBED_PORTS):
        for family, host, v6only in probes:
            try:
                probe = socket.socket(family, socket.SOCK_STREAM)
            except OSError:
                continue  # no such family on this machine (IPv6 disabled)
            with probe:
                if family == socket.AF_INET6:
                    probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, v6only)
                try:
                    probe.bind((host, port))
                except PermissionError:
                    break
                except OSError as exc:
                    raise CaddyRefusal(f"site port {port} is already in use ({exc})") from exc


# --- Windows job object: caddy dies with this process ---------------------------
#
# A graceful stop cannot cover a hard kill of the harness (``taskkill /F`` or a
# crash runs no Python cleanup), so on Windows the caddy child is put in a job
# with KILL_ON_JOB_CLOSE. The OS closes this process's handles on exit by any
# means, and closing the last handle kills every member. The job handle is not
# inheritable and Popen closes inherited handles, so caddy cannot hold it open.

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _BasicLimitInfo(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedLimitInfo(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInfo),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32() -> ctypes.CDLL:
    """The kernel32 entry points the job object needs, typed so handles stay 64-bit."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle, uint, int_ = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
    signatures = {
        "CreateJobObjectW": (handle, [ctypes.c_void_p, ctypes.c_void_p]),
        "SetInformationJobObject": (int_, [handle, int_, ctypes.c_void_p, uint]),
        "OpenProcess": (handle, [uint, int_, uint]),
        "AssignProcessToJobObject": (int_, [handle, handle]),
        "CloseHandle": (int_, [handle]),
    }
    for name, (restype, argtypes) in signatures.items():
        fn = getattr(k32, name)
        fn.restype, fn.argtypes = restype, argtypes
    return k32


class _KillOnCloseJob:
    """A job object whose members die when its handle closes. Windows only."""

    def __init__(self) -> None:
        self._k32 = _kernel32()
        self._job: int | None = self._k32.CreateJobObjectW(None, None)
        if not self._job:
            raise CaddyRefusal(f"caddy could not get a job object ({ctypes.get_last_error()})")
        info = _ExtendedLimitInfo()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        size = ctypes.sizeof(info)
        if not self._k32.SetInformationJobObject(
            self._job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), size
        ):
            err = ctypes.get_last_error()
            self.close()
            raise CaddyRefusal(f"caddy job object could not be configured ({err})")

    def attach(self, pid: int) -> None:
        """Put ``pid`` in the job. Raises ``OSError`` when the process cannot be opened or added."""
        assert self._job is not None
        proc = self._k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, 0, pid)
        if not proc:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self._k32.AssignProcessToJobObject(self._job, proc):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self._k32.CloseHandle(proc)

    def close(self) -> None:
        """Close the handle. Any member still running is killed by the OS."""
        if self._job:
            self._k32.CloseHandle(self._job)
            self._job = None


def _send_graceful(proc: subprocess.Popen) -> None:
    """Ask caddy to exit cleanly: CTRL_BREAK to its own group on Windows, SIGTERM elsewhere."""
    try:
        if os.name == "nt":
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGTERM)
    except OSError:
        pass  # already gone


def _watch(
    proc: subprocess.Popen, on_exit: Callable[[int], None], stopping: threading.Event
) -> None:
    """Report an exit the caller did not ask for; a deliberate stop reports nothing."""
    while not stopping.wait(_POLL_S):
        rc = proc.poll()
        if rc is not None:
            if not stopping.is_set():
                on_exit(rc)
            return


class CaddyHandle:
    """A running caddy child plus its watch thread; ``stop`` ends both.

    Idempotent, like ``WebHandle.stop``, so the normal shutdown and the error
    paths can both call it.
    """

    def __init__(
        self,
        proc: subprocess.Popen,
        watcher: threading.Thread,
        stopping: threading.Event,
        job: _KillOnCloseJob | None,
    ) -> None:
        self._proc: subprocess.Popen | None = proc
        self._watcher = watcher
        self._stopping = stopping
        self._job = job

    @property
    def pid(self) -> int:
        return self._proc.pid if self._proc is not None else 0

    def stop(self, timeout: float = _STOP_GRACE_S) -> None:
        """Graceful signal, wait up to ``timeout``, then tree-kill whatever is left."""
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        self._stopping.set()
        if proc.poll() is None:
            _send_graceful(proc)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_pid_tree(proc.pid)
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    pass
        self._watcher.join(timeout=_POLL_S + 1.0)
        if self._job is not None:
            self._job.close()


def start(
    exe: str,
    path: Path,
    *,
    log_path: Path | None,
    on_exit: Callable[[int], None],
) -> CaddyHandle:
    """Start ``caddy run`` on the caddyfile in its own process group.

    On Windows the child is also tied to a kill-on-close job object, so it dies
    with this process even when the process is killed without cleanup.

    Output is appended to ``log_path`` (the daemon's case) or inherited when it
    is ``None`` (standalone ``web``). ``on_exit(rc)`` runs once, from the watch
    thread, when caddy exits without ``stop`` having been called. Raises
    ``CaddyRefusal`` when the child cannot be spawned.
    """
    # Own group, so Ctrl-C reaches the daemon first and stop() can address caddy's
    # group alone (same split as spawn.py:202-212).
    group_kwargs = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    argv = [exe, "run", "--config", str(path), "--adapter", "caddyfile"]
    job = _KillOnCloseJob() if os.name == "nt" else None
    try:
        if log_path is None:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, **group_kwargs)
        else:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("ab") as log:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    **group_kwargs,
                )
    except OSError as exc:
        if job is not None:
            job.close()
        # Same refusal type as the pre-run checks, so the caller reports it the same way.
        raise CaddyRefusal(f"caddy could not start: {exc}") from exc
    if job is not None:
        try:
            job.attach(proc.pid)
        except OSError as exc:
            # A child that has already exited needs no confining; the watch thread reports it.
            if proc.poll() is None:
                proc.kill()
                job.close()
                raise CaddyRefusal(f"caddy could not be tied to this process: {exc}") from exc
    stopping = threading.Event()
    watcher = threading.Thread(
        target=_watch,
        args=(proc, on_exit, stopping),
        name="backseat-caddy-watch",
        daemon=True,
    )
    watcher.start()
    return CaddyHandle(proc, watcher, stopping, job)
