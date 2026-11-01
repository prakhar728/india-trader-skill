"""india-entry tests. Offline, on real Yahoo fixtures:

common/yahoo-RELIANCE.NS-60d5m.json.gz   60 sessions of RELIANCE 5-minute bars to 2026-10-29
                                         (fetched 15:29 IST, after the close).
common/yahoo-RELIANCE.NS-1d1m.json       RELIANCE 1-minute bars, 2026-10-29.
india-entry/yahoo-SUZLON.NS-1d5m.json    SUZLON 5-minute bars, 2026-10-29: it never closed above its
                                         opening range (a real no-breakout day). Fetched 2026-10-29
                                         with curl, UA "Mozilla/5.0 (compatible; india-trader-skill/1.0)".

The worked example: RELIANCE on Thu 2026-10-29. Opening range 09:15-09:30 high 1,170.50, low
1,156.20; the 09:35 bar (bar 4) closes 1,174.30 above it, so the trigger is 09:40 IST.
Dhan responses cannot be recorded (no keys): the Dhan test builds one in the documented
/v2/charts/intraday shape (parallel open/high/low/close/volume/timestamp arrays) from the
real Yahoo bars.
"""

import copy
import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, COMMON, ROOT

import nse

entry = load("india-entry", "entry")

R60 = os.path.join(COMMON, "yahoo-RELIANCE.NS-60d5m.json.gz")
R1M = os.path.join(COMMON, "yahoo-RELIANCE.NS-1d1m.json")
SUZLON = os.path.join(FIX, "india-entry", "yahoo-SUZLON.NS-1d5m.json")
SCRIP = os.path.join(COMMON, "dhan-scrip-sample.csv")
DT = datetime.datetime
DAY = datetime.date(2026, 10, 29)


def text(path=R60):
    return nse.read_maybe_gz(path).decode("utf-8")


def chart(path=R60, sym="RELIANCE"):
    return entry.parse_chart(text(path), sym)


def at(when, ch=None, **kw):
    return entry.evaluate(ch or chart(), entry.parse_now(when), **kw)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = entry.main(argv)
    return code, out.getvalue(), err.getvalue()


def shifted(ch, start, n=None):
    """The 2026-10-29 session's bars moved to start a session at `start` (same prices)."""
    out = copy.deepcopy(ch)
    day = [b for b in out["bars"] if b["t"].date() == DAY]
    delta = start - DT(2026, 10, 29, 9, 15)
    out["bars"] = [dict(b, t=b["t"] + delta) for b in day[:n]]
    out["data_time"] = None
    return out


class Example(unittest.TestCase):
    def test_triggers_at_0940_on_the_0935_bar(self):
        r = at("2026-10-29 09:40")
        self.assertEqual(r["status"], "triggered")
        self.assertEqual(r["opening_range"]["high"], 1170.5)
        self.assertEqual(r["opening_range"]["low"], 1156.2)
        self.assertEqual(r["opening_range"]["bars"], 3)
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_start"], t["bar_index"], t["price"]), ("09:40", "09:35", 4, 1174.3))
        self.assertEqual(t["pct_over"], 0.32)
        self.assertEqual(r["level"], 1170.5)
        self.assertEqual((r["no_entries_before"], r["no_entries_after"], r["flat_by"]), ("09:30", "14:30", "15:05"))
        text_ = "\n".join(entry.describe(r))
        self.assertIn("TRIGGERED 09:40 IST at Rs 1,174.30: the 09:35 bar (bar 4)", text_)
        self.assertIn("This is the moment to get in", text_)
        self.assertIn("Nothing was placed", text_)

    def test_at_0938_the_0935_bar_is_still_forming(self):
        r = at("2026-10-29 09:38")
        self.assertEqual(r["status"], "waiting")
        self.assertIsNone(r["trigger"])
        self.assertEqual(r["last"]["bar_start"], "09:30")
        self.assertEqual(r["last"]["close"], 1169.5)
        self.assertEqual(r["forming"]["bar_start"], "09:35")
        self.assertIn("Rs 1.00 under the range high Rs 1,170.50", r["reason"])
        self.assertEqual(at("2026-10-29 09:39:59")["status"], "waiting")

    def test_before_the_open_and_while_the_range_forms(self):
        r = at("2026-10-29 09:05")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "pre-open"))
        self.assertIn("opens at 09:15", r["reason"])
        r = at("2026-10-29 09:27")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "forming"))
        self.assertEqual(r["opening_range"]["bars"], 2)
        self.assertFalse(r["opening_range"]["complete"])
        r = at("2026-10-29 09:31")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "open"))
        self.assertEqual(r["opening_range"]["bars"], 3)

    def test_after_the_close_it_stays_triggered_and_says_it_is_late(self):
        r = at("2026-10-29 16:00")
        self.assertEqual((r["status"], r["phase"]), ("triggered", "after-close"))
        text_ = "\n".join(entry.describe(r))
        self.assertIn("minutes ago", text_)
        self.assertNotIn("This is the moment", text_)

    def test_offline_without_now_reads_as_of_the_file(self):
        c = chart()
        self.assertEqual(c["data_time"], DT(2026, 10, 29, 15, 29, 3))
        r = entry.evaluate(c, c["data_time"])
        self.assertEqual(r["_bars"][-1]["t"].time(), datetime.time(15, 15))   # F&O: no 15:20 bar
        self.assertEqual(r["bars_closed"], 73)                                # Yahoo dropped two bars


class Bars(unittest.TestCase):
    def test_a_live_tick_is_never_a_bar(self):
        c = chart()
        c["bars"].append({"t": DT(2026, 10, 29, 9, 37, 30), "o": 1180.0, "h": 1180.0, "l": 1180.0, "c": 1180.0, "v": 0})
        r = entry.evaluate(c, DT(2026, 10, 29, 9, 39))
        self.assertEqual(r["status"], "waiting")
        self.assertTrue(all(b["t"].second == 0 for b in r["_bars"]))

    def test_waits_for_the_feed_to_catch_up(self):
        c = chart()
        c["bars"] = [b for b in c["bars"] if b["t"] <= DT(2026, 10, 29, 9, 35)]
        c["data_time"] = DT(2026, 10, 29, 9, 39, 40)              # last trade before the 09:35 bar closed
        r = entry.evaluate(c, DT(2026, 10, 29, 9, 40, 10))
        self.assertEqual(r["status"], "waiting")
        self.assertIn("last trade is 09:39:40", r["lag_note"])
        self.assertEqual(entry.evaluate(c, DT(2026, 10, 29, 9, 41, 0))["status"], "triggered")

    def test_volume_check_skips_the_zero_volume_0915_bar(self):
        r = at("2026-10-29 12:00", min_volume_x=1)
        self.assertEqual(r["opening_range"]["avg_volume"], 368006)            # (285741 + 450272) / 2
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_index"], t["price"], t["volume_x"]), ("10:00", 8, 1173.8, 2.26))
        r = at("2026-10-29 09:41", min_volume_x=1)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("0.74x the range's volume, under your 1.00x", r["reason"])

    def test_level_setup(self):
        r = at("2026-10-29 12:00", setup="level", level=1178.5)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_start"], r["trigger"]["price"]), ("09:55", "09:50", 1179.0))
        r = at("2026-10-29 12:00", setup="level", level=1179)          # a close AT the level is not above it
        self.assertEqual(r["status"], "waiting")
        r = at("2026-10-29 12:00", setup="level", level=1100)
        self.assertEqual(r["trigger"]["time"], "09:30")                       # the 09:25 bar, at no_entries_before
        with self.assertRaises(entry.EntryError):
            at("2026-10-29 10:30", setup="level")

    def test_one_minute_bars(self):
        r = at("2026-10-29 12:00", chart(R1M), interval="1m")
        self.assertEqual((r["opening_range"]["bars"], r["opening_range"]["high"]), (15, 1170.5))
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_start"], t["bar_index"], t["price"]), ("09:34", "09:33", 18, 1171.3))

    def test_no_breakout_waits_then_expires_at_1430(self):
        c = chart(SUZLON, "SUZLON")
        r = entry.evaluate(c, DT(2026, 10, 29, 12, 0))
        self.assertEqual(r["status"], "waiting")
        self.assertEqual((r["opening_range"]["high"], r["opening_range"]["low"]), (37.87, 37.34))
        self.assertEqual(entry.evaluate(c, DT(2026, 10, 29, 14, 29))["status"], "waiting")
        r = entry.evaluate(c, DT(2026, 10, 29, 14, 30))
        self.assertEqual(r["status"], "expired")
        self.assertIn("by 14:30 IST", r["reason"])
        self.assertEqual(entry.evaluate(c, DT(2026, 10, 29, 15, 45))["phase"], "after-close")

    def test_config_window(self):
        r = at("2026-10-29 09:40", cfg={"no_entries_before": "09:30", "no_entries_after": "09:35"})
        self.assertEqual(r["status"], "expired")
        r = at("2026-10-29 10:00", cfg={"no_entries_before": "09:45", "no_entries_after": "14:30"})
        self.assertEqual((r["trigger"]["time"], r["trigger"]["price"]), ("09:45", 1174.5))
        r = at("2026-10-29 09:40", cfg={"no_entries_after": "15:30", "flat_by": "09:35"})
        self.assertEqual(r["status"], "expired")                              # never at or after flat-by

    def test_bad_inputs(self):
        with self.assertRaises(entry.EntryError):
            at("2026-10-29 10:15", or_minutes=7)
        with self.assertRaises(entry.EntryError):
            at("2026-10-29 10:15", interval="15m")
        with self.assertRaises(entry.EntryError):
            entry.parse_now("tomorrow")
        with self.assertRaises(entry.EntryError):
            entry.parse_hhmm("25:00", "x")
        self.assertEqual(entry.parse_now("2026-10-29 09:40 IST"), DT(2026, 10, 29, 9, 40))
        self.assertEqual(entry.parse_now("2026-10-29T09:40:00"), DT(2026, 10, 29, 9, 40))


class Calendar(unittest.TestCase):
    def test_weekend_holiday_and_next_open(self):
        r = at("2026-10-31 10:30")
        self.assertEqual((r["status"], r["phase"]), ("no-data", "closed"))
        self.assertIn("weekend", r["reason"])
        self.assertEqual(r["next_open"], "2026-11-02 09:15")
        r = at("2026-11-10 10:30")
        self.assertIn("Diwali-Balipratipada", r["reason"])
        r = at("2026-10-20 10:30")
        self.assertIn("Dussehra", r["reason"])
        self.assertEqual(r["next_open"], "2026-10-21 09:15")
        r = at("2026-10-30 10:30", cfg={"extra_holidays": {"2026-10-30": "test closure"}})
        self.assertIn("test closure", r["reason"])

    def test_muhurat_needs_the_hour_then_runs_its_own_window(self):
        r = at("2026-11-08 18:20")
        self.assertEqual(r["status"], "no-data")
        self.assertIn("special_sessions", r["reason"])
        cfg = {"special_sessions": {"2026-11-08": ["18:00", "19:00"]}}
        ch = shifted(chart(), DT(2026, 11, 8, 18, 0), n=12)
        r = entry.evaluate(ch, DT(2026, 11, 8, 18, 30), cfg=cfg)
        self.assertEqual((r["opens"], r["closes"], r["no_entries_before"], r["no_entries_after"], r["flat_by"]),
                         ("18:00", "19:00", "18:15", "18:25", "18:55"))
        self.assertEqual((r["trigger"]["time"], r["trigger"]["price"]), ("18:25", 1174.3))
        self.assertIn("Special session today", "\n".join(entry.describe(r)))
        self.assertEqual(entry.evaluate(ch, DT(2026, 11, 8, 18, 22), cfg=cfg)["status"], "waiting")

    def test_unknown_year_warns(self):
        ch = shifted(chart(), DT(2027, 1, 4, 9, 15))
        r = entry.evaluate(ch, DT(2027, 1, 4, 9, 40))
        self.assertEqual(r["status"], "triggered")
        self.assertFalse(r["calendar_known"])
        self.assertIn("holiday list is not in this pack", "\n".join(entry.describe(r)))

    def test_empty_and_broken_data(self):
        r = at("2026-10-30 10:15")                               # the file holds no 2026-10-30 bars
        self.assertEqual(r["status"], "no-data")
        bad = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                              "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(entry.EntryError) as cm:
            entry.parse_chart(bad, "ZZZZ")
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(entry.EntryError):
            entry.parse_chart("<html>", "RELIANCE")


class Sources(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._http, self._sleep = entry._http, entry._sleep
        entry._sleep = lambda s: None
        self.body = text()

    def tearDown(self):
        entry._http, entry._sleep = self._http, self._sleep

    def test_yahoo_url_and_host_fallback(self):
        seq = [nse.NSEError("HTTP 503 from query1"), self.body]

        def fake(url):
            self.calls.append(url)
            x = seq.pop(0)
            if isinstance(x, Exception):
                raise x
            return x
        entry._http = fake
        ch = entry.get_chart("reliance", "5m", DAY, today=DAY, cfg={"data_source": "yahoo"})
        self.assertEqual(ch["symbol"], "RELIANCE")
        self.assertEqual(self.calls, ["https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m",
                                      "https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m"])

    def test_ranges_for_past_sessions(self):
        self.assertEqual(entry.chart_range(DAY, DAY, "5m"), "1d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 10, 26), DAY, "5m"), "5d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 10, 5), DAY, "5m"), "1mo")
        self.assertEqual(entry.chart_range(datetime.date(2026, 9, 5), DAY, "5m"), "60d")
        with self.assertRaises(entry.EntryError):
            entry.chart_range(datetime.date(2026, 10, 1), DAY, "1m")

    def test_data_source_picks_dhan_only_with_keys(self):
        self.assertEqual(entry.pick_source({}, env={}), "yahoo")
        self.assertEqual(entry.pick_source({}, env={"DHAN_CLIENT_ID": "1", "DHAN_ACCESS_TOKEN": "t"}), "dhan")
        self.assertEqual(entry.pick_source({"data_source": "yahoo"}, env={"DHAN_CLIENT_ID": "1",
                                                                         "DHAN_ACCESS_TOKEN": "t"}), "yahoo")
        with self.assertRaises(entry.EntryError):
            entry.pick_source({"data_source": "nse"}, env={})

    def test_dhan_intraday_gives_the_same_trigger(self):
        day = [b for b in chart()["bars"] if b["t"].date() == DAY]
        # built from the real bars in Dhan's documented shape (UTC epoch seconds)
        doc = {"open": [b["o"] for b in day], "high": [b["h"] for b in day], "low": [b["l"] for b in day],
               "close": [b["c"] for b in day], "volume": [b["v"] for b in day],
               "timestamp": [nse.epoch_from_ist(b["t"]) for b in day]}
        sent = []

        def fake(url, headers=None, data=None, method=None, **kw):
            sent.append((url, data))
            return json.dumps(doc).encode()
        with open(SCRIP) as fh:
            scrip = nse.parse_dhan_scrip(fh.read())
        ch = entry.dhan_chart("RELIANCE", "5m", DAY, {}, dhan=nse.Dhan("1100000000", "tok", transport=fake),
                              scrip=scrip)
        self.assertEqual(sent[0][0], "https://api.dhan.co/v2/charts/intraday")
        self.assertEqual((sent[0][1]["securityId"], sent[0][1]["interval"], sent[0][1]["fromDate"]),
                         ("2885", "5", "2026-10-29 09:15:00"))
        r = entry.evaluate(ch, DT(2026, 10, 29, 9, 40))
        self.assertEqual((r["source"], r["trigger"]["time"], r["trigger"]["price"]), ("dhan", "09:40", 1174.3))


class Watch(unittest.TestCase):
    OPTS = {"setup": "orb", "or_minutes": 15, "interval": "5m", "level": None, "min_volume_x": None, "cfg": None}

    def provider(self, calls):
        base = chart()

        def provide(now):
            calls.append(now)
            bars = []
            for b in base["bars"]:
                if b["t"] > now:
                    continue
                b = dict(b)
                if b["t"] + datetime.timedelta(minutes=5) > now:
                    b["c"], b["h"] = 1171.0, max(b["h"], 1171.0)     # in progress: a spike above the high
                bars.append(b)
            return dict(base, bars=bars, data_time=now)
        return provide

    def test_fires_on_the_first_closed_bar(self):
        calls, lines, fired = [], [], []
        clock = entry.SimClock(DT(2026, 10, 29, 9, 31))
        code, res = entry.watch_loop(self.provider(calls), clock.now, clock.sleep, lines.append, dict(self.OPTS),
                                     every=30, on_trigger=lambda r, now: fired.append(r))
        self.assertEqual(code, 0)
        self.assertEqual(fired[0]["trigger"]["time"], "09:40")
        self.assertEqual([c.strftime("%H:%M:%S") for c in calls], ["09:31:00", "09:35:02", "09:40:02"])
        text_ = "\n".join(lines)
        self.assertIn("09:30 bar  closed Rs 1,169.50  Rs 1.00 under Rs 1,170.50", text_)
        self.assertIn("09:35 bar  closed Rs 1,174.30  TRIGGERED", text_)
        self.assertEqual(text_.count("TRIGGERED"), 1)

    def test_from_before_the_open_prints_the_range(self):
        calls, lines = [], []
        clock = entry.SimClock(DT(2026, 10, 29, 9, 0))
        code, res = entry.watch_loop(self.provider(calls), clock.now, clock.sleep, lines.append, dict(self.OPTS))
        self.assertEqual(code, 0)
        self.assertIn("not open yet", lines[0])
        self.assertIn("09:25 bar  closed Rs 1,169.00  opening range set: high Rs 1,170.50, low Rs 1,156.20",
                      "\n".join(lines))

    def test_expires_closed_days_and_errors(self):
        c = chart(SUZLON, "SUZLON")
        clock = entry.SimClock(DT(2026, 10, 29, 14, 0))
        lines = []
        code, res = entry.watch_loop(lambda now: c, clock.now, clock.sleep, lines.append, dict(self.OPTS), every=300)
        self.assertEqual((code, res["status"]), (1, "expired"))
        self.assertEqual(clock.now().strftime("%H:%M"), "14:30")
        clock = entry.SimClock(DT(2026, 10, 31, 10, 0))
        code, _ = entry.watch_loop(lambda now: c, clock.now, clock.sleep, lines.append, dict(self.OPTS))
        self.assertIn("weekend", lines[-1])

        def broken(now):
            raise entry.EntryError("Yahoo did not answer")
        clock = entry.SimClock(DT(2026, 10, 29, 10, 0))
        code, _ = entry.watch_loop(broken, clock.now, clock.sleep, lines.append, dict(self.OPTS))
        self.assertEqual(code, 1)
        self.assertIn("Stopping after 5 failed reads", lines[-1])


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.env = os.environ.get("TRADER_DIR")
        os.environ["TRADER_DIR"] = os.path.join(self.d, "trader")

    def tearDown(self):
        if self.env is None:
            os.environ.pop("TRADER_DIR", None)
        else:
            os.environ["TRADER_DIR"] = self.env
        shutil.rmtree(self.d)

    def tpath(self, name):
        return os.path.join(self.d, "trader", name)

    def test_check_write_is_the_contract_shape(self):
        code, out, _ = run(["check", "RELIANCE", "--offline", R60, "--now", "2026-10-29 09:40", "--write"])
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED 09:40 IST at Rs 1,174.30", out)
        self.assertIn("/india-exit", out)
        with open(self.tpath("entry.json")) as fh:
            e = json.load(fh)
        want = {"created_by": "india-entry", "symbol": "RELIANCE", "exchange": "NSE", "setup": "orb", "side": "long",
                "date": "2026-10-29", "status": "triggered", "time": "09:40", "bar_start": "09:35", "bar_index": 4,
                "price": 1174.3, "level": 1170.5, "interval": "5m",
                "opening_range": {"high": 1170.5, "low": 1156.2, "minutes": 15},
                "source": "offline", "checked_at": "2026-10-29T09:40:00+05:30"}
        for k, v in want.items():
            self.assertEqual(e[k], v, k)

    def test_check_waiting_json_and_exit_codes(self):
        code, out, _ = run(["check", "RELIANCE", "--offline", R60, "--now", "2026-10-29 09:38", "--json"])
        self.assertEqual(code, 1)
        r = json.loads(out)
        self.assertEqual(r["status"], "waiting")
        self.assertNotIn("_bars", r)
        code, out, _ = run(["check", "RELIANCE", "--offline", R60, "--now", "2026-10-29 09:38",
                            "--write", os.path.join(self.d, "e.json")])
        with open(os.path.join(self.d, "e.json")) as fh:
            self.assertEqual(json.load(fh)["status"], "waiting")
        self.assertIn("still forming", out)

    def test_offline_folder_and_mismatches(self):
        os.mkdir(os.path.join(self.d, "charts"))
        with open(R1M, "rb") as src, gzip.open(os.path.join(self.d, "charts", "RELIANCE.json.gz"), "wb") as dst:
            dst.write(src.read())
        code, out, _ = run(["check", "reliance.ns", "--offline", os.path.join(self.d, "charts"), "--interval", "1m",
                            "--now", "2026-10-29 09:40"])
        self.assertEqual(code, 0)
        code, _, err = run(["check", "HDFCBANK", "--offline", R60])
        self.assertIn("holds RELIANCE, not HDFCBANK", err)
        code, _, err = run(["check", "RELIANCE", "--offline", R1M])
        self.assertIn("1m bars, not 5m", err)

    def test_longs_only_and_flag_errors(self):
        code, _, err = run(["check", "RELIANCE", "--offline", R60, "--side", "short"])
        self.assertEqual(code, 1)
        self.assertIn("longs only", err)
        code, _, err = run(["check", "RELIANCE", "--offline", R60, "--level", "300"])
        self.assertIn("--level is for --setup level", err)
        code, _, err = run(["check", "RELIANCE", "--offline", R60, "--setup", "level"])
        self.assertIn("needs --level", err)
        self.assertNotIn("Traceback", err)

    def test_config_is_read(self):
        os.makedirs(os.path.join(self.d, "trader"))
        with open(self.tpath("config.json"), "w") as fh:
            json.dump({"no_entries_after": "09:35"}, fh)
        code, out, _ = run(["check", "RELIANCE", "--offline", R60, "--now", "2026-10-29 09:40"])
        self.assertEqual(code, 1)
        self.assertIn("EXPIRED", out)
        with open(self.tpath("config.json"), "w") as fh:
            fh.write("{nope")
        code, _, err = run(["check", "RELIANCE", "--offline", R60])
        self.assertIn("not valid JSON", err)

    def test_watch_replays_the_file_and_writes_the_entry(self):
        code, out, _ = run(["watch", "RELIANCE", "--offline", R60, "--now", "2026-10-29 09:25", "--every", "30"])
        self.assertEqual(code, 0)
        self.assertIn("09:35 bar  closed Rs 1,174.30  TRIGGERED", out)
        with open(self.tpath("entry.json")) as fh:
            self.assertEqual(json.load(fh)["bar_index"], 4)
        code, out, _ = run(["watch", "SUZLON", "--offline", SUZLON, "--now", "2026-10-29 13:00", "--until", "13:30",
                            "--out", os.path.join(self.d, "x.json")])
        self.assertEqual(code, 1)
        self.assertIn("--until 13:30", out)
        self.assertFalse(os.path.exists(os.path.join(self.d, "x.json")))

    def test_watchlist(self):
        charts = os.path.join(self.d, "charts")
        os.mkdir(charts)
        shutil.copy(R60, os.path.join(charts, "RELIANCE.json.gz"))
        shutil.copy(SUZLON, os.path.join(charts, "SUZLON.json"))
        os.makedirs(os.path.join(self.d, "trader"))
        with open(self.tpath("watchlist.json"), "w") as fh:
            json.dump({"created_by": "india-scanner", "hits": [
                {"symbol": "RELIANCE", "setups": ["orb"]}, {"symbol": "ZZZZ"}, {"symbol": "SUZLON"}]}, fh)
        code, out, _ = run(["watchlist", "--offline", charts, "--now", "2026-10-29 09:40"])
        self.assertEqual(code, 0)
        rows = out.splitlines()
        self.assertTrue(rows[1].startswith("RELIANCE    triggered  09:40  Rs 1,174.30"), rows[1])
        self.assertIn("ZZZZ        error: no offline file", rows[2])
        self.assertTrue(rows[3].startswith("SUZLON      waiting"), rows[3])
        self.assertIn("1 of 3 triggered", out)
        code, _, err = run(["watchlist", "--watchlist", os.path.join(self.d, "missing.json")])
        self.assertIn("run /india-scanner first", err)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "india-entry", "SKILL.md"), encoding="utf-8") as fh:
            text_ = fh.read()
        self.assertTrue(text_.startswith("---\nname: india-entry\n"))
        self.assertGreater(len(text_.splitlines()), 60)
        low = " ".join(text_.lower().split())
        for must in ("bar by bar", "can lag", "day trading loses most people money", "/india-exit", "longs only",
                     "in the background", "/loop", "nothing was placed", "not financial advice", '"yes"',
                     "09:40 ist", "rs 1,174.30", "bar 4", "muhurat", "15:10"):
            self.assertIn(must, low)
        for never in ("\u2014", "nyse", "new york", "$1", "$3", "alpaca", "sec ", " et ", "usd"):
            self.assertNotIn(never, low)
        with open(os.path.join(ROOT, "india-entry", "entry.py"), encoding="utf-8") as fh:
            src = fh.read().lower()
        for never in ("nyse", "new york", "alpaca", "usd"):
            self.assertNotIn(never, src)


if __name__ == "__main__":
    unittest.main()
