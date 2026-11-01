"""india-backtest swing, point-in-time universe (pit.py + swing.py --universe pit100|pit500). Offline.

Real fixtures in fixtures/india-swing-pit/ (cut from NSE archive files downloaded 2026-10-29, plain GET
on nsearchives.nseindia.com):
  cm02JAN2015bhav-cut.csv.zip    legacy CM bhavcopy, header + a handful of rows (EQ, BE, an ETF, SME)
  udiff-20260128-cut.csv.zip     UDiFF CM bhavcopy (BhavCopy_NSE_CM_0_0_0_20260128_F_0000), cut the same way
  bhav-ca-rows.json              slim bhavcopy rows (pit.slim layout) for RELIANCE around its 1:1 bonuses
                                 (ex 2017-09-07, legacy; ex 2024-10-28, UDiFF), IRCTC around its 1:5 split
                                 (ex 2021-10-28), TATAMOTORS around the TMPV demerger (ex 2025-10-14)
  bc-rows.json                   the matching rows of NSE's Bc corporate-action files (PRddmmyy.zip)
  bc070917-cut.csv, bc28012026-cut.csv   two Bc files cut to a few rows (old dd/mm/yyyy, new ISO dates)
  pit-mini/                      a tiny TRADER_DIR: cache/pit_universe.json (lists built for the test
                                 from five real bhavcopy-adjusted series) + cache/pit_daily/*.json.gz
Benchmarks for the CLI run: fixtures/india-swing/ (Yahoo ^NSEI, NIFTYBEES, ^INDIAVIX).
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
from contextlib import redirect_stderr, redirect_stdout

from _load import load, FIX

import nse

sw = load("india-backtest", "swing")
pit = load("india-backtest", "pit")

PF = os.path.join(FIX, "india-swing-pit")
SF = os.path.join(FIX, "india-swing")


def days(n, start="2020-01-01"):
    d = datetime.date.fromisoformat(start)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


def series(sym, dates, closes, vol=1000):
    rows = []
    for d, c in zip(dates, closes):
        rows.append([d, c, c * 1.005, c * 0.995, c, vol])
    return sw.Series(sym, rows, True)


# ================================================================ bhavcopy parsers

class Parsers(unittest.TestCase):
    def test_legacy_bhavcopy(self):
        raw = open(os.path.join(PF, "cm02JAN2015bhav-cut.csv.zip"), "rb").read()
        day, rows = nse.parse_cm_bhav(raw)
        self.assertEqual(day, "2015-01-02")
        rel = [r for r in rows if r["symbol"] == "RELIANCE"][0]
        self.assertEqual((rel["series"], rel["open"], rel["high"], rel["low"], rel["close"], rel["prev_close"]),
                         ("EQ", 888.1, 896.05, 884.3, 885.55, 887.9))
        self.assertEqual(rel["volume"], 1675827)
        self.assertAlmostEqual(rel["value"], 1490461120.25)
        self.assertEqual(rel["isin"], "INE002A01018")
        day, kept = nse.parse_cm_bhav(raw, ("EQ", "BE", "BZ"))
        self.assertTrue(all(r["series"] in ("EQ", "BE", "BZ") for r in kept))
        self.assertLess(len(kept), len(rows))

    def test_udiff_bhavcopy(self):
        raw = open(os.path.join(PF, "udiff-20260128-cut.csv.zip"), "rb").read()
        day, rows = nse.parse_cm_bhav(raw, ("EQ", "BE", "BZ"))
        self.assertEqual(day, "2026-01-28")
        rel = [r for r in rows if r["symbol"] == "RELIANCE"][0]
        self.assertEqual((rel["open"], rel["high"], rel["low"], rel["close"], rel["prev_close"]),
                         (1385.0, 1409.9, 1384.9, 1396.7, 1380.5))
        self.assertEqual(rel["volume"], 11121657)
        self.assertEqual(rel["isin"], "INE002A01018")
        self.assertNotIn("GB", set(r["series"] for r in rows))

    def test_urls_and_switch_date(self):
        self.assertEqual(nse.cm_bhav_url(datetime.date(2019, 6, 3)),
                         "https://nsearchives.nseindia.com/content/historical/EQUITIES/2019/JUN/cm03JUN2019bhav.csv.zip")
        self.assertEqual(nse.cm_bhav_url(datetime.date(2024, 7, 5)).rsplit("/", 1)[1], "cm05JUL2024bhav.csv.zip")
        self.assertEqual(nse.cm_bhav_url(datetime.date(2024, 7, 8)),
                         "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv.zip")
        self.assertIn("nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR070917.zip",
                      datetime.date(2017, 9, 7).strftime(pit.PR_URL))

    def test_not_a_bhavcopy(self):
        with self.assertRaises(nse.NSEError):
            nse.parse_cm_bhav("A,B\n1,2\n")

    def test_fetch_falls_back_and_404_is_a_holiday(self):
        calls = []

        def fake(url):
            calls.append(url)
            raise nse.NSEError("HTTP 404 from nsearchives.nseindia.com")
        old = pit._http
        pit._http = fake
        try:
            how, doc = pit.fetch_day(datetime.date(2024, 3, 25), sleep=lambda s: None)
        finally:
            pit._http = old
        self.assertEqual(how, "holiday")
        self.assertEqual(len(calls), 2)                                     # legacy, then UDiFF (2024 back-fill)
        self.assertIn("cm25MAR2024bhav", calls[0])
        self.assertIn("20240325", calls[1])

    def test_slim_rows(self):
        raw = open(os.path.join(PF, "udiff-20260128-cut.csv.zip"), "rb").read()
        day, rows = nse.parse_cm_bhav(raw, pit.KEEP_SERIES)
        doc = pit.slim(day, rows, "udiff")
        r = [x for x in doc["rows"] if x[0] == "RELIANCE"][0]
        self.assertEqual(r, ["RELIANCE", "EQ", 1385.0, 1409.9, 1384.9, 1396.7, 1380.5, 11121657, 15535335258,
                             "INE002A01018"])


# ================================================================ corporate actions

def ca_rows():
    return json.load(open(os.path.join(PF, "bhav-ca-rows.json")))


def chain_rows(sym, d_from=None, d_to=None):
    """pit.build_chains rows [d, o, h, l, c, pc, v, value, series] for one symbol from the fixture."""
    out = []
    for d, rows in sorted(ca_rows().items()):
        if (d_from and d < d_from) or (d_to and d > d_to):
            continue
        for r in rows:
            if r[0] == sym:
                out.append([d, r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[1]])
    return out


def actions():
    out = {}
    for sym, ser, ex, purpose in json.load(open(os.path.join(PF, "bc-rows.json"))):
        if pit.ca_kinds(purpose):
            L = out.setdefault(sym, {}).setdefault(ex, [])
            if purpose not in L:
                L.append(purpose)
    return out


class CorporateActions(unittest.TestCase):
    def test_bhavcopy_prev_close_is_not_adjusted(self):
        """Why the prev-close route is not used: on these real ex-dates NSE's PREVCLOSE is the raw close."""
        rows = ca_rows()
        for sym, before, ex in (("RELIANCE", "2017-09-06", "2017-09-07"), ("RELIANCE", "2024-10-25", "2024-10-28"),
                                ("IRCTC", "2021-10-27", "2021-10-28"), ("TATAMOTORS", "2025-10-13", "2025-10-14")):
            c0 = [r for r in rows[before] if r[0] == sym][0][5]
            pc = [r for r in rows[ex] if r[0] == sym][0][6]
            self.assertAlmostEqual(pc, c0, places=2, msg="%s %s" % (sym, ex))

    def test_purpose_parsing(self):
        k = pit.ca_kinds
        self.assertEqual(k("BONUS 1:1"), [("bonus 1:1", 0.5)])
        self.assertEqual(k("BONUS 3:2"), [("bonus 3:2", 0.4)])
        self.assertEqual(k("FV SPLT FRM RS 10 TO RS 2"), [("split 10->2", 0.2)])
        self.assertEqual(k("FV SPLT FRM RS 5 TO RE 1"), [("split 5->1", 0.2)])
        self.assertEqual(k("FACE VALUE SPLIT (SUB-DIVISION) - FROM RS 10/- PER SHARE TO RS 2/- PER SHARE"),
                         [("split 10->2", 0.2)])
        both = k("BONUS 4:1/FV SPLIT RS 2 TO RE 1")
        self.assertAlmostEqual(both[0][1] * both[1][1], 0.1)
        self.assertEqual(k("CONSOLIDATION OF SHARES FROM RS 1 TO RS 10"), [("consolidation 1->10", 10.0)])
        self.assertEqual(k("RIGHTS 1:5 @ PREMIUM RS 85/-"), [("rights", None)])
        self.assertEqual(k("SCHEME OF ARRANGEMENT"), [("demerger", None)])
        # NSE's purpose field is cut at ~25 characters: the ratio is lost, the gap decides (snapped)
        self.assertEqual(k("FV SPLIT FROM RS10 TO RS"), [("split (ratio cut off)", None)])
        self.assertEqual(k("BONUS 1:1/FV SPL 10 TO 2"), [("bonus 1:1", 0.5), ("split 10->2", 0.2)])
        self.assertEqual(k("SCH OF ARNGMNT/CAP REDCTN"), [("demerger", None)])
        self.assertAlmostEqual(pit.snap_ratio(0.1985), 0.2)
        self.assertAlmostEqual(pit.snap_ratio(0.0995), 0.1)
        rows = [["2020-01-01", 100, 101, 99, 100, 99, 10, 1000, "EQ"], ["2020-01-02", 10.1, 10.3, 9.9, 10.2, 100, 90, 900, "EQ"]]
        ev = pit.ca_factors(rows, [("2020-01-02", "X", ["FV SPLIT FROM RS10 TO RS"])])[0]
        self.assertAlmostEqual(ev["factor"], 0.1)
        for x in ("DIV-RS 5 PER SHARE", "INTEREST PAYMENT", "ANNUAL GENERAL MEETING", "SCHEME OF AMALGAMATION"):
            self.assertEqual(k(x), [], x)

    def test_bc_files_old_and_new_date_formats(self):
        old = pit.parse_bc(open(os.path.join(PF, "bc070917-cut.csv"), "rb").read())
        self.assertIn(["RELIANCE", "EQ", "2017-09-07", "BONUS 1:1"], old)
        new = pit.parse_bc(open(os.path.join(PF, "bc28012026-cut.csv"), "rb").read())
        self.assertTrue(new and all(len(r[2]) == 10 and r[2][4] == "-" for r in new))
        self.assertTrue(all(r[1] in pit.KEEP_SERIES for r in new + old))

    def _events(self, sym, d_from, d_to):
        rows = chain_rows(sym, d_from, d_to)
        evs = pit.ca_factors(rows, pit.chain_actions([(sym, rows[0][0])], actions()))
        return rows, evs

    def test_real_bonus_reliance_2024(self):
        rows, evs = self._events("RELIANCE", "2024-10-01", "2024-11-30")
        ev = [e for e in evs if e["date"] == "2024-10-28"][0]
        self.assertEqual(ev["factor"], 0.5)
        self.assertEqual(ev["how"], "purpose ratio")
        bars = pit.apply_factors(rows, evs)
        d = dict((b[0], b) for b in bars)
        self.assertAlmostEqual(d["2024-10-25"][4], 2655.70 * 0.5, places=2)      # 1,327.85 as Yahoo shows it
        self.assertAlmostEqual(d["2024-10-28"][4], 1334.35, places=2)
        raw = dict((r[0], r) for r in rows)
        self.assertEqual(d["2024-10-25"][5], int(round(raw["2024-10-25"][6] / 0.5)))   # volume doubled

    def test_real_bonus_reliance_2017_legacy(self):
        rows, evs = self._events("RELIANCE", "2017-08-15", "2017-09-30")
        ev = [e for e in evs if e["date"] == "2017-09-07"][0]
        self.assertEqual(ev["factor"], 0.5)

    def test_real_split_irctc_2021(self):
        rows, evs = self._events("IRCTC", "2021-10-01", "2021-11-30")
        ev = [e for e in evs if e["date"] == "2021-10-28"][0]
        self.assertAlmostEqual(ev["factor"], 0.2)
        bars = pit.apply_factors(rows, evs)
        d = dict((b[0], b[4]) for b in bars)
        self.assertLess(abs(d["2021-10-28"] / d["2021-10-27"] - 1), 0.30)       # no fake -78 % day left

    def test_real_demerger_tata_motors_2025(self):
        rows, evs = self._events("TATAMOTORS", "2025-09-15", "2025-10-31")
        ev = [e for e in evs if e["date"] == "2025-10-14"][0]
        self.assertEqual(ev["how"], "ex-day open / last close")
        self.assertAlmostEqual(ev["factor"], 400.0 / 660.75, places=4)
        bars = pit.apply_factors(rows, evs)
        d = dict((b[0], b) for b in bars)
        self.assertAlmostEqual(d["2025-10-13"][4], 660.75 * 400.0 / 660.75, places=2)

    def test_listed_action_without_a_price_effect_is_skipped(self):
        rows = chain_rows("RELIANCE", "2024-10-01", "2024-11-30")
        fake = {"RELIANCE": {"2024-10-15": ["BONUS 1:1"]}}                     # nothing happened that day
        evs = pit.ca_factors(rows, pit.chain_actions([("RELIANCE", rows[0][0])], fake))
        self.assertIsNone(evs[0]["factor"])
        self.assertIn("skipped", evs[0]["how"])


# ================================================================ PIT ranking

def day_rows(spec, dates):
    """spec {symbol: (isin, series, close, value or [values], first index)} -> [(date, slim rows)]."""
    out = []
    for k, d in enumerate(dates):
        rows = []
        for sym, (isin, ser, px, val, first, last) in sorted(spec.items()):
            if k < first or k > last:
                continue
            v = val[k] if isinstance(val, list) else val
            s = ser(k) if callable(ser) else ser
            rows.append([sym, s, px, px, px, px, px, 100, v, isin])
        out.append((d, rows))
    return out


class Ranking(unittest.TestCase):
    def test_month_end_ranking_by_median_traded_value(self):
        ds = days(260, "2019-01-01")
        spiky = [1.0] * 260
        spiky[255] = 1e12                                                    # one huge day: the median ignores it
        spec = {"BIG": ("INE000A01011", "EQ", 100.0, 5e8, 0, 999),
                "MID": ("INE000B01011", "EQ", 100.0, 2e8, 0, 999),
                "SPIKY": ("INE000C01011", "EQ", 100.0, spiky, 0, 999),
                "ETF": ("INF000D01011", "EQ", 100.0, 9e9, 0, 999),           # an ETF: not a company
                "PENNY": ("INE000E01011", "EQ", 12.0, 8e9, 0, 999),          # under Rs 20
                "YOUNG": ("INE000F01011", "EQ", 100.0, 7e9, 120, 999),       # under 200 sessions of history
                "T2T": ("INE000G01011", "BE", 100.0, 6e9, 0, 999),           # trade-to-trade that day
                "GONE": ("INE000H01011", "EQ", 100.0, 4e8, 0, 230)}          # stops trading after session 230
        dd = day_rows(spec, ds)
        chains = pit.build_chains(iter(dd), keep=set())
        names = pit.unique_names(chains)
        lists = pit.pit_lists(dict((names[c], ch) for c, ch in chains.items()), ds,
                              sizes={"top2": 2, "top4": 4})
        ends = sorted(lists)
        first = ends[0]
        self.assertGreaterEqual(ds.index(first) + 1, pit.MIN_HISTORY)
        self.assertEqual(lists[first]["top2"], ["BIG", "GONE"])
        self.assertEqual(lists[first]["top4"], ["BIG", "GONE", "MID", "SPIKY"])
        self.assertEqual(lists[first]["eligible"], 4)
        last = ends[-1]
        self.assertNotIn("GONE", lists[last]["top4"])                       # no row on that month-end
        self.assertEqual(lists[last]["top2"], ["BIG", "MID"])
        self.assertEqual(lists[last]["top4"][-1], "SPIKY")

    def test_a_rename_continues_the_chain(self):
        ds = days(12, "2024-01-01")
        spec = {"OLDNAME": ("INE111A01011", "EQ", 50.0, 1e7, 0, 5),
                "NEWNAME": ("INE111A01011", "EQ", 50.0, 1e7, 6, 999),
                "OTHER": ("INE222A01011", "EQ", 80.0, 1e7, 0, 999)}
        chains = pit.build_chains(iter(day_rows(spec, ds)))
        self.assertEqual(len(chains), 2)
        ch = [c for c in chains.values() if "NEWNAME" in c["symbols"]][0]
        self.assertEqual(ch["symbols"], ["OLDNAME", "NEWNAME"])
        self.assertEqual(len(ch["rows"]), 12)
        self.assertEqual(ch["segs"], [("OLDNAME", ds[0]), ("NEWNAME", ds[6])])
        self.assertEqual(pit.unique_names(chains)[[k for k, c in chains.items() if c is ch][0]], "NEWNAME")

    def test_be_days_stay_in_the_chain(self):
        ds = days(10, "2024-01-01")
        spec = {"SLIDE": ("INE333A01011", lambda k: "BE" if 3 <= k <= 6 else "EQ", 30.0, 1e6, 0, 999)}
        ch = list(pit.build_chains(iter(day_rows(spec, ds))).values())[0]
        self.assertEqual(len(ch["rows"]), 10)
        self.assertEqual(list(ch["eq"]), [1, 1, 1, 0, 0, 0, 0, 1, 1, 1])

    def test_missing_session_is_flagged_not_adjusted(self):
        ds = days(30, "2024-01-01")
        spec = dict(("S%02d" % i, ("INE9%02dA01011" % i, "EQ", 100.0, 1e6, 0, 999)) for i in range(60))
        dd = day_rows(spec, ds)
        for r in dd[20][1]:
            r[6] = 101.0                                                      # every prev close says 101
        st = {}
        pit.build_chains(iter(dd), keep=set(), stats=st)
        self.assertEqual(pit.suspect_sessions(st), [ds[20]])


# ================================================================ the engine with membership

class Membership(unittest.TestCase):
    def setUp(self):
        self.ds = days(80, "2021-01-01")
        flat = [100.0] * 80
        up = [100.0] * 60 + [110.0] + [111.0] * 19                             # breaks out on session 60
        self.uni = {"IN": series("IN", self.ds, up), "OUT": series("OUT", self.ds, up),
                    "BASE": series("BASE", self.ds, flat)}

    def test_membership_lookup(self):
        m = sw.Membership({"2021-01-29": ["A"], "2021-02-26": ["B"]}, name="pit100")
        self.assertFalse(m.member("A", "2021-01-28"))
        self.assertTrue(m.member("A", "2021-01-29"))                           # known at that close
        self.assertTrue(m.member("A", "2021-02-25"))
        self.assertFalse(m.member("A", "2021-02-26"))
        self.assertTrue(m.member("B", "2021-03-31"))
        self.assertTrue(m.is_core("B", "2021-03-31"))
        m5 = sw.Membership({"2021-01-29": ["A", "B"]}, core={"2021-01-29": ["A"]})
        self.assertTrue(m5.is_core("A", "2021-02-01"))
        self.assertFalse(m5.is_core("B", "2021-02-01"))
        self.assertEqual(m5.ever(), {"A", "B"})

    def test_cannot_buy_a_non_member_and_can_exit_after_leaving(self):
        sig = self.ds[60]
        lists = {self.ds[0]: ["IN", "BASE"]}
        # IN leaves the list right after it was bought
        lists[self.ds[62]] = ["BASE"]
        mem = sw.Membership(lists)
        p = {"n": 20, "exit_n": 10}
        res = sw.simulate(self.uni, "breakout_20d", p, self.ds, {"membership": mem, "delist_exit": True})
        syms = [t["symbol"] for t in res["trades"]] + [o["symbol"] for o in res["open"]]
        self.assertIn("IN", syms)
        self.assertNotIn("OUT", syms)                                            # same signal, not a member
        both = sw.simulate(self.uni, "breakout_20d", p, self.ds, {})
        self.assertIn("OUT", [t["symbol"] for t in both["trades"]] + [o["symbol"] for o in both["open"]])
        # now make IN fall after leaving the list: the trailing exit still fires
        down = [100.0] * 60 + [110.0, 111.0, 111.0, 112.0] + [90.0] * 16
        uni = dict(self.uni, IN=series("IN", self.ds, down))
        res = sw.simulate(uni, "breakout_20d", p, self.ds, {"membership": mem, "delist_exit": True})
        t = [x for x in res["trades"] if x["symbol"] == "IN"][0]
        self.assertEqual(t["signal_date"], sig)
        self.assertEqual(t["entry_date"], self.ds[61])
        self.assertGreater(t["exit_date"], self.ds[62])
        self.assertTrue(t["reason"].startswith("close below"))

    def test_rotation_ranks_members_only(self):
        ds = days(300, "2020-01-01")
        a = [100.0 * (1.003 ** k) for k in range(300)]
        b = [100.0 * (1.002 ** k) for k in range(300)]
        uni = {"A": series("A", ds, a), "B": series("B", ds, b)}
        me = [d for d in sw.month_ends(ds)]
        mem = sw.Membership(dict((e, ["B"]) for e in me))
        res = sw.simulate(uni, "momentum_rotation", {"top": 1, "look": 252, "skip": 21}, ds, {"membership": mem})
        held = set(t["symbol"] for t in res["trades"]) | set(o["symbol"] for o in res["open"])
        self.assertEqual(held, {"B"})
        res = sw.simulate(uni, "momentum_rotation", {"top": 1, "look": 252, "skip": 21}, ds, {})
        self.assertEqual(set(o["symbol"] for o in res["open"]), {"A"})

    def test_pit500_slippage_follows_the_top_100_at_the_time(self):
        mem = sw.Membership({self.ds[0]: ["IN", "BASE"]}, core={self.ds[0]: ["BASE"]})
        res = sw.simulate(self.uni, "breakout_20d", {"n": 20, "exit_n": 10}, self.ds, {"membership": mem})
        pos = ([t for t in res["trades"] if t["symbol"] == "IN"] or [o for o in res["open"] if o["symbol"] == "IN"])[0]
        op = self.uni["IN"].o[61]
        self.assertAlmostEqual(pos["entry"], round(op * 1.0025, 4))             # 0.25 %: not in the top 100


class Delisting(unittest.TestCase):
    def test_position_exits_at_the_last_close_when_bars_end(self):
        ds = days(80, "2021-01-01")
        up = [100.0] * 60 + [110.0, 111.0, 112.0, 113.0, 114.0]                # trades until session 64 only
        uni = {"GONE": series("GONE", ds[:65], up), "BASE": series("BASE", ds, [100.0] * 80)}
        mem = sw.Membership({ds[0]: ["GONE", "BASE"]})
        res = sw.simulate(uni, "breakout_20d", {"n": 20, "exit_n": 10}, ds, {"membership": mem, "delist_exit": True})
        t = [x for x in res["trades"] if x["symbol"] == "GONE"][0]
        self.assertEqual(t["reason"], "delisted")
        self.assertEqual(t["exit_date"], ds[64])
        self.assertAlmostEqual(t["exit_ref"], 114.0)
        self.assertAlmostEqual(t["exit"], round(114.0 * (1 - 0.001), 4))        # last close less slippage
        self.assertEqual(res["open"], [])
        # without the delisting rule (the Yahoo path) the position just sits there
        res = sw.simulate(uni, "breakout_20d", {"n": 20, "exit_n": 10}, ds, {"membership": mem})
        self.assertEqual([o["symbol"] for o in res["open"]], ["GONE"])


# ================================================================ a whole PIT run on a tiny fixture

class Rerun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        shutil.copytree(os.path.join(PF, "pit-mini", "cache"), os.path.join(self.tmp, "cache"))
        os.makedirs(os.path.join(self.tmp, "cache", "daily"))
        for f in os.listdir(SF):
            if f.startswith("yahoo-") and ("NSEI" in f or "INDIAVIX" in f or "NIFTYBEES" in f):
                shutil.copy(os.path.join(SF, f), os.path.join(self.tmp, "cache", "daily"))
        self.old = os.environ.get("TRADER_DIR")
        os.environ["TRADER_DIR"] = self.tmp

    def tearDown(self):
        if self.old is None:
            os.environ.pop("TRADER_DIR", None)
        else:
            os.environ["TRADER_DIR"] = self.old
        shutil.rmtree(self.tmp)

    def test_compare_pit_writes_and_compares_with_the_biased_twin(self):
        out, err = io.StringIO(), io.StringIO()
        offline = os.path.join(self.tmp, "cache", "daily")
        args = ["--from", "2019-01-01", "--now", "2026-10-29T18:00", "--offline", offline, "--quiet"]
        with redirect_stdout(out), redirect_stderr(err):
            rc = sw.main(["compare", "--universe", "pit100", "--strategy", "breakout_20d,momentum_rotation",
                          "--no-extras"] + args + ["--offline", "%s,%s" % (offline, os.path.join(self.tmp, "cache"))])
        self.assertEqual(rc, 0, err.getvalue())
        doc = json.load(open(os.path.join(self.tmp, "backtest", "2026-10-29-swing-pit100.json")))
        m = doc["meta"]
        self.assertEqual(m["universe"], "pit100")
        self.assertTrue(m["pit"]["ended_count"] >= 1)
        self.assertTrue(m["caveats"][0].startswith("POINT-IN-TIME"))
        r = doc["strategies"]["breakout_20d"]
        # every entry was a member on its signal date
        U = json.load(open(os.path.join(self.tmp, "cache", "pit_universe.json")))["pit100"]
        mem = sw.Membership(U)
        for t in r["trades"]:
            self.assertTrue(mem.member(t["symbol"], t["signal_date"]), t)
        md = open(os.path.join(self.tmp, "backtest", "2026-10-29-swing-pit100.md")).read()
        self.assertIn("Point-in-time universe", md)
        # a biased twin written the same day -> the bias table and sw_bias rows
        twin = dict(doc, meta=dict(m, universe="nifty100"))
        twin.pop("bias", None)
        for n in twin["strategies"]:
            twin["strategies"][n] = json.loads(json.dumps(twin["strategies"][n]))
            twin["strategies"][n]["full"]["cagr_pct"] = (doc["strategies"][n]["full"]["cagr_pct"] or 0) + 5.0
        json.dump(twin, open(os.path.join(self.tmp, "backtest", "2026-10-29-swing-nifty100.json"), "w"))
        with redirect_stdout(out), redirect_stderr(err):
            rc = sw.main(["compare", "--universe", "pit100", "--strategy", "breakout_20d,momentum_rotation",
                          "--no-extras"] + args + ["--offline", "%s,%s" % (offline, os.path.join(self.tmp, "cache"))])
        self.assertEqual(rc, 0, err.getvalue())
        doc = json.load(open(os.path.join(self.tmp, "backtest", "2026-10-29-swing-pit100.json")))
        self.assertEqual(doc["bias"]["pair"], "nifty100→pit100")
        row = [x for x in doc["bias"]["rows"] if x["strategy"] == "breakout_20d"][0]
        self.assertAlmostEqual(row["inflation_pp"], 5.0, places=1)
        dash = json.load(open(os.path.join(self.tmp, "backtest", "2026-10-29-swing-dash.json")))
        self.assertIn("sw_bias", dash)
        self.assertEqual(set(dash["sw_bias"][0]) >= {"strategy", "universe_pair", "cagr_biased", "cagr_pit",
                                                    "inflation_pp", "oos_biased", "oos_pit"}, True)
        self.assertEqual([u for u in dict.fromkeys(x["universe"] for x in dash["sw_strategies"])],
                         ["nifty100", "pit100"])
        self.assertLess(os.path.getsize(os.path.join(self.tmp, "backtest", "2026-10-29-swing-dash.json")), 1000000)


if __name__ == "__main__":
    unittest.main()
