import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from _load import load, FIX, COMMON

import nse

size = load("india-budget", "size")
check = load("india-risk", "check")
dhan = load("india-broker", "dhan")

SCRIP_CSV = os.path.join(COMMON, "dhan-scrip-sample.csv")
SCRIP = nse.scrip_master(offline=SCRIP_CSV)
PLAN_PATH = os.path.join(FIX, "india-budget", "plan-reliance.json")
TODAY = "2026-10-29"
NOW = nse.parse_now("2026-10-29 09:41")
CFG = {}          # the defaults = the contract's config


def reliance(**cfg_over):
    with open(PLAN_PATH) as fh:
        plan = json.load(fh)
    return size.make_order(size.size_trade(plan, dict(size.DEFAULTS, **cfg_over), SCRIP, TODAY))


def edit(order, **kw):
    """Change order fields and keep the Dhan body in step (so only the field under test is wrong)."""
    o = copy.deepcopy(order)
    body = o["dhan"]["body"]
    for k, v in kw.items():
        o[k] = v
        m = {"qty": "quantity", "entry": "price", "stop": "stopLossPrice", "target": "targetPrice",
             "security_id": "securityId", "product": "productType"}.get(k)
        if m:
            body[m] = v
    return o


def verdict(orders, now=NOW, cfg=None, **kw):
    return check.check(orders if isinstance(orders, list) else [orders], cfg or CFG, SCRIP, now, **kw)


def reasons(res, i=0):
    return " | ".join(res["results"][i]["reasons"])


def sent(sym, max_loss, date=TODAY, mode="paper", fp="x"):
    return {"at": date + "T09:50:00+05:30", "date": date, "status": "sent", "kind": "entry", "symbol": sym,
            "side": "buy", "qty": 10, "max_loss_all_in": max_loss, "mode": mode, "fingerprint": fp}


class Checks(unittest.TestCase):
    def test_the_reliance_order_passes(self):
        res = verdict(reliance())
        self.assertEqual((res["pass"], res["block"]), (1, 0), reasons(res))
        r = res["results"][0]
        self.assertEqual((r["kind"], r["max_loss"], r["reward_risk"]), ("entry", 231.0, 2.0))
        self.assertEqual(r["max_loss_all_in"], round(231 + nse.round_trip_cost(21, 1174.3, 1163.3), 2))
        self.assertEqual(r["fingerprint"], dhan.fingerprint(reliance()))       # same function in the broker

    def test_be_series_is_trade_to_trade(self):
        o = edit(reliance(), symbol="KAYA", security_id="10282", series="BE")
        self.assertIn("trade-to-trade", reasons(verdict(o)))

    def test_symbol_and_security_id_from_dhan(self):
        self.assertIn("security_id 2885", reasons(verdict(edit(reliance(), security_id="9999"))))
        self.assertIn("not in Dhan", reasons(verdict(edit(reliance(), symbol="NOSUCHCO"))))

    def test_prices_on_the_tick(self):
        r = reasons(verdict(edit(reliance(), entry=1174.35)))
        self.assertIn("tick: the entry 1174.35 is not on RELIANCE's 0.1 tick", r)
        self.assertIn("tick: the stop 1163.33", reasons(verdict(edit(reliance(), stop=1163.33))))

    def test_window_and_calendar(self):
        o = reliance()
        self.assertIn("no new entries before 09:30", reasons(verdict(o, nse.parse_now("2026-10-29 09:20"))))
        self.assertIn("no new entries after 14:30", reasons(verdict(o, nse.parse_now("2026-10-29 14:41"))))
        r = reasons(verdict(o, nse.parse_now("2026-10-29 15:06")))
        self.assertIn("flat-by 15:05", r)
        self.assertIn("market is closed", reasons(verdict(o, nse.parse_now("2026-10-31 10:00"))))     # Saturday
        self.assertIn("Dussehra", reasons(verdict(o, nse.parse_now("2026-10-20 10:00"))))
        self.assertIn("market is closed", reasons(verdict(o, nse.parse_now("2026-10-29 09:05"))))     # pre-open

    def test_limits(self):
        self.assertIn("loss: 60 x 11.00", reasons(verdict(edit(reliance(), qty=60), cfg={"position_cap_pct": 100})))
        self.assertIn("position:", reasons(verdict(edit(reliance(), qty=30))))
        self.assertIn("max order value", reasons(verdict(edit(reliance(), qty=45), cfg={"position_cap_pct": 100,
                                                                                         "max_loss_per_trade": 10 ** 6})))
        self.assertIn("reward:risk: 1.00", reasons(verdict(edit(reliance(), target=1185.3))))
        self.assertIn("blocked_symbols", reasons(verdict(reliance(), cfg={"blocked_symbols": ["RELIANCE"]})))
        self.assertIn("stop: the stop", reasons(verdict(edit(reliance(), stop=1180.0))))

    def test_daily_loss_and_trades_count_from_the_logs(self):
        broker = {"orders": [sent("TCS", 700), sent("INFY", 600)]}
        r = reasons(verdict(reliance(), broker=broker))
        self.assertIn("daily loss: Rs 1,300.00 already at risk", r)
        self.assertNotIn("trades:", r)
        broker["orders"].append(sent("SBIN", 10))
        self.assertIn("trades: 3 entries sent today", reasons(verdict(reliance(), broker=broker)))
        # yesterday's and live-mode entries do not count in today's paper day
        old = {"orders": [sent("TCS", 1400, date="2026-10-28"), sent("INFY", 1400, mode="live")]}
        self.assertEqual(verdict(reliance(), broker=old)["block"], 0)
        # the realised loss in the paper ledger has hit the limit
        paper = {"orders": [{"date": TODAY, "symbol": "TCS", "status": "CLOSED", "pnl": -1520.0, "fingerprint": "t"}]}
        self.assertIn("realised loss Rs 1,520.00 has hit", reasons(verdict(reliance(), paper=paper)))
        # a stop that filled worse than planned counts at what it really lost
        broker = {"orders": [sent("TCS", 500, fp="t")]}
        paper = {"orders": [{"date": TODAY, "symbol": "TCS", "status": "CLOSED", "pnl": -1300.0, "fingerprint": "t"}]}
        self.assertIn("Rs 1,300.00 already at risk", reasons(verdict(reliance(), broker=broker, paper=paper)))

    def test_duplicates(self):
        self.assertIn("already sent today", reasons(verdict(reliance(), broker={"orders": [sent("RELIANCE", 100)]})))
        res = verdict([reliance(), reliance()], cfg={"position_cap_pct": 100})
        self.assertEqual([r["verdict"] for r in res["results"]], ["PASS", "BLOCK"])
        self.assertIn("second RELIANCE", reasons(res, 1))

    def test_product_leverage_fno_short(self):
        self.assertIn("leveraged", reasons(verdict(edit(reliance(), product="MARGIN"))))
        self.assertIn("CNC", reasons(verdict(edit(reliance(), product="CNC"))))
        self.assertEqual(verdict(edit(reliance(), product="CNC"), cfg={"product": "CNC"})["block"], 0)
        o = edit(reliance())
        o["dhan"]["body"]["exchangeSegment"] = "NSE_FNO"
        o["exchange"] = "NSE"
        self.assertIn("F&O", reasons(verdict(o)))
        short = edit(reliance(), side="sell")
        short["dhan"]["body"]["transactionType"] = "SELL"
        self.assertIn("short:", reasons(verdict(short)))

    def test_the_dhan_body_must_agree(self):
        o = reliance()
        o["dhan"]["body"]["quantity"] = 22
        self.assertIn("body quantity is 22", reasons(verdict(o)))
        o = reliance()
        o["dhan"]["body"]["stopLossPrice"] = 1150.0
        self.assertIn("body stopLossPrice", reasons(verdict(o)))
        o = reliance()
        o["dhan"]["body"]["dhanClientId"] = "1100012345"
        self.assertIn("never be on disk", reasons(verdict(o)))
        o = reliance()
        o["dhan"]["endpoint"] = "POST /v2/orders"
        self.assertIn("super/orders", reasons(verdict(o)))
        o = reliance()
        o["dhan"]["body"]["correlationId"] = "x" * 31
        self.assertIn("correlationId", reasons(verdict(o)))

    def test_circuit_band(self):
        r = reasons(verdict(reliance(), circuits={"2885": (1056.9, 1191.8)}))
        self.assertIn("target 1196.30 is above the upper circuit 1191.80", r)
        self.assertEqual(verdict(reliance(), circuits={"2885": (1056.9, 1291.7)})["block"], 0)

    def test_circuit_from_dhan_quote(self):
        # Built from the documented /marketfeed/quote shape (data -> segment -> security id), not recorded.
        calls = []

        def fake(url, headers=None, data=None, method=None, **kw):
            calls.append((method, url, data))
            return json.dumps({"data": {"NSE_EQ": {"2885": {"last_price": 1167.4, "upper_circuit_limit": 1291.7,
                                                            "lower_circuit_limit": 1056.9,
                                                            "ohlc": {"open": 1159.2, "close": 1174.0,
                                                                     "high": 1182.0, "low": 1156.2}}}},
                               "status": "success"}).encode()
        with mock.patch.dict(os.environ, {"DHAN_CLIENT_ID": "1100012345", "DHAN_ACCESS_TOKEN": "tok.en.secret"}):
            band, warn = check.fetch_circuits([reliance()], {}, transport=fake)
        self.assertEqual((band, warn), ({"2885": (1056.9, 1291.7)}, None))
        self.assertEqual(calls[0][0], "POST")
        self.assertTrue(calls[0][1].endswith("/v2/marketfeed/quote"))
        self.assertEqual(calls[0][2], {"NSE_EQ": [2885]})
        with mock.patch.dict(os.environ, {"DHAN_CLIENT_ID": "", "DHAN_ACCESS_TOKEN": ""}):
            self.assertEqual(check.fetch_circuits([reliance()], {}), ({}, None))       # no keys: skipped silently

    def test_market_order_measured_from_a_fresh_higher_price(self):
        o = edit(reliance(), order_type="MARKET")
        o["dhan"]["body"]["orderType"] = "MARKET"
        o["dhan"]["body"]["price"] = 0
        fresh = {"RELIANCE": NOW - nse.datetime.timedelta(minutes=2)}
        res = verdict(o, prices={"RELIANCE": 1180.0}, price_times=fresh)
        self.assertEqual(res["results"][0]["entry_ref"], 1180.0)
        old = {"RELIANCE": NOW - nse.datetime.timedelta(minutes=40)}
        res = verdict(o, prices={"RELIANCE": 1180.0}, price_times=old)
        self.assertEqual(res["results"][0]["entry_ref"], 1174.3)
        self.assertIn("older than 15 minutes", " ".join(res["results"][0]["warnings"]))


class Exits(unittest.TestCase):
    def exit_order(self, qty=21):
        return {"symbol": "RELIANCE", "exchange": "NSE", "security_id": "2885", "side": "sell", "qty": qty,
                "product": "INTRADAY", "order_type": "MARKET", "flat_by": "15:05", "kind": "exit",
                "dhan": {"endpoint": "POST /v2/orders", "body": {
                    "transactionType": "SELL", "exchangeSegment": "NSE_EQ", "productType": "INTRADAY",
                    "orderType": "MARKET", "validity": "DAY", "securityId": "2885", "quantity": qty, "price": 0}}}

    def test_getting_out_is_never_blocked(self):
        paper = {"orders": [{"date": TODAY, "symbol": "RELIANCE", "status": "TRADED", "qty": 21, "side": "buy",
                             "fingerprint": "f"},
                            {"date": TODAY, "symbol": "TCS", "status": "CLOSED", "pnl": -2000.0, "fingerprint": "t"}]}
        broker = {"orders": [sent("RELIANCE", 300), sent("TCS", 500), sent("INFY", 500)]}
        for when in ("2026-10-29 15:20", "2026-10-29 15:45", "2026-10-29 11:00"):
            res = verdict(self.exit_order(), nse.parse_now(when), paper=paper, broker=broker,
                          cfg={"blocked_symbols": ["RELIANCE"]})
            r = res["results"][0]
            self.assertEqual((r["verdict"], r["kind"]), ("PASS", "exit"), r["reasons"])
            self.assertIn("exit, not blocked", " ".join(r["warnings"]))

    def test_selling_more_than_held_is_a_short(self):
        paper = {"orders": [{"date": TODAY, "symbol": "RELIANCE", "status": "TRADED", "qty": 21, "fingerprint": "f"}]}
        res = verdict(self.exit_order(30), paper=paper)
        self.assertEqual(res["results"][0]["kind"], "entry")
        self.assertIn("short:", reasons(res))

    def test_live_positions_snapshot_counts_as_held(self):
        broker = {"snapshots": [{"date": TODAY, "what": "positions", "positions": [
            {"symbol": "RELIANCE", "securityId": "2885", "netQty": 21, "productType": "INTRADAY", "realizedProfit": 0}]}]}
        res = verdict(self.exit_order(), nse.parse_now("2026-10-29 15:00"), cfg={"mode": "live"}, broker=broker)
        self.assertEqual(res["results"][0]["verdict"], "PASS")
        self.assertIn("LIVE mode", " ".join(res["warnings"]))


class Cli(unittest.TestCase):
    def test_cli_writes_risk_json(self):
        with tempfile.TemporaryDirectory() as d:
            nse.write_json(os.path.join(d, "orders.json"), {"orders": [reliance()]})
            out, err = io.StringIO(), io.StringIO()
            env = {"TRADER_DIR": d, "DHAN_CLIENT_ID": "", "DHAN_ACCESS_TOKEN": ""}
            with mock.patch.dict(os.environ, env), redirect_stdout(out), redirect_stderr(err):
                code = check.main(["--scrip-offline", SCRIP_CSV, "--now", "2026-10-29 09:41"])
            self.assertEqual(code, 0, out.getvalue() + err.getvalue())
            self.assertIn("PASS  #1 buy RELIANCE 21 shares, entry", out.getvalue())
            self.assertIn("Not financial advice", out.getvalue())
            with open(os.path.join(d, "risk.json")) as fh:
                rec = json.load(fh)
            self.assertEqual((rec["date"], rec["checked_at"]), (TODAY, "2026-10-29T09:41:00+05:30"))
            self.assertEqual(rec["results"][0]["fingerprint"], dhan.fingerprint(reliance()))
            self.assertEqual(rec["history"][0]["verdict"], "PASS")
            with mock.patch.dict(os.environ, env), redirect_stdout(io.StringIO()):
                self.assertEqual(check.main(["--scrip-offline", SCRIP_CSV, "--now", "2026-10-29 14:45"]), 1)


if __name__ == "__main__":
    unittest.main()
