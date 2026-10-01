# The `.backseat/` workspace contract

This is the stable on-disk seam between backseat-harness and anything that wants
to orchestrate it (a workspace-templates tool, CI, a human running `tail -f`).
The harness is the **only writer** to these files — with one exception:
`config.toml`, at every layer, is read-only input the harness never writes. Every
file is plain JSON / TOML / Markdown so a parent workflow can read state without
importing the loop.

All files carry a `"v"` schema version (currently `1`). Unknown top-level
sections are ignored, not rejected.

```
.backseat/
  config.toml                # harness defaults, provider prefs, budgets
  tasks/
    <task-slug>/             # one lane = one meta-prompt goal
      meta.md                # the meta-prompt (free text)
      config.toml            # optional per-task overrides (highest config layer)
      checklist.json         # durable, append-only-in-spirit progress map
      binding.json           # root_uuid -> generation chain (recoverable cache)
      ledger.jsonl           # append-only, one line per completed iteration
      state.json             # mutable status/iteration/generation
      transcript.md          # full managed session, redacted, rewritten each turn
      docs/                  # managed-agent handoff docs (research, plan, reviews)
```

A task slug must be filesystem-safe. Lanes are independent: separate session
chain, checklist, binding, and ledger. Multi-lane parallelism is out of scope
for v0.1, but `task_dir` is a parameter from day one so it stays cheap to add.

## config.toml

Config is layered, lowest precedence first:

1. `~/.backseat/config.toml` (user)
2. `<workspace>/.backseat/config.toml` (workspace)
3. `<workspace>/.backseat/tasks/<slug>/config.toml` (task; loaded when a command
   names a slug)

Each layer overrides the ones below it. Arrays (`extra_args`, `verify_commands`)
are **replaced wholesale**, never concatenated. A task layer may override
anything, `[operator]` included. Every layer is read-only to the harness.

```toml
[harness]
provider = "opencode"          # managed-agent CLI (opencode | claude | kiro)
model = "..."                  # optional: managed agent's model (--model)
agent = "..."                  # optional: named provider agent for managed turns
extra_args = []                # flags per managed turn; where you grant perms
turn_timeout_s = 3600          # hard per-turn ceiling
compact_threshold_bytes = 1000000
max_iterations = 50
max_wall_clock_s = 86400
max_generations = 20
problem_repeat_cap = 3        # consecutive identical-prompt PROBLEMs before
                              # BLOCKED as stuck; 0 = disabled (max_iterations)

[models]                     # per-step model slots: slot-name = "model-id"
fast = "..."                 # a step's `model` field names one of these keys
default = "..."              # reserved: the binding for a step naming no slot

[operator]
provider = "opencode"          # operator CLI (may differ / be stronger)
model = "..."                  # optional
agent = "..."                  # optional: a named agent instead of the provider default
extra_args = []                # optional, as above
verify_commands = []           # optional: exact commands the operator may run itself
```

Every layer is optional; code defaults fill anything no layer sets.

`[models]` is the per-step model vocabulary, and it is the one table that does
not replace wholesale: a task layer rebinds one slot name and keeps the rest.
Scalars and arrays are overridden per field.

A key is a slot by virtue of existing — there is no fixed vocabulary — and its
value is an opaque model id, passed verbatim to the managed CLI as
`--model <id>`. `default` is the single reserved name, and it is what a step
resolves to when it names no slot (or names the literal `"default"`, which is
the same thing). A checklist step routes itself by setting `model` to one of
these keys; see `checklist.json` below.

A pair whose name or id is not a non-empty string is dropped at load, with a
stderr warning naming the pair. That is deliberate, and it is the one rule here
that is never silent: an empty id would otherwise be a slot the operator is
offered in its prompt vocabulary which can produce no `--model` at all, so the
step would quietly run the CLI's own default under a name that promised
otherwise.

Every turn runs auto-approved: a non-interactive process cannot answer a prompt,
so the harness approves rather than block (opencode/claude) or silently deny
every write (kiro). The grant is in `build_command`, so it applies to the
managed turn and the operator alike. Narrow it with `[harness].agent` /
`[operator].agent` (a repo-defined agent) rather than by withholding the grant;
`extra_args` is for per-workspace adjustments on top.

The operator runs the provider's default agent, or `[operator].agent` when set,
under the same auto-approve default as the managed turn. Narrow it with
`[operator].agent`; `[operator].model` and `[operator].extra_args` are the
remaining knobs.

The persona is prepended to every operator prompt, so all providers share one
source of truth and your own `[operator].agent` still receives it.

The persona reaches the operator by one of two paths: opencode's written
agent file carries it as the system prompt (a workspace's named
`[operator].agent` owns its own persona), while claude and kiro — which
write no profile file — receive the persona prepended to every operator
prompt by the invoker.

The managed side has no harness-written profile; `[harness].agent` only
forwards `--agent` so a turn can run under a named agent definition of yours.

## session markers

Every managed generation's first user message starts with
`<meta>START SESSION <root_uuid> #n</meta>` (see `binding.json` below). Every
operator prompt starts with the UUID-free `<meta>OPERATOR SESSION <slug>
<phase></meta>` (`phase` is `plan` or `review`) so stored operator sessions can
be recognized and filtered without ever matching the task's root-UUID marker
search — the operator must not be discoverable as a managed generation.

## checklist.json

A **map** of steps keyed by id (never an array — arrays are how agents erase
history). The operator emits a `checklist_update` delta each turn; the
harness validates and merges it. Rules:

- **Additive / no deletes.** `null` values are stripped before merge.
- **Field whitelist** per step: `title`, `status`, `notes`, `evidence`,
  `updated_at`, `model`. Unknown fields are dropped. `model` is a pass-through
  slot *name*, not a model id: it is resolved against `[models]` every turn
  (never at merge), and stored here with no semantics of its own.
- **Free status.** A step's `status` may be set to any of `pending`, `active`,
  `blocked`, or `done` at any time -- there is no forward-only ordering, so the
  operator can move a step back to redo it. Invalid values are ignored.
- **`remarks`** is a freeform append-only log channel.

```json
{
  "steps": {
    "route": { "title": "Add GET /hello", "status": "done", "evidence": "turn 4 tests green" }
  },
  "remarks": ["blocked briefly on a flaky import in turn 3"]
}
```

A `checklist_update` delta looks the same shape; only the whitelisted subset is
applied:

```json
{
  "steps": { "route": { "status": "done", "evidence": "..." } },
  "remarks": ["..."]
}
```

## binding.json

Cache mapping the stable root UUID to its chain of generations. **Identity is
the marker**, not this file — if lost, the chain is reconstructible by searching
for `<meta>START SESSION <root_uuid> #n</meta>` in the transcripts. A new
generation is appended on compaction; `native_id` is captured from the provider,
never minted by the harness.

```json
{
  "v": 1,
  "root_uuid": "d2ea8f1f-1674-4fec-b4e1-8235d2963fe6",
  "provider": "opencode",
  "generations": [
    { "n": 1, "native_id": "ses_abc", "transcript_bytes": 812345, "mtime": 1750000000.0 },
    { "n": 2, "native_id": "ses_def", "transcript_bytes": 40211,  "mtime": 1750003600.0 }
  ]
}
```

`transcript_bytes` is whatever chat-mother-forker reports as the session's stored
size, refreshed after each turn. The unit is provider-dependent — summed SQLite
row bytes for opencode, file bytes for a JSONL-backed CLI — so compare it against
`compact_threshold_bytes`, never across providers.

Every chat-history read is retried (3 attempts, 1s apart) and then degraded, never
fatal: the store belongs to the live managed CLI, which writes it while the harness
reads it. Exhausting the attempts warns on stderr and yields "nothing there" — the
size stands, the fork is empty, capture is skipped — so one contended read cannot
end a lane. A misconfigured `history_provider` is the exception: it raises
`HistoryError` immediately.

## state.json

```json
{
  "v": 1,
  "slug": "hello-endpoint",
  "status": "continue",      // continue | blocked (needs human) | complete
  "iteration": 7,
  "generation": 2,
  "updated_at": 1750003600.0,
  "reason": ""               // why the lane is terminal (question / budget guard)
}
```

## ledger.jsonl

Append-only, one JSON object per operator decision. It is written **last**
(after checklist + binding + state), so its final line marks the last
fully-committed step — the anchor for crash recovery. `phase` is `"plan"` for a
decision made with no managed turn (a new or resumed run; `iteration` is the
number of turns so far) and `"review"` after a managed turn. `next_prompt` is
the exact prompt the operator queued for the next turn (`null` when the
verdict queues none).

```json
{"at": 1750003600.0, "phase": "review", "iteration": 7, "generation": 2, "verdict": "continue", "exit_code": 0, "timed_out": false, "duration_s": 42.1, "notes": "wired the route; test next", "next_prompt": "Add a test for GET /hello ..."}
```

## transcript.md

The full managed session, rendered by chat-mother-forker's `load_chat` and
written **by the harness** after each managed turn, and again before a plan-phase
operator call (a resume), so a re-planning operator gets the same record a
reviewing one does — the file on disk would otherwise predate whatever advanced
the session since, such as a takeover. Unlike the compaction fork
(middle-truncated) and unlike `docs/` (managed-agent written), this is the
complete, untruncated record — tool-call results included — so the operator can
inspect what actually happened rather than an elided summary. One file,
overwritten each turn; compaction forks a fresh session per generation, so it
tracks the current generation's work. The task's root UUID is redacted (the
generation ordinal stays), so if the operator quotes the file its own stored
session can't hijack the task's marker search. Best-effort: if history is
unavailable, capture is skipped and the run continues — including on a fresh
task, where the just-minted root UUID has no session to capture, so the file is
neither written nor named in the prompt.

## docs/

Handoff docs written by the managed agent at the operator's request: research
notes, the plan, independent-review findings, captured evidence (e.g. full test
output). The harness creates the directory and lists its files in every
operator prompt, but never writes inside it. It is the only place under
`.backseat/` the managed agent is asked to write; that is a prompt-level
convention, not an enforced permission.

## Verdict envelope (operator -> harness, on stdout)

Not stored as a file; the harness parses the operator's final message and
applies it. Tolerates prose around a single JSON object.

```json
{
  "v": 1,
  "verdict": "continue",     // continue | done | ask_human | problem
  "next_prompt": "the exact next prompt for the managed agent, or null",
  "checklist_update": { "steps": { "route": { "status": "done" } }, "remarks": [] },
  "notes": "one line for the ledger"
}
```

`ask_human` blocks the lane (`status` = `blocked`, `notes` = the question). The
human answers with `backseat-harness takeover <slug>`, which opens the managed
session in the provider's interactive UI, waits for them to advance it, then
re-plans from the advanced session on exit. There is no stored answer channel:
the human's input becomes the managed agent's own conversation history, which
the operator reads on the next turn.
