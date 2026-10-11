# backseat-harness

Simple, single-task agent loops. _Buckle up, you're not at the wheel anymore._

## What it does

backseat-harness automates vibecoding toward a goal. You write the goal once, as the requirements for finishing a task. The harness drives an off-the-shelf CLI coding agent (opencode, claude, or kiro-cli) one small step at a time toward it. This is the "managed" agent session. After each step, a separate operator agent reviews the whole change, code, tests, and transcript, and writes the next prompt. That is the ordinary vibecoding loop (prompt, work, review, next prompt) that coding models are already trained to follow.

The managed agent keeps the long-horizon context across the whole task, instead of passing lossy summaries between agents. The operator is a fresh, adversarial reviewer: it starts each review cold and reads the full diff itself, along with a transcript of the managed agent session. Rather than a fixed pipeline of stages, it is a continuous, unstructured adversary. Each managed turn is a plain session of the provider's own CLI, so you can intervene and `takeover` a lane and drive it yourself at any time.

## Requirements

- Python 3.11 or newer.
- One provider CLI on your PATH, installed and signed in: `claude`, `kiro-cli`, or `opencode`. Each CLI uses its own login.
- Managed runs are auto-approved, since a non-interactive run has nobody to answer a prompt.

## Install

```bash
pip install -e .   # or: uv sync, for a dev environment
```

## Quick start

```bash
# start a task: creates .backseat/tasks/<slug>/, and the operator plans the first step
backseat-harness run hello-endpoint "Add a GET /hello endpoint returning JSON."

# resume later: the operator picks the next step
backseat-harness run hello-endpoint

# answer a blocked lane: your words ride every operator prompt of this run
backseat-harness run hello-endpoint "Use SQLite; no external service."

# drive the managed session yourself, then the loop resumes when you quit
backseat-harness takeover hello-endpoint

# check progress (--format goes before the subcommand)
backseat-harness --format json status hello-endpoint
backseat-harness list
```

A blocked or complete lane refuses a bare `run` and prints the pending question. `init <slug> [goal]` scaffolds a lane without running it.

## Daemon install

The daemon runs lanes in the background, so you don't start each one by hand. Once installed, it starts any lane that has a goal and is not blocked or finished, unattended and auto-approved, one lane at a time per workspace.

```bash
backseat-harness register-workspace /path/to/workspace   # the daemon scans registered workspaces (must contain .backseat/, e.g. from init)
backseat-harness daemon install                          # register and start the daemon
backseat-harness daemon status                           # show the daemon's status
backseat-harness daemon restart                          # restart the registered daemon
backseat-harness daemon stop                             # stop it
backseat-harness daemon install --uninstall              # remove it (stop the daemon first)
```

On Windows, `daemon install` registers a Task Scheduler task named `backseat-daemon`. It runs as your account and starts at logon. Its output goes to `~/.backseat/daemon.log`. `daemon install` does not stop a daemon that is already running, and on Windows uninstall does not stop one either, so run `daemon stop` first. To run it in the foreground instead, use `backseat-harness daemon run`, adding `--web` to serve the web interface from the same process.

On Linux, `daemon install` registers a systemd user unit, and on macOS a LaunchAgent. _TODO: testing on these platforms_

The installed daemon also serves the web interface on the configured port (default 8080), so a separate `backseat-harness web` on that port will not start.

## Web interface

```bash
backseat-harness web          # http://127.0.0.1:8080, localhost only; --host and --port change it
```

- **Workspaces:** register a folder and see its tasks.
- **Tasks:** create a task from a name and goal, then review its status, checklist, and transcript.
- **Runs:** start a task run from its page, with an optional brief.
- **Chats:** browse and search provider sessions, and fork one.
- **Shell:** open a terminal in the browser at a workspace.

## Configuration

Config is layered: `~/.backseat/config.toml`, then `.backseat/config.toml` in the workspace, then `.backseat/tasks/<slug>/config.toml` for a task. Higher layers win. See [`examples/.backseat/config.toml`](examples/.backseat/config.toml).

## Development

```bash
uv run ruff check .
uv run pytest
```

## License

MIT. See [LICENSE](LICENSE).
