"""india-backtest momentum_paper.py (the forward paper portfolio for the monthly momentum rotation) and
pit.month_end_sessions' last-session rule. Offline: prices are swapped in."""

import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import load

import nse

pit = load("india-backtest", "pit")
mp = load("india-backtest", "momentum_paper")


class MonthEnd(unittest.TestCase):
    def test_last_session_counts_when_the_next_session_is_next_month(self):
        # 2026-10-30 is a Friday; NSE's next session is Monday 2026-11-02
        self.assertEqual(pit.month_end_sessions(["2026-10-29", "2026-10-30"]), ["2026-10-30"])

    def test_last_session_mid_month_does_not(self):
        self.assertEqual(pit.month_end_sessions(["2026-10-28", "2026-10-29"]), [])

    def test_earlier_month_ends_unchanged(self):
        self.assertEqual(pit.month_end_sessions(["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-05"]),
                         ["2026-09-30"])


class Paper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_env = os.environ.get("TRADER_DIR")
        os.environ["TRADER_DIR"] = self.tmp
        self.saved = (mp.day_bar, mp.yahoo_day)
        mp.yahoo_day = lambda sym, d: (100.0, 101.0, 99.0, 100.5)

    def tearDown(self):
        mp.day_bar, mp.yahoo_day = self.saved
        if self.old_env is None:
            os.environ.pop("TRADER_DIR", None)
        else:
            os.environ["TRADER_DIR"] = self.old_env
        shutil.rmtree(self.tmp)

    def run_cmd(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = mp.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def ledger(self, holdings, pending, cash):
        self.assertEqual(self.run_cmd("init", "--capital", "100000", "--top", "2", "--broker", "dhan")[0], 0)
        L = mp.load_ledger()
        L["cash"], L["holdings"], L["pending"] = cash, holdings, pending
        mp.save_ledger(L)

    def test_init_refuses_to_overwrite(self):
        self.run_cmd("init")
        rc, _, err = self.run_cmd("init")
        self.assertEqual(rc, 2)
        self.assertIn("already exists", err)

    def test_plan_refuses_a_mid_month_date(self):
        self.run_cmd("init")
        rc, _, err = self.run_cmd("plan", "--date", "2026-10-29")
        self.assertEqual(rc, 2)
        self.assertIn("not the last session of its month", err)

    def test_fill_sells_first_then_buys_with_equity_over_n(self):
        self.ledger({"OLD": {"qty": 10, "fill": 500.0, "entry_date": "2026-10-01", "buy_charges": 10.0,
                             "last": 500.0, "why": ""}},
                    {"signal_date": "2026-10-30", "fill_on": "2026-11-02", "equity_at_signal": 100000.0,
                     "sells": [{"symbol": "OLD", "why": "left the top 2"}],
                     "buys": [{"symbol": "NEW", "why": "rank 1"}, {"symbol": "LOCK", "why": "rank 2"}],
                     "top": [["NEW", 50.0], ["LOCK", 40.0]]}, cash=95000.0)
        bars = {"OLD": (600.0, 610.0, 590.0, 605.0, "test"), "NEW": (1000.0, 1010.0, 990.0, 1005.0, "test"),
                "LOCK": (200.0, 200.0, 200.0, 200.0, "test")}
        mp.day_bar = lambda sym, d: bars.get(sym)
        rc, out, _ = self.run_cmd("fill", "--date", "2026-11-02")
        self.assertEqual(rc, 0, out)
        L = mp.load_ledger()
        self.assertNotIn("OLD", L["holdings"])
        self.assertNotIn("LOCK", L["holdings"])               # locked at a circuit: skipped, as in the backtest
        self.assertIn("locked at a circuit", out)
        sell = [t for t in L["trades"] if t["side"] == "SELL"][0]
        self.assertAlmostEqual(sell["fill"], 600.0 * (1 - 0.0025), places=4)   # not in pit100 -> 0.25 %
        sv = 10 * sell["fill"]
        self.assertAlmostEqual(sell["net_pnl"], sv - sell["charges"] - 10 * 500.0 - 10.0, places=1)
        new = L["holdings"]["NEW"]
        px = 1000.0 * 1.0025
        self.assertEqual(new["qty"], int(50000.0 / (px * 1.0015)))
        cash = 95000.0 + sv - sell["charges"] - new["qty"] * px - new["buy_charges"]
        self.assertAlmostEqual(L["cash"], cash, places=1)
        self.assertIsNone(L["pending"])
        self.assertEqual(L["bench"]["date"], "2026-11-02")
        self.assertEqual(L["bench"]["^NSEI"], 100.0)

    def test_unfilled_sell_stays_pending(self):
        self.ledger({"OLD": {"qty": 5, "fill": 100.0, "entry_date": "2026-10-01", "buy_charges": 5.0,
                             "last": 100.0, "why": ""}},
                    {"signal_date": "2026-10-30", "fill_on": "2026-11-02", "equity_at_signal": 100000.0,
                     "sells": [{"symbol": "OLD", "why": "left"}], "buys": [], "top": []}, cash=99500.0)
        mp.day_bar = lambda sym, d: None
        self.run_cmd("fill", "--date", "2026-11-02")
        L = mp.load_ledger()
        self.assertIn("OLD", L["holdings"])
        self.assertEqual([o["symbol"] for o in L["pending"]["sells"]], ["OLD"])

    def test_fill_before_its_day_is_refused(self):
        self.ledger({}, {"signal_date": "2026-10-30", "fill_on": "2026-11-02", "equity_at_signal": 1.0,
                         "sells": [], "buys": [], "top": []}, cash=100000.0)
        rc, _, err = self.run_cmd("fill", "--date", "2026-10-30")
        self.assertEqual(rc, 2)
        self.assertIn("fill at the open of 2026-11-02", err)

    def test_never_imports_a_broker(self):
        with open(mp.__file__) as fh:
            src = fh.read()
        for word in ("dhan", "angel_broker", "ticket_core", "/orders", "placeOrder"):
            self.assertNotIn("import " + word, src)
        self.assertNotIn("placeOrder", src)
        self.assertNotIn("/orders", src)


if __name__ == "__main__":
    unittest.main()
