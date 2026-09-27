# backseat-harness

Simple, single-task agent loops. _Buckle up, you're not at the wheel anymore._

backseat-harness drives an off-the-shelf CLI coding agent (opencode today,
Claude Code / kiro-cli later) one small step at a time toward a goal you write
once. After each managed turn, a **fresh, adversarial operator** reviews the
whole session — code, tests, transcript — against a durable checklist and
decides the single next prompt.

Because the managed loop is a plain CLI session underneath, a human can
`takeover` at any time. Low-friction takeover; the autonomous loop is never
a black box.

## How it works

```
your meta-prompt (goal)
   │
   ▼
┌─────────────────────────── loop (one task lane) ───────────────────────────┐
│  (no prompt queued) operator plans the checklist + writes prompt #1          │
│  prep next prompt                                                            │
│    → (if transcript > ~1MB) start a new generation seeded with a chat-fork   │
│  spawn managed agent, non-interactive, one shot                              │
│    → wait for process exit          ← turn is over                           │
│  fresh operator reviews: meta-prompt + checklist + chat-fork + outcome       │
│    → emits a JSON verdict on stdout                                          │
│  harness applies verdict: merge checklist, update binding, append ledger     │
│    → continue | done | ask_human | problem                                   │
└──────────────────────────────────────────────────────────────────────────┘
```

Key decisions (see `docs/workspace-contract.md` for the full contract):

- **Bare driver.** No tools are injected into the managed agent. All state and
  tooling live on the harness / operator side.
- **Turn end = process exit.** Any nonzero exit or timeout is treated uniformly
  and passed to the operator only as a hint. Managed turns have a hard
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
- **Verdict on stdout.** The operator returns a JSON envelope
  (`verdict`, `next_prompt`, `checklist_update`, `notes`). The harness is the
  only writer to disk.
- **Durable checklist.** A map (not an array) that agents can add to and revise
  but never erase: null-stripped, field-whitelisted, freely-settable status.
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

Permissions: the managed agent runs as opencode's default agent with no extra
flags (or under a named agent via `[harness].agent`). Anything it would have to
*ask* about can't be answered in a non-interactive run, so grant more via
`extra_args` if your task needs it (opencode's `--auto` approves everything not
explicitly denied, which it flags as dangerous for good reason). The harness
never assumes `--auto`.

The operator runs under a harness-written `backseat-operator` opencode agent
(`.backseat/agents/opencode/agents/backseat-operator.md`, rewritten at every
`run`). Its system prompt is the operator persona
(`src/backseat_harness/operator_persona.md`), and its permissions deny
everything except reads, search, read-only git, and any configured
`verify_commands`. It's loaded through `OPENCODE_CONFIG_DIR` on the operator
process only, so your own opencode config is untouched. Set
`[operator].agent` to use your own agent instead.

## Use as a CLI

```bash
# start a task from nothing: creates .backseat/tasks/<slug>/, then the operator
# plans the checklist and writes the first managed prompt
backseat-harness run hello-endpoint "Add a GET /hello endpoint returning JSON."

# resume later (the operator re-assesses and picks the next step; nothing else to pass)
backseat-harness run hello-endpoint

# optional: scaffold without running, or force the first managed prompt
backseat-harness init hello-endpoint "..."
backseat-harness run hello-endpoint -p "Start by locating the router."

# answer an ask_human pause by driving the session yourself, then auto-resume
backseat-harness takeover hello-endpoint

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
    operator_invoker=my_operator_invoker, # str prompt -> str final message
    first_prompt=None,                # None: the operator plans first
)
print(result.status, result.iterations)
```

The managed runner and system invoker are injected, so the loop is fully
deterministic and testable without a live CLI.

## Human takeover

When the operator hits something only you can settle — an ambiguous goal, a
product call, a credential — it returns the `ask_human` verdict and the loop
blocks the lane (`status` shows `blocked`, with the question as the reason). Answer it by
taking the wheel:

```bash
backseat-harness takeover hello-endpoint
```

This opens the managed session in the CLI's own interactive UI (for opencode,
the TUI at the current generation's session), attached to your terminal. Drive
the conversation as far as you need to remove the ambiguity, then quit. The
harness is blocked on that process; when it exits, the operator re-plans from
the session you just advanced and the loop continues. Because your input lands
as the managed agent's own conversation history, the operator sees it as
context on the next turn — no separate answer channel needed.

`--no-resume` hands off without running the loop afterward, for when you just
want to poke at the session. And the session is an ordinary CLI session, so you
can always resume it by hand instead:

```bash
opencode --session <native_session_id>      # native id is shown in `status`
```

The `<meta>… #n</meta>` marker at the top tells you which generation you landed
in. Each compaction opens a *new* session, so `status` shows the current
generation's id, not the whole chain — `binding.json` has the rest.

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
