"""Interactive ``setup-caddy`` command: install caddy, open the site port, and
write ``~/.backseat/caddyfile`` with a bcrypt-hashed login.

This is the Python port of the old ``scripts/setup-caddy-windows.ps1`` (now
deleted). Living in the package instead of the repo is what lets it ship inside
the wheel and run under ``uvx backseat-harness setup-caddy``, where a repo-relative
script is not available.

Windows is the supported path: it replaces the PowerShell cmdlets with ``winget``
and ``netsh`` so no PowerShell host is needed, and it keeps the old refusal to run
unelevated. The Linux branch is deliberately *speculative and untested*: it
prefers whichever of apt/dnf/pacman is present, and ufw/firewalld for the port
rule, and reports rather than hides a step it could not perform.

The interactive order is host, then web port, then username, then password: host
and port sit together because both are addressing answers, and username and
password sit together because they are the pair that becomes one caddyfile line.
"""

from __future__ import annotations

import ctypes
import getpass
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from backseat_harness.web import caddy as caddy_mod

#: The HTTPS site port. Fixed, like the old script: the caddyfile lists
#: ``<host>:8443`` and the firewall rule opens this port. The web app itself
#: stays on ``[web] port`` (default 8090), which is what ``reverse_proxy`` targets.
SITE_PORT = 8443
_DEFAULT_SITE_HOST = "localhost"
_DEFAULT_USERNAME = "alice"
_DEFAULT_WEB_PORT = 8090

#: Firewall rule display name; hyphens so ``netsh`` needs no quoting of spaces.
_WINDOWS_RULE = f"backseat-harness-caddy-{SITE_PORT}"

#: Same allowlist as the old script: a site host is a DNS name, not a URL.
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")
_USER_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_BCRYPT_RE = re.compile(r"^\$2[abxy]\$")


class SetupRefusal(RuntimeError):
    """A setup precondition failed; the CLI exits 2 with this message."""


# --- the interactive surface ---------------------------------------------------


class Prompter(Protocol):
    """The prompts the flow needs, so tests run without a terminal."""

    def line(self, prompt: str, default: str) -> str: ...

    def secret(self, prompt: str) -> str: ...

    def confirm(self, prompt: str, expect: str) -> bool: ...


class ConsolePrompter:
    """The real terminal: visible lines, masked secrets, a typed confirmation."""

    def line(self, prompt: str, default: str) -> str:
        answer = input(f"{prompt} [{default}]: ").strip()
        return answer or default

    def secret(self, prompt: str) -> str:
        """Read a secret, echoing ``*`` per character where the interpreter allows it.

        ``getpass`` only grew ``echo_char`` in Python 3.14; on 3.10-3.13 the call
        raises ``TypeError`` while binding arguments, before any prompt is shown,
        so the fallback is the plain hidden read. The asterisks make a paste
        visibly register, the way the old ``Read-Host -AsSecureString`` did.
        """
        try:
            return getpass.getpass(f"{prompt}: ", echo_char="*")
        except TypeError:
            return getpass.getpass(f"{prompt}: ")

    def confirm(self, prompt: str, expect: str) -> bool:
        return input(f"{prompt}: ") == expect


# --- small pure helpers --------------------------------------------------------


def _is_elevated(platform: str) -> bool:
    """Whether this process can change the machine (Windows admin / Linux root)."""
    if platform == "win32":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def _is_private_ipv4(address: str) -> bool:
    """RFC 1918 check, the same set the old script allowed (10/8, 172.16/12, 192.168/16)."""
    try:
        ip = ipaddress.IPv4Address(address)
    except ipaddress.AddressValueError:
        return False
    return ip.is_private


def find_private_ipv4() -> str | None:
    """The machine's private IPv4 on the default route, else any private one, else None.

    Opens a UDP socket and asks the OS to pick the outbound interface; no packet
    is sent. Falls back to resolving the hostname, matching what the old script
    reached for with ``Get-NetRoute``/``Get-NetIPAddress``.
    """
    candidates: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            candidates.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        candidates.append(socket.gethostbyname(socket.gethostname()))
    except OSError:
        pass
    for address in candidates:
        if _is_private_ipv4(address):
            return address
    return None


def _default_run(argv: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command captured and non-interactive; the default ``run`` seam."""
    return subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        **kwargs,
    )


def _require(condition: object, message: str) -> None:
    if not condition:
        raise SetupRefusal(message)


def _parse_port(raw: str) -> int:
    _require(re.fullmatch(r"[0-9]{1,5}", raw), f"web port {raw!r} is not a port number")
    port = int(raw)
    _require(1 <= port <= 65535, f"web port {raw!r} is out of range (1-65535)")
    return port


# --- answers -------------------------------------------------------------------


@dataclass(frozen=True)
class Answers:
    """The four things the owner supplies, in prompt order."""

    site_host: str
    web_port: int
    username: str
    password: str


@dataclass(frozen=True)
class SetupResult:
    """What setup produced, for the CLI payload and the closing instructions."""

    caddyfile: Path
    site_hosts: tuple[str, ...]
    username: str
    caddy_installed: bool
    firewall_updated: bool

    @property
    def url(self) -> str:
        return " or ".join(f"https://{name}:{SITE_PORT}" for name in self.site_hosts)


def collect_answers(
    prompter: Prompter,
    *,
    default_host: str = _DEFAULT_SITE_HOST,
    default_port: int = _DEFAULT_WEB_PORT,
    site_host: str | None = None,
    web_port: int | None = None,
    username: str | None = None,
) -> Answers:
    """Gather host, web port, username and password, in that order.

    Host and port are addressed first, then the credential pair. A value passed
    as an override skips its prompt but still validates, and the password is
    never an override -- it is typed twice and hidden.
    """
    host = site_host or prompter.line(
        "Site host name (must resolve to this machine from the browser)", default_host
    )
    _require(
        _HOST_RE.match(host),
        f"site host {host!r} may contain only letters, digits, dots and hyphens",
    )

    if web_port is not None:
        port = _parse_port(str(web_port))
    else:
        port = _parse_port(
            prompter.line(
                "Web app port that reverse_proxy targets (matches [web] port)",
                str(default_port),
            )
        )

    user = username or prompter.line("Basic-auth username", _DEFAULT_USERNAME)
    _require(
        _USER_RE.match(user),
        f"username {user!r} may contain only letters, digits, dots, underscores and hyphens",
    )

    plain = prompter.secret("Password")
    repeat = prompter.secret("Repeat password")
    _require(len(plain) > 0, "the password is empty; nothing was written")
    _require(plain == repeat, "the two passwords do not match; nothing was written")
    return Answers(site_host=host, web_port=port, username=user, password=plain)


# --- machine changes -----------------------------------------------------------


def _refresh_windows_path() -> None:
    """Re-read Machine+User PATH after winget, which updates the registry only."""
    if sys.platform != "win32":
        return
    import winreg

    parts: list[str] = []
    locations = (
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    )
    for root, key_path in locations:
        try:
            with winreg.OpenKey(root, key_path) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        if value:
            parts.append(value)
    if parts:
        os.environ["PATH"] = os.pathsep.join(parts)


_LINUX_INSTALLERS: tuple[tuple[str, list[str]], ...] = (
    ("apt-get", ["install", "-y", "caddy"]),
    ("dnf", ["install", "-y", "caddy"]),
    ("pacman", ["-S", "--noconfirm", "caddy"]),
)


def _install_caddy_linux(
    which: Callable[[str], str | None],
    run: Callable[..., subprocess.CompletedProcess],
    emit: Callable[[str], None],
) -> None:
    """Best-effort install through a distro package manager. Speculative/untested.

    Uses ``sudo`` unless already root. apt/dnf/pacman are tried in order; when
    none is present the refusal points at the download page rather than guessing.
    """
    elevated = _is_elevated("linux")
    for manager, args in _LINUX_INSTALLERS:
        if which(manager):
            argv = ([manager] if elevated else ["sudo", manager]) + args
            emit(f"caddy not found on PATH; installing with {manager} (experimental Linux path)...")
            run(argv)
            return
    raise SetupRefusal(
        "caddy is not on PATH and no known package manager (apt-get, dnf, pacman) "
        "was found. Install caddy from https://caddyserver.com/docs/install, then run this again."
    )


def _ensure_caddy(
    platform: str,
    *,
    which: Callable[[str], str | None],
    run: Callable[..., subprocess.CompletedProcess],
    emit: Callable[[str], None],
) -> tuple[str, bool]:
    """Path of ``caddy``, installing it first if needed; ``(exe, was_installed)``."""
    found = which("caddy")
    if found:
        emit(f"using caddy at {found}")
        return found, False
    if platform == "win32":
        if which("winget") is None:
            raise SetupRefusal(
                "caddy is not on PATH and winget is not available. Install caddy from "
                "https://caddyserver.com/docs/install, then run this again."
            )
        emit("caddy not found on PATH; installing CaddyServer.Caddy with winget...")
        run(["winget", "install", "--id", "CaddyServer.Caddy", "-e",
             "--accept-package-agreements", "--accept-source-agreements"])
        _refresh_windows_path()
    else:
        _install_caddy_linux(which, run, emit)
    found = which("caddy")
    _require(
        found,
        "caddy is still not on PATH. Open a new shell so PATH is refreshed, then run this again.",
    )
    emit(f"using caddy at {found}")
    return found, True


def _firewall_windows(
    port: int, run: Callable[..., subprocess.CompletedProcess], emit: Callable[[str], None]
) -> bool:
    probe = run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={_WINDOWS_RULE}"])
    if probe.returncode == 0 and _WINDOWS_RULE in (probe.stdout or ""):
        emit("firewall rule already present; leaving it as it is.")
        return True
    run([
        "netsh", "advfirewall", "firewall", "add", "rule",
        f"name={_WINDOWS_RULE}", "dir=in", "action=allow", "protocol=TCP", f"localport={port}",
    ])
    emit("firewall rule added.")
    return True


def _firewall_linux(
    port: int,
    *,
    which: Callable[[str], str | None],
    run: Callable[..., subprocess.CompletedProcess],
    emit: Callable[[str], None],
) -> bool:
    """ufw or firewalld when present; otherwise report and continue (no rule)."""
    elevated = _is_elevated("linux")

    def sudo(argv: list[str]) -> list[str]:
        return argv if elevated else ["sudo", *argv]

    if which("ufw"):
        run(sudo(["ufw", "allow", f"{port}/tcp"]))
        emit(f"ufw rule added for TCP {port}.")
        return True
    if which("firewall-cmd"):
        run(sudo(["firewall-cmd", "--permanent", f"--add-port={port}/tcp"]))
        run(sudo(["firewall-cmd", "--reload"]))
        emit(f"firewalld rule added for TCP {port}.")
        return True
    emit(f"no ufw or firewalld found; open TCP {port} in your firewall yourself if needed.")
    return False


def _ensure_firewall(
    platform: str,
    port: int,
    *,
    which: Callable[[str], str | None],
    run: Callable[..., subprocess.CompletedProcess],
    emit: Callable[[str], None],
) -> bool:
    if platform == "win32":
        return _firewall_windows(port, run, emit)
    return _firewall_linux(port, which=which, run=run, emit=emit)


def hash_password(
    exe: str, plaintext: str, run: Callable[..., subprocess.CompletedProcess]
) -> str:
    """The bcrypt hash from ``caddy hash-password``; refuses anything that is not one.

    The plaintext is a single argv element, so (unlike the old PowerShell 5.1
    native-argument path) a quote or trailing backslash needs no special ban.
    """
    proc = run([exe, "hash-password", "--plaintext", plaintext])
    detail = (proc.stderr or proc.stdout or "").strip()
    _require(proc.returncode == 0, f"caddy hash-password exited {proc.returncode}: {detail}")
    lines = (proc.stdout or "").strip().splitlines()
    password_hash = lines[-1].strip() if lines else ""
    _require(
        _BCRYPT_RE.match(password_hash),
        "caddy hash-password did not return a bcrypt hash; nothing was written",
    )
    return password_hash


# --- the caddyfile -------------------------------------------------------------


def site_names(site_host: str, lan_ip: str | None) -> tuple[str, ...]:
    """The names the site answers under, lowercased and de-duplicated.

    The host, both loopback spellings, and the machine's private address when one
    was found -- the same set the old script wrote, minus duplicates (caddy rejects
    the same site address twice).
    """
    names = [site_host.lower(), "localhost", "127.0.0.1"]
    if lan_ip:
        names.append(lan_ip.lower())
    return tuple(dict.fromkeys(names))


def render_caddyfile(
    names: Sequence[str], web_port: int, username: str, password_hash: str
) -> str:
    """The caddyfile text: internal TLS, a bcrypt login, and a loopback proxy."""
    site_line = ", ".join(f"{name}:{SITE_PORT}" for name in names)
    return (
        "{\n"
        "\tskip_install_trust\n"
        "\tauto_https disable_redirects\n"
        "}\n"
        f"{site_line} {{\n"
        "\ttls internal\n"
        "\tbasic_auth {\n"
        f"\t\t{username} {password_hash}\n"
        "\t}\n"
        f"\treverse_proxy 127.0.0.1:{web_port}\n"
        "}\n"
    )


def write_caddyfile(
    path: Path,
    content: str,
    *,
    exe: str,
    prompter: Prompter,
    validate: Callable[[str, Path], None],
    emit: Callable[[str], None],
) -> None:
    """Validate a staged write, then replace the caddyfile, keeping a backup.

    A refused validation removes the staged file and leaves any existing
    caddyfile untouched, so a bad edit never costs a working one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(path.name + ".new")
    if path.exists():
        emit(f"A caddyfile already exists at {path}:")
        for line in path.read_text(encoding="utf-8").splitlines():
            emit(f"    {line}")
        emit("Continuing moves it to caddyfile.bak-<timestamp> and writes a new one.")
        _require(
            prompter.confirm("Type REPLACE to continue, anything else to stop", "REPLACE"),
            "not replacing the existing caddyfile; nothing was changed",
        )
    staged.write_text(content, encoding="utf-8", newline="\n")
    try:
        validate(exe, staged)
    except caddy_mod.CaddyRefusal as exc:
        staged.unlink(missing_ok=True)
        raise SetupRefusal(f"{exc}; the existing caddyfile (if any) was left alone") from exc
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        path.replace(backup)
        emit(f"previous caddyfile moved to {backup}")
    staged.replace(path)
    emit(f"caddyfile written: {path}")


# --- the flow ------------------------------------------------------------------


def run_setup(
    *,
    prompter: Prompter,
    platform: str | None = None,
    run: Callable[..., subprocess.CompletedProcess] = _default_run,
    which: Callable[[str], str | None] = shutil.which,
    find_ip: Callable[[], str | None] = find_private_ipv4,
    validate: Callable[[str, Path], None] = caddy_mod.validate,
    emit: Callable[[str], None] = print,
    site_host: str | None = None,
    web_port: int | None = None,
    username: str | None = None,
    caddyfile: Path | None = None,
) -> SetupResult:
    """Run the whole setup: privilege, caddy, firewall, answers, write, validate.

    On Windows an unelevated session is refused before any side effect, matching
    the old script. On Linux it warns instead, because the caddyfile is user-owned
    and only the install and firewall steps want privilege.

    Every external effect goes through an injected seam (``run``, ``which``,
    ``find_ip``, ``validate``, ``emit``), so the flow is exercised without a real
    machine behind it.
    """
    plat = platform or sys.platform
    _require(
        plat == "win32" or plat.startswith("linux"),
        f"setup-caddy supports Windows and (experimentally) Linux; on {plat!r} install "
        "caddy and write ~/.backseat/caddyfile by hand",
    )
    elevated = _is_elevated(plat)
    if plat == "win32":
        _require(
            elevated,
            "this command must run from an elevated (Administrator) shell on Windows. "
            "Nothing was installed, changed, or written.",
        )
    elif not elevated:
        emit(
            "[warn] not running as root: installing caddy or a firewall rule may prompt "
            "for sudo, and either step is skipped if it cannot run."
        )

    emit("1/7 checking privileges")
    emit("2/7 finding caddy")
    exe, installed = _ensure_caddy(plat, which=which, run=run, emit=emit)

    emit(f"3/7 firewall rule for TCP {SITE_PORT}")
    firewall_updated = _ensure_firewall(plat, SITE_PORT, which=which, run=run, emit=emit)

    emit("4/7 settings")
    answers = collect_answers(
        prompter, site_host=site_host, web_port=web_port, username=username
    )

    emit("5/7 hashing the password")
    password_hash = hash_password(exe, answers.password, run)
    emit("password hashed.")

    emit("6/7 writing ~/.backseat/caddyfile")
    path = caddyfile or caddy_mod.caddyfile_path()
    names = site_names(answers.site_host, find_ip())
    content = render_caddyfile(names, answers.web_port, answers.username, password_hash)

    emit("7/7 validating")
    write_caddyfile(
        path, content, exe=exe, prompter=prompter, validate=validate, emit=emit
    )

    emit("")
    emit("Setup finished. Next steps, in this order:")
    emit("  1. backseat-harness daemon install     (re-run so the daemon PATH includes caddy)")
    emit("  2. backseat-harness daemon restart     (starts caddy with the new caddyfile)")
    result = SetupResult(
        caddyfile=path,
        site_hosts=names,
        username=answers.username,
        caddy_installed=installed,
        firewall_updated=firewall_updated,
    )
    emit(f"Then open {result.url} and sign in as {answers.username}.")
    emit(
        "The browser will show a certificate warning (tls internal) unless you choose to "
        "trust caddy's local CA yourself."
    )
    return result
