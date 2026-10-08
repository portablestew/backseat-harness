# backseat-harness

Simple, single-task agent loops. _Buckle up, you're not at the wheel anymore._

backseat-harness drives an off-the-shelf CLI coding agent (opencode, Claude
Code, or kiro-cli) one small step at a time toward a goal you write
once. After each managed turn, a **fresh, adversarial operator** reviews the
whole session — code, tests, transcript — against a durable checklist and
decides the single next prompt.

Because the managed loop is a plain CLI session underneath, a human can
`takeover` at any time. Low-friction takeover; the autonomous loop is never
a black box.

## How it works

```
your goal-prompt (the goal)
   │
   ▼
┌─────────────────────────── loop (one task lane) ───────────────────────────┐
│  (no prompt queued) operator plans the checklist + writes prompt #1          │
│  prep next prompt                                                            │
│    → (if transcript > ~1MB) start a new generation seeded with a chat-fork   │
│  spawn managed agent, non-interactive, one shot                              │
│    → wait for process exit          ← turn is over                           │
│  fresh operator reviews: goal-prompt + checklist + chat-fork + outcome       │
│    → emits a JSON verdict on stdout                                          │
│  harness applies verdict: merge checklist, update binding, append ledger     │
│    → continue | done | ask_human | problem                                   │
└──────────────────────────────────────────────────────────────────────────┘
```

Key decisions:

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
  file to stat. That store belongs to the *live* CLI, so reads are retried
  (3 attempts, 1s apart) and a store that stays unreadable warns and degrades
  rather than ending the run.
- **Verdict on stdout.** The operator returns a JSON envelope
  (`verdict`, `next_prompt`, `checklist_update`, `notes`) and the harness applies
  it. The harness is the only writer to a lane's state, with one exception:
  `docs/`, which the managed agent writes at the operator's request. It never
  writes any `config.toml` at any layer.
- **Durable checklist.** A map (not an array) that agents can add to and revise
  but never erase: null-stripped, field-whitelisted, freely-settable status.
- **Library-first.** `run_task(...)` is the entry point; the CLI is a thin
  wrapper. Every path is injectable so an orchestrator can build on it.

## On-disk layout

A workspace is any directory holding `.backseat/`. Each task gets one lane, and
lanes are independent: separate session chain, checklist, binding, and ledger.

```
.backseat/
  config.toml                # harness defaults, provider prefs, budgets
  tasks/
    <slug>/
      goal.md                # the goal-prompt, written once by `init`
      config.toml            # optional per-task config (highest layer)
      checklist.json         # durable progress map
      binding.json           # root_uuid -> generation chain (recoverable cache)
      ledger.jsonl           # append-only, one line per operator decision
      state.json             # status, iteration, generation
      transcript.md          # the managed session, rewritten each turn
      logs/                  # one file per provider call
      docs/                  # handoff docs, written by the managed agent
```

Every file is plain JSON / TOML / Markdown and carries a `"v"` schema version, so
a parent workflow can read a lane's state without importing the loop. Unknown
top-level sections are ignored rather than rejected. `TaskPaths` in
`config.py` resolves all of these paths; `storage.py` reads and writes the JSON.

Four invariants are load-bearing and enforced in code rather than described here:

| invariant | why | enforced by |
|---|---|---|
| the ledger line lands **last** | its final line marks the last fully-committed step, so it anchors crash recovery | `loop._persist` |
| the root UUID is redacted from `transcript.md` and every log | an operator that quotes a file must not make its own stored session match the task's marker search | `marker.redact`, `logs.redact_file` |
| identity is the marker, not `binding.json` | a lost binding is reconstructible by searching transcripts for `<meta>START SESSION` | `marker.prepend_marker` |
| unknown step fields are dropped, not merged | a step is a map the operator revises but never erases, so a stray key must not become state | `checklist.STEP_FIELDS` |

## Install

```bash
uv sync            # dev environment (installs chat-mother-forker + dev tools)
# or
pip install -e .
```

Requires Python 3.10+ and the managed agent's own CLI — one of
[`opencode`](https://opencode.ai), Claude Code (`claude`), or kiro-cli
(`kiro-cli`) — installed and authenticated on your PATH.
backseat-harness never stores provider keys; it uses the CLI's ambient auth.

Permissions: every CLI runs auto-approved (opencode `--auto`, claude `--permission-mode auto`,
kiro `--trust-all-tools`). A non-interactive process has nobody to answer a prompt,
so deny (exit 0, silent failure) is worse than approve. The harness picks approve.
Narrow it with `[harness].agent` / `[operator].agent` (repo-level agent definition),
not by removing `--auto`: a single allowlist versioned with the repo beats a CLI
switch turned wide open. `extra_args` is for adjustments on top.

The operator runs the provider's default agent, or your own via
`[operator].agent`. The persona (`src/backseat_harness/operator_persona.md`) is
prepended to every operator prompt, so one source of truth serves all providers
and your own agent still receives it. Granting the operator an auto-approve
mode means it *can* write; the persona is the only thing telling it not to.

## Use as a CLI

```bash
# start a task from nothing: creates .backseat/tasks/<slug>/, then the operator
# plans the checklist and writes the first managed prompt
backseat-harness run hello-endpoint "Add a GET /hello endpoint returning JSON."

# resume later (the operator re-assesses and picks the next step; nothing else to pass)
backseat-harness run hello-endpoint

# add your own words to a run: the second argument on a lane that already has a
# goal is a brief from you, not a new goal. It rides every operator prompt of
# the run, outranks the goal where they conflict, and ends with the run.
backseat-harness run hello-endpoint "Skip the caching step for now."

# answer an ask_human pause. This is the normal way: the operator asked, the
# operator is told, and the question is reprinted above your answer.
backseat-harness run hello-endpoint "Use SQLite; no external service."

# or drive the session yourself, then auto-resume
backseat-harness takeover hello-endpoint

# optional: scaffold without running, or force the first managed prompt
backseat-harness init hello-endpoint "..."
backseat-harness run hello-endpoint -p "Start by locating the router."

# check progress (add --format json for machine-readable output)
backseat-harness status hello-endpoint
backseat-harness list
```

A lane that is `blocked` or `complete` refuses a bare `run`: it prints the
pending question in full and tells you the brief-argument form to resume it with.
Repeating the goal-prompt verbatim is ignored rather than injected, so a wrapper
that forwards its config cannot spam the operator with your own brief.

Run everything against a specific workspace with `-C /path/to/workspace`.

### Configuration

Config resolves in three optional layers, higher layers overriding lower ones:
`~/.backseat/config.toml` (user), `.backseat/config.toml` (workspace), and
`.backseat/tasks/<slug>/config.toml` (task — loaded by `init`/`run`/`takeover`/
`status`; `list` reads only the user and workspace layers). Arrays (`extra_args`,
`verify_commands`) are **replaced wholesale**, never concatenated. A task layer
may override anything, `[operator]` included. Every layer is read-only to the
harness; code defaults fill anything no layer sets. A commented example lives in
[`examples/.backseat/config.toml`](examples/.backseat/config.toml).

**Separate ceilings.** `[harness].turn_timeout_s` (managed turn, default 3600) and
`[operator].timeout_s` (operator verdict, default 1800) are independent. `operator_retry_budget` (default 3) counts invocations per verdict — one call plus two re-asks; `1` disables re-asking. It multiplies `timeout_s`, so raise the timeout for heavy validation and lower the budget to bound the worst case.

`[models]` is the per-step model vocabulary, and the one table that merges by key
rather than being replaced wholesale: a task layer rebinds one slot and keeps the
rest. A key is a slot by virtue of existing — there is no fixed vocabulary — and
its value is an opaque model id passed verbatim as `--model`. `default` is the
only reserved name, and is what a step resolves to when it names no slot. A
checklist step routes itself by setting its `model` field to one of those keys.
`[operator].model` is not a slot: it profiles the operator, which is never routed
per step.

## Use as a library

```python
from backseat_harness.config import load_config
from backseat_harness.loop import run_task
from backseat_harness.providers import get_provider
from backseat_harness.spawn import run_turn

config = load_config("/path/to/workspace")                     # user + workspace layers
config = load_config("/path/to/workspace", "hello-endpoint")   # + the task layer
provider = get_provider(config.provider)

result = run_task(
    slug="hello-endpoint",
    config=config,
    provider=provider,
    managed_runner=run_turn,          # or inject your own
    operator_invoker=my_operator_invoker, # str prompt -> str final message
    first_prompt=None,                # None: the operator plans first
    owner_brief=None,                  # or the task owner's words for this run
)
print(result.status, result.iterations)
```

The managed runner and system invoker are injected, so the loop is fully
deterministic and testable without a live CLI.

## Human takeover and briefs

`ask_human` blocks the lane. Answer with a brief (the operator's words ride
every prompt of the run, outrank the goal, and end with the run; the run's plan
line records them in `ledger.jsonl`):

```bash
backseat-harness run <slug> "Use SQLite."
```

`takeover <slug>` opens the managed session in the CLI's interactive UI; when
you quit, the harness re-plans and continues. A brief never reaches the managed
agent; a takeover never reaches the operator. They are not interchangeable.

## Scope

**v0.1 target:** three registered providers — opencode, Claude Code
(`claude`), and kiro-cli (`kiro`), each live-validated end to end
(one-shot, resume, operator) — plus marker + binding, checklist, verdict loop,
compaction-by-fork. Library + thin CLI.

**Deferred to later versions:** parallel lanes
/ git worktrees, a workspace-templates orchestrator, a checklist MCP tool,
mid-turn steering / hooks, and a human-takeover lock beyond dual-CLI semantics.

## Development

```bash
uv run ruff check .
uv run pytest
```

## License

MIT
