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

scan = load("india-scanner", "scan")

SDIR = os.path.join(FIX, "india-scanner")
WALL = os.path.join(SDIR, "nifty5m-2026-10-29.json.gz")     # 35 real Yahoo v8 5-min charts, Thu Oct 29 2026
SPARK_A = os.path.join(SDIR, "spark-3mo-1d-a.json")         # real spark, 20 asked (TATAMOTORS, ZZZZQX not returned)
SPARK_B = os.path.join(SDIR, "spark-3mo-1d-b.json")         # real spark, 20 more Nifty 50 names
SPARK5 = os.path.join(SDIR, "spark-1d-5m.json")             # real spark 5-minute closes, 20 names, after the close
FO = os.path.join(SDIR, "fo_mktlots.csv")                   # NSE F&O market lots, Oct 29 2026
EQ = os.path.join(COMMON, "EQUITY_L.csv")
N500 = os.path.join(COMMON, "n500.csv")
LISTS = ["--equity-list", EQ, "--index-list", N500, "--fo-list", FO]
DT = datetime.datetime
D = datetime.date
DAY = D(2026, 10, 29)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


def lists():
    return scan.Lists(EQ, N500, FO)


def bars_series(day, closes, start="09:15", highs=None):
    t0 = DT.strptime(day + " " + start, "%Y-%m-%d %H:%M")
    bars = []
    for i, c in enumerate(closes):
        if c is None:
            continue
        h = highs[i] if highs else c + 0.1
        bars.append({"t": t0 + datetime.timedelta(minutes=5 * i), "o": c, "h": h, "l": c - 0.1, "c": c, "v": 1000})
    return {"bars": bars, "meta": {}}


class Universe(unittest.TestCase):
    def test_nifty500_is_eq_only(self):
        kept, counts = scan.build_universe(lists(), "nifty500")
        self.assertEqual(counts["listed"], 501)
        self.assertEqual(len(kept), 496)
        self.assertEqual(counts["series BE"], 2)                 # HFCL, STLTECH: trade-to-trade
        self.assertEqual(counts["series RR"], 3)                 # REITs (BAGMANE, BIRET, EMBASSY)
        syms = dict(kept)
        self.assertEqual(syms["RELIANCE"], "Reliance Industries Limited")   # EQUITY_L's name wins
        self.assertIn("M&M", syms)
        eq = nse.parse_equity_list(nse.read_maybe_gz(EQ).decode("utf-8"))
        self.assertTrue(all(eq[s]["series"] == "EQ" for s in syms if s in eq))
        self.assertNotIn("BAGMANE", syms)       # not in EQUITY_L: the index's own series (RR) decides

    def test_all_drops_trade_to_trade(self):
        kept, counts = scan.build_universe(lists(), "all")
        self.assertEqual((len(kept), counts["series BE"], counts["series BZ"]), (2368, 219, 33))

    def test_fo_list(self):
        with open(FO, encoding="utf-8") as fh:
            fo = scan.parse_fo_list(fh.read())
        self.assertIn("RELIANCE", fo)
        self.assertIn("M&M", fo)
        self.assertNotIn("NIFTY", fo)          # index rows are dropped
        self.assertNotIn("BANKNIFTY", fo)
        self.assertNotIn("SYMBOL", fo)
        self.assertGreater(len(fo), 200)

    def test_series_check_for_given_symbols(self):
        eq = nse.parse_equity_list(nse.read_maybe_gz(EQ).decode("utf-8"))
        be = sorted(s for s, r in eq.items() if r["series"] == "BE")[0]
        bz = sorted(s for s, r in eq.items() if r["series"] == "BZ")[0]
        kept, dropped = scan.check_series(["RELIANCE", be, bz, "NOTASTOCK"], lists())
        self.assertEqual(kept, ["RELIANCE"])
        self.assertIn("trade-to-trade", dropped[be])
        self.assertIn("trade-to-trade", dropped[bz])
        self.assertIn("not in NSE", dropped["NOTASTOCK"])

    def test_universe_cli(self):
        d = tempfile.mkdtemp()
        try:
            out = os.path.join(d, "universe.txt")
            code, text, _ = run(["universe", "--out", out] + LISTS)
            self.assertEqual(code, 0)
            self.assertIn("kept 496 series-EQ stocks", text)
            with open(out, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            self.assertEqual(len(lines), 496)
            self.assertIn("RELIANCE|Reliance Industries Limited|F&O", lines)
        finally:
            shutil.rmtree(d)


class Spark(unittest.TestCase):
    def test_a_real_spark_answer(self):
        with open(SPARK_A, encoding="utf-8") as fh:
            data = scan.parse_spark(fh.read())
        self.assertEqual(len(data), 18)               # 20 asked: TATAMOTORS (renamed) and ZZZZQX dropped
        self.assertNotIn("TATAMOTORS", data)
        self.assertIn("M&M", data)                    # M&M.NS comes back as plain M&M
        r = data["RELIANCE"]
        self.assertIsNone(r["bars"][0]["h"])          # spark is closes only
        self.assertEqual(r["bars"][-1]["t"], DT(2026, 10, 29, 9, 15))   # daily bars stamped 09:15 IST
        self.assertEqual(r["bars"][-1]["c"], 1167.4)
        self.assertEqual(r["meta"]["regularMarketVolume"], 20750193)
        closes = scan.daily_closes(r, DT(2026, 10, 29, 15, 29))
        self.assertEqual(closes[-1][0], D(2026, 10, 28))      # today's bar in progress until 15:30
        closes = scan.daily_closes(r, DT(2026, 10, 29, 15, 30))
        self.assertEqual(closes[-1], (D(2026, 10, 29), 1167.4))

    def test_urls_and_errors(self):
        self.assertEqual(scan.spark_path(["M&M", "reliance"], "3mo", "1d"),
                         "/v7/finance/spark?symbols=M%26M.NS,RELIANCE.NS&range=3mo&interval=1d")
        self.assertEqual(scan.chart_path("BAJAJ-AUTO", "1d", "5m"),
                         "/v8/finance/chart/BAJAJ-AUTO.NS?range=1d&interval=5m")
        too_many = ('{"spark":{"result":null,"error":{"code":"Bad Request",'
                    '"description":"Number of symbols needs to be less than or equal to 20"}}}')
        with self.assertRaises(scan.ScanError) as cm:
            scan.parse_spark(too_many)
        self.assertIn("less than or equal to 20", str(cm.exception))
        self.assertEqual(scan.parse_spark('{"spark":{"result":[],"error":null}}'), {})


class Network(unittest.TestCase):
    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = scan.GET, scan.SLEEP
        scan.SLEEP = self.sleeps.append
        with open(SPARK_A, "rb") as fh:
            self.body = fh.read()

    def tearDown(self):
        scan.GET, scan.SLEEP = self._get, self._sleep

    def answers(self, codes):
        codes = list(codes)

        def fake(url, retries=1, **kw):
            self.calls.append(url.split(".finance")[0])
            code = codes.pop(0) if codes else 200
            if code != 200:
                raise nse.NSEError("HTTP %d from query1.finance.yahoo.com" % code)
            return self.body
        scan.GET = fake

    def test_429_falls_back_then_waits_once(self):
        self.answers([429, 429, 200])
        self.assertEqual(scan.yahoo_get("/v7/finance/spark?symbols=TCS.NS"), self.body)
        self.assertEqual(self.calls, ["https://query1", "https://query2", "https://query1"])
        self.assertEqual(self.sleeps, [scan.BACKOFF])

    def test_a_404_is_final(self):
        self.answers([404])
        with self.assertRaises(scan.ScanError):
            scan.yahoo_get("/v8/finance/chart/NOPE.NS?range=1d&interval=5m")
        self.assertEqual(len(self.calls), 1)

    def test_batches_of_20_and_partial(self):
        self.answers([200] + [429] * 10)
        syms = ["RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "SBIN", "BHARTIARTL", "ITC", "LT",
                "HINDUNILVR", "AXISBANK", "KOTAKBANK", "BAJFINANCE", "MARUTI", "SUNPHARMA", "TATAMOTORS", "M&M",
                "NTPC", "TITAN", "ZZZZQX"] + ["X%02d" % i for i in range(20)]
        data, failed = scan.fetch_spark(syms, "3mo", "1d", 0.5)
        self.assertEqual(len(data), 18)
        self.assertEqual(failed["TATAMOTORS"], "no data from Yahoo")
        self.assertEqual(len(failed), 22)
        self.assertIn("HTTP 429", failed["X00"])
        self.assertEqual(self.sleeps, [0.5, scan.BACKOFF])


class Daily(unittest.TestCase):
    # Real closes through Thu Oct 29 2026 (scan after the 15:30 close), hand-checked where noted.
    AFTER_CLOSE = {
        "KOTAKBANK": ["breakout_20d"], "ADANIPORTS": ["breakout_20d"], "LT": ["breakout_20d"],
        "DRREDDY": ["breakout_20d"], "INFY": ["bull_flag"], "TCS": ["bull_flag"], "HCLTECH": ["bull_flag"],
        "SBIN": ["reclaim_ma20"], "ITC": ["reclaim_ma20"], "HINDUNILVR": ["reclaim_ma20"],
        "TITAN": ["reclaim_ma20"], "POWERGRID": ["reclaim_ma20"], "INDUSINDBK": ["reclaim_ma20"],
        "GRASIM": ["reclaim_ma20"],
    }
    # Pre-market the same morning: closes through Wed Oct 28
    PRE_MARKET = {
        "INFY": ["bull_flag"], "ADANIPORTS": ["reclaim_ma20"], "TCS": ["bull_flag"],
        "LT": ["breakout_20d", "reclaim_ma20"],   # 3801.30 > 3770.00 (best of the 20 before); 3665.10 under
                                                  # its average 3682.54, then over 3684.28
        "M&M": ["reclaim_ma20"], "APOLLOHOSP": ["reclaim_ma20"], "HCLTECH": ["bull_flag"],
        "DRREDDY": ["breakout_20d"],
    }

    @classmethod
    def setUpClass(cls):
        cls.data = {}
        for f in (SPARK_A, SPARK_B):
            d, _ = scan.load_offline(f)
            cls.data.update(d)
        cls.syms = sorted(cls.data)

    def test_after_the_close(self):
        hits, counts, last = scan.scan_daily(self.data, self.syms, DT(2026, 10, 29, 16, 0), 0, 0)
        self.assertEqual(dict((h["symbol"], h["setups"]) for h in hits), self.AFTER_CLOSE)
        self.assertEqual(last, DAY)
        self.assertEqual(counts["scanned"], 38)
        self.assertEqual(hits[0]["symbol"], "KOTAKBANK")      # most rupees traded first
        k = hits[0]
        self.assertEqual((k["price"], k["close_date"], k["exchange"]), (459.0, "2026-10-29", "NSE"))

    def test_pre_market(self):
        hits, counts, last = scan.scan_daily(self.data, self.syms, DT(2026, 10, 29, 9, 0), 0, 0)
        self.assertEqual(dict((h["symbol"], h["setups"]) for h in hits), self.PRE_MARKET)
        self.assertEqual(last, D(2026, 10, 28))
        during = scan.scan_daily(self.data, self.syms, DT(2026, 10, 29, 11, 0), 0, 0)
        self.assertEqual(during[0], hits)                     # the in-progress bar is dropped

    def test_price_and_turnover_floors(self):
        _, counts, _ = scan.scan_daily(self.data, self.syms, DT(2026, 10, 29, 16, 0), 50, 10 * scan.CRORE)
        self.assertEqual(counts["illiquid"], 0)               # Nifty 50 names all trade over Rs 10 crore
        hits, counts, _ = scan.scan_daily(self.data, self.syms, DT(2026, 10, 29, 16, 0), 50, 500 * scan.CRORE)
        self.assertTrue(all(h["turnover_cr"] >= 500 for h in hits))
        self.assertGreater(counts["illiquid"], 10)
        _, counts, _ = scan.scan_daily(self.data, self.syms, DT(2026, 10, 29, 16, 0), 5000, 0)
        self.assertEqual(counts["scanned"], 4)                # BAJAJ-AUTO, MARUTI, APOLLOHOSP, EICHERMOT

    def test_holiday_today_counts_as_closed(self):
        s = {"bars": [{"t": DT(2026, 10, 19 + i, 9, 15), "c": 10.0 + i} for i in range(3)], "meta": {}}
        # Oct 20 2026 is Dussehra: a bar that day (there is none in real data) is not "in progress"
        self.assertEqual(len(scan.daily_closes(s, DT(2026, 10, 20, 11, 0))), 2)

    def test_rsi_and_oversold(self):
        self.assertIsNone(scan.rsi([1.0] * 10))
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
                  46.28, 46.28]
        self.assertAlmostEqual(scan.rsi(closes), 70.46, places=1)   # Wilder's textbook example
        falling = [100.0 - i for i in range(35)]
        self.assertIn("oversold", scan.find_setups(falling)[0])
        self.assertIn("oversold_bounce", scan.find_setups(falling + [67.0])[0])

    def test_short_charts(self):
        s = {"bars": [{"t": DT(2026, 10, d, 9, 15), "c": 100.0} for d in (21, 22, 23)],
             "meta": {"regularMarketPrice": 100, "regularMarketVolume": 10 ** 7}}
        hits, counts, last = scan.scan_daily({"X": s}, ["X", "GONE"], DT(2026, 10, 29, 9, 0), 50, 0)
        self.assertEqual((hits, counts["short"], counts["fetched"], last), ([], 1, 1, None))


class Orb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.errors = scan.load_offline(WALL)
        cls.syms = sorted(cls.data)

    def at(self, hhmm, syms=None, min_price=0, or_minutes=15, confirm=None, max_confirm=None):
        day, cut = scan.orb_window(DT(2026, 10, 29, *map(int, hhmm.split(":"))), None, or_minutes,
                                   datetime.time(14, 30))
        return scan.scan_orb(self.data, syms or self.syms, day, cut, or_minutes, min_price, 0, confirm, max_confirm)

    def test_reliance_by_hand(self):
        # range 09:15-09:25 bars: high 1170.50 (09:25), low 1156.20 (09:15); the 09:30 bar closed 1169.50,
        # the 09:35 bar closed 1174.30 > 1170.50 -> a hit at 09:40, 0.32 % over
        hits, counts, _ = self.at("10:15", ["RELIANCE"])
        self.assertEqual(hits, [{"symbol": "RELIANCE", "exchange": "NSE", "setups": ["orb"], "price": 1174.3,
                                 "at": "09:40", "bar_start": "09:35", "bar_index": 4, "or_high": 1170.5,
                                 "or_low": 1156.2, "pct_over": 0.32}])
        hits, _, _ = self.at("09:39", ["RELIANCE"])
        self.assertEqual(hits, [])                            # the 09:35 bar is not done until 09:40

    def test_sbin_and_tcs(self):
        hits, _, _ = self.at("10:15", ["SBIN", "TCS"])
        # SBIN range high 961.50; first close above: the 10:05 bar at 961.65. TCS fell from its range.
        self.assertEqual([(h["symbol"], h["at"], h["price"]) for h in hits], [("SBIN", "10:10", 961.65)])

    def test_the_whole_wall_at_1015(self):
        hits, counts, failed = self.at("10:15", min_price=50)
        self.assertEqual(len(self.syms), 35)
        self.assertEqual(counts["illiquid"], 2)               # IDEA and YESBANK under Rs 50
        self.assertEqual(counts["scanned"], 33)
        self.assertEqual(len(hits), 19)
        self.assertEqual([h["at"] for h in hits], sorted(h["at"] for h in hits))
        self.assertEqual(hits[0]["symbol"], "ADANIENT")       # 09:35, then by symbol

    def test_stage_one_is_necessary(self):
        day, cut = DAY, DT(2026, 10, 29, 11, 0)
        for sym, s in self.data.items():
            bars = scan.session_bars(s, day, cut)
            if scan.usable(bars, day, cut, 3) and scan.orb_hit(bars, day, 3):
                self.assertTrue(scan.orb_candidate(bars, day, 3), sym)

    def test_confirm_with_v8_highs_and_the_cap(self):
        spark_like = dict((k, {"bars": [dict(b, h=None, l=None, o=None) for b in v["bars"]], "meta": v["meta"]})
                          for k, v in self.data.items())
        asked = []

        def confirm(sym):
            asked.append(sym)
            return self.data[sym]
        day, cut = DAY, DT(2026, 10, 29, 10, 15)
        hits, counts, _ = scan.scan_orb(spark_like, self.syms, day, cut, 15, 0, 0, confirm)
        full, _, _ = self.at("10:15")
        self.assertEqual(hits, full)
        self.assertEqual(counts["candidates"], len(asked))
        self.assertLess(len(asked), 35)
        asked[:] = []
        capped, counts, _ = scan.scan_orb(spark_like, self.syms, day, cut, 15, 0, 0, confirm, max_confirm=5)
        self.assertEqual((counts["confirmed"], len(asked)), (5, 5))
        self.assertEqual(counts["unconfirmed"], counts["candidates"] - 5)
        for h in capped:
            self.assertIn(h, full)

    def test_real_spark_5m_answer(self):
        with open(SPARK5, encoding="utf-8") as fh:
            spark = scan.parse_spark(fh.read())
        self.assertEqual(len(spark), 20)
        r = spark["RELIANCE"]
        self.assertIsNone(r["bars"][0]["h"])
        bars = scan.session_bars(r, DAY, DT(2026, 10, 29, 10, 15))
        self.assertEqual([hm.strftime("%H:%M") for hm in (bars[0]["t"], bars[-1]["t"])], ["09:15", "10:10"])
        self.assertTrue(scan.orb_candidate(bars, DAY, 3))

    def test_yahoo_drops_bars_and_cas(self):
        # a bar missing after the range is tolerated (Yahoo drops one now and then), a range bar is not
        closes = [100, 100.2, 100.1, 100.0, None, 100.6, 100.7]
        s = bars_series("2026-10-29", closes)
        cut = DT(2026, 10, 29, 9, 50)
        self.assertTrue(scan.usable(scan.session_bars(s, DAY, cut), DAY, cut, 3))
        self.assertFalse(scan.usable(scan.session_bars(s, DAY, cut), DAY, cut, 3, max_missing=0))
        s = bars_series("2026-10-29", [100, None, 100.1, 100.0, 100.5, 100.6, 100.7])
        self.assertFalse(scan.usable(scan.session_bars(s, DAY, cut), DAY, cut, 3))
        # after 15:15 the closing auction: F&O stocks have no 15:15-15:25 bars, nothing is "missing"
        self.assertEqual(scan.expected_slots(DAY, DT(2026, 10, 29, 15, 30)), 72)
        bars = scan.session_bars(self.data["RELIANCE"], DAY, DT(2026, 10, 29, 15, 30))
        self.assertEqual((len(bars), bars[-1]["t"]), (73, DT(2026, 10, 29, 15, 15)))   # 15:20/15:25 null

    def test_window_messages(self):
        until = datetime.time(14, 30)

        def msg(now, at=None, until=until):
            with self.assertRaises(scan.ScanError) as cm:
                scan.orb_window(now, at, 15, until)
            return str(cm.exception)
        self.assertIn("weekend", msg(DT(2026, 10, 31, 10, 15)))
        self.assertIn("Dussehra", msg(DT(2026, 10, 20, 10, 15)))
        self.assertIn("Run orb again after 09:35", msg(DT(2026, 10, 29, 9, 30)))
        self.assertIn("expired", msg(DT(2026, 10, 29, 14, 45)))
        self.assertIn("closed at 15:30 IST", msg(DT(2026, 10, 29, 15, 41), until=None))
        self.assertIn("later than now", msg(DT(2026, 10, 29, 10, 0), at=datetime.time(10, 15)))
        self.assertEqual(scan.orb_window(DT(2026, 10, 29, 15, 41), datetime.time(10, 15), 15, until),
                         (DAY, DT(2026, 10, 29, 10, 15)))
        with self.assertRaises(scan.ScanError):
            scan.orb_window(DT(2026, 10, 29, 10, 15), None, 7, until)

    def test_muhurat_session_from_config(self):
        cfg = {"special_sessions": {"2026-11-08": ["18:00", "19:00"]}}
        day, cut = scan.orb_window(DT(2026, 11, 8, 18, 40), None, 15, None, cfg=cfg)
        self.assertEqual(cut, DT(2026, 11, 8, 18, 40))
        s = bars_series("2026-11-08", [100, 100.4, 100.2, 100.3, 100.5, 100.6], start="18:00")
        hits, _, _ = scan.scan_orb({"X": s}, ["X"], day, cut, 15, cfg=cfg)
        self.assertEqual((hits[0]["at"], hits[0]["or_high"]), ("18:30", 100.5))   # range high 100.4 + 0.1; the 18:25 bar closed 100.6


class Dhan(unittest.TestCase):
    """The Dhan path. Dhan responses cannot be recorded without keys: the snapshot below follows the
    documented /marketfeed/ohlc shape, and the bars are the real Yahoo bars of Oct 29 standing in for
    /charts/intraday (same 5-minute OHLCV)."""

    @classmethod
    def setUpClass(cls):
        cls.bundle = json.loads(gzip.open(WALL).read())

    def bars(self, sym):
        return nse.parse_yahoo_chart(self.bundle[sym])[1]

    def snapshot(self, syms):
        out = {}
        for i, s in enumerate(syms):
            day = [b for b in self.bars(s) if b["t"] < DT(2026, 10, 29, 10, 15)]
            out[str(1000 + i)] = {"last_price": day[-1]["c"], "ohlc": {
                "open": day[0]["o"], "high": max(b["h"] for b in day), "low": min(b["l"] for b in day),
                "close": 0}}
        return out

    def test_dhan_orb_matches_yahoo(self):
        syms = ["RELIANCE", "SBIN", "TCS", "IDEA", "HDFCBANK", "BAJAJ-AUTO"]
        sid_of = dict((s, str(1000 + i)) for i, s in enumerate(syms))
        fetched = []

        def fetch(sym):
            fetched.append(sym)
            return self.bars(sym)
        day, cut = DAY, DT(2026, 10, 29, 10, 15)
        hits, counts, failed = scan.scan_orb_dhan(self.snapshot(syms), sid_of, syms + ["NODHAN"], day, cut, 15,
                                                  fetch, min_price=50)
        data, _ = scan.load_offline(WALL)
        want, _, _ = scan.scan_orb(data, syms, day, cut, 15, 50, 0)
        self.assertEqual(hits, want)
        self.assertEqual(counts["illiquid"], 1)               # IDEA under Rs 50
        self.assertIn("NODHAN", failed)
        self.assertNotIn("IDEA", fetched)
        for s in fetched:                                    # stage 1: the day's high above the open
            snap = self.snapshot(syms)[sid_of[s]]["ohlc"]
            self.assertGreater(snap["high"], snap["open"])

    def test_ohlc_request_shape_and_batches(self):
        sent = []

        def transport(url, headers=None, data=None, method=None):
            sent.append((url, method, data, headers))
            ids = data["NSE_EQ"]
            return json.dumps({"data": {"NSE_EQ": dict((str(i), {"last_price": 1.0, "ohlc": {}}) for i in ids)},
                               "status": "success"}).encode()
        dhan = nse.Dhan("1000000001", "token", transport=transport)
        got = dhan.ohlc(range(1, 1502))
        self.assertEqual(len(got), 1501)
        self.assertEqual([len(s[2]["NSE_EQ"]) for s in sent], [1000, 501])
        self.assertTrue(sent[0][0].endswith("/v2/marketfeed/ohlc"))
        self.assertEqual(sent[0][1], "POST")

    def test_source_choice(self):
        class A(object):
            source = None
        old = dict((k, os.environ.pop(k, None)) for k in ("DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN"))
        try:
            self.assertEqual(scan.data_source(A(), {}), "yahoo")
            with self.assertRaises(scan.ScanError):
                scan.data_source(A(), {"data_source": "dhan"})
            os.environ.update({"DHAN_CLIENT_ID": "1", "DHAN_ACCESS_TOKEN": "t"})
            self.assertEqual(scan.data_source(A(), {}), "dhan")
            self.assertEqual(scan.data_source(A(), {"data_source": "yahoo"}), "yahoo")
        finally:
            for k, v in old.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.wl = os.path.join(self.d, "trader", "watchlist.json")
        self.cfg = os.path.join(self.d, "config.json")
        with open(self.cfg, "w") as fh:
            json.dump({}, fh)

    def tearDown(self):
        shutil.rmtree(self.d)

    def read(self):
        with open(self.wl, encoding="utf-8") as fh:
            return json.load(fh)

    def test_orb_end_to_end(self):
        code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-10-29 10:15", "--out", self.wl,
                             "--config", self.cfg] + LISTS)
        self.assertEqual(code, 0)
        self.assertIn("19 of 33 charts closed above their opening-range high by 10:15 IST", text)
        self.assertIn("09:40  RELIANCE      Rs 1,174.30  range 1,156.20 to 1,170.50  +0.32% over  F&O", text)
        self.assertIn("Not financial advice", text)
        self.assertNotIn("$", text)
        doc = self.read()
        self.assertEqual(dict((k, doc[k]) for k in ("created_by", "created_at", "mode", "as_of", "universe",
                                                    "scanned", "source", "exchange", "opening_range")),
                         {"created_by": "india-scanner", "created_at": "2026-10-29T10:15:00+05:30", "mode": "orb",
                          "as_of": "2026-10-29 10:15 IST", "universe": 35, "scanned": 33, "source": "yahoo",
                          "exchange": "NSE", "opening_range": "09:15-09:30"})
        rel = [h for h in doc["hits"] if h["symbol"] == "RELIANCE"][0]
        self.assertEqual((rel["name"], rel["fno"]), ("Reliance Industries Limited", True))
        # read back as a watchlist with --at
        code, text, _ = run(["orb", "--offline", WALL, "--at", "10:15", "--watchlist", self.wl, "--json",
                             "--no-write", "--config", self.cfg] + LISTS)
        out = json.loads(text)
        self.assertEqual((out["universe"], len(out["hits"])), (19, 19))

    def test_daily_end_to_end(self):
        code, text, _ = run(["daily", "--offline", SPARK_A, "--now", "2026-10-29 09:00", "--out", self.wl,
                             "--config", self.cfg] + LISTS)
        self.assertEqual(code, 0)
        self.assertIn("Fetched 18 of 18 charts", text)
        self.assertIn("closes through 2026-10-28", text)
        doc = self.read()
        self.assertEqual(doc["as_of"], "2026-10-28 close")
        self.assertEqual(doc["filters"], {"min_price": 50.0, "min_turnover_cr": 10.0, "series": "EQ",
                                          "fno_only": False})
        lt = [h for h in doc["hits"] if h["symbol"] == "LT"][0]
        self.assertEqual(lt["setups"], ["breakout_20d", "reclaim_ma20"])
        self.assertEqual((lt["exchange"], lt["price"], lt["close_date"]), ("NSE", 3801.3, "2026-10-28"))

    def test_symbols_are_series_checked(self):
        eq = nse.parse_equity_list(nse.read_maybe_gz(EQ).decode("utf-8"))
        be = sorted(s for s, r in eq.items() if r["series"] == "BE")[0]
        code, text, _ = run(["daily", "--offline", SPARK_A, "--symbols", "reliance,TCS.NS,%s" % be,
                             "--now", "2026-10-29 16:00", "--json", "--no-write", "--config", self.cfg] + LISTS)
        out = json.loads(text)
        self.assertEqual(out["universe"], 2)
        self.assertIn("trade-to-trade", out["dropped"][be])

    def test_plain_messages(self):
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-10-31 10:15", "--out", self.wl,
                            "--config", self.cfg] + LISTS)
        self.assertEqual(code, 1)
        self.assertIn("No session on Sat 2026-10-31 (weekend)", err)
        self.assertFalse(os.path.exists(self.wl))
        code, _, err = run(["orb", "--offline", WALL, "--config", self.cfg] + LISTS)
        self.assertIn("--at HH:MM", err)
        with open(self.cfg, "w") as fh:
            json.dump({"no_entries_after": "10:00"}, fh)
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-10-29 10:15", "--config", self.cfg] + LISTS)
        self.assertIn("expired", err)
        self.assertNotIn("Traceback", err)
        code, _, err = run(["daily", "--offline", os.path.join(self.d, "nothing")] + LISTS)
        self.assertIn("no offline file", err)

    def test_fno_only(self):
        code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-10-29 10:15", "--fno-only", "--min-price",
                             "0", "--json", "--no-write", "--config", self.cfg] + LISTS)
        out = json.loads(text)
        self.assertTrue(all(h["fno"] for h in out["hits"]))


class Docs(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(os.path.dirname(SDIR), "..", "..", "india-scanner", "SKILL.md"),
                  encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: india-scanner\n"))
        self.assertIn("Not financial advice", text)
        self.assertGreater(len(text.split("---", 2)[2].splitlines()), 60)
        for bad in ("NYSE", "SEC ", "Alpaca", "New York"):
            self.assertNotIn(bad, text)


if __name__ == "__main__":
    unittest.main()
