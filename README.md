# backseat-harness

Simple, single-task agent loops. Buckle up.

backseat-harness drives an off-the-shelf CLI coding agent (opencode today,
Claude Code / kiro-cli later) one small step at a time toward a goal you write
once. After each managed turn, a **fresh, adversarial system agent** reviews the
whole session — code, tests, transcript — against a durable checklist and
decides the single next prompt.

The name has a double meaning: the system agent is the backseat driver that
never touches the wheel, and a harness is the seatbelt that keeps an autonomous
run safe. Because the managed session is a plain CLI session underneath, a human
can `--continue` it at any time. Low-friction takeover; the autonomous loop is
never a black box.

## How it works

```
your meta-prompt (goal)
   │
   ▼
┌─────────────────────────── loop (one task lane) ───────────────────────────┐
│  prep next prompt                                                            │
│    → (if transcript > ~1MB) start a new generation seeded with a chat-fork   │
│  spawn managed agent, non-interactive, one shot                              │
│    → wait for process exit          ← turn is over                           │
│  fresh system agent reviews: meta-prompt + checklist + chat-fork + outcome   │
│    → emits a JSON verdict on stdout                                          │
│  harness applies verdict: merge checklist, update binding, append ledger     │
│    → continue | done | ask_human | problem                                   │
└──────────────────────────────────────────────────────────────────────────┘
```

Key decisions (see `docs/workspace-contract.md` for the full contract):

- **Bare driver.** No tools are injected into the managed agent. All state and
  tooling live on the harness / system-agent side.
- **Turn end = process exit.** Any nonzero exit or timeout is treated uniformly
  and passed to the system agent only as a hint. Managed turns have a hard
  timeout (1h default) and run with non-interactive permissions so they bail
  rather than hang.
- **Marker-first identity.** Each generation's first user message begins with
  `<meta>START SESSION <root-uuid> #n</meta>`. The harness captures whatever
  native session id the CLI reports; it never mints `--session-id`. `binding.json`
  is a recoverable cache, not the source of truth — the marker is in the
  transcript, so a lost binding is reconstructible by searching for it.
- **Prompts go on stdin.** A compacted first turn carries a forked transcript far
  too large for argv (Windows caps a command line near 32k), so the prompt is
  piped to the managed CLI rather than passed as an argument.
- **Preemptive compaction.** When the transcript passes ~1MB (~250-300k tokens),
  the next turn starts a fresh session seeded with a middle-truncated chat-fork
  of the prior one (via [chat-mother-forker](https://pypi.org/project/chat-mother-forker/)),
  then the real prompt. `#n` increments; the root UUID stays stable.
- **History, not files.** Transcript size and fork rendering both come from
  chat-mother-forker, scoped to the managed CLI's own storage. The harness never
  stats a transcript file — opencode keeps sessions in SQLite, so there is no
  file to stat.
- **Verdict on stdout.** The system agent returns a JSON envelope
  (`verdict`, `next_prompt`, `checklist_update`, `notes`). The harness is the
  only writer to disk.
- **Durable checklist.** A map (not an array) that agents can add to and advance
  but never erase: null-stripped, field-whitelisted, forward-only status.
- **Library-first.** `run_task(...)` is the entry point; the CLI is a thin
  wrapper. Every path is injectable so an orchestrator can build on it.

## Install

```bash
uv sync            # dev environment (installs chat-mother-forker + dev tools)
# or
pip install -e .
```

Requires Python 3.10+ and the managed agent's own CLI — for v0.1 that is
[`opencode`](https://opencode.ai) — installed and authenticated on your PATH.
backseat-harness never stores provider keys; it uses the CLI's ambient auth.

One thing to decide before your first run: opencode blocks on permission
prompts, and a blocked turn just burns the timeout. Grant it whatever it needs
via `extra_args` in your config (opencode's own `--auto` approves everything not
explicitly denied, which it flags as dangerous for good reason). The harness
won't pick a permission posture for you.

## Use as a CLI

```bash
# scaffold a task lane under .backseat/tasks/<slug>/
backseat-harness init hello-endpoint -m "Add a GET /hello endpoint returning JSON."

# drive it
backseat-harness run hello-endpoint -p "Start by locating the router."

# check progress (add --format json for machine-readable output)
backseat-harness status hello-endpoint
backseat-harness list
```

Run everything against a specific workspace with `-C /path/to/workspace`.

## Use as a library

```python
from backseat_harness.config import load_config
from backseat_harness.loop import run_task
from backseat_harness.providers import get_provider
from backseat_harness.spawn import run_turn

config = load_config("/path/to/workspace")
provider = get_provider(config.provider)

result = run_task(
    slug="hello-endpoint",
    config=config,
    provider=provider,
    managed_runner=run_turn,          # or inject your own
    system_invoker=my_system_invoker, # str prompt -> str final message
    first_prompt="Start by locating the router.",
)
print(result.status, result.iterations)
```

The managed runner and system invoker are injected, so the loop is fully
deterministic and testable without a live CLI.

## Human takeover

The managed session is an ordinary CLI session. To step in, resume it by its
native id (shown in `status`):

```bash
opencode --session <native_session_id>      # TUI, same session
```

The `<meta>… #n</meta>` marker at the top tells you which generation you landed
in. Note that each compaction opens a *new* session, so `status` shows the
current generation's id, not the whole chain — `binding.json` has the rest.

## Scope

**v0.1 target:** opencode provider, marker + binding, checklist, verdict loop,
compaction-by-fork. Library + thin CLI.

**Deferred to later versions:** Claude Code and kiro-cli adapters (the Claude
adapter is written and contract-conformant in `providers/claude.py`, but is
unvalidated against a live install, so it is not registered yet), parallel lanes
/ git worktrees, a workspace-templates orchestrator, a checklist MCP tool,
mid-turn steering / hooks, and a human-takeover lock beyond dual-CLI semantics.

## Development

```bash
uv run ruff check .
uv run pytest
```

## License

MIT
