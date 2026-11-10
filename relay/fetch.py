#!/usr/bin/env python3
"""Fetch new Telegram updates into a local queue file.

Designed to be run from inside a Muse agent turn (a scheduled watch
task, for example), because only agent turns can use the connector
credential. Appends compact records to queue.jsonl, persists the
getUpdates offset in state.json, and prints a digest. Each fresh
relevant message gets a 👀 reaction at once (read receipt) and, when a
reply is plausible, a typing indicator kept alive by a detached
typing_keeper.py until the message is marked handled, plus a detached
stuck_watchdog.py that posts one hint into the chat if the message is
still unhandled after a few minutes (the classic case is the agent
being frozen on a user approval prompt that only exists in the host
app, so the chat would otherwise see the bot read, type, then vanish):

- "NONE" when nothing relevant arrived
- "RELEVANT <n>" plus one compact line per relevant update
- "ERROR <description>" (exit 1) when the API call failed; the offset
  is not advanced in that case

Relevant means: any update in a group/supergroup chat, any private
message to the bot, any my_chat_member change (bot added/removed),
any message from another bot, and anything mentioning the bot.

Private chats can optionally be restricted to the owner: set
"owner_ids" in local.json (or TG_OWNER_IDS) to the Telegram user
id(s) allowed to talk to the bot privately. Private messages from
anyone else are then dropped silently — no 👀 reaction, no typing
indicator, no queue entry; they are appended to dropped.log in the
relay directory instead, so the owner can review them later. When
owner_ids is not configured, every private message counts as
relevant (the original behaviour).

Group chats can likewise be restricted with "group_ids" in
local.json (or TG_GROUP_IDS): messages from groups that are not
listed are dropped the same silent way. my_chat_member events are
never dropped, so the watch still notices (and can report) when the
bot is added to a new group — that is also how the owner learns the
new group's id to allowlist it. When group_ids is not configured,
every group the bot belongs to counts (the original behaviour).

Setup: tell the scripts who your bot is in a local.json file placed
in the relay directory (TG_RELAY_DIR, where queue.jsonl lives), e.g.
{"bot_username": "your_bot_username", "bot_id": 123456789,
"bot_name_keywords": ["your", "bot", "nicknames"],
"owner_ids": [123456789], "group_ids": [-123456789]}, with the
values from `tg.py get-me`. That file is local-only and never
committed; the same keys can also come from TG_BOT_USERNAME /
TG_BOT_ID / TG_BOT_NAME_KEYWORDS / TG_OWNER_IDS / TG_GROUP_IDS
(comma-separated) environment variables, which take precedence.
Point TG_CONNECTOR_BIN at the folder holding tg.py if you placed it
somewhere else.
"""
import json
import os
import re
import sys
import time

CONNECTOR_BIN = os.environ.get(
    "TG_CONNECTOR_BIN",
    os.path.expanduser("~/workspace/custom-connectors/custom.telegram-bot/bin"),
)
sys.path.insert(0, CONNECTOR_BIN)
import tg  # noqa: E402

BASE = os.environ.get(
    "TG_RELAY_DIR", os.path.expanduser("~/workspace/telegram-relay"))
STATE = os.path.join(BASE, "state.json")
QUEUE = os.path.join(BASE, "queue.jsonl")
DROPPED = os.path.join(BASE, "dropped.log")


def load_local_config() -> dict:
    """Bot identity for this deployment; never committed to the repo.

    Precedence: TG_BOT_USERNAME / TG_BOT_ID / TG_BOT_NAME_KEYWORDS
    environment variables, then local.json in the relay directory,
    then local.json next to this script. Missing values fall back to
    the placeholders below, which match nothing.
    """
    cfg = {}
    for path in (os.path.join(BASE, "local.json"),
                 os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "local.json")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cfg.update(json.load(fh))
        except Exception:
            pass
    if os.environ.get("TG_BOT_USERNAME"):
        cfg["bot_username"] = os.environ["TG_BOT_USERNAME"]
    if os.environ.get("TG_BOT_ID"):
        try:
            cfg["bot_id"] = int(os.environ["TG_BOT_ID"])
        except ValueError:
            pass
    if os.environ.get("TG_BOT_NAME_KEYWORDS"):
        cfg["bot_name_keywords"] = [
            w.strip() for w in
            os.environ["TG_BOT_NAME_KEYWORDS"].split(",") if w.strip()]
    if os.environ.get("TG_OWNER_IDS"):
        ids = []
        for w in os.environ["TG_OWNER_IDS"].split(","):
            try:
                ids.append(int(w.strip()))
            except ValueError:
                pass
        if ids:
            cfg["owner_ids"] = ids
    if os.environ.get("TG_GROUP_IDS"):
        ids = []
        for w in os.environ["TG_GROUP_IDS"].split(","):
            try:
                ids.append(int(w.strip()))
            except ValueError:
                pass
        if ids:
            cfg["group_ids"] = ids
    if os.environ.get("TG_HUB_MODE"):
        cfg["hub_mode"] = os.environ["TG_HUB_MODE"].strip().lower() in (
            "1", "true", "yes", "on")
    return cfg


_CONFIG = load_local_config()
BOT_USERNAME = _CONFIG.get("bot_username", "your_bot_username")
BOT_ID = int(_CONFIG.get("bot_id", 0) or 0)

# Optional: words that count as naming the bot in group text (its
# display name, for example). A group message from another bot that
# mentions the bot by @username or by one of these words also gets the
# typing indicator.
BOT_NAME_KEYWORDS = tuple(_CONFIG.get("bot_name_keywords", ()))

# Optional: Telegram user ids allowed to talk to the bot in private
# chats (normally just the owner). Empty = no restriction. When set,
# private messages from anyone else are dropped silently; see
# is_owner_dropped.
OWNER_IDS = set()
for _x in _CONFIG.get("owner_ids", ()):
    try:
        OWNER_IDS.add(int(_x))
    except (TypeError, ValueError):
        pass

# Optional: group/supergroup chat ids the bot should watch. Empty =
# no restriction. When set, messages from other groups are dropped
# silently; see is_group_dropped.
GROUP_IDS = set()
for _x in _CONFIG.get("group_ids", ()):
    try:
        GROUP_IDS.add(int(_x))
    except (TypeError, ValueError):
        pass

# --- Hub mode: several agents sharing one group -----------------------
# Off by default (original behaviour: every group message is relevant).
# With "hub_mode": true in local.json, a group message wakes this agent
# only when it is addressed to it:
#   - from a human: @bot_username, a reply to one of this bot's
#     messages, one of bot_name_keywords, or one of all_keywords
#     (a broadcast to every agent);
#   - from another bot: @bot_username or a reply to one of this bot's
#     messages, and only fresh messages (edits are ignored). Bots
#     cannot broadcast and name keywords do not count for them, so one
#     agent merely talking about another does not wake it.
# When owner_ids is also set, group messages from humans who are not
# owners are dropped silently, like non-owner private messages.
# Everything not addressed still lands in queue.jsonl as context.
#
# Loop guard: at most b2b_limit bot-originated wake-ups per chat in any
# b2b_window_secs window. Beyond that the message is queued but does
# not wake the agent, it is logged to throttled.log, and one notice is
# posted in the chat per window. A human message is never throttled,
# so the owner can always restart a stalled exchange.
HUB_MODE = bool(_CONFIG.get("hub_mode", False))
ALL_KEYWORDS = tuple(
    str(w).lower() for w in _CONFIG.get("all_keywords", ("@all", "@所有人"))
    if str(w).strip())
try:
    B2B_LIMIT = int(_CONFIG.get("b2b_limit", 8))
except (TypeError, ValueError):
    B2B_LIMIT = 8
try:
    B2B_WINDOW_SECS = int(_CONFIG.get("b2b_window_secs", 600)) or 600
except (TypeError, ValueError):
    B2B_WINDOW_SECS = 600
B2B_STATE = os.path.join(BASE, "b2b_window.json")

# --- Script-owned inbox ------------------------------------------------
# Which messages still owe a reply is tracked here, not in the agent's
# head, so nothing is lost when one watcher shift hands over to the
# next. Every queue record carries "wake": true/false. A wake record
# stays pending until its key (chat_id:message_id) is in handled.txt,
# which inbox.py writes only after a reply was actually sent.
# When a pending message is reported to the agent it is claimed for
# claim_ttl_secs; if it is still pending when the claim runs out it is
# reported again (marked "redelivered"), to whichever watcher is on
# duty by then. An interim reply extends the claim to working_ttl_secs.
HANDLED = os.path.join(BASE, "handled.txt")
CLAIMS = os.path.join(BASE, "claims.json")


def _cfg_int(name: str, default: int) -> int:
    try:
        return int(_CONFIG.get(name, default))
    except (TypeError, ValueError):
        return default


CLAIM_TTL = _cfg_int("claim_ttl_secs", 600)
WORKING_TTL = _cfg_int("working_ttl_secs", 3600)
PENDING_MAX_AGE = _cfg_int("pending_max_age_secs", 86400)
THROTTLED = os.path.join(BASE, "throttled.log")
THROTTLE_NOTICE = ("⏸ 机器人之间来回太密，我先暂停接其他 agent 的消息。"
                   "需要继续的话，请主人直接 @ 我。")

# Usernames are case-insensitive, and @foo_bot must not match @foo_bot2.
_MENTION_RE = re.compile(
    "@" + re.escape(BOT_USERNAME) + r"(?![A-Za-z0-9_])", re.IGNORECASE)


def load_offset() -> int:
    try:
        with open(STATE, "r", encoding="utf-8") as fh:
            return int(json.load(fh).get("offset", 0))
    except Exception:
        return 0


def save_offset(offset: int) -> None:
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"offset": offset}, fh)
    os.replace(tmp, STATE)


def compact(update: dict) -> dict:
    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    record = {
        "update_id": update.get("update_id"),
        "kind": "message" if "message" in update else
                "edited_message" if "edited_message" in update else
                "my_chat_member" if "my_chat_member" in update else "other",
        "message_id": msg.get("message_id"),
        "date": msg.get("date"),
        "chat_id": chat.get("id"),
        "chat_type": chat.get("type"),
        "chat_title": chat.get("title"),
        "thread_id": msg.get("message_thread_id"),
        "from_id": sender.get("id"),
        "from_name": sender.get("first_name"),
        "from_username": sender.get("username"),
        "from_is_bot": sender.get("is_bot", False),
        "text": msg.get("text") or msg.get("caption"),
        "reply_to_message_id": (msg.get("reply_to_message") or {}).get("message_id"),
        "reply_to_from_id": ((msg.get("reply_to_message") or {})
                             .get("from") or {}).get("id"),
        "new_chat_members": [m.get("username") for m in msg.get("new_chat_members", [])],
    }
    if "my_chat_member" in update:
        mcm = update["my_chat_member"]
        record["chat_id"] = (mcm.get("chat") or {}).get("id")
        record["chat_type"] = (mcm.get("chat") or {}).get("type")
        record["chat_title"] = (mcm.get("chat") or {}).get("title")
        record["text"] = "my_chat_member: " + json.dumps(
            mcm.get("new_chat_member", {}), ensure_ascii=False)[:200]
    return record


def is_owner_dropped(record: dict) -> bool:
    """Private chat from a non-owner while owner_ids is configured.

    Such messages are dropped silently: no 👀 reaction, no typing
    indicator, no queue entry (they land in dropped.log instead).
    Group chats and my_chat_member events are never dropped here.
    """
    if not OWNER_IDS:
        return False
    if record.get("from_id") == BOT_ID:
        return False
    if record.get("kind") == "my_chat_member":
        return False
    if record.get("chat_type") != "private":
        return False
    return record.get("from_id") not in OWNER_IDS


def is_group_dropped(record: dict) -> bool:
    """Group message from a group not in group_ids, when configured.

    Dropped just as silently as a non-owner private message.
    my_chat_member events are never dropped: the watch must still
    notice when the bot is added to a new group (and learn its id).
    Private chats are governed by is_owner_dropped instead.
    """
    if not GROUP_IDS:
        return False
    if record.get("from_id") == BOT_ID:
        return False
    if record.get("kind") == "my_chat_member":
        return False
    if record.get("chat_type") not in ("group", "supergroup"):
        return False
    return record.get("chat_id") not in GROUP_IDS


def is_hub_stranger_dropped(record: dict) -> bool:
    """Hub mode: group message from a human who is not an owner.

    Only when both hub_mode and owner_ids are configured. Bots are not
    dropped here (they are the other agents); whether a bot message
    wakes this agent is decided by is_addressed.
    """
    if not (HUB_MODE and OWNER_IDS):
        return False
    if record.get("from_id") == BOT_ID:
        return False
    if record.get("kind") == "my_chat_member":
        return False
    if record.get("chat_type") not in ("group", "supergroup"):
        return False
    if record.get("from_is_bot"):
        return False
    return record.get("from_id") not in OWNER_IDS


def is_dropped(record: dict) -> bool:
    return (is_owner_dropped(record) or is_group_dropped(record)
            or is_hub_stranger_dropped(record))


def is_addressed(record: dict) -> bool:
    """Hub mode: is this group message addressed to this bot?"""
    text = record.get("text") or ""
    mentioned = bool(_MENTION_RE.search(text))
    replied = bool(BOT_ID) and record.get("reply_to_from_id") == BOT_ID
    if record.get("from_is_bot"):
        if record.get("kind") != "message":
            return False
        return mentioned or replied
    if mentioned or replied:
        return True
    low = text.lower()
    if any(str(w).lower() in low for w in BOT_NAME_KEYWORDS if str(w).strip()):
        return True
    return any(w in low for w in ALL_KEYWORDS)


def _throttle_notice(chat_id) -> None:
    try:
        tg.call("sendMessage",
                {"chat_id": chat_id, "text": THROTTLE_NOTICE},
                http_timeout=15)
    except Exception:
        pass


def b2b_throttled(record: dict, now: float = None) -> bool:
    """Loop guard for bot-originated wake-ups in hub mode.

    Counts wake-ups caused by other bots per chat in a sliding window
    and returns True once the limit is reached. State lives in
    b2b_window.json in the relay directory. Call it only for records
    that would otherwise wake the agent, once per record.
    """
    if not (HUB_MODE and B2B_LIMIT > 0 and record.get("from_is_bot")
            and record.get("chat_type") in ("group", "supergroup")):
        return False
    now = time.time() if now is None else now
    key = str(record.get("chat_id"))
    try:
        with open(B2B_STATE, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        if not isinstance(state, dict):
            state = {}
    except Exception:
        state = {}
    stamps = [t for t in state.get(key, [])
              if isinstance(t, (int, float)) and 0 <= now - t < B2B_WINDOW_SECS]
    throttled = len(stamps) >= B2B_LIMIT
    notices = state.get("_notices")
    if not isinstance(notices, dict):
        notices = {}
    send_notice = False
    if throttled:
        last = notices.get(key, 0)
        if not isinstance(last, (int, float)) or now - last >= B2B_WINDOW_SECS:
            notices[key] = now
            send_notice = True
    else:
        stamps.append(now)
    state[key] = stamps
    state["_notices"] = notices
    try:
        tmp = B2B_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, B2B_STATE)
    except Exception:
        pass
    if throttled:
        try:
            with open(THROTTLED, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass
        if send_notice:
            _throttle_notice(record.get("chat_id"))
    return throttled


def should_wake(record: dict) -> bool:
    """is_relevant plus the hub-mode loop guard. Use this in fetch loops."""
    return is_relevant(record) and not b2b_throttled(record)


def record_key(record: dict):
    """chat_id:message_id, or None for records without a message."""
    if record.get("chat_id") is None or not record.get("message_id"):
        return None
    return "%s:%s" % (record["chat_id"], record["message_id"])


def load_handled() -> set:
    try:
        with open(HANDLED, "r", encoding="utf-8") as fh:
            return {line.strip() for line in fh if line.strip()}
    except Exception:
        return set()


def mark_handled(key: str) -> None:
    if key in load_handled():
        return
    with open(HANDLED, "a", encoding="utf-8") as fh:
        fh.write(key + "\n")


def load_claims() -> dict:
    try:
        with open(CLAIMS, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def claim(keys, ttl: int = None, now: float = None) -> None:
    """Mark messages as being worked on until now + ttl."""
    keys = [k for k in keys if k]
    if not keys:
        return
    now = time.time() if now is None else now
    ttl = CLAIM_TTL if ttl is None else ttl
    claims = {k: v for k, v in load_claims().items()
              if isinstance(v, (int, float)) and v > now - 86400}
    for key in keys:
        claims[key] = now + ttl
    try:
        tmp = CLAIMS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(claims, fh)
        os.replace(tmp, CLAIMS)
    except Exception:
        pass


def queue_records(limit: int = 5000) -> list:
    try:
        with open(QUEUE, "r", encoding="utf-8") as fh:
            lines = fh.readlines()[-limit:]
    except Exception:
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def find_record(key: str):
    """Latest queue record with this key, or None."""
    found = None
    for rec in queue_records():
        if record_key(rec) == key:
            found = rec
    return found


def pending_records(now: float = None) -> list:
    """Messages that woke the agent and have not been answered yet."""
    now = time.time() if now is None else now
    handled = load_handled()
    by_key = {}
    for rec in queue_records():
        if not rec.get("wake"):
            continue
        if rec.get("kind") not in ("message", "edited_message"):
            continue
        key = record_key(rec)
        if not key or key in handled:
            continue
        date = rec.get("date")
        if isinstance(date, (int, float)) and now - date > PENDING_MAX_AGE:
            continue
        by_key[key] = rec
    return list(by_key.values())


def due_pending(now: float = None) -> list:
    """Pending messages nobody is currently working on."""
    now = time.time() if now is None else now
    claims = load_claims()
    out = []
    for rec in pending_records(now):
        expires = claims.get(record_key(rec))
        if isinstance(expires, (int, float)) and expires > now:
            continue
        out.append(rec)
    return out


def digest(record: dict, redelivered: bool = False) -> str:
    line = {
        "key": record_key(record),
        "chat_id": record.get("chat_id"),
        "chat_type": record.get("chat_type"),
        "chat_title": record.get("chat_title"),
        "thread_id": record.get("thread_id"),
        "from": record.get("from_username") or record.get("from_name"),
        "from_is_bot": record.get("from_is_bot"),
        "message_id": record.get("message_id"),
        "text": (record.get("text") or "")[:300],
    }
    if redelivered:
        line["redelivered"] = True
    return json.dumps(line, ensure_ascii=False)


def is_relevant(record: dict) -> bool:
    if record["from_id"] == BOT_ID:
        return False
    if record["kind"] == "my_chat_member":
        return True
    if is_dropped(record):
        return False
    if HUB_MODE and record["chat_type"] in ("group", "supergroup"):
        return is_addressed(record)
    if record["chat_type"] in ("group", "supergroup", "private"):
        return True
    if record.get("from_is_bot"):
        return True
    if record.get("text") and ("@" + BOT_USERNAME) in record["text"]:
        return True
    return False


def needs_typing(record: dict) -> bool:
    """Whether an incoming relevant message should show typing at once.

    Private chats always (when owner_ids is configured, non-owner
    private messages have already been dropped before this point). In
    groups only when a human writes or the bot is named, so other
    bots' routine acknowledgements don't flash a typing indicator for
    a message that will get no reply.
    """
    if record.get("kind") != "message" or not record.get("message_id"):
        return False
    if record.get("chat_type") == "private":
        return True
    if record.get("chat_type") in ("group", "supergroup"):
        if not record.get("from_is_bot"):
            return True
        text = record.get("text") or ""
        if ("@" + BOT_USERNAME) in text:
            return True
        low = text.lower()
        if any(word in low for word in BOT_NAME_KEYWORDS):
            return True
    return False


def signal_typing(record: dict) -> None:
    """Best-effort typing indicator the moment a relevant message lands.

    Telegram typing lasts ~5s but agent turns take minutes, so spawn a
    detached keeper (typing_keeper.py, placed next to this script's
    data directory) that refreshes it until the message is marked
    handled. Never blocks the fetch loop.
    """
    import subprocess
    if not needs_typing(record):
        return
    key = "%s:%s" % (record["chat_id"], record["message_id"])
    try:
        subprocess.Popen(
            [sys.executable, os.path.join(BASE, "typing_keeper.py"),
             str(record["chat_id"]), key],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except Exception:
        pass


def signal_watchdog(record: dict) -> None:
    """Best-effort stuck watchdog the moment a relevant message lands.

    Spawns a detached stuck_watchdog.py (placed next to this script's
    data directory) that watches this one message: if it is still
    unhandled after a few minutes it posts one hint into the chat,
    and it exits as soon as the message is marked handled. Same
    population as the typing keeper (messages that may get a reply).
    Never blocks the fetch loop.
    """
    import subprocess
    if not needs_typing(record):
        return
    if HUB_MODE and record.get("from_is_bot"):
        # The hint is a reply to the stalled message; replying to
        # another agent's message would wake that agent for nothing.
        return
    key = "%s:%s" % (record["chat_id"], record["message_id"])
    try:
        subprocess.Popen(
            [sys.executable, os.path.join(BASE, "stuck_watchdog.py"),
             str(record["chat_id"]), key],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except Exception:
        pass


def mark_read(record: dict) -> None:
    """Best-effort 👀 reaction on a message just pulled (read receipt).

    Only for fresh (non-edited) messages with an id; failures are
    ignored so a reaction problem never blocks the fetch loop.
    """
    if record.get("kind") != "message" or not record.get("message_id"):
        return
    try:
        tg.call("setMessageReaction", {
            "chat_id": record["chat_id"],
            "message_id": record["message_id"],
            "reaction": [{"type": "emoji", "emoji": "👀"}],
        })
    except Exception:
        pass


def main() -> int:
    os.makedirs(BASE, exist_ok=True)
    offset = load_offset()
    result = tg.call("getUpdates", {
        "offset": offset,
        "timeout": 0,
        "limit": 100,
        "allowed_updates": ["message", "edited_message", "my_chat_member"],
    }, http_timeout=30)
    if not result.get("ok"):
        print("ERROR " + str(result.get("description", result))[:200])
        return 1
    updates = result.get("result", [])
    relevant = []
    max_id = offset - 1
    with open(QUEUE, "a", encoding="utf-8") as qf:
        for update in updates:
            record = compact(update)
            max_id = max(max_id, int(update.get("update_id", 0)))
            if is_dropped(record):
                try:
                    with open(DROPPED, "a", encoding="utf-8") as df:
                        df.write(json.dumps(
                            record, ensure_ascii=False) + "\n")
                except Exception:
                    pass
                continue
            wake = should_wake(record)
            record["wake"] = wake
            qf.write(json.dumps(record, ensure_ascii=False) + "\n")
            if wake:
                relevant.append(record)
                mark_read(record)
                signal_typing(record)
                signal_watchdog(record)
    if updates:
        save_offset(max_id + 1)
    claim([record_key(r) for r in relevant])
    # Anything an earlier shift pulled but never answered comes back here.
    due = due_pending()
    claim([record_key(r) for r in due])
    if not relevant and not due:
        print("NONE")
        return 0
    print(f"RELEVANT {len(relevant) + len(due)}")
    for r in relevant:
        print(digest(r))
    for r in due:
        print(digest(r, redelivered=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
