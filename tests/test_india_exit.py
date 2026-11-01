"""india-exit tests. Offline, on real fixtures:

common/yahoo-RELIANCE.NS-60d5m.json.gz  RELIANCE 5-minute bars (the ATR stop and the end-to-end example)
common/dhan-scrip-sample.csv            rows of Dhan's api-scrip-master.csv (ticks in paise)
india-exit/yahoo-IDEA.NS-1d5m.json      IDEA (about Rs 12, a 0.01 tick) 5-minute bars, 2026-10-29, fetched with
                                        curl, UA "Mozilla/5.0 (compatible; india-trader-skill/1.0)"

The worked example: RELIANCE 2026-10-29, entry 1,174.30 at 09:40 IST (bar 4), opening range
1,156.20 to 1,170.50: stop 1,163.30 (midpoint 1,163.35 rounded down to the 0.10 tick), risk 11.00,
2R target 1,196.30.
"""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, COMMON, ROOT

import nse

exitp = load("india-exit", "exit")
entry = load("india-entry", "entry")

R60 = os.path.join(COMMON, "yahoo-RELIANCE.NS-60d5m.json.gz")
IDEA = os.path.join(FIX, "india-exit", "yahoo-IDEA.NS-1d5m.json")
SCRIP = os.path.join(COMMON, "dhan-scrip-sample.csv")

PLAN = {"symbol": "RELIANCE", "exchange": "NSE", "side": "long", "date": "2026-10-29", "entry": 1174.3,
        "stop": 1163.3, "target": 1196.3, "tick": 0.1, "risk_per_share": 11.0, "r_multiple": 2.0,
        "stop_rule": "or-mid", "target_rule": "2R", "flat_by": "15:05",
        "opening_range": {"high": 1170.5, "low": 1156.2, "minutes": 15}, "made": "2026-10-29T09:41:00+05:30",
        "entry_time": "09:40", "entry_bar": 4, "setup": "orb", "interval": "5m", "reward_risk": 2.0}
ENTRY = {"created_by": "india-entry", "symbol": "RELIANCE", "exchange": "NSE", "setup": "orb", "side": "long",
         "date": "2026-10-29", "status": "triggered", "time": "09:40", "bar_start": "09:35", "bar_index": 4,
         "price": 1174.3, "level": 1170.5, "interval": "5m",
         "opening_range": {"high": 1170.5, "low": 1156.2, "minutes": 15}, "source": "yahoo",
         "checked_at": "2026-10-29T09:40:00+05:30"}


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def c(entry_px, stop, target="2R", oh=1170.5, ol=1156.2, atr=None, n=None, tick=None):
    sr, sp = exitp.parse_stop(stop)
    tr, tp = exitp.parse_target(target)
    return exitp.compute(entry_px, sr, sp, tr, tp, oh, ol, atr, n, tick)


class Math(unittest.TestCase):
    def test_the_example_or_mid_2r(self):
        p = c(1174.3, "or-mid")
        self.assertEqual((p["stop"], p["risk"], p["target"], p["reward_risk"], p["tick"]),
                         (1163.3, 11.0, 1196.3, 2.0, 0.1))
        self.assertIn("rounded down to the 0.1 tick", p["stop_detail"])
        self.assertEqual(p["target_label"], "2R (two times the risk)")
        self.assertEqual(c(1174.3, "or-mid", "1R")["target"], 1185.3)

    def test_raw_yahoo_floats_give_the_same_prices(self):
        p = c(1174.300048828125, "or-mid", "2R", 1170.5, 1156.199951171875)
        self.assertEqual((p["entry"], p["stop"], p["target"]), (1174.3, 1163.3, 1196.3))

    def test_other_stops(self):
        p = c(1174.3, "or-low")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (1156.2, 18.1, 1210.5))
        p = c(1174.3, "price:1163.38")
        self.assertEqual(p["stop"], 1163.3)                                   # rounds DOWN to the tick
        p = c(1174.3, "pct:1")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (1162.5, 11.8, 1197.9))   # 1162.557 -> 1162.5
        p = c(1174.3, "atr:1.5", atr=5.84, n=5)
        self.assertEqual((p["stop"], p["stop_label"]), (1165.5, "1.5 x ATR(5) under the entry"))

    def test_the_target_rounds_down_to_the_tick_too(self):
        p = c(1174.3, "price:1165.05", "1.5R")                                # stop 1165.0, risk 9.3
        self.assertEqual((p["stop"], p["risk"], p["target"]), (1165.0, 9.3, 1188.2))   # 1188.25 -> 1188.2
        self.assertIn("rounded down to the tick", p["target_label"])
        p = c(1174.3, "or-mid", "price:1190.07")
        self.assertEqual((p["target"], p["reward_risk"], p["target_label"]), (1190.0, 1.43, "your price (1.43R)"))

    def test_ticks_by_price_band(self):
        p = c(682.5, "or-mid", oh=682.45, ol=678.3)                           # HDFCBANK-like, 0.05 tick
        self.assertEqual((p["tick"], p["stop"], p["risk"], p["target"]), (0.05, 680.35, 2.15, 686.8))
        p = c(11.92, "or-mid", oh=11.90, ol=11.78)                            # IDEA, 0.01 tick
        self.assertEqual((p["tick"], p["stop"], p["risk"], p["target"]), (0.01, 11.84, 0.08, 12.08))
        p = c(1174.3, "or-mid", tick=0.05)                                    # an explicit tick wins
        self.assertEqual(p["stop"], 1163.35)

    def test_refusals_say_why(self):
        cases = [("price:1174.3", "2R", "at or above the entry"),
                 ("price:1180", "2R", "at or above the entry"),
                 ("or-mid", "price:1174", "at or below the entry"),
                 ("price:1174.0", "2R", "too tight to survive the spread"),
                 ("pct:0.04", "2R", "too tight to survive the spread")]
        for stop, target, why in cases:
            with self.assertRaises(exitp.PlanError) as cm:
                c(1174.3, stop, target)
            self.assertIn(why, str(cm.exception), stop)
        # 0.05 % of 1,174.30 is 0.587: a 0.60 risk is allowed, 0.50 is not
        self.assertEqual(c(1174.3, "price:1173.7")["risk"], 0.6)
        with self.assertRaises(exitp.PlanError):
            c(1174.3, "price:1173.8")
        # on a 0.01-tick stock the floor is 3 ticks
        self.assertEqual(c(11.92, "price:11.89", oh=None, ol=None)["risk"], 0.03)
        with self.assertRaises(exitp.PlanError) as cm:
            c(11.92, "price:11.90", oh=None, ol=None)
        self.assertIn("3 ticks of 0.01", str(cm.exception))
        with self.assertRaises(exitp.PlanError):
            c(1160.0, "or-mid")                                               # the midpoint is above this entry
        for bad in ("mid-ish", "pct:100", "price:0", "atr:-1"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_stop(bad)
        for bad in ("0R", "R", "lots", "price:x"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_target(bad)
        with self.assertRaises(exitp.PlanError):
            c(1174.3, "or-mid", oh=None, ol=None)
        with self.assertRaises(exitp.PlanError):
            c(1174.3, "or-mid", oh=1156.2, ol=1170.5)

    def test_charges_and_r_after_costs(self):
        ch = exitp.charges_view(1174.3, 1163.3, 1196.3, 45)
        self.assertEqual(ch["at_stop"], nse.charges(45 * 1174.3, 45 * 1163.3)["total"])
        self.assertEqual((ch["at_stop"], ch["at_target"]), (56.18, 56.75))
        self.assertEqual((ch["per_share_at_stop"], ch["per_share_at_target"]), (1.25, 1.26))
        self.assertEqual((ch["r_net_at_target"], ch["r_net_at_stop"], ch["reward_risk_net"]), (1.89, -1.11, 1.69))
        self.assertEqual(exitp.reference_qty({}, 11.0), (45, "max_loss_per_trade Rs 500.00 / risk 11"))
        self.assertEqual(exitp.reference_qty({"max_loss_per_trade": 1000}, 11.0)[0], 90)
        self.assertEqual(exitp.reference_qty({}, 0.08)[0], 6250)
        # a 0.01-tick stock: 6,250 IDEA shares for an 8-paise risk
        ch = exitp.charges_view(11.92, 11.84, 12.08, 6250)
        self.assertEqual((ch["r_net_at_target"], ch["r_net_at_stop"]), (1.85, -1.15))

    def test_atr_on_the_session_bars(self):
        ch = exitp.load_bars("RELIANCE", "5m", None, R60)
        day = exitp.datetime.date(2026, 10, 29)
        bars = exitp.session_bars(ch, day, "5m", upto_slot=4)
        self.assertEqual(len(bars), 5)                                        # 09:15 ... the 09:35 entry bar
        trs, prev = [], None
        for b in bars:
            r = b["h"] - b["l"]
            if prev is not None:
                r = max(r, abs(b["h"] - prev), abs(b["l"] - prev))
            trs.append(r)
            prev = b["c"]
        atr, n = exitp.atr_of(bars)
        self.assertEqual(n, 5)
        self.assertAlmostEqual(atr, sum(trs) / 5, places=9)
        self.assertAlmostEqual(atr, 5.84, places=2)
        self.assertEqual(exitp.atr_of(exitp.session_bars(ch, day, "5m"))[1], 14)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.env = os.environ.get("TRADER_DIR")
        os.environ["TRADER_DIR"] = os.path.join(self.d, "trader")
        os.makedirs(os.path.join(self.d, "trader"))

    def tearDown(self):
        if self.env is None:
            os.environ.pop("TRADER_DIR", None)
        else:
            os.environ["TRADER_DIR"] = self.env
        shutil.rmtree(self.d)

    def tpath(self, name):
        return os.path.join(self.d, "trader", name)

    def write(self, name, doc):
        with open(self.tpath(name), "w") as fh:
            json.dump(doc, fh)

    def plan(self, *args):
        return run(exitp, ["plan", "--now", "2026-10-29 09:41"] + list(args))

    def read_plan(self):
        with open(self.tpath("plan.json")) as fh:
            return json.load(fh)

    def test_entry_to_plan_end_to_end(self):
        code, _, _ = run(entry, ["check", "RELIANCE", "--offline", R60, "--now", "2026-10-29 09:40", "--write"])
        self.assertEqual(code, 0)
        code, out, err = self.plan("--write")
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        for k, v in PLAN.items():
            if k != "made":
                self.assertEqual(p[k], v, k)
        self.assertEqual(p["made"], "2026-10-29T09:41:00+05:30")
        self.assertEqual(p["charges"]["qty"], 45)
        for line in ("Entry         Rs 1,174.30 at 09:40 IST (bar 4)",
                     "Stop          Rs 1,163.30  opening-range midpoint",
                     "Target        Rs 1,196.30  2R (two times the risk)",
                     "Risk a share  Rs 11.00 (0.94% of the entry), tick 0.1",
                     "Reward:risk   2.0", "Rs 56.18 at the stop (Rs 1.25 a share)",
                     "After costs   target +1.89R, stop -1.11R, reward:risk 1.69",
                     "Flat by       15:05 IST", "not a guarantee", "/india-timemachine", "Nothing was placed"):
            self.assertIn(line, out)
        for never in ("$", "ET", "NYSE", "pro" + "fit"):
            self.assertNotIn(never, out)
        code, out, _ = self.plan("--target", "1R", "--write")
        self.assertEqual((self.read_plan()["target"], self.read_plan()["target_rule"]), (1185.3, "1R"))
        code, out, _ = run(exitp, ["show", "--now", "2026-10-29 10:00"])
        self.assertEqual(code, 0)
        self.assertIn("Rs 1,185.30", out)
        self.assertNotIn("not today", out)
        code, out, _ = run(exitp, ["show", "--now", "2026-10-30 10:00"])
        self.assertIn("not today", out)

    def test_manual_flags_json_and_the_dhan_tick(self):
        code, out, _ = self.plan("--symbol", "reliance", "--entry", "1174.30", "--or-high", "1170.50",
                                 "--or-low", "1156.20", "--time", "09:40", "--entry-bar", "4", "--date", "2026-10-29",
                                 "--scrip", SCRIP, "--json")
        self.assertEqual(code, 0)
        p = json.loads(out)
        for k in ("entry", "stop", "target", "risk_per_share", "tick", "entry_time", "entry_bar"):
            self.assertEqual(p[k], PLAN[k], k)
        self.assertEqual(p["tick_from"], "Dhan instrument file")
        self.assertEqual(p["warnings"], [])
        code, out, _ = self.plan("--symbol", "RELIANCE", "--entry", "1174.30", "--or-high", "1170.50",
                                 "--or-low", "1156.20", "--date", "2026-10-29", "--tick", "0.05", "--json")
        self.assertEqual(json.loads(out)["stop"], 1163.35)
        # the cache india-common writes is read when present (never downloaded here)
        os.makedirs(self.tpath("cache"))
        with open(SCRIP) as fh:
            nse.write_json(self.tpath(os.path.join("cache", "dhan-nse-equity.json")), nse.parse_dhan_scrip(fh.read()))
        code, out, _ = self.plan("--symbol", "IDEA", "--entry", "11.92", "--or-high", "11.90", "--or-low", "11.78",
                                 "--date", "2026-10-29", "--json")
        p = json.loads(out)
        self.assertEqual((p["tick"], p["tick_from"], p["stop"], p["target"]), (0.01, "Dhan instrument file", 11.84, 12.08))

    def test_atr_stop_reads_bars(self):
        self.write("entry.json", ENTRY)
        code, out, err = self.plan("--stop", "atr:1.5", "--target", "1R", "--offline", R60, "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(out)
        self.assertEqual((p["stop"], p["risk_per_share"], p["target"]), (1165.5, 8.8, 1183.1))
        self.assertEqual((p["stop_rule"], p["atr_bars"]), ("atr", 5))
        self.assertIn("under your min_reward_risk 1.5", p["warnings"][0])
        code, _, err = self.plan("--stop", "atr:1.5", "--offline", R60, "--entry-bar", "1")
        self.assertEqual(code, 1)
        self.assertIn("at least 3 closed", err)

    def test_idea_from_its_own_bars(self):
        code, _, _ = run(entry, ["check", "IDEA", "--offline", IDEA, "--now", "2026-10-29 12:00", "--write"])
        self.assertEqual(code, 0)
        with open(self.tpath("entry.json")) as fh:
            e = json.load(fh)
        self.assertEqual((e["time"], e["price"], e["opening_range"]), ("10:25", 11.92, {"high": 11.9, "low": 11.78,
                                                                                         "minutes": 15}))
        code, out, err = self.plan("--json")
        p = json.loads(out)
        self.assertEqual((p["tick"], p["stop"], p["target"], p["risk_per_share"]), (0.01, 11.84, 12.08, 0.08))
        self.assertEqual(p["charges"]["qty"], 6250)

    def test_refusals_from_the_cli(self):
        self.write("entry.json", dict(ENTRY, status="waiting", time=None, price=None, bar_index=None, bar_start=None))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("RELIANCE is waiting, not triggered", err)
        code, out, _ = self.plan("--entry", "1170.60", "--json")             # plan ahead at the level
        self.assertEqual(code, 0)
        p = json.loads(out)
        self.assertEqual((p["entry"], p["stop"], p["target"], p["entry_time"]), (1170.6, 1163.3, 1185.2, None))
        self.write("entry.json", dict(ENTRY, setup="level", level=1170.0))
        code, _, err = self.plan()
        self.assertIn("level setup has no opening-range stop", err)
        self.assertEqual(self.plan("--stop", "pct:1")[0], 0)
        code, _, err = self.plan("--side", "short")
        self.assertIn("longs only", err)
        self.write("entry.json", dict(ENTRY, side="short"))
        code, _, err = self.plan()
        self.assertIn("longs only", err)
        os.remove(self.tpath("entry.json"))
        code, _, err = self.plan()
        self.assertIn("no entry", err)
        code, _, err = self.plan("--symbol", "RELIANCE")
        self.assertIn("needs --entry", err)
        code, _, err = self.plan("--symbol", "RELIANCE", "--from", "x.json", "--entry", "1")
        self.assertIn("not both", err)
        code, _, err = self.plan("--symbol", "RELIANCE", "--entry", "1174.3", "--or-high", "1170.5")
        self.assertIn("both --or-high and --or-low", err)
        self.assertNotIn("Traceback", err)

    def test_flat_by_caps_and_late_entries(self):
        self.write("entry.json", ENTRY)
        self.write("config.json", {"flat_by": "15:25", "min_reward_risk": 2.5})
        code, out, _ = self.plan("--json")
        p = json.loads(out)
        self.assertEqual(p["flat_by"], "15:10")
        self.assertIn("Dhan squares off", p["warnings"][0])
        self.assertIn("under your min_reward_risk 2.5", p["warnings"][1])
        os.remove(self.tpath("config.json"))
        code, out, _ = self.plan("--flat-by", "14:45", "--json")
        self.assertEqual(json.loads(out)["flat_by"], "14:45")
        self.write("entry.json", dict(ENTRY, time="15:05"))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("no time left to hold it", err)
        # Muhurat: the flat-by sits inside the evening session, not at 15:05
        self.write("entry.json", dict(ENTRY, date="2026-11-08", time="18:25"))
        self.write("config.json", {"special_sessions": {"2026-11-08": ["18:00", "19:00"]}})
        code, out, err = run(exitp, ["plan", "--now", "2026-11-08 18:26", "--json"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["flat_by"], "18:55")

    def test_show_without_a_plan(self):
        code, out, _ = run(exitp, ["show"])
        self.assertEqual(code, 1)
        self.assertIn("No plan", out)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "india-exit", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: india-exit\n"))
        self.assertGreater(len(text.splitlines()), 60)
        low = " ".join(text.lower().split())
        for must in ("before the entry", "a stop is a plan, not a guarantee", "day trading loses most people money",
                     "/india-timemachine", "/india-budget", "longs only", "rs 1,163.30", "rs 1,196.30", "rs 11.00",
                     "too tight to survive the spread", "rounded down", "15:10", "not financial advice", '"yes"',
                     "+1.89r", "-1.11r"):
            self.assertIn(must, low)
        for never in ("\u2014", "nyse", "new york", "$3", "alpaca", " et ", "usd"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()
