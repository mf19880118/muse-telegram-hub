#!/usr/bin/env python3
"""Watch one message and hint in chat if handling stalls.

Started detached by fetch.py's signal_watchdog() alongside
typing_keeper.py whenever a message is pulled that will probably get
a reply. The agent handling the message can stall for a long time —
most confusingly when one of its tool calls is waiting on a user
approval prompt that only exists in the host app: the agent's turn
is frozen, so from the chat it looks like the bot read the message,
typed for a while, then vanished. This watchdog is the out-of-band
observer for exactly that case: it polls handled.txt, and if the
message is still unhandled after --threshold seconds it posts ONE
hint into the same chat (replying to the stalled message) saying it
is still being worked on and that an approval prompt in the host app
may be waiting. It then keeps watching and exits as soon as the
message is marked handled, or when --max-secs expires.

It deliberately does not claim to know WHY handling stalled — it
cannot see the approval system; a genuinely long task trips the same
hint, so the wording stays conditional. Pure script: no agent turn,
no tokens.

Usage: stuck_watchdog.py <chat_id> <chat_id:message_id>
       [--threshold 300] [--interval 15] [--max-secs 7200] [--dry-run]

Best-effort like typing_keeper.py: never raises, logs start/stop and
the hint to stuck_watchdog.log, and a second watchdog for the same
message exits at once (pidfile in /tmp).

Setup: place this file in the relay directory (TG_RELAY_DIR, the
same directory fetch.py keeps its state in).
"""
import os
import sys
import time

BASE = os.environ.get(
    "TG_RELAY_DIR", os.path.expanduser("~/workspace/telegram-relay"))
HANDLED = os.path.join(BASE, "handled.txt")
LOG = os.path.join(BASE, "stuck_watchdog.log")

CONNECTOR_BIN = os.environ.get(
    "TG_CONNECTOR_BIN",
    os.path.expanduser("~/workspace/custom-connectors/custom.telegram-bot/bin"),
)
sys.path.insert(0, CONNECTOR_BIN)
import tg  # noqa: E402

HINT = ("这条处理得有点久了，我还在处理。"
        "如果 Muse 里有确认卡在等你，去点一下就会继续。")


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
    pidfile = "/tmp/tg-stuck-%s.pid" % key.replace(":", "_").replace("-", "m")
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


def wait_slice(key: str, secs: float) -> bool:
    """Sleep up to secs, returning True early if the message is handled."""
    end = time.time() + secs
    while time.time() < end:
        if is_handled(key):
            return True
        time.sleep(min(1.0, max(0.05, end - time.time())))
    return is_handled(key)


def main() -> int:
    argv = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry_run = len(argv) != len(sys.argv) - 1
    if len(argv) < 2:
        print("usage: stuck_watchdog.py <chat_id> <chat_id:message_id> "
              "[--threshold N] [--interval N] [--max-secs N] [--dry-run]",
              file=sys.stderr)
        return 2
    chat_id = int(argv[0])
    key = argv[1]
    threshold = 300.0
    interval = 15.0
    max_secs = 7200.0
    i = 2
    while i < len(argv):
        if argv[i] == "--threshold" and i + 1 < len(argv):
            threshold = float(argv[i + 1])
            i += 2
        elif argv[i] == "--interval" and i + 1 < len(argv):
            interval = float(argv[i + 1])
            i += 2
        elif argv[i] == "--max-secs" and i + 1 < len(argv):
            max_secs = float(argv[i + 1])
            i += 2
        else:
            i += 1
    if already_running(key):
        log("skip %s watchdog already running" % key)
        return 0
    try:
        message_id = int(key.rsplit(":", 1)[1])
    except Exception:
        message_id = None
    log("start %s chat=%s threshold=%.0fs" % (key, chat_id, threshold))
    started = time.time()
    deadline = started + max_secs
    hinted = False
    while time.time() < deadline:
        if not hinted:
            remain = started + threshold - time.time()
            if remain > 0:
                if wait_slice(key, min(remain, interval)):
                    log("stop %s handled" % key)
                    return 0
                continue
            # Threshold reached, still unhandled: post the one hint.
            if dry_run:
                print("DRY hint chat=%s key=%s" % (chat_id, key), flush=True)
                log("hint %s (dry-run)" % key)
            else:
                try:
                    params = {"chat_id": chat_id, "text": HINT}
                    if message_id:
                        params["reply_to_message_id"] = message_id
                    tg.call("sendMessage", params, http_timeout=15)
                    log("hint %s sent" % key)
                except Exception as exc:
                    log("hint %s FAILED %s" % (key, str(exc)[:120]))
            hinted = True
        if wait_slice(key, interval):
            log("stop %s handled (after hint)" % key)
            return 0
    log("stop %s timeout %.0fs hinted=%s" % (key, max_secs, hinted))
    return 0


if __name__ == "__main__":
    sys.exit(main())
