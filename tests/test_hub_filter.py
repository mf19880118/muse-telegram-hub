#!/usr/bin/env python3
"""Local tests for the hub-mode patch in relay/fetch.py and relay/watch.py.

No network and no Muse runtime needed: a stub `tg` module replaces the
real connector helper, and the typing keeper / stuck watchdog spawns
are replaced with recorders. Run from anywhere:

    python3 tests/test_hub_filter.py
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest

RELAY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "relay")

ME = 111           # this bot
OTHER = 222        # another agent's bot
OWNER = 900        # the human owner
STRANGER = 901     # some other human
GROUP = -1001


def load(name: str, config: dict, filename: str = "fetch.py"):
    """Import a fresh copy of a relay script against a temp relay dir."""
    base = tempfile.mkdtemp(prefix="hubtest-")
    stub = os.path.join(base, "stub")
    os.makedirs(stub)
    with open(os.path.join(stub, "tg.py"), "w", encoding="utf-8") as fh:
        fh.write("CALLS = []\nRESPONSES = []\nFAIL = set()\n"
                 "def call(method, params, http_timeout=40.0):\n"
                 "    CALLS.append((method, params))\n"
                 "    if method in FAIL:\n"
                 "        return {'ok': False, 'description': 'boom'}\n"
                 "    if method == 'sendMessage':\n"
                 "        return {'ok': True, 'result': {'message_id': 7000 + len(CALLS)}}\n"
                 "    if method == 'getUpdates':\n"
                 "        return RESPONSES.pop(0) if RESPONSES else "
                 "{'ok': True, 'result': []}\n"
                 "    return {'ok': True, 'result': {}}\n")
    with open(os.path.join(base, "local.json"), "w", encoding="utf-8") as fh:
        json.dump(config, fh)
    for var in ("TG_BOT_USERNAME", "TG_BOT_ID", "TG_BOT_NAME_KEYWORDS",
                "TG_OWNER_IDS", "TG_GROUP_IDS", "TG_HUB_MODE"):
        os.environ.pop(var, None)
    os.environ["TG_RELAY_DIR"] = base
    os.environ["TG_CONNECTOR_BIN"] = stub
    sys.modules.pop("tg", None)
    sys.modules.pop("fetch", None)
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(RELAY, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fetch = mod if filename == "fetch.py" else mod.F
    fetch.spawned = []
    fetch.signal_typing = lambda r: fetch.spawned.append(("typing", r["message_id"]))
    real_watchdog_gate = fetch.signal_watchdog
    fetch.real_signal_watchdog = real_watchdog_gate
    fetch.signal_watchdog = lambda r: fetch.spawned.append(("watchdog", r["message_id"]))
    return mod


def upd(uid, text, from_id, is_bot=False, chat_id=GROUP, chat_type="supergroup",
        reply_to_from=None, edited=False, mid=None):
    msg = {
        "message_id": mid if mid is not None else uid,
        "date": int(time.time()),
        "chat": {"id": chat_id, "type": chat_type, "title": "hub"},
        "from": {"id": from_id, "is_bot": is_bot, "first_name": "n%s" % from_id,
                 "username": "u%s" % from_id},
        "text": text,
    }
    if reply_to_from is not None:
        msg["reply_to_message"] = {"message_id": 1,
                                   "from": {"id": reply_to_from, "is_bot": True}}
    return {"update_id": uid, ("edited_message" if edited else "message"): msg}


HUB = {"bot_username": "Alpha_bot", "bot_id": ME,
       "bot_name_keywords": ["阿尔法"], "owner_ids": [OWNER],
       "group_ids": [GROUP], "hub_mode": True,
       "b2b_limit": 3, "b2b_window_secs": 600}


class HubAddressing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.F = load("fetch_hub", HUB)

    def rel(self, *a, **k):
        return self.F.is_relevant(self.F.compact(upd(*a, **k)))

    def test_owner_plain_chatter_does_not_wake(self):
        self.assertFalse(self.rel(1, "今天天气不错", OWNER))

    def test_owner_mention_wakes(self):
        self.assertTrue(self.rel(2, "@Alpha_bot 查一下", OWNER))

    def test_mention_is_case_insensitive(self):
        self.assertTrue(self.rel(3, "@alpha_BOT 查一下", OWNER))

    def test_mention_of_longer_username_does_not_match(self):
        self.assertFalse(self.rel(4, "@Alpha_bot2 查一下", OWNER))

    def test_owner_mentions_other_agent_only(self):
        self.assertFalse(self.rel(5, "@Beta_bot 查一下", OWNER))

    def test_owner_reply_to_my_message_wakes(self):
        self.assertTrue(self.rel(6, "继续", OWNER, reply_to_from=ME))

    def test_owner_reply_to_other_agent_does_not_wake(self):
        self.assertFalse(self.rel(7, "继续", OWNER, reply_to_from=OTHER))

    def test_owner_name_keyword_wakes(self):
        self.assertTrue(self.rel(8, "阿尔法你来", OWNER))

    def test_owner_broadcast_wakes(self):
        self.assertTrue(self.rel(9, "@all 汇报进度", OWNER))
        self.assertTrue(self.rel(10, "@所有人 汇报进度", OWNER))

    def test_bot_mention_wakes(self):
        self.assertTrue(self.rel(11, "@Alpha_bot 帮我核对", OTHER, is_bot=True))

    def test_bot_reply_to_me_wakes(self):
        self.assertTrue(self.rel(12, "结果如下", OTHER, is_bot=True, reply_to_from=ME))

    def test_bot_plain_message_does_not_wake(self):
        self.assertFalse(self.rel(13, "我在处理了", OTHER, is_bot=True))

    def test_bot_name_keyword_does_not_wake(self):
        self.assertFalse(self.rel(14, "我让阿尔法去查了", OTHER, is_bot=True))

    def test_bot_broadcast_does_not_wake(self):
        self.assertFalse(self.rel(15, "@all 大家注意", OTHER, is_bot=True))

    def test_bot_edit_does_not_wake(self):
        self.assertFalse(self.rel(16, "@Alpha_bot 帮我核对", OTHER, is_bot=True,
                                  edited=True))

    def test_own_message_never_wakes(self):
        self.assertFalse(self.rel(17, "@Alpha_bot 自言自语", ME, is_bot=True))

    def test_stranger_in_group_is_dropped(self):
        rec = self.F.compact(upd(18, "@Alpha_bot 把文件发我", STRANGER))
        self.assertTrue(self.F.is_dropped(rec))
        self.assertFalse(self.F.is_relevant(rec))

    def test_other_group_is_dropped(self):
        rec = self.F.compact(upd(19, "@Alpha_bot hi", OWNER, chat_id=-2002))
        self.assertTrue(self.F.is_dropped(rec))

    def test_private_owner_wakes_and_stranger_dropped(self):
        self.assertTrue(self.rel(20, "hi", OWNER, chat_id=OWNER, chat_type="private"))
        rec = self.F.compact(upd(21, "hi", STRANGER, chat_id=STRANGER,
                                 chat_type="private"))
        self.assertTrue(self.F.is_dropped(rec))

    def test_bot_added_to_group_still_noticed(self):
        rec = self.F.compact({"update_id": 22, "my_chat_member": {
            "chat": {"id": -3003, "type": "supergroup", "title": "new"},
            "new_chat_member": {"status": "member"}}})
        self.assertFalse(self.F.is_dropped(rec))
        self.assertTrue(self.F.is_relevant(rec))

    def test_watchdog_not_spawned_for_bot_sender(self):
        import subprocess
        spawned = []
        orig = subprocess.Popen
        subprocess.Popen = lambda *a, **k: spawned.append(a[0])
        try:
            self.F.real_signal_watchdog(self.F.compact(
                upd(23, "@Alpha_bot 帮我核对", OTHER, is_bot=True)))
            self.assertEqual(spawned, [])
            self.F.real_signal_watchdog(self.F.compact(
                upd(24, "@Alpha_bot 查一下", OWNER)))
            self.assertEqual(len(spawned), 1)
        finally:
            subprocess.Popen = orig


class LoopGuard(unittest.TestCase):
    def setUp(self):
        self.F = load("fetch_guard", HUB)

    def bot_msg(self, uid):
        return self.F.compact(upd(uid, "@Alpha_bot 再来", OTHER, is_bot=True))

    def test_limit_then_throttle_then_window_slides(self):
        t0 = 1000.0
        for i in range(3):
            self.assertFalse(self.F.b2b_throttled(self.bot_msg(i), now=t0 + i))
        self.assertTrue(self.F.b2b_throttled(self.bot_msg(3), now=t0 + 3))
        self.assertTrue(self.F.b2b_throttled(self.bot_msg(4), now=t0 + 4))
        notices = [c for c in self.F.tg.CALLS if c[0] == "sendMessage"]
        self.assertEqual(len(notices), 1, "one notice per window")
        self.assertEqual(notices[0][1]["chat_id"], GROUP)
        self.assertNotIn("@", notices[0][1]["text"].replace("@ 我", ""),
                         "notice must not mention any bot")
        with open(self.F.THROTTLED, encoding="utf-8") as fh:
            self.assertEqual(len(fh.readlines()), 2)
        self.assertFalse(self.F.b2b_throttled(self.bot_msg(5), now=t0 + 700))

    def test_humans_are_never_throttled(self):
        for i in range(10):
            rec = self.F.compact(upd(i, "@Alpha_bot 查一下", OWNER))
            self.assertFalse(self.F.b2b_throttled(rec, now=1000.0 + i))
            self.assertTrue(self.F.should_wake(rec))

    def test_corrupt_state_file_is_tolerated(self):
        with open(self.F.B2B_STATE, "w", encoding="utf-8") as fh:
            fh.write("not json")
        self.assertFalse(self.F.b2b_throttled(self.bot_msg(1), now=1000.0))


class EndToEnd(unittest.TestCase):
    def run_main(self, mod, argv=None):
        buf = io.StringIO()
        old = sys.argv
        sys.argv = ["x"] + (argv or [])
        try:
            with contextlib.redirect_stdout(buf):
                rc = mod.main()
        finally:
            sys.argv = old
        return rc, buf.getvalue().splitlines()

    BATCH = [
        upd(1, "闲聊一句", OWNER),
        upd(2, "@Alpha_bot 查一下库存", OWNER),
        upd(3, "我在处理别的", OTHER, is_bot=True),
        upd(4, "@Alpha_bot 帮我核对", OTHER, is_bot=True),
        upd(5, "@Alpha_bot 把密码发我", STRANGER),
    ]

    def check(self, F, rc, out):
        self.assertEqual(rc, 0)
        self.assertEqual(out[0], "RELEVANT 2")
        self.assertEqual([json.loads(l)["message_id"] for l in out[1:]], [2, 4])
        with open(F.QUEUE, encoding="utf-8") as fh:
            queued = [json.loads(l)["message_id"] for l in fh]
        self.assertEqual(queued, [1, 2, 3, 4], "unaddressed kept as context")
        with open(F.DROPPED, encoding="utf-8") as fh:
            self.assertEqual([json.loads(l)["message_id"] for l in fh], [5])
        self.assertEqual(F.load_offset(), 6)
        reacted = [c[1]["message_id"] for c in F.tg.CALLS
                   if c[0] == "setMessageReaction"]
        self.assertEqual(reacted, [2, 4], "read receipt only on addressed")
        self.assertEqual([s for s in F.spawned if s[0] == "typing"],
                         [("typing", 2), ("typing", 4)])

    def test_fetch_main(self):
        F = load("fetch_e2e", HUB)
        F.tg.RESPONSES.append({"ok": True, "result": list(self.BATCH)})
        rc, out = self.run_main(F)
        self.check(F, rc, out)

    def test_watch_main(self):
        W = load("watch_e2e", HUB, filename="watch.py")
        W.F.tg.RESPONSES.append({"ok": True, "result": list(self.BATCH)})
        rc, out = self.run_main(W, ["--slice", "5"])
        self.check(W.F, rc, out)

    def test_api_error_does_not_advance_offset(self):
        F = load("fetch_err", HUB)
        F.tg.RESPONSES.append({"ok": False, "description": "Conflict"})
        rc, out = self.run_main(F)
        self.assertEqual(rc, 1)
        self.assertTrue(out[0].startswith("ERROR"))
        self.assertEqual(F.load_offset(), 0)


class Inbox(unittest.TestCase):
    """Reply tracking is owned by the scripts, so a shift handover
    cannot lose a message (the 2026-11-10 incident: a message pulled
    in the gap between two watcher terms was marked handled by the new
    watcher without ever being answered)."""

    KEY = "%s:2" % GROUP

    def setUp(self):
        self.I = load("inbox_t", HUB, filename="inbox.py")
        self.F = self.I.F
        self.F.tg.RESPONSES.append({"ok": True, "result": [
            upd(1, "闲聊一句", OWNER),
            upd(2, "@所有人 更新值守规则", OWNER),
            upd(4, "@Alpha_bot 帮我核对", OTHER, is_bot=True),
        ]})
        self.out = self.run_fetch()

    def run_fetch(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.F.main()
        return buf.getvalue().splitlines()

    def call(self, fn, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fn(*args)
        return rc, buf.getvalue()

    def keys(self, records):
        return sorted(self.F.record_key(r) for r in records)

    def test_digest_carries_keys_and_only_wake_records_are_pending(self):
        self.assertEqual(self.out[0], "RELEVANT 2")
        self.assertEqual([json.loads(l)["key"] for l in self.out[1:]],
                         [self.KEY, "%s:4" % GROUP])
        self.assertEqual(self.keys(self.F.pending_records()),
                         sorted([self.KEY, "%s:4" % GROUP]))

    def test_claimed_messages_are_not_handed_out_twice(self):
        self.assertEqual(self.F.due_pending(), [])
        self.assertEqual(self.run_fetch(), ["NONE"])

    def test_unanswered_message_is_redelivered_after_claim_expires(self):
        later = time.time() + self.F.CLAIM_TTL + 1
        self.assertEqual(self.keys(self.F.due_pending(now=later)),
                         sorted([self.KEY, "%s:4" % GROUP]))

    def test_new_shift_gets_the_lost_message_from_watch_slice(self):
        # Simulate the claim running out, then a fresh watcher slice.
        with open(self.F.CLAIMS, "w", encoding="utf-8") as fh:
            json.dump({}, fh)
        W = load("watch_redeliver", HUB, filename="watch.py")
        # Same relay dir contents: copy the state across.
        import shutil
        for name in ("queue.jsonl", "state.json"):
            shutil.copy(os.path.join(os.path.dirname(self.F.QUEUE), name),
                        os.path.join(os.path.dirname(W.F.QUEUE), name))
        old = sys.argv
        sys.argv = ["x", "--slice", "5"]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                W.main()
        finally:
            sys.argv = old
        out = buf.getvalue().splitlines()
        self.assertEqual(out[0], "RELEVANT 2")
        self.assertTrue(all(json.loads(l)["redelivered"] for l in out[1:]))
        self.assertEqual([c for c in W.F.tg.CALLS if c[0] == "getUpdates"], [],
                         "handed out before polling for new messages")

    def test_reply_sends_then_closes(self):
        rc, out = self.call(self.I.cmd_reply, self.KEY, "已更新", False)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("DONE"))
        sent = [c[1] for c in self.F.tg.CALLS if c[0] == "sendMessage"]
        self.assertEqual(sent[-1]["chat_id"], GROUP)
        self.assertEqual(sent[-1]["reply_parameters"]["message_id"], 2)
        self.assertIn(self.KEY, self.F.load_handled())
        self.assertNotIn(self.KEY, self.keys(self.F.pending_records()))

    def test_failed_send_leaves_message_open(self):
        self.F.tg.FAIL.add("sendMessage")
        rc, out = self.call(self.I.cmd_reply, self.KEY, "已更新", False)
        self.assertEqual(rc, 1)
        self.assertNotIn(self.KEY, self.F.load_handled())
        self.assertIn(self.KEY, self.keys(self.F.pending_records()))

    def test_empty_reply_is_refused(self):
        rc, _ = self.call(self.I.cmd_reply, self.KEY, "   ", False)
        self.assertEqual(rc, 2)
        self.assertNotIn(self.KEY, self.F.load_handled())

    def test_interim_reply_keeps_open_and_extends_claim(self):
        rc, out = self.call(self.I.cmd_reply, self.KEY, "收到，在处理", True)
        self.assertEqual(rc, 0)
        self.assertNotIn(self.KEY, self.F.load_handled())
        later = time.time() + self.F.CLAIM_TTL + 1
        self.assertNotIn(self.KEY, self.keys(self.F.due_pending(now=later)))
        much_later = time.time() + self.F.WORKING_TTL + 1
        self.assertIn(self.KEY, self.keys(self.F.due_pending(now=much_later)))

    def test_owner_message_cannot_be_skipped(self):
        rc, out = self.call(self.I.cmd_skip, self.KEY, "只是通知")
        self.assertEqual(rc, 2)
        self.assertTrue(out.startswith("REFUSED"))
        self.assertNotIn(self.KEY, self.F.load_handled())

    def test_bot_message_can_be_skipped(self):
        key = "%s:4" % GROUP
        rc, out = self.call(self.I.cmd_skip, key, "纯确认")
        self.assertEqual(rc, 0)
        self.assertIn(key, self.F.load_handled())

    def test_long_reply_is_split(self):
        rc, _ = self.call(self.I.cmd_reply, self.KEY, "字" * 9000, False)
        self.assertEqual(rc, 0)
        sent = [c[1] for c in self.F.tg.CALLS if c[0] == "sendMessage"]
        self.assertEqual([len(m["text"]) for m in sent], [4000, 4000, 1000])
        self.assertIn("reply_parameters", sent[0])
        self.assertNotIn("reply_parameters", sent[1])

    def test_old_messages_stop_being_pending(self):
        far = time.time() + self.F.PENDING_MAX_AGE + 10
        self.assertEqual(self.F.pending_records(now=far), [])

    def test_list_shows_open_messages(self):
        rc, out = self.call(self.I.cmd_list)
        self.assertEqual(out.splitlines()[0], "OPEN 2")


class Heartbeat(unittest.TestCase):
    """The watch slice keeps watcher-state.json fresh by itself, so a
    quiet 600-second slice no longer looks like a dead watcher to the
    supervisor run (its limit is 180 seconds)."""

    def setUp(self):
        self.W = load("watch_beat", HUB, filename="watch.py")
        self.F = self.W.F

    def run_slice(self, *argv):
        old = sys.argv
        sys.argv = ["x"] + list(argv)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = self.W.main()
        finally:
            sys.argv = old
        return rc, buf.getvalue().splitlines()

    def test_first_beat_starts_a_term(self):
        state = self.F.beat(now=1000.0)
        self.assertEqual(state, {"started_at": 1000, "beat_at": 1000})
        self.assertEqual(self.F.load_watcher_state(), state)

    def test_later_beats_keep_the_term(self):
        self.F.beat(now=1000.0)
        state = self.F.beat(now=1050.0)
        self.assertEqual(state, {"started_at": 1000, "beat_at": 1050})

    def test_gap_between_slices_does_not_restart_the_term(self):
        # The watcher spent 20 minutes on a task between two slices.
        # Its term must still look as old as it is, or the supervisor
        # would not hand over at 50 minutes and the watch would lapse.
        self.F.beat(now=1000.0)
        state = self.F.beat(now=1000.0 + 1200)
        self.assertEqual(state["started_at"], 1000)

    def test_new_term_flag_resets_a_fresh_term(self):
        self.F.beat(now=1000.0)
        state = self.F.beat(new_term=True, now=1010.0)
        self.assertEqual(state, {"started_at": 1010, "beat_at": 1010})

    def test_corrupt_state_is_tolerated(self):
        with open(self.F.WATCHER_STATE, "w", encoding="utf-8") as fh:
            fh.write("not json")
        self.assertEqual(self.F.beat(now=1000.0)["started_at"], 1000)

    def test_quiet_slice_writes_the_heartbeat(self):
        before = int(time.time())
        rc, out = self.run_slice("--slice", "0")
        self.assertEqual(out, ["NONE"])
        state = self.F.load_watcher_state()
        self.assertGreaterEqual(state["beat_at"], before)
        self.assertGreaterEqual(state["started_at"], before)

    def test_slice_continues_a_running_term(self):
        started = int(time.time()) - 100
        with open(self.F.WATCHER_STATE, "w", encoding="utf-8") as fh:
            json.dump({"started_at": started, "beat_at": started + 90}, fh)
        self.run_slice("--slice", "0")
        self.assertEqual(self.F.load_watcher_state()["started_at"], started)

    def test_takeover_slice_starts_a_new_term(self):
        started = int(time.time()) - 3000
        with open(self.F.WATCHER_STATE, "w", encoding="utf-8") as fh:
            json.dump({"started_at": started,
                       "beat_at": int(time.time()) - 5}, fh)
        self.run_slice("--slice", "0", "--new-term")
        self.assertGreater(self.F.load_watcher_state()["started_at"], started)

    def test_locked_slice_leaves_the_heartbeat_alone(self):
        import fcntl
        holder = open(self.W.LOCK, "w")
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            rc, out = self.run_slice("--slice", "0", "--new-term")
        finally:
            holder.close()
        self.assertEqual(out, ["LOCKED"])
        self.assertEqual(self.F.load_watcher_state(), {})


class ContextAndTiming(unittest.TestCase):
    """Messages are handed out with the last few earlier messages of
    the chat attached, and every reply records how long it took."""

    KEY = "%s:3" % GROUP

    def setUp(self):
        self.I = load("inbox_ctx", dict(HUB, context_messages=2),
                      filename="inbox.py")
        self.F = self.I.F
        self.sent_at = int(time.time()) - 40
        batch = [
            upd(1, "第一句闲聊", OWNER),
            upd(2, "我在处理别的", OTHER, is_bot=True),
            upd(9, "别的群的话", OWNER, chat_id=OWNER, chat_type="private"),
            upd(3, "@Alpha_bot 查一下库存", OWNER),
            upd(4, "后面才来的话", OWNER),
        ]
        for u in batch:
            u["message"]["date"] = self.sent_at
        self.F.tg.RESPONSES.append({"ok": True, "result": batch})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.F.main()
        self.out = buf.getvalue().splitlines()

    def call(self, fn, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fn(*args)
        return rc, buf.getvalue().splitlines()

    def line(self, key):
        return [json.loads(l) for l in self.out[1:]
                if json.loads(l)["key"] == key][0]

    def test_digest_carries_earlier_messages_of_the_same_chat(self):
        ctx = self.line(self.KEY)["context"]
        self.assertEqual([c["text"] for c in ctx],
                         ["第一句闲聊", "我在处理别的"])
        self.assertEqual(ctx[1]["from"], "u%s" % OTHER)

    def test_context_respects_the_limit(self):
        rec = self.F.find_record(self.KEY)
        self.assertEqual([c["text"] for c in self.F.recent_context(rec, 1)],
                         ["我在处理别的"])
        self.assertEqual(self.F.recent_context(rec, 0), [])

    def test_first_message_of_a_chat_has_no_context_field(self):
        self.assertNotIn("context", self.line("%s:9" % OWNER))

    def test_list_stays_compact(self):
        rc, out = self.call(self.I.cmd_list)
        self.assertEqual(out[0], "OPEN 2")
        self.assertTrue(all("context" not in json.loads(l) for l in out[1:]))

    def test_message_text_is_handed_out_in_full(self):
        long_text = "@Alpha_bot " + "字" * 2000
        rec = self.F.compact(upd(50, long_text, OWNER))
        self.assertEqual(json.loads(self.F.digest(rec))["text"], long_text)
        short = json.loads(self.F.digest(rec, max_text=300))["text"]
        self.assertEqual(len(short), 301)
        self.assertTrue(short.endswith("…"))

    def test_cut_context_is_marked(self):
        self.F.tg.RESPONSES.append({"ok": True, "result": [
            upd(60, "长" * 500, OWNER), upd(61, "@Alpha_bot 看上面", OWNER)]})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.F.main()
        line = json.loads(buf.getvalue().splitlines()[1])
        self.assertEqual(line["context"][-1]["text"], "长" * 160 + "…")

    def test_edited_message_does_not_count_as_a_long_wait(self):
        old = upd(70, "@Alpha_bot 改过的话", OWNER, edited=True)
        old["edited_message"]["date"] = int(time.time()) - 5000
        self.F.tg.RESPONSES.append({"ok": True, "result": [old]})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.F.main()
        self.call(self.I.cmd_reply, "%s:70" % GROUP, "好", False)
        with open(self.I.LATENCY, encoding="utf-8") as fh:
            row = [json.loads(l) for l in fh][-1]
        self.assertNotIn("total_secs", row)
        self.assertNotIn("wait_secs", row)
        self.assertIn("handle_secs", row)

    def test_queue_records_carry_the_pull_time(self):
        rec = self.F.find_record(self.KEY)
        self.assertGreaterEqual(rec["pulled_at"], self.sent_at + 40)

    def test_reply_records_where_the_time_went(self):
        rc, _ = self.call(self.I.cmd_reply, self.KEY, "有货", False)
        self.assertEqual(rc, 0)
        with open(self.I.LATENCY, encoding="utf-8") as fh:
            rows = [json.loads(l) for l in fh]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["key"], self.KEY)
        self.assertFalse(row["interim"])
        self.assertGreaterEqual(row["wait_secs"], 40)
        self.assertLess(row["handle_secs"], 5)
        self.assertGreaterEqual(row["total_secs"], row["wait_secs"])

    def test_failed_send_records_nothing(self):
        self.F.tg.FAIL.add("sendMessage")
        self.call(self.I.cmd_reply, self.KEY, "有货", False)
        self.assertFalse(os.path.exists(self.I.LATENCY))

    def test_stats_counts_the_first_reply_per_message(self):
        self.call(self.I.cmd_reply, self.KEY, "收到，开始处理", True)
        self.call(self.I.cmd_reply, self.KEY, "有货", False)
        self.call(self.I.cmd_reply, "%s:9" % OWNER, "在", False)
        rc, out = self.call(self.I.cmd_stats, 20)
        self.assertEqual(rc, 0)
        self.assertEqual(out[0], "STATS 2")
        self.assertTrue(out[1].startswith("MEDIAN sent->first reply "))
        self.assertIn("sent->pulled", out[1])
        rows = [json.loads(l) for l in out[2:]]
        self.assertEqual([r["key"] for r in rows], [self.KEY, "%s:9" % OWNER])
        self.assertTrue(rows[0]["interim"], "the interim reply came first")

    def test_stats_without_any_reply(self):
        rc, out = self.call(self.I.cmd_stats, 20)
        self.assertEqual((rc, out), (0, ["STATS 0"]))


class UpstreamBehaviourUnchanged(unittest.TestCase):
    """hub_mode off: every group message is relevant, as before the patch."""

    @classmethod
    def setUpClass(cls):
        cfg = dict(HUB)
        cfg.pop("hub_mode")
        cls.F = load("fetch_plain", cfg)

    def test_everything_in_group_is_relevant(self):
        F = self.F
        for u in (upd(1, "闲聊一句", OWNER), upd(2, "路过", STRANGER),
                  upd(3, "我在处理别的", OTHER, is_bot=True)):
            rec = F.compact(u)
            self.assertFalse(F.is_dropped(rec))
            self.assertTrue(F.should_wake(rec))

    def test_no_throttle_when_hub_off(self):
        F = self.F
        for i in range(20):
            rec = F.compact(upd(i, "@Alpha_bot x", OTHER, is_bot=True))
            self.assertTrue(F.should_wake(rec))
        self.assertFalse(os.path.exists(F.B2B_STATE))


if __name__ == "__main__":
    unittest.main(verbosity=1)
