"""india-broker: tickets, the yes, paper fills and Dhan.

Dhan responses below are BUILT from the shapes on dhanhq.co/docs/v2 (super-order, orders,
portfolio, funds, authentication, annexure), not recorded: there are no keys in this repo.
"""

import io
import json
import os
import shutil
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
PLAN_PATH = os.path.join(FIX, "india-budget", "plan-reliance.json")
BARS = os.path.join(COMMON, "yahoo-RELIANCE.NS-1d1m.json")
NOW = "2026-10-29 09:41"
CID, TOKEN = "1100012345", "eyJhbGciOiJIUzUxMiJ9.fake-test-token.sig"
KEYS = {"DHAN_CLIENT_ID": CID, "DHAN_ACCESS_TOKEN": TOKEN}
NOKEYS = {"DHAN_CLIENT_ID": "", "DHAN_ACCESS_TOKEN": ""}


class FakeDhan(object):
    """Stands in for nse.http_get: records every request, answers from a table of documented shapes."""

    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, url, headers=None, data=None, method=None, **kw):
        path = url.split("/v2", 1)[1]
        self.calls.append({"method": method, "path": path, "headers": dict(headers or {}),
                           "body": json.loads(json.dumps(data)) if data is not None else None})
        r = self.table[(method, path)]
        if isinstance(r, Exception):
            raise r
        return json.dumps(r).encode()


SUPER_PLACED = {"orderId": "112111182198", "orderStatus": "PENDING"}
SUPER_LIST = [{"dhanClientId": CID, "orderId": "112111182198", "correlationId": "india-RELIANCE-20261029-1",
               "orderStatus": "TRADED", "transactionType": "BUY", "exchangeSegment": "NSE_EQ",
               "productType": "INTRADAY", "orderType": "LIMIT", "validity": "DAY", "tradingSymbol": "RELIANCE",
               "securityId": "2885", "quantity": 21, "remainingQuantity": 0, "ltp": 1170.1, "price": 1174.3,
               "afterMarketOrder": False, "legName": "ENTRY_LEG", "trailingJump": 0, "exchangeOrderId": "1",
               "createTime": "2026-10-29 09:41:02", "updateTime": "2026-10-29 09:41:03",
               "exchangeTime": "2026-10-29 09:41:03", "omsErrorDescription": "", "averageTradedPrice": 1173.4,
               "filledQty": 21,
               "legDetails": [
                   {"orderId": "112111182199", "legName": "STOP_LOSS_LEG", "transactionType": "SELL",
                    "totalQuatity": 21, "remainingQuantity": 21, "triggeredQuantity": 0, "price": 1163.3,
                    "orderStatus": "PENDING", "trailingJump": 0},
                   {"orderId": "112111182200", "legName": "TARGET_LEG", "transactionType": "SELL",
                    "totalQuatity": 21, "remainingQuantity": 21, "triggeredQuantity": 0, "price": 1196.3,
                    "orderStatus": "PENDING", "trailingJump": 0}]}]
POSITIONS = [{"dhanClientId": CID, "tradingSymbol": "RELIANCE", "securityId": "2885", "positionType": "LONG",
              "exchangeSegment": "NSE_EQ", "productType": "INTRADAY", "buyAvg": 1173.4, "buyQty": 21,
              "costPrice": 1173.4, "sellAvg": 0, "sellQty": 0, "netQty": 21, "realizedProfit": 0,
              "unrealizedProfit": -69.3, "multiplier": 1}]


def run(argv, env):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env), redirect_stdout(out), redirect_stderr(err):
        mod = {"size": size, "check": check}.get(argv[0])
        code = mod.main(argv[1:]) if mod else dhan.main(argv)
    return code, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    mode = "paper"

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.env = dict(NOKEYS, TRADER_DIR=self.d)
        nse.write_json(self.p("config.json"), {"mode": self.mode})
        dhan.TRANSPORT = None

    def tearDown(self):
        dhan.TRANSPORT = None
        shutil.rmtree(self.d)

    def p(self, name):
        return os.path.join(self.d, name)

    def raw(self, name):
        with open(self.p(name)) as fh:
            return fh.read()

    def raw_plan(self):
        with open(PLAN_PATH) as fh:
            return json.load(fh)

    def doc(self, name):
        with open(self.p(name)) as fh:
            return json.load(fh)

    def go(self, *argv, **kw):
        env = dict(self.env, **kw.pop("env", {}))
        return run(list(argv), env)

    def prepare(self, now=NOW):
        self.assertEqual(self.go("size", "--plan", PLAN_PATH, "--scrip-offline", SCRIP_CSV, "--now", now, "--write")[0], 0)
        self.assertEqual(self.go("check", "--scrip-offline", SCRIP_CSV, "--now", now, "--no-quote")[0], 0)


class PaperChain(Base):
    def test_plan_to_orders_to_pass_to_yes_to_send_to_fill(self):
        self.prepare()
        code, text, _ = self.go("show", "--now", NOW)
        self.assertEqual(code, 0)
        self.assertIn("ORDER 1 of 1, PAPER", text)
        self.assertIn("buy 21 RELIANCE at Rs 1,174.30 limit, stop Rs 1,163.30, target Rs 1,196.30, "
                      "max loss ~ Rs 257 incl. charges, flat by 15:05", text)
        self.assertIn("READY: india-risk PASS at 09:41", text)
        self.assertIn('"securityId": "2885"', text)

        code, text, _ = self.go("approve", "--index", "1", "--yes", "sure", "--now", NOW)
        self.assertEqual(code, 1)
        self.assertIn("Not recorded", text)
        code, text, _ = self.go("approve", "--index", "1", "--yes", "yes buy 50 RELIANCE", "--now", NOW)
        self.assertEqual(code, 1)
        self.assertIn("the reply says 50", text)
        code, text, _ = self.go("approve", "--index", "1", "--yes", "yes", "--now", NOW)
        self.assertEqual(code, 0, text)

        code, text, err = self.go("send", "--index", "1", "--now", "2026-10-29 09:42", "--bars", BARS)
        self.assertEqual(code, 0, err)
        self.assertIn("Nothing went to Dhan", text)
        self.assertIn("RELIANCE bought 21 at 1173.50", text)
        code, _, err = self.go("send", "--index", "1", "--now", "2026-10-29 09:43", "--bars", BARS)
        self.assertEqual(code, 1)
        self.assertIn("last: sent", err)                     # one yes, one send

        code, text, _ = self.go("paper-check", "--now", "2026-10-29 15:20", "--bars", BARS)
        self.assertEqual(code, 0)
        self.assertIn("flat-by 15:05", text)
        pp = self.doc("paper.json")
        o = pp["orders"][0]
        self.assertEqual((o["status"], o["fill_price"], o["exit_price"]), ("CLOSED", 1173.5, 1169.3))
        ch = nse.charges(21 * 1173.5, 21 * 1169.3)["total"]
        self.assertEqual(o["pnl"], round(21 * (1169.3 - 1173.5) - ch, 2))
        st = self.doc("broker.json")
        self.assertEqual([r["status"] for r in st["orders"]], ["approved", "sent", "filled", "closed"])
        self.assertEqual(st["orders"][0]["reply"], "yes")
        self.assertEqual(st["days"]["2026-10-29"]["entries_sent"], 1)
        self.assertEqual(st["days"]["2026-10-29"]["realised"], o["pnl"])

        # india-risk now counts it: the same entry again is a duplicate
        res = check.check([size.make_order(size.size_trade(self.raw_plan(), size.DEFAULTS,
                                                           nse.scrip_master(offline=SCRIP_CSV), "2026-10-29"))],
                          {}, nse.scrip_master(offline=SCRIP_CSV), nse.parse_now("2026-10-29 10:30"),
                          broker=st, paper=pp)
        self.assertIn("already sent today", " ".join(res["results"][0]["reasons"]))
        self.assertEqual(res["today"]["entries_sent"], 1)

    def test_stop_is_hit_in_the_bars(self):
        self.prepare()
        # same order, stop moved up to 1172.0 by hand would change the fingerprint; instead simulate directly
        pp = {"cash": 100000.0, "orders": []}
        o = {"id": "P-1", "symbol": "RELIANCE", "qty": 21, "order_type": "LIMIT", "entry": 1174.3, "stop": 1172.0,
             "target": 1196.3, "flat_by": "15:05", "status": "PENDING", "placed_at": "2026-10-29T09:41:00+05:30"}
        _, bars = nse.parse_yahoo_chart(nse.read_maybe_gz(BARS))
        ev = []
        dhan.simulate(o, bars, nse.parse_now("2026-10-29 15:20"), pp, ev)
        self.assertEqual((o["status"], o["exit_reason"]), ("CLOSED", "stop"))
        self.assertLessEqual(o["exit_price"], 1172.0)

    def test_flatten_gets_out_after_the_window(self):
        self.prepare()
        self.go("approve", "--index", "1", "--yes", "yes buy 21 RELIANCE", "--now", NOW)
        self.go("send", "--index", "1", "--now", NOW, "--bars", BARS)
        late = "2026-10-29 14:50"
        code, text, _ = self.go("flatten", "--symbol", "RELIANCE", "--now", late, "--bars", BARS,
                                "--scrip-offline", SCRIP_CSV)
        self.assertEqual(code, 0)
        # a marketable LIMIT, never MARKET: the 14:49 close 1167.70 minus 5 ticks of 0.10
        self.assertIn("sell 21 RELIANCE at Rs 1,167.20 limit (marketable, intraday)", text)
        self.assertIn("last 1167.70 minus 5 ticks", text)
        order = self.doc("orders.json")["orders"][0]
        self.assertEqual((order["order_type"], order["limit"]), ("LIMIT", 1167.2))
        self.assertEqual(order["dhan"], {"endpoint": "POST /v2/orders", "body": {
            "transactionType": "SELL", "exchangeSegment": "NSE_EQ", "productType": "INTRADAY", "orderType": "LIMIT",
            "validity": "DAY", "securityId": "2885", "quantity": 21, "price": 1167.2,
            "correlationId": "india-flat-RELIANCE-1450"}})
        code, text, _ = self.go("check", "--scrip-offline", SCRIP_CSV, "--now", late, "--no-quote")
        self.assertEqual(code, 0, text)                    # 14:50 is after the 14:30 entry cutoff: still passes
        self.assertIn("exit", text)
        self.go("show", "--now", late)
        self.assertEqual(self.go("approve", "--index", "1", "--yes", "yes sell 21 RELIANCE", "--now", late)[0], 0)
        code, text, err = self.go("send", "--index", "1", "--now", late, "--bars", BARS)
        self.assertEqual(code, 0, err)
        o = self.doc("paper.json")["orders"][0]
        self.assertEqual((o["status"], o["exit_price"]), ("CLOSED", 1167.7))
        self.assertTrue(o["exit_reason"].startswith("flatten"))

    def test_exit_needs_no_risk_pass_but_still_a_yes(self):
        self.prepare()
        self.go("approve", "--index", "1", "--yes", "yes buy 21 RELIANCE", "--now", NOW)
        self.go("send", "--index", "1", "--now", NOW, "--bars", BARS)
        late = "2026-10-29 15:04"
        self.go("flatten", "--symbol", "RELIANCE", "--now", late, "--offline", "--price", "RELIANCE=1166",
                "--scrip-offline", SCRIP_CSV)
        code, text, _ = self.go("show", "--now", late)
        self.assertIn("READY (exit", text)
        self.assertEqual(self.go("send", "--index", "1", "--now", late, "--offline", "--price", "1166")[0], 1)
        self.assertEqual(self.go("approve", "--index", "1", "--yes", "yes", "--now", late)[0], 0)
        self.assertEqual(self.go("send", "--index", "1", "--now", late, "--offline", "--price", "1166")[0], 0)


class Refusals(Base):
    def test_no_pass_no_ticket(self):
        self.assertEqual(self.go("size", "--plan", PLAN_PATH, "--scrip-offline", SCRIP_CSV, "--now", NOW, "--write")[0], 0)
        code, text, _ = self.go("show", "--now", NOW)
        self.assertEqual(code, 1)
        self.assertIn("NOT READY: india-risk has not checked", text)
        code, _, err = self.go("approve", "--index", "1", "--yes", "yes", "--now", NOW)
        self.assertIn("not ready", err)
        code, _, err = self.go("send", "--index", "1", "--now", NOW, "--offline")
        self.assertEqual(code, 1)
        self.assertIn("not sent", err)

    def test_blocked_order_is_not_ready(self):
        self.prepare()
        self.go("check", "--scrip-offline", SCRIP_CSV, "--now", "2026-10-29 14:45", "--no-quote")
        code, text, _ = self.go("show", "--now", "2026-10-29 14:45")
        self.assertIn("BLOCKED by india-risk: window", text)

    def test_yesterdays_check_does_not_count(self):
        self.prepare()
        code, text, _ = self.go("show", "--now", "2026-10-30 09:41")
        self.assertIn("not today", text)

    def test_no_yes_and_stale_yes(self):
        self.prepare()
        code, _, err = self.go("send", "--index", "1", "--now", NOW, "--offline")
        self.assertIn("no yes recorded", err)
        self.go("approve", "--index", "1", "--yes", "yes buy 21 RELIANCE", "--now", NOW)
        code, _, err = self.go("send", "--index", "1", "--now", "2026-10-29 09:55", "--offline")
        self.assertEqual(code, 1)
        self.assertIn("stale", err)
        self.assertFalse(os.path.exists(self.p("paper.json")))

    def test_a_bare_yes_needs_the_ticket_shown(self):
        self.prepare()
        code, text, _ = self.go("approve", "--index", "1", "--yes", "yes", "--now", NOW)
        self.assertEqual(code, 1)
        self.assertIn("show it first", text)
        self.assertEqual(self.go("approve", "--index", "1", "--yes", "yes buy 21 RELIANCE", "--now", NOW)[0], 0)

    def test_parse_yes(self):
        o = {"symbol": "RELIANCE", "qty": 21, "entry": 1174.3, "stop": 1163.3, "target": 1196.3,
             "max_loss": 231.0, "max_loss_all_in": 257.22, "side": "buy", "order_type": "LIMIT", "flat_by": "15:05"}
        ok = lambda r, shown=True: dhan.parse_yes(r, o, shown, ["RELIANCE", "TCS"])[0]  # noqa: E731
        self.assertTrue(ok("yes"))
        self.assertTrue(ok("Yes, buy 21 RELIANCE at 1174.30", shown=False))
        self.assertTrue(ok("y"))
        for bad in ("sure", "ok go", "yes to all", "yes but wait", "yes?", "yes buy 20 RELIANCE",
                    "yes buy 21 TCS", "", "no", "yes buy 21 RELIANCE at 1180"):
            self.assertFalse(ok(bad), bad)
        self.assertFalse(ok("yes", shown=False))

    def test_decline(self):
        self.prepare()
        self.assertEqual(self.go("decline", "--index", "1", "--now", NOW)[0], 0)
        code, text, _ = self.go("show", "--now", NOW)
        self.assertIn("DONE: declined", text)


class LiveGates(Base):
    def test_live_flag_without_live_config(self):
        self.prepare()
        self.go("approve", "--index", "1", "--yes", "yes buy 21 RELIANCE", "--now", NOW)
        code, _, err = self.go("send", "--index", "1", "--live", "--now", NOW, env=KEYS)
        self.assertEqual(code, 1)
        self.assertIn("set \"mode\": \"live\" in the config yourself", err)

    def test_live_config_without_flag_or_keys(self):
        nse.write_json(self.p("config.json"), {"mode": "live"})
        self.prepare()
        self.go("approve", "--index", "1", "--yes", "yes buy 21 RELIANCE", "--now", NOW)
        code, _, err = self.go("send", "--index", "1", "--now", NOW, env=KEYS)
        self.assertIn("needs --live", err)
        code, _, err = self.go("send", "--index", "1", "--now", NOW, "--live")
        self.assertIn("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN", err)
        st = self.doc("broker.json")
        self.assertEqual([r["status"] for r in st["orders"]], ["approved"])


class Live(Base):
    mode = "live"

    def ready(self):
        self.prepare()
        self.go("show", "--now", NOW)
        self.assertEqual(self.go("approve", "--index", "1", "--yes", "yes", "--now", NOW)[0], 0)

    def test_send_posts_the_super_order(self):
        self.ready()
        fake = FakeDhan({("POST", "/super/orders"): SUPER_PLACED})
        dhan.TRANSPORT = fake
        code, text, err = self.go("send", "--index", "1", "--live", "--now", NOW, env=KEYS)
        self.assertEqual(code, 0, err)
        self.assertIn("LIVE: Dhan order 112111182198, status PENDING", text)
        call = fake.calls[0]
        self.assertEqual((call["method"], call["path"]), ("POST", "/super/orders"))
        self.assertEqual(call["headers"]["access-token"], TOKEN)
        self.assertEqual(call["headers"]["client-id"], CID)
        self.assertEqual(call["body"], dict(self.doc("orders.json")["orders"][0]["dhan"]["body"], dhanClientId=CID))
        blob = self.raw("broker.json") + self.raw("orders.json") + text + err
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn(CID, blob)
        st = self.doc("broker.json")
        rec = st["orders"][-1]
        self.assertEqual((rec["status"], rec["mode"], rec["broker_order_id"]), ("sent", "live", "112111182198"))
        self.assertEqual(rec["request"]["path"], "/v2/super/orders")
        self.assertEqual(st["days"]["2026-10-29"]["risk_used"], rec["max_loss_all_in"])

    def test_token_expired(self):
        self.ready()
        err_body = ('{"errorType":"Invalid_Authentication","errorCode":"DH-901",'
                    '"errorMessage":"Client ID or user generated access token is invalid or expired."}')
        dhan.TRANSPORT = FakeDhan({("POST", "/super/orders"): nse.NSEError("HTTP 401 from api.dhan.co " + err_body)})
        code, text, err = self.go("send", "--index", "1", "--live", "--now", NOW, env=KEYS)
        self.assertEqual(code, 1)
        self.assertIn("web.dhan.co", err)
        self.assertIn("export DHAN_ACCESS_TOKEN", err)
        self.assertNotIn(TOKEN, err + text)
        self.assertEqual(self.doc("broker.json")["orders"][-1]["status"], "failed")

    def test_order_error_and_rate_limit_and_static_ip(self):
        for errtext, want in (
                ("Dhan DH-906: Market is Closed! Want to place an offline order?", "DH-906"),
                ("Dhan DH-905: Missing required fields, bad values for parameters etc.", "DH-905"),
                ("HTTP 429 from api.dhan.co", "rate limit"),
                ("HTTP 403 from api.dhan.co Static IP not whitelisted", "static IP")):
            self.assertIn(want, dhan.dhan_error(nse.NSEError(errtext)))
        self.ready()
        dhan.TRANSPORT = FakeDhan({("POST", "/super/orders"): nse.NSEError(
            "Dhan DH-906: Order price is outside the circuit limit")})
        code, _, err = self.go("send", "--index", "1", "--live", "--now", NOW, env=KEYS)
        self.assertIn("NOT placed", err)

    def test_no_answer_is_unknown_and_blocks_a_resend(self):
        self.ready()
        dhan.TRANSPORT = FakeDhan({("POST", "/super/orders"): nse.NSEError("could not reach api.dhan.co: timed out")})
        code, _, err = self.go("send", "--index", "1", "--live", "--now", NOW, env=KEYS)
        self.assertIn("MAY have reached Dhan", err)
        code, text, _ = self.go("show", "--now", NOW)
        self.assertIn("UNKNOWN", text)
        code, _, err = self.go("send", "--index", "1", "--live", "--now", NOW, env=KEYS)
        self.assertIn("last: unknown", err)

    def test_flatten_cancels_legs_then_sells_the_net_qty(self):
        fake = FakeDhan({("GET", "/positions"): POSITIONS, ("GET", "/super/orders"): SUPER_LIST,
                         ("DELETE", "/super/orders/112111182198/STOP_LOSS_LEG"):
                             {"orderId": "112111182198", "orderStatus": "CANCELLED"},
                         ("DELETE", "/super/orders/112111182198/TARGET_LEG"):
                             {"orderId": "112111182198", "orderStatus": "CANCELLED"},
                         ("POST", "/orders"): {"orderId": "112111182300", "orderStatus": "TRANSIT"},
                         ("GET", "/orders/112111182300"): {"orderId": "112111182300", "orderStatus": "TRADED",
                                                           "filledQty": 21, "quantity": 21}})
        dhan.TRANSPORT = fake
        late = "2026-10-29 15:04"
        code, text, err = self.go("flatten", "--symbol", "RELIANCE", "--now", late, "--price", "RELIANCE=1170.1",
                                  "--scrip-offline", SCRIP_CSV, env=KEYS)
        self.assertEqual(code, 0, err)
        self.assertIn("at Rs 1,169.60 limit", text)
        code, text, err = self.go("check", "--scrip-offline", SCRIP_CSV, "--now", late, "--no-quote", env=KEYS)
        self.assertEqual(code, 0, text + err)                   # held per the positions snapshot: an exit
        self.go("show", "--now", late)
        self.assertEqual(self.go("approve", "--index", "1", "--yes", "yes sell 21 RELIANCE", "--now", late)[0], 0)
        code, text, err = self.go("send", "--index", "1", "--live", "--now", late, env=KEYS)
        self.assertEqual(code, 0, err)
        self.assertEqual([(c["method"], c["path"]) for c in fake.calls],
                         [("GET", "/positions"), ("GET", "/super/orders"),
                          ("DELETE", "/super/orders/112111182198/STOP_LOSS_LEG"),
                          ("DELETE", "/super/orders/112111182198/TARGET_LEG"),
                          ("GET", "/positions"), ("POST", "/orders"), ("GET", "/orders/112111182300")])
        self.assertEqual(fake.calls[-2]["body"], {
            "transactionType": "SELL", "exchangeSegment": "NSE_EQ", "productType": "INTRADAY", "orderType": "LIMIT",
            "validity": "DAY", "securityId": "2885", "quantity": 21, "price": 1169.6,
            "correlationId": "india-flat-RELIANCE-1504", "dhanClientId": CID})
        self.assertIn("sell 21 RELIANCE at 1169.60 limit", text)
        self.assertNotIn(CID, self.raw("broker.json"))

    def test_read_only_commands(self):
        fake = FakeDhan({("GET", "/ip/getIP"): {"modifyDateSecondary": "2026-11-05", "secondaryIP": "",
                                                "modifyDatePrimary": "2026-11-05", "primaryIP": "203.0.113.7"},
                         ("GET", "/profile"): {"dhanClientId": CID, "tokenValidity": "30/10/2026 09:30",
                                               "activeSegment": "Equity, Derivative", "ddpi": "Active",
                                               "mtf": "Deactive", "dataPlan": "Active",
                                               "dataValidity": "2026-11-28 23:59:59.0"},
                         ("GET", "/fundlimit"): {"dhanClientId": CID, "availabelBalance": 98440.0, "sodLimit": 113642,
                                                 "collateralAmount": 0, "receiveableAmount": 0, "utilizedAmount": 15202,
                                                 "blockedPayoutAmount": 0, "withdrawableBalance": 98310}})
        dhan.TRANSPORT = fake
        code, text, _ = self.go("ip", env=KEYS)
        self.assertIn("primary 203.0.113.7", text)
        code, text, _ = self.go("profile", env=KEYS)
        self.assertIn("Token valid until 30/10/2026 09:30. Data plan Active", text)
        code, text, _ = self.go("funds", env=KEYS)
        self.assertIn("Available balance: Rs 98,440.00", text)
        for out in (text,):
            self.assertNotIn(CID, out)
        self.assertNotIn(CID, self.raw("broker.json"))


if __name__ == "__main__":
    unittest.main()
