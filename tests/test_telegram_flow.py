"""End to end through the REAL Telegram polling code against a fake Bot API that mimics Telegram's rules:
an update is re-delivered on every getUpdates call until a later call confirms it with a higher `offset`."""
import tempfile
import unittest
from pathlib import Path
from crypto_watcher.bot import Bot
from crypto_watcher.config import Settings
from crypto_watcher.data import DemoProvider
from crypto_watcher.executor import Executor
from crypto_watcher.state import StateStore
from crypto_watcher.telegram import TelegramNotifier
from helpers import Clock, FakeExchange, make_infos


class FakeBotApi:
    def __init__(self, chat_id=1, bot_name="mybot"):
        self.updates, self.sent, self.chat_id, self.next_id, self.bot_name = [], [], chat_id, 1000, bot_name

    def user_says(self, text, chat_id=None, user=7):
        self.next_id += 1
        self.updates.append({"update_id": self.next_id, "message": {"chat": {"id": chat_id or self.chat_id}, "from": {"id": user}, "text": text}})

    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[1]
        result = {"ok": True, "result": True}
        if method == "getUpdates":
            offset = json.get("offset", 0)
            self.updates = [u for u in self.updates if u["update_id"] >= offset] if offset else self.updates   # confirm
            if offset == -1:                                                                                  # "only the newest"
                self.updates = self.updates[-1:]
            result = {"ok": True, "result": list(self.updates)}
        elif method == "getMe":
            result = {"ok": True, "result": {"username": self.bot_name}}
        elif method == "sendMessage":
            self.sent.append(json["text"])
        return type("R", (), {"status_code": 200, "json": lambda self_: result})()


class FlowBase(unittest.TestCase):
    def build(self, state_path=None, chat_id="1"):
        self.clock = Clock()
        self.api = getattr(self, "api", None) or FakeBotApi(int(chat_id))
        prices = {s: v["last"] for s, v in DemoProvider(int(self.clock() * 1000)).tickers().items()}
        self.ex = FakeExchange(prices, self.clock)
        self.state = StateStore(state_path)
        notifier = TelegramNotifier("T", chat_id, session=self.api, sleep=lambda s: None)
        notifier.clock = self.clock
        settings = Settings(min_quote_volume=1.0)
        executor = Executor(self.ex, settings, self.state, notifier, make_infos(list(prices)), self.clock)
        self.bot = Bot(settings, DemoProvider(int(self.clock() * 1000)), "demo", None, notifier, self.state, executor, self.ex, None,
                       self.clock, sleep=lambda s: None)
        self.notifier = notifier
        return self.bot


class CommandFlowTests(FlowBase):
    def test_one_command_gets_exactly_one_reply_no_matter_how_often_we_poll(self):
        bot = self.build()
        bot.start()
        self.api.sent.clear()
        self.api.user_says("/positions")
        for _ in range(50):                                   # 50 polls = 100 seconds at command_poll_seconds=2
            bot._poll_commands()
        self.assertEqual(len(self.api.sent), 1, self.api.sent)

    def test_each_of_several_commands_is_answered_once(self):
        bot = self.build()
        bot.start()
        self.api.sent.clear()
        for text in ("/status", "/pnl", "/positions@mybot"):
            self.api.user_says(text)
        for _ in range(10):
            bot._poll_commands()
        self.assertEqual(len(self.api.sent), 3)

    def test_group_chat_flow(self):
        self.api = FakeBotApi(-100777)
        bot = self.build(chat_id="-100777")
        bot.start()
        self.api.sent.clear()
        self.api.user_says("/positions@mybot", user=9)
        self.api.user_says("/pause@mybot", user=9)            # not allowed in a group
        for _ in range(10):
            bot._poll_commands()
        self.assertEqual(len(self.api.sent), 2)
        self.assertFalse(self.state["paused"])

    def test_commands_typed_while_the_bot_was_down_are_dropped_not_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s" / "state.json"
            bot = self.build(path)
            bot.start()
            self.api.user_says("/positions")
            bot._poll_commands()
            self.assertEqual(len(self.api.sent), 1)
            self.api.user_says("/pause")                       # typed while the bot is "down"
            self.api.sent.clear()
            restarted = self.build(path)
            restarted.start()
            for _ in range(3):
                restarted._poll_commands()
            self.assertEqual(self.api.sent, [])
            self.assertFalse(self.state["paused"])

    def test_a_restart_with_lost_state_does_not_replay_the_backlog(self):
        """State file gone (new container without a volume, wrong path...): Telegram still holds old unconfirmed commands."""
        self.api = FakeBotApi()
        for text in ("/pause", "/positions", "/resume", "/positions"):
            self.api.user_says(text)                           # typed hours ago, never confirmed
        bot = self.build(None)                                 # empty state: tg_offset = 0
        bot.start()
        self.api.sent.clear()
        for _ in range(5):
            bot._poll_commands()
        self.assertEqual(self.api.sent, [])                    # stale commands are discarded at startup, not executed
        self.assertFalse(self.state["paused"])
        self.api.user_says("/status")
        bot._poll_commands()
        self.assertEqual(len(self.api.sent), 1)                # but new ones work


class NeverConfirmingApi(FakeBotApi):
    """A broken/pathological API: hands back the same update on every call, whatever offset we send."""
    def post(self, url, json=None, timeout=None):
        if url.endswith("/getUpdates") and json.get("offset", 0) != -1 and json.get("offset", 0) < self.next_id + 1:
            return type("R", (), {"status_code": 200, "json": lambda self_: {"ok": True, "result": list(self.updates)}})()
        return super().post(url, json, timeout)


class FloodBrakeTests(unittest.TestCase):
    def notifier(self, clock):
        api = FakeBotApi()
        return TelegramNotifier("T", "1", session=api, sleep=lambda s: None, clock=clock), api

    def test_a_burst_of_distinct_messages_is_capped_and_announced_once(self):
        clock = Clock(1000.0)
        notifier, api = self.notifier(clock)
        for i in range(500):
            notifier.send(f"message {i}")
        texts = api.sent
        self.assertEqual(len([t for t in texts if t.startswith("message")]), TelegramNotifier.MAX_PER_MINUTE)
        self.assertEqual(len([t for t in texts if "Çok fazla mesaj" in t]), 1)
        self.assertLessEqual(len(texts), TelegramNotifier.MAX_PER_MINUTE + 1)

    def test_identical_messages_are_limited_but_two_in_a_row_are_fine(self):
        clock = Clock(1000.0)
        notifier, api = self.notifier(clock)
        for _ in range(6):
            notifier.send("📭 Açık pozisyon yok.")
            clock.now += 1
        self.assertEqual(len([t for t in api.sent if t.startswith("📭")]), TelegramNotifier.DUPLICATES_ALLOWED)
        clock.now += 60                                          # window passed: allowed again
        notifier.send("📭 Açık pozisyon yok.")
        self.assertEqual(len([t for t in api.sent if t.startswith("📭")]), TelegramNotifier.DUPLICATES_ALLOWED + 1)

    def test_budget_refills_and_there_is_an_hourly_ceiling(self):
        clock = Clock(1000.0)
        notifier, api = self.notifier(clock)
        for i in range(TelegramNotifier.MAX_PER_MINUTE + 5):
            notifier.send(f"a{i}")
        clock.now += 61
        self.assertTrue(notifier.send("after a minute"))
        count = len(api.sent)
        for i in range(400):                                     # steady flow of 1 message / 8 s for ~53 minutes
            clock.now += 8
            notifier.send(f"steady {i}")
        sent_in_hour = len([t for t in api.sent if t.startswith("steady")]) + 1
        self.assertLessEqual(sent_in_hour, TelegramNotifier.MAX_PER_HOUR)
        self.assertGreater(len(api.sent), count)                 # not permanently muted

    def test_normal_trading_volume_is_never_throttled(self):
        clock = Clock(1000.0)
        notifier, api = self.notifier(clock)
        for i in range(30):                                      # 30 trade events spread over a day
            clock.now += 2700
            self.assertTrue(notifier.send(f"trade event {i}"))
        self.assertEqual(len(api.sent), 30)

    def test_a_telegram_that_never_confirms_updates_cannot_cause_a_message_storm(self):
        api = NeverConfirmingApi()
        api.user_says("/positions")
        clock = Clock()
        notifier = TelegramNotifier("T", "1", session=api, sleep=lambda s: None, clock=clock)
        base = FlowBase()
        base.api = api
        bot = base.build()
        bot.notify = notifier
        bot.executor.notify = notifier
        for _ in range(1800):                                    # one hour of polls at 2 s
            clock.now += 2
            bot._poll_commands()
        replies = [t for t in api.sent if "pozisyon" in t.lower()]
        self.assertLess(len(replies), 100)                       # without the brake this would be 1800
        self.assertGreater(len(replies), 0)


class StateResilienceTests(unittest.TestCase):
    def test_unwritable_state_directory_fails_fast_with_a_clear_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "file"
            blocker.write_text("x")
            store = StateStore(blocker / "state.json")          # parent is a regular file: cannot be created, even as root
            with self.assertRaisesRegex(RuntimeError, "not writable"):
                store.check_writable()
            self.assertFalse(store.save())                       # and saving never raises at runtime
            StateStore(Path(tmp) / "ok" / "state.json").check_writable()


if __name__ == "__main__":
    unittest.main()
