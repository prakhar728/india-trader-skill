"""india-common/prefetch.py: chunking, resume, atomic writes, rate-limit handling, offline with a fake client."""

import datetime
import os
import shutil
import tempfile
import unittest

from _load import load

pf = load("india-common", "prefetch")
D = datetime.date


class FakeClient(object):
    """Serves 5m bars for weekdays on/after `floor`; scripted errors first."""

    def __init__(self, floor=D(2025, 4, 1), errors=()):
        self.floor, self.errors, self.calls, self.logins = floor, list(errors), [], 0

    def login(self):
        self.logins += 1

    def post(self, route, body):
        assert route == "candles"           # read-only: no other route is ever used
        self.calls.append(body)
        if self.errors:
            raise Exception(self.errors.pop(0))
        a = datetime.datetime.strptime(body["fromdate"], "%Y-%m-%d %H:%M").date()
        b = datetime.datetime.strptime(body["todate"], "%Y-%m-%d %H:%M").date()
        rows, d = [], max(a, self.floor)
        while d <= b:
            if d.weekday() < 5:
                for k in range(3):
                    t = datetime.datetime.combine(d, datetime.time(9, 15 + 5 * k))
                    rows.append([t.isoformat() + "+05:30", 100, 101, 99, 100.5, 10])
            d += datetime.timedelta(days=1)
        return rows


class Sleeper(object):
    def __init__(self):
        self.t, self.sleeps = 1000.0, []

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def clock(self):
        return self.t


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.s = Sleeper()
        self.now = datetime.datetime(2026, 10, 29, 18, 0)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def req(self, client, gap=3.0):
        return pf.Requester(client, gap=gap, sleep=self.s.sleep, clock=self.s.clock, log=lambda *a: None)

    def test_windows_cover_range_without_overlap(self):
        w = pf.windows(D(2025, 4, 1), D(2026, 10, 29), 90)
        self.assertEqual(w[0][0], D(2025, 4, 1))
        self.assertEqual(w[-1][1], D(2026, 10, 29))
        for (a1, b1), (a2, b2) in zip(w, w[1:]):
            self.assertEqual(b1 + datetime.timedelta(days=1), a2)
        self.assertTrue(all((b - a).days + 1 <= 90 for a, b in w))

    def test_plan_resume(self):
        full = pf.plan(None, D(2025, 4, 1), D(2026, 10, 29), 90)
        self.assertEqual(full[0][1], D(2026, 10, 29))            # newest first
        self.assertEqual(pf.plan(["2025-04-01", "2026-10-29"], D(2025, 4, 1), D(2026, 10, 29), 90), [])
        p = pf.plan(["2025-09-14", "2026-10-20"], D(2025, 4, 1), D(2026, 10, 29), 90)
        self.assertEqual(p[0], (D(2026, 10, 21), D(2026, 10, 29)))   # forward first
        self.assertEqual(p[1][1], D(2025, 9, 13))                     # then backward

    def test_fetch_writes_backtest_format_and_resumes(self):
        c = FakeClient()
        r = self.req(c)
        st = {}
        n = pf.fetch_symbol(r, "NIFTY", "99926000", "5m", D(2025, 4, 1), D(2025, 9, 30), st, base=self.tmp,
                            now=self.now, learn_floor=True)
        self.assertEqual(n, 3)       # 183 days / 90 -> 3 chunks
        doc = pf.read_store(pf.store_path("NIFTY", "5m", self.tmp))
        self.assertEqual(doc["covered"], ["2025-04-01", "2025-09-30"])
        self.assertEqual(doc["source"], "angel")
        self.assertEqual(doc["token"], "99926000")
        self.assertEqual(doc["bars"][0]["t"], datetime.datetime(2025, 4, 1, 9, 15))
        self.assertFalse(os.path.exists(pf.store_path("NIFTY", "5m", self.tmp) + ".tmp"))
        again = pf.fetch_symbol(r, "NIFTY", "99926000", "5m", D(2025, 4, 1), D(2025, 9, 30), st, base=self.tmp,
                                now=self.now)
        self.assertEqual(again, 0)
        more = pf.fetch_symbol(r, "NIFTY", "99926000", "5m", D(2025, 4, 1), D(2025, 10, 10), st, base=self.tmp,
                               now=self.now)
        self.assertEqual(more, 1)    # only the forward gap

    def test_floor_stops_the_walk_back(self):
        c = FakeClient(floor=D(2026, 1, 1))
        st = {}
        pf.fetch_symbol(self.req(c), "NIFTY", "1", "5m", D(2025, 4, 1), D(2026, 3, 31), st, base=self.tmp,
                        now=self.now, learn_floor=True)
        self.assertEqual(st["floor"]["5m"], "2026-01-01")
        calls = len(c.calls)
        pf.fetch_symbol(self.req(c), "ABC", "2", "5m", D(2025, 4, 1), D(2026, 3, 31), st, base=self.tmp,
                        now=self.now)
        self.assertLessEqual(len(c.calls) - calls, 2)    # no chunk wholly before the floor
        self.assertEqual(pf.read_store(pf.store_path("ABC", "5m", self.tmp))["covered"][0], "2026-01-01")

    def test_rate_limit_sleeps_60_and_retries_then_gives_up(self):
        c = FakeClient(errors=["HTTP 403 exceeding access rate"] * 2)
        bars = self.req(c).candles("1", D(2026, 1, 5), D(2026, 1, 6), "5m")
        self.assertEqual(len(bars), 6)
        self.assertEqual(self.s.sleeps.count(60), 2)
        c = FakeClient(errors=["HTTP 403 exceeding access rate"] * 9)
        with self.assertRaises(pf.RateLimited):
            self.req(c).candles("1", D(2026, 1, 5), D(2026, 1, 6), "5m")
        self.assertEqual(len(c.calls), 5)      # first try + 4 retries

    def test_gap_between_calls_and_single_login(self):
        c = FakeClient()
        r = self.req(c)
        for _ in range(3):
            r.candles("1", D(2026, 1, 5), D(2026, 1, 5), "5m")
        self.assertEqual(c.logins, 1)
        self.assertTrue(all(s >= 2.99 for s in self.s.sleeps if s < 60))

    def test_session_error_relogs_in_but_not_more_than_every_5_minutes(self):
        c = FakeClient(errors=["AG8001 Invalid Token"])
        r = self.req(c)
        r.candles("1", D(2026, 1, 5), D(2026, 1, 5), "5m")
        self.assertEqual(c.logins, 2)
        self.assertTrue(any(s > 250 for s in self.s.sleeps))      # waited out the 5-minute guard

    def test_bar_issues_flags_bad_bars(self):
        t = datetime.datetime(2026, 1, 5, 9, 15)
        bars = [{"t": t, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 5},
                {"t": t + datetime.timedelta(minutes=5), "o": 0, "h": 2, "l": 0, "c": 1, "v": 5},
                {"t": t + datetime.timedelta(minutes=10), "o": 1, "h": 1, "l": 2, "c": 1, "v": 0}]
        iss = " ".join(pf.bar_issues(bars, "5m", False))
        self.assertIn("non_positive_price", iss)
        self.assertIn("high_below_low", iss)
        self.assertIn("zero_volume_bar", iss)

    def test_universe_maps_tokens_and_reports_missing(self):
        csv_text = "Company Name,Industry,Symbol,Series,ISIN Code\nA,x,AAA,EQ,I1\nB,x,TATAPOWER,EQ,I2\nC,x,ZZZ,EQ,I3\n"
        eq = {"AAA": {"token": "11", "series": "EQ"}, "TATAPOWER": {"token": "3426", "series": "EQ"},
              "NTPC": {"token": "11630", "series": "EQ"}}
        uni, problems = pf.build_universe(csv_text, eq, {"Nifty 50": "99926000"})
        syms = [u["symbol"] for u in uni]
        self.assertEqual(syms[0], "NIFTY")
        self.assertEqual(syms.count("TATAPOWER"), 1)
        self.assertIn("NTPC", syms)
        self.assertTrue(any("ZZZ" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
