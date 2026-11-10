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
        fh.write("CALLS = []\nRESPONSES = []\n"
                 "def call(method, params, http_timeout=40.0):\n"
                 "    CALLS.append((method, params))\n"
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
        "date": 1700000000 + uid,
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
