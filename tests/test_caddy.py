"""Caddy web mode helpers: caddyfile site addresses, mode resolution, and the
caddy child process (find, validate, probe, start, stop, unexpected exit).

The child tests run the ``fake_caddy`` stub (tests/conftest.py) on PATH, so
no real caddy is needed.
"""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

import psutil
import pytest

from backseat_harness.web import caddy as caddy_mod
from backseat_harness.web.caddy import (
    CaddyRefusal,
    caddyfile_path,
    find_caddy,
    probe_ports,
    resolve_mode,
    site_hosts,
    start,
    validate,
)

REFUSED = CaddyRefusal

SITE_BODY = "{\n\treverse_proxy 127.0.0.1:8090\n}\n"


@pytest.mark.parametrize(
    "text, expected",
    [
        # host:port keeps the port and nothing else.
        (f"site.lan:8443 {SITE_BODY}", ({"site.lan:8443"}, {8443})),
        # Case is folded: the middleware compares lowercase.
        (f"SITE.LAN:8443 {SITE_BODY}", ({"site.lan:8443"}, {8443})),
        # https:// and bare hosts default to 443, and both Host forms are allowed.
        (f"https://Site.lan {SITE_BODY}", ({"site.lan", "site.lan:443"}, {443})),
        (f"site.lan {SITE_BODY}", ({"site.lan", "site.lan:443"}, {443})),
        # http:// defaults to 80.
        (f"http://site.lan {SITE_BODY}", ({"site.lan", "site.lan:80"}, {80})),
        # Brace on the next line is the same site block.
        ("site.lan\n{\n}\n", ({"site.lan", "site.lan:443"}, {443})),
        # A host-less port answers on either loopback name.
        (f":9443 {SITE_BODY}", ({"localhost:9443", "127.0.0.1:9443"}, {9443})),
        # Comma-separated addresses in one block.
        (
            f"a.lan, b.lan:8443 {SITE_BODY}",
            ({"a.lan", "a.lan:443", "b.lan:8443"}, {443, 8443}),
        ),
        # Two site blocks.
        (
            f"a.lan:8443 {SITE_BODY}b.lan:9443 {SITE_BODY}",
            ({"a.lan:8443", "b.lan:9443"}, {8443, 9443}),
        ),
        # Global options, a snippet, comments, and nested directive blocks are skipped.
        (
            "# top comment\n"
            "{\n\tadmin off\n}\n"
            "(common) {\n\theader X-Frame-Options DENY\n}\n"
            "site.lan:8443 { # the site\n"
            "\tbasic_auth {\n\t\tuser $2a$14$hash\n\t}\n"
            "\treverse_proxy 127.0.0.1:8090\n"
            "}\n",
            ({"site.lan:8443"}, {8443}),
        ),
        # Refusals: unsupported forms fail loudly rather than dropping a site.
        ("*.lan:8443 {}", REFUSED),
        ("[::1]:8443 {}", REFUSED),
        ("site.lan/admin {}", REFUSED),
        ("h2c://site.lan {}", REFUSED),
        ("site.lan:0 {}", REFUSED),
        ("site.lan:70000 {}", REFUSED),
        ("site.lan:abc {}", REFUSED),
        ("bad_host! {}", REFUSED),
        # Refusals: no site address at all.
        ("", REFUSED),
        ("{\n\tadmin off\n}\n", REFUSED),
        ("(common) {\n\theader X DENY\n}\n", REFUSED),
        # Refusals: malformed structure.
        ("site.lan {}\n}", REFUSED),
        ("site.lan {", REFUSED),
        ("site.lan:8443", REFUSED),
    ],
)
def test_site_hosts_forms(text, expected):
    if expected is REFUSED:
        with pytest.raises(CaddyRefusal):
            site_hosts(text)
    else:
        assert site_hosts(text) == expected


def test_resolve_mode_auto_follows_caddyfile(fake_home):
    path = caddyfile_path()
    assert path == fake_home / ".backseat" / "caddyfile"

    # auto: no file means direct; the file's presence flips it to caddy.
    assert resolve_mode("auto", path) == "direct"
    path.parent.mkdir(parents=True)
    path.write_text("site.lan:8443 {\n}\n", encoding="utf-8")
    assert resolve_mode("auto", path) == "caddy"

    # direct ignores the file.
    assert resolve_mode("direct", path) == "direct"

    # explicit caddy is refused without a file, and accepted with one.
    assert resolve_mode("caddy", path) == "caddy"
    path.unlink()
    with pytest.raises(CaddyRefusal, match="needs a caddyfile"):
        resolve_mode("caddy", path)

    # none is the CLI's job; the resolver does not accept it.
    with pytest.raises(ValueError, match="unknown web mode"):
        resolve_mode("none", path)


# --- the caddy child -----------------------------------------------------------


def _wait_until(condition: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the stub caddy")
        time.sleep(0.05)


def _gone(pid: int) -> bool:
    """True once the pid has exited (a zombie counts as exited: it no longer runs)."""
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _caddyfile(tmp_path: Path) -> Path:
    path = tmp_path / "caddyfile"
    path.write_text(f"site.lan:8443 {SITE_BODY}", encoding="utf-8")
    return path


def test_find_caddy_missing_is_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(CaddyRefusal, match="not on PATH"):
        find_caddy()


def test_find_caddy_uses_the_stub_on_path(fake_caddy):
    assert "caddy" in Path(find_caddy()).name


def test_validate_passes_adapter_and_accepts_zero_exit(fake_caddy, tmp_path):
    path = _caddyfile(tmp_path)
    validate(find_caddy(), path)
    assert fake_caddy.calls() == [["validate", "--config", str(path), "--adapter", "caddyfile"]]


def test_validate_failure_is_refusal(fake_caddy, tmp_path):
    fake_caddy.mode("validate_fail")
    with pytest.raises(CaddyRefusal, match="rejected") as info:
        validate(find_caddy(), _caddyfile(tmp_path))
    assert "unrecognized directive" in str(info.value)


def _listener(kind: str, port: int) -> socket.socket:
    if kind == "dual":
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind(("::", port))
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("" if kind == "wildcard" else "127.0.0.1", port))
    sock.listen()
    return sock


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize("kind", ["wildcard", "loopback", "dual"])
def test_probe_busy_port_is_refusal(kind):
    """A real bind per listener kind. Windows hides a listener from a bind on
    another address (the reason probe_ports checks three), and SO_REUSEADDR
    would let the bind succeed on a busy port, so the probe must not set it."""
    port = _free_port()
    try:
        listener = _listener(kind, port)
    except OSError:
        pytest.skip(f"cannot bind a {kind} listener here")
    try:
        with pytest.raises(CaddyRefusal, match=f"site port {port} is already in use"):
            probe_ports([port])
    finally:
        listener.close()


def test_probe_free_port_passes():
    probe_ports([_free_port()])


def test_start_passes_run_config_adapter(fake_caddy, tmp_path):
    path = _caddyfile(tmp_path)
    handle = start(find_caddy(), path, log_path=tmp_path / "caddy.log", on_exit=lambda rc: None)
    try:
        _wait_until(lambda: (fake_caddy.out / "run.pid").exists())
    finally:
        handle.stop()
    assert ["run", "--config", str(path), "--adapter", "caddyfile"] in fake_caddy.calls()


def test_stop_exits_gracefully_and_reports_nothing(fake_caddy, tmp_path):
    exits: list[int] = []
    handle = start(find_caddy(), _caddyfile(tmp_path), log_path=None, on_exit=exits.append)
    _wait_until(lambda: (fake_caddy.out / "run.pid").exists())
    pid = fake_caddy.pid("run")

    started = time.monotonic()
    handle.stop()
    elapsed = time.monotonic() - started

    assert _gone(pid)
    assert elapsed < caddy_mod._STOP_GRACE_S  # the graceful signal did it, not the fallback
    handle.stop()  # idempotent
    assert exits == []


def test_stop_kills_tree(fake_caddy, tmp_path):
    """A caddy that ignores the graceful signal, with a child: the fallback
    tree-kill must take out both processes."""
    fake_caddy.mode("spawn_child")
    exits: list[int] = []
    handle = start(find_caddy(), _caddyfile(tmp_path), log_path=None, on_exit=exits.append)
    try:
        _wait_until(lambda: (fake_caddy.out / "child.pid").exists())
    except AssertionError:
        handle.stop()
        raise
    run_pid, child_pid = fake_caddy.pid("run"), fake_caddy.pid("child")

    handle.stop(timeout=0.5)

    assert _gone(run_pid)
    assert _gone(child_pid)
    assert exits == []


def test_unexpected_exit_calls_on_exit_once(fake_caddy, tmp_path):
    fake_caddy.mode("die_after")
    exited = threading.Event()
    exits: list[int] = []

    def on_exit(rc: int) -> None:
        exits.append(rc)
        exited.set()

    handle = start(find_caddy(), _caddyfile(tmp_path), log_path=None, on_exit=on_exit)
    try:
        assert exited.wait(timeout=10.0), "watch thread did not report the caddy death"
        handle.stop()
    finally:
        handle.stop()
    assert exits == [3]


# --- real caddy (skipped when the binary is absent) ----------------------------

_README = Path(__file__).resolve().parents[1] / "README.md"
_EXAMPLE_START = "<!-- caddyfile-example:start -->"
_EXAMPLE_END = "<!-- caddyfile-example:end -->"


def _readme_caddyfile() -> str:
    text = _README.read_text(encoding="utf-8")
    block = text[text.index(_EXAMPLE_START) + len(_EXAMPLE_START) : text.index(_EXAMPLE_END)]
    match = re.search(r"```\n(.*?)```", block, re.S)
    assert match, "README caddyfile example has no fenced block"
    return match.group(1)


@pytest.mark.skipif(shutil.which("caddy") is None, reason="caddy binary is not on PATH")
def test_real_caddy_validates_readme_example(tmp_path):
    """The README example passes a real ``caddy validate`` once its hash is real.

    The placeholder hash in the README is replaced with one generated here by
    ``caddy hash-password``, so the check never depends on a fixed hash.
    """
    exe = shutil.which("caddy")
    hashed = subprocess.run(
        [exe, "hash-password", "--plaintext", "readme-check"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.strip()
    text = _readme_caddyfile().replace("<bcrypt hash>", hashed)
    assert "<bcrypt hash>" not in text

    path = tmp_path / "caddyfile"
    path.write_text(text, encoding="utf-8")
    validate(exe, path)
    assert site_hosts(text) == ({"site.lan:8443"}, {8443})

    broken = tmp_path / "broken"
    broken.write_text(text.replace("reverse_proxy", "reverse_proxi"), encoding="utf-8")
    with pytest.raises(CaddyRefusal):
        validate(exe, broken)
