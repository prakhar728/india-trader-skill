"""india-backtest tests. Offline, on real Yahoo fixtures:

common/yahoo-RELIANCE.NS-60d5m.json.gz               RELIANCE, 60 sessions of 5-minute bars to 2026-10-29
india-timemachine/yahoo-HDFCBANK.NS-60d5m.json.gz    HDFCBANK, same range
india-backtest/yahoo-<SYM>.NS-60d5m.json.gz          TCS INFY ICICIBANK SBIN TATAPOWER TATASTEEL ITC AXISBANK
                                                     BHARTIARTL LT: 60 sessions of 5-minute bars to 2026-10-29
india-backtest/yahoo-<SYM>.NS-1y1d.json.gz           the same ten plus RELIANCE and HDFCBANK: a year of daily bars
    all fetched 2026-10-29 after 16:50 IST from query1.finance.yahoo.com/v8/finance/chart with
    UA "Mozilla/5.0 (compatible; india-trader-skill/1.0)", 1.5 s apart, gzipped as received.

Angel One responses cannot be recorded here (no network in tests): the Angel test builds
getCandleData answers in the documented [[time, o, h, l, c, v], ...] shape from the same real
RELIANCE bars, and checks the chunking, pacing, resume and that only the candle route is used.

Hand-checked trades (slippage 1 tick, india-budget sizing, Rs 1,00,000 capital, Rs 500 max loss):
  orb15           RELIANCE 2026-10-29: range 1170.50/1156.20, mid 1163.35 -> stop 1163.30; the 09:35 bar
                  closes 1174.30 (09:40) -> fill 1174.40; target 1174.30 + 2 x 11.00 = 1196.30; neither hit;
                  out at the 15:00 bar's close 1169.30 - 0.10 = 1169.20 (15:05); 21 shares (25 % cap).
  orb30           RELIANCE 2026-08-31: 30-min range 1278.20/1271.10 -> stop 1274.60; 10:25 bar closes 1278.70;
                  target 1286.90 hit by the 11:55 bar (high 1286.90).
  pdh             RELIANCE 2026-08-06: prior high 1299.00; 09:25 bar closed 1297.70, 09:30 bar 1300.40 ->
                  entry 09:35; stop = low of the last 3 bars 1295.00; target 1311.20 hit at 11:20.
  gap_go          TCS 2026-09-30: prior close 2032.40, open 2068.60 (+1.8 %); range low 2055.00 held;
                  09:55 bar closes 2090.00 > 2087.70; stop 2055.00; out at 2054.90 on the 14:25 bar.
  vwap_pullback   RELIANCE 2026-08-06: the 10:10 bar touches VWAP, the 10:20 bar closes back above it at
                  1300.40; pullback low 1298.20 -> stop 1298.10; target 1305.00 hit at 11:00.
  orb_pullback    RELIANCE 2026-08-31: breakout at 10:30, retest, the 11:15 bar closes 1278.30 > 1278.20;
                  stop 1274.60 (mid); target 1285.70 hit at 11:55.
  oversold_bounce TATAPOWER 2026-09-02: daily RSI 25.6; 09:35 bar closes 352.50 > 351.30; stop 347.40
                  (0.05 tick); target 362.70 hit at 14:50.
  gap through the stop: TATAPOWER 2026-09-08 orb15: stop 363.95, the 13:50 bar opens 363.90 -> 363.85.
"""

import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, COMMON

import nse

bt = load("india-backtest", "backtest")
tm = load("india-timemachine", "timemachine")

BTF = os.path.join(FIX, "india-backtest")
DIRS = [COMMON, os.path.join(FIX, "india-timemachine"), BTF]
NOW = datetime.datetime(2026, 10, 29, 17, 0)
SYMS = ["RELIANCE", "HDFCBANK", "TCS", "INFY", "ICICIBANK", "SBIN", "TATAPOWER", "TATASTEEL", "ITC",
        "AXISBANK", "BHARTIARTL", "LT"]
WIN = bt.Window({})
# the time machine's history sizes qty = max loss / risk, uncapped, with no charges in the size
TM_SIZING = {"capital": 1e9, "position_cap_pct": 0, "max_order_value": 0, "cash_buffer_pct": 0,
             "include_charges": False}
_CACHE = {}


def sessions(sym, daily=True):
    key = (sym, daily)
    if key not in _CACHE:
        b5, b1, _ = bt.load_offline(DIRS, sym)
        _CACHE[key] = bt.build_sessions(sym, b5, b1 if daily else None, NOW)[0]
    return _CACHE[key]


def trade_on(sym, name, date, params=None, cfg=None, slip=1):
    tr, _ = bt.run_symbol(sessions(sym), name, params or bt.STRATEGIES[name]["default"], WIN, cfg or {}, slip)
    hit = [t for t in tr if t["date"] == date]
    return hit[0] if hit else None


def bar(o, h, l, c, v=1000):
    return {"o": o, "h": h, "l": l, "c": c, "v": v}


def flat_session(price=500.0, n=75, date="2026-10-01", **over):
    """A synthetic session: n bars at a flat price; over={slot: bar} replaces some."""
    bars = [bar(price, price + 0.5, price - 0.5, price) if k < n else None for k in range(bt.SLOTS)]
    for k, b in over.items():
        bars[int(k[1:])] = b
    return bt.Session("TEST", date, bars, {"date": "2026-09-30", "high": price + 5, "low": price - 5,
                                           "close": price}, 50.0)


def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = bt.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Registry(unittest.TestCase):
    def test_seven_strategies_with_rules(self):
        self.assertEqual(bt.ORDER, ["orb15", "orb30", "pdh", "gap_go", "vwap_pullback", "orb_pullback",
                                    "oversold_bounce"])
        for n in bt.ORDER:
            s = bt.STRATEGIES[n]
            self.assertIn(s["default"], s["variants"], n)
            self.assertGreater(len(s["rules"]), 80, n)

    def test_list_prints_every_rule(self):
        code, out, _ = cli("list")
        self.assertEqual(code, 0)
        for n in bt.ORDER:
            self.assertIn(n + ":", out)
        self.assertIn("range midpoint", out)
        self.assertIn("RSI(14", out)

    def test_parse_strategies(self):
        self.assertEqual(bt.parse_strategies("all"), bt.ORDER)
        self.assertEqual(bt.parse_strategies("orb15,PDH,orb15"), ["orb15", "pdh"])
        with self.assertRaises(bt.BTError):
            bt.parse_strategies("magic")


class TimeMachineConsistency(unittest.TestCase):
    """The time machine on 2026-10-29's plan reported: 28 past fires, 8 target, 13 stop, 7 neither,
    +0.156R gross, -0.031R after charges. Same rules here: orb15, stop at the range midpoint, 2R,
    no slippage, sessions before 2026-10-29, the time machine's sizing (max loss / risk)."""

    def setUp(self):
        ss = [s for s in sessions("RELIANCE", daily=False) if s.date < "2026-10-29"]
        self.ss = ss
        self.trades, _ = bt.run_symbol(ss, "orb15", {"or_minutes": 15, "stop": "or_mid", "r": 2.0}, WIN,
                                       TM_SIZING, 0)

    def test_reproduces_the_counts_and_R(self):
        m = bt.metrics(self.trades, len(self.ss), len(self.ss))
        self.assertEqual(len(self.ss), 59)
        self.assertEqual(m["trades"], 28)
        self.assertEqual((m["outcomes"]["target"], m["outcomes"]["stop"] + m["outcomes"]["stop_gap"],
                          m["outcomes"]["flat"]), (8, 13, 7))
        self.assertEqual(m["avg_R"], 0.156)
        self.assertEqual(m["avg_R_net"], -0.031)

    def test_trade_by_trade_against_the_time_machine(self):
        text = nse.read_maybe_gz(os.path.join(COMMON, "yahoo-RELIANCE.NS-60d5m.json.gz")).decode()
        meta, bars, raw = tm.chart_from_text(text, "RELIANCE")
        sb, _ = tm.parse_bars(bars, NOW)
        pool, _ = tm.pool_sessions(tm.split_sessions(sb), "2026-10-29")
        h = tm.history_replay(pool, {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 2.0},
                              69, (570, 870), 0.1, 500.0)
        self.assertEqual(h["triggered"], 28)
        mine = [(t["date"], t["time"], t["signal"], t["stop"], t["target"],
                 {"flat": "close", "stop_gap": "stop"}.get(t["outcome"], t["outcome"]), round(t["R"], 2),
                 round(t["R_net"], 2)) for t in self.trades]
        theirs = [(t["date"], t["time"], t["entry"], t["stop"], t["target"], t["outcome"], t["R"], t["R_net"])
                  for t in h["trades"]]
        self.assertEqual(mine, theirs)

    def test_the_live_pack_rules_move_it(self):
        # 1 tick of slippage and india-budget's sizing (25 % position cap) make it worse
        tr, _ = bt.run_symbol(self.ss, "orb15", bt.STRATEGIES["orb15"]["default"], WIN, {}, 1)
        m = bt.metrics(tr, 59, 59)
        self.assertEqual(m["trades"], 28)
        self.assertLess(m["avg_R"], 0.156)
        self.assertLess(m["avg_R_net"], -0.031)


class HandChecked(unittest.TestCase):
    def test_orb15_reliance_2026_10_29(self):
        t = trade_on("RELIANCE", "orb15", "2026-10-29")
        self.assertEqual((t["time"], t["signal"], t["entry"], t["stop"], t["target"], t["tick"]),
                         ("09:40", 1174.3, 1174.4, 1163.3, 1196.3, 0.1))
        self.assertEqual((t["outcome"], t["exit"], t["exit_time"], t["qty"], t["sized_by"]),
                         ("flat", 1169.2, "15:05", 21, "position_cap_pct"))
        self.assertAlmostEqual(t["risk"], 11.1)
        self.assertAlmostEqual(t["R"], (1169.2 - 1174.4) / 11.1, places=4)

    def test_charges_math_on_that_trade(self):
        t = trade_on("RELIANCE", "orb15", "2026-10-29")
        buy, sell = 21 * 1174.4, 21 * 1169.2                      # 24,662.40 and 24,553.20
        brokerage = 0.0003 * buy + 0.0003 * sell                  # under the Rs 20 cap: 7.40 + 7.37
        stt, stamp = round(0.00025 * sell), round(0.00003 * buy)  # Rs 6, Rs 1
        exch = (buy + sell) * 0.0030699 / 100
        sebi = (buy + sell) * 0.0001 / 100
        gst = 0.18 * (brokerage + exch + sebi + (buy + sell) * 1e-9)
        self.assertEqual((stt, stamp), (6, 1))
        self.assertAlmostEqual(t["charges"], brokerage + stt + exch + sebi + stamp + gst, delta=0.03)
        self.assertEqual(t["charges"], nse.charges(buy, sell)["total"])
        self.assertAlmostEqual(t["pnl"], -109.2, places=2)
        self.assertAlmostEqual(t["pnl_net"], -109.2 - t["charges"], places=2)
        self.assertAlmostEqual(t["R_net"], t["pnl_net"] / (11.1 * 21), places=4)

    def test_orb30_reliance_2026_08_31(self):
        t = trade_on("RELIANCE", "orb30", "2026-08-31")
        self.assertEqual((t["time"], t["signal"], t["stop"], t["target"], t["outcome"], t["exit"], t["exit_time"]),
                         ("10:30", 1278.7, 1274.6, 1286.9, "target", 1286.9, "12:00"))
        s = [x for x in sessions("RELIANCE") if x.date == "2026-08-31"][0]
        self.assertEqual(bt.opening_range(s, 6), (1278.2, 1271.1))
        self.assertTrue(all(s.bars[k]["c"] <= 1278.2 for k in range(6, 14) if s.bars[k]))

    def test_pdh_reliance_2026_08_06(self):
        t = trade_on("RELIANCE", "pdh", "2026-08-06")
        s = [x for x in sessions("RELIANCE") if x.date == "2026-08-06"][0]
        self.assertEqual(s.prev["high"], 1299.0)
        self.assertEqual((s.bars[2]["c"], s.bars[3]["c"]), (1297.7, 1300.4))
        self.assertEqual((t["time"], t["signal"], t["stop"], t["target"], t["outcome"], t["exit_time"]),
                         ("09:35", 1300.4, 1295.0, 1311.2, "target", "11:20"))
        t2 = trade_on("RELIANCE", "pdh", "2026-08-06", {"stop": "day_low", "r": 2.0})
        self.assertEqual(t2["stop"], 1283.6)                      # the 09:15 bar's low

    def test_gap_go_tcs_2026_09_30(self):
        t = trade_on("TCS", "gap_go", "2026-09-30")
        s = [x for x in sessions("TCS") if x.date == "2026-09-30"][0]
        self.assertEqual((s.prev["close"], s.bars[0]["o"]), (2032.4, 2068.6))
        self.assertEqual((t["time"], t["signal"], t["entry"], t["stop"], t["target"]),
                         ("10:00", 2090.0, 2090.1, 2055.0, 2160.0))
        self.assertEqual((t["outcome"], t["exit"], t["exit_time"]), ("stop", 2054.9, "14:30"))
        self.assertIsNone(trade_on("TCS", "gap_go", "2026-09-30", {"gap": 2.0, "stop": "or_low", "r": 2.0}))

    def test_vwap_pullback_reliance_2026_08_06(self):
        t = trade_on("RELIANCE", "vwap_pullback", "2026-08-06")
        s = [x for x in sessions("RELIANCE") if x.date == "2026-08-06"][0]
        pv = vol = 0.0
        vw = {}
        for k, b in enumerate(s.bars):
            if b:
                pv += (b["h"] + b["l"] + b["c"]) / 3 * b["v"]
                vol += b["v"]
                if vol:
                    vw[k] = pv / vol
        self.assertLessEqual(s.bars[11]["l"], vw[11])            # the 10:10 bar touches VWAP
        self.assertGreater(s.bars[10]["c"], vw[10])              # after closing above it
        self.assertLessEqual(s.bars[12]["c"], vw[12])            # the 10:15 bar closes under
        self.assertGreater(s.bars[13]["c"], vw[13])              # the 10:20 bar closes back above: entry
        self.assertEqual((t["time"], t["signal"], t["stop"], t["target"], t["outcome"], t["exit_time"]),
                         ("10:25", 1300.4, 1298.1, 1305.0, "target", "11:00"))

    def test_orb_pullback_reliance_2026_08_31(self):
        t = trade_on("RELIANCE", "orb_pullback", "2026-08-31")
        self.assertEqual((t["time"], t["signal"], t["stop"], t["target"], t["outcome"], t["exit_time"]),
                         ("11:20", 1278.3, 1274.6, 1285.7, "target", "11:55"))
        t2 = trade_on("RELIANCE", "orb_pullback", "2026-08-31", {"stop": "pb_low", "r": 2.0})
        self.assertEqual(t2["stop"], 1275.1)

    def test_oversold_bounce_tatapower_2026_09_02(self):
        t = trade_on("TATAPOWER", "oversold_bounce", "2026-09-02")
        s = [x for x in sessions("TATAPOWER") if x.date == "2026-09-02"][0]
        self.assertAlmostEqual(s.rsi, 25.58, places=2)
        self.assertEqual((t["time"], t["signal"], t["entry"], t["stop"], t["target"], t["tick"]),
                         ("09:40", 352.5, 352.55, 347.4, 362.7, 0.05))
        self.assertEqual((t["outcome"], t["exit_time"], t["qty"]), ("target", "14:50", 70))

    def test_daily_rsi_matches_the_scanner_formula(self):
        scan = load("india-scanner", "scan")
        b5, b1, _ = bt.load_offline(DIRS, "TATAPOWER")
        closes = [round(b["c"], 2) for b in b1 if b["t"].date() < datetime.date(2026, 9, 2)]
        self.assertAlmostEqual(scan.rsi(closes), sessions("TATAPOWER")[[x.date for x in sessions("TATAPOWER")]
                                                                         .index("2026-09-02")].rsi, places=6)


class EngineRules(unittest.TestCase):
    def test_gap_through_the_stop_fills_at_the_open(self):
        t = trade_on("TATAPOWER", "orb15", "2026-09-08")
        self.assertEqual((t["stop"], t["outcome"], t["exit"], t["exit_time"]), (363.95, "stop_gap", 363.85, "13:55"))
        # synthetic: a 20-rupee gap under the stop fills at that open, not at the stop
        s = flat_session(500, b3=bar(500, 505, 499.5, 504), b4=bar(504, 506, 503, 505.5), b10=bar(480, 482, 478, 481))
        t = bt.simulate(s, {"k": 4, "price": 505.5, "stop": 499.03, "r": 2.0, "why": ""}, WIN, {}, 1)[0]
        self.assertEqual((t["outcome"], t["exit"], t["stop"], t["exit_time"]), ("stop_gap", 479.95, 499.0, "10:10"))

    def test_a_bar_touching_both_counts_as_the_stop(self):
        s = flat_session(500, b5=bar(501, 520, 495, 510))
        t = bt.simulate(s, {"k": 4, "price": 500.0, "stop": 498.0, "r": 2.0, "why": ""}, WIN, {}, 0)[0]
        self.assertEqual((t["target"], t["outcome"], t["exit"], t["R"]), (504.0, "stop", 498.0, -1.0))

    def test_target_fills_exactly_and_stop_pays_slippage(self):
        s = flat_session(500, b6=bar(501, 504.5, 500.2, 503))
        t = bt.simulate(s, {"k": 4, "price": 500.0, "stop": 498.0, "r": 2.0, "why": ""}, WIN, {}, 2)[0]
        self.assertEqual((t["entry"], t["target"], t["outcome"], t["exit"]), (500.1, 504.0, "target", 504.0))
        s = flat_session(500, b6=bar(499.5, 499.6, 497.0, 497.5))
        t = bt.simulate(s, {"k": 4, "price": 500.0, "stop": 498.0, "r": 2.0, "why": ""}, WIN, {}, 2)[0]
        self.assertEqual((t["outcome"], t["exit"]), ("stop", 497.9))
        self.assertLess(t["R"], -1.0)

    def test_flat_by_exit(self):
        s = flat_session(500, b69=bar(500, 501, 499.5, 501))
        t = bt.simulate(s, {"k": 4, "price": 500.0, "stop": 498.0, "r": 2.0, "why": ""}, WIN, {}, 1)[0]
        self.assertEqual((t["outcome"], t["exit"], t["exit_time"]), ("flat", 500.95, "15:05"))
        # the 15:00 bar missing: out at the last bar that exists before flat-by
        s = flat_session(500, b68=bar(500, 501, 499.5, 500.7))
        s.bars[69] = None
        t = bt.simulate(s, {"k": 4, "price": 500.0, "stop": 498.0, "r": 2.0, "why": ""}, WIN, {}, 0)[0]
        self.assertEqual(t["exit"], 500.7)
        # bars after flat-by never count, even a target hit at 15:10
        s = flat_session(500, b71=bar(500, 530, 499.6, 520))
        t = bt.simulate(s, {"k": 4, "price": 500.0, "stop": 498.0, "r": 2.0, "why": ""}, WIN, {}, 0)[0]
        self.assertEqual(t["outcome"], "flat")
        # an earlier flat_by in the config
        w = bt.Window({"flat_by": "14:00"})
        self.assertEqual((w.last, w.cutoff), (56, 840))
        t = bt.simulate(flat_session(500), {"k": 4, "price": 500.0, "stop": 498.0, "r": 2.0, "why": ""}, w, {}, 0)[0]
        self.assertEqual(t["exit_time"], "14:00")
        self.assertEqual(bt.Window({"flat_by": "15:20"}).flat, 15 * 60 + 10)   # never after Dhan's 15:10

    def test_ticks_round_down_and_tight_stops_are_refused(self):
        s = flat_session(500)
        t = bt.simulate(s, {"k": 4, "price": 500.0, "stop": 497.83, "r": 1.5, "why": ""}, WIN, {}, 1)[0]
        self.assertEqual((t["stop"], t["target"]), (497.8, 503.3))        # 500 + 1.5 x 2.2 = 503.30
        t, why = bt.simulate(s, {"k": 4, "price": 500.0, "stop": 499.9, "r": 2.0, "why": ""}, WIN, {}, 1)
        self.assertIsNone(t)
        self.assertIn("too tight", why)
        s.tick = 0.1                                                     # a cached instrument tick wins
        t = bt.simulate(s, {"k": 4, "price": 500.0, "stop": 497.83, "r": 2.0, "why": ""}, WIN, {}, 1)[0]
        self.assertEqual((t["stop"], t["entry"]), (497.8, 500.1))

    def test_sizing_like_india_budget(self):
        q, by = bt.size(1174.4, 1163.3, {})
        self.assertEqual((q, by), (21, "position_cap_pct"))
        q, by = bt.size(500.0, 480.0, {})                                # Rs 20 risk: 500 / 20 = 25 less charges
        self.assertEqual(by, "max_loss_per_trade")
        loss = q * 20 + nse.round_trip_cost(q, 500.0, 480.0)
        self.assertLessEqual(loss, 500)
        self.assertGreater((q + 1) * 20 + nse.round_trip_cost(q + 1, 500.0, 480.0), 500)
        self.assertEqual(bt.size(60000.0, 59000.0, {})[0], 0)            # one share is over the 25 % cap
        s = flat_session(60000.0)
        t, why = bt.simulate(s, {"k": 4, "price": 60000.0, "stop": 59900.0, "r": 2.0, "why": ""}, WIN, {}, 1)
        self.assertEqual(why, "zero shares fit the budget")

    def test_entry_window(self):
        # a breakout bar closing at 14:35 is past no_entries_after 14:30
        s = flat_session(500)
        s.bars[63] = bar(500, 510, 500, 509)                             # the 14:30 bar closes 14:35
        self.assertIsNone(bt.strat_orb(s, {"or_minutes": 15, "stop": "or_mid", "r": 2.0}, WIN))
        s.bars[62] = bar(500, 510, 500, 509)                             # the 14:25 bar closes 14:30: allowed
        self.assertEqual(bt.strat_orb(s, {"or_minutes": 15, "stop": "or_mid", "r": 2.0}, WIN)["k"], 62)
        s.bars[62] = None
        s.bars[2] = bar(500, 500.5, 499.5, 500)
        s.bars[3] = bar(500, 510, 500, 509)                              # 09:30 bar, closes 09:35: first chance
        self.assertEqual(bt.strat_orb(s, {"or_minutes": 15, "stop": "or_mid", "r": 2.0}, WIN)["k"], 3)


class MissingBars(unittest.TestCase):
    def test_fixture_sessions_are_short_and_never_75(self):
        ss = sessions("RELIANCE")
        self.assertEqual(len(ss), 60)
        self.assertTrue(all(72 <= s.n <= 75 for s in ss))
        self.assertTrue(all(s.bars[73] is None for s in ss))           # no 15:20 bar: NSE's closing auction

    def test_missing_opening_range_bar_skips_the_day(self):
        s = flat_session(500)
        s.bars[1] = None
        self.assertIn("no opening range", bt.strat_orb(s, bt.STRATEGIES["orb15"]["default"], WIN))
        _, skips = bt.run_symbol([s], "orb15", bt.STRATEGIES["orb15"]["default"], WIN, {}, 1)
        self.assertEqual(list(skips), ["no opening range (a bar is missing)"])

    def test_missing_bars_inside_a_trade_are_skipped(self):
        s = flat_session(500, b5=None, b6=None, b7=bar(500, 504.5, 499.9, 504))
        s.bars[5] = s.bars[6] = None
        t = bt.simulate(s, {"k": 4, "price": 500.0, "stop": 498.0, "r": 2.0, "why": ""}, WIN, {}, 0)[0]
        self.assertEqual((t["outcome"], t["exit_time"]), ("target", "09:55"))

    def test_short_sessions_are_left_out(self):
        b5, b1, _ = bt.load_offline(DIRS, "RELIANCE")
        cut = [b for b in b5 if not (b["t"].date() == datetime.date(2026, 10, 1) and b["t"].hour >= 13)]
        ss, notes = bt.build_sessions("RELIANCE", cut, b1, NOW)
        self.assertNotIn("2026-10-01", [s.date for s in ss])
        self.assertEqual(notes["short"][0]["date"], "2026-10-01")
        # a session still running is not used
        ss, _ = bt.build_sessions("RELIANCE", b5, b1, datetime.datetime(2026, 10, 29, 11, 0))
        last = [s for s in ss if s.date == "2026-10-29"]
        self.assertEqual(last, [])                                     # 11:00 leaves only 21 bars: short

    def test_prior_day_from_5m_when_no_daily(self):
        ss = sessions("RELIANCE", daily=False)
        self.assertIsNone(ss[0].prev)
        self.assertEqual(ss[1].prev["date"], ss[0].date)
        withd = sessions("RELIANCE")
        self.assertEqual(withd[0].prev["date"], "2026-08-03")            # from the daily bars


class Metrics(unittest.TestCase):
    def mk(self, rs, date="2026-10-01", sym="X"):
        return [{"symbol": sym, "date": "2026-10-%02d" % (i + 1), "time": "10:00", "R": r, "R_net": r - 0.1,
                 "pnl": r * 100, "pnl_net": (r - 0.1) * 100, "charges": 10, "outcome": "flat"} for i, r in enumerate(rs)]

    def test_streak_drawdown_profit_factor(self):
        m = bt.metrics(self.mk([2, -1, -1, -1, 2, -1]), 6, 6)
        self.assertEqual(m["max_consecutive_losses"], 3)
        self.assertEqual(m["max_drawdown_R"], 3.3)                        # 1.9 -> -1.4
        self.assertEqual(m["max_drawdown_inr"], 330.0)
        self.assertEqual(m["profit_factor"], round(380 / 440.0, 2))
        self.assertEqual(m["win_pct"], 33.3)
        self.assertEqual(m["equity_curve"][-1]["cum_R_net"], round(sum([2, -1, -1, -1, 2, -1]) - 0.6, 3))
        self.assertEqual(sum(h["trades"] for h in m["histogram_R_net"]), 6)
        self.assertEqual(m["days_with_trade_pct"], 100.0)

    def test_split_two_thirds(self):
        dates = ["d%02d" % i for i in range(60)]
        a, b = bt.split_dates(dates)
        self.assertEqual((len(a), len(b), a[-1], b[0]), (40, 20, "d39", "d40"))

    def test_walk_forward_picks_in_sample_and_reports_out_of_sample(self):
        uni = dict((s, sessions(s)) for s in SYMS[:6])
        res, meta = bt.backtest(uni, ["orb15"], {}, 1)
        r = res["orb15"]
        wf = r["walk_forward"]
        cut = wf["out_of_sample"]["from"]
        self.assertEqual((wf["in_sample"]["sessions"], wf["out_of_sample"]["sessions"]), (40, 20))
        ok = [v for v in r["variants"] if v["in_sample"]["trades"] >= 10]
        best = max(ok, key=lambda v: (v["in_sample"]["avg_R_net"], v["in_sample"]["trades"]))
        self.assertEqual(wf["chosen"], best["label"])
        self.assertEqual(wf["chosen_out_of_sample"]["trades"], best["out_of_sample"]["trades"])
        self.assertEqual(wf["chosen_out_of_sample"]["avg_R_net"], best["out_of_sample"]["avg_R_net"])
        # the out-of-sample trades are exactly the chosen variant's trades from the cut on
        tr = []
        for s in uni:
            tr += bt.run_symbol(uni[s], "orb15", best["params"], WIN, {}, 1)[0]
        oos = [t for t in tr if t["date"] >= cut]
        self.assertEqual(len(oos), best["out_of_sample"]["trades"])
        self.assertTrue(all(c["date"] >= cut for c in wf["chosen_out_of_sample"]["equity_curve"]))

    def test_verdicts(self):
        self.assertIn("too few", bt.verdict({"trades": 12, "avg_R": 1, "avg_R_net": 1}, {}, 60))
        self.assertIn("no edge after charges",
                      bt.verdict({"trades": 300, "avg_R": 0.1, "avg_R_net": -0.1}, {"trades": 90, "avg_R_net": -0.2}, 60))
        self.assertIn("did not hold out of sample",
                      bt.verdict({"trades": 300, "avg_R": 0.3, "avg_R_net": 0.1}, {"trades": 90, "avg_R_net": -0.2}, 60))
        self.assertIn("thin sample",
                      bt.verdict({"trades": 60, "avg_R": 0.3, "avg_R_net": 0.1, "t_stat": 0.8},
                                 {"trades": 20, "avg_R_net": 0.05}, 60))


class Universe(unittest.TestCase):
    N500 = os.path.join(COMMON, "n500.csv")

    def test_index_csv_keeps_series_eq(self):
        kept, dropped = bt.parse_index_csv(nse.read_maybe_gz(self.N500).decode("utf-8-sig"))
        self.assertGreater(len(kept), 490)
        self.assertIn("RELIANCE", dict(kept))
        name, syms, notes = bt.resolve_universe("nifty500", self.N500)
        self.assertEqual((name, len(syms)), ("nifty500", len(kept)))

    def test_tata_group(self):
        name, syms, notes = bt.resolve_universe("tata", self.N500)
        self.assertEqual(name, "tata")
        self.assertEqual(syms, sorted(["TATACAP", "TATACHEM", "TATACOMM", "TCS", "TATACONSUM", "TATAELXSI",
                                       "TATAINVEST", "TMCV", "TMPV", "TATAPOWER", "TATASTEEL", "TATATECH",
                                       "TTML", "TITAN", "TRENT", "VOLTAS"]))
        self.assertEqual(notes, [])

    def test_symbols_and_files(self):
        self.assertEqual(bt.resolve_universe("reliance, TCS.NS")[1], ["RELIANCE", "TCS"])
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "mine.txt")
            with open(p, "w") as fh:
                fh.write("INFY\n# a comment\nsbin\n")
            self.assertEqual(bt.resolve_universe(p), ("mine", ["INFY", "SBIN"], []))
        finally:
            shutil.rmtree(d)
        with self.assertRaises(bt.BTError):
            bt.resolve_universe("NOT A SYMBOL!")


class Fetching(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.old = os.environ.get("TRADER_DIR")
        os.environ["TRADER_DIR"] = os.path.join(self.dir, "trader")
        self.calls = []
        self._http, self._sleep = bt._http, bt._sleep
        bt._sleep = lambda s: self.calls.append(("sleep", s))

    def tearDown(self):
        bt._http, bt._sleep = self._http, self._sleep
        if self.old is None:
            os.environ.pop("TRADER_DIR", None)
        else:
            os.environ["TRADER_DIR"] = self.old
        shutil.rmtree(self.dir)

    def test_yahoo_is_cached_and_reruns_offline(self):
        files = {"5m": nse.read_maybe_gz(os.path.join(BTF, "yahoo-TCS.NS-60d5m.json.gz")),
                 "1d": nse.read_maybe_gz(os.path.join(BTF, "yahoo-TCS.NS-1y1d.json.gz"))}
        fails = [429]

        def http(url, headers=None):
            self.calls.append(url)
            if fails:
                fails.pop()
                raise nse.NSEError("HTTP 429 from query1.finance.yahoo.com")
            return files["5m" if "interval=5m" in url else "1d"]
        bt._http = http
        b5, b1, notes = bt.load_yahoo("TCS", NOW)
        urls = [c for c in self.calls if isinstance(c, str)]
        self.assertTrue(all("TCS.NS" in u and "query" in u for u in urls))
        self.assertIn("range=60d&interval=5m", urls[0])
        self.assertTrue(any(isinstance(c, tuple) for c in self.calls))   # paused between requests
        self.assertTrue(os.path.exists(bt.store_path("yahoo", "TCS", "5m")))
        n = len(urls)
        b5b, b1b, _ = bt.load_yahoo("TCS", NOW + datetime.timedelta(hours=2))
        self.assertEqual(len([c for c in self.calls if isinstance(c, str)]), n)   # fresh: no network
        self.assertEqual(len(b5b), len(b5))
        self.assertEqual(len(b1b), len(b1))

    def test_today_is_not_cached_while_the_market_is_open(self):
        bt._http = lambda url, headers=None: nse.read_maybe_gz(
            os.path.join(BTF, "yahoo-TCS.NS-60d5m.json.gz" if "5m" in url else "yahoo-TCS.NS-1y1d.json.gz"))
        b5, _, _ = bt.load_yahoo("TCS", datetime.datetime(2026, 10, 29, 12, 0))
        self.assertEqual(max(b["t"] for b in b5).date(), datetime.date(2026, 10, 28))

    def test_angel_chunks_paces_and_resumes(self):
        real = nse.parse_yahoo_chart(nse.read_maybe_gz(os.path.join(COMMON, "yahoo-RELIANCE.NS-60d5m.json.gz")))[1]

        class FakeAngel(object):
            def __init__(s):
                s.requests = []

            def candles(s, token, start, end, minutes=5, exchange="NSE"):
                s.requests.append(("candles", token, start, end))
                self.assertLessEqual((end.date() - start.date()).days, bt.ANGEL_CHUNK_DAYS - 1)
                rows = [[(b["t"]).isoformat() + "+05:30", b["o"], b["h"], b["l"], b["c"], b["v"]]
                        for b in real if start <= b["t"] <= end]
                import importlib
                return importlib.import_module("angel").candle_bars(rows)

            def post(s, route, body):
                s.requests.append((route, body["interval"]))
                self.assertEqual(route, "candles")
                return []

        fake = FakeAngel()
        scrip = {"RELIANCE": {"token": "2885", "tick": 0.1}}
        b5, b1, info = bt.load_angel("RELIANCE", NOW, 250, fake, scrip)
        candle_calls = [r for r in fake.requests if r[0] == "candles" and len(r) == 4]
        # 250 sessions = 395 calendar days wanted (x1.5 + 20): the newest 90-day chunk, then the one before it comes
        # back empty (these bars start 2026-08-04), so it stops instead of asking for older chunks
        self.assertEqual(len(candle_calls), 2)
        self.assertEqual(candle_calls[0][3], datetime.datetime(2026, 10, 29, 15, 30))
        self.assertEqual(candle_calls[1][3].date(), candle_calls[0][2].date() - datetime.timedelta(days=1))
        self.assertEqual(info["covered"][0], (NOW.date() - datetime.timedelta(days=395)).isoformat())
        self.assertTrue(all(r[0] == "candles" for r in fake.requests))   # read-only: never an order route
        self.assertEqual(len(b5), len(nse.on_grid(real, 5)))
        sleeps = [c for c in self.calls if isinstance(c, tuple)]
        self.assertGreaterEqual(len(sleeps), 1)                          # paced at about 1 request a second
        n = len(fake.requests)
        bt.load_angel("RELIANCE", NOW + datetime.timedelta(hours=1), 250, fake, scrip)
        self.assertEqual(len(fake.requests), n)                          # resumed from the cache: nothing new

    def test_angel_backs_off_on_errors(self):
        tries = []

        def flaky():
            tries.append(1)
            if len(tries) < 3:
                raise nse.NSEError("Angel AB1019: Access denied because of exceeding access rate")
            return "ok"
        self.assertEqual(bt.angel_call(flaky, "x"), "ok")
        waits = [c[1] for c in self.calls if isinstance(c, tuple)]
        self.assertIn(bt.BACKOFF[0], waits)
        self.assertIn(bt.BACKOFF[1], waits)


class Cli(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.old = os.environ.get("TRADER_DIR")
        os.environ["TRADER_DIR"] = os.path.join(self.dir, "trader")
        self.off = ",".join(DIRS)

    def tearDown(self):
        if self.old is None:
            os.environ.pop("TRADER_DIR", None)
        else:
            os.environ["TRADER_DIR"] = self.old
        shutil.rmtree(self.dir)

    def test_run_prints_the_report_and_caveats(self):
        code, out, err = cli("run", "--strategy", "orb15", "--universe", "RELIANCE,TCS", "--offline", self.off,
                             "--now", "2026-10-29 17:00", "--variants")
        self.assertEqual(code, 0, err)
        for s in ("orb15: 15-minute opening-range breakout", "Walk-forward:", "Verdict:", "By entry time:",
                  "Survivorship", "slippage", "Not financial advice", "never places an order", "r=3 stop=or_low"):
            self.assertIn(s, out)
        for bad in ("NYSE", "$", "Alpaca"):
            self.assertNotIn(bad, out)

    def test_compare_writes_json_and_markdown(self):
        code, out, err = cli("compare", "--universe", ",".join(SYMS), "--offline", self.off,
                             "--now", "2026-10-29 17:00", "--write")
        self.assertEqual(code, 0, err)
        path = os.path.join(os.environ["TRADER_DIR"], "backtest", "2026-10-29-12-stocks-all.json")
        self.assertTrue(os.path.exists(path), out)
        self.assertTrue(os.path.exists(path[:-5] + ".md"))
        doc = nse.read_json(path)
        self.assertEqual(sorted(doc["strategies"]), sorted(bt.ORDER))
        m = doc["meta"]
        for k in ("universe", "symbols", "from", "to", "sessions", "source", "slippage_ticks", "charges_model",
                  "caveats", "split", "sizing"):
            self.assertIn(k, m)
        self.assertEqual((m["from"], m["to"], m["sessions"], len(m["symbols"])), ("2026-08-04", "2026-10-29", 60, 12))
        for n, r in doc["strategies"].items():
            f = r["full"]
            for k in ("trades", "win_pct", "avg_R", "avg_R_net", "expectancy_inr", "profit_factor",
                      "max_consecutive_losses", "max_drawdown_R", "max_drawdown_inr", "equity_curve",
                      "histogram_R_net", "by_entry_time", "by_stock"):
                self.assertIn(k, f, n)
            wf = r["walk_forward"]
            self.assertIn("avg_R_net", wf["chosen_in_sample"])
            self.assertIn("avg_R_net", wf["chosen_out_of_sample"])
            self.assertTrue(r["verdict"])
        # ranked by out-of-sample net R (thin ones last)
        thick = [n for n in doc["ranking"] if doc["strategies"][n]["walk_forward"]["chosen_out_of_sample"]["trades"] >= 10]
        vals = [doc["strategies"][n]["walk_forward"]["chosen_out_of_sample"]["avg_R_net"] for n in thick]
        self.assertEqual(vals, sorted(vals, reverse=True))
        self.assertEqual(doc["ranking"][:len(thick)], thick)
        with open(path[:-5] + ".md", encoding="utf-8") as fh:
            self.assertIn("| strategy | trades |", fh.read())
        self.assertIn("OOS", out)

    def test_json_and_dates(self):
        code, out, err = cli("run", "--strategy", "pdh,gap_go", "--universe", "RELIANCE", "--offline", self.off,
                             "--now", "2026-10-29 17:00", "--from", "2026-09-01", "--to", "2026-09-30", "--json")
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual((doc["meta"]["from"], doc["meta"]["to"]), ("2026-09-01", "2026-09-30"))
        self.assertTrue(all("2026-09-01" <= t["date"] <= "2026-09-30" for t in doc["strategies"]["pdh"]["trades"]))

    def test_errors(self):
        code, _, err = cli("run", "--strategy", "nope", "--universe", "RELIANCE", "--offline", self.off)
        self.assertEqual(code, 1)
        self.assertIn("unknown strategy", err)
        code, _, err = cli("run", "--universe", "ZZZZ", "--offline", self.off, "--now", "2026-10-29 17:00")
        self.assertEqual(code, 1)
        self.assertIn("no bars", err)


class ResearchHoldout(unittest.TestCase):
    """Sessions inside india-research's locked holdouts are withheld unless --allow-holdout."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.old = os.environ.get("TRADER_DIR")
        os.environ["TRADER_DIR"] = os.path.join(self.dir, "trader")
        nse.write_json(os.path.join(os.environ["TRADER_DIR"], "research", "splits.json"), {
            "angel:x:5m": {"explore": ["2026-01-01", "2026-06-30"], "validate": ["2026-07-01", "2026-10-12"],
                           "holdout": ["2026-10-13", "2026-10-29"], "symbols": ["RELIANCE"]}})
        self.off = ",".join(DIRS)

    def tearDown(self):
        if self.old is None:
            os.environ.pop("TRADER_DIR", None)
        else:
            os.environ["TRADER_DIR"] = self.old
        shutil.rmtree(self.dir)

    def run_json(self, *extra):
        code, out, err = cli("run", "--strategy", "orb15", "--universe", "RELIANCE,TCS", "--offline", self.off,
                             "--now", "2026-10-29 17:00", "--json", *extra)
        self.assertEqual(code, 0, err)
        return json.loads(out), err

    def test_withheld_by_default_with_a_note(self):
        doc, err = self.run_json()
        m = doc["meta"]
        self.assertFalse(m["holdout_included"])
        self.assertEqual(m["holdout_withheld"]["withheld_sessions"], 12)        # 2026-10-13..10-29 (10-20 a holiday)
        self.assertEqual(m["holdout_withheld"]["by_dataset"]["angel:x:5m"]["symbols"], 1)
        self.assertIn("Holdout: withheld 12 session(s)", err)                   # stderr: --json stays clean
        self.assertTrue(any("withheld" in n for n in m["notes"]))
        rel = [t for t in doc["strategies"]["orb15"]["trades"] if t["symbol"] == "RELIANCE"]
        self.assertTrue(rel and all(t["date"] < "2026-10-13" for t in rel))
        tcs = [t for t in doc["strategies"]["orb15"]["trades"] if t["symbol"] == "TCS"]
        self.assertTrue(any(t["date"] >= "2026-10-13" for t in tcs))           # TCS is in no split
        self.assertEqual(m["to"], "2026-10-29")

    def test_allow_holdout_includes_and_records_it(self):
        doc, err = self.run_json("--allow-holdout")
        self.assertTrue(doc["meta"]["holdout_included"])
        self.assertIn("INCLUDED", err)
        rel = [t for t in doc["strategies"]["orb15"]["trades"] if t["symbol"] == "RELIANCE"]
        self.assertTrue(any(t["date"] >= "2026-10-13" for t in rel))

    def test_everything_withheld_says_why(self):
        code, _, err = cli("run", "--strategy", "orb15", "--universe", "RELIANCE", "--offline", self.off,
                           "--now", "2026-10-29 17:00", "--from", "2026-10-13")
        self.assertEqual(code, 1)
        self.assertIn("--allow-holdout", err)


if __name__ == "__main__":
    unittest.main()
