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

The installed daemon also serves the web interface on the configured port (default 8090), so a separate `backseat-harness web` on that port will not start.

## Web interface

```bash
backseat-harness web          # http://127.0.0.1:8090, localhost only; --host and --port change it
```

For HTTPS and a login prompt in front of it, see [Caddy mode](#caddy-mode-https--login).

- **Workspaces:** register a folder and see its tasks.
- **Tasks:** create a task from a name and goal, then review its status, checklist, and transcript.
- **Runs:** start a task run from its page, with an optional brief.
- **Chats:** browse and search provider sessions, and fork one.
- **Shell:** open a terminal in the browser at a workspace.

### Caddy mode (HTTPS + login)

Caddy mode puts [Caddy](https://caddyserver.com) in front of the web interface. Caddy serves the site over HTTPS and asks for a username and password. The web app keeps listening only on `127.0.0.1`. Caddy must be installed and on `PATH`.

With the default `auto` mode, caddy is used when `~/.backseat/caddyfile` exists. A minimal file:

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

Replace `<bcrypt hash>` with the output of `caddy hash-password`, and keep `alice` or change it to the user name you want.

Two things the example does that you may not expect:

- **Port 80.** Caddy also opens a listener on TCP 80 that redirects plain-HTTP requests to HTTPS, even though the site is on another port. If something else holds port 80, caddy exits at start with `listen tcp :80: ... Only one usage of each socket address`, and the daemon or `web` logs `caddy exited rc=1`. The pre-start check does not look at port 80, so this shows up after start. If you cannot bind 80, or do not want the redirect listener, add a global options block at the top of the caddyfile:

  ```
  {
  	auto_https disable_redirects
  }
  ```

  The site is then served over HTTPS on its own port only. Do not use `auto_https off` for this: it turns HTTPS off for the site.

- **Trust prompt on Windows.** `tls internal` makes caddy install its local root certificate into the OS trust store the first time it runs. On Windows that shows a "Security Warning" dialog, and caddy waits for it. Trusting the root lets every program on this machine trust caddy's certificates. To keep caddy from installing it, add `skip_install_trust` to the same global block:

  ```
  {
  	auto_https disable_redirects
  	skip_install_trust
  }
  ```

  With this, the browser shows the certificate warning described under [Certificate](#caddy-mode-https--login) instead, and nothing is trusted. The two options above are the ones this setup uses when you do not want to bind 80 or trust the root. Both were checked with `caddy validate` against the example.

On Windows, `scripts\setup-caddy-windows.ps1` does these steps interactively from an elevated PowerShell: it installs caddy if needed, adds a firewall rule for TCP 8443, asks for the user name and password, writes the caddyfile, and validates it. It is opt-in, and it refuses to run unless elevated. The daemon itself never writes a caddyfile.

- **Port:** the `reverse_proxy` port must equal `[web] port` in your config (default 8090). Nothing checks this. A mismatch shows as 502 errors in the browser.
- **Site address:** write `host:port`, `https://host`, bare `host` (port 443), `http://host` (port 80), or `:port` (answers as `localhost` and `127.0.0.1`). Separate several addresses with spaces or commas. Wildcards, IPv6 literals, paths, and other schemes are refused with exit 2. The host name must resolve to this machine from the browser, for example through a hosts-file entry.
- **Checks before start:** the daemon or `web` runs `caddy validate` on the file and checks that the site ports are free. Any failure exits 2 with the reason.
- **Certificate:** `tls internal` signs with caddy's local certificate authority, so the browser shows a certificate warning. Trusting that authority is optional and not part of the setup steps. If you want to trust it, run `caddy trust` yourself while caddy is running. Without `skip_install_trust`, caddy also tries to install that root on first run (see the trust prompt above).

Choose the mode with the `--web` flag on the daemon or with `--mode` on `web`:

```bash
backseat-harness daemon run --web            # same as --web=auto
backseat-harness daemon run --web=direct     # plain uvicorn on the web port, no caddy
backseat-harness daemon run --web=caddy      # caddy is required; exit 2 without the caddyfile
backseat-harness daemon run --web=none       # no web interface, same as omitting --web
backseat-harness web --mode auto|direct|caddy   # default auto
```

- **`auto`** (bare `--web`, and the default for `web`) uses caddy when the caddyfile exists and direct mode when it does not. If the caddyfile exists but caddy is not on `PATH`, `auto` exits 2 and does not fall back to direct mode. A missing caddy never serves without the login.
- **If caddy dies:** under the daemon, the daemon logs `caddy exited rc=N` to `~/.backseat/daemon.log` and keeps running. The web app stays on `127.0.0.1:8090`, but the HTTPS site is gone until the next restart. Standalone `web` stops the server and exits 1.
- **Caddy's output:** under the daemon it goes to `~/.backseat/caddy.log`. Standalone `web` shows it on the terminal.

**Happy path:**

1. Install caddy and check that `caddy version` works in a new terminal.
2. Run `backseat-harness daemon install`. The installed daemon uses the `PATH` in effect at install time, so if you install caddy later, run `daemon install` again to pick it up.
3. Write `~/.backseat/caddyfile` as above.
4. Run `backseat-harness daemon restart`. The daemon checks for the caddyfile each time it starts, so it picks up the new file.
5. Open `https://site.lan:8443` (or your site address). The browser will show a certificate warning until you trust caddy's local CA, which is optional.

## Configuration

Config is layered: `~/.backseat/config.toml`, then `.backseat/config.toml` in the workspace, then `.backseat/tasks/<slug>/config.toml` for a task. Higher layers win. See [`examples/.backseat/config.toml`](examples/.backseat/config.toml).

## Development

```bash
uv run ruff check .
uv run pytest
```

## License

MIT. See [LICENSE](LICENSE).
