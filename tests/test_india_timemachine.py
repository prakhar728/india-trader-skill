"""india-timemachine tests. Offline, on real Yahoo fixtures:

common/yahoo-RELIANCE.NS-60d5m.json.gz              RELIANCE, 60 sessions of 5-minute bars to 2026-10-29
                                                    (72 to 75 bars a session; no 15:20 bar, the F&O
                                                    closing auction; 15:25 on 12 days only)
india-timemachine/yahoo-HDFCBANK.NS-60d5m.json.gz   HDFCBANK, same range (about Rs 680: a 0.05 tick),
                                                    fetched 2026-10-29 15:29 IST with curl, UA
                                                    "Mozilla/5.0 (compatible; india-trader-skill/1.0)"

The worked example (entry -> exit -> time machine, seed 19): RELIANCE Thu 2026-10-29, entry
1,174.30 at 09:40 IST (bar 4), stop 1,163.30 (opening-range midpoint), target 1,196.30 (2R),
flat by 15:05, 45 shares for the charges. Dhan responses cannot be recorded (no keys): the Dhan
test builds one in the documented /v2/charts/intraday shape from the same real bars.
"""

import datetime
import hashlib
import io
import json
import os
import random
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, COMMON, ROOT

import nse

tm = load("india-timemachine", "timemachine")
entry = load("india-entry", "entry")
exitp = load("india-exit", "exit")

R60 = os.path.join(COMMON, "yahoo-RELIANCE.NS-60d5m.json.gz")
HDFC = os.path.join(FIX, "india-timemachine", "yahoo-HDFCBANK.NS-60d5m.json.gz")
SCRIP = os.path.join(COMMON, "dhan-scrip-sample.csv")
TEXT = nse.read_maybe_gz(R60).decode("utf-8")
META, BARS, RAW = tm.chart_from_text(TEXT, "RELIANCE")
NOW = datetime.datetime(2026, 10, 29, 9, 45)

# trader/plan.json for the example, as india-exit writes it
PLAN = {"created_by": "india-exit", "symbol": "RELIANCE", "exchange": "NSE", "side": "long", "date": "2026-10-29",
        "entry": 1174.3, "stop": 1163.3, "target": 1196.3, "tick": 0.1, "risk_per_share": 11.0, "r_multiple": 2.0,
        "stop_rule": "or-mid", "target_rule": "2R", "flat_by": "15:05",
        "opening_range": {"high": 1170.5, "low": 1156.2, "minutes": 15}, "made": "2026-10-29T09:41:00+05:30",
        "setup": "orb", "interval": "5m", "entry_time": "09:40", "entry_bar": 4, "charges": {"qty": 45}}


def sha(x):
    return hashlib.sha256(json.dumps(x).encode("utf-8")).hexdigest()[:16]


def plan(**kw):
    raw = dict(PLAN)
    raw.update(kw)
    return tm.normalize_plan(raw)


def machine(p=None, bars=None, now=NOW, meta=None, **kw):
    return tm.time_machine(meta if meta is not None else META, BARS if bars is None else bars, p or plan(), now,
                           raw_count=RAW, **kw)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv)
    return code, out.getvalue(), err.getvalue()


class Example(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = machine()

    def test_the_odds(self):
        m = self.doc["mc"]
        self.assertEqual((m["replays"], m["seed"], m["window"], m["until"]), (10000, 19, 2, "15:05"))
        self.assertEqual((m["pool_sessions"], m["bars_left"]), (59, 65))
        self.assertEqual((m["pool_from"], m["pool_to"]), ("2026-08-04", "2026-10-28"))
        self.assertEqual((m["p_target"], m["p_stop"], m["p_neither"], m["p_green"]), (0.0293, 0.2886, 0.6821, 0.453))
        self.assertEqual((m["median_close"], m["p5_close"], m["p95_close"]), (1173.35, 1157.02, 1190.16))
        self.assertEqual((m["avg_R"], m["avg_R_net"]), (-0.06, -0.174))
        self.assertEqual((m["qty"], m["r_at_target"], m["r_net_at_target"], m["r_net_at_stop"]), (45, 2.0, 1.885, -1.113))
        self.assertEqual(m["bands"][-1], [1156.74, 1166.43, 1173.27, 1180.15, 1189.96])
        self.assertEqual(m["hist"]["bins"][:4], [92, 90, 160, 187])
        self.assertEqual(m["skipped_slots"], [])
        self.assertEqual(m["pool_bars"], {"min": 58, "max": 59})          # Yahoo dropped a bar here and there

    def test_net_r_is_the_charges_formula(self):
        m = self.doc["mc"]
        c_t = nse.charges(45 * 1174.3, 45 * 1196.3)["total"]
        c_s = nse.charges(45 * 1174.3, 45 * 1163.3)["total"]
        self.assertAlmostEqual(m["r_net_at_target"], (22.0 * 45 - c_t) / (11.0 * 45), places=3)
        self.assertAlmostEqual(m["r_net_at_stop"], (-11.0 * 45 - c_s) / (11.0 * 45), places=3)

    def test_the_history_replay(self):
        h = self.doc["history"]
        self.assertEqual((h["sessions"], h["triggered"], h["no_opening_range"]), (59, 28, 0))
        self.assertEqual((h["target"], h["stop"], h["close"], h["green"]), (8, 13, 7, 13))
        self.assertEqual((h["avg_R"], h["avg_R_net"]), (0.156, -0.031))
        self.assertEqual(h["rule"], {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 2.0})
        self.assertEqual(h["entry_window"], ["09:30", "14:30"])
        first = h["trades"][0]
        self.assertEqual((first["date"], first["time"], first["entry"], first["stop"], first["target"], first["outcome"]),
                         ("2026-08-06", "09:45", 1300.9, 1292.0, 1318.7, "target"))
        self.assertTrue(all(t["time"] >= "09:30" and t["time"] <= "14:30" for t in h["trades"]))

    def test_the_verdict_weighs_the_charges(self):
        self.assertEqual(self.doc["verdict"],
                         "Strongly toward the stop: at 2R (+1.89R after charges, -1.11R at the stop) the target has "
                         "to come first in 37 % of the replays that hit one side; it did in 9 % (target first 2.9 % "
                         "vs stop first 28.9 %).")
        gap, share, even = tm.lean(self.doc["mc"])
        self.assertEqual((round(share, 3), round(even, 3)), (0.092, 0.371))
        self.assertEqual(self.doc["verdict_notes"],
                         ["68.2 % of the replays hit neither by 15:05; the price at 15:05 decides those."])

    def test_bands_start_at_the_entry(self):
        m = self.doc["mc"]
        self.assertEqual(m["bands"][0], [1174.3] * 5)
        self.assertEqual(len(m["bands"]), 66)
        self.assertEqual((m["band_times"][0], m["band_times"][-1], len(m["band_times"])), ("09:40", "15:05", 66))
        self.assertTrue(all(p[0] == 1174.3 and len(p) == 66 for p in m["sample_paths"]))
        self.assertEqual(len(m["sample_paths"]), 160)

    def test_the_text_report(self):
        text = "\n".join(tm.report(self.doc))
        for line in ("RELIANCE long, Thursday 2026-10-29: entry 1174.30 at 09:40 IST (bar 4), stop 1163.30, "
                     "target 1196.30 (2R, risk 11.00 a share)", "10,000 replays", "65 five-minute bars", "seed 19",
                     "last 59 sessions", "target first   2.9 %", "stop first     28.9 %",
                     "neither        68.2 %   still open at 15:05", "green          45.3 %",
                     "median 1173.35, 90 % of replays between 1157.02 and 1190.16",
                     "Average R: -0.060 gross, -0.174 after charges",
                     "fired 28 times: 8 target, 13 stop, 7 neither. Average R +0.156 gross, -0.031 after charges",
                     "not a prediction", "--seed 19"):
            self.assertIn(line, text)
        for word in ("profit", "P&L", "guarantee", "$", "NYSE", " ET", "New York"):
            self.assertNotIn(word, text)

    def test_the_one_r_plan_leans_to_the_stop_after_charges(self):
        d = machine(plan(target=1185.3))
        m = d["mc"]
        self.assertEqual((m["p_target"], m["p_stop"], m["avg_R"], m["avg_R_net"]), (0.2601, 0.2875, -0.045, -0.159))
        # gross, 26.0 vs 28.7 would read as a coin flip; after charges 1R needs 56 % of the decided replays
        self.assertTrue(d["verdict"].startswith("Leans toward the stop: at 1R (+0.89R after charges"))


class Seeds(unittest.TestCase):
    def test_same_seed_same_numbers(self):
        self.assertEqual(machine(replays=2000, seed=5)["mc"], machine(replays=2000, seed=5)["mc"])

    def test_another_seed_gives_other_numbers(self):
        m = machine(seed=7)["mc"]
        self.assertNotEqual((m["p_target"], m["p_stop"], m["median_close"]), (0.0293, 0.2886, 1173.35))
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)

    def test_the_draws_are_stable_across_pythons(self):
        r = random.Random(19)
        self.assertEqual([r.randint(-2, 2) for _ in range(8)], [-2, 2, -2, 2, -1, 1, 0, 2])
        self.assertEqual(r.choice(range(59)), 18)

    def test_window_and_replays(self):
        a = machine(replays=500, window=0)["mc"]
        self.assertEqual((a["replays"], a["window"]), (500, 0))
        self.assertNotEqual(a["bands"], machine(replays=500)["mc"]["bands"])
        with self.assertRaises(tm.TMError):
            machine(replays=0)
        with self.assertRaises(tm.TMError):
            machine(window=11)


class Gaps(unittest.TestCase):
    def test_the_closing_auction_slots_are_skipped_not_fatal(self):
        d = machine(until="15:30", replays=2000)
        m = d["mc"]
        self.assertEqual(m["skipped_slots"], [{"slot": 73, "start": "15:20", "sessions": 0},
                                              {"slot": 74, "start": "15:25", "sessions": 12}])
        self.assertEqual((m["bars_left"], m["band_times"][-1]), (68, "15:20"))
        self.assertIn("Skipped 15:20 (0 of 59), 15:25 (12 of 59)", "\n".join(tm.report(d)))

    def test_random_holes_never_crash(self):
        rng = random.Random(3)
        holed = [b for b in BARS if rng.random() > 0.12]                    # drop about 12 % of the bars
        d = machine(bars=holed, replays=1000)
        self.assertGreaterEqual(d["mc"]["pool_sessions"], 20)
        self.assertAlmostEqual(d["mc"]["p_target"] + d["mc"]["p_stop"] + d["mc"]["p_neither"], 1.0, places=6)
        self.assertGreater(d["history"]["no_opening_range"], 0)            # a lost 09:15-09:25 bar skips that day

    def test_a_short_session_is_left_out(self):
        cut = [b for b in BARS if not (b["t"].date() == datetime.date(2026, 9, 4) and b["t"].hour >= 12)]
        d = machine(bars=cut, replays=300)
        self.assertEqual(d["mc"]["pool_sessions"], 58)
        self.assertEqual(d["data"]["short_sessions_left_out"], [{"date": "2026-09-04", "bars": 33}])
        self.assertNotIn("2026-09-04", [t["date"] for t in d["history"]["trades"]])
        self.assertIn("Left out 1 short session", "\n".join(tm.report(d)))

    def test_the_pool_keeps_the_last_60_and_never_today(self):
        d = machine(replays=200)
        self.assertEqual(d["mc"]["pool_to"], "2026-10-28")
        self.assertNotIn("2026-10-29", [t["date"] for t in d["history"]["trades"]])
        days = tm.split_sessions(tm.parse_bars(BARS, NOW)[0])
        self.assertEqual(len(tm.pool_sessions(days, "2026-10-29", keep=30)[0]), 30)

    def test_fewer_than_20_sessions_is_refused(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(bars=[b for b in BARS if b["t"].date() >= datetime.date(2026, 9, 30)])
        self.assertIn("only 19 full session(s)", str(cm.exception))
        self.assertIn("at least 20", str(cm.exception))
        d = machine(bars=[b for b in BARS if b["t"].date() >= datetime.date(2026, 9, 29)], replays=200)
        self.assertEqual(d["mc"]["pool_sessions"], 20)

    def test_bars_not_closed_yet_are_dropped(self):
        sb, dropped = tm.parse_bars(BARS, NOW, None, RAW)
        self.assertEqual(sb[-1]["start"], "09:40")
        self.assertEqual(dropped["no_price"], 116)                           # Yahoo's null bars
        with self.assertRaises(tm.TMError) as cm:
            machine(now=datetime.datetime(2026, 10, 29, 9, 38))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("its last is the 09:30 bar", str(cm.exception))

    def test_empty_and_broken_data(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(bars=[])
        self.assertIn("no complete 5-minute bars", str(cm.exception))
        gone = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                               "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(tm.TMError) as cm:
            tm.chart_from_text(gone, "ZZZZ")
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(tm.TMError):
            tm.chart_from_text("<html>rate limited</html>", "RELIANCE")
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(symbol="HDFCBANK"))
        self.assertIn("data is for RELIANCE.NS", str(cm.exception))


class Plans(unittest.TestCase):
    def test_plan_checks(self):
        cases = [
            (dict(entry_time="09:42", entry_bar=None), "5-minute grid"),
            (dict(entry_time="15:30", entry_bar=None), "between 09:20 and 15:25"),
            (dict(entry_bar=5), "disagrees with itself"),
            (dict(entry_time=None, entry_bar=None), "entry_time"),
            (dict(interval="1m"), "7 days of 1-min bars"),
            (dict(stop=1180.0), "stop < entry < target"),
            (dict(side="short"), "longs only"),
            (dict(symbol="DROP TABLE"), "NSE symbol"),
            (dict(entry="abc"), "number for entry"),
            (dict(date="29 Oct"), "date like"),
            (dict(opening_range={"minutes": 7}), "multiple of 5"),
        ]
        for change, words in cases:
            with self.assertRaises(tm.TMError, msg=change) as cm:
                plan(**change)
            self.assertIn(words, str(cm.exception), change)

    def test_entry_bar_or_entry_time_alone(self):
        self.assertEqual(plan(entry_time=None)["entry_time"], "09:40")
        self.assertEqual(plan(entry_bar=None)["entry_bar"], 4)
        self.assertEqual(plan(entry_time="9:40 am", entry_bar=None)["entry_bar"], 4)

    def test_until(self):
        d = machine(until="14:30", replays=300)
        self.assertEqual((d["mc"]["until"], d["mc"]["bars_left"]), ("14:30", 58))
        for bad, words in (("09:40", "nothing to replay"), ("15:35", "no later than 15:30"), ("14:32", "5-minute grid"),
                           ("noon", "IST time")):
            with self.assertRaises(tm.TMError) as cm:
                machine(until=bad, replays=10)
            self.assertIn(words, str(cm.exception), bad)

    def test_the_stop_rule_is_worked_out_when_not_given(self):
        self.assertEqual(machine(plan(stop_rule=None), replays=100)["history"]["rule"]["stop_rule"], "or_mid")
        rule = machine(plan(stop_rule=None, stop=1156.2, target=1210.5), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["target_r"]), ("or_low", 2.0))
        rule = machine(plan(stop_rule="atr", stop=1165.5, target=1183.1), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["stop_pct"], rule["target_r"]), ("pct", 0.7494, 1.0))

    def test_other_setups_skip_the_history(self):
        d = machine(plan(setup="level"), replays=200)
        self.assertIsNone(d["history"]["rule"])
        self.assertIn("History: skipped", "\n".join(tm.report(d)))

    def test_an_entry_outside_its_bar_is_flagged(self):
        d = machine(plan(entry=1180.0, stop=1170.0, target=1200.0), replays=200)
        self.assertIn("outside that bar's range", d["warnings"][0])

    def test_muhurat_is_refused(self):
        cfg = {"special_sessions": {"2026-11-08": ["18:00", "19:00"]}}
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-11-08"), cfg=cfg)
        self.assertIn("Muhurat", str(cm.exception))

    def test_qty_from_the_config_when_the_plan_has_none(self):
        d = machine(plan(charges=None), cfg={"max_loss_per_trade": 1000}, replays=200)
        self.assertEqual(d["mc"]["qty"], 90)


class OtherSources(unittest.TestCase):
    def test_hdfcbank_a_005_tick_stock(self):
        meta, bars, raw = tm.chart_from_text(nse.read_maybe_gz(HDFC).decode("utf-8"), "HDFCBANK")
        ch = entry.parse_chart(nse.read_maybe_gz(HDFC).decode("utf-8"), "HDFCBANK")
        r = entry.evaluate(ch, datetime.datetime(2026, 10, 29, 9, 40))
        self.assertEqual((r["trigger"]["time"], r["trigger"]["price"]), ("09:35", 682.5))
        sr, sp = exitp.parse_stop("or-mid")
        c = exitp.compute(682.5, sr, sp, "r", 2.0, r["opening_range"]["high"], r["opening_range"]["low"])
        self.assertEqual((c["tick"], c["stop"], c["target"]), (0.05, 680.35, 686.8))
        p = tm.normalize_plan({"symbol": "HDFCBANK", "date": "2026-10-29", "entry": 682.5, "stop": c["stop"],
                               "target": c["target"], "tick": c["tick"], "entry_time": "09:35",
                               "opening_range": {"minutes": 15}, "stop_rule": "or-mid", "flat_by": "15:05"})
        d = tm.time_machine(meta, bars, p, datetime.datetime(2026, 10, 29, 9, 40), replays=2000, raw_count=raw)
        self.assertEqual(d["mc"]["pool_sessions"], 59)
        self.assertAlmostEqual(d["mc"]["p_target"] + d["mc"]["p_stop"] + d["mc"]["p_neither"], 1.0, places=6)
        self.assertGreater(d["history"]["triggered"], 0)
        self.assertTrue(all(nse.on_tick(t["stop"], 0.05) and nse.on_tick(t["target"], 0.05)
                            for t in d["history"]["trades"]))

    def test_dhan_bars_give_the_same_numbers(self):
        # Dhan's documented /v2/charts/intraday shape, built from the same real bars
        doc = {"open": [b["o"] for b in BARS], "high": [b["h"] for b in BARS], "low": [b["l"] for b in BARS],
               "close": [b["c"] for b in BARS], "volume": [b["v"] for b in BARS],
               "timestamp": [nse.epoch_from_ist(b["t"]) for b in BARS]}
        sent = []

        def fake(url, headers=None, data=None, method=None, **kw):
            sent.append((url, data))
            return json.dumps(doc).encode()
        with open(SCRIP) as fh:
            scrip = nse.parse_dhan_scrip(fh.read())
        meta, bars = tm.fetch_dhan("RELIANCE", datetime.date(2026, 10, 29), nse.Dhan("1", "t", transport=fake), scrip)
        self.assertEqual((sent[0][1]["securityId"], sent[0][1]["interval"], sent[0][1]["fromDate"]),
                         ("2885", "5", "2026-08-01 09:15:00"))
        d = tm.time_machine(meta, bars, plan(), NOW, replays=10000, source="dhan")
        self.assertEqual((d["mc"]["p_target"], d["mc"]["p_stop"], d["mc"]["avg_R_net"]), (0.0293, 0.2886, -0.174))


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

    def test_entry_exit_timemachine_end_to_end(self):
        code, _, _ = run_mod(entry, ["check", "RELIANCE", "--offline", R60, "--now", "2026-10-29 09:40", "--write"])
        self.assertEqual(code, 0)
        code, _, err = run_mod(exitp, ["plan", "--now", "2026-10-29 09:41", "--write"])
        self.assertEqual(code, 0, err)
        code, out, err = run(["run", "--offline", R60, "--now", "2026-10-29 09:45", "--write"])
        self.assertEqual(code, 0, err)
        self.assertIn("target first   2.9 %", out)
        self.assertIn("Average R: -0.060 gross, -0.174 after charges", out)
        self.assertIn("/india-budget", out)
        with open(self.tpath("timemachine.json")) as fh:
            doc = json.load(fh)
        self.assertEqual((doc["created_by"], doc["mc"]["p_stop"], doc["plan"]["qty"]), ("india-timemachine", 0.2886, 45))
        self.assertEqual(doc["made"], "2026-10-29T09:45:00+05:30")
        code, out, _ = run(["chart", "--from"])
        self.assertEqual(code, 0)
        self.assertIn("1196.30 |", out)
        self.assertIn("target", out)
        self.assertIn("Strongly toward the stop", out)

    def test_a_contract_plan_without_entry_time_uses_entry_json(self):
        os.makedirs(os.path.join(self.d, "trader"))
        bare = {k: PLAN[k] for k in ("symbol", "exchange", "side", "date", "entry", "stop", "target", "tick",
                                     "risk_per_share", "r_multiple", "stop_rule", "target_rule", "flat_by",
                                     "opening_range", "made")}
        nse.write_json(self.tpath("plan.json"), bare)
        code, _, err = run(["run", "--offline", R60, "--now", "2026-10-29 09:45", "--replays", "100"])
        self.assertIn("needs entry_time", err)
        nse.write_json(self.tpath("entry.json"), {"symbol": "RELIANCE", "date": "2026-10-29", "status": "triggered",
                                                  "time": "09:40", "bar_index": 4, "price": 1174.3})
        code, out, err = run(["run", "--offline", R60, "--now", "2026-10-29 09:45", "--json"])
        self.assertEqual(code, 0, err)
        d = json.loads(out)
        self.assertNotIn("sample_paths", d["mc"])
        self.assertEqual((d["plan"]["entry_bar"], d["mc"]["p_target"], d["mc"]["qty"]), (4, 0.0293, 45))

    def test_flags_and_errors(self):
        code, out, err = run(["run", "--symbol", "RELIANCE", "--date", "2026-10-29", "--entry", "1174.30",
                              "--stop", "1163.30", "--target", "1196.30", "--entry-time", "09:40",
                              "--offline", R60, "--now", "2026-10-29 09:45", "--replays", "500", "--qty", "45"])
        self.assertEqual(code, 0, err)
        self.assertIn("500 replays", out)
        code, _, err = run(["run", "--symbol", "RELIANCE", "--entry", "1"])
        self.assertIn("also give", err)
        code, _, err = run(["run"])
        self.assertIn("run /india-exit first", err)
        code, _, err = run(["run", "--side", "short"])
        self.assertIn("longs only", err)
        code, _, err = run(["chart", "--from"])
        self.assertIn("run timemachine.py run --write first", err)
        self.assertNotIn("Traceback", err)


def run_mod(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "india-timemachine", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: india-timemachine\n"))
        self.assertGreater(len(text.splitlines()), 60)
        low = " ".join(text.lower().split())
        for must in ("10,000", "seed 19", "not a prediction", "day trading loses most people money", "/india-budget",
                     "after charges", "15:20", "closing auction", "target first 2.9 %", "28.9 %", "-0.174",
                     "not financial advice", '"yes"', "never fatal"):
            self.assertIn(must, low)
        for never in ("\u2014", "nyse", "new york", "$3", "alpaca", " et ", "usd", "tick by tick", "real time"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()
