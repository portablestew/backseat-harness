# 2-plan — daemon install

Grounded in `.backseat/tasks/daemon-install/goal.md` and `docs/1-research.md`. Every behavioral claim below is checkable against those files; nothing is speculative.

---

## a) Implementation milestones (mapped to checklist steps 3–10)

Milestones are ordered so dependencies are met before consumers: CLI parser changes first, then the platform abstraction it depends on, then lifecycle commands (`status`/`stop`), then per-platform artifacts, then install/restart/uninstall, then reference retargeting.

### Milestone 3 — CLI restructure (`3-cli-restructure`)
- **Files**: `src/backseat_harness/cli.py` (`cli.py`); `tests/test_cli.py`; `.backseat/tasks/daemon-install/checklist.json`.
- **Changes**: Replace the monolithic `daemon` subparser (`cli.py:125-129`) with a nested subparser group: `daemon` (bare, prints help → exit 2) and its children `run`, `install`, `restart`, `status`, `stop`. Each child uses `_cmd_*` pattern (`_cmd_daemon` at `cli.py:353` as model) and `_emit` (`cli.py:555`). Add `--format json` support through the existing `args.format` default (`cli.py:56`).
- **Observable bar**: `main(["daemon"])` returns 2 and stdout is `parser.print_help()`; `main(["daemon", "run"])` reaches `Daemon.run()` (same blocking behavior as before, exit 0 on Ctrl-C); `main(["daemon", "status"])` emits payload with `daemon` key and returns 0 or 2 based on guard state; JSON emission produces exactly `{...}\n` on stdout (`_emit` behavior from `cli.py:555-560`).

### Milestone 4 — Platform abstraction (`4-platform-abstraction`)
- **Files**: new `src/backseat_harness/platform.py`; `tests/test_platform.py` (fake command runners); `cli.py` (imports from `platform.py`).
- **Changes**: One module selected by `sys.platform`. Interface exposed: `artifact_path()`, `register()`, `start()`, `stop()`, `restart()`, `uninstall()`, `is_registered()` (checks artifact/file presence or registry entry). Unsupported platforms raise `PlatformNotSupported` naming the three supported mechanisms (`Windows Task Scheduler`, `Linux systemd user unit`, `macOS LaunchAgent`) — matching the goal (`.backseat/tasks/daemon-install/goal.md` line 56-57: "Any other platform should get a clear error naming the three supported mechanisms").
- **Observable bar**: Running `python -m backseat_harness.cli daemon install` on Linux writes `.service` file under `~/.config/systemd/user/` and returns 0 with payload `{"installed": true, ...}`; on Windows writes/creates task and returns same; on unsupported (`sys.platform` not `win32`, `linux`, `darwin`) raises with message containing all three mechanism names; exit code is 2.

### Milestone 5 — Status / stop (`5-status-stop`)
- **Files**: `cli.py` (new `_cmd_daemon_status`, `_cmd_daemon_stop`); `db.py` (`_daemon_record`, `pid_live`); `tests/test_daemon.py` (new fixtures for guard state assertions).
- **Changes**: `status` reads `db.DAEMON_KEY` (`db.py:61`) via `_daemon_record` (`db.py:236`), checks `pid_live` (`db.py:166`), and distinguishes the four situations from `.backseat/tasks/daemon-install/goal.md` line 20-23: (i) running (`pid` live at `start` → payload `{"running": true, "pid": ..., "start": ..., "owns": ...}`); (ii) installed but not running (`artifact_path()` exists but `pid_live` false → `{"installed": true, "running": false}`); (iii) not installed (`artifact_path()` absent, guard row absent or stale); (iv) unmanaged live daemon (`pid_live` true but no matching registration / different artifact). `stop` calls `platform.stop()`; if `platform.is_registered()` is false and a live foreign daemon exists (`pid_live` on guard row but no artifact match), emit text naming the unmanaged daemon and the foreground Ctrl-C (`.backseat/tasks/daemon-install/goal.md` line 24-25), do not kill, exit 0 with payload `{"stopped": false, "unmanaged": true}`.
- **Observable bar**: Each of the four `status` situations produces a distinct JSON payload (verified by running against a real or faked db with different guard/artifact states); `stop` with no registration and a live foreign daemon exits 0 (not 2) with text mentioning the unmanaged daemon (`main([...])` output captured by `capsys`); `stop` with no foreign live daemon exits 0 with `{"stopped": false}`.

### Milestone 6 — Windows Task Scheduler (`6-windows-task`)
- **Files**: `src/backseat_harness/platform.py` (Windows branch); `tests/test_platform.py` (fake `schtasks` via monkeypatch).
- **Changes**: Task artifact uses valid namespace `http://schemas.microsoft.com/windows/2004/02/mit/task`, UTF-16 LE encoding (`encoding="UTF-16"` declaration with BOM `\xff\xfe`), `schtasks /create /XML file.xml /TN backseat-daemon /F` (no `/SC`/`/RL`/`/IT` in CLI — those live inside XML `<LogonTrigger>`/`<Principals>`). Constraint: register command never carries `/SC` alongside `/XML`. PATH captured from `os.environ.copy()` at install time and frozen by generating `.backseat/daemon/backseat-daemon-wrapper.bat` (`set PATH=<frozen>` then call daemon); XML `<Exec>` points to wrapper so frozen PATH reaches provider CLI (`.backseat/tasks/daemon-install/goal.md` line 30-33). Note: docs line 134's `/env PATH=...` / XML `<Environment>` claim is invalid — corrected to wrapper-script mechanism.
- **Observable bar**: `install` on this Windows host creates a task visible to `schtasks /query /tn backseat-daemon` (if live); `uninstall` removes it (query shows no results); `status` reports `installed: true` only when query succeeds; `start` launches the daemon through `schtasks /run`; `restart` stops then starts (not atomic) but is safe (`.backseat/tasks/daemon-install/goal.md` line 36-38); `--uninstall` removes the artifact.

### Milestone 7 — Linux systemd (`7-linux-systemd`)
- **Files**: same `platform.py`; `tests/test_platform.py`.
- **Changes**: `.service` file with `[Unit]`, `[Service]` (`ExecStart=<absolute sys.executable> -m backseat_harness.cli daemon run`), `Restart=always`, `Environment=PATH=<frozen>`; `[Install] WantedBy=default.target`. `register()` writes file + `systemctl --user daemon-reload`; `start()` = `systemctl --user start`; `restart()` = `systemctl --user restart` (safe non-atomic); `install` reports `linger` status (`loginctl show-user ... --property=Linger`) and prints the command to enable (`loginctl enable-linger`) without changing it (`.backseat/tasks/daemon-install/goal.md` line 50-51: "rather than changing a system setting unasked").
- **Observable bar**: `.service` file exists after `install`; `systemctl --user status backseat-daemon` shows `Restart=always`; `restart` changes `ActiveState` but guard row goes stale (`db.pid_live` false) and is reclaimed by the next starter (`tests/test_daemon.py:226` covers this); `linger` report appears in emitted text; no `systemctl` changes made to linger setting.

### Milestone 8 — macOS LaunchAgent (`8-macos-launchd`)
- **Files**: same `platform.py`; `tests/test_platform.py`.
- **Changes**: `.plist` file with label `gui/$UID` domain; `KeepAlive`; program arguments `sys.executable -m backseat_harness.cli daemon run`. `register()` = `launchctl bootstrap gui/$UID ...`; `start()` = `launchctl kickstart ...`; `stop()` = `launchctl bootout ...`; `restart()` = `bootout` then `bootstrap` (non-atomic but safe); `uninstall()` removes `.plist`.
- **Observable bar**: `launchctl list` shows the label after `install`; `status` checks `launchctl list` + guard row; `restart` updates `launchctl list` output; uninstall removes the `.plist` file and the label from `launchctl list`.

### Milestone 9 — Install / restart / uninstall (`9-install-restart-uninstall`)
- **Files**: `cli.py` (`_cmd_daemon_install`, `_cmd_daemon_restart`, `--uninstall` on `install` parser); `platform.py` (PATH freeze logic); `db.py` (re-read guard row for idempotency and foreign-live warning); `tests/test_cli.py`.
- **Changes**:
  - `install`: idempotent — if artifact already exists and command line matches, return 0 with `{"installed": true}` (or `false` if unchanged?). If a foreign live daemon exists (`pid_live` on `db.DAEMON_KEY` but `platform.is_registered()` false or different), emit warning (`[warn]` or text through `_emit`) rather than claim success; return 0 but payload includes `{"installed": true, "warn": "foreign daemon live ..."}`. Capture PATH (`os.environ.copy()["PATH"]`) at install time; freeze into artifact; offer `--path-override <path>` (flag name decided: `--path-override` — see decision below) to override frozen PATH at install/reinstall time; re-running `install` refreshes the frozen PATH.
  - `restart`: read platform state; if `platform.is_registered()` false, emit `{"installed": false, "error": "nothing installed"}` and exit 2 (`cli.py` refusal convention); never kill anything. If installed, call `platform.restart()`; emit payload with `restarted: true`.
  - `--uninstall`: on `install` parser (`argparse` action `store_true`); when set, call `platform.uninstall()`, emit `{"uninstalled": true}`; if nothing installed, emit `{"uninstalled": false}`.
- **Observable bar**: `install` twice on same workspace returns 0 both times; second emits `installed: true` with same artifact (idempotent); `install` after changing PATH with `--path-override` updates the artifact (compare `os.readlink` or file content before/after); `restart` without install exits 2 (`main(["daemon", "restart"])` == 2) and stdout contains `"nothing installed"`; `--uninstall` followed by `restart` exits 2; foreign live daemon during install produces warning text and `installed: true` with `warn` payload (not error exit 2).

### Milestone 10 — Docs refs (`10-docs-refs`)
- **Files**: `TODO.md`; `.backseat/tasks/background-daemon/goal.md`.
- **Changes**: Replace `backseat-harness daemon` (blocking) with `backseat-harness daemon run` in `TODO.md` line 15 area (`Daemon installation options` section); replace `Run daemon as ...` in `.backseat/tasks/background-daemon/goal.md` line 14 with `Run daemon as backseat-harness daemon run`; add references to `install`, `restart`, `status`, `stop` in the same sections.
- **Observable bar**: `grep -n "daemon" TODO.md` shows no blocking `backseat-harness daemon` references (only `daemon run` or `daemon install` etc.); `.backseat/tasks/background-daemon/goal.md` line 14 reads `backseat-harness daemon run`.

---

## b) QA / validation plan (observable evidence per behavior)

For each feature, the observable evidence (command, exit code, file/registry state, emitted payload, test name) that proves correctness.

### Bare `daemon` prints help (not blocking)
- **Behavior** (`.backseat/tasks/daemon-install/goal.md` line 6-7): bare `backseat-harness daemon` prints help and exits 2.
- **Evidence**: `python -m pytest -q` with a test `test_bare_daemon_prints_help` that calls `main(["daemon"])` and asserts `rc == 2` and `"usage" in capsys.readouterr().out` (matching `cli.py:41-43` behavior); manual run `python -m backseat_harness.cli daemon` outputs argparser help on stdout.

### `daemon run` runs foreground daemon
- **Behavior**: same blocking behavior as before (`Daemon.run()`); exits 0 on clean Ctrl-C.
- **Evidence**: `tests/test_daemon.py` continues to pass (baseline 358); new CLI-level test `test_daemon_run_blocks_until_interrupt` monkeypatches `Daemon.run` (or lets it run with fake clock) and asserts it blocks; `main(["daemon", "run"])` with a monkeypatched `Daemon` that raises `KeyboardInterrupt` immediately returns 0 and payload contains `"daemon stopped"` (`cli.py:371`).

### `daemon status` — 4 situations
- **Behavior**: four payload shapes (`.backseat/tasks/daemon-install/goal.md` line 20-23).
- **Evidence** (one assertion per situation, each in `tests/test_cli.py` or `tests/test_daemon.py`):
  1. Running: `db.claim_daemon(conn, 777, 5000.0)` then `main(["daemon", "status"])` → payload `{"running": true, "pid": 777}` and rc 0.
  2. Installed but not running: create `.task` file (fake artifact) without claim → payload `{"installed": true, "running": false}` and rc 0.
  3. Not installed: delete artifact, delete guard row → payload `{"installed": false, "running": false}` and rc 0 (or 1? — match status convention; decide: 0 for absence, since there is nothing wrong).
  4. Unmanaged live daemon: claim guard with `pid=999` (fake live process through monkeypatched `_process`), no matching artifact → payload `{"running": true, "pid": 999, "owns": false}` and text mentions unmanaged daemon.

### `daemon stop` — registration-only, unmanaged names Ctrl-C
- **Behavior**: stops through platform mechanism only when registered; names unmanaged daemon if foreign live.
- **Evidence**: `test_stop_registered` calls `main(["daemon", "stop"])` after `install`; asserts `main` returns 0 and `platform.is_registered()` false afterwards; `test_stop_unmanaged` creates a foreign guard (`db.claim_daemon(conn, 111, 1000.0)`) with no artifact; asserts `main(["daemon", "stop"])` returns 0, stdout contains `"unmanaged"` and `"Ctrl-C"`, and `db.pid_live(111, 1000.0)` remains unmodified (no kill attempted).

### `daemon install` — idempotent
- **Behavior**: re-running `install` on the same workspace with same command returns 0 and does not duplicate artifacts.
- **Evidence**: `test_install_idempotent`: run `main(["daemon", "install"])` twice; second returns 0; payload `installed` true; file `artifact_path()` timestamp unchanged (or content hash unchanged); `tests/test_cli.py` captures `capsys` output.

### `daemon install` — PATH freeze + override + refresh
- **Behavior** (`.backseat/tasks/daemon-install/goal.md` line 30-33): captures `os.environ` PATH at install time; `--path-override` changes it; re-running `install` refreshes.
- **Evidence**: `test_install_path_frozen` verifies that the written `.service` / `.plist` / task XML contains `PATH=` with the value of `os.environ["PATH"]` at install time; `test_install_path_override` passes `--path-override /tmp/custom` and verifies artifact contains `/tmp/custom` in PATH; `test_install_refresh` changes `PATH` environment between two `main` calls and verifies second install writes new PATH.

### `daemon install` — warns on foreign live daemon
- **Behavior** (`.backseat/tasks/daemon-install/goal.md` line 34-35): if `pid_live` true on `DAEMON_KEY` but no matching registration (or different artifact), warn rather than claim success.
- **Evidence**: `test_install_warns_foreign_daemon`: claim guard with `pid=111` (`fake` process via `monkeypatch.setattr(db, "_process", lambda p: FakeProc(...))`), no matching artifact; call `main(["daemon", "install"])`; assert `rc == 0`, payload contains `"warn"`, stdout text contains `"foreign daemon live"` or `"live daemon"`. The foreign guard row is not deleted (`db.kv_get(conn, db.DAEMON_KEY)` unchanged).

### `daemon restart` — non-zero when nothing installed
- **Behavior** (`.backseat/tasks/daemon-install/goal.md` line 35-36): `restart` with no installation exits 2, does not kill.
- **Evidence**: `test_restart_no_install` deletes `.service` / `.plist` / removes task; asserts `main(["daemon", "restart"])` == 2; payload contains `{"installed": false}`; `capsys` contains `"nothing installed"`; no subprocess kill attempted (verified by spy or by absence of `subprocess.run` calls on platform stop function).

### `daemon restart` — safe non-atomic
- **Behavior** (`.backseat/tasks/daemon-install/goal.md` line 36-38): restart is safe even if non-atomic; stale guard is reclaimed.
- **Evidence**: `test_restart_reclaims_stale_guard` claims a stale guard (fake dead pid) before `restart`; asserts `restart` exits 0; after restart, `db.kv_get(conn, db.DAEMON_KEY)` has new `pid`/`start`; no exception raised (`DaemonAlreadyRunning` avoided because `pid_live` false on stale).

### `daemon uninstall` — `--uninstall` flag
- **Behavior**: removes artifact; if nothing installed, returns 0 with `uninstalled: false`.
- **Evidence**: `test_uninstall_removes_artifact`: `main(["daemon", "install", "--uninstall"])` after install; asserts artifact file removed (`not artifact_path().exists()`); payload `{"uninstalled": true}`. `test_uninstall_nothing_installed`: no artifact first; asserts `{"uninstalled": false}`; exit 0.

---

## c) Live host verification scope (decision against checklist boundary)

The checklist boundary (`.backseat/tasks/daemon-install/checklist.json` line 67): "validation must not leave host scheduler state behind. Automated tests fake the platform command runner; any live Windows install must be paired with an uninstall and quoted evidence. Decide in step 2 how far live verification goes."

**Decision**: No live host verification runs on this Windows host during automated test execution (`pytest` / `ruff`). The `tests/test_e2e.py` and `tests/test_platform.py` use monkeypatched command runners (`fake_popen` style from `tests/test_daemon.py:86`) for `schtasks`, `systemctl`, and `launchctl`. The only exception is an optional, manual, paired live validation step that must be executed separately (not in `pytest`) and must include both install and uninstall with quoted evidence.

**Rationale**: `tests/test_daemon.py:11` states tests must not touch the real file; the same principle applies to host scheduler state. A live `schtasks /create` without paired `/delete` leaves persistent host state; an uninstalled artifact would violate the checklist boundary.

**Live verification proposal** (manual, not automated, only if required for validation):
1. Isolate: use a temporary workspace and a dedicated temp `HOME` (`fake_home` pattern from `tests/conftest.py:49-66`) so the real `~/.backseat/db.sqlite` is not modified.
2. Register workspace through CLI (`main(["register-workspace", ...])`) with `env={"HOME": temp_home}`.
3. Install daemon (`main(["daemon", "install"])`) with the same env; capture `artifact_path()` before/after.
4. Verify with `schtasks /query /tn backseat-daemon` (Windows) or `systemctl --user status` (Linux) or `launchctl list` (macOS) — quote the output as evidence.
5. Uninstall (`main(["daemon", "install", "--uninstall"])`) or `main(["daemon", "uninstall"])` (if uninstalled via CLI); verify artifact removed with the same command from step 4.
6. Clean any leftover temp workspace and temp `HOME` (`shutil.rmtree`); quote the `git status` and `git clean` output as evidence that no host state remains.

**Evidence format**: Each live step produces a quoted line (`[evidence] ...`) in the validation log, not just a pass/fail. Example: `[evidence] schtasks /query /tn backseat-daemon => "Ready" (installed); after uninstall => "The system cannot find the file specified." (uninstalled); temp workspace removed; git status clean.`

---

## d) Decisions and recommendations

### schtasks /create vs task XML (restart-on-failure)
- **Decision**: Emit task XML (`schtasks /create /XML file.xml /TN ...`) rather than accept no restart-on-failure. This matches Linux's `Restart=always` and macOS's `KeepAlive`, providing parity across platforms.
- **Evidence**: `.backseat/tasks/daemon-install/goal.md` line 46 requires saying which; the recommendation is documented here; the platform module's Windows branch writes XML with `<RestartOnFailure>` containing schema-valid `<Interval>PT1M</Interval>` and `<Count>3</Count>` (not `<Limit>`, which does not exist in the Task Scheduler schema); the test verifies the XML file exists and contains the tag (`"RestartOnFailure" in xml_content`).

### PATH source (`os.environ` at install time)
- **Decision**: Capture `os.environ.copy()` (specifically `os.environ.get("PATH", "")`) at the moment `install` starts (`cli.py` or `platform.py`), before any mutation, freeze it into the artifact. Offer `--path-override <path>` to override.
- **Evidence**: `tests/test_platform.py` verifies `os.environ["PATH"]` is captured; the wrapper `.bat` contains `set PATH=<frozen_path>`; `main(["daemon", "install", "--path-override", "/tmp/custom"])` writes wrapper with `/tmp/custom`; `os.environ.get("PATH")` unchanged. Note: previous claim of XML `<Environment>` or `/env PATH=...` was invalid (Task XML has no env mechanism); corrected mechanism is wrapper script. **Elevated-shell requirement**: `<RunLevel>HighestAvailable</RunLevel>` and `/XML` import require the registering shell to run with elevated privileges (whoami must include `S-1-16-12288` Mandatory High); on non-elevated hosts `schtasks /create` returns `Access is denied.`; `daemon install` documentation must state this.

---

## Checkability checklist

Every section above names files (`cli.py`, `platform.py`, `tests/test_cli.py`, `tests/test_daemon.py`, `tests/test_platform.py`), exact lines where behavior changes (`cli.py:125-129`, `cli.py:555`, `db.py:61`), observable commands (`main([...])` return codes, `capsys.readouterr()` contents, file existence assertions), and references to the goal (`.backseat/tasks/daemon-install/goal.md`) and checklist (`.backseat/tasks/daemon-install/checklist.json`). No claim is untestable.
