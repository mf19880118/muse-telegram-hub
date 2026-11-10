#!/usr/bin/env python3
"""Answer and close Telegram messages; the only writer of handled.txt.

fetch.py / watch.py hand out messages that woke the agent (one JSON
line each, with a "key" of chat_id:message_id). Each one stays pending
until it is closed here, and is handed out again if nobody closes it,
so a message can no longer be lost between two watcher shifts.

  inbox.py reply --key <key> --text "..."      reply to the message and
                                               close it (closed only if
                                               Telegram accepted the send)
  inbox.py reply --key <key> --text-file f     same, text read from a file
  inbox.py reply --key <key> --text "..." --interim
                                               reply but keep it open: use
                                               for "on it" before a long
                                               task, then reply again
                                               without --interim when done
  inbox.py skip --key <key> --reason "..."     close without replying;
                                               refused in hub mode for
                                               messages from a human
  inbox.py list                                show what is still open
  inbox.py stats                               how long recent replies
                                               took, and where the time
                                               went (waiting to be pulled
                                               vs. pulled to replied)

Never append to handled.txt by hand. Place this file next to fetch.py
in the relay directory and run it inside a Muse agent turn.
"""
import argparse
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

SKIPPED = os.path.join(BASE, "skipped.log")
LATENCY = os.path.join(BASE, "latency.log")
CHUNK = 4000  # Telegram caps a message at 4096 characters


def parse_key(key: str):
    chat, _, mid = key.rpartition(":")
    return int(chat), int(mid)


def log_latency(key: str, record: dict, interim: bool) -> None:
    """One line per reply sent: how long the sender waited, and where.

    wait_secs is message sent -> pulled by a watcher (nobody was polling,
    or the watcher was busy); handle_secs is pulled -> this reply (the
    agent's own turn). Best-effort, never raises.
    """
    try:
        now = time.time()
        sent = record.get("date")
        pulled = record.get("pulled_at")
        entry = {"key": key, "at": int(now), "interim": bool(interim),
                 "from": record.get("from_username") or record.get("from_name"),
                 "from_is_bot": bool(record.get("from_is_bot"))}
        if record.get("kind") == "edited_message":
            sent = None  # its date is when the original was sent
        if isinstance(sent, (int, float)):
            entry["total_secs"] = int(now - sent)
            if isinstance(pulled, (int, float)):
                entry["wait_secs"] = max(0, int(pulled - sent))
        if isinstance(pulled, (int, float)):
            entry["handle_secs"] = max(0, int(now - pulled))
        with open(LATENCY, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _median(values):
    values = sorted(values)
    if not values:
        return None
    return values[len(values) // 2]


def cmd_stats(last: int) -> int:
    """First reply per message, most recent `last` messages."""
    first = {}
    try:
        with open(LATENCY, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if isinstance(entry, dict) and entry.get("key"):
                    first.setdefault(entry["key"], entry)
    except Exception:
        pass
    rows = list(first.values())[-last:]
    print("STATS %d" % len(rows))
    if not rows:
        return 0
    parts = []
    for name, label in (("total_secs", "sent->first reply"),
                        ("wait_secs", "sent->pulled"),
                        ("handle_secs", "pulled->first reply")):
        mid = _median([r[name] for r in rows if name in r])
        if mid is not None:
            parts.append("%s %ds" % (label, mid))
    print("MEDIAN " + " | ".join(parts))
    for row in rows[-10:]:
        print(json.dumps(row, ensure_ascii=False))
    return 0


def cmd_list() -> int:
    now = time.time()
    claims = F.load_claims()
    pending = F.pending_records(now)
    print("OPEN %d" % len(pending))
    for rec in pending:
        line = json.loads(F.digest(rec, context=False, max_text=300))
        expires = claims.get(line["key"])
        left = int(expires - now) if isinstance(expires, (int, float)) else 0
        line["claimed_for_secs"] = max(0, left)
        print(json.dumps(line, ensure_ascii=False))
    return 0


def cmd_reply(key: str, text: str, interim: bool) -> int:
    try:
        chat_id, message_id = parse_key(key)
    except ValueError:
        print("ERROR bad key, expected chat_id:message_id")
        return 2
    text = (text or "").strip()
    if not text:
        print("ERROR empty text")
        return 2
    record = F.find_record(key) or {}
    chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]
    sent_ids = []
    for index, chunk in enumerate(chunks):
        params = {"chat_id": chat_id, "text": chunk}
        if index == 0:
            params["reply_parameters"] = {
                "message_id": message_id,
                "allow_sending_without_reply": True}
        if record.get("thread_id"):
            params["message_thread_id"] = record["thread_id"]
        try:
            result = F.tg.call("sendMessage", params)
        except Exception as exc:
            result = {"ok": False, "description": str(exc)}
        if not result.get("ok"):
            print("ERROR send failed, message stays open: "
                  + str(result.get("description", result))[:200])
            return 1
        sent_ids.append((result.get("result") or {}).get("message_id"))
    log_latency(key, record, interim)
    if interim:
        F.claim([key], ttl=F.WORKING_TTL)
        print("SENT interim %s (still open) %s" % (key, json.dumps(sent_ids)))
    else:
        F.mark_handled(key)
        print("DONE %s %s" % (key, json.dumps(sent_ids)))
    return 0


def cmd_skip(key: str, reason: str) -> int:
    try:
        parse_key(key)
    except ValueError:
        print("ERROR bad key, expected chat_id:message_id")
        return 2
    record = F.find_record(key)
    if (F.HUB_MODE and record and record.get("wake")
            and not record.get("from_is_bot")):
        print("REFUSED %s is from a person and was addressed to you: "
              "answer it with `inbox.py reply`" % key)
        return 2
    F.mark_handled(key)
    try:
        with open(SKIPPED, "a", encoding="utf-8") as fh:
            fh.write("%s %s %s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S %Z"), key, reason or ""))
    except Exception:
        pass
    print("SKIPPED %s" % key)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram inbox")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p_stats = sub.add_parser("stats")
    p_stats.add_argument("--last", type=int, default=20)
    p_reply = sub.add_parser("reply")
    p_reply.add_argument("--key", required=True)
    group = p_reply.add_mutually_exclusive_group(required=True)
    group.add_argument("--text")
    group.add_argument("--text-file")
    p_reply.add_argument("--interim", action="store_true")
    p_skip = sub.add_parser("skip")
    p_skip.add_argument("--key", required=True)
    p_skip.add_argument("--reason", default="")
    args = parser.parse_args()
    if args.cmd == "list":
        return cmd_list()
    if args.cmd == "stats":
        return cmd_stats(args.last)
    if args.cmd == "reply":
        text = args.text
        if args.text_file:
            with open(args.text_file, "r", encoding="utf-8") as fh:
                text = fh.read()
        return cmd_reply(args.key, text, args.interim)
    return cmd_skip(args.key, args.reason)


if __name__ == "__main__":
    sys.exit(main())
