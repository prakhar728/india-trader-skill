"""india-backtest swing tests. Offline, on real fixtures in fixtures/india-swing/:

yahoo-<SYM>.NS-1d.json.gz   Yahoo chart JSON (query1.finance.yahoo.com/v8/finance/chart, interval=1d,
                            period1 2017-01-01 IST to period2 2026-10-29, events div,splits), recorded
                            2026-10-29 for RELIANCE, HDFCBANK, INFY, TCS, ICICIBANK, ITC, SBIN, LT and
                            NIFTYBEES; yahoo-NSEI-1d.json.gz (^NSEI) and yahoo-INDIAVIX-1d.json.gz (^INDIAVIX).
ind_nifty100list.csv        NSE's Nifty 100 list (nsearchives.nseindia.com/content/indices/), 2026-10-29.

Facts the fixtures carry and the tests rely on:
  * RELIANCE's 1:1 bonus (ex 2024-10-28) is already in Yahoo's OHLC: 2024-10-25 closes 1,327.85
    (it traded near 2,655 that day). Yahoo lists the split events; prices are not dividend-adjusted.
  * NIFTYBEES 2019-12-19 and 2019-12-20 sit at a tenth of the price (12.92, 13.03 between 129.25 and
    129.93): a bad print the loader drops.

Hand-checked trades (default variants, the 8 stocks, simulation from 2018-01-01, Rs 1,00,000, 10 slots,
Dhan delivery charges, 0.10 % slippage a side): see Strategies.test_* below; each one re-derives the
signal from the raw bars with the test's own arithmetic.
"""

import datetime
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
import io

from _load import load, FIX

import nse

sw = load("india-backtest", "swing")
bt = load("india-backtest", "backtest")

SF = os.path.join(FIX, "india-swing")
SYMS = ["RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK", "ITC", "SBIN", "LT"]
_C = {}


def universe():
    if "uni" not in _C:
        _C["uni"] = dict((s, sw.Series(s, sw.clean_rows(sw.offline_rows([SF], s))[0], True)) for s in SYMS)
        _C["bench"] = dict((k, sw.Series(k, sw.clean_rows(sw.offline_rows([SF], k), index=k.startswith("^"))[0]))
                           for k in (sw.NIFTY, sw.BEES, sw.VIX))
        cal_all = sw.build_calendar(_C["uni"], _C["bench"][sw.NIFTY])
        _C["cal"] = [d for d in cal_all if d >= "2018-01-01"]
        _C["runs"] = {}
    return _C["uni"], _C["bench"], _C["cal"]


def run(name, p=None, cfg=None):
    uni, _, cal = universe()
    p = p or sw.STRATEGIES[name]["default"]
    key = (name, json.dumps(p, sort_keys=True), json.dumps(cfg or {}, sort_keys=True))
    if key not in _C["runs"]:
        _C["runs"][key] = sw.simulate(uni, name, p, cal, cfg)
    return _C["runs"][key]


def next_session(cal, d):
    return cal[cal.index(d) + 1]


# ---- the test's own arithmetic (independent of swing.py) -------------------------------------

def my_sma(c, i, n):
    return sum(c[i - n + 1:i + 1]) / float(n)


def my_rsi(c, i, n):
    """Wilder's RSI at index i, written out longhand."""
    ch = [c[k] - c[k - 1] for k in range(1, i + 1)]
    ag = sum(max(x, 0) for x in ch[:n]) / n
    al = sum(max(-x, 0) for x in ch[:n]) / n
    for x in ch[n:]:
        ag = (ag * (n - 1) + max(x, 0)) / n
        al = (al * (n - 1) + max(-x, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def synthetic(sym, rows):
    return sw.Series(sym, rows, True)


def flat_rows(n, start="2020-01-01", px=100.0, vol=1000):
    d0 = datetime.date.fromisoformat(start)
    out = []
    k = 0
    d = d0
    while len(out) < n:
        if d.weekday() < 5:
            out.append([d.isoformat(), px, px + 0.5, px - 0.5, px, vol])
            k += 1
        d += datetime.timedelta(days=1)
    return out


# ================================================================ data

class Data(unittest.TestCase):
    def test_yahoo_ohlc_is_split_and_bonus_adjusted(self):
        uni, _, _ = universe()
        S = uni["RELIANCE"]
        self.assertAlmostEqual(S.c[S.idx["2024-10-25"]], 1327.85, places=2)   # traded ~2,655 before the bonus
        self.assertLess(abs(S.c[S.idx["2024-10-28"]] / S.c[S.idx["2024-10-25"]] - 1), 0.05)
        rows, ev = sw.rows_from_chart(nse.read_maybe_gz(os.path.join(SF, "yahoo-RELIANCE.NS-1d.json.gz")))
        self.assertIn(("2024-10-28", "2:1"), [tuple(x) for x in ev["splits"]])

    def test_bad_print_dropped(self):
        rows, notes = sw.clean_rows(sw.offline_rows([SF], sw.BEES))
        self.assertEqual(notes["bad_prints"], ["2019-12-19", "2019-12-20"])
        d = dict((r[0], r[4]) for r in rows)
        self.assertNotIn("2019-12-19", d)
        self.assertGreater(d["2019-12-23"], 100)

    def test_clean_fixes_high_low_and_drops_filler(self):
        rows = [["2020-01-01", 10, 10.5, 9.5, 10, 100], ["2020-01-02", 10, 10.2, 9.9, 10.4, 100],
                ["2020-01-03", 10, 10, 10, 10, 0], ["2020-01-06", 0, 1, 1, 1, 5]]
        out, notes = sw.clean_rows(rows)
        self.assertEqual([r[0] for r in out], ["2020-01-01", "2020-01-02"])
        self.assertEqual(out[1][2], 10.4)
        self.assertEqual((notes["fixed_high_low"], notes["filler"]), (1, 1))
        out, _ = sw.clean_rows(rows[:3], index=True)      # an index has no volume: keep it
        self.assertEqual(len(out), 3)

    def test_demerger_gap_is_back_adjusted(self):
        rows = [["2025-10-10", 650, 665, 645, 660, 100], ["2025-10-13", 660, 662, 655, 660.75, 100],
                ["2025-10-14", 400, 410, 395, 395, 300], ["2025-10-15", 396, 405, 390, 400, 100],
                ["2025-10-16", 400, 404, 398, 402, 100]]
        out, notes = sw.clean_rows(rows)
        f = 400 / 660.75
        self.assertEqual(notes["breaks"], [["2025-10-14", round(f, 4), "demerger or other"]])
        self.assertAlmostEqual(out[1][4], round(660.75 * f, 4))   # yesterday's close = today's open
        self.assertEqual(out[1][5], 100)                          # volume untouched
        self.assertEqual(out[2], rows[2])
        rows[2][1] = 600                                          # a 9 % gap and a real fall: kept as is
        out, notes = sw.clean_rows(rows)
        self.assertEqual((notes["breaks"], out[1][4]), ([], 660.75))

    def test_unadjusted_split_scales_volume_too(self):
        rows = [["2024-%02d-%02d" % (1 + k // 20, 1 + k % 20), 400.0, 404.0, 396.0, 400.0, 1000] for k in range(25)]
        rows += [["2024-03-%02d" % (1 + k), 100.0, 101.0, 99.0, 100.0, 4100] for k in range(25)]
        out, notes = sw.clean_rows(rows)
        self.assertEqual(notes["breaks"], [["2024-03-01", 0.25, "split (volume x4.00 too)"]])
        self.assertEqual((out[0][4], out[0][5]), (100.0, 4000))

    def test_calendar_and_chart_url(self):
        uni, bench, cal = universe()
        self.assertEqual(cal[0], "2018-01-01")
        self.assertNotIn("2018-01-26", cal)                   # Republic Day
        u = sw.chart_url("^NSEI", 1, 2)
        self.assertIn("/chart/%5ENSEI?period1=1&period2=2&interval=1d", u)
        self.assertIn("RELIANCE.NS", sw.chart_url("RELIANCE", 1, 2))

    def test_fetch_is_polite_and_resumable(self):
        raw = nse.read_maybe_gz(os.path.join(SF, "yahoo-TCS.NS-1d.json.gz"))
        calls, sleeps = [], []
        d = tempfile.mkdtemp()
        old = (sw._http, sw._sleep, os.environ.get("TRADER_DIR"))
        try:
            os.environ["TRADER_DIR"] = d
            sw._sleep = sleeps.append
            state = {"n": 0}

            def http(url):
                calls.append(url)
                state["n"] += 1
                if state["n"] == 1:
                    raise nse.NSEError("HTTP 429 from query1.finance.yahoo.com")
                return raw
            sw._http = http
            now = datetime.datetime(2026, 10, 29, 18, 0)
            how, doc = sw.fetch_symbol("TCS", now, 12)
            self.assertEqual(how, "fetched")
            self.assertEqual(len(calls), 2)                   # 429 on query1, then query2 answered
            self.assertTrue(os.path.isfile(os.path.join(d, "cache", "daily", "TCS.json.gz")))
            how, _ = sw.fetch_symbol("TCS", now, 12)
            self.assertEqual((how, len(calls)), ("cached", 2))  # fresh cache: no request
            how, _ = sw.fetch_symbol("TCS", now, 14)              # wants older history: refetch
            self.assertEqual((how, len(calls)), ("fetched", 3))
        finally:
            sw._http, sw._sleep = old[0], old[1]
            if old[2] is None:
                os.environ.pop("TRADER_DIR", None)
            else:
                os.environ["TRADER_DIR"] = old[2]
            shutil.rmtree(d)


# ================================================================ indicators

class Indicators(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(sw.sma([1, 2, 3, 4, 5], 3), [None, None, 2.0, 3.0, 4.0])

    def test_rsi2_wilder_by_hand(self):
        # changes +1, -0.5, +1.5: avg gain (1+0)/2 = 0.5, avg loss 0.25 -> RSI 66.667;
        # next: gain (0.5 + 1.5)/2 = 1.0, loss 0.25/2 = 0.125 -> RS 8 -> 88.889
        r = sw.rsi([10, 11, 10.5, 12], 2)
        self.assertIsNone(r[1])
        self.assertAlmostEqual(r[2], 66.6667, places=3)
        self.assertAlmostEqual(r[3], 88.8889, places=3)

    def test_rsi14_wilder_by_hand(self):
        c = [100.0]
        for k in range(14):
            c.append(c[-1] + (1 if k % 2 == 0 else -0.5))
        c.append(c[-1] + 1)
        r = sw.rsi(c, 14)
        self.assertAlmostEqual(r[14], 66.6667, places=3)      # avg gain 0.5, avg loss 0.25
        self.assertAlmostEqual(r[15], 69.7674, places=3)      # (0.5*13+1)/14 vs 0.25*13/14

    def test_rsi_matches_longhand_on_real_closes(self):
        uni, _, _ = universe()
        c = uni["SBIN"].c
        for n in (2, 14):
            r = sw.rsi(c, n)
            for i in (n + 5, 400, 1500, len(c) - 1):
                self.assertAlmostEqual(r[i], my_rsi(c, i, n), places=6)

    def test_atr_wilder(self):
        h, l, c = [10, 11, 12, 13], [9, 10, 11, 12], [9.5, 10.5, 11.5, 12.5]
        a = sw.atr(h, l, c, 2)
        self.assertEqual(a[:2], [None, None])
        self.assertAlmostEqual(a[2], 1.5)                     # TRs 1.5, 1.5
        self.assertAlmostEqual(a[3], 1.5)

    def test_prior_extremes_and_means_against_brute_force(self):
        uni, _, _ = universe()
        S = uni["LT"]
        hi, lo, av = S.ind("hi_close", 20), S.ind("lo_low", 10), S.ind("vol_avg", 50)
        for i in (0, 19, 20, 21, 300, 1999, len(S.c) - 1):
            self.assertEqual(hi[i], max(S.c[i - 20:i]) if i >= 20 else None)
            self.assertEqual(lo[i], min(S.l[i - 10:i]) if i >= 10 else None)
            if i >= 50:
                self.assertAlmostEqual(av[i], sum(S.v[i - 50:i]) / 50.0, places=4)
        m = S.ind("mom", 252, 21)
        self.assertAlmostEqual(m[400], S.c[379] / S.c[148] - 1)

    def test_no_look_ahead_in_any_signal(self):
        """A signal on bar i must be the same when the series stops at bar i."""
        uni, _, _ = universe()
        S = uni["ICICIBANK"]
        for name in sw.ORDER:
            spec = sw.STRATEGIES[name]
            if spec.get("kind") == "rotation":
                continue
            for p in spec["variants"]:
                hits = [i for i in range(300, len(S.c)) if spec["entry"](S, i, p)][:15]
                for i in hits + [400, 900, 1700]:
                    rows = [[S.dates[k], S.o[k], S.h[k], S.l[k], S.c[k], S.v[k]] for k in range(i + 1)]
                    T = sw.Series("ICICIBANK", rows, True)
                    self.assertEqual(spec["entry"](S, i, p), spec["entry"](T, i, p), (name, p, S.dates[i]))


# ================================================================ costs

class DeliveryCharges(unittest.TestCase):
    def test_dhan_round_trip_by_hand(self):
        c = nse.delivery_charges(100000, 110000)
        # brokerage 0; STT 100 + 110; NSE txn 210000 x 0.0030699 % = 6.45; SEBI 0.21; stamp 15;
        # DP 12.50; GST 18 % of (6.4468 + 0.21 + 0.0002 + 12.50) = 3.45
        self.assertEqual((c["brokerage"], c["stt"], c["exchange"], c["sebi"], c["stamp"], c["dp"], c["gst"]),
                         (0.0, 210.0, 6.45, 0.21, 15.0, 12.5, 3.45))
        self.assertEqual(c["total"], 247.61)

    def test_angel_brokerage_and_dp(self):
        c = nse.delivery_charges(100000, 110000, broker="angel")
        self.assertEqual((c["brokerage"], c["dp"]), (40.0, 20.0))   # Rs 20 an order (0.1 % is more)
        self.assertEqual(c["gst"], 12.0)                            # 18 % of 40 + 6.45 + 0.21 + 20
        self.assertEqual(c["total"], 303.66)
        self.assertEqual(nse.delivery_charges(2000, 0, broker="angel")["brokerage"], 5.0)    # minimum Rs 5
        self.assertEqual(nse.delivery_charges(8000, 0, broker="angel")["brokerage"], 8.0)    # 0.1 %

    def test_buy_leg_has_no_dp_and_dp_is_per_debit(self):
        self.assertEqual(nse.delivery_charges(10000, 0)["dp"], 0.0)
        self.assertEqual(nse.delivery_charges(0, 10000)["dp"], 12.5)
        self.assertEqual(nse.delivery_charges(0, 10000, dp_debits=0)["dp"], 0.0)
        self.assertEqual(nse.delivery_charges(0, 10000, dp_debits=2)["dp"], 25.0)

    def test_engine_charges_dp_once_per_scrip_per_sell_day(self):
        res = run("pullback_rsi2")
        trades = res["trades"]
        sells = set((t["exit_date"], t["symbol"]) for t in trades)
        self.assertEqual(len(sells), len(trades))
        want = 0.0
        for t in trades:
            bv = t["qty"] * t["entry"]
            want += nse.delivery_charges(bv, 0)["total"] + nse.delivery_charges(0, t["qty"] * t["exit"])["total"]
        self.assertAlmostEqual(sum(t["charges_inr"] for t in trades), want, places=1)
        # a scrip sold twice on one day pays one DP (the engine's rule, checked on two legs)
        one = nse.delivery_charges(0, 5000, dp_debits=1)["total"] + nse.delivery_charges(0, 5000, dp_debits=0)["total"]
        self.assertAlmostEqual(one, nse.delivery_charges(0, 10000)["total"], places=1)

    def test_costs_off_means_no_charges_and_no_slippage(self):
        res = run("pullback_rsi2", cfg={"costs": False})
        t = res["trades"][0]
        self.assertEqual((t["charges_inr"], t["slippage_inr"]), (0.0, 0.0))
        self.assertEqual(t["entry"], t["entry_open"])
        self.assertAlmostEqual(t["net_inr"], t["gross_inr"], places=2)


# ================================================================ engine

class Engine(unittest.TestCase):
    def test_every_entry_is_the_next_sessions_open_plus_slippage(self):
        uni, _, cal = universe()
        for name in sw.ORDER:
            for t in run(name)["trades"]:
                S = uni[t["symbol"]]
                self.assertEqual(t["entry_date"], next_session(cal, t["signal_date"]), (name, t))
                self.assertEqual(t["entry_open"], S.o[S.idx[t["entry_date"]]])
                self.assertAlmostEqual(t["entry"], t["entry_open"] * 1.001, places=3)
                if t["reason"] not in ("stop", "stop_gap", "target"):     # close-based and time exits: next open
                    self.assertEqual(t["exit_ref"], round(S.o[S.idx[t["exit_date"]]], 4))
                    self.assertAlmostEqual(t["exit"], t["exit_ref"] * 0.999, places=3)

    def _gap_universe(self, day62):
        rows = flat_rows(60)
        d = datetime.date.fromisoformat(rows[-1][0])
        days = [(d + datetime.timedelta(days=k)).isoformat() for k in range(1, 12) if (d + datetime.timedelta(days=k)).weekday() < 5]
        rows.append([days[0], 105.0, 108.0, 104.0, 107.0, 5000])   # gap +5 %, 5x volume, top half -> signal, stop 104
        rows.append([days[1]] + list(day62[0]))                     # the fill day
        rows.append([days[2]] + list(day62[1]))
        for k in range(3, 6):
            rows.append([days[k], 106.0, 106.5, 105.5, 106.0, 1000])
        S = synthetic("GAPCO", rows)
        return {"GAPCO": S}, S.dates[55:]

    def test_gap_through_the_stop_fills_at_the_open(self):
        uni, cal = self._gap_universe([(107.0, 108.0, 106.0, 107.5, 1000), (103.0, 104.5, 102.0, 104.0, 1000)])
        res = sw.simulate(uni, "gap_volume_drift", sw.STRATEGIES["gap_volume_drift"]["default"], cal)
        t = res["trades"][0]
        self.assertEqual((t["reason"], t["stop"], t["exit_ref"]), ("stop_gap", 104.0, 103.0))
        self.assertAlmostEqual(t["exit"], 103.0 * 0.999, places=4)
        self.assertAlmostEqual(t["entry"], 107.0 * 1.001, places=4)

    def test_stop_on_the_low_fills_at_the_stop(self):
        uni, cal = self._gap_universe([(107.0, 108.0, 106.0, 107.5, 1000), (105.0, 105.5, 103.5, 104.5, 1000)])
        t = sw.simulate(uni, "gap_volume_drift", sw.STRATEGIES["gap_volume_drift"]["default"], cal)["trades"][0]
        self.assertEqual((t["reason"], t["exit_ref"]), ("stop", 104.0))
        self.assertAlmostEqual(t["exit"], 104.0 * 0.999, places=4)

    def test_entry_skipped_when_it_opens_under_the_stop(self):
        uni, cal = self._gap_universe([(103.9, 105.0, 103.0, 104.0, 1000), (104.0, 104.5, 103.5, 104.0, 1000)])
        res = sw.simulate(uni, "gap_volume_drift", sw.STRATEGIES["gap_volume_drift"]["default"], cal)
        self.assertEqual(res["trades"], [])
        self.assertEqual(res["skips"], {"opened at or under the stop": 1})

    def test_locked_circuit_day_cannot_be_traded(self):
        uni, cal = self._gap_universe([(110.0, 110.0, 110.0, 110.0, 50), (111.0, 112.0, 110.0, 111.0, 1000)])
        res = sw.simulate(uni, "gap_volume_drift", sw.STRATEGIES["gap_volume_drift"]["default"], cal)
        self.assertEqual(res["trades"] + res["open"], [])
        self.assertEqual(res["skips"], {"locked at a circuit on the fill day (no range): no fill": 1})
        # locked under the stop (104): no sale is possible that day; the next day opens at 106, above it
        uni, cal = self._gap_universe([(107.0, 108.0, 106.0, 107.5, 1000), (100.0, 100.0, 100.0, 100.0, 50)])
        res = sw.simulate(uni, "gap_volume_drift", sw.STRATEGIES["gap_volume_drift"]["default"], cal)
        self.assertEqual(res["trades"], [])
        self.assertEqual([o["symbol"] for o in res["open"]], ["GAPCO"])

    def _test_strategy(self, fires):
        """A throwaway strategy: fires {date: score} for each symbol; hold 3 sessions."""
        def entry(S, i, p):
            sc = fires.get((S.sym, S.dates[i]))
            return None if sc is None else {"score": sc, "stop": None, "why": "test"}
        sw.STRATEGIES["_t"] = {"title": "t", "rules": "t", "score": "t", "entry": entry, "exit": None,
                               "default": {"max_days": 3}, "variants": [{"max_days": 3}]}
        self.addCleanup(sw.STRATEGIES.pop, "_t", None)

    def test_slots_go_to_the_best_scores(self):
        uni = dict((s, synthetic(s, flat_rows(30))) for s in ("AAA", "BBB", "CCC"))
        cal = uni["AAA"].dates
        d = cal[10]
        self._test_strategy({("AAA", d): 1.0, ("BBB", d): 3.0, ("CCC", d): 2.0})
        res = sw.simulate(uni, "_t", {"max_days": 3}, cal, {"max_positions": 2, "costs": False})
        self.assertEqual(sorted(t["symbol"] for t in res["trades"]), ["BBB", "CCC"])
        self.assertEqual(res["skips"], {"no free slot": 1})
        t = res["trades"][0]
        self.assertEqual((t["entry_date"], t["exit_date"], t["held"]), (cal[11], cal[14], 3))
        self.assertEqual(t["qty"], 500)                      # Rs 1,00,000 / 2 slots / Rs 100

    def test_compounding_sizes_from_the_last_equity(self):
        rows = flat_rows(30)
        for k, r in enumerate(rows):                          # price doubles on day 12 and stays
            if k >= 12:
                rows[k] = [r[0], 200.0, 200.5, 199.5, 200.0, 1000]
        uni = {"AAA": synthetic("AAA", rows), "BBB": synthetic("BBB", flat_rows(30))}
        cal = uni["AAA"].dates
        self._test_strategy({("AAA", cal[10]): 1.0, ("BBB", cal[20]): 1.0})
        res = sw.simulate(uni, "_t", {"max_days": 3}, cal, {"max_positions": 2, "costs": False})
        a, b = res["trades"]
        self.assertEqual((a["symbol"], a["qty"], a["exit_ref"]), ("AAA", 500, 200.0))
        self.assertEqual(res["equity"][-1], 150000.0)
        self.assertEqual((b["symbol"], b["qty"]), ("BBB", 750))   # Rs 1,50,000 / 2 / Rs 100

    def test_risk_sizing_is_capped_at_equal_weight(self):
        uni, cal = self._gap_universe([(107.0, 108.0, 106.0, 107.5, 1000), (106.0, 107.0, 105.0, 106.0, 1000)])
        t = sw.simulate(uni, "gap_volume_drift", sw.STRATEGIES["gap_volume_drift"]["default"], cal,
                        {"sizing": "risk", "costs": False, "risk_pct": 0.1})["open"][0]
        self.assertEqual(t["qty"], 33)                        # 0.1 % of 1L = Rs 100 / (107 - 104) = 33 shares
        t = sw.simulate(uni, "gap_volume_drift", sw.STRATEGIES["gap_volume_drift"]["default"], cal,
                        {"sizing": "risk", "costs": False})["open"][0]
        self.assertEqual(t["qty"], 93)                        # 1 %: 333 shares, capped at Rs 10,000 / 107

    def test_monthly_rotation_dates(self):
        cal = ["2024-01-30", "2024-01-31", "2024-02-01", "2024-02-29", "2024-03-01", "2024-03-04"]
        self.assertEqual(sw.month_ends(cal), {"2024-01-31", "2024-02-29"})
        uni, _, cal = universe()
        ends = sw.month_ends(cal)
        tr = run("momentum_rotation")["trades"]
        self.assertTrue(tr)
        for t in tr:
            self.assertIn(t["signal_date"], ends)
            self.assertNotEqual(t["entry_date"][:7], t["signal_date"][:7])
            self.assertIn(t["exit_date"], [next_session(cal, e) for e in ends])


# ================================================================ metrics

class Metrics(unittest.TestCase):
    def test_cagr_and_drawdown_on_a_synthetic_curve(self):
        dates = ["2020-06-01", "2021-01-01", "2021-06-01", "2022-01-01"]
        eq = [120.0, 90.0, 130.0, 121.0]
        st = sw.curve_stats(dates, eq, "2020-01-01", 100.0)
        years = (datetime.date(2022, 1, 1) - datetime.date(2020, 1, 1)).days / 365.25
        self.assertAlmostEqual(st["cagr_pct"], round(100 * (1.21 ** (1 / years) - 1), 2))
        self.assertEqual(st["total_return_pct"], 21.0)
        self.assertEqual(st["max_dd_pct"], 25.0)              # 120 -> 90
        self.assertEqual((st["max_dd_peak"], st["max_dd_trough"], st["max_dd_recovered"]),
                         ("2020-06-01", "2021-01-01", "2021-06-01"))
        self.assertEqual(st["max_dd_days"], (datetime.date(2021, 6, 1) - datetime.date(2020, 6, 1)).days)

    def test_wiped_out_account_is_minus_100(self):
        st = sw.curve_stats(["2020-01-02", "2021-01-02"], [50.0, -1.0], "2020-01-01", 100.0)
        self.assertEqual(st["cagr_pct"], -100.0)

    def test_unrecovered_drawdown_runs_to_the_end(self):
        st = sw.curve_stats(["2020-01-02", "2020-01-03", "2020-02-03"], [110.0, 99.0, 100.0], "2020-01-01", 100.0)
        self.assertEqual((st["max_dd_pct"], st["max_dd_recovered"], st["max_dd_days"]), (10.0, None, 32))

    def test_sharpe_and_year_table(self):
        dates = ["2020-12-30", "2020-12-31", "2021-01-01", "2021-01-04"]
        eq = [101.0, 102.01, 101.0, 103.02]
        yt = sw.year_table(dates, eq, "2020-12-29", 100.0)
        self.assertEqual(yt, {"2020": 2.01, "2021": 0.99})
        st = sw.curve_stats(dates, eq, "2020-12-29", 100.0)
        self.assertIsNotNone(st["sharpe"])

    def test_walk_forward_split(self):
        self.assertEqual(sw.wf_split(["2015-01-01", "2020-01-01", "2026-10-29"]), "2021-12-31")
        cal = ["2019-%02d-01" % m for m in range(1, 13)]
        self.assertEqual(sw.wf_split(cal), cal[7])           # data starts late: 2/3 : 1/3

    def test_oos_counts_only_trades_entered_after_the_split(self):
        uni, _, cal = universe()
        res = run("pullback_rsi2")
        is_end = sw.wf_split(cal)
        full, ins, oos = sw.evaluate(res, is_end, "2017-12-29")
        self.assertEqual(oos["trades"], sum(1 for t in res["trades"] if t["entry_date"] > is_end))
        self.assertEqual(ins["trades"], sum(1 for t in res["trades"] if t["exit_date"] <= is_end))
        self.assertGreater(oos["start"], is_end)

    def test_regimes_use_the_previous_close(self):
        _, bench, cal = universe()
        reg = sw.regimes(bench[sw.NIFTY], bench[sw.VIX], cal)
        N = bench[sw.NIFTY]
        d = "2020-04-01"
        i = N.idx["2020-03-31"]
        s200 = N.ind("sma", 200)
        want = "up" if N.c[i] > s200[i] and s200[i] > s200[i - 20] else (
            "down" if N.c[i] < s200[i] and s200[i] < s200[i - 20] else "sideways")
        self.assertEqual(reg[d][0], want)
        self.assertEqual(reg[d][0], "down")                  # Covid crash
        self.assertEqual(reg[d][1], "vix_high")

    def test_verdict_wording(self):
        self.assertEqual(sw.verdict({"trades": 0}, {}, None), "never fired: nothing to judge")
        self.assertEqual(sw.verdict({"trades": 12}, {}, None), "too few trades to judge (12)")
        v = sw.verdict({"trades": 300, "cagr_pct": -1.0}, {}, {"cagr_pct": 2.0})
        self.assertTrue(v.startswith("no edge after costs"))
        v = sw.verdict({"trades": 300, "cagr_pct": 4.0, "t_stat": 3.0}, {"cagr_pct": -2.0, "trades": 50,
                                                                       "start": "2022-01-03"}, {"cagr_pct": 6.0})
        self.assertIn("did not hold out of sample", v)
        v = sw.verdict({"trades": 300, "cagr_pct": 4.0, "t_stat": 3.0}, {"cagr_pct": 2.0, "trades": 50}, {"cagr_pct": 6.0})
        self.assertTrue(v.startswith("positive after costs"))
        self.assertIn("did not beat simply holding the index",
                      sw.vs_index({"cagr_pct": 5.0, "max_dd_pct": 20}, {"cagr_pct": 9.0, "max_dd_pct": 38},
                                  {"cagr_pct": 10.0}))
        self.assertEqual(sw.vs_index({"cagr_pct": 14.0, "max_dd_pct": 48}, {"cagr_pct": 10.0, "max_dd_pct": 38},
                                     {"cagr_pct": 11.0}),
                         "beat Nifty by 4.0 % a year (3.0 % over NIFTYBEES incl. dividends) with 10 pts deeper "
                         "drawdown (48 % vs 38 %)")


# ================================================================ one hand-checked trade per strategy

class Strategies(unittest.TestCase):
    def first(self, name):
        uni, _, cal = universe()
        t = run(name)["trades"][0]
        S = uni[t["symbol"]]
        return t, S, S.idx[t["signal_date"]], S.idx[t["entry_date"]], S.idx[t["exit_date"]]

    def held_closes(self, S, e, x):
        return range(e, x)                                   # closes from the entry day to the day before the exit

    def test_pullback_rsi2(self):
        t, S, i, e, x = self.first("pullback_rsi2")
        self.assertEqual((t["symbol"], t["signal_date"], t["entry_date"], t["exit_date"], t["qty"]),
                         ("RELIANCE", "2018-01-01", "2018-01-02", "2018-01-05", 23))
        self.assertGreater(S.c[i], my_sma(S.c, i, 200))
        self.assertLess(my_rsi(S.c, i, 2), 10)
        self.assertAlmostEqual(t["entry"], 417.3929 * 1.001, places=3)
        # out at the open after the first close above the 5-day SMA (2018-01-04)
        self.assertGreater(S.c[x - 1], my_sma(S.c, x - 1, 5))
        for k in range(e, x - 1):
            self.assertLessEqual(S.c[k], my_sma(S.c, k, 5))
        self.assertEqual(t["exit_ref"], S.o[x])
        bv, sv = 23 * t["entry"], 23 * t["exit"]
        ch = nse.delivery_charges(bv, 0)["total"] + nse.delivery_charges(0, sv)["total"]
        self.assertAlmostEqual(t["net_inr"], round(sv - bv - ch, 2), places=1)
        self.assertEqual(t["qty"], int(10000 / (t["entry"] * 1.0015)))

    def test_oversold_bounce_d(self):
        t, S, i, e, x = self.first("oversold_bounce_d")
        self.assertEqual((t["symbol"], t["signal_date"], t["entry_date"], t["exit_date"], t["reason"]),
                         ("HDFCBANK", "2018-09-11", "2018-09-12", "2018-10-03", "RSI(14) above 50"))
        self.assertGreater(S.c[i], my_sma(S.c, i, 200))
        self.assertLess(my_rsi(S.c, i, 14), 30)
        self.assertGreater(my_rsi(S.c, x - 1, 14), 50)
        self.assertEqual(t["held"], 12)

    def test_breakout_20d(self):
        t, S, i, e, x = self.first("breakout_20d")
        self.assertEqual((t["symbol"], t["signal_date"], t["entry_date"], t["exit_date"]),
                         ("ICICIBANK", "2018-01-15", "2018-01-16", "2018-02-05"))
        self.assertGreater(S.c[i], max(S.c[i - 20:i]))
        self.assertLess(S.c[x - 1], min(S.l[x - 11:x - 1]))   # close under the 10 lows before it
        for k in range(e, x - 1):
            self.assertGreaterEqual(S.c[k], min(S.l[k - 10:k]))

    def test_high_52w(self):
        t, S, i, e, x = self.first("high_52w")
        self.assertEqual((t["symbol"], t["signal_date"], t["entry_date"], t["exit_date"]),
                         ("RELIANCE", "2018-01-22", "2018-01-23", "2018-02-05"))
        self.assertGreater(S.c[i], max(S.c[i - 252:i]))
        self.assertGreaterEqual(S.v[i], 1.5 * sum(S.v[i - 50:i]) / 50)
        self.assertLess(S.c[x - 1], my_sma(S.c, x - 1, 50))

    def test_ma_cross(self):
        t, S, i, e, x = self.first("ma_cross")
        self.assertEqual((t["symbol"], t["signal_date"], t["entry_date"], t["exit_date"]),
                         ("ITC", "2018-05-16", "2018-05-17", "2018-12-17"))
        self.assertGreater(my_sma(S.c, i, 50), my_sma(S.c, i, 200))
        self.assertLessEqual(my_sma(S.c, i - 1, 50), my_sma(S.c, i - 1, 200))
        self.assertLess(my_sma(S.c, x - 1, 50), my_sma(S.c, x - 1, 200))

    def test_momentum_rotation(self):
        uni, _, cal = universe()
        t, S, i, e, x = self.first("momentum_rotation")
        self.assertEqual((t["signal_date"], t["entry_date"], t["exit_date"]), ("2018-01-31", "2018-02-01", "2018-03-01"))
        # by hand: the 12-1 month return of every stock above its 200-day SMA on 2018-01-31; all 8 qualify
        moms = {}
        for s, T in uni.items():
            k = T.idx["2018-01-31"]
            if T.c[k] > my_sma(T.c, k, 200):
                moms[s] = T.c[k - 21] / T.c[k - 252] - 1
        first_buys = sorted(tr["symbol"] for tr in run("momentum_rotation")["trades"] + [
            {"symbol": o["symbol"], "entry_date": o["entry_date"]} for o in run("momentum_rotation")["open"]]
            if tr["entry_date"] == "2018-02-01")
        self.assertEqual(first_buys, sorted(moms))            # 8 stocks, 10 slots: every eligible one
        self.assertEqual(t["qty"], int(10000 / (t["entry"] * 1.0015)))

    def test_gap_volume_drift(self):
        t, S, i, e, x = self.first("gap_volume_drift")
        self.assertEqual((t["symbol"], t["signal_date"], t["entry_date"], t["exit_date"], t["reason"]),
                         ("ICICIBANK", "2018-05-08", "2018-05-09", "2018-05-16", "stop"))
        self.assertGreaterEqual(S.o[i] / S.c[i - 1] - 1, 0.04)
        self.assertGreaterEqual(S.v[i], 3 * sum(S.v[i - 50:i]) / 50)
        self.assertGreaterEqual(S.c[i], (S.h[i] + S.l[i]) / 2)
        self.assertEqual(t["stop"], S.l[i])                   # the gap day's low
        self.assertGreater(S.o[x], t["stop"])                 # no gap through: filled at the stop
        self.assertLessEqual(S.l[x], t["stop"])
        self.assertAlmostEqual(t["exit"], t["stop"] * 0.999, places=4)

    def test_bull_flag_d(self):
        t, S, i, e, x = self.first("bull_flag_d")
        self.assertEqual((t["symbol"], t["signal_date"], t["entry_date"], t["exit_date"], t["reason"]),
                         ("TCS", "2018-04-20", "2018-04-23", "2018-05-22", "time (20 sessions)"))
        c = S.c[:i]                                          # india-scanner's rule on the closes to yesterday
        self.assertGreater(c[-6] / c[-16] - 1, 0.10)
        self.assertLess((max(c[-5:]) - min(c[-5:])) / c[-1], 0.05)
        self.assertGreater(S.c[i], max(S.h[i - 5:i]))
        self.assertEqual(t["stop"], min(S.l[i - 5:i]))
        self.assertAlmostEqual(t["target"], t["entry"] + 2 * (t["entry"] - t["stop"]), places=3)
        self.assertEqual(t["held"], 20)


# ================================================================ end to end

class CLI(unittest.TestCase):
    def test_compare_writes_json_md_and_a_small_dash(self):
        d = tempfile.mkdtemp()
        old = os.environ.get("TRADER_DIR")
        try:
            os.environ["TRADER_DIR"] = d
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = bt.main(["swing", "compare", "--universe", ",".join(SYMS), "--offline", SF, "--core-list",
                              os.path.join(SF, "ind_nifty100list.csv"), "--from", "2018-01-01",
                              "--now", "2026-10-29 18:00", "--quiet"])
            self.assertEqual(rc, 0, err.getvalue())
            text = out.getvalue()
            self.assertIn("bench_nifty50", text)
            self.assertIn("SURVIVORSHIP BIAS", text)
            files = sorted(os.listdir(os.path.join(d, "backtest")))
            self.assertEqual(files, ["2026-10-29-swing-8-stocks.json", "2026-10-29-swing-8-stocks.md",
                                     "2026-10-29-swing-dash.json"])
            path = os.path.join(d, "backtest", "2026-10-29-swing-dash.json")
            self.assertLess(os.path.getsize(path), 1000000)
            dash = nse.read_json(path)
            self.assertEqual(set(dash), {"made", "sw_strategies", "sw_equity", "sw_years", "sw_regimes", "sw_meta"})
            row = dash["sw_strategies"][0]
            for k in ("id", "universe", "strategy", "title", "rules", "variant", "cagr_pct", "total_return_pct",
                      "max_dd_pct", "max_dd_days", "sharpe", "exposure_pct", "trades", "win_pct", "avg_trade_pct",
                      "avg_hold_days", "profit_factor", "best_year_pct", "worst_year_pct", "oos_cagr_pct",
                      "oos_max_dd_pct", "bench_cagr_pct", "excess_cagr_pct", "costs_pct_of_gross", "verdict",
                      "vs_index"):
                self.assertIn(k, row)
            self.assertEqual(len(dash["sw_strategies"]), 8)
            strategies = set(r["strategy"] for r in dash["sw_equity"])
            self.assertTrue({"bench_nifty50", "bench_niftybees", "pullback_rsi2"} <= strategies)
            self.assertEqual(len(dash["sw_meta"]), 1)
            self.assertIn("Survivorship", dash["sw_meta"][0]["survivorship_note"])
            doc = nse.read_json(os.path.join(d, "backtest", "2026-10-29-swing-8-stocks.json"))
            self.assertEqual(doc["meta"]["split"]["in_sample"][0], "2018-01-01")
            nb = doc["benchmarks"]["bench_nifty50"]["full"]
            N = universe()[1][sw.NIFTY]
            years = (datetime.date(2026, 10, 29) - datetime.date(2017, 12, 29)).days / 365.25
            want = 100 * ((N.close_on("2026-10-29") / N.close_on("2017-12-29")) ** (1 / years) - 1)
            self.assertAlmostEqual(nb["cagr_pct"], round(want, 2), places=2)
        finally:
            if old is None:
                os.environ.pop("TRADER_DIR", None)
            else:
                os.environ["TRADER_DIR"] = old
            shutil.rmtree(d)

    def test_list_and_unknown_strategy(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(bt.main(["swing", "list"]), 0)
        for n in sw.ORDER:
            self.assertIn(n, out.getvalue())
        with self.assertRaises(sw.SwingError):
            sw.parse_strategies("nope")


if __name__ == "__main__":
    unittest.main()
