"""``setup-caddy``: the ported, in-package caddy setup helper.

Everything the flow touches outside the process goes through an injected seam
(``run``, ``which``, ``find_ip``, ``validate``, ``emit``) and the prompts go
through a ``Prompter``, so the whole path runs offline: no real caddy, no real
terminal, no real elevation.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from backseat_harness import cli
from backseat_harness.web import caddy as caddy_mod
from backseat_harness.web import caddy_setup
from backseat_harness.web.caddy_setup import (
    SetupRefusal,
    collect_answers,
    hash_password,
    render_caddyfile,
    run_setup,
    site_names,
    write_caddyfile,
)

_BCRYPT = "$2a$14$abcdefghijklmnopqrstuvwx"


class FakePrompter:
    """A scripted Prompter that records the order it was asked."""

    def __init__(self, lines=(), secrets=(), confirm=True):
        self._lines = list(lines)
        self._secrets = list(secrets)
        self._confirm = confirm if callable(confirm) else (lambda prompt, expect: confirm)
        self.calls: list[tuple[str, str]] = []

    def line(self, prompt: str, default: str) -> str:
        self.calls.append(("line", prompt))
        return self._lines.pop(0)

    def secret(self, prompt: str) -> str:
        self.calls.append(("secret", prompt))
        return self._secrets.pop(0)

    def confirm(self, prompt: str, expect: str) -> bool:
        self.calls.append(("confirm", prompt))
        return self._confirm(prompt, expect)


class RecordingRun:
    """A ``run`` seam that records argv and returns canned stdout per keyword."""

    def __init__(self, stdout_for: dict[str, str] | None = None):
        self.calls: list[list[str]] = []
        self._stdout_for = stdout_for or {}

    def __call__(self, argv, **kwargs) -> subprocess.CompletedProcess:
        argv = list(argv)
        self.calls.append(argv)
        stdout = next((v for k, v in self._stdout_for.items() if k in argv), "")
        return subprocess.CompletedProcess(argv, 0, stdout, "")


def _which(caddy: str | None = "/usr/bin/caddy", ufw: str | None = None):
    def which(name: str) -> str | None:
        if name == "caddy":
            return caddy
        if name == "ufw":
            return ufw
        return None

    return which


# --- refusal / privilege -------------------------------------------------------


def test_windows_unelevated_refuses_before_any_side_effect(tmp_path, monkeypatch):
    """Unelevated on Windows, the flow refuses before caddy, firewall, or a write."""
    monkeypatch.setattr(caddy_setup, "_is_elevated", lambda platform: False)
    run = RecordingRun()
    caddyfile = tmp_path / "home" / ".backseat" / "caddyfile"
    prompter = FakePrompter(lines=["site.lan"], secrets=["pw", "pw"])

    with pytest.raises(SetupRefusal) as info:
        run_setup(
            prompter=prompter,
            platform="win32",
            run=run,
            which=_which(),
            find_ip=lambda: None,
            validate=lambda exe, path: None,
            emit=lambda line: None,
            caddyfile=caddyfile,
        )

    assert "elevated" in str(info.value)
    assert "Nothing was installed, changed, or written" in str(info.value)
    assert run.calls == [], "a command ran before the elevation check"
    assert not caddyfile.exists()
    assert prompter.calls == [], "a prompt ran before the elevation check"


def test_unsupported_platform_is_refused():
    with pytest.raises(SetupRefusal, match="supports Windows and"):
        run_setup(prompter=FakePrompter(), platform="darwin", emit=lambda line: None)


# --- prompt order --------------------------------------------------------------


def test_prompts_host_then_port_then_username_then_password(tmp_path):
    """Host and port are adjacent, and username and password are adjacent."""
    prompter = FakePrompter(
        lines=["site.lan", "8090", "alice"], secrets=["pw", "pw"]
    )
    run_setup(
        prompter=prompter,
        platform="linux",
        run=RecordingRun(stdout_for={"hash-password": _BCRYPT}),
        which=_which(),
        find_ip=lambda: None,
        validate=lambda exe, path: None,
        emit=lambda line: None,
        caddyfile=tmp_path / "caddyfile",
    )
    kinds = prompter.calls
    assert [kind for kind, _ in kinds] == ["line", "line", "line", "secret", "secret"]
    assert "host" in kinds[0][1]
    assert "port" in kinds[1][1]
    assert "username" in kinds[2][1]
    assert "password" in kinds[3][1].lower()
    assert "repeat" in kinds[4][1].lower()


def test_overrides_skip_their_prompts_but_password_is_always_typed():
    """Flags answer host/port/user; the password is still typed twice and hidden."""
    prompter = FakePrompter(secrets=["pw", "pw"])
    answers = collect_answers(
        prompter, site_host="site.lan", web_port=9000, username="bob"
    )
    assert (answers.site_host, answers.web_port, answers.username) == ("site.lan", 9000, "bob")
    assert [kind for kind, _ in prompter.calls] == ["secret", "secret"]


# --- answers -------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"site_host": "bad_host!"}, "site host"),
        ({"username": "bad user"}, "username"),
        ({"web_port": 0}, "out of range"),
        ({"web_port": 70000}, "out of range"),
    ],
)
def test_collect_answers_validates(kwargs, message):
    prompter = FakePrompter(secrets=["pw", "pw"])
    base = {"site_host": "site.lan", "web_port": 8090, "username": "alice"}
    base.update(kwargs)
    with pytest.raises(SetupRefusal, match=message):
        collect_answers(prompter, **base)


def test_collect_answers_rejects_empty_and_mismatched_password():
    overrides = {"site_host": "site.lan", "web_port": 8090, "username": "alice"}
    with pytest.raises(SetupRefusal, match="empty"):
        collect_answers(FakePrompter(secrets=["", ""]), **overrides)
    with pytest.raises(SetupRefusal, match="do not match"):
        collect_answers(FakePrompter(secrets=["one", "two"]), **overrides)


# --- caddyfile -----------------------------------------------------------------


def test_site_names_lowercases_and_dedupes():
    assert site_names("Site.LAN", "192.168.1.5") == (
        "site.lan",
        "localhost",
        "127.0.0.1",
        "192.168.1.5",
    )
    # A localhost host must not appear twice; caddy rejects a duplicate address.
    assert site_names("localhost", None) == ("localhost", "127.0.0.1")


def test_render_caddyfile_shape():
    text = render_caddyfile(("site.lan", "localhost"), 8090, "alice", _BCRYPT)
    assert "site.lan:8443, localhost:8443 {" in text
    assert "skip_install_trust" in text
    assert "auto_https disable_redirects" in text
    assert "\t\talice " + _BCRYPT in text
    assert "reverse_proxy 127.0.0.1:8090" in text


@pytest.mark.parametrize("address", ["10.0.0.1", "172.16.0.1", "192.168.1.5"])
def test_private_ipv4_true(address):
    assert caddy_setup._is_private_ipv4(address)


@pytest.mark.parametrize("address", ["8.8.8.8", "172.32.0.1", "not-an-ip", "::1"])
def test_private_ipv4_false(address):
    assert not caddy_setup._is_private_ipv4(address)


# --- hashing -------------------------------------------------------------------


def test_hash_password_takes_the_last_line():
    run = RecordingRun(stdout_for={"hash-password": "log noise\n" + _BCRYPT + "\n"})
    assert hash_password("/usr/bin/caddy", "pw", run) == _BCRYPT


def test_hash_password_rejects_a_non_bcrypt_reply():
    run = RecordingRun(stdout_for={"hash-password": "not a hash\n"})
    with pytest.raises(SetupRefusal, match="did not return a bcrypt hash"):
        hash_password("/usr/bin/caddy", "pw", run)


# --- writing -------------------------------------------------------------------


def test_write_refuses_without_replace_confirmation(tmp_path):
    path = tmp_path / "caddyfile"
    path.write_text("old\n", encoding="utf-8")
    prompter = FakePrompter(confirm=False)
    with pytest.raises(SetupRefusal, match="not replacing"):
        write_caddyfile(
            path,
            "new\n",
            exe="/usr/bin/caddy",
            prompter=prompter,
            validate=lambda exe, staged: None,
            emit=lambda line: None,
        )
    assert path.read_text(encoding="utf-8") == "old\n"
    assert not (tmp_path / "caddyfile.new").exists()


def test_write_backs_up_the_existing_file_on_replace(tmp_path):
    path = tmp_path / "caddyfile"
    path.write_text("old\n", encoding="utf-8")
    write_caddyfile(
        path,
        "new\n",
        exe="/usr/bin/caddy",
        prompter=FakePrompter(confirm=True),
        validate=lambda exe, staged: None,
        emit=lambda line: None,
    )
    assert path.read_text(encoding="utf-8") == "new\n"
    backups = list(tmp_path.glob("caddyfile.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "old\n"


def test_write_validation_failure_leaves_the_existing_file(tmp_path):
    path = tmp_path / "caddyfile"
    path.write_text("old\n", encoding="utf-8")

    def reject(exe: str, staged: Path) -> None:
        raise caddy_mod.CaddyRefusal("caddy validate rejected it")

    with pytest.raises(SetupRefusal, match="left alone"):
        write_caddyfile(
            path,
            "new\n",
            exe="/usr/bin/caddy",
            prompter=FakePrompter(confirm=True),
            validate=reject,
            emit=lambda line: None,
        )
    assert path.read_text(encoding="utf-8") == "old\n"
    assert not (tmp_path / "caddyfile.new").exists()


# --- the whole flow ------------------------------------------------------------


def test_run_setup_linux_writes_the_caddyfile(tmp_path):
    caddyfile = tmp_path / ".backseat" / "caddyfile"
    emitted: list[str] = []
    result = run_setup(
        prompter=FakePrompter(lines=["site.lan", "8090", "alice"], secrets=["pw", "pw"]),
        platform="linux",
        run=RecordingRun(stdout_for={"hash-password": _BCRYPT}),
        which=_which(),
        find_ip=lambda: "192.168.1.5",
        validate=lambda exe, path: None,
        emit=emitted.append,
        caddyfile=caddyfile,
    )
    text = caddyfile.read_text(encoding="utf-8")
    assert "site.lan:8443, localhost:8443, 127.0.0.1:8443, 192.168.1.5:8443 {" in text
    assert result.site_hosts == ("site.lan", "localhost", "127.0.0.1", "192.168.1.5")
    assert result.username == "alice"
    assert result.caddy_installed is False
    assert result.url.startswith("https://site.lan:8443 or ")


def test_run_setup_linux_installs_caddy_when_missing(tmp_path):
    """With no caddy on PATH, apt-get is used (speculative Linux path)."""
    run = RecordingRun(stdout_for={"hash-password": _BCRYPT})
    state = {"installed": False}

    def which(name: str) -> str | None:
        if name == "caddy":
            return "/usr/bin/caddy" if state["installed"] else None
        if name == "apt-get":
            return "/usr/bin/apt-get"
        return None

    def run_and_install(argv, **kwargs):
        result = run(argv, **kwargs)
        if "install" in argv and "caddy" in argv:
            state["installed"] = True
        return result

    caddy_setup.run_setup(
        prompter=FakePrompter(lines=["site.lan", "8090", "alice"], secrets=["pw", "pw"]),
        platform="linux",
        run=run_and_install,
        which=which,
        find_ip=lambda: None,
        validate=lambda exe, path: None,
        emit=lambda line: None,
        caddyfile=tmp_path / "caddyfile",
    )
    assert any("install" in call and "caddy" in call for call in run.calls)


# --- CLI wiring ----------------------------------------------------------------


def test_parser_has_setup_caddy():
    args = cli._build_parser().parse_args(["setup-caddy", "--host", "site.lan", "--user", "bob"])
    assert args.host == "site.lan"
    assert args.user == "bob"
    assert callable(args.func)


def test_cli_setup_caddy_refusal_exits_2_json(monkeypatch, capsys):
    def refuse(**kwargs):
        raise SetupRefusal("caddy is not on PATH")

    monkeypatch.setattr(caddy_setup, "run_setup", refuse)
    code = cli.main(["--format", "json", "setup-caddy"])
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "caddy is not on PATH"


# --- the real prompter ---------------------------------------------------------


def test_console_prompter_uses_echo_char(monkeypatch):
    """The real prompter asks getpass to echo ``*`` (masked paste confirmation)."""
    seen: dict[str, object] = {}

    def fake_getpass(prompt, echo_char=None):
        seen["prompt"] = prompt
        seen["echo_char"] = echo_char
        return "pw"

    monkeypatch.setattr(caddy_setup.getpass, "getpass", fake_getpass)
    assert caddy_setup.ConsolePrompter().secret("Password") == "pw"
    assert seen["echo_char"] == "*"
    assert seen["prompt"] == "Password: "


def test_console_prompter_falls_back_without_echo_char(monkeypatch):
    """On Python < 3.14 (no ``echo_char``), the hidden read still works."""
    calls: list[str] = []

    def old_getpass(prompt):
        calls.append(prompt)
        return "pw"

    monkeypatch.setattr(caddy_setup.getpass, "getpass", old_getpass)
    assert caddy_setup.ConsolePrompter().secret("Password") == "pw"
    assert calls == ["Password: "]


def test_cli_setup_caddy_success_payload(monkeypatch, capsys):
    result = caddy_setup.SetupResult(
        caddyfile=Path("/home/u/.backseat/caddyfile"),
        site_hosts=("site.lan", "localhost"),
        username="alice",
        caddy_installed=True,
        firewall_updated=True,
    )
    monkeypatch.setattr(caddy_setup, "run_setup", lambda **kwargs: result)
    code = cli.main(["--format", "json", "setup-caddy"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["caddy_installed"] is True
    assert payload["url"] == "https://site.lan:8443 or https://localhost:8443"
