# backseat-harness

Simple, single-task agent loops. _Buckle up, you're not at the wheel anymore._

## What it does

backseat-harness automates vibecoding toward a goal. You write the goal once, as the requirements for finishing a task. The harness drives an off-the-shelf CLI coding agent (opencode, claude, or kiro-cli) one small step at a time toward it. This is the "managed" agent session. After each step, a separate operator agent reviews the whole change, code, tests, and transcript, and writes the next prompt. That is the ordinary vibecoding loop (prompt, work, review, next prompt) that coding models are already trained to follow.

The managed agent keeps the long-horizon context across the whole task, instead of passing lossy summaries between agents. The operator is a fresh, adversarial reviewer: it starts each review cold and reads the full diff itself, along with a transcript of the managed agent session. Rather than a fixed pipeline of stages, it is a continuous, unstructured adversary. Each managed turn is a plain session of the provider's own CLI, so you can intervene and `takeover` a lane and drive it yourself at any time.

## Requirements

- Python 3.11 or newer.
- At least one provider CLI on your PATH, installed and signed in: `claude`, `kiro-cli`, or `opencode`. Each CLI uses its own login.
- Managed runs are auto-approved, since a non-interactive run has nobody to answer a prompt.

## Quick start

The quick start sets up [Caddy](https://caddyserver.com) in front of the web interface (HTTPS + a login prompt).

1. Install the tool:

   ```bash
   uv tool install backseat-harness
   ```

2. Install caddy and write the caddyfile (caddy is installed with `winget` if it is missing; on Windows this must run from an elevated, Administrator shell):

   ```bash
   backseat-harness setup-caddy   # installs caddy, opens the site port, writes ~/.backseat/caddyfile
   ```

   <!-- TODO: the Linux branch (apt/dnf/pacman + ufw/firewalld) and the LaunchAgent daemon install are untested -->

3. Install and start the daemon (caddy is expected on `PATH`):

   ```bash
   backseat-harness daemon install
   ```

4. Open the local web page and sign in — `https://localhost:8443` (or the hostname you configured for Caddy).

## Upgrading

```bash
uv tool upgrade backseat-harness
backseat-harness daemon restart   # restart the daemon on the new version
```

## Web interface

The main workflow, in brief:

1. **Register a workspace** — add a folder (it gets a `.backseat/` marker) and open it.

   ![backseat-harness web interface: sign-in and workspaces page](https://raw.githubusercontent.com/portablestew/backseat-harness/main/docs/media/web-mainpage-example.png)

2. **Start a chat** — from the workspace web UI (New chat), your IDE, or the CLI of your choice. Discuss a plan for your idea interactively with the agent.

   ![Workspace page](https://raw.githubusercontent.com/portablestew/backseat-harness/main/docs/media/web-workspace-example.png)

3. **Generate a task from the chat** — the ✨ Task button distils the finished chat into task requirements (`goal.md`).

   ![Generate a task from the chat](https://raw.githubusercontent.com/portablestew/backseat-harness/main/docs/media/web-chat-generate-task.png)

4. **The task completes autonomously** — the daemon runs it to the goal: managed agent step, operator review, next step.

5. **Amend or takeover** — after a run, add a brief to guide/redirect it, or Takeover to drive the managed session yourself. On exit the task continues autonomously.

   ![Task page: checklist, Run and Takeover](https://raw.githubusercontent.com/portablestew/backseat-harness/main/docs/media/web-task-example.png)

For simpler requests, skip the chat: **add a task directly** with a name and goal, and let it run (fire-and-forget).

## Configuration

Two optional files under `~/.backseat/`:

**`config.toml`** — providers, models, timeouts, loop budgets, web port. Layered: `~/.backseat/config.toml`, then `.backseat/config.toml` in the workspace, then `.backseat/tasks/<slug>/config.toml`; higher layers win. See [`examples/.backseat/config.toml`](examples/.backseat/config.toml).

**`caddyfile`** — when present, the web interface is served over HTTPS with a login prompt (`auto` mode). A minimal file:

<!-- caddyfile-example:start -->
```
site.lan:8443 {
	tls internal
	basic_auth {
		alice <bcrypt hash>
	}
	reverse_proxy 127.0.0.1:8090
}
```
<!-- caddyfile-example:end -->

Replace `<bcrypt hash>` with `caddy hash-password`; the `reverse_proxy` port must match `[web] port` (default 8090). `backseat-harness setup-caddy` writes this file for you. The web app itself listens on `127.0.0.1` only.

Choose the serving mode with `--mode` on `web`, or `--web` on the daemon: `auto` (caddy when the caddyfile exists), `direct` (plain http only), `caddy` (caddyfile required), `none` (CLI only).

## CLI

Everything the web does is also a subcommand:

```bash
backseat-harness init <slug> [goal]        # scaffold a task lane
backseat-harness run <slug> "goal"         # create and drive a task
backseat-harness run <slug> "brief"        # resume with an instruction for this run
backseat-harness takeover <slug>           # drive the managed session yourself, then resume
backseat-harness generate-task <provider>:<conversation_id>   # task from a chat
backseat-harness status <slug>
backseat-harness list
```

The daemon runs ready lanes unattended, one at a time per workspace:

```bash
backseat-harness register-workspace /path/to/workspace
backseat-harness daemon install            # register and start (Task Scheduler / systemd / LaunchAgent)
backseat-harness daemon status|restart|stop
backseat-harness daemon install --uninstall
```

Daemon output goes to `~/.backseat/daemon.log`; `daemon run --web` serves the web interface from the same process.

## Development

```bash
uv sync
uv run ruff check .
uv run pytest
```

## License

MIT. See [LICENSE](LICENSE).
