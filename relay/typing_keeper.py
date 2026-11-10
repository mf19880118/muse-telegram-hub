#!/usr/bin/env python3
"""Keep Telegram 'typing...' alive while the agent handles a message.

Telegram shows a typing chat action for ~5s, but agent turns take
minutes, so a single action sent when a message is pulled expires long
before the reply lands. This keeper re-sends sendChatAction typing
every few seconds until the message is marked handled (handled.txt in
the relay directory) or a time budget expires.

Usage: typing_keeper.py <chat_id> <chat_id:message_id>
       [--interval 4] [--max-secs 600] [--dry-run]

Started detached by fetch.py's signal_typing() when a relevant message
is pulled. Best-effort: never raises, logs start/stop to
typing_keeper.log. A second keeper for the same message exits at once
(pidfile in /tmp).

Setup: place this file in the relay directory (TG_RELAY_DIR, the same
directory fetch.py keeps its state in).
"""
import os
import sys
import time

BASE = os.environ.get(
    "TG_RELAY_DIR", os.path.expanduser("~/workspace/telegram-relay"))
HANDLED = os.path.join(BASE, "handled.txt")
LOG = os.path.join(BASE, "typing_keeper.log")

CONNECTOR_BIN = os.environ.get(
    "TG_CONNECTOR_BIN",
    os.path.expanduser("~/workspace/custom-connectors/custom.telegram-bot/bin"),
)
sys.path.insert(0, CONNECTOR_BIN)
import tg  # noqa: E402


def log(msg: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S %Z") + " " + msg + "\n")
    except Exception:
        pass


def is_handled(key: str) -> bool:
    try:
        with open(HANDLED, "r", encoding="utf-8") as fh:
            return key in {line.strip() for line in fh}
    except Exception:
        return False


def already_running(key: str) -> bool:
    pidfile = "/tmp/tg-typing-%s.pid" % key.replace(":", "_").replace("-", "m")
    try:
        with open(pidfile, "r", encoding="utf-8") as fh:
            pid = int(fh.read().strip())
        os.kill(pid, 0)
        return True
    except Exception:
        pass
    try:
        with open(pidfile, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except Exception:
        pass
    return False


def main() -> int:
    argv = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry_run = len(argv) != len(sys.argv) - 1
    if len(argv) < 2:
        print("usage: typing_keeper.py <chat_id> <chat_id:message_id> "
              "[--interval N] [--max-secs N] [--dry-run]", file=sys.stderr)
        return 2
    chat_id = int(argv[0])
    key = argv[1]
    interval = 4.0
    max_secs = 600.0
    i = 2
    while i < len(argv):
        if argv[i] == "--interval" and i + 1 < len(argv):
            interval = float(argv[i + 1])
            i += 2
        elif argv[i] == "--max-secs" and i + 1 < len(argv):
            max_secs = float(argv[i + 1])
            i += 2
        else:
            i += 1
    if already_running(key):
        log("skip %s keeper already running" % key)
        return 0
    log("start %s chat=%s" % (key, chat_id))
    deadline = time.time() + max_secs
    while time.time() < deadline:
        if is_handled(key):
            log("stop %s handled" % key)
            return 0
        if dry_run:
            print("DRY typing chat=%s key=%s" % (chat_id, key), flush=True)
        else:
            try:
                tg.call("sendChatAction",
                        {"chat_id": chat_id, "action": "typing"},
                        http_timeout=10)
            except Exception:
                pass
        end = time.time() + interval
        while time.time() < end:
            if is_handled(key):
                log("stop %s handled" % key)
                return 0
            time.sleep(0.5)
    log("stop %s timeout %.0fs" % (key, max_secs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
