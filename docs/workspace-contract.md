# The `.backseat/` workspace contract

This is the stable on-disk seam between backseat-harness and anything that wants
to orchestrate it (a workspace-templates tool, CI, a human running `tail -f`).
The harness is the **only writer** to these files. Every file is plain JSON /
TOML / Markdown so a parent workflow can read state without importing the loop.

All files carry a `"v"` schema version (currently `1`). Unknown top-level
sections are ignored, not rejected.

```
.backseat/
  config.toml                # harness defaults, provider prefs, budgets
  agents/                    # (optional) system-agent profile assets
  tasks/
    <task-slug>/             # one lane = one meta-prompt goal
      meta.md                # the meta-prompt (free text)
      checklist.json         # durable, append-only-in-spirit progress map
      binding.json           # root_uuid -> generation chain (recoverable cache)
      ledger.jsonl           # append-only, one line per completed iteration
      state.json             # mutable status/iteration/generation
```

A task slug must be filesystem-safe. Lanes are independent: separate session
chain, checklist, binding, and ledger. Multi-lane parallelism is out of scope
for v0.1, but `task_dir` is a parameter from day one so it stays cheap to add.

## config.toml

```toml
[harness]
provider = "opencode"          # managed-agent CLI (v0.1: opencode)
extra_args = []                # flags per managed turn; where you grant perms
turn_timeout_s = 3600          # hard per-turn ceiling
compact_threshold_bytes = 1000000
max_iterations = 50
max_wall_clock_s = 86400
max_generations = 20

[system_agent]
provider = "opencode"          # reviewer CLI (may differ / be stronger)
model = "..."                  # optional
agent = "..."                  # optional, maps to --agent / profile
extra_args = []                # optional, as above
verify_commands = []           # optional adversarial checks (tests, typecheck)
```

`extra_args` has no default on purpose. A managed turn runs non-interactively, so
it needs enough permission to finish without stopping at an approval prompt — but
which permissions those are is the workspace owner's call, not the harness's.

## checklist.json

A **map** of steps keyed by id (never an array — arrays are how agents erase
history). The system agent emits a `checklist_update` delta each turn; the
harness validates and merges it. Rules:

- **Additive / no deletes.** `null` values are stripped before merge.
- **Field whitelist** per step: `title`, `status`, `notes`, `evidence`,
  `updated_at`. Unknown fields are dropped.
- **Forward-only status:** `pending -> active -> done` (`blocked` is a sidecar
  at the `active` rank). A regress requires an explicit `"reopen": true` on that
  step in the delta.
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

## state.json

```json
{
  "v": 1,
  "slug": "hello-endpoint",
  "status": "active",        // active | paused | done | failed
  "iteration": 7,
  "generation": 2,
  "updated_at": 1750003600.0
}
```

## ledger.jsonl

Append-only, one JSON object per completed iteration. It is written **last** in
each iteration (after checklist + binding + state), so its final line marks the
last fully-committed iteration — the anchor for crash recovery.

```json
{"at": 1750003600.0, "iteration": 7, "generation": 2, "verdict": "continue", "exit_code": 0, "timed_out": false, "duration_s": 42.1, "notes": "wired the route; test next"}
```

## Verdict envelope (system agent -> harness, on stdout)

Not stored as a file; the harness parses the system agent's final message and
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
