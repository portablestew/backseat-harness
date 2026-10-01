"""Step 10a3 -- one live two-turn opencode run, two model slots.

The opencode half of plan row 8 (``docs/2-plan.md:252``). Same shape as the
claude slice (``tests/manual/live_claude_slots.py``, commit 37fe3d4): real
``loop.run_task``, real ``OpencodeProvider``, real ``spawn.run_turn``, exactly
two managed turns, the slot flipped by the scripted operator's
``checklist_update``. Run in a throwaway temp workspace so the live turns cannot
touch a working tree.

**Do not inherit claude's confidence here.** Research 1-research-model-switch.md
:79-87 established that opencode's ``--format json`` stream carries no model
field at all, so there is nothing in-stream to read. This driver therefore rests
its verdict on a *disk* surface research did not check: ``message.data`` in
``~/.local/share/opencode/opencode.db`` carries a per-message ``modelID``,
written by opencode for each assistant message. Per-message means per-turn --
it can show turn 1 and turn 2 differing, which a single session-level column
could not. The agent's own reply text is captured too, but only as corroboration:
on a resume, turn 2 can read turn 1's reply out of history, so a turn-2 reply
naming turn 1's model is transcript echo, not independent proof.

The DB is opened read-only (``mode=ro``) and only after the run finishes.

    uv run python tests/manual/live_opencode_slots.py [raw_capture_path]
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path

from backseat_harness import storage
from backseat_harness.config import Config
from backseat_harness.loop import run_task
from backseat_harness.providers import get_provider
from backseat_harness.providers.base import Provider, SpawnSpec
from backseat_harness.spawn import resolve_argv, run_turn

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW = (
    REPO_ROOT
    / ".backseat"
    / "tasks"
    / "per-step-models"
    / "docs"
    / "10a-live-opencode-raw.txt"
)
DB_PATH = Path(os.path.expanduser(r"~\.local\share\opencode\opencode.db"))

# Two slots, two different models, same provider prefix so the only thing that
# changes between turns is the model. Both are in `opencode models` output under
# the authenticated "OpenCode Go" credential; deepseek-v4-pro is the one
# research section 1 used successfully. Whether the account actually serves both
# is what this run establishes.
SLOT_A = "opencode-go/deepseek-v4-pro"
SLOT_B = "opencode-go/gpt-6-luna"

FIRST_PROMPT = (
    "State only the exact model id you are running as, in one short line "
    "(for example: deepseek-v4-pro). Then on a second line reply with the "
    "single word: TURN-OK. Do not use any tools and do not read or write files."
)
SECOND_PROMPT = (
    "State only the exact model id you are running as, in one short line "
    "(for example: deepseek-v4-pro). Then on a second line reply with the "
    "single word: TURN-OK-AGAIN. Do not use any tools and do not read or write "
    "files."
)


def _banner(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


class Turn:
    """One managed turn: the argv the loop built, and the stream it got back."""

    def __init__(self, index: int, spec: SpawnSpec, result, provider: Provider):
        self.index = index
        self.spec = spec
        self.result = result
        self.argv = resolve_argv(provider.build_command(spec))
        self.reply = provider.extract_text(result.stdout)
        self.message_id: str | None = None
        self.disk_model: str | None = None
        self.disk_provider: str | None = None
        self.disk_text: str | None = None

    @property
    def model_flag(self) -> str | None:
        argv = self.argv
        return argv[argv.index("--model") + 1] if "--model" in argv else None

    @property
    def session_flag(self) -> str | None:
        """opencode resumes with --session, not --resume (providers/opencode.py:12)."""
        argv = self.argv
        return argv[argv.index("--session") + 1] if "--session" in argv else None

    @property
    def bare_model(self) -> str:
        """The slot value with the provider/ prefix the CLI already implies."""
        flag = self.model_flag or ""
        return flag.split("/", 1)[1] if "/" in flag else flag


def _recording_runner(turns: list[Turn]):
    def runner(provider, spec):
        result = run_turn(provider, spec)
        turns.append(Turn(len(turns) + 1, spec, result, provider))
        return result

    return runner


def _scripted_operator(calls: list[int]):
    """Exactly two answers: continue (flipping the slot), then done."""

    def operator(_prompt: str) -> str:
        calls.append(1)
        n = len(calls)
        if n == 1:
            return json.dumps(
                {
                    "verdict": "continue",
                    "notes": "turn 1 done on slot A; switching the step to slot B",
                    "next_prompt": SECOND_PROMPT,
                    "checklist_update": {
                        "steps": {"s1": {"status": "active", "model": "b"}}
                    },
                }
            )
        return json.dumps(
            {
                "verdict": "done",
                "notes": "turn 2 done on slot B",
                "checklist_update": {"steps": {"s1": {"status": "done"}}},
            }
        )

    return operator


def read_disk_surface(session_id: str) -> list[dict]:
    """Per-message modelID for one session, oldest first. Read-only.

    ``message.data`` is opencode's own per-message record, so this is per-turn
    state rather than a session-level last-write-wins field. Joined to the text
    part so each message can be tied back to the turn that produced it.
    """
    con = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select m.rowid, m.id, m.data from message m "
            "where m.session_id = ? and json_extract(m.data, '$.role') = 'assistant' "
            "order by m.time_created, m.rowid",
            (session_id,),
        ).fetchall()
        out = []
        for rowid, msg_id, data in rows:
            record = json.loads(data)
            texts = [
                json.loads(p).get("text")
                for (p,) in con.execute(
                    "select data from part where message_id = ? order by time_created",
                    (msg_id,),
                )
                if json.loads(p).get("type") == "text"
            ]
            out.append(
                {
                    "rowid": rowid,
                    "message_id": msg_id,
                    "modelID": record.get("modelID"),
                    "providerID": record.get("providerID"),
                    "text": "\n".join(t for t in texts if isinstance(t, str)),
                }
            )
        return out
    finally:
        con.close()


def _match_turn(disk: list[dict], turn: Turn) -> dict | None:
    """Tie a disk message to a turn by the sentinel word in its text.

    The prompts asked for TURN-OK / TURN-OK-AGAIN, so the mapping is by content
    rather than by row order -- no assumption that the nth message is the nth
    turn.
    """
    sentinel = "TURN-OK-AGAIN" if turn.index == 2 else "TURN-OK"
    for record in disk:
        text = record.get("text") or ""
        if sentinel in text and not (
            turn.index == 1 and "TURN-OK-AGAIN" in text
        ):
            return record
    return None


def main(argv: list[str]) -> int:
    raw_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_RAW

    ws = Path(tempfile.mkdtemp(prefix="live-opencode-slots-"))
    config = replace(
        Config(workspace_root=ws),
        provider="opencode",
        models={"a": SLOT_A, "b": SLOT_B},
    )
    paths = config.task_paths("live-opencode")
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.meta.write_text(
        "Prove a per-step model slot changes the serving opencode model.\n",
        encoding="utf-8",
    )
    paths.checklist.write_text(
        json.dumps(
            {"steps": {"s1": {"status": "active", "model": "a"}}, "remarks": []}
        ),
        encoding="utf-8",
    )

    _banner("setup")
    print(f"workspace_root (disposable temp) : {ws}")
    print(f"[models]                         : {config.models}")
    print("checklist step s1                : model='a' -> operator flips it to 'b'")
    print("operator invoker                 : scripted, 2 answers (continue, done)")
    print(f"disk surface (read-only)         : {DB_PATH}")

    turns: list[Turn] = []
    calls: list[int] = []
    err = io.StringIO()
    with redirect_stderr(err):
        result = run_task(
            "live-opencode",
            config,
            get_provider("opencode"),
            _recording_runner(turns),
            _scripted_operator(calls),
            first_prompt=FIRST_PROMPT,
        )

    print(f"managed turns spawned            : {len(turns)}  (operator calls: {len(calls)})")

    _banner("(a) full argv the harness built, per turn")
    for turn in turns:
        print(f"--- turn {turn.index}")
        print(f"  argv                 : {turn.argv}")
        print(f"  --model              : {turn.model_flag}")
        print(f"  --session (resume)   : {turn.session_flag}")
        print(f"  spec.cwd             : {turn.spec.cwd}")
        print(f"  exit_code            : {turn.result.exit_code}")
        print(f"  native_session_id    : {turn.result.native_session_id}")
        print(f"  duration_s           : {turn.result.duration_s:.2f}")
        print(f"  stderr               : {turn.result.stderr.strip() or '(empty)'}")
    sids = [t.result.native_session_id for t in turns]
    print()
    print(f"  session ids          : {sids}")
    print(f"  same id on both turns: {len(set(sids)) == 1 and sids[0] is not None}")

    _banner("(b) reply text from the --format json stream, per turn")
    for turn in turns:
        print(f"--- turn {turn.index}  (--model {turn.model_flag})")
        for line in turn.reply.splitlines():
            print(f"  {line}")

    _banner("(c) disk surface: message.data.modelID, per turn")
    session_id = sids[0] if sids else None
    disk = read_disk_surface(session_id) if session_id else []
    print(f"session queried                  : {session_id}")
    print(f"assistant messages in session    : {len(disk)}")
    for record in disk:
        print(
            f"  rowid={record['rowid']}  modelID={record['modelID']}  "
            f"providerID={record['providerID']}"
        )
        for line in (record["text"] or "").splitlines():
            print(f"      {line}")
    print()
    for turn in turns:
        record = _match_turn(disk, turn)
        if record:
            turn.message_id = record["message_id"]
            turn.disk_model = record["modelID"]
            turn.disk_provider = record["providerID"]
            turn.disk_text = record["text"]
        print(
            f"  turn {turn.index}: --model {turn.model_flag}  ->  "
            f"disk modelID {turn.disk_model}   (matched: {bool(record)})"
        )

    _banner("(d) verdict")
    if all(t.disk_model for t in turns):
        a, b = turns[0], turns[1]
        print(f"  turn 1 slot 'a' -> --model {a.model_flag}")
        print(f"          disk message.data.modelID = {a.disk_model}")
        print(f"  turn 2 slot 'b' -> --model {b.model_flag}")
        print(f"          disk message.data.modelID = {b.disk_model}")
        switched = a.disk_model != b.disk_model
        print()
        print(
            f"  DID THE SLOT CHANGE THE SERVING MODEL? {'YES' if switched else 'NO'}"
        )
        print(
            "  evidence: message.data.modelID in opencode.db -- opencode's own "
            "per-message record, read-only"
        )
        print(
            "  NOT from the reply text: on a resume, turn 2 can read turn 1's "
            "reply, so a reply naming the old model is transcript echo"
        )
        print(
            f"  reply-text corroboration: turn 1 mentions {a.disk_model!r}: "
            f"{a.disk_model in (a.reply or '')}   turn 2 mentions {b.disk_model!r}: "
            f"{b.disk_model in (b.reply or '')}"
        )
    else:
        print("  COULD NOT DECIDE -- a turn has no modelID on disk; see (c).")
    print()
    print(f"  run status           : {result.status.value}  ({result.reason})")
    print(f"  iterations           : {result.iterations}")
    print(f"  final_verdict        : {result.final_verdict}")
    print(f"  state.status on disk : {storage.load_state(paths, 'live-opencode').status.value}")

    _banner("stderr emitted by the run")
    for line in err.getvalue().splitlines():
        print(f"  {line}")
    if not err.getvalue().strip():
        print("  (empty)")

    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with raw_path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(f"# session id: {session_id}\n")
        fh.write(f"# db: {DB_PATH}\n")
        for turn in turns:
            fh.write(f"########## TURN {turn.index} ##########\n")
            fh.write(f"# argv: {json.dumps(turn.argv)}\n")
            fh.write(f"# --model: {turn.model_flag}\n")
            fh.write(f"# exit_code: {turn.result.exit_code}\n")
            fh.write(f"# native_session_id: {turn.result.native_session_id}\n")
            fh.write(f"# disk message_id: {turn.message_id}\n")
            fh.write(f"# disk modelID: {turn.disk_model}\n")
            fh.write(f"# disk providerID: {turn.disk_provider}\n")
            fh.write(f"# reply: {json.dumps(turn.reply)}\n")
            fh.write("########## STDOUT ##########\n")
            fh.write(turn.result.stdout)
            if not turn.result.stdout.endswith("\n"):
                fh.write("\n")
            fh.write("########## STDERR ##########\n")
            fh.write(turn.result.stderr or "(empty)\n")
            if turn.result.stderr and not turn.result.stderr.endswith("\n"):
                fh.write("\n")
        fh.write("########## DISK SURFACE (message.data) ##########\n")
        for record in disk:
            fh.write(
                f"rowid={record['rowid']} message_id={record['message_id']} "
                f"modelID={record['modelID']} providerID={record['providerID']}\n"
            )
            fh.write(f"text: {json.dumps(record['text'])}\n")
    print()
    print(f"raw capture written to: {raw_path}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
