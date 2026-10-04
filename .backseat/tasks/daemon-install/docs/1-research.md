# 1-research — daemon install lifecycle

Grounded in this repo (`C:\Dev\github\backseat-harness`). Every structural claim carries a `file:line` cite; nothing is invented.

---

## daemon today

Parser: `sub.add_parser("daemon", ...)` at `cli.py:125`; default handler `_cmd_daemon` at `cli.py:353`.

- Foreground behavior: `Daemon.run()` claims the single-instance guard (`db.claim_daemon`) at `daemon.py:289-300`, pumps the scheduler (`self._sched.run()` at `daemon.py:323`), releases it in a `finally` (`db.clear_daemon` at `daemon.py:327`).
- `Daemon.run` is foreground: `instance.run()` in `_cmd_daemon` (`cli.py:353`) blocks until `KeyboardInterrupt` (`cli.py:369`); the guard is cleared by the `finally` in `Daemon.run`, so a clean Ctrl-C exits 0 with `"daemon stopped"` (`cli.py:371`).
- What changes: the goal (`.backseat/tasks/daemon-install/goal.md`) moves the foreground `Daemon.run` behavior to a new subcommand `daemon run`, so `daemon` (bare) prints help instead of blocking forever, and new siblings `install`, `restart`, `status`, `stop` manage the lifecycle.

---

## guard-row API status / stop

The authority is `kv["daemon"]` (`db.DAEMON_KEY` at `db.py:61`) holding `{pid, start}`.

- Read it: `_daemon_record(conn)` (`db.py:236`) parses the JSON; returns `{"pid": int, "start": float}` or `None`.
- Write claim: `claim_daemon(conn, pid, start)` (`db.py:253`) runs inside `_txn` (`db.py:143`) (immediate begin, commit/rollback); checks `pid_live(existing["pid"], existing["start"])` before writing; raises `DaemonAlreadyRunning` (`db.py:73`) if a live holder exists.
- Live check: `pid_live(pid, start)` (`db.py:166`) uses `_process(pid)` (`db.py:161` -> `psutil.Process`) and compares `proc.create_time()` against `start` within `START_TOLERANCE_S` (`db.py:47`). A recycled pid fails the start-time compare.
- What `{pid, start}` proves: it proves the process at `pid` started at `start` and is the same process now (`pid_live`). A supervisor (e.g., Windows Task Manager, `systemctl`) can only check existence of `pid`, not the start-time identity; the cross-check is therefore non-portable and not required by the goal (`.backseat/tasks/daemon-install/goal.md` line 19-20: "a supervisor's own view may be consulted as a cross-check but is neither portable nor required").
- Clear: `clear_daemon(conn, pid, start)` (`db.py:286`) deletes the row only if `current["pid"] == pid` (and `start` matches when given); returns `True` only when it cleared its own row (`db.py:301`), preventing an exiting daemon from deleting a newer reclaim.
- Stop reads the guard (`db.DAEMON_KEY`) and stops through the platform registration (Task Scheduler / systemd user unit / LaunchAgent). Where there is no registration (`install` was never run), `stop` must not kill a foreign process; the goal requires naming the foreground Ctrl-C instead.

---

## CLI conventions to match

Parser layout (`cli.py:47-131`): `argparse.ArgumentParser` with subparsers (`sub = p.add_subparsers(dest="command")` at `cli.py:63`); each command (`init`, `run`, `takeover`, `status`, `list`, `register-workspace`, `daemon`) adds its parser; defaults set via `set_defaults(func=...)`.

- `_emit(args, payload, text)` (`cli.py:555`): if `args.format == "json"` (`cli.py:56`) dumps `json.dump(payload, sys.stdout, indent=2)` with a trailing newline; else `print(text)` (`cli.py:559`). Every subcommand uses `_emit`; new `daemon` subcommands (`run`, `install`, `restart`, `status`, `stop`) must do the same.
- Return codes: `0` for success (`_cmd_init`, `_cmd_run` when result status is `complete`/`blocked`); `2` for refusal (`_cmd_run` on missing goal or blocked lane without brief, `_cmd_daemon` when `DaemonAlreadyRunning` raised at `cli.py:366`, `_cmd_register_workspace` when workspace shape missing at `cli.py:411`); `1` for `status` when no state exists (`cli.py:330`). The goal (`.backseat/tasks/daemon-install/goal.md`) requires `restart` with nothing installed exit non-zero; match `2`.
- Bare `daemon` behavior today (`cli.py:125-129`): `sub.add_parser("daemon", help="run the background scheduler: workspace check-ins (one instance)")`; `daemon.set_defaults(func=_cmd_daemon)`; no sub-subcommands. Calling `main(["daemon"])` runs `_cmd_daemon`, which creates a `Daemon()` and blocks in `run()` (`cli.py:363-365`). After the change, a bare `daemon` must print help (`parser.print_help()`, `cli.py:42`) and exit `2` (`cli.py:43`), matching the convention used when `func` is missing.

---

## Test conventions

- Fixtures (`tests/test_daemon.py`): `no_real_psutil` (`tests/test_daemon.py:56`) monkeypatches `db._process` to a fake that raises (`_boom`) so no real `psutil.Process` is consulted; `spawns` (`tests/test_daemon.py:86`) monkeypatches `daemon.subprocess.Popen` to a spy that records `(argv, kwargs)` and returns `object()` (no real process launched); `daemon` (`tests/test_daemon.py:71`) creates `Daemon(sched=Scheduler(clock=clock), pid=777, start=5000.0, file_interval=0.01)`; `clock` (`tests/test_daemon.py:67`) is a `FakeClock` with `now`, `advance()`.
- Seam `db._process`: `db.py:161` defines `_process(pid) = psutil.Process(int(pid))`; the `no_real_psutil` fixture monkeypatches this directly (`tests/test_daemon.py:63`: `monkeypatch.setattr(db, "_process", _boom)`). Any new platform module that needs process checks must go through `db.pid_live`, which uses `_process`; faking `_process` covers both.
- CLI tests (`tests/test_cli.py`): use `main([...])` with `capsys` (`capsys.readouterr()`); monkeypatch `cli.run_task`, `cli.get_provider`, etc.; use `load_config(tmp_path)`; create `TaskPaths` and `state.json` via `storage.save_state` (`tests/test_cli.py:204`). New `daemon` CLI subcommand tests follow the same pattern: call `main(["daemon", "run"])` or `main(["daemon", "status"])` and assert return code and emitted payload.
- Test / lint commands (`README.md` line 247): `uv run ruff check .` (lint) and `uv run pytest` (tests). `README.md:245-248` shows `pyproject.toml` config (`line-length = 100`, `select = ["E","F","I","UP","B"]` at `pyproject.toml:45-50`).

---

## Platform seam

Platform selection lives at `sys.platform` (not yet present in `cli.py` or daemon code). The goal (`.backseat/tasks/daemon-install/goal.md` line 56) requires: "Keep each platform's commands in one small module selected by `sys.platform`"; unsupported platforms must get a clear error naming the three supported mechanisms.

Per mechanism (quoted from `.backseat/tasks/daemon-install/goal.md`):

- **Windows, Task Scheduler** (`.backseat/tasks/daemon-install/goal.md` line 42-47):
  - Artifact: a scheduled task (`schtasks /create`).
  - Constraint: must run "only when the user is logged on, with the interactive token"; a task running any other way executes in Session 0, where provider CLI is neither on PATH nor authenticated.
  - Register: `schtasks /create /tn ... /tr ... /sc ONLOGON /rl HIGHEST` (or equivalent XML); `sys.executable -m backseat_harness.cli daemon run` as command.
  - Start/stop/restart/uninstall: `schtasks /run`, `/end`, `/delete`.
  - Restart-on-failure: the goal explicitly asks to "decide whether to accept that [`schtasks /create` cannot express restart-on-failure] or emit task XML, and say which". Recommendation: emit task XML (`/XML` import) because it supports `RestartOnFailure` (`<RestartOnFailure>`); this is the only way to express the same `Restart=always` behavior Linux gets. Document the decision.
  - PATH capture: freeze the installing shell's PATH (`os.environ.copy()` or read `PATH` before any mutation) into the artifact environment (`/env` or XML `<Environment>`); corrected mechanism is wrapper `.bat` (`set PATH=<frozen>`) pointing XML `<Exec>` to it (Task XML has no `<Environment>` element).
  - **Elevated-shell requirement** (verified by live probe `e320bf5`): `<RunLevel>HighestAvailable</RunLevel>` with `/XML` import requires elevated privileges. On a non-elevated host (`whoami`: Mandatory Label `Medium` `S-1-16-8192`, no Elevated `S-1-16-12288`), both `schtasks /create /XML` of the host's own valid exported task (`Microsoft\Windows\Defrag\ScheduledDefrag`) and the fixed artifact return identical `Access is denied.`; when the invalid `<Limit>` element was present, the error was `unexpected node` instead. `daemon install` documentation must state: run from an elevated shell (or the host scheduler rejects the import).
- **Linux, systemd user unit** (`.backseat/tasks/daemon-install/goal.md` line 48-51):
  - Artifact: `~/.config/systemd/user/backseat-daemon.service` (user unit, not system-wide).
  - Register: write the `.service` file; run `systemctl --user daemon-reload`.
  - Start/stop/restart: `systemctl --user start/stop/restart backseat-daemon`.
  - Uninstall: `systemctl --user stop`; delete the `.service`; `daemon-reload`.
  - Constraint: `Restart=always` in the unit; captured PATH written into `[Service] Environment=PATH=...`; report whether `linger` is enabled (`loginctl show-user $USER --property=Linger`) and print the command to enable (`loginctl enable-linger`) rather than changing it unasked.
- **macOS, launchd LaunchAgent** (`.backseat/tasks/daemon-install/goal.md` line 52-54):
  - Artifact: `~/Library/LaunchAgents/com.backseat.harness.plist`.
  - Constraint: `KeepAlive` provides crash restart; prefer current `bootstrap`/`bootout`/`kickstart` verbs over legacy `load`; recent macOS wants `gui/$UID` domain (the plist domain is the user GUID).
  - Register: `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.backseat.harness.plist`.
  - Start/stop/restart/uninstall: `launchctl kickstart -k ...` / `bootout ...` / `bootstrap ...`; uninstall removes the `.plist`.

The platform module (`sys.platform` selection) must expose a consistent interface (artifact path, register, start, stop, restart, uninstall) and return the captured PATH and the registered command line (`sys.executable -m backseat_harness.cli daemon run`) so `install` can refresh the artifact without rewriting the whole module.

---

## The two reference files to retarget

From `.backseat/tasks/background-daemon/goal.md` (`.backseat/tasks/background-daemon/goal.md` line 14):

```
- Run daemon as `backseat-harness daemon`
```

This must become the new `daemon run` reference; the bare `backseat-harness daemon` must point to the help-output behavior.

From `TODO.md` (`TODO.md` line 15):

```
- Daemon installation options
```

And the surrounding context (`TODO.md` line 13: `Daemon installation` under "General usage" in the README review checklist) must reference the new subcommands (`install`, `restart`, `status`, `stop`, `run`). The exact daemon-command line references in both files (`backseat-harness daemon` as a blocking command, the single-instance guard description, the `run daemon as ...` line) are quoted above and must be retargeted.

---

## Baseline

Command: `python -m pytest -q`

Pass summary: `358 passed in 56.75s` (recorded before any source change in this session).

---

## Open questions

Genuinely ambiguous points left by `.backseat/tasks/daemon-install/goal.md` (not decided by this repo):

1. **PATH-override flag name** — the goal says "offer a way to override" the frozen PATH (line 30) but does not name the flag (`--env`? `--path`? `--with-env`?). The CLI already uses `-C` (`--workspace`) for workspace root (`cli.py:52`); a new `--env` or `--path-override` is undecided.
2. **Per-platform artifact paths** — the user-unit path (`~/.config/systemd/user/`) is standard, but the exact file name (`backseat-daemon.service`, `backseat-harness-daemon.service`?) is not specified; same for LaunchAgent label (`com.backseat.harness`, `org.backseat-harness`?) and Windows task name.
3. **Whether live install/uninstall may touch this host during validation** — the checklist remark (`.backseat/tasks/daemon-install/checklist.json` line 67) says "validation must not leave host scheduler state behind" and asks whether automated tests may touch the host; the answer must be decided in step 2 (plan) and affects whether `tests/test_e2e.py` runs `schtasks /create` or only fakes the command runner.
4. **Restart-on-failure artifact format** — the goal explicitly leaves this open (`.backseat/tasks/daemon-install/goal.md` line 46): `schtasks /create` cannot express it; the recommendation is to emit task XML (`/XML` import with `RestartOnFailure`), but whether the module uses `schtasks /create /XML file.xml` or writes XML directly and imports with `schtasks /create /XML ... /TN ...` is not locked.
5. **Live validation scope** — `.backseat/tasks/daemon-install/goal.md` does not say whether `install` runs the daemon immediately after registration, or only writes the artifact; `restart` implies it must be installed first, but whether `restart` also calls `start` through the mechanism (as `systemctl restart` does) or stops then starts separately is ambiguous.
6. **PATH freeze mechanism** — whether the captured PATH comes from `os.environ["PATH"]` at `install` time, or from reading the parent process environment (`os.environ.copy()`), and whether it should include the user's full `PATH` or a minimal subset (provider CLI needs at minimum the CLI binary's directory).

---

Baseline command (exact, quoted): `python -m pytest -q`
Baseline pass summary line (quoted): `358 passed in 56.75s`
