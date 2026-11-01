"""india-research tests. Offline, on the pack's real fixtures:

common/yahoo-RELIANCE.NS-60d5m.json.gz, india-timemachine/yahoo-HDFCBANK.NS-60d5m.json.gz and
india-backtest/yahoo-<SYM>.NS-60d5m.json.gz / -1y1d.json.gz: 12 Nifty 50 stocks, 60 sessions of
5-minute bars to 2026-10-29 plus a year of daily bars (Yahoo, recorded by india-backtest's builder).
common/n500.csv: NSE's Nifty 500 list (Industry column). common/*.xml, bulk.csv, block.csv and
india-insiders/InsiderTrading.xml: NSE RSS feeds and deal files of 2026-10-29.
common/yahoo-RELIANCE.NS-1d1m.json: RELIANCE 1-minute bars of 2026-10-29.

Hand-checked short (short_orb_breakdown default: 15-min range, stop at the midpoint, 2R),
RELIANCE 2026-08-04: range high 1313.70 / low 1306.00 (09:15-09:25 bars), mid 1309.85 -> stop
rounds UP to 1309.90 (tick 0.10). The 09:30 bar closes 1305.20 < 1306.00 (first close under the
low; bar ends 09:35) -> sell, fill 1305.20 - 0.10 = 1305.10. Target 1305.20 - 2 x 4.65 = 1295.90.
No later high reaches 1309.90 (max 1307.60); the 11:55 bar's low 1295.50 hits the target (exit
time 12:00). Size 19 (25 % of Rs 1,00,000 / 1305.10). Gross (1305.10 - 1295.90) x 19 = 174.80.
Charges: sell 24,796.90 (STT round(6.20) = 6), buy 24,622.10 (stamp round(0.74) = 1), brokerage
7.44 + 7.39, NSE txn 1.52, SEBI 0.05, GST 2.95 -> 26.35. Net 148.45; R 9.20 / 4.80 = 1.9167;
net R 148.45 / 91.20 = 1.6277.
"""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, COMMON, ROOT

import nse

rs = load("india-research", "research")
import lab_engine as le  # noqa: E402  (on sys.path once research.py is loaded)
import stats as st  # noqa: E402
import peers as pr  # noqa: E402
import events as ev  # noqa: E402
import backtest as bt  # noqa: E402

DIRS = [COMMON, os.path.join(FIX, "india-timemachine"), os.path.join(FIX, "india-backtest")]
SYMS = ["RELIANCE", "HDFCBANK", "TCS", "INFY", "ICICIBANK", "SBIN", "TATAPOWER", "TATASTEEL", "ITC",
        "AXISBANK", "BHARTIARTL", "LT"]
N500 = os.path.join(COMMON, "n500.csv")
LAB = os.path.join(ROOT, "india-research", "lab")
NOW = "2026-10-29 18:00"
WIN = bt.Window({})


def jload(path):
    with open(path) as fh:
        return json.load(fh)


def sectors():
    return pr.load_sectors(N500)[0]


class TempTrader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get("TRADER_DIR")
        os.environ["TRADER_DIR"] = self.tmp

    def tearDown(self):
        if self.old is None:
            os.environ.pop("TRADER_DIR", None)
        else:
            os.environ["TRADER_DIR"] = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = rs.main(["--trader-dir", self.tmp, "--now", NOW, "--sectors", N500] + list(argv))
        return code, out.getvalue(), err.getvalue()

    def log(self, exp_id, example, *extra):
        return self.cli("log", "--id", exp_id, "--from-example", example, "--bars", ",".join(DIRS),
                        "--universe", ",".join(SYMS), *extra)

    def dataset(self, split, ticket=None):
        return le.load_dataset("fixture:12:5m", SYMS, DIRS, split, ticket=ticket, sectors=sectors(),
                               now=datetime.datetime(2026, 10, 29, 18, 0))


def lab(name):
    return le.load_strategy(os.path.join(LAB, name + ".py"))


def write_lab(folder, name, body):
    path = os.path.join(folder, name + ".py")
    with open(path, "w") as fh:
        fh.write(body)
    return le.load_strategy(path, name)


# ---------------------------------------------------------------- registry

def _hammer(n):
    for i in range(n):
        rs.append({"op": "note", "id": "p%d-%d" % (os.getpid(), i)}, nse.parse_now(NOW))


class Registry(TempTrader):
    def test_parallel_writers_keep_the_hash_chain(self):
        # several researchers run at once; read-last-line + append must be one step
        import multiprocessing
        ctx = multiprocessing.get_context("fork")
        procs = [ctx.Process(target=_hammer, args=(25,)) for _ in range(8)]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
        recs, problem = rs.read_registry(strict=False)
        self.assertIsNone(problem)
        self.assertEqual(len(recs), 200)

    def test_peers_has_no_holdout_bypass(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            rs.main(["--trader-dir", self.tmp, "peers", "TATAPOWER", "--include-holdout"])

    def test_log_needs_a_mechanism_and_a_new_id(self):
        code, _, err = self.cli("log", "--id", "x1", "--family", "f", "--rationale", "it backtested well",
                                "--rules", "buy the open, sell the close of the day", "--universe", "RELIANCE")
        self.assertEqual(code, 1)
        self.assertIn("mechanism", err)
        self.assertEqual(self.log("v1", "vwap_reversion")[0], 0)
        code, _, err = self.log("v1", "vwap_reversion")
        self.assertEqual(code, 1)
        self.assertIn("new id", err)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "research", "lab", "v1.py")))

    def test_every_combo_counts_once_per_code_and_grid(self):
        self.log("s1", "short_orb_breakdown")
        self.assertEqual(self.cli("explore", "s1")[0], 0)
        recs, _ = rs.read_registry()
        self.assertEqual(rs.total_trials(recs), 8)                 # 2 x 2 x 2 grid
        self.cli("explore", "s1")                                  # same code, same grid: not recounted
        recs, _ = rs.read_registry()
        self.assertEqual(rs.total_trials(recs), 8)
        self.assertEqual(len([r for r in recs if r["op"] == "explore"]), 2)
        path = os.path.join(self.tmp, "research", "lab", "s1.py")
        with open(path) as fh:
            src = fh.read()
        with open(path, "w") as fh:                               # a smaller grid on changed code: counted
            fh.write(src.replace('"r": [1.5, 2.0]}', '"r": [2.0]}'))
        self.cli("explore", "s1")
        recs, _ = rs.read_registry()
        self.assertEqual(rs.total_trials(recs), 12)
        self.log("v1", "vwap_reversion")
        self.cli("explore", "v1")
        recs, _ = rs.read_registry()
        self.assertEqual(rs.total_trials(recs), 15)
        self.assertEqual(len(rs.trial_srs(recs)), 15)

    def test_append_only_hash_chain(self):
        self.log("s1", "short_orb_breakdown")
        self.log("v1", "vwap_reversion")
        self.assertEqual(self.cli("verify")[0], 0)
        path = rs.reg_path()
        with open(path) as fh:
            lines = fh.read().splitlines()
        lines[0] = lines[0].replace("short-breakout", "something-else")
        with open(path, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        code, out, _ = self.cli("verify")
        self.assertEqual(code, 1)
        self.assertIn("BROKEN", out)
        code, _, err = self.log("z1", "sector_lead_lag")
        self.assertEqual(code, 1)
        self.assertIn("append-only", err)
        with open(path) as fh:                                    # nothing was appended
            self.assertEqual(len(fh.read().splitlines()), 2)

    def test_deleting_a_line_breaks_the_chain(self):
        self.log("s1", "short_orb_breakdown")
        self.log("v1", "vwap_reversion")
        self.log("l1", "sector_lead_lag")
        with open(rs.reg_path()) as fh:
            lines = fh.read().splitlines()
        with open(rs.reg_path(), "w") as fh:
            fh.write("\n".join([lines[0], lines[2]]) + "\n")
        self.assertIsNotNone(rs.read_registry(strict=False)[1])

    def test_status_flow_and_lab_freeze(self):
        self.log("s1", "short_orb_breakdown")
        code, out, err = self.cli("pipeline", "s1")
        self.assertEqual(code, 0, err)
        self.assertIn("Gates for s1 on validate", out)
        self.assertIn("REJECTED", out)
        _, e = rs.get_exp("s1")
        self.assertEqual(e["status"], "rejected")
        self.assertEqual([h[0] for h in e["history"]], ["idea", "tested", "rejected"])
        code, _, err = self.cli("explore", "s1")
        self.assertEqual(code, 1)
        self.assertIn("new id", err)
        with open(os.path.join(self.tmp, "research", "lab", "s1.py"), "a") as fh:
            fh.write("\n# tweak after seeing validate\n")
        code, _, err = self.cli("gate", "s1")
        self.assertEqual(code, 1)
        self.assertIn("changed after validate", err)
        code, _, err = self.cli("promote", "s1")
        self.assertEqual(code, 1)


# ---------------------------------------------------------------- splits and the holdout lock

class Holdout(TempTrader):
    def test_splits_are_60_20_20_and_locked(self):
        ds = self.dataset("explore")
        sp = ds.split_rec
        self.assertEqual(sp["sessions"], [36, 12, 12])
        self.assertEqual((sp["explore"], sp["validate"], sp["holdout"]),
                         (["2026-08-04", "2026-09-23"], ["2026-09-24", "2026-10-12"], ["2026-10-13", "2026-10-29"]))
        before = jload(le.splits_path())
        le.save_split("fixture:12:5m", le.make_split(["2026-01-%02d" % d for d in range(1, 31)], SYMS))
        self.assertEqual(jload(le.splits_path()), before)   # an existing split never changes

    def test_explore_and_validate_loaders_cannot_see_holdout_bars(self):
        ex = self.dataset("explore")
        self.assertLess(ex.max_bar_date(), "2026-09-24")
        self.assertEqual((ex.dates[0], ex.dates[-1]), ("2026-08-04", "2026-09-23"))
        for ss in ex.sessions.values():
            for s in ss:
                self.assertLess(s.date, "2026-09-24")
        for rows in ex.daily.values():                              # the daily bars are cut too
            self.assertTrue(all(d < "2026-09-24" for d, _ in rows))
        va = self.dataset("validate")
        self.assertLess(va.max_bar_date(), "2026-10-13")
        self.assertEqual(va.dates[-1], "2026-10-12")
        with self.assertRaises(le.HoldoutError):
            self.dataset("holdout")
        with self.assertRaises(le.HoldoutError):
            le.HoldoutTicket("nobody", os.path.join(self.tmp, "research", "experiments.jsonl"))

    def test_another_datasets_holdout_is_locked_for_shared_symbols(self):
        self.dataset("explore")
        other = le.load_dataset("other:RELIANCE:5m", ["RELIANCE"], DIRS, "explore")
        # the other dataset has the same dates, so its explore window must lose the locked holdout dates
        self.assertLess(other.max_bar_date(), "2026-10-13")
        self.assertIn("locked", " ".join(other.notes))

    def test_holdout_runs_once(self):
        self.log("v1", "vwap_reversion")
        self.cli("explore", "v1")
        self.cli("validate", "v1")
        code, _, err = self.cli("holdout", "v1")
        self.assertEqual(code, 1)
        self.assertIn("only a candidate", err)
        rs.set_status("v1", "candidate", "test: pretend every gate passed", datetime.datetime(2026, 10, 29, 18, 1))
        code, out, err = self.cli("holdout", "v1")
        self.assertEqual(code, 0, err)
        self.assertIn("Gates for v1 on HOLDOUT (2026-10-13 to 2026-10-29", out)
        self.assertIn("Recorded permanently", out)
        code, _, err = self.cli("holdout", "v1")
        self.assertEqual(code, 1)
        self.assertIn("already used its holdout", err)
        recs, _ = rs.read_registry()
        self.assertEqual([r["op"] for r in recs if r["op"].startswith("holdout")], ["holdout_claim", "holdout"])
        _, e = rs.get_exp("v1")
        self.assertIn(e["status"], ("holdout-passed", "holdout-failed"))
        self.assertEqual(self.cli("validate", "v1")[0], 1)          # validate is closed after the holdout
        run = jload(rs.runs_path("v1", "holdout"))
        self.assertTrue(all("2026-10-13" <= t["date"] <= "2026-10-29" for t in run["trades"]))


# ---------------------------------------------------------------- statistics

class Statistics(unittest.TestCase):
    def test_dsr_bailey_lopez_de_prado_example(self):
        # The paper's numerical example: annualised SR 2.5 over 5 years of daily returns (T=1250), N=100
        # trials whose annualised SRs have variance 1/2, skew -3, kurtosis 10 -> SR0 0.1132, DSR 0.9004.
        sr = 2.5 / 250 ** 0.5
        p, sr0 = st.dsr(sr, 1250, -3.0, 10.0, 100, 0.5 / 250)
        self.assertAlmostEqual(sr0, 0.1132, places=4)
        self.assertAlmostEqual(p, 0.9004, places=4)
        self.assertEqual(st.expected_max_sr(1, 0.01), 0.0)
        self.assertLess(st.dsr(sr, 1250, -3.0, 10.0, 1000, 0.5 / 250)[0], p)   # more trials, harder

    def test_moments(self):
        m, sd, sk, ku = st.moments([1.0, 2.0, 3.0, 4.0, 10.0])
        self.assertAlmostEqual(m, 4.0)
        self.assertAlmostEqual(sd, 3.5355, places=4)
        self.assertGreater(sk, 0)
        self.assertAlmostEqual(st.moments([1.0, -1.0] * 50)[3], 1.0)

    def trades(self):
        ds = [("2026-09-%02d" % d) for d in range(1, 21)]
        rows = []
        for i, d in enumerate(ds):
            for j in range(5):
                r = ((i * 7 + j * 3) % 11 - 4) / 4.0
                rows.append({"date": d, "R_net": r, "pnl_net": r * 100, "symbol": "S%d" % j})
        return rows

    def test_bootstrap_is_deterministic_with_a_seed(self):
        tr = self.trades()
        a = st.bootstrap_ci(tr, "R_net")
        self.assertEqual(a, st.bootstrap_ci(tr, "R_net"))
        self.assertNotEqual(a, st.bootstrap_ci(tr, "R_net", seed=1))
        mean = sum(t["R_net"] for t in tr) / len(tr)
        self.assertLess(a[0], mean)
        self.assertGreater(a[1], mean)
        self.assertEqual(st.bootstrap_ci([], "R_net"), (None, None))

    def test_gates_on_a_clear_loser_and_a_clear_winner(self):
        dates = ["2026-09-%02d" % d for d in range(1, 21)]
        lose = [{"date": d, "R_net": -0.2 + 0.01 * (i % 3), "pnl_net": -20.0, "symbol": "S%d" % (i % 4)}
                for i, d in enumerate(dates * 6)]
        g = dict(st.gates(lose, dates, lose, {}, 10, kind="intraday", look={"decided_after_entry": 0,
                                                                           "loader_ok": True, "violations": 0}))
        self.assertTrue(g["min_trades"]["pass"])
        self.assertFalse(g["bootstrap_ci"]["pass"])
        self.assertFalse(g["cost_stress"]["pass"])
        self.assertFalse(g["benchmark"]["pass"])
        self.assertTrue(g["no_look_ahead"]["pass"])
        win = [{"date": d, "R_net": 0.5 + 0.1 * (i % 5), "pnl_net": 60.0 + i % 7, "symbol": "S%d" % (i % 4)}
               for i, d in enumerate(dates * 6)]
        bench = {"universe_ew": dict((d, 0.001 * ((i % 5) - 2)) for i, d in enumerate(dates))}
        g = dict(st.gates(win, dates, win, bench, 10, kind="intraday", look={"decided_after_entry": 0,
                                                                           "loader_ok": True, "violations": 0}))
        self.assertEqual([k for k, v in g.items() if not v["pass"]], [])          # a real edge passes every gate
        self.assertEqual(g["breadth"]["value"]["months"], 1)
        g = dict(st.gates(win, dates, win, bench, 10, kind="intraday", look={"decided_after_entry": 1,
                                                                           "loader_ok": True, "violations": 0}))
        self.assertFalse(g["no_look_ahead"]["pass"])


# ---------------------------------------------------------------- the engine: shorts, charges, longs

class Engine(TempTrader):
    def setUp(self):
        super().setUp()
        b5, b1, _ = bt.load_offline(DIRS, "RELIANCE")
        self.ss = bt.build_sessions("RELIANCE", b5, b1, None)[0]
        self.ds = le.Dataset("t", "explore", {"RELIANCE": self.ss}, "2099-01-01", ("0", "9"), {}, [])

    def session(self, date):
        return [s for s in self.ss if s.date == date][0]

    def test_short_orb_breakdown_reliance_2026_08_04_by_hand(self):
        tr, info = le.run_strategy(self.ds, lab("short_orb_breakdown"), {"or_minutes": 15, "stop": "or_mid", "r": 2.0},
                                   {}, ["2026-08-04"])
        self.assertEqual(len(tr), 1)
        t = tr[0]
        want = {"side": "short", "time": "09:35", "signal": 1305.2, "entry": 1305.1, "stop": 1309.9, "target": 1295.9,
                "risk": 4.8, "qty": 19, "sized_by": "position_cap_pct", "exit": 1295.9, "exit_time": "12:00",
                "outcome": "target", "pnl": 174.8, "charges": 26.35, "stt": 6.0, "stamp": 1.0, "pnl_net": 148.45,
                "R": 1.9167, "R_net": 1.6277, "seen_until": "09:35"}
        self.assertEqual(dict((k, t[k]) for k in want), want)
        self.assertEqual(info["violations"], 0)

    def test_short_charges_stt_on_the_sell_stamp_on_the_buy(self):
        qty, sell, buy = 19, 1305.1, 1295.9
        ch = nse.charges(qty * buy, qty * sell)
        self.assertEqual(ch["stt"], round(qty * sell * 0.025 / 100))
        self.assertEqual(ch["stamp"], round(qty * buy * 0.003 / 100))
        self.assertEqual(ch["total"], 26.35)
        # a big short: STT follows the SELL value even though the sell came first
        big = nse.charges(1000 * 990.0, 1000 * 1000.0)
        self.assertEqual(big["stt"], 250.0)                        # 0.025 % of the 10,00,000 sell
        self.assertEqual(big["stamp"], 30.0)                       # 0.003 % of the 9,90,000 buy

    def test_short_stop_hit_gap_and_both(self):
        s = bt.Session("TEST", "2026-10-01", [{"o": 500.0, "h": 500.5, "l": 499.5, "c": 500.0, "v": 1000}] * 75,
                       None, 0.05)
        bars = list(s.bars)
        bars[10] = {"o": 500.0, "h": 506.2, "l": 499.0, "c": 505.0, "v": 1}      # touches the stop
        s.bars = bars
        t, _ = le.simulate(s, 5, "short", 506.0, 490.0, WIN, {}, 1)
        self.assertEqual((t["outcome"], t["exit"], t["exit_time"]), ("stop", 506.05, "10:10"))
        bars[10] = {"o": 507.0, "h": 508.0, "l": 489.0, "c": 505.0, "v": 1}      # gaps over it
        t, _ = le.simulate(s, 5, "short", 506.0, 490.0, WIN, {}, 1)
        self.assertEqual((t["outcome"], t["exit"]), ("stop_gap", 507.05))
        bars[10] = {"o": 500.0, "h": 506.5, "l": 489.0, "c": 495.0, "v": 1}      # both: the stop
        t, _ = le.simulate(s, 5, "short", 506.0, 490.0, WIN, {}, 1)
        self.assertEqual(t["outcome"], "stop")
        bars[10] = {"o": 500.0, "h": 500.5, "l": 489.9, "c": 495.0, "v": 1}      # target, exact
        t, _ = le.simulate(s, 5, "short", 506.03, 489.97, WIN, {}, 1)
        self.assertEqual((t["stop"], t["target"]), (506.05, 490.0))               # both round UP
        self.assertEqual((t["outcome"], t["exit"], t["entry"]), ("target", 490.0, 499.95))
        bars[10] = {"o": 500.0, "h": 500.5, "l": 499.5, "c": 500.0, "v": 1}
        t, _ = le.simulate(s, 5, "short", 506.0, 490.0, WIN, {}, 1)                # flat by 15:05
        self.assertEqual((t["outcome"], t["exit"], t["exit_time"]), ("flat", 500.05, "15:05"))
        self.assertEqual(le.simulate(s, 5, "short", 499.0, 490.0, WIN, {}, 1)[1], "stop not above the entry")
        self.assertEqual(le.simulate(s, 5, "short", 500.1, 490.0, WIN, {}, 1)[1],
                         "stop too tight (under 3 ticks or 0.05 %)")

    def test_short_sizing_counts_charges_at_the_stop(self):
        q, by = le.size_short(1000.0, 1010.0, {"max_loss_per_trade": 500, "position_cap_pct": 0,
                                               "max_order_value": 0, "capital": 1e9})
        self.assertEqual(by, "max_loss_per_trade")
        loss = q * 10.0 + nse.charges(q * 1010.0, q * 1000.0)["total"]
        self.assertLessEqual(loss, 500)
        self.assertGreater((q + 1) * 10.0 + nse.charges((q + 1) * 1010.0, (q + 1) * 1000.0)["total"], 500)

    def test_long_side_is_backtest_py_exactly(self):
        n = 0
        for s in self.ss:
            sig = bt.strat_orb(s, bt.STRATEGIES["orb15"]["default"], WIN)
            if not isinstance(sig, dict):
                continue
            a, why_a = bt.simulate(s, sig, WIN, {}, 1)
            price = round(sig["price"], 2)
            stop = nse.round_down(sig["stop"], nse.band_tick(price))
            b, why_b = le.simulate(s, sig["k"], "long", sig["stop"], price + sig["r"] * (price - stop), WIN, {}, 1,
                                   why=sig["why"])
            self.assertEqual(why_a, why_b)
            if a:
                n += 1
                for k in a:
                    self.assertEqual(a[k], b[k], (s.date, k))
        self.assertGreater(n, 20)


# ---------------------------------------------------------------- no look-ahead

PEEK = '''
META = {"default": {}, "grid": {}}
def signals(day, ctx):
    k = day.k
    try:
        future = day.bars[k + 3]          # three bars that have not closed yet
    except Exception:
        return []                         # caught: the engine must still trip
    return []
'''

BACKDATE = '''
META = {"default": {}, "grid": {}}
def signals(day, ctx):
    # buys "the day's low so far" once it is clearly the low, dated back to the low bar
    rows = [(b["l"], i) for i, b in enumerate(day.bars) if b is not None]
    low, i = min(rows)
    if day.k - i >= 6 and i >= 3:
        from lab_engine import end_time
        b = day.bars[i]
        return [{"side": "long", "entry_time": end_time(i), "entry": b["c"], "stop": b["l"] - 2, "target": b["c"] + 4}]
    return []
'''

FUTURE = '''
META = {"default": {}, "grid": {}}
def signals(day, ctx):
    from lab_engine import end_time
    b = day.bars[-1]
    return [{"side": "long", "entry_time": end_time(day.k + 1), "entry": b["c"], "stop": b["c"] - 3, "target": b["c"] + 6}]
'''

PEER_PEEK = '''
META = {"default": {}, "grid": {}}
def signals(day, ctx):
    for p in ctx.peers.values():
        if len(p.bars) != day.k + 1:
            raise AssertionError("peer cut differs")
        p.bars[day.k + 1]
    return []
'''

BAD_ENTRY = '''
META = {"default": {}, "grid": {}}
def signals(day, ctx):
    b = day.bars[-1]
    return [{"side": "long", "entry_time": day.time, "entry": b["l"], "stop": b["l"] - 3, "target": b["c"] + 6}]
'''


class LookAhead(TempTrader):
    def setUp(self):
        super().setUp()
        self.ds = self.dataset("explore")
        self.labdir = os.path.join(self.tmp, "lab")
        os.makedirs(self.labdir)

    def test_reading_an_unclosed_bar_trips_even_if_caught(self):
        with self.assertRaises(le.LookAheadError) as cm:
            le.run_strategy(self.ds, write_lab(self.labdir, "peek", PEEK), {}, {}, self.ds.dates[:1])
        self.assertIn("read bar 5 (09:40-09:45) but only bars up to 09:30 have closed", str(cm.exception))
        self.assertIn("the strategy caught the error, the engine did not", str(cm.exception))

    def test_a_back_dated_decision_trips(self):
        with self.assertRaises(le.LookAheadError) as cm:
            le.run_strategy(self.ds, write_lab(self.labdir, "backdate", BACKDATE), {}, {}, self.ds.dates[:5])
        self.assertIn("back-dated", str(cm.exception))

    def test_an_entry_after_the_last_closed_bar_trips(self):
        with self.assertRaises(le.LookAheadError):
            le.run_strategy(self.ds, write_lab(self.labdir, "future", FUTURE), {}, {}, self.ds.dates[:1])

    def test_peers_are_cut_at_the_same_bar(self):
        with self.assertRaises(le.LookAheadError):
            le.run_strategy(self.ds, write_lab(self.labdir, "peerpeek", PEER_PEEK), {}, {}, self.ds.dates[:1],
                            ["HDFCBANK"])

    def test_entry_must_be_the_close(self):
        with self.assertRaises(le.StrategyError):
            le.run_strategy(self.ds, write_lab(self.labdir, "badentry", BAD_ENTRY), {}, {}, self.ds.dates[:1])

    def test_prefix_view(self):
        trip = []
        p = le.Prefix(list(range(10)), 4, trip, "X")
        self.assertEqual((len(p), p[-1], p[0], p[1:100], list(p)), (4, 3, 0, [1, 2, 3], [0, 1, 2, 3]))
        with self.assertRaises(le.LookAheadError):
            p[4]
        self.assertTrue(trip)
        self.assertEqual((le.slot_of("09:35"), le.slot_of("09:37"), le.end_time(3)), (3, None, "09:35"))

    def test_bars_are_read_only(self):
        s = self.ds.sessions["RELIANCE"][0]
        f = self.ds.frozen(s)
        with self.assertRaises(TypeError):
            f[0]["c"] = 1.0


# ---------------------------------------------------------------- peers

class Peers(unittest.TestCase):
    def test_sector_groups_from_the_nifty_500_list(self):
        s2i, groups = pr.load_sectors(N500)
        self.assertGreater(len(s2i), 450)
        self.assertEqual(s2i["TATAPOWER"], "Power")
        self.assertIn("NTPC", groups["Power"])
        self.assertIn("POWERGRID", groups["Power"])
        self.assertEqual(s2i["HDFCBANK"], s2i["ICICIBANK"])

    def test_correlations_on_cached_daily_bars(self):
        r = pr.correlated("HDFCBANK", DIRS, ["ICICIBANK", "AXISBANK", "SBIN", "TCS", "INFY"])
        self.assertEqual(len(r), 5)
        self.assertTrue(all(-1 <= c <= 1 and n >= 200 for _, c, n in r))
        tcs = dict((s, c) for s, c, _ in pr.correlated("TCS", DIRS, ["INFY", "ITC"]))
        self.assertGreater(tcs["INFY"], tcs["ITC"])                # two IT majors move together

    def test_holdout_dates_are_left_out(self):
        full = pr.daily_closes(DIRS, "TCS")
        cut = pr.daily_closes(DIRS, "TCS", [("2026-10-13", "2026-10-29")])
        self.assertTrue(any("2026-10-13" <= d for d in full))
        self.assertFalse(any("2026-10-13" <= d <= "2026-10-29" for d in cut))

    def test_report_lines(self):
        s2i, groups = pr.load_sectors(N500)
        lines = pr.report("TATAPOWER", s2i, groups, DIRS)
        self.assertIn("sector 'Power'", lines[0])
        self.assertTrue(any(l.strip().startswith("NTPC") for l in lines))


# ---------------------------------------------------------------- events

class Events(TempTrader):
    OFF = [COMMON, os.path.join(FIX, "india-insiders")]

    def record(self, now, prices=None):
        equity = nse.parse_equity_list(nse.read_maybe_gz(os.path.join(COMMON, "EQUITY_L.csv")).decode())
        return ev.record(now, ev.Getter(self.OFF), ev.news.Resolver(equity), set(sectors()),
                         prices=prices or ev.OfflinePrices([COMMON]), cfg={}, max_symbols=0)

    def lines(self):
        with open(os.path.join(self.tmp, "research", "events", "2026-10.jsonl")) as fh:
            return fh.read().splitlines()

    def test_idempotent_on_the_rss_fixtures(self):
        now = datetime.datetime(2026, 10, 29, 15, 40)
        a = self.record(now)
        self.assertGreater(a["new"], 50)
        n = len(self.lines())
        b = self.record(now)
        self.assertEqual((b["new"], b["filled"]), (0, 0))
        self.assertEqual(len(self.lines()), n)
        known, _ = ev.read_all()
        cats = set(e["category"] for e in known.values())
        self.assertTrue({"announcement", "board", "action", "insider"} <= cats)
        self.assertEqual(len(known), a["new"])

    def test_snapshots_from_one_minute_bars(self):
        # an announcement seen at 10:02 IST on 2026-10-29: detect at 10:02, +5m 10:07, +15m 10:17, +60m 11:02
        known = {"x": {"id": "x", "symbol": "RELIANCE", "category": "announcement",
                       "detected": "2026-10-29T10:02:00+05:30", "snaps": {}}}
        m1 = ev.OfflinePrices([COMMON])("RELIANCE", "1m")
        f, missed, pending, fetched = ev.fill(known, datetime.datetime(2026, 10, 29, 12, 0),
                                              lambda s, i: m1 if i == "1m" else [], max_symbols=0)
        sn = known["x"]["snaps"]
        self.assertEqual(sorted(sn), ["15m", "5m", "60m", "detect"])
        want = {}
        for key, t in (("detect", "10:02"), ("5m", "10:07"), ("15m", "10:17"), ("60m", "11:02")):
            at = datetime.datetime(2026, 10, 29, int(t[:2]), int(t[3:]))
            last = max((b for b in m1 if b["t"] + datetime.timedelta(minutes=1) <= at), key=lambda b: b["t"])
            want[key] = round(last["c"], 2)
            self.assertEqual(sn[key]["bar"], (at - datetime.timedelta(minutes=1)).isoformat())
        self.assertEqual(dict((k, sn[k]["price"]) for k in want), want)
        self.assertEqual(f, 4)

    def test_after_hours_events_start_at_the_next_open(self):
        e = {"detected": "2026-10-29T20:15:00+05:30"}
        tg = ev.targets(e)
        self.assertEqual(tg["5m"][2], datetime.datetime(2026, 10, 30, 9, 20))
        self.assertEqual(tg["close"][2], datetime.date(2026, 10, 30))
        self.assertEqual(tg["next_close"][2], datetime.date(2026, 11, 2))          # weekend skipped
        late = ev.targets({"detected": "2026-10-29T15:00:00+05:30"})
        self.assertEqual(late["60m"][0], "na")                                     # after the close
        self.assertEqual(ev.lag_minutes("2026-10-29T08:55:40+05:30", datetime.datetime(2026, 10, 29, 9, 0)), 4)

    def test_lock_blocks_a_second_writer(self):
        with ev.Lock():
            with self.assertRaises(ev.EventError):
                with ev.Lock():
                    pass

    def test_cli_offline(self):
        code, out, err = self.cli("events", "record", "--offline", ",".join(self.OFF))
        self.assertEqual(code, 0, err)
        self.assertIn("new", out)
        code, out, _ = self.cli("events", "stats")
        self.assertIn("testable at 500", out)


# ---------------------------------------------------------------- the example strategies

class Examples(TempTrader):
    def setUp(self):
        super().setUp()
        self.ds = self.dataset("explore")

    def test_short_orb_breakdown(self):
        tr, info = le.run_strategy(self.ds, lab("short_orb_breakdown"), lab("short_orb_breakdown").default(), {})
        self.assertGreater(len(tr), 100)
        self.assertEqual(set(t["side"] for t in tr), {"short"})
        self.assertTrue(all(t["stop"] > t["entry"] > t["target"] for t in tr))
        self.assertTrue(all(t["time"] >= "09:35" and t["time"] <= "14:30" for t in tr))
        self.assertLessEqual(max(sum(1 for t in tr if (t["symbol"], t["date"]) == k)
                                 for k in set((t["symbol"], t["date"]) for t in tr)), 1)

    def test_vwap_reversion_trades_both_sides(self):
        s = lab("vwap_reversion")
        tr, _ = le.run_strategy(self.ds, s, s.default(), {})
        self.assertEqual(set(t["side"] for t in tr), {"long", "short"})
        self.assertTrue(all(t["time"] >= "10:00" for t in tr))
        per_day = {}
        for t in tr:
            per_day.setdefault((t["symbol"], t["date"]), []).append(t)
        for rows in per_day.values():
            self.assertLessEqual(len(rows), 2)
            if len(rows) == 2:
                self.assertLessEqual(rows[0]["exit_time"], rows[1]["time"])        # one at a time

    def test_sector_lead_lag_uses_sector_peers(self):
        self.assertEqual(self.ds.peer_map["TCS"], ("INFY",))
        self.assertEqual(self.ds.peer_map["RELIANCE"], ())
        s = lab("sector_lead_lag")
        tr, _ = le.run_strategy(self.ds, s, s.default(), {})
        self.assertGreater(len(tr), 5)
        banks = {"HDFCBANK", "ICICIBANK", "AXISBANK", "SBIN"}
        self.assertTrue(set(t["symbol"] for t in tr) <= banks | {"TCS", "INFY"})
        self.assertTrue(all("leader" in t["why"] for t in tr))

    def test_pipeline_and_report(self):
        for name in rs.EXAMPLES:
            self.assertEqual(self.log(name, name)[0], 0)
            code, out, err = self.cli("pipeline", name)
            self.assertEqual(code, 0, err)
            self.assertIn("Result:", out)
            self.assertIn("no_look_ahead", out)
        code, out, _ = self.cli("list")
        self.assertIn("15 trials", out)
        code, _, _ = self.cli("report")
        self.assertEqual(code, 0)
        dash = jload(os.path.join(self.tmp, "research", "research-dash.json"))
        self.assertEqual(len(dash["rs_experiments"]), 3)
        row = dash["rs_experiments"][0]
        for k in ("id", "family", "status", "trades", "net_R", "net_inr", "ci_low", "ci_high", "dsr", "gates_passed",
                  "holdout"):
            self.assertIn(k, row)
        self.assertEqual(dash["rs_meta"][0]["total_trials"], 15)
        with open(os.path.join(self.tmp, "research", "report.md")) as fh:
            md = fh.read()
        self.assertIn("**15 trials**", md)
        self.assertIn("holdout 2026-10-13..2026-10-29", md)
        code, out, _ = self.cli("show", "vwap_reversion")
        self.assertIn("Why it should work", out)


# ---------------------------------------------------------------- the 2026-10-29 audit's fixes

class TrialAccounting(TempTrader):
    def test_note_looks_raise_the_total_and_dsr_is_restated_at_todays_n(self):
        self.log("s1", "short_orb_breakdown")
        self.assertEqual(self.cli("pipeline", "s1")[0], 0)
        recs, _ = rs.read_registry()
        self.assertEqual(rs.total_trials(recs), 8)
        g = [r for r in recs if r["op"] == "gate"][-1]
        self.assertEqual(g["trials_at_gate"], 8)
        self.assertIn("inputs", g["gates"]["dsr"])                  # new gates store the DSR's inputs
        self.assertEqual(rs.dsr_now(g, recs), g["gates"]["dsr"]["value"])   # same N: same DSR
        code, _, err = self.cli("note", "--looks", "0", "--dataset", "fixture:12:5m", "--why", "x" * 30)
        self.assertEqual(code, 1)
        code, _, err = self.cli("note", "--looks", "5", "--dataset", "fixture:12:5m", "--why", "short")
        self.assertEqual(code, 1)
        self.assertIn("--why", err)
        code, out, err = self.cli("note", "--looks", "300", "--dataset", "fixture:12:5m", "--family", "lead-lag",
                                  "--why", "pairwise lag correlations read before logging anything")
        self.assertEqual(code, 0, err)
        self.assertIn("Registry total now 308 trials (8 explore + 300 side looks)", out)
        recs, _ = rs.read_registry()
        self.assertEqual((rs.total_trials(recs), rs.explore_trials(recs), rs.side_looks(recs)), (308, 8, 300))
        self.assertEqual(len(rs.trial_srs(recs)), 8)                 # looks add to N, not to the SR variance
        # the old gate re-stated at N = 308 is harder than as gated at N = 8
        x = rs.dsr_inputs(g)
        p8 = st.dsr_at(x[0], x[1], x[2], x[3], 8)[0]
        p308 = st.dsr_at(x[0], x[1], x[2], x[3], 308)[0]
        self.assertLess(p308, p8)
        code, out, _ = self.cli("show", "s1")
        self.assertIn("at today's 308 trials", out)
        code, out, _ = self.cli("list")
        self.assertIn("308 trials in all (8 explore combinations + 300 logged side looks)", out)
        self.assertIn("DSR now", out)
        self.assertEqual(self.cli("report")[0], 0)
        dash = jload(os.path.join(self.tmp, "research", "research-dash.json"))
        row = dash["rs_experiments"][0]
        self.assertEqual(row["trials_at_gate"], 8)
        self.assertIn("dsr_now", row)
        meta = dash["rs_meta"][0]
        self.assertEqual((meta["total_trials"], meta["explore_trials"], meta["side_looks"]), (308, 8, 300))
        with open(os.path.join(self.tmp, "research", "report.md")) as fh:
            md = fh.read()
        self.assertIn("**308 trials**", md)
        self.assertIn("## Side looks (counted as trials)", md)
        self.assertIn("**300** on fixture:12:5m (lead-lag)", md)
        self.assertIn("DSR now", md)
        code, out, _ = self.cli("gate", "s1")                       # gate shows then vs now
        self.assertIn("DSR as gated", out)
        self.assertIn("at today's N=308", out)
        self.assertEqual(self.cli("verify")[0], 0)

    def test_old_gate_text_is_parsed(self):
        g = {"gates": {"dsr": {"value": 0.9227, "text": "SR 0.146 a trade (skew -0.01, kurtosis 2.09, T=392); 8 trials "
                                                       "so far -> luck alone would show 0.074; DSR +0.923"}}}
        self.assertEqual(rs.dsr_inputs(g), (0.146, 392, -0.01, 2.09))
        self.assertIsNone(rs.dsr_inputs({"gates": {"dsr": {"text": "not enough trades for a Sharpe ratio"}}}))

    def test_log_stores_an_absolute_list_file(self):
        cwd = os.getcwd()
        os.chdir(COMMON)
        try:
            code, _, err = self.cli("--list-file", "n500.csv", "log", "--id", "abs1", "--from-example",
                                    "vwap_reversion", "--universe", "nifty500")
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0, err)
        recs, _ = rs.read_registry()
        self.assertEqual(recs[-1]["list_file"], os.path.join(COMMON, "n500.csv"))


class CapitalCap(TempTrader):
    def setUp(self):
        super().setUp()
        self.ds = self.dataset("explore")
        self.s = lab("short_orb_breakdown")

    def test_cap_limits_concurrent_positions_and_capital(self):
        capped, ic = le.run_strategy(self.ds, self.s, self.s.default(), {})
        free, iff = le.run_strategy(self.ds, self.s, self.s.default(), {"capital_cap": False})
        self.assertEqual(ic["capital_cap"], {"on": True, "capital": 100000.0, "max_positions": 5})
        self.assertFalse(iff["capital_cap"]["on"])
        self.assertGreater(ic["capital_skips"], 0)
        self.assertEqual(iff["capital_skips"], 0)
        self.assertEqual(len(capped) + ic["capital_skips"] >= len(free), True)
        self.assertLess(len(capped), len(free))
        self.assertGreater(iff["peak_deployed"], 100000)              # uncapped: more than the account
        self.assertLessEqual(ic["peak_deployed"], 100000)
        self.assertLessEqual(ic["peak_positions"], 5)
        # re-check from the trades: at the close of every decision bar, open positions fit the cap
        by_day = {}
        for t in capped:
            by_day.setdefault(t["date"], []).append(t)
        for rows in by_day.values():
            for k in sorted(set(le.slot_of(t["time"]) for t in rows)):
                live = [t for t in rows if le.slot_of(t["time"]) <= k < t["exit_k"]]
                self.assertLessEqual(len(live), 5)
                self.assertLessEqual(sum(t["entry"] * t["qty"] for t in live), 100000 + 1e-6)
        # trades a capped account took are the uncapped engine's trades (same rules, fewer of them)
        key = lambda t: (t["symbol"], t["date"], t["time"])           # noqa: E731
        fk = dict((key(t), t) for t in free)
        same = [t for t in capped if key(t) in fk]
        self.assertGreater(len(same), 0.8 * len(capped))
        for t in same:
            self.assertEqual(t["pnl_net"], fk[key(t)]["pnl_net"])
        self.assertEqual([t for t in capped], sorted(capped, key=lambda t: (t["date"], t["symbol"], t["time"])))

    def test_max_positions_one(self):
        tr, info = le.run_strategy(self.ds, self.s, self.s.default(), {"max_positions": 1})
        self.assertEqual(info["peak_positions"], 1)
        for d in set(t["date"] for t in tr):
            rows = sorted((t for t in tr if t["date"] == d), key=lambda t: le.slot_of(t["time"]))
            for a, b in zip(rows, rows[1:]):
                self.assertGreaterEqual(le.slot_of(b["time"]), a["exit_k"])

    def test_runs_record_the_setting_and_no_capital_cap_flag(self):
        self.log("s1", "short_orb_breakdown")
        code, out, err = self.cli("explore", "s1")
        self.assertEqual(code, 0, err)
        self.assertIn("Portfolio cap: at most", out)
        run = jload(rs.runs_path("s1", "explore"))
        self.assertTrue(run["capital_cap"]["on"])
        recs, _ = rs.read_registry()
        ex = [r for r in recs if r["op"] == "explore"][-1]
        self.assertTrue(ex["capital_cap"]["on"])
        self.assertIn("capital_skips", ex["variants"][0])
        code, out, err = self.cli("--no-capital-cap", "validate", "s1")
        self.assertEqual(code, 0, err)
        self.assertIn("Portfolio cap: OFF", out)
        doc = jload(rs.runs_path("s1", "validate"))
        self.assertFalse(doc["capital_cap"]["on"])
        recs, _ = rs.read_registry()
        self.assertFalse([r for r in recs if r["op"] == "validate"][-1]["capital_cap"]["on"])
        self.assertTrue(rs.config()["capital_cap"] is False)
        self.cli("list")                                              # a later command without the flag: ON again
        self.assertTrue(rs.config()["capital_cap"])

    def test_never_sized_symbols_are_reported(self):
        cfg = {"position_cap_pct": 1.5}                                # Rs 1,500 a position: the big caps never fit
        rows = le.unsizable(self.ds, cfg)
        names = [r["symbol"] for r in rows]
        self.assertIn("LT", names)
        self.assertNotIn("ITC", names)
        self.assertTrue(all(r["min_price"] > r["limit"] == 1500.0 for r in rows))
        self.assertEqual(le.unsizable(self.ds, {}), [])
        nse.write_json(os.path.join(self.tmp, "config.json"), cfg)
        self.log("s1", "short_orb_breakdown")
        code, out, err = self.cli("explore", "s1")
        self.assertEqual(code, 0, err)
        self.assertIn("never sized", out)
        self.assertIn("LT (low", out)
        run = jload(rs.runs_path("s1", "explore"))
        self.assertIn("LT", [r["symbol"] for r in run["never_sized"]])


class Exposure(TempTrader):
    def artefact(self, name, frm, to, symbols=None, underlying=None):
        meta = {"from": frm, "to": to}
        if symbols:
            meta["symbols"] = symbols
        if underlying:
            meta["underlying"] = underlying
        nse.write_json(os.path.join(self.tmp, "backtest", name), {"made": "2026-10-29T17:08:00+05:30", "meta": meta})

    def test_new_split_records_what_saw_it_before(self):
        self.artefact("2026-10-29-nifty50-all.json", "2026-08-04", "2026-10-29", ["RELIANCE", "TCS", "ZZZ"])
        self.artefact("2026-10-29-options-NIFTY.json", "2025-10-08", "2026-10-28", underlying="NIFTY")
        self.artefact("old.json", "2026-01-01", "2026-03-01", ["RELIANCE"])            # before validate: no overlap
        nse.write_json(os.path.join(self.tmp, "backtest", "dash.json"), {"made": "x", "rows": []})   # no meta
        self.dataset("explore")
        sp = jload(le.splits_path())["fixture:12:5m"]
        eb = sp["exposed_before"]
        arts = set(x["artefact"] for x in eb)
        self.assertEqual(arts, {os.path.join("backtest", "2026-10-29-nifty50-all.json")})
        h = [x for x in eb if x["window"] == "holdout"][0]
        self.assertEqual((h["overlap"], h["symbols"], h["n_symbols"]), (["2026-10-13", "2026-10-29"], ["RELIANCE", "TCS"], 2))
        v = [x for x in eb if x["window"] == "validate"][0]
        self.assertEqual(v["overlap"], ["2026-09-24", "2026-10-12"])
        # a second dataset over the same dates: its validate/holdout overlap the first split's explore/validate
        le.load_dataset("other:RELIANCE:5m", ["RELIANCE"], DIRS, "explore")
        doc = jload(le.splits_path())
        self.assertEqual(doc["fixture:12:5m"], sp)                      # the existing record never changes
        o = [x for x in doc["other:RELIANCE:5m"]["exposed_before"] if x["kind"] == "split"]
        self.assertTrue(any(x["artefact"] == "splits.json fixture:12:5m (validate)" for x in o))
        code, out, _ = self.cli("splits")
        self.assertIn("exposed before (scanned when the split was made", out)
        self.assertIn("2026-10-29-nifty50-all.json on holdout 2026-10-13..2026-10-29", out)

    def test_existing_split_without_the_field_is_not_touched(self):
        rec = le.make_split(["2026-01-%02d" % d for d in range(1, 31)], ["RELIANCE"])
        nse.write_json(le.splits_path(), {"old:RELIANCE:5m": rec})
        before = jload(le.splits_path())
        le.save_split("old:RELIANCE:5m", dict(rec, sessions=[1, 1, 1]))
        self.assertEqual(jload(le.splits_path()), before)
        self.assertNotIn("exposed_before", before["old:RELIANCE:5m"])
        code, out, _ = self.cli("splits")
        self.assertIn("exposure: not recorded", out)

    def test_exposure_note_is_append_only_and_once_per_source(self):
        self.dataset("explore")                                          # split made before any artefact
        self.artefact("2026-10-29-nifty50-all.json", "2026-08-04", "2026-10-29", ["RELIANCE", "TCS"])
        self.artefact("2026-10-29-tata-all.json", "2026-08-04", "2026-10-29", ["TATAPOWER"])
        why = "pre-research backtests ran the long rules over this holdout before the split existed"
        code, _, err = self.cli("exposure-note", "--dataset", "nope:5m", "--why", why)
        self.assertEqual(code, 1)
        code, _, err = self.cli("exposure-note", "--dataset", "fixture:12:5m", "--why", why, "--source", "audit",
                                "--artefact", "missing.json")
        self.assertEqual(code, 1)
        self.assertIn("missing.json", err)
        before = jload(le.splits_path())
        code, out, err = self.cli("exposure-note", "--dataset", "fixture:12:5m", "--why", why, "--source", "audit",
                                  "--artefact", "2026-10-29-nifty50-all.json")
        self.assertEqual(code, 0, err)
        self.assertIn("1 more artefact(s) overlap per the scan", out)
        self.assertEqual(jload(le.splits_path()), before)               # informational: splits unchanged
        recs, _ = rs.read_registry()
        n = [r for r in recs if r["op"] == "exposure_note"]
        self.assertEqual(len(n), 1)
        self.assertEqual([x["window"] for x in n[0]["artefacts"]], ["validate", "holdout"])
        self.assertEqual(n[0]["also_found_by_scan"], [os.path.join("backtest", "2026-10-29-tata-all.json")])
        code, _, err = self.cli("exposure-note", "--dataset", "fixture:12:5m", "--why", why, "--source", "audit")
        self.assertEqual(code, 1)
        self.assertIn("recorded once", err)
        self.assertEqual(rs.total_trials(recs), 0)                       # a note about exposure is not a trial
        code, out, _ = self.cli("splits")
        self.assertIn("exposure note", out)
        self.assertEqual(self.cli("report")[0], 0)
        with open(os.path.join(self.tmp, "research", "report.md")) as fh:
            md = fh.read()
        self.assertIn("## Holdout exposure", md)
        self.assertIn("2026-10-29-nifty50-all.json on holdout", md)
        self.assertEqual(self.cli("verify")[0], 0)


if __name__ == "__main__":
    unittest.main()
