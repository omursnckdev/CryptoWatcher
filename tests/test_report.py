import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path
from crypto_watcher import journal as jr
from crypto_watcher import signals as sg
from crypto_watcher.config import Settings
from crypto_watcher.state import StateStore

spec = importlib.util.spec_from_file_location("report_tool", Path(__file__).resolve().parent.parent / "tools" / "report.py")
report_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report_tool)
DAY = 86_400_000


def trade(i, symbol, side, reason, r, pnl, kind, mfe=0.1, mae=-1.0, day=0):
    return {"id": f"{symbol}-{i}", "symbol": symbol, "side": side, "opened_ms": 1_790_000_000_000 + (day + i) * 3_600_000,
            "closed_ms": 1_790_000_000_000 + day * DAY + i * 3_600_000, "entry": 100.0, "exit": 99.0, "risk_distance": 2.0,
            "reason": reason, "pnl": pnl, "r": r, "mfe_r": mfe, "mae_r": mae, "hold_h": 3.0, "kind": kind, "after": None,
            "entry_ctx": {"score": 74.0}}


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name)

    def snapshot(self):
        return {p.name: p.read_bytes() for p in self.path.iterdir()}

    def test_empty_directory_does_not_crash(self):
        text = report_tool.report(self.path)
        self.assertIn("Bot durumu", text)
        self.assertIn("günlükte sonuçlanmış işlem yok", text)
        self.assertIn("shadow.jsonl yok", text)
        self.assertEqual(list(self.path.iterdir()), [])             # nothing was created either

    def test_full_report_and_nothing_is_written(self):
        journal = jr.Journal(self.path / "journal.jsonl")
        rows = [trade(1, "AAAUSDT", "LONG", "TAKE_PROFIT", 1.9, 95.0, "win", mfe=2.0, day=0),
                trade(2, "BBBUSDT", "SHORT", "STOP_LOSS", -1.07, -53.0, "never_worked", day=0),
                trade(3, "BBBUSDT", "SHORT", "STOP_LOSS", -1.07, -53.0, "never_worked", day=1),
                trade(4, "BBBUSDT", "SHORT", "LIQUIDATION", -11.0, -560.0, "forced", mae=-11.0, day=1),
                trade(5, "AAAUSDT", "LONG", "BREAKEVEN_STOP", -0.02, -1.0, "breakeven", mfe=1.5, day=2)]
        for row in rows:
            journal.add(row)
        state = StateStore(self.path / "state.json")
        state["totals"] = {"trades": 5, "wins": 1, "realized": -572.0}
        state["history"] = [{"symbol": "BBBUSDT", "pnl": -560.0, "reason": "LIQUIDATION", "closed_ms": 1}]
        state.save()
        (self.path / "shadow.jsonl").write_text(json.dumps({"k": "start", "ms": 1_790_000_000_000}) + "\n")
        before = self.snapshot()
        text = report_tool.report(self.path)
        self.assertEqual(self.snapshot(), before)                    # strictly read-only
        for expected in ("5 işlem", "toplam -572 USDT", "kazanma %20", "en kötü 3: -11.0R, -1.1R, -1.1R",
                         "LIQUIDATION", "STOP_LOSS", "SHORT", "borsa zorla kapattı", "BBBUSDT", "Gölge kayıt", "Atlanan sinyaller"):
            self.assertIn(expected, text)
        self.assertIn("  STOP_LOSS         2 işlem · ort -1.07R · toplam -106 USDT", text)
        coin_rows = [l.split() for l in text.split("Coin bazında")[1].splitlines() if l.startswith("  ") and "işlem" in l]
        self.assertEqual([(c[0], c[1]) for c in coin_rows], [("BBBUSDT", "3")])         # coin table needs >= 3 trades
        self.assertNotIn("<b>", text)                                       # telegram markup stripped
        days = [l for l in text.splitlines() if re.match(r"  \d\d-\d\d ", l)]
        self.assertEqual(len(days), 3)

    def test_shadow_file_without_start_line_is_not_modified(self):
        (self.path / "shadow.jsonl").write_text("")
        report_tool.report(self.path)
        self.assertEqual((self.path / "shadow.jsonl").read_text(), "")


if __name__ == "__main__":
    unittest.main()
