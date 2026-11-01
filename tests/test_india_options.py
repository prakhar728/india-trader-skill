"""india-backtest options tests. Offline, on real fixtures in fixtures/india-options/:

fo_bhav/<date>.json.gz        NSE F&O bhavcopy (UDiFF, nsearchives.nseindia.com/content/fo/), slimmed by
                              options.slim_bhav exactly as `options fetch` caches it: 2025-10-24, 2025-10-27,
                              2025-10-28 (a Nifty weekly expiry, lot 75), 2026-10-23, 2026-10-26,
                              2026-10-27 (a Nifty monthly expiry, lot 65), 2026-10-28. Downloaded 2026-10-29.
BhavCopy_NSE_FO_20261027-cut.csv.gz   real rows cut from the 2026-10-27 bhavcopy CSV (header kept as is):
                              NIFTY index options 22300-23200, SBIN stock options 900-990, a few others.
trading-days.json             every date with a bhavcopy from 2025-10-08 to 2026-10-28 (the trading calendar).
NIFTY-5m.json.gz              Angel SmartAPI getCandleData FIVE_MINUTE, Nifty 50 (token 99926000), those days.
INDIAVIX-5m.json.gz           the same for India VIX (token 99926017).
NIFTY03NOV2623000PE-5m.json.gz  Angel's real 5-minute bars (exchange NFO) of a live contract, 2026-10-23..28.
angel-scrip-cut.json          rows cut from Angel's OpenAPIScripMaster.json (2026-10-29).

Hand-checked trade, opt_orb default on NIFTY 2026-10-27 (a monthly expiry day):
  range 09:15-09:30 high 22746.40; the 09:40 bar closes 22758.65 (09:45) above it -> buy the ATM call
  NIFTY 2026-10-27 22750 CE (expiry day, dte 0). IV implied from its bhavcopy open 52.50 against
  Nifty's 09:15 open 22708.80 with 375 trading minutes left: 12.18 %. Model premium 73.09; + 0.5 %
  half-spread -> 73.50 (rounded up to the 0.05 tick). Rs 20,000 / (73.50 x 65) -> 4 lots (260).
  Stop 73.50 x 0.75 = 55.125 -> 55.10, target 110.25. The 10:20 bar (Nifty low 22684.55) takes the
  stop: 55.10 - 0.5 % -> 54.80 at 10:25. P&L (54.80 - 73.50) x 260 = -4,862.00; charges 83.22
  (Rs 40 brokerage, STT Rs 21, NSE txn 11.85, SEBI 0.03, stamp Rs 1, GST 9.34) -> -4,945.22.
"""

import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

import nse

bt = load("india-backtest", "backtest")
op = load("india-backtest", "options")
angel = load("india-common", "angel")

OF = os.path.join(FIX, "india-options")
BHAV = os.path.join(OF, "fo_bhav")
with open(os.path.join(OF, "trading-days.json")) as _fh:
    TRADING_DAYS = json.load(_fh)
CFG = {"capital": 100000.0, "alloc_pct": 20.0, "slip_pct": 0.5, "r": op.R_DEFAULT, "q": op.Q_DEFAULT,
       "vix_drift": False, "roll": False, "expiry_flat": "15:00", "flat_by": "15:05"}
_C = {}


def read(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as fh:
        return fh.read()


def store(name):
    return bt.read_store(os.path.join(OF, name))["bars"]


def nifty_days(roll=False):
    if roll not in _C:
        ss = bt.build_sessions("NIFTY", store("NIFTY-5m.json.gz"), None, None)[0]
        vx = bt.build_sessions("INDIAVIX", store("INDIAVIX-5m.json.gz"), None, None)[0]
        days, missing = op.build_days("NIFTY", ss, vx, BHAV)
        _C[roll] = days
    return _C[roll]


def engine(**over):
    cfg = dict(CFG, **over)
    return op.Engine("NIFTY", nifty_days(), op.Clock(TRADING_DAYS), cfg)


def day(date):
    return [d for d in nifty_days() if d.date == date][0]


class Bhavcopy(unittest.TestCase):
    def setUp(self):
        self.text = gzip.decompress(read(os.path.join(OF, "BhavCopy_NSE_FO_20261027-cut.csv.gz"), "rb")).decode()

    def test_parse_keeps_option_rows_only(self):
        rows = op.parse_bhav(self.text)
        self.assertTrue(rows)
        self.assertEqual(set(r["kind"] for r in rows), {"IDO", "STO"})          # IDF / STF futures dropped
        r = [x for x in rows if x["sym"] == "NIFTY" and x["expiry"] == "2026-10-27" and x["strike"] == 22750
             and x["type"] == "CE"][0]
        self.assertEqual((r["open"], r["high"], r["low"], r["close"], r["und"], r["lot"]),
                         (52.5, 116.35, 0.05, 30.9, 22749.75, 65))
        self.assertEqual(len(op.parse_bhav(self.text, ["SBIN"])), sum(1 for x in rows if x["sym"] == "SBIN"))

    def test_zip_round_trip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("BhavCopy_NSE_FO_0_0_0_20261027_F_0000.csv", self.text)
        self.assertEqual(op.unzip_bhav(buf.getvalue()), self.text)
        self.assertEqual(op.bhav_url(datetime.date(2026, 10, 27)),
                         "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_20261027_F_0000.csv.zip")

    def test_slim_keeps_near_and_monthly_expiries_and_strikes_around_the_underlying(self):
        rows = op.parse_bhav(self.text, ["NIFTY", "SBIN"])
        doc = op.slim_bhav(rows, "2026-10-27", ["NIFTY", "SBIN"], each_side=3)
        ch = op.Chain(doc)
        # nearest two (10-27, 11-03) + October's and November's monthly (10-27, 11-23); not 11-09, 12-29
        self.assertEqual(ch.exp["NIFTY"], ["2026-10-27", "2026-11-03", "2026-11-23"])
        self.assertEqual(ch.strikes[("NIFTY", "2026-10-27")],
                         [22600.0, 22650.0, 22700.0, 22750.0, 22800.0, 22850.0, 22900.0])
        self.assertEqual(ch.exp["SBIN"], ["2026-10-27", "2026-11-23"])
        self.assertEqual(doc["symbols"], ["NIFTY", "SBIN"])

    def test_keep_expiries_rule(self):
        ex = ["2026-10-27", "2026-11-03", "2026-11-09", "2026-11-17", "2026-11-23", "2026-12-01", "2026-12-29"]
        self.assertEqual(op.keep_expiries(ex, True), {"2026-10-27", "2026-11-03", "2026-11-23"})
        self.assertEqual(op.keep_expiries(ex, False), {"2026-10-27", "2026-11-03"})

    def test_fetch_is_polite_resumable_and_remembers_holidays(self):
        tmp = tempfile.mkdtemp()
        old = os.environ.get("TRADER_DIR")
        os.environ["TRADER_DIR"] = os.path.join(tmp, "trader")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("BhavCopy_NSE_FO_0_0_0_20261027_F_0000.csv", self.text)
        calls, sleeps = [], []

        def http(url):
            calls.append(url)
            if "20261027" in url:
                return buf.getvalue()
            raise nse.NSEError("HTTP 404 from nsearchives.nseindia.com")
        try:
            now = datetime.datetime(2026, 10, 29, 18, 0)
            dates = [datetime.date(2026, 10, 20), datetime.date(2026, 10, 24), datetime.date(2026, 10, 27),
                     datetime.date(2026, 10, 29)]
            res = op.fetch_bhav(dates, ["NIFTY"], now, http=http, sleep=sleeps.append, log=lambda m: None)
            self.assertEqual(len(calls), 3)                        # Saturday 10-24 never asked for
            self.assertEqual(res, {"fetched": 1, "cached": 0, "missing": 2, "errors": 0})
            miss = nse.read_json(os.path.join(op.bhav_dir(), "_missing.json"))
            self.assertEqual(list(miss), ["2026-10-20"])           # today's 404 is not remembered
            self.assertTrue(all(s <= op.BHAV_GAP for s in sleeps))
            doc = op.read_gz_json(op.bhav_path("2026-10-27"))
            self.assertEqual(doc["symbols"], ["NIFTY"])
            self.assertFalse([f for f in os.listdir(op.bhav_dir()) if f.endswith(".zip")])
            calls.clear()
            res = op.fetch_bhav(dates, ["NIFTY"], now, http=http, sleep=sleeps.append, log=lambda m: None)
            self.assertEqual(calls, [op.bhav_url(datetime.date(2026, 10, 29))])
            self.assertEqual(res["cached"], 1)
        finally:
            if old is None:
                del os.environ["TRADER_DIR"]
            else:
                os.environ["TRADER_DIR"] = old
            shutil.rmtree(tmp)


class ChainAndExpiry(unittest.TestCase):
    def test_expiry_day_trades_the_expiring_contract_unless_rolling(self):
        ch = op.load_chain("2026-10-27", BHAV)
        self.assertEqual(ch.nearest("NIFTY"), "2026-10-27")
        self.assertEqual(ch.nearest("NIFTY", roll=True), "2026-11-03")
        ch2 = op.load_chain("2026-10-28", BHAV)
        self.assertEqual(ch2.nearest("NIFTY"), "2026-11-03")
        self.assertEqual(op.load_chain("2025-10-28", BHAV).nearest("NIFTY"), "2025-10-28")

    def test_atm_is_the_nearest_listed_strike(self):
        ch = op.load_chain("2026-10-27", BHAV)
        self.assertEqual(ch.atm("NIFTY", "2026-10-27", 22758.65), 22750.0)
        self.assertEqual(ch.atm("NIFTY", "2026-10-27", 22776.0), 22800.0)
        self.assertEqual(ch.atm("NIFTY", "2026-10-27", 22775.0), 22750.0)    # a tie goes to the lower strike
        self.assertIsNone(ch.atm("NIFTY", "2030-01-01", 22000))

    def test_lot_size_comes_from_the_bhavcopy(self):
        self.assertEqual(op.load_chain("2025-10-28", BHAV).row("NIFTY", "2025-10-28", 26000, "CE")["lot"], 75)
        self.assertEqual(op.load_chain("2026-10-27", BHAV).row("NIFTY", "2026-10-27", 22750, "PE")["lot"], 65)

    def test_trading_clock(self):
        c = op.Clock(TRADING_DAYS)
        d = datetime.date
        self.assertFalse(c.is_trading(d(2026, 10, 20)))           # Dussehra
        self.assertFalse(c.is_trading(d(2025, 10, 22)))           # 2025: from the bhavcopy dates, not nse.py
        self.assertEqual(c.days_between(d(2026, 10, 16), d(2026, 10, 27)), 5)
        self.assertEqual(c.minutes(d(2026, 10, 27), 9 * 60 + 15, d(2026, 10, 27)), 375)
        self.assertEqual(c.minutes(d(2026, 10, 27), 15 * 60, d(2026, 10, 27)), 30)
        self.assertEqual(c.minutes(d(2026, 10, 26), 9 * 60 + 15, d(2026, 10, 27)), 750)
        self.assertEqual(c.minutes(d(2026, 10, 28), 600, d(2026, 10, 27)), 0)
        self.assertEqual(c.years(d(2026, 10, 27), 15 * 60 + 40, d(2026, 10, 27)), 1 / 94500.0)  # floor 1 minute
        self.assertEqual((c.dte(d(2026, 10, 27), d(2026, 10, 27)), c.dte(d(2026, 10, 28), d(2026, 11, 3))), (0, 4))


class BlackScholes(unittest.TestCase):
    def test_textbook_vector(self):
        # Hull, Options Futures and Other Derivatives, example 15.6: S 42, K 40, r 10 %, vol 20 %, 6 months
        self.assertAlmostEqual(op.bs_price(42, 40, 0.5, 0.2, 0.10, 0.0, "CE"), 4.76, places=2)
        self.assertAlmostEqual(op.bs_price(42, 40, 0.5, 0.2, 0.10, 0.0, "PE"), 0.81, places=2)

    def test_put_call_parity_with_dividend_yield(self):
        import math
        for S, K, T, v in ((22750, 22750, 5 / 252.0, 0.12), (965.0, 980.0, 0.08, 0.25), (26000, 25500, 0.002, 0.3)):
            c = op.bs_price(S, K, T, v, 0.055, 0.012, "CE")
            p = op.bs_price(S, K, T, v, 0.055, 0.012, "PE")
            self.assertAlmostEqual(c - p, S * math.exp(-0.012 * T) - K * math.exp(-0.055 * T), places=6)

    def test_expired_is_intrinsic(self):
        self.assertEqual(op.bs_price(22800, 22750, 0, 0.2, typ="CE"), 50)
        self.assertEqual(op.bs_price(22800, 22750, 0, 0.2, typ="PE"), 0)

    def test_implied_vol_round_trip(self):
        for S, K, T, v, typ in ((22708.8, 22750, 375 / 94500.0, 0.1218, "CE"), (22708.8, 22650, 0.01, 0.18, "PE"),
                                (970.0, 1000.0, 0.07, 0.31, "CE"), (970.0, 940.0, 0.07, 0.27, "PE")):
            price = op.bs_price(S, K, T, v, typ=typ)
            self.assertAlmostEqual(op.implied_vol(price, S, K, T, typ=typ), v, places=5)

    def test_implied_vol_outside_bounds_is_none(self):
        self.assertIsNone(op.implied_vol(40.0, 22800, 22750, 0.01, typ="CE"))   # under intrinsic
        self.assertIsNone(op.implied_vol(0, 22800, 22750, 0.01))
        self.assertIsNone(op.implied_vol(30000, 22800, 22750, 0.01))


class OptionCharges(unittest.TestCase):
    def test_after_april_2026(self):
        buy, sell = 19110.0, 14248.0                              # the hand-checked trade (73.50 / 54.80 x 260)
        c = nse.option_charges(buy, sell, on="2026-10-27")
        turnover = buy + sell
        exch = turnover * 0.03553 / 100
        sebi = turnover * 0.0001 / 100
        gst = 0.18 * (40 + exch + sebi + turnover * 1e-9)
        self.assertEqual((c["brokerage"], c["stt"], c["stamp"]), (40.0, 21.0, 1.0))  # 0.15 % of 14,248 = 21.37
        self.assertAlmostEqual(c["exchange"], exch, places=2)
        self.assertAlmostEqual(c["gst"], gst, places=2)
        self.assertEqual(c["total"], 83.22)

    def test_stt_was_0_1_pct_before_april_2026(self):
        self.assertEqual(nse.option_charges(10000, 12000, on="2026-03-31")["stt"], 12.0)
        self.assertEqual(nse.option_charges(10000, 12000, on="2026-04-01")["stt"], 18.0)
        self.assertEqual(nse.option_charges(10000, 12000)["stt"], 18.0)

    def test_charges_on_premium_not_notional_and_brokerage_flat(self):
        a = nse.option_charges(1000, 0)                           # expired worthless: still both orders' brokerage
        self.assertEqual((a["brokerage"], a["stt"]), (40.0, 0.0))
        self.assertEqual(nse.option_charges(1000, 1000, orders=1)["brokerage"], 20.0)
        self.assertLess(nse.option_charges(50000, 50000)["total"], 200)


class Clamp(unittest.TestCase):
    def test_clamp_path_clips_into_the_real_day_range(self):
        p = [(10.0, 12.0, 9.0, 11.0), None, (5.0, 20.0, 1.0, 15.0)]
        self.assertEqual(op.clamp_path(p, 8.0, 14.0), [(10.0, 12.0, 9.0, 11.0), None, (8.0, 14.0, 8.0, 14.0)])
        self.assertEqual(op.clamp_path(p, 0, 0), p)                # no range known: unchanged

    def _exits(self, path, row, p, clamped):
        eng = engine()
        d = day("2026-10-28")
        sig = {"k": 0, "side": "CE", "level": 0.0}
        return eng.exits(path if not clamped else op.clamp_path(path, row["low"], row["high"]), 0, 6, p, d, sig, row,
                         datetime.date(2026, 11, 3), 0.15, clamped)

    def test_target_above_the_real_high_is_not_hit(self):
        path = [(100, 100, 100, 100), (100, 120, 98, 118), (118, 160, 117, 158), (158, 159, 140, 150)] + [None] * 3
        row = {"low": 90.0, "high": 140.0, "strike": 22500, "type": "CE"}
        p = {"stop_pct": 25, "target_pct": 50}
        raw = self._exits(path, row, p, False)
        self.assertEqual((raw["entry"], raw["target"], raw["outcome"], raw["exit"]), (100.5, 150.75, "target", 150.75))
        cl = self._exits(path, row, p, True)
        self.assertEqual(cl["outcome"], "flat")                    # the real day never traded at 150.75
        self.assertEqual(cl["exit"], 139.3)                        # 140 (clamped close) - 0.5 % half-spread

    def test_stop_below_the_real_low_cannot_fill_there(self):
        path = [(100, 100, 100, 100), (100, 101, 60, 70), (70, 72, 55, 60)] + [None] * 4
        row = {"low": 80.0, "high": 110.0, "strike": 22500, "type": "CE"}
        p = {"stop_pct": 35, "target_pct": 50}
        raw = self._exits(path, row, p, False)
        self.assertEqual((raw["stop"], raw["outcome"]), (65.3, "stop"))
        cl = self._exits(path, row, p, True)
        self.assertEqual(cl["outcome"], "flat")
        self.assertEqual(cl["exit"], 79.6)

    def test_bar_touching_both_is_the_stop_and_a_gap_fills_at_the_open(self):
        row = {"low": 1.0, "high": 999.0, "strike": 22500, "type": "CE"}
        p = {"stop_pct": 25, "target_pct": 50}
        both = [(100, 100, 100, 100), (100, 160, 70, 120)] + [None] * 5
        self.assertEqual(self._exits(both, row, p, True)["outcome"], "stop")
        gap = [(100, 100, 100, 100), (60, 65, 55, 62)] + [None] * 5
        g = self._exits(gap, row, p, True)
        self.assertEqual((g["outcome"], g["exit"]), ("stop_gap", 59.7))

    def test_trailing_stop(self):
        row = {"low": 1.0, "high": 999.0, "strike": 22500, "type": "CE"}
        p = {"stop_pct": 25, "target_pct": None, "trail_after": 30, "trail_pct": 20}
        path = [(100, 100, 100, 100), (100, 140, 99, 138), (138, 139, 110, 112)] + [None] * 4
        t = self._exits(path, row, p, True)
        self.assertEqual(t["outcome"], "trail_stop")               # peak 140 -> trail at 112
        self.assertEqual(t["exit"], 111.4)


class HandChecked(unittest.TestCase):
    def setUp(self):
        self.eng = engine()
        self.d = day("2026-10-27")

    def test_signal(self):
        s = self.d.sess
        self.assertEqual(bt.opening_range(s, 3), (22746.4, 22653.2))
        sig = op.sig_orb(s, {"or_minutes": 15}, self.eng.win)
        self.assertEqual((sig["k"], sig["side"], bt.clock(bt.slot_end(sig["k"]))), (5, "CE", "09:45"))
        self.assertEqual(s.bars[5]["c"], 22758.65)

    def test_trade_end_to_end(self):
        sig = op.sig_orb(self.d.sess, {"or_minutes": 15}, self.eng.win)
        t, why = self.eng.trade(self.d, sig, op.STRATS["opt_orb"]["default"])
        self.assertIsNone(why)
        self.assertEqual((t["contract"], t["expiry_day"], t["dte"], t["iv_source"], t["lot"]),
                         ("NIFTY 2026-10-27 22750 CE", True, 0, "bhav_open", 65))
        iv = op.implied_vol(52.5, 22708.8, 22750, 375 / 94500.0, 0.055, 0.012, "CE")
        self.assertAlmostEqual(t["iv"], iv, places=4)
        self.assertAlmostEqual(iv, 0.1218, places=4)
        self.assertEqual((t["model_premium"], t["entry"], t["lots"], t["qty"], t["stop"], t["target"]),
                         (73.09, 73.5, 4, 260, 55.1, 110.25))
        self.assertEqual((t["outcome"], t["exit"], t["exit_time"]), ("stop", 54.8, "10:25"))
        self.assertEqual(self.d.sess.bars[13]["l"], 22684.55)
        self.assertEqual((t["pnl"], t["charges"], t["pnl_net"]), (-4862.0, 83.22, -4945.22))
        self.assertEqual(t["charges"], nse.option_charges(73.5 * 260, 54.8 * 260, on="2026-10-27")["total"])
        self.assertEqual((t["real_low"], t["real_high"], t["clamp_changed"]), (0.05, 116.35, False))
        self.assertAlmostEqual(t["pnl_per_lot_net"], (54.8 - 73.5) * 65 - t["charges_per_lot"], places=2)

    def test_sizing_whole_lots_or_skip(self):
        sig = op.sig_orb(self.d.sess, {"or_minutes": 15}, self.eng.win)
        small = engine(alloc_pct=5)                                # Rs 5,000 buys one lot (73.50 x 65 = 4,777.50)
        t, _ = small.trade(self.d, sig, op.STRATS["opt_orb"]["default"])
        self.assertEqual(t["lots"], 1)
        tiny = engine(alloc_pct=4)                                 # Rs 4,000 < Rs 4,777.50: skipped, still per lot
        t, _ = tiny.trade(self.d, sig, op.STRATS["opt_orb"]["default"])
        self.assertEqual((t["lots"], t["pnl_net"]), (0, 0.0))
        self.assertIn("more than 4 %", t["skipped"])
        self.assertLess(t["pnl_per_lot_net"], 0)

    def test_roll_on_expiry_trades_next_week(self):
        eng = engine(roll=True)
        sig = op.sig_orb(self.d.sess, {"or_minutes": 15}, eng.win)
        t, _ = eng.trade(self.d, sig, op.STRATS["opt_orb"]["default"])
        self.assertEqual((t["expiry"], t["expiry_day"], t["dte"]), ("2026-11-03", False, 5))

    def test_no_expiry_strategy_skips_expiry_days(self):
        eng = engine()
        tr, so, sk = op.run_strategy(eng, "opt_no_expiry", op.STRATS["opt_no_expiry"]["default"])
        tr2, _, _ = op.run_strategy(eng, "opt_orb", op.STRATS["opt_orb"]["default"])
        self.assertIn("2026-10-27", [t["date"] for t in tr2])
        self.assertNotIn("2026-10-27", [t["date"] for t in tr])
        self.assertNotIn("2025-10-28", [t["date"] for t in tr])
        self.assertTrue(all(not t["expiry_day"] for t in tr))

    def test_pdh_pdl_signal(self):
        s = day("2026-10-28").sess
        sig = op.sig_pdh_pdl(s, {}, self.eng.win)
        self.assertIsNotNone(sig)
        k = sig["k"]
        if sig["side"] == "PE":
            self.assertLess(s.bars[k]["c"], s.prev["low"])
        else:
            self.assertGreater(s.bars[k]["c"], s.prev["high"])


class Metrics(unittest.TestCase):
    def T(self, date, pnl, **kw):
        t = {"date": date, "time": "09:40", "pnl_net": pnl, "pnl": pnl + 50, "ret_pct": 1.0, "ret_pct_net": pnl / 200.0,
             "ret_pct_mid": 1.5, "charges": 50.0, "premium": 20000.0, "lots": 2, "outcome": "stop", "side": "CE",
             "expiry_day": False, "clamp_changed": False, "iv_source": "bhav_open"}
        t.update(kw)
        return t

    def test_account_drawdown_streak_and_ruin_windows(self):
        dates = ["2026-01-%02d" % i for i in range(1, 31)]
        trades = [self.T(d, -5000.0) for d in dates[:7]] + [self.T(d, 1000.0) for d in dates[7:]]
        m = op.opt_metrics(trades, dates, 100000.0)
        self.assertEqual(m["max_dd_inr"], 35000.0)
        self.assertEqual(m["max_dd_pct"], 35.0)
        self.assertEqual(m["longest_losing_streak"], 7)
        self.assertEqual(m["worst_day"], {"date": "2026-01-01", "inr": -5000.0})
        # 11 windows of 20 sessions; a fresh Rs 1L account falls more than 30 % only in the window from day 1
        self.assertEqual((m["ruin_windows"], m["ruin_windows_fell"]), (11, 1))
        self.assertEqual(m["worst_20d_loss_pct"], 35.0)
        self.assertIsNone(m["blown_on"])

    def test_buckets(self):
        trades = [self.T("2026-01-02", 100.0, expiry_day=True, side="PE"), self.T("2026-02-03", -300.0)]
        m = op.opt_metrics(trades, ["2026-01-02", "2026-02-03"], 100000.0)
        self.assertEqual([(b["bucket"], b["trades"]) for b in m["by_expiry_day"]], [("expiry_day", 1), ("other_day", 1)])
        self.assertEqual([(b["bucket"], b["avg_inr_net"]) for b in m["by_side"]], [("CE", -300.0), ("PE", 100.0)])
        self.assertEqual([b["bucket"] for b in m["by_month"]], ["2026-01", "2026-02"])

    def test_verdicts(self):
        base = {"trades": 250, "avg_inr_gross": -500.0, "avg_inr_net": -600.0, "avg_ret_pct_before_costs": -2.0,
                "ruin_windows_pct": 40.0, "blown_on": "2026-03-01", "t_stat": -3.0}
        v = op.opt_verdict(base, {"trades": 50, "avg_inr_net": -100.0}, 100000)
        self.assertTrue(v.startswith("no edge, even before costs"))
        self.assertIn("40 % of 20-session windows", v)
        self.assertIn("wiped out by 2026-03-01", v)
        v = op.opt_verdict(dict(base, avg_ret_pct_before_costs=1.0, blown_on=None, ruin_windows_pct=0), {}, 100000)
        self.assertTrue(v.startswith("no edge after costs"))
        self.assertEqual(op.opt_verdict({"trades": 12}, {}, 100000), "too few trades to judge (12)")

    def test_walk_forward_splits_by_expiry(self):
        is_dates, info = op.split_by_expiry(nifty_days())
        self.assertEqual(info["expiries"], len(set(d.expiry for d in nifty_days())))
        self.assertTrue(all(d.expiry <= info["cut_expiry"] for d in nifty_days() if d.date in is_dates))


class Backtest(unittest.TestCase):
    def test_backtest_and_dash_rows(self):
        eng = engine()
        res, split = op.backtest_underlying(eng, ["opt_orb", "opt_pdh_pdl"])
        r = res["opt_orb"]
        self.assertEqual(len(r["variants"]), len(op.STRATS["opt_orb"]["variants"]))
        self.assertGreaterEqual(r["full"]["trades"], 5)
        doc = {"made": "2026-10-29T18:00:00+05:30", "ranking": op.ranking(res), "strategies": res,
               "meta": {"underlying": "NIFTY", "from": "2025-10-24", "to": "2026-10-28", "expiries": 5,
                        "caveats": ["x"]}}
        d = op.dash([doc], {"contracts": [{"contract": "c", "sessions": 1, "bars": 2, "median_abs_err_pct": 3.0,
                                            "hit_agreement_pct": 100.0}],
                            "summary": {"contract_days": 1, "bars": 2, "median_abs_err_pct": 3.0,
                                        "hit_agreement_pct": 100.0}}, doc["made"])
        self.assertEqual(set(d), {"made", "opt_strategies", "opt_equity", "opt_by_bucket", "opt_validation", "opt_meta"})
        row = [x for x in d["opt_strategies"] if x["strategy"] == "opt_orb"][0]
        for k in ("id", "underlying", "strategy", "title", "rules", "trades", "win_pct", "avg_ret_pct_net",
                  "avg_inr_net", "total_inr_net", "profit_factor", "max_dd_inr", "max_dd_pct", "ruin_windows_pct",
                  "expiry_day_avg_inr", "other_day_avg_inr", "oos_avg_inr_net", "clamp_changed_pct", "verdict"):
            self.assertIn(k, row)
        self.assertEqual(row["id"], "NIFTY-opt_orb")
        self.assertEqual(set(b["bucket_type"] for b in d["opt_by_bucket"]), {"expiry_day", "side", "month"})
        self.assertEqual(d["opt_validation"][-1]["contract"], "ALL")
        self.assertEqual(len(d["opt_meta"]), 1)
        for k in ("period", "expiries", "data_sources", "assumptions", "charges_model", "caveats"):
            self.assertIn(k, d["opt_meta"][0])


class ResearchHoldout(unittest.TestCase):
    """bhav:nifty-weekly's holdout is withheld from `options run` on NIFTY unless --allow-holdout."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.old = os.environ.get("TRADER_DIR")
        td = os.environ["TRADER_DIR"] = os.path.join(self.dir, "trader")
        angel_dir = os.path.join(td, "cache", "bars", "angel")
        os.makedirs(angel_dir)
        for n in ("NIFTY", "INDIAVIX"):
            shutil.copyfile(os.path.join(OF, "%s-5m.json.gz" % n), os.path.join(angel_dir, "%s-5m.json.gz" % n))
        shutil.copytree(BHAV, os.path.join(td, "cache", "fo_bhav"))
        nse.write_json(os.path.join(td, "research", "splits.json"), {
            "bhav:nifty-weekly": {"explore": ["2025-10-24", "2025-10-28"], "validate": ["2026-10-23", "2026-10-23"],
                                  "holdout": ["2026-10-26", "2026-10-28"], "symbols": ["NIFTY"]}})

    def tearDown(self):
        if self.old is None:
            os.environ.pop("TRADER_DIR", None)
        else:
            os.environ["TRADER_DIR"] = self.old
        shutil.rmtree(self.dir)

    def run_json(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = op.main(["run", "--underlying", "NIFTY", "--strategy", "opt_orb", "--json", "--no-write",
                            "--now", "2026-10-29 18:00"] + list(extra))
        self.assertEqual(code, 0, err.getvalue())
        return json.loads(out.getvalue()), err.getvalue()

    def test_withheld_by_default_and_allowed_on_request(self):
        doc, err = self.run_json()
        m = doc["meta"]
        self.assertEqual((m["from"], m["to"], m["sessions"]), ("2025-10-24", "2026-10-23", 4))
        self.assertFalse(m["holdout_included"])
        self.assertEqual(m["holdout_withheld"]["withheld_sessions"], 3)
        self.assertIn("NIFTY: Holdout: withheld 3 session(s)", err)
        doc, err = self.run_json("--allow-holdout")
        m = doc["meta"]
        self.assertEqual((m["to"], m["sessions"]), ("2026-10-28", 7))
        self.assertTrue(m["holdout_included"])
        self.assertIn("INCLUDED", err)


class Validation(unittest.TestCase):
    def test_model_vs_real_bars_of_a_live_contract(self):
        info = {"name": "NIFTY", "expiry": "2026-11-03", "strike": 23000.0, "type": "PE",
                "symbol": "NIFTY03NOV2623000PE", "token": "x"}
        ss = bt.build_sessions("NIFTY", store("NIFTY-5m.json.gz"), None, None)[0]
        vx = bt.build_sessions("INDIAVIX", store("INDIAVIX-5m.json.gz"), None, None)[0]
        r = op.validate_contract(info, store("NIFTY03NOV2623000PE-5m.json.gz"), dict((s.date, s) for s in ss),
                                 dict((s.date, s.bars) for s in vx), op.Clock(TRADING_DAYS), base=BHAV)
        self.assertEqual(r["sessions"], 4)                        # 10-23, 10-26, 10-27, 10-28
        self.assertGreater(r["bars"], 100)
        s = op.summarize_contract(r)
        self.assertLess(s["median_abs_err_pct"], 25)
        self.assertGreater(r["trials"], 0)
        v = op.summarize_validation([r])
        self.assertEqual(v["bars"], r["bars"])
        self.assertTrue(v["by_moneyness"] and v["by_dte"] and v["by_time_of_day"])

    def test_outcome_on(self):
        bars = [(100, 100, 100, 100), (100, 120, 90, 110), (110, 151, 105, 150)]
        self.assertEqual(op.outcome_on(bars, 0, 2), ("target", 50.0))
        self.assertEqual(op.outcome_on([(100,) * 4, (100, 101, 74, 80)], 0, 1), ("stop", -25.0))
        self.assertEqual(op.outcome_on([(100,) * 4, (100, 101, 99, 90)], 0, 1)[0], "flat")

    def test_pick_contracts(self):
        rows = angel.parse_nfo_options(read(os.path.join(OF, "angel-scrip-cut.json")))
        picks = op.pick_validation_contracts(rows, "SBIN", 971.9, "2026-10-29")
        self.assertEqual(sorted(set(o["strike"] for o in picks)), [960.0, 970.0, 980.0])
        self.assertEqual(set(o["type"] for o in picks), {"CE", "PE"})


class AngelNFO(unittest.TestCase):
    def test_parse_nfo_options(self):
        rows = angel.parse_nfo_options(read(os.path.join(OF, "angel-scrip-cut.json")))
        self.assertTrue(rows)
        self.assertEqual(set(r["kind"] for r in rows), {"OPTIDX", "OPTSTK"})   # futures, BFO, NSE rows dropped
        r = [x for x in rows if x["symbol"] == "NIFTY03NOV2623000PE"][0]
        self.assertEqual((r["name"], r["expiry"], r["strike"], r["type"], r["lot"], r["tick"]),
                         ("NIFTY", "2026-11-03", 23000.0, "PE", 65, 0.05))
        self.assertEqual(angel.parse_nfo_options(read(os.path.join(OF, "angel-scrip-cut.json")), ["SBIN"])[0]["name"],
                         "SBIN")

    def test_index_tokens(self):
        rows = json.loads(read(os.path.join(OF, "angel-scrip-cut.json")))
        names = dict((r["token"], r["symbol"]) for r in rows if r["exch_seg"] == "NSE")
        self.assertEqual(names[angel.INDEX_TOKENS["NIFTY"]], "Nifty 50")
        self.assertEqual(names[angel.INDEX_TOKENS["INDIAVIX"]], "India VIX")
        self.assertEqual(op.INDEX_TOKENS, angel.INDEX_TOKENS)


class Safety(unittest.TestCase):
    def test_read_only(self):
        src = read(op.__file__)
        for bad in ("client.place", ".place(", ".modify(", ".cancel(", "placeOrder", "www.nseindia.com/api"):
            self.assertNotIn(bad, src)

    def test_cli(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = bt.main(["options", "list"])
        self.assertEqual(code, 0)
        for n in op.ORDER:
            self.assertIn(n + ":", out.getvalue())
        self.assertEqual(op.parse_strats("orb,opt_pdh_pdl"), ["opt_orb", "opt_pdh_pdl"])
        with self.assertRaises(op.OptError):
            op.parse_strats("magic")


if __name__ == "__main__":
    unittest.main()
