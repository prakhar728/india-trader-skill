import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from _load import load, ROOT, FIX, COMMON

import nse

size = load("india-budget", "size")

SCRIP_CSV = os.path.join(COMMON, "dhan-scrip-sample.csv")
SCRIP = nse.scrip_master(offline=SCRIP_CSV)
PLAN_PATH = os.path.join(FIX, "india-budget", "plan-reliance.json")
CFG_PATH = os.path.join(ROOT, "india-budget", "config.example.json")
TODAY = "2026-10-29"
NOW = "2026-10-29 09:41"
# The config.json shape, key for key.
CONTRACT_CFG = {
    "capital": 100000, "cash_buffer_pct": 10,
    "max_loss_per_trade": 500, "daily_loss_limit": 1500, "max_trades_per_day": 3,
    "min_reward_risk": 1.5, "position_cap_pct": 25, "max_order_value": 50000,
    "include_charges": True,
    "no_entries_before": "09:30", "no_entries_after": "14:30", "flat_by": "15:05",
    "product": "INTRADAY", "exchange": "NSE",
    "allow_leverage": False, "allow_short": False, "allow_fno": False,
    "blocked_symbols": [], "mode": "paper", "data_source": "auto",
    "extra_holidays": {}, "special_sessions": {},
    "broker": "dhan", "static_ip": "", "stop_limit_buffer_ticks": 5, "exit_limit_ticks": 5,
    "exit_reprice_max": 5,
}
ORDER_KEYS = {"symbol", "exchange", "series", "security_id", "side", "qty", "entry", "stop", "target", "tick",
              "product", "order_type", "flat_by", "value", "max_loss", "charges_at_stop", "max_loss_all_in",
              "reward_at_target", "reward_risk", "decided_by", "dhan"}
BODY_KEYS = {"transactionType", "exchangeSegment", "productType", "orderType", "securityId", "quantity", "price",
             "targetPrice", "stopLossPrice", "trailingJump", "correlationId"}


def plan(**over):
    with open(PLAN_PATH) as fh:
        p = json.load(fh)
    p.update(over)
    return p


def cfg(**over):
    c = dict(CONTRACT_CFG)
    c.update(over)
    return c


def run(argv, env_dir):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, {"TRADER_DIR": env_dir}), redirect_stdout(out), redirect_stderr(err):
        code = size.main(argv)
    return code, out.getvalue(), err.getvalue()


class Sizing(unittest.TestCase):
    """RELIANCE ORB on 2026-10-29 (real 1-minute bars): entry 1174.30, stop 1163.30, target 1196.30."""

    def test_config_example_is_the_contract(self):
        with open(CFG_PATH) as fh:
            self.assertEqual(json.load(fh), CONTRACT_CFG)

    def test_defaults_position_cap_decides(self):
        s = size.size_trade(plan(), cfg(), SCRIP, TODAY)
        self.assertEqual((s["qty"], s["decided_by"]), (21, "position_cap_pct"))
        self.assertEqual([(l["key"], l["shares"]) for l in s["limits"]],
                         [("max_loss_per_trade", 40), ("position_cap_pct", 21), ("max_order_value", 42), ("cash", 76)])
        self.assertEqual((s["value"], s["max_loss"], s["risk_per_share"], s["reward_risk"]), (24660.3, 231.0, 11.0, 2.0))
        ch = nse.round_trip_cost(21, 1174.3, 1163.3)
        self.assertEqual(s["charges_at_stop"], ch)
        self.assertEqual(s["max_loss_all_in"], round(231.0 + ch, 2))
        self.assertIsNone(s["refusal"])

    def test_max_loss_includes_charges_at_the_stop(self):
        s = size.size_trade(plan(), cfg(position_cap_pct=100, max_order_value=10 ** 7), SCRIP, TODAY)
        self.assertEqual((s["qty"], s["decided_by"]), (40, "max_loss_per_trade"))
        self.assertLessEqual(s["max_loss_all_in"], 500)
        self.assertGreater(size.loss_all_in(41, 1174.3, 1163.3)[2], 500)     # one more share breaks it
        # without charges the same Rs 500 buys 45 shares (45 x 11 = 495)
        s = size.size_trade(plan(), cfg(position_cap_pct=100, max_order_value=10 ** 7, include_charges=False),
                            SCRIP, TODAY)
        self.assertEqual(s["qty"], 45)

    def test_each_limit_can_decide(self):
        big = dict(position_cap_pct=100, max_order_value=10 ** 7, max_loss_per_trade=10 ** 6)
        self.assertEqual(size.size_trade(plan(), cfg(**dict(big, max_order_value=20000)), SCRIP, TODAY)["decided_by"],
                         "max_order_value")
        s = size.size_trade(plan(), cfg(**dict(big, capital=20000)), SCRIP, TODAY)
        self.assertEqual((s["decided_by"], s["qty"]), ("cash", 15))         # 18,000 / 1174.30
        s = size.size_trade(plan(), cfg(**big), SCRIP, TODAY, cash=5000)
        self.assertEqual((s["decided_by"], s["qty"]), ("cash", 0))          # 5,000 - 10,000 buffer

    def test_stop_too_far_is_a_refusal_not_a_smaller_order(self):
        s = size.size_trade(plan(), cfg(max_loss_per_trade=10), SCRIP, TODAY)
        self.assertEqual(s["qty"], 0)
        self.assertIn("one share risks", s["refusal"])
        self.assertIn("No order", s["refusal"])
        self.assertIsNone(size.make_order(s))
        self.assertEqual(s["stop"], 1163.3)                                 # the stop was not moved

    def test_reward_risk_under_minimum_refused(self):
        s = size.size_trade(plan(target=1185.3), cfg(min_reward_risk=1.5), SCRIP, TODAY)
        self.assertIn("reward:risk 1.00", s["refusal"])

    def test_scrip_master_series_and_symbol(self):
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(symbol="KAYA"), cfg(), SCRIP, TODAY)
        self.assertIn("trade-to-trade", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(symbol="NOSUCHCO"), cfg(), SCRIP, TODAY)
        self.assertIn("not in Dhan", str(e.exception))
        with self.assertRaises(size.SizeError):
            size.size_trade(plan(date="2026-10-28"), cfg(), SCRIP, TODAY)
        with self.assertRaises(size.SizeError):
            size.size_trade(plan(side="short"), cfg(), SCRIP, TODAY)
        with self.assertRaises(size.SizeError):
            size.size_trade(plan(), cfg(blocked_symbols=["reliance"]), SCRIP, TODAY)
        with self.assertRaises(size.SizeError):
            size.size_trade(plan(), cfg(product="MARGIN"), SCRIP, TODAY)

    def test_ticks_from_dhan(self):
        s = size.size_trade(plan(entry=1174.33, stop=1163.37, target=1196.39), cfg(), SCRIP, TODAY)
        self.assertEqual((s["entry"], s["stop"], s["target"], s["tick"]), (1174.4, 1163.3, 1196.3, 0.1))
        self.assertEqual(len(s["notes"]), 3)

    def test_market_order_sized_at_a_fresh_higher_price(self):
        s = size.size_trade(plan(), cfg(position_cap_pct=100, max_order_value=10 ** 7), SCRIP, TODAY,
                            market=True, price=1176.3)
        self.assertEqual((s["entry"], s["entry_from"], s["risk_per_share"], s["order_type"]), (1176.3, "price", 13.0, "MARKET"))
        o = size.make_order(s)
        self.assertEqual(o["dhan"]["body"]["price"], 0)
        doc = {"prices": {"RELIANCE": 1176.3}, "detail": {"RELIANCE": {"time": "2026-10-29T04:10:00Z"}}}
        now = nse.parse_now(NOW)
        self.assertEqual(size.fresh_price(doc, "RELIANCE", now)[2], True)    # 09:40 IST
        self.assertEqual(size.fresh_price(doc, "RELIANCE", nse.parse_now("2026-10-29 10:00"))[2], False)

    def test_order_is_the_contract_shape(self):
        o = size.make_order(size.size_trade(plan(), cfg(), SCRIP, TODAY))
        self.assertTrue(ORDER_KEYS <= set(o))
        self.assertEqual(o["dhan"]["endpoint"], "POST /v2/super/orders")
        self.assertEqual(set(o["dhan"]["body"]), BODY_KEYS)
        self.assertEqual(o["dhan"]["body"], {
            "transactionType": "BUY", "exchangeSegment": "NSE_EQ", "productType": "INTRADAY", "orderType": "LIMIT",
            "securityId": "2885", "quantity": 21, "price": 1174.3, "targetPrice": 1196.3, "stopLossPrice": 1163.3,
            "trailingJump": 0, "correlationId": "india-RELIANCE-20261029-1"})
        self.assertNotIn("dhanClientId", json.dumps(o))
        self.assertEqual((o["series"], o["security_id"], o["flat_by"], o["side"], o["qty"]), ("EQ", "2885", "15:05", "buy", 21))

    def test_correlation_id_fits_30(self):
        self.assertEqual(size.correlation_id("ABCDEFGHIJKLMNOPQRST", "2026-10-29", 12), "india-ABCDEFGHIJKL-20261029-12")
        self.assertLessEqual(len(size.correlation_id("M&M", "2026-10-29", 1)), 30)
        self.assertEqual(size.correlation_id("M&M", "2026-10-29", 1), "india-MM-20261029-1")

    def test_cli_writes_one_order(self):
        with tempfile.TemporaryDirectory() as d:
            code, text, err = run(["--plan", PLAN_PATH, "--scrip-offline", SCRIP_CSV, "--now", NOW, "--write"], d)
            self.assertEqual(code, 0, err)
            self.assertIn("Size: 21 shares of RELIANCE = Rs 24,660.30", text)
            self.assertIn("<- decides", text)
            self.assertIn("Not financial advice", text)
            self.assertIn('"yes"', text)
            with open(os.path.join(d, "orders.json")) as fh:
                doc = json.load(fh)
            self.assertEqual(doc["made"], "2026-10-29T09:41:00+05:30")
            self.assertEqual(len(doc["orders"]), 1)

    def test_cli_refusal_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "config.json"), "w") as fh:
                json.dump({"max_loss_per_trade": 10}, fh)
            code, text, _ = run(["--plan", PLAN_PATH, "--scrip-offline", SCRIP_CSV, "--now", NOW, "--write"], d)
            self.assertEqual(code, 1)
            self.assertIn("No order: one share risks", text)
            self.assertFalse(os.path.exists(os.path.join(d, "orders.json")))
            code, _, err = run(["--plan", PLAN_PATH, "--scrip-offline", SCRIP_CSV, "--now", "2026-10-30 09:41"], d)
            self.assertEqual(code, 1)
            self.assertIn("make a new plan", err)


if __name__ == "__main__":
    unittest.main()
