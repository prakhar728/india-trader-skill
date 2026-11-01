import datetime
import json
import os
import unittest

from _load import COMMON

import nse

D = datetime.date
DT = datetime.datetime


class Clock(unittest.TestCase):
    def test_ist_is_utc_plus_530_all_year(self):
        self.assertEqual(nse.ist_from_utc(DT(2026, 1, 5, 3, 45)), DT(2026, 1, 5, 9, 15))
        self.assertEqual(nse.ist_from_utc(DT(2026, 7, 5, 3, 45)), DT(2026, 7, 5, 9, 15))
        self.assertEqual(nse.utc_from_ist(DT(2026, 10, 29, 15, 30)), DT(2026, 10, 29, 10, 0))

    def test_holidays_match_nse_holiday_master(self):
        with open(os.path.join(COMMON, "hol.json")) as fh:
            cm = json.load(fh)["CM"]
        for h in cm:
            d = datetime.datetime.strptime(h["tradingDate"], "%d-%b-%Y").date().isoformat()
            self.assertIn(d, nse.NSE_HOLIDAYS, h)
        self.assertEqual(len(cm), len(nse.NSE_HOLIDAYS))

    def test_trading_days(self):
        self.assertTrue(nse.is_trading_day(D(2026, 10, 29)))
        self.assertFalse(nse.is_trading_day(D(2026, 10, 20)))      # Dussehra
        self.assertFalse(nse.is_trading_day(D(2026, 11, 10)))
        self.assertFalse(nse.is_trading_day(D(2026, 10, 31)))      # Saturday
        self.assertFalse(nse.is_trading_day(D(2026, 11, 8)))       # Sunday, until Muhurat is set
        cfg = {"special_sessions": {"2026-11-08": ["18:00", "19:00"]}}
        self.assertEqual(nse.session(D(2026, 11, 8), cfg), (DT(2026, 11, 8, 18), DT(2026, 11, 8, 19)))
        self.assertFalse(nse.is_trading_day(D(2026, 10, 30), {"extra_holidays": {"2026-10-30": "test"}}))

    def test_market_status(self):
        self.assertEqual(nse.market_status(DT(2026, 10, 29, 9, 5))["state"], "pre-open")
        s = nse.market_status(DT(2026, 10, 29, 15, 0))
        self.assertEqual((s["state"], s["minutes_to_close"]), ("open", 30))
        s = nse.market_status(DT(2026, 10, 19, 20, 0))
        self.assertEqual((s["state"], s["next_open"]), ("closed", "2026-10-21T09:15:00+05:30"))
        self.assertIn("warning", nse.market_status(DT(2027, 1, 4, 10, 0)))

    def test_flat_by_never_after_dhan_square_off(self):
        self.assertEqual(nse.flat_by({}), datetime.time(15, 5))
        self.assertEqual(nse.flat_by({"flat_by": "15:25"}), datetime.time(15, 10))
        self.assertEqual(nse.flat_by({"flat_by": "14:30"}), datetime.time(14, 30))
        muhurat = {"special_sessions": {"2026-11-08": ["18:00", "19:00"]}}
        self.assertEqual(nse.flat_by(muhurat, D(2026, 11, 8)), datetime.time(18, 55))


class KnownSpecialSessions(unittest.TestCase):
    def test_budget_sunday_2026_02_01_is_a_trading_day_at_normal_hours(self):
        d = D(2026, 2, 1)                                          # Sunday, Union Budget special session
        self.assertEqual(d.weekday(), 6)
        self.assertIn("2026-02-01", nse.KNOWN_SPECIAL_SESSIONS)
        self.assertNotIn("2026-02-01", nse.NSE_HOLIDAYS)           # a session, not a holiday removal
        self.assertTrue(nse.is_trading_day(d))
        self.assertEqual(nse.session(d), (DT(2026, 2, 1, 9, 15), DT(2026, 2, 1, 15, 30)))
        self.assertIsNone(nse.special_session(d))                  # normal hours: backtests keep its bars
        self.assertEqual(nse.previous_trading_day(D(2026, 2, 2)), d)
        self.assertFalse(nse.is_trading_day(D(2026, 2, 8)))        # the next Sunday is shut
        self.assertFalse(nse.is_trading_day(d, {"extra_holidays": {"2026-02-01": "closed after all"}}))


class Holdout(unittest.TestCase):
    """india-common/holdout.py: research holdouts withheld outside the lab."""

    SPLITS = {"angel:x:5m": {"explore": ["2026-01-01", "2026-06-30"], "validate": ["2026-07-01", "2026-08-31"],
                             "holdout": ["2026-09-01", "2026-10-29"], "symbols": ["RELIANCE", "TCS"]},
              "bhav:nifty-weekly": {"holdout": ["2026-10-01", "2026-10-28"], "symbols": ["NIFTY"]}}

    class Row(object):
        def __init__(self, date):
            self.date = date

    def rows(self, *dates):
        return [self.Row(d) for d in dates]

    def test_windows_by_symbol(self):
        import holdout
        w = holdout.windows(["RELIANCE", "NIFTY", "INFY"], splits=self.SPLITS)
        self.assertEqual(w, {"RELIANCE": [("2026-09-01", "2026-10-29", "angel:x:5m")],
                             "NIFTY": [("2026-10-01", "2026-10-28", "bhav:nifty-weekly")]})
        self.assertEqual(holdout.inside("2026-09-01", w["RELIANCE"]), "angel:x:5m")
        self.assertIsNone(holdout.inside("2026-08-31", w["RELIANCE"]))

    def test_withhold_and_allow(self):
        import holdout
        data = {"RELIANCE": self.rows("2026-08-31", "2026-09-01", "2026-10-29"),
                "INFY": self.rows("2026-09-01", "2026-10-29")}
        kept, info = holdout.withhold(data, splits=self.SPLITS)
        self.assertEqual([r.date for r in kept["RELIANCE"]], ["2026-08-31"])
        self.assertEqual(len(kept["INFY"]), 2)                     # not in any split: untouched
        self.assertEqual((info["withheld_sessions"], info["withheld_stock_days"], info["holdout_included"]),
                         (2, 2, False))
        self.assertEqual(info["by_dataset"]["angel:x:5m"]["from"], "2026-09-01")
        self.assertIn("withheld 2 session(s)", holdout.note(info))
        self.assertIn("--allow-holdout", holdout.note(info))
        kept, info = holdout.withhold(data, splits=self.SPLITS, allow=True)
        self.assertEqual(len(kept["RELIANCE"]), 3)
        self.assertTrue(info["holdout_included"])
        self.assertEqual(info["withheld_sessions"], 0)
        self.assertIn("INCLUDED", holdout.note(info))
        kept, info = holdout.withhold({"INFY": self.rows("2026-10-01")}, splits=self.SPLITS)
        self.assertIsNone(holdout.note(info))                      # nothing withheld: no line

    def test_reads_trader_research_splits(self):
        import holdout
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp()
        try:
            nse.write_json(os.path.join(tmp, "research", "splits.json"), self.SPLITS)
            self.assertEqual(sorted(holdout.windows(None, base=tmp)), ["NIFTY", "RELIANCE", "TCS"])
            self.assertEqual(holdout.windows(["X"], base=os.path.join(tmp, "nothing")), {})
        finally:
            shutil.rmtree(tmp)


class Ticks(unittest.TestCase):
    def test_bands(self):
        for p, t in ((99.5, 0.01), (249.99, 0.01), (250, 0.05), (1399, 0.10), (9000, 0.50),
                     (15000, 1.0), (140000, 5.0)):
            self.assertEqual(nse.band_tick(p), t, p)

    def test_rounding(self):
        self.assertEqual(nse.round_down(1412.37, 0.10), 1412.3)
        self.assertEqual(nse.round_up(1412.31, 0.10), 1412.4)
        self.assertEqual(nse.round_down(1412.30, 0.10), 1412.3)
        self.assertEqual(nse.round_down(512.12, 0.05), 512.1)
        self.assertEqual(nse.round_down(1156.199951, 0.10), 1156.2)
        self.assertEqual(nse.round_up(1156.200049, 0.10), 1156.2)
        self.assertTrue(nse.on_tick(1412.3, 0.1))
        self.assertFalse(nse.on_tick(1412.33, 0.1))

    def test_scrip_master_tick_in_paise(self):
        with open(os.path.join(COMMON, "dhan-scrip-sample.csv")) as fh:
            scrip = nse.parse_dhan_scrip(fh.read())
        self.assertEqual(scrip["RELIANCE"]["security_id"], "2885")
        self.assertEqual(scrip["RELIANCE"]["tick"], 0.10)
        self.assertEqual(scrip["MRF"]["tick"], 5.0)
        self.assertEqual(scrip["IDEA"]["tick"], 0.01)
        self.assertEqual(nse.tick_for(1400, "RELIANCE", scrip), 0.10)
        self.assertEqual(nse.security_id("m&m", scrip), "2031")


class Charges(unittest.TestCase):
    def test_round_trip(self):
        # 10 RELIANCE bought at 1,400 and sold at 1,420: brokerage 4.20 + 4.26
        c = nse.charges(14000.0, 14200.0)
        self.assertEqual(c["brokerage"], 8.46)
        self.assertEqual(c["stt"], 4.0)          # 0.025 % of 14,200 = 3.55 -> 4
        self.assertEqual(c["stamp"], 0.0)        # 0.003 % of 14,000 = 0.42 -> 0
        self.assertAlmostEqual(c["exchange"], 0.87, places=2)
        self.assertEqual(c["gst"], 1.68)
        self.assertEqual(c["total"], 15.04)

    def test_brokerage_capped_at_20(self):
        c = nse.charges(500000.0, 500000.0)
        self.assertEqual(c["brokerage"], 40.0)
        self.assertEqual(c["stt"], 125.0)
        self.assertEqual(c["stamp"], 15.0)

    def test_inr_grouping(self):
        self.assertEqual(nse.inr(1234567.5), "Rs 12,34,567.50")
        self.assertEqual(nse.inr(999), "Rs 999.00")
        self.assertEqual(nse.inr(-50000, 0), "-Rs 50,000")


class Symbols(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(nse.clean_symbol("reliance.ns"), "RELIANCE")
        self.assertEqual(nse.clean_symbol("NSE:M&M"), "M&M")
        self.assertEqual(nse.clean_symbol("BAJAJ-AUTO"), "BAJAJ-AUTO")
        self.assertEqual(nse.yahoo_symbol("M&M"), "M&M.NS")
        with self.assertRaises(nse.NSEError):
            nse.clean_symbol("DROP TABLE")

    def test_equity_list_and_series(self):
        with open(os.path.join(COMMON, "EQUITY_L.csv")) as fh:
            eq = nse.parse_equity_list(fh.read())
        self.assertGreater(len(eq), 2000)
        self.assertEqual(eq["RELIANCE"]["series"], "EQ")
        self.assertTrue(nse.intraday_allowed("EQ")[0])
        self.assertFalse(nse.intraday_allowed("BE")[0])

    def test_nifty500(self):
        with open(os.path.join(COMMON, "n500.csv")) as fh:
            rows = nse.parse_index_list(fh.read())
        self.assertGreaterEqual(len(rows), 500)


class Bars(unittest.TestCase):
    def test_yahoo_nse_bars_are_ist_75_a_day(self):
        meta, bars = nse.parse_yahoo_chart(nse.read_maybe_gz(os.path.join(COMMON, "yahoo-RELIANCE.NS-60d5m.json.gz")))
        self.assertEqual(meta["gmtoffset"], 19800)
        days = nse.by_day(bars)
        self.assertEqual(len(days), 60)
        # 75 five-minute bars from 09:15 to 15:25; Yahoo skips a bar or three a day
        self.assertTrue(all(72 <= len(b) <= 75 for b in days.values()))
        first = days[min(days)]
        self.assertEqual(first[0]["t"].time(), datetime.time(9, 15))
        # RELIANCE is an F&O stock: since NSE's closing auction (Aug 3 2026) continuous
        # trading ends 15:15, so Yahoo never has a 15:20 bar and rarely a 15:25 one
        self.assertFalse(any(b["t"].time() == datetime.time(15, 20) for b in bars))

    def test_resample_and_grid(self):
        _, bars = nse.parse_yahoo_chart(nse.read_maybe_gz(os.path.join(COMMON, "yahoo-RELIANCE.NS-1d1m.json")))
        five = nse.resample(nse.on_grid(bars, 1), 5)
        self.assertEqual(five[0]["t"].time(), datetime.time(9, 15))
        self.assertEqual(five[0]["h"], max(b["h"] for b in bars[:5]))

    def test_dhan_bars_utc_and_ist_epochs(self):
        base = nse.epoch_from_ist(DT(2026, 10, 29, 9, 15))
        n = 75
        doc = {"timestamp": [base + 300 * i for i in range(n)], "open": [1] * n, "high": [1] * n,
               "low": [1] * n, "close": [1] * n, "volume": [5] * n}
        self.assertEqual(nse.dhan_bars(doc)[0]["t"], DT(2026, 10, 29, 9, 15))
        shifted = dict(doc, timestamp=[t + 19800 for t in doc["timestamp"]])
        self.assertEqual(nse.dhan_bars(shifted)[0]["t"], DT(2026, 10, 29, 9, 15))


class DhanClient(unittest.TestCase):
    def test_headers_and_quote_batching(self):
        calls = []

        def fake(url, headers=None, data=None, method=None, **kw):
            calls.append((url, headers, data, method))
            return json.dumps({"data": {"NSE_EQ": {"2885": {"last_price": 1412.3}}}, "status": "success"}).encode()

        d = nse.Dhan("1100000000", "tok", transport=fake)
        out = d.ltp(["2885"])
        self.assertEqual(out["2885"]["last_price"], 1412.3)
        url, h, body, method = calls[0]
        self.assertEqual(url, "https://api.dhan.co/v2/marketfeed/ltp")
        self.assertEqual((h["access-token"], h["client-id"], method), ("tok", "1100000000", "POST"))
        self.assertEqual(body, {"NSE_EQ": [2885]})

    def test_error_code_raises(self):
        d = nse.Dhan("1", "t", transport=lambda *a, **k: b'{"errorCode":"DH-902","errorMessage":"no data plan"}')
        with self.assertRaises(nse.NSEError):
            d.ltp([1])


class DotEnv(unittest.TestCase):
    def test_parse_and_shell_wins(self):
        import tempfile
        text = ('# keys\nexport DHAN_CLIENT_ID="110"\nDHAN_ACCESS_TOKEN=abc # note\n'
                "ANGEL_PIN='1234'\nEMPTY=\n")
        self.assertEqual(nse.parse_env(text), {"DHAN_CLIENT_ID": "110", "DHAN_ACCESS_TOKEN": "abc",
                                                "ANGEL_PIN": "1234", "EMPTY": ""})
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write(text)
        env = {"DHAN_CLIENT_ID": "from-shell"}
        nse.load_env([fh.name], env)
        os.unlink(fh.name)
        self.assertEqual(env, {"DHAN_CLIENT_ID": "from-shell", "DHAN_ACCESS_TOKEN": "abc", "ANGEL_PIN": "1234"})


if __name__ == "__main__":
    unittest.main()
