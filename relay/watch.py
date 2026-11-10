#!/usr/bin/env python3
"""Long-poll watch slice for a Muse Telegram bot.

Used by the telegram-group-watch agent cron, which runs in a
supervisor-plus-watcher pattern: a cron firing every 10 minutes
checks the watcher heartbeat and either exits at once (watcher
healthy) or starts a new watcher term of about 60 minutes. The
watcher loops this script in slices (default 600s). Each slice
long-polls getUpdates (50s server-side waits) and:

- prints "RELEVANT <n>" plus one compact line per relevant update and
  exits as soon as a relevant batch has been queued,
- prints "NONE" when the slice elapsed with nothing relevant,
- prints "LOCKED" when another slice currently holds the watch lock
  (a previous watcher is still on duty). With --wait-lock <secs> the
  slice instead queues on the lock for up to that long and takes over
  as soon as the holder releases it (seamless shift handover); LOCKED
  is printed only after the wait budget is exhausted,
- prints "ERROR <description>" (exit 1) when the API call failed; the
  offset is not advanced in that case.

The slice also owns the watcher heartbeat (watcher-state.json): it is
refreshed on every poll, so the supervisor sees a watcher on duty for
the whole slice and the agent has no bookkeeping to do between slices.
Pass --new-term on the first slice of a takeover or a recovery to
start a new term (resets started_at).

Boundary rules shared with fetch.py: updates are appended to
queue.jsonl first and the offset in state.json is advanced only after
the append, so a crash between slices never loses a fetched update;
updates Telegram resends with an id below the stored offset are
skipped, and handled.txt on the agent side prevents duplicate replies.

Setup: place this file next to fetch.py (or in the relay directory
TG_RELAY_DIR) and run it from inside a Muse agent turn, because only
agent turns can use the connector credential. Bot identity comes from
fetch.py's local.json, as described there.
"""
import fcntl
import json
import os
import sys
import time

BASE = os.environ.get(
    "TG_RELAY_DIR", os.path.expanduser("~/workspace/telegram-relay"))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
if BASE not in sys.path:
    sys.path.insert(0, BASE)
import fetch as F  # noqa: E402

LOCK = os.path.join(BASE, "watch.lock")


def main() -> int:
    slice_secs = 600
    if "--slice" in sys.argv:
        slice_secs = int(sys.argv[sys.argv.index("--slice") + 1])
    wait_lock = 0
    if "--wait-lock" in sys.argv:
        wait_lock = int(sys.argv[sys.argv.index("--wait-lock") + 1])
    new_term = "--new-term" in sys.argv
    os.makedirs(BASE, exist_ok=True)
    lock_fh = open(LOCK, "w")
    wait_deadline = time.time() + wait_lock
    while True:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() >= wait_deadline:
                print("LOCKED")
                return 0
            time.sleep(min(5.0, max(0.1, wait_deadline - time.time())))
    # Holding the lock means this slice is the watcher on duty.
    F.beat(new_term=new_term)
    deadline = time.time() + slice_secs
    while True:
        F.beat()
        # Messages pulled earlier (by this or a previous shift) that are
        # still unanswered and unclaimed are handed out again first.
        due = F.due_pending()
        if due:
            F.claim([F.record_key(r) for r in due])
            print(f"RELEVANT {len(due)}")
            for r in due:
                print(F.digest(r, redelivered=True))
            return 0
        remaining = deadline - time.time()
        if remaining <= 0:
            print("NONE")
            return 0
        offset = F.load_offset()
        try:
            result = F.tg.call("getUpdates", {
                "offset": offset,
                "timeout": int(max(1, min(50, remaining))),
                "limit": 100,
                "allowed_updates": ["message", "edited_message", "my_chat_member"],
            }, http_timeout=int(max(10, min(70, remaining + 15))))
        except Exception as exc:  # network hiccup: report, don't crash
            print("ERROR " + str(exc)[:200])
            return 1
        if not result.get("ok"):
            print("ERROR " + str(result.get("description", result))[:200])
            return 1
        updates = [u for u in result.get("result", [])
                   if int(u.get("update_id", 0)) >= offset]
        if not updates:
            continue
        relevant = []
        max_id = offset - 1
        with open(F.QUEUE, "a", encoding="utf-8") as qf:
            for update in updates:
                record = F.compact(update)
                max_id = max(max_id, int(update.get("update_id", 0)))
                if F.is_dropped(record):
                    try:
                        with open(F.DROPPED, "a", encoding="utf-8") as df:
                            df.write(json.dumps(
                                record, ensure_ascii=False) + "\n")
                    except Exception:
                        pass
                    continue
                wake = F.should_wake(record)
                record["wake"] = wake
                record["pulled_at"] = int(time.time())
                qf.write(json.dumps(record, ensure_ascii=False) + "\n")
                if wake:
                    relevant.append(record)
                    F.mark_read(record)
                    F.signal_typing(record)
                    F.signal_watchdog(record)
        F.save_offset(max_id + 1)
        if relevant:
            F.claim([F.record_key(r) for r in relevant])
            F.beat()  # the agent gets the full grace period to answer
            print(f"RELEVANT {len(relevant)}")
            for r in relevant:
                print(F.digest(r))
            return 0


if __name__ == "__main__":
    sys.exit(main())
