import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX, COMMON

import nse

alerts = load("india-alerts", "alerts")

QDIR = os.path.join(FIX, "india-alerts")
PLAN = os.path.join(QDIR, "plan-reliance.json")
SCRIP = os.path.join(COMMON, "dhan-scrip-sample.csv")
DT = datetime.datetime
MUHURAT = {"special_sessions": {"2026-11-08": ["18:00", "19:00"]}}


def ist(text):
    return nse.parse_now(text)


def run(argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    saved = dict(os.environ)
    os.environ.pop("DHAN_CLIENT_ID", None)
    os.environ.pop("DHAN_ACCESS_TOKEN", None)
    os.environ.update(env or {})
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = alerts.main(argv)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return code, out.getvalue(), err.getvalue()


def plan_rules(cfg=None):
    with open(PLAN) as fh:
        return alerts.rules_from_plan(json.load(fh), cfg)


class Clock(unittest.TestCase):
    def test_states_and_next_open(self):
        st = alerts.status(ist("2026-10-29 16:00"))
        self.assertEqual((st["state"], st["reason"], st["next_open"]),
                         ("closed", "after the close", "2026-10-30T09:15:00+05:30"))
        st = alerts.status(ist("2026-10-30 09:05"))
        self.assertEqual(st["state"], "pre-open")
        self.assertTrue(alerts.status(ist("2026-10-30 09:15"))["open"])
        self.assertFalse(alerts.status(ist("2026-10-30 15:30"))["open"])
        st = alerts.status(ist("2026-10-31 11:00"))
        self.assertEqual((st["reason"], st["next_open"]), ("weekend", "2026-11-02T09:15:00+05:30"))
        st = alerts.status(ist("2026-11-10 11:00"))                     # Diwali Balipratipada
        self.assertIn("Diwali", st["reason"])
        self.assertEqual(st["next_open"], "2026-11-11T09:15:00+05:30")

    def test_muhurat_needs_the_hour_in_config(self):
        st = alerts.status(ist("2026-11-08 18:30"))
        self.assertEqual(st["state"], "closed")
        self.assertIn("special_sessions", st["muhurat"])
        st = alerts.status(ist("2026-11-08 18:30"), MUHURAT)
        self.assertEqual((st["state"], st["reason"], st["session_close"]), ("open", "special session", "19:00"))
        self.assertIn("2026-11-08", alerts.upcoming_muhurat(ist("2026-10-29 16:00")))
        self.assertIsNone(alerts.upcoming_muhurat(ist("2026-10-29 16:00"), MUHURAT))

    def test_clock_cli(self):
        code, text, _ = run(["clock", "--now", "2026-10-29 16:00"])
        self.assertEqual(code, 1)
        self.assertIn("NSE CLOSED, after the close", text)
        self.assertIn("Next open: 2026-10-30 09:15 IST (Friday)", text)
        self.assertIn("Muhurat", text)
        code, text, _ = run(["clock", "--now", "2026-10-29 10:00"])
        self.assertEqual(code, 0)
        self.assertIn("330 minutes to the close. Flat by 15:05", text)
        code, text, _ = run(["clock", "--now", "2027-01-04 10:00"])
        self.assertIn("2027 holiday list is not in this pack", text)
        code, text, _ = run(["clock", "--now", "2026-10-02 10:00"])
        self.assertIn("Mahatma Gandhi Jayanti", text)


class Quotes(unittest.TestCase):
    def test_yahoo_fixtures(self):
        q, errors = alerts.yahoo_quotes(["RELIANCE", "HDFCBANK", "INFY", "TATAMOTORS"], QDIR)
        self.assertEqual(q["RELIANCE"]["price"], 1167.4)
        self.assertEqual(q["RELIANCE"]["prev_close"], 1158.0)
        self.assertEqual(q["RELIANCE"]["time"], "2026-10-29T15:29:03+05:30")
        self.assertEqual(q["HDFCBANK"]["price"], 683.3)
        self.assertEqual(len(errors), 1)
        self.assertIn("No data found", errors[0])

    def test_offline_gz(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(QDIR, "yahoo-INFY.json"), "rb") as src:
                with gzip.open(os.path.join(d, "yahoo-INFY.json.gz"), "wb") as dst:
                    dst.write(src.read())
            q, errors = alerts.yahoo_quotes(["INFY"], d)
            self.assertEqual((q["INFY"]["price"], errors), (1146.5, []))
        finally:
            shutil.rmtree(d)

    def test_dhan_ltp_one_call_for_every_symbol(self):
        with open(SCRIP) as fh:
            scrip = nse.parse_dhan_scrip(fh.read())
        calls = []

        def transport(url, headers=None, data=None, method=None, **kw):
            calls.append((url, method, data, headers["client-id"]))
            return json.dumps({"data": {"NSE_EQ": {"2885": {"last_price": 1167.4}, "1333": {"last_price": 683.3}}},
                               "status": "success"}).encode()
        client = nse.Dhan("1100000001", "tok", transport=transport)
        q, errors = alerts.dhan_quotes(["RELIANCE", "HDFCBANK", "NOPE"], ist("2026-10-29 15:20"), scrip=scrip,
                                       client=client)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ("https://api.dhan.co/v2/marketfeed/ltp", "POST"))
        self.assertEqual(sorted(calls[0][2]["NSE_EQ"]), [1333, 2885])
        self.assertEqual((q["RELIANCE"]["price"], q["HDFCBANK"]["price"]), (1167.4, 683.3))
        self.assertIsNone(q["RELIANCE"]["prev_close"])
        self.assertIn("NOPE", errors[0])

    def test_source_choice(self):
        self.assertEqual(alerts.pick_source(None, {}, {}), "yahoo")
        keys = {"DHAN_CLIENT_ID": "1", "DHAN_ACCESS_TOKEN": "t"}
        self.assertEqual(alerts.pick_source(None, {}, keys), "dhan")
        self.assertEqual(alerts.pick_source(None, {"data_source": "yahoo"}, keys), "yahoo")
        with self.assertRaises(alerts.AlertError):
            alerts.pick_source("dhan", {}, {})


class FakeHttp(object):
    def __init__(self, answers):
        self.answers, self.urls, self.sleeps = list(answers), [], []
        with open(os.path.join(QDIR, "yahoo-RELIANCE.json"), "rb") as fh:
            self.body = fh.read()

    def get(self, url, **kw):
        self.urls.append(url)
        a = self.answers.pop(0)
        if a == 200:
            return self.body
        if a == 404:
            raise nse.NSEError('HTTP 404 from query1.finance.yahoo.com {"chart":{"result":null,"error":'
                               '{"code":"Not Found","description":"No data found, symbol may be delisted"}}}')
        raise nse.NSEError("HTTP %d from host" % a)


class Network(unittest.TestCase):
    def setUp(self):
        self.saved = (alerts._http, alerts._sleep)

    def tearDown(self):
        alerts._http, alerts._sleep = self.saved

    def fake(self, answers):
        f = FakeHttp(answers)
        alerts._http, alerts._sleep = f.get, f.sleeps.append
        return f

    def test_ns_suffix_and_one_symbol_per_request_with_a_pause(self):
        f = self.fake([200, 200])
        q, errors = alerts.yahoo_quotes(["RELIANCE", "INFY"])
        self.assertEqual(errors, [])
        self.assertEqual(f.urls[0], "https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=1d")
        self.assertIn("/INFY.NS?", f.urls[1])
        self.assertEqual(f.sleeps, [nse.PAUSE])

    def test_second_host_on_error_but_not_on_404(self):
        f = self.fake([503, 200])
        self.assertIn("RELIANCE", alerts.yahoo_quotes(["RELIANCE"])[0])
        self.assertEqual([u.split("/")[2].split(".")[0] for u in f.urls], ["query1", "query2"])
        f = self.fake([404])
        q, errors = alerts.yahoo_quotes(["TATAMOTORS"])
        self.assertEqual((q, len(f.urls)), ({}, 1))
        self.assertIn("No data found", errors[0])


class Rules(unittest.TestCase):
    def setUp(self):
        self.q, _ = alerts.yahoo_quotes(["RELIANCE", "HDFCBANK", "INFY"], QDIR)

    def at(self, text, rules, state=None, quotes=None, cfg=None):
        now = ist(text)
        return alerts.evaluate(rules, self.q if quotes is None else quotes, alerts.status(now, cfg), state or {},
                               now, cfg)

    def test_the_plan_gives_a_stop_a_target_and_flat_by(self):
        rules = plan_rules()
        self.assertEqual([(r["id"], r["type"]) for r in rules],
                         [("reliance-plan-stop", "stop"), ("reliance-plan-target", "target"),
                          ("reliance-flat-by", "time")])
        self.assertEqual((rules[0]["price"], rules[1]["price"], rules[2]["at"]), (1163.3, 1185.2, "15:05"))
        self.assertTrue(all(r["date"] == "2026-10-29" for r in rules))
        rings, st = self.at("2026-10-29 11:00", rules)                  # 1,167.40: between the two
        self.assertEqual(rings, [])
        q = json.loads(json.dumps(self.q))
        q["RELIANCE"]["price"] = 1163.2
        rings, st = self.at("2026-10-29 11:30", rules, st, q)
        self.assertEqual([r["id"] for r in rings], ["reliance-plan-stop"])
        self.assertIn("RELIANCE is Rs 1,163.20: at or below your stop Rs 1,163.30 (plan stop: or-mid)",
                      rings[0]["message"])
        self.assertEqual(self.at("2026-10-29 11:35", rules, st, q)[0], [])      # rings once
        q["RELIANCE"]["price"] = 1185.2
        self.assertEqual([r["id"] for r in self.at("2026-10-29 12:00", rules, st, q)[0]], ["reliance-plan-target"])

    def test_flat_by_is_capped_at_dhan_square_off(self):
        self.assertEqual(plan_rules({"flat_by": "15:25"})[2]["at"], "15:05")     # the plan's own 15:05 wins
        with open(PLAN) as fh:
            plan = json.load(fh)
        plan.pop("flat_by")
        self.assertEqual(alerts.rules_from_plan(plan, {"flat_by": "15:25"})[2]["at"], "15:10")
        self.assertEqual(alerts.rules_from_plan(plan, {})[2]["at"], "15:05")

    def test_flat_by_rings_once_on_the_plan_day(self):
        rules = plan_rules()[2:]
        self.assertEqual(self.at("2026-10-29 15:04", rules)[0], [])
        rings, st = self.at("2026-10-29 15:05", rules)
        self.assertIn("Flat by: it is 15:05 IST (set for 15:05), NSE closes at 15:30", rings[0]["message"])
        self.assertIn("india-broker flatten --symbol RELIANCE", rings[0]["message"])
        self.assertIn("15:10", rings[0]["message"])
        self.assertEqual(st["rung"]["reliance-flat-by"], "2026-10-29T15:05:00+05:30")
        self.assertEqual(self.at("2026-10-29 15:09", rules, st)[0], [])
        rings, _ = self.at("2026-10-29 15:40", rules)
        self.assertIn("the market has closed", rings[0]["message"])
        self.assertEqual(self.at("2026-10-30 15:06", rules, st)[0], [])         # plan rules: their day only

    def test_time_alert_moves_inside_a_muhurat_session(self):
        rule = [{"id": "flat", "type": "time", "at": "15:05", "label": "flat by", "before_close": True}]
        self.assertEqual(self.at("2026-11-08 18:54", rule, cfg=MUHURAT)[0], [])
        rings, _ = self.at("2026-11-08 18:55", rule, cfg=MUHURAT)
        self.assertIn("set for 18:55), NSE closes at 19:00", rings[0]["message"])
        self.assertEqual(self.at("2026-11-08 18:55", rule)[0], [])               # no hour set: no session
        self.assertEqual(self.at("2026-10-31 15:06", rule)[0], [])               # Saturday
        self.assertEqual(self.at("2026-10-20 15:06", rule)[0], [])               # Dussehra

    def test_pre_open_and_opening_bell_once_a_day(self):
        rules = [{"id": "pre", "type": "pre_open"}, {"id": "bell", "type": "opening_bell"}]
        self.assertEqual(self.at("2026-10-30 08:59", rules)[0], [])
        rings, st = self.at("2026-10-30 09:00", rules)
        self.assertEqual([r["id"] for r in rings], ["pre"])
        self.assertEqual(self.at("2026-10-30 09:10", rules, st)[0], [])
        rings, st = self.at("2026-10-30 09:15", rules, st)
        self.assertEqual([r["id"] for r in rings], ["bell"])
        self.assertIn("Opening bell: NSE is open (09:15 IST, normal market 09:15 to 15:30)", rings[0]["message"])
        self.assertEqual(self.at("2026-10-30 10:00", rules, st)[0], [])
        rings, _ = self.at("2026-11-02 09:16", rules, st)                        # Monday: the bell again
        self.assertEqual([r["id"] for r in rings], ["bell"])
        rings, _ = self.at("2026-11-08 18:01", rules, cfg=MUHURAT)               # Muhurat: bell, no pre-open
        self.assertEqual([r["id"] for r in rings], ["bell"])

    def test_pct_move_and_rearm(self):
        up = [{"id": "infy-2", "symbol": "INFY", "type": "pct_move", "pct": 2, "direction": "up"}]
        rings, st = self.at("2026-10-29 15:20", up)
        self.assertIn("INFY moved +2.08% to Rs 1,146.50 (from Rs 1,123.15)", rings[0]["message"])
        self.assertEqual(self.at("2026-10-29 15:25", up, st)[0], [])
        down = [{"id": "x", "symbol": "INFY", "type": "pct_move", "pct": 2, "direction": "down"}]
        self.assertEqual(self.at("2026-10-29 15:20", down)[0], [])
        rules = [{"id": "hdfc", "symbol": "HDFCBANK", "type": "below", "price": 685, "rearm": True}]
        rings, st = self.at("2026-10-29 15:00", rules)
        self.assertEqual(len(rings), 1)
        q = json.loads(json.dumps(self.q))
        q["HDFCBANK"]["price"] = 690.0
        rings, st = self.at("2026-10-29 15:05", rules, st, q)
        self.assertNotIn("hdfc", st["rung"])

    def test_pct_move_without_previous_close_says_so(self):
        q = {"RELIANCE": {"symbol": "RELIANCE", "price": 1167.4, "prev_close": None, "source": "dhan"}}
        rules = [{"id": "r", "symbol": "RELIANCE", "type": "pct_move", "pct": 1}]
        rings, st = self.at("2026-10-29 15:00", rules, quotes=q)
        self.assertEqual(rings, [])
        self.assertIn("ref", st["notes"]["r"])
        rules[0]["ref"] = 1150
        self.assertEqual(len(self.at("2026-10-29 15:00", rules, quotes=q)[0]), 1)

    def test_example_rules_are_valid(self):
        with open(os.path.join(ROOT, "india-alerts", "alerts.example.json")) as fh:
            rules = json.load(fh)["rules"]
        for r in rules:
            alerts.validate(r)
        plan_ids = [r for r in rules if r["id"].startswith("reliance-")]
        self.assertEqual(plan_ids, plan_rules())

    def test_bad_rules(self):
        st = alerts.status(ist("2026-10-29 10:00"))
        for bad in ({"id": "x", "type": "moon"}, {"id": "x", "type": "above", "symbol": "INFY"},
                    {"id": "x", "type": "time", "at": "3pm"}):
            with self.assertRaises(alerts.AlertError):
                alerts.evaluate([bad], {}, st, {}, ist("2026-10-29 10:00"))
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "RELIANCE", "stop": 1163.3})


class Holdings(unittest.TestCase):
    def test_symbols_from_any_shape(self):
        self.assertEqual(alerts.symbols_in({"symbol": "reliance.ns"}), ["RELIANCE"])
        self.assertEqual(alerts.symbols_in({"positions": [{"symbol": "INFY", "qty": 5}]}), ["INFY"])
        self.assertEqual(alerts.symbols_in({"positions": {"TCS": {"qty": 3}, "SBIN": {"qty": 0}}}), ["TCS"])
        self.assertEqual(alerts.symbols_in({"symbols": ["HDFCBANK", "M&M"]}), ["HDFCBANK", "M&M"])
        self.assertEqual(alerts.symbols_in({"setups": [{"symbol": "ITC"}]}), ["ITC"])
        self.assertEqual(alerts.symbols_in(None), [])


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.base = ["--trader-dir", self.d]

    def tearDown(self):
        shutil.rmtree(self.d)

    def check(self, now, extra=()):
        return run(self.base + ["check", "--offline", QDIR, "--now", now] + list(extra))

    def test_the_trade_from_the_plan(self):
        code, text, _ = run(self.base + ["add", "--from-plan", "--plan", PLAN])
        self.assertEqual(code, 0)
        self.assertIn("Added reliance-flat-by", text)
        self.assertEqual(run(self.base + ["add", "--opening-bell"])[0], 0)
        code, text, _ = self.check("2026-10-29 10:52")
        self.assertEqual(code, 0)                                                 # the bell first
        code, text, _ = self.check("2026-10-29 10:53")
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit. 4 rule(s) watched today, RELIANCE Rs 1,167.40.", text)
        with open(os.path.join(self.d, "prices.json")) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"RELIANCE": 1167.4})
        self.assertEqual(doc["source"], "yahoo")
        self.assertEqual(doc["detail"]["RELIANCE"]["fetched"], "2026-10-29T10:53:00+05:30")
        code, text, _ = self.check("2026-10-29 15:05")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Flat by", text)
        self.assertIn("not an order", text)
        self.assertEqual(self.check("2026-10-29 15:07")[0], 1)
        code, text, _ = self.check("2026-10-30 09:20")
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-10-30 09:25")
        self.assertIn("Nothing hit. 1 rule(s) watched today.", text)
        run(self.base + ["add", "--from-plan", "--plan", PLAN])                   # a new plan re-arms its rules
        with open(os.path.join(self.d, "state", "alerts.json")) as fh:
            self.assertNotIn("reliance-flat-by", json.load(fh)["rung"])

    def test_add_list_reset_and_duplicates(self):
        for args in (["--pre-open"], ["--symbol", "infy.ns", "--above", "1150"], ["--symbol", "HDFCBANK", "--stop", "675"]):
            self.assertEqual(run(self.base + ["add"] + args)[0], 0)
        code, _, err = run(self.base + ["add", "--symbol", "INFY", "--above", "1150"])
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        with open(os.path.join(self.d, "alerts.json")) as fh:
            rules = json.load(fh)["rules"]
        self.assertEqual(rules[1], {"id": "infy-above-1150", "symbol": "INFY", "type": "above", "price": 1150.0})
        code, text, _ = self.check("2026-10-30 09:02")
        self.assertIn("ALERT Pre-open", text)
        code, text, _ = run(self.base + ["list"])
        self.assertIn("rang 2026-10-30T09:02:00+05:30", text)
        self.assertEqual(run(self.base + ["reset", "pre-open"])[0], 0)
        self.assertEqual(run(self.base + ["reset", "pre-open"])[0], 1)
        self.assertEqual(run(self.base + ["add", "--at", "3pm"])[0], 1)
        code, _, err = run(self.base + ["add", "--from-plan", "--plan", os.path.join(self.d, "none.json")])
        self.assertIn("no plan", err)

    def test_quote_write_merges_and_holdings(self):
        code, text, _ = run(self.base + ["quote", "RELIANCE", "INFY", "--offline", QDIR, "--write",
                                         "--now", "2026-10-29 15:45"])
        self.assertEqual(code, 0)
        self.assertIn("RELIANCE   Rs 1,167.40 (+0.81% vs prev close Rs 1,158.00)", text)
        with open(os.path.join(self.d, "paper.json"), "w") as fh:
            json.dump({"positions": [{"symbol": "HDFCBANK", "qty": 10}]}, fh)
        code, text, _ = run(self.base + ["quote", "--holdings", "--offline", QDIR, "--write"])
        with open(os.path.join(self.d, "prices.json")) as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(doc["prices"]), ["HDFCBANK", "INFY", "RELIANCE"])
        self.assertEqual(doc["detail"]["INFY"]["fetched"], "2026-10-29T15:45:00+05:30")

    def test_dhan_offline_when_keys_are_set(self):
        keys = {"DHAN_CLIENT_ID": "1", "DHAN_ACCESS_TOKEN": "t"}
        code, text, _ = run(self.base + ["quote", "RELIANCE", "HDFCBANK", "--offline", QDIR, "--scrip", SCRIP,
                                         "--json", "--now", "2026-10-29 15:20"], keys)
        self.assertEqual(code, 0)
        q = json.loads(text)["quotes"]
        self.assertEqual((q["RELIANCE"]["source"], q["RELIANCE"]["price"], q["HDFCBANK"]["security_id"]),
                         ("dhan", 1167.4, "1333"))

    def test_check_without_rules(self):
        code, _, err = self.check("2026-10-29 10:00")
        self.assertEqual(code, 1)
        self.assertIn("no rules", err)


class Hygiene(unittest.TestCase):
    def test_no_us_market_words_in_the_skill(self):
        for name in ("alerts.py", "SKILL.md"):
            with open(os.path.join(ROOT, "india-alerts", name)) as fh:
                text = fh.read()
            for word in ("NYSE", "New York", "Alpaca", "Stooq", "USD", "SEC "):
                self.assertNotIn(word, text, (name, word))


if __name__ == "__main__":
    unittest.main()
