"""Angel One (SmartAPI): the client helpers, india-budget's angel block, india-risk's Angel checks and
india-broker's angel_broker.py, offline.

Every Angel One response below is BUILT, not recorded: the routes, headers and placeOrder body are the ones
in Angel's official SDK (github.com/angel-one/smartapi-python, SmartApi/smartConnect.py and its README:
placeOrder answers data.orderid / data.uniqueorderid; cancelOrder takes variety + orderid), wrapped in the
envelope the SDK reads ({"status": true, "message": "SUCCESS", "errorcode": "", "data": ...}). Order-book,
position and quote field names (orderid, status, filledshares, averageprice, symboltoken, netqty, fetched /
symbolToken / ltp) follow Angel's SmartAPI docs and are NOT in the SDK: unverified. No real order is placed.
The scrip-master fixture is a real cut of OpenAPIScripMaster.json (2026-10-29).
"""

import copy
import datetime
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
import angel

size = load("india-budget", "size")
check = load("india-risk", "check")
dhan = load("india-broker", "dhan")
ab = load("india-broker", "angel_broker")

SCRIP_JSON = os.path.join(FIX, "india-broker", "angel-scrip-sample.json")
SCRIP = angel.scrip_master(offline=SCRIP_JSON)
DHAN_SCRIP = nse.scrip_master(offline=os.path.join(COMMON, "dhan-scrip-sample.csv"))
PLAN_PATH = os.path.join(FIX, "india-budget", "plan-reliance.json")
BARS = os.path.join(COMMON, "yahoo-RELIANCE.NS-1d1m.json")
TODAY = "2026-10-29"
NOW = "2026-10-29 09:41"

API_KEY, CLIENT, PIN, SECRET = "ak-Fake-Api-Key-7Q2", "A1234567", "8642", "JBSWY3DPEHPK3PXP"
JWT, FEED = "eyJhbGciOiJIUzUxMiJ9.angel-fake-jwt.sig", "feed-0a1b2c3d4e5f"
KEYS = {"ANGEL_API_KEY": API_KEY, "ANGEL_CLIENT_CODE": CLIENT, "ANGEL_PIN": PIN, "ANGEL_TOTP_SECRET": SECRET}
NOKEYS = dict((k, "") for k in KEYS)
SECRETS = (API_KEY, PIN, SECRET, JWT, FEED)


def plan(**over):
    with open(PLAN_PATH) as fh:
        p = json.load(fh)
    p.update(over)
    return p


def angel_order(**cfg):
    c = dict(size.DEFAULTS, broker="angel", **cfg)
    return size.make_order(size.size_trade(plan(), c, SCRIP, TODAY))


def ok(data):
    return {"status": True, "message": "SUCCESS", "errorcode": "", "data": data}


class Reject(Exception):
    """The fake answers {"status": false, ...} for this route."""

    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code, self.message = code, message


class FakeAngel(object):
    """Stands in for nse.http_get: records url / headers / body, and keeps a tiny order book so the
    chain entry -> stop -> exit reads like Angel's order book and positions would. Built, not recorded."""

    def __init__(self, ltp=1170.0, entry_status="complete", fail=None, sell_plan=None, ltps=None):
        self.calls, self.book, self.n = [], [], 0
        self.ltp, self.entry_status = ltp, entry_status
        self.sell_plan = list(sell_plan or [])   # status of each exit sell placed / modified ("complete" when empty)
        self.ltps = list(ltps or [])             # successive last prices (then self.ltp)
        self.fail = dict(fail or {})
        self.by_path = dict((v, k) for k, v in angel.ROUTES.items())

    def __call__(self, url, headers=None, data=None, method=None, **kw):
        route = self.by_path[url[len(angel.ROOT):]]
        self.calls.append({"route": route, "url": url, "method": method, "headers": dict(headers or {}),
                           "body": json.loads(json.dumps(data)) if data is not None else None})
        f = self.fail.get(route)
        if isinstance(f, list):
            f = f.pop(0) if f else None
        if isinstance(f, Reject):
            return json.dumps({"status": False, "message": f.message, "errorcode": f.code, "data": None}).encode()
        if isinstance(f, Exception):
            raise f
        return json.dumps(ok(getattr(self, "r_" + route)(data or {}))).encode()

    def r_login(self, b):
        assert b["clientcode"] == CLIENT and b["password"] == PIN and len(b["totp"]) == 6
        return {"jwtToken": JWT, "refreshToken": "refresh-xyz", "feedToken": FEED}

    def r_place(self, b):
        self.n += 1
        oid = "2610290000%02d" % self.n
        if b["variety"] == "STOPLOSS":
            status, filled = "trigger pending", "0"
        elif b["transactiontype"] == "BUY":
            status = self.entry_status
            filled = b["quantity"] if status == "complete" else "0"
        else:
            status = self.sell_plan.pop(0) if self.sell_plan else "complete"
            filled = b["quantity"] if status == "complete" else "0"
        self.book.append(dict(b, orderid=oid, uniqueorderid="u-" + oid, status=status, orderstatus=status,
                              filledshares=filled, averageprice=b["price"] if filled != "0" else "0", text=""))
        return {"script": b["tradingsymbol"], "orderid": oid, "uniqueorderid": "u-" + oid}

    def r_modify(self, b):
        status = self.sell_plan.pop(0) if self.sell_plan else "complete"
        for r in self.book:
            if r["orderid"] == b["orderid"]:
                r.update(price=b["price"], quantity=b["quantity"], status=status, orderstatus=status,
                         filledshares=b["quantity"] if status == "complete" else "0",
                         averageprice=b["price"] if status == "complete" else "0")
        return {"orderid": b["orderid"], "uniqueorderid": "u-" + b["orderid"]}

    def r_cancel(self, b):
        for r in self.book:
            if r["orderid"] == b["orderid"]:
                r["status"] = r["orderstatus"] = "cancelled"
        return {"orderid": b["orderid"], "uniqueorderid": "u-" + b["orderid"]}

    def r_orders(self, b):
        return copy.deepcopy(self.book)

    def r_trades(self, b):
        return [{"orderid": r["orderid"], "tradingsymbol": r["tradingsymbol"], "transactiontype": r["transactiontype"],
                 "fillsize": r["filledshares"], "fillprice": r["averageprice"], "fillid": "f-" + r["orderid"]}
                for r in self.book if r["filledshares"] != "0"]

    def r_positions(self, b):
        net = {}
        for r in self.book:
            if r["status"] == "complete":
                q = int(r["filledshares"]) * (1 if r["transactiontype"] == "BUY" else -1)
                net[r["symboltoken"]] = (r["tradingsymbol"], net.get(r["symboltoken"], ("", 0))[1] + q)
        return [{"tradingsymbol": ts, "symboltoken": tok, "producttype": "INTRADAY", "exchange": "NSE",
                 "netqty": str(q), "realised": "0", "unrealised": "0"} for tok, (ts, q) in net.items()]

    def r_quote(self, b):
        toks = b["exchangeTokens"]["NSE"]
        if self.ltps:
            self.ltp = self.ltps.pop(0)
        return {"fetched": [{"exchange": "NSE", "tradingSymbol": "RELIANCE-EQ", "symbolToken": t, "ltp": self.ltp}
                            for t in toks], "unfetched": []}

    def r_profile(self, b):
        return {"clientcode": CLIENT, "name": "TEST USER", "email": "x@example.com", "mobileno": "9999999999",
                "exchanges": ["NSE", "BSE"], "products": ["MIS", "CNC"], "lastlogintime": "", "brokerid": "B2C"}

    def r_funds(self, b):
        return {"net": "98440.00", "availablecash": "98440.00", "availableintradaypayin": "0",
                "availablelimitmargin": "0", "utiliseddebits": "0", "utilisedpayout": "0"}

    def places(self):
        return [c["body"] for c in self.calls if c["route"] == "place"]

    def routes(self, name):
        return [c["body"] for c in self.calls if c["route"] == name]


def run(argv, env):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env), redirect_stdout(out), redirect_stderr(err):
        mod = {"size": size, "risk": check}.get(argv[0])        # "risk" = india-risk; "check" = the broker
        code = mod.main(argv[1:]) if mod else ab.main(argv)
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------- the client helpers

class Client(unittest.TestCase):
    def test_totp_rfc6238_sha1_vector(self):
        # RFC 6238 appendix B: the ASCII key "12345678901234567890" in base32, T = 59 s.
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        self.assertEqual(angel.totp(secret, at=59), "287082")
        self.assertEqual(angel.totp(secret, at=59, digits=8), "94287082")
        self.assertEqual(angel.totp(secret.lower(), at=1111111109, digits=8), "07081804")

    def test_parse_scrip(self):
        self.assertEqual(len(SCRIP), 22)                     # 20 -EQ + KAYA-BE + COMPINFO-BZ
        r = SCRIP["RELIANCE"]
        self.assertEqual((r["token"], r["security_id"], r["trading_symbol"], r["series"], r["tick"], r["cas"]),
                         ("2885", "2885", "RELIANCE-EQ", "EQ", 0.1, True))
        self.assertEqual((SCRIP["M&M"]["token"], SCRIP["M&M"]["tick"]), ("2031", 0.1))
        self.assertEqual(SCRIP["IDEA"]["tick"], 0.01)
        self.assertEqual((SCRIP["MRF"]["tick"], SCRIP["MRF"]["cas"]), (5.0, False))
        self.assertEqual((SCRIP["KAYA"]["series"], SCRIP["COMPINFO"]["series"]), ("BE", "BZ"))
        for gone in ("JFLLIFE", "NIFTY", "Nifty 50"):        # SME board, index
            self.assertNotIn(gone, SCRIP)
        # BSE and NFO rows of RELIANCE do not leak into the NSE table
        self.assertEqual(SCRIP["RELIANCE"]["token"], "2885")
        self.assertEqual(angel.parse_scrip(nse.read_maybe_gz(SCRIP_JSON), "BSE"), {})
        # the same token as Dhan's security_id, for every stock in both samples
        for sym in set(SCRIP) & set(DHAN_SCRIP):
            self.assertEqual(SCRIP[sym]["token"], DHAN_SCRIP[sym]["security_id"], sym)

    def test_cache_without_series_still_works(self):
        old = {"RELIANCE": {"token": "2885", "trading_symbol": "RELIANCE-EQ", "tick": 0.1, "lot": 1, "cas": True}}
        self.assertEqual(angel._fill(old)["RELIANCE"]["series"], "EQ")
        self.assertEqual(old["RELIANCE"]["security_id"], "2885")

    def test_candle_bars(self):
        bars = angel.candle_bars([["2026-10-29T09:15:00+05:30", 1159.2, 1162.0, 1156.2, 1160.5, 123456],
                                  ["2026-10-29T03:50:00+00:00", 1160.5, 1163, 1160, 1162.1, None]])
        self.assertEqual(bars[0], {"t": datetime.datetime(2026, 10, 29, 9, 15), "o": 1159.2, "h": 1162.0,
                                   "l": 1156.2, "c": 1160.5, "v": 123456})
        self.assertEqual((bars[1]["t"], bars[1]["v"]), (datetime.datetime(2026, 10, 29, 9, 20), 0))

    def test_order_body_never_market(self):
        b = angel.order_body("SBIN-EQ", 3045, "buy", 1, 19500)
        self.assertEqual(b, {"variety": "NORMAL", "tradingsymbol": "SBIN-EQ", "symboltoken": "3045",
                             "transactiontype": "BUY", "exchange": "NSE", "ordertype": "LIMIT",
                             "producttype": "INTRADAY", "duration": "DAY", "price": "19500.00", "squareoff": "0",
                             "stoploss": "0", "quantity": "1"})
        for kw in ({"ordertype": "MARKET"}, {"ordertype": "STOPLOSS_MARKET", "variety": "STOPLOSS"},
                   {"variety": "ROBO"}):
            with self.assertRaises(angel.AngelError):
                angel.order_body("SBIN-EQ", 3045, "BUY", 1, 100, **kw)
        with self.assertRaises(angel.AngelError):
            angel.check_body(dict(b, duration="IOC"))

    def test_ltps_and_headers(self):
        self.assertEqual(angel.ltps({"fetched": [{"symbolToken": "2885", "ltp": 1170.1}, {"symboltoken": "3045",
                                                                                         "ltp": "571.8"}]}),
                         {"2885": 1170.1, "3045": 571.8})
        fake = FakeAngel()
        c = angel.Angel(API_KEY, CLIENT, PIN, SECRET, transport=fake)
        c.funds()
        login, rms = fake.calls
        self.assertEqual((login["route"], rms["route"], rms["method"]), ("login", "funds", "GET"))
        self.assertEqual(rms["headers"]["X-PrivateKey"], API_KEY)
        self.assertEqual(rms["headers"]["Authorization"], "Bearer " + JWT)
        self.assertNotIn("Authorization", login["headers"])
        self.assertEqual(c._clean("key %s jwt %s pin %s" % (API_KEY, JWT, PIN)), "key *** jwt *** pin ***")


# ---------------------------------------------------------------- india-budget

class Budget(unittest.TestCase):
    def test_angel_block(self):
        o = angel_order()
        self.assertNotIn("dhan", o)
        blk = o["angel"]
        self.assertEqual(blk["route"], "placeOrder")
        self.assertEqual(blk["body"], {"variety": "NORMAL", "tradingsymbol": "RELIANCE-EQ", "symboltoken": "2885",
                                       "transactiontype": "BUY", "exchange": "NSE", "ordertype": "LIMIT",
                                       "producttype": "INTRADAY", "duration": "DAY", "price": "1174.30",
                                       "squareoff": "0", "stoploss": "0", "quantity": "21"})
        # the stop-limit: trigger at the stop, limit 5 ticks of 0.10 under it
        self.assertEqual(blk["stop_order"], dict(blk["body"], variety="STOPLOSS", transactiontype="SELL",
                                                 ordertype="STOPLOSS_LIMIT", price="1162.80", triggerprice="1163.30"))
        for b in (blk["body"], blk["stop_order"]):
            self.assertTrue(all(isinstance(v, str) for v in b.values()))
        self.assertEqual((o["tick"], o["security_id"], o["series"]), (0.1, "2885", "EQ"))

    def test_broker_neutral_fields_are_the_dhan_order(self):
        d = size.make_order(size.size_trade(plan(), size.DEFAULTS, DHAN_SCRIP, TODAY))
        a = angel_order()
        self.assertEqual(dict((k, v) for k, v in d.items() if k != "dhan"),
                         dict((k, v) for k, v in a.items() if k != "angel"))

    def test_stop_limit_buffer_is_configurable(self):
        self.assertEqual(angel_order(stop_limit_buffer_ticks=3)["angel"]["stop_order"]["price"], "1163.00")
        o = size.make_order(size.size_trade(plan(symbol="IDEA", entry=10.52, stop=10.31, target=10.94),
                                            dict(size.DEFAULTS, broker="angel", max_order_value=5000), SCRIP, TODAY))
        self.assertEqual((o["angel"]["stop_order"]["triggerprice"], o["angel"]["stop_order"]["price"]),
                         ("10.31", "10.26"))                    # IDEA's tick is 0.01

    def test_refusals(self):
        c = dict(size.DEFAULTS, broker="angel")
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), c, SCRIP, TODAY, market=True)
        self.assertIn("prohibits MARKET", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(symbol="KAYA"), c, SCRIP, TODAY)
        self.assertIn("trade-to-trade", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(symbol="NOSUCHCO"), c, SCRIP, TODAY)
        self.assertIn("not in Angel One's", str(e.exception))
        with self.assertRaises(size.SizeError):
            size.size_trade(plan(), dict(c, broker="zerodha"), SCRIP, TODAY)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            nse.write_json(os.path.join(d, "config.json"), {"broker": "angel"})
            code, text, err = run(["size", "--plan", PLAN_PATH, "--scrip-offline", SCRIP_JSON, "--now", NOW,
                                   "--write"], dict(NOKEYS, TRADER_DIR=d))
            self.assertEqual(code, 0, err)
            self.assertIn("Angel One token 2885", text)
            self.assertIn("Proposed Angel One orders", text)
            self.assertIn("sells no lower than 1162.80", text)
            self.assertNotIn("Dhan super order", text)
            self.assertIn("Not financial advice", text)
            doc = nse.read_json(os.path.join(d, "orders.json"))
            self.assertEqual(doc["orders"][0]["angel"], angel_order()["angel"])


# ---------------------------------------------------------------- india-risk

def verdict(o, now=NOW, **kw):
    cfg = dict({"broker": "angel"}, **kw.pop("cfg", {}))
    return check.check([o], cfg, SCRIP, nse.parse_now(now), **kw)


def reasons(res):
    return " | ".join(res["results"][0]["reasons"])


class Risk(unittest.TestCase):
    def test_pass_and_fingerprint(self):
        res = verdict(angel_order())
        self.assertEqual((res["pass"], res["block"]), (1, 0), reasons(res))
        self.assertIn("Angel stop-limit: trigger 1163.30, limit 1162.80", " ".join(res["results"][0]["info"]))
        self.assertEqual(res["results"][0]["fingerprint"], ab.fingerprint(angel_order()))
        self.assertEqual(ab.fingerprint(angel_order()), dhan.fingerprint(angel_order()))

    def test_market_and_ioc_blocked(self):
        o = angel_order()
        o["order_type"] = "MARKET"
        o["angel"]["body"]["ordertype"] = "MARKET"
        r = reasons(verdict(o))
        self.assertIn("market orders are prohibited", r)
        self.assertIn("MARKET orders are prohibited", r)
        o = angel_order()
        o["angel"]["body"]["duration"] = "IOC"
        self.assertIn("IOC is prohibited", reasons(verdict(o)))
        o = angel_order()
        o["angel"]["stop_order"]["ordertype"] = "STOPLOSS_MARKET"
        self.assertIn("stop_order is STOPLOSS_MARKET", reasons(verdict(o)))
        o = angel_order()
        o["angel"]["body"]["variety"] = "ROBO"
        self.assertIn("ROBO", reasons(verdict(o)))

    def test_block_must_agree_with_the_order(self):
        o = angel_order()
        o["angel"]["body"]["quantity"] = "22"
        self.assertIn("body quantity is '22'", reasons(verdict(o)))
        o = angel_order()
        o["angel"]["body"]["quantity"] = 21
        self.assertIn("must be a string", reasons(verdict(o)))
        o = angel_order()
        o["angel"]["stop_order"]["triggerprice"] = "1150.00"
        self.assertIn("stop_order triggerprice", reasons(verdict(o)))
        o = angel_order()
        o["angel"]["stop_order"]["price"] = "1162.85"
        self.assertIn("stop-limit price 1162.85 is not on RELIANCE's 0.1 tick", reasons(verdict(o)))
        o = angel_order()
        del o["angel"]["stop_order"]
        self.assertIn("needs its exchange-held stop", reasons(verdict(o)))
        o = angel_order()
        o["angel"]["body"]["tradingsymbol"] = "RELIANCE-BE"
        self.assertIn("tradingsymbol", reasons(verdict(o)))

    def test_token_from_the_scrip_master(self):
        o = angel_order()
        o["security_id"] = "9999"
        o["angel"]["body"]["symboltoken"] = o["angel"]["stop_order"]["symboltoken"] = "9999"
        self.assertIn("RELIANCE has Angel One token 2885, the order says 9999", reasons(verdict(o)))

    def test_wrong_broker_block(self):
        d = size.make_order(size.size_trade(plan(), size.DEFAULTS, DHAN_SCRIP, TODAY))
        self.assertIn("no Angel One request body", reasons(verdict(d)))
        self.assertIn("no Dhan request body", reasons(check.check([angel_order()], {}, DHAN_SCRIP,
                                                                  nse.parse_now(NOW))))

    def test_no_circuit_quote_for_angel(self):
        with mock.patch.dict(os.environ, {"DHAN_CLIENT_ID": "1", "DHAN_ACCESS_TOKEN": "tok.en"}):
            self.assertEqual(check.fetch_circuits([angel_order()], {"broker": "angel"},
                                                  transport=lambda *x, **k: 1 / 0), ({}, None))

    def test_angel_exit_is_never_blocked(self):
        body = angel.order_body("RELIANCE-EQ", "2885", "SELL", 21, 1166.4)
        ex = {"symbol": "RELIANCE", "exchange": "NSE", "series": "EQ", "security_id": "2885", "side": "sell",
              "qty": 21, "product": "INTRADAY", "order_type": "LIMIT", "limit": 1166.4, "flat_by": "15:05",
              "kind": "exit", "angel": {"route": "placeOrder", "body": body}}
        broker = {"snapshots": [{"date": TODAY, "what": "positions", "positions": [
            {"symbol": "RELIANCE", "securityId": "2885", "netQty": 21, "productType": "INTRADAY",
             "realizedProfit": -5000}]}]}
        res = verdict(ex, "2026-10-29 15:20", cfg={"mode": "live", "blocked_symbols": ["RELIANCE"]}, broker=broker)
        self.assertEqual((res["results"][0]["verdict"], res["results"][0]["kind"]), ("PASS", "exit"), reasons(res))
        ex2 = copy.deepcopy(ex)
        ex2["limit"] = 1166.45
        ex2["angel"]["body"]["price"] = "1166.45"
        self.assertIn("limit 1166.45 is not on", " ".join(verdict(ex2, "2026-10-29 15:20", cfg={"mode": "live"},
                                                                  broker=broker)["results"][0]["warnings"]))


# ---------------------------------------------------------------- india-broker: angel_broker.py

class Base(unittest.TestCase):
    mode = "paper"

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.env = dict(NOKEYS, TRADER_DIR=self.d, DHAN_CLIENT_ID="", DHAN_ACCESS_TOKEN="")
        nse.write_json(self.p("config.json"), {"mode": self.mode, "broker": "angel", "static_ip": "203.0.113.7"})
        ab.TRANSPORT = None
        ab._CLIENT[0] = None
        self.outputs = []

    def tearDown(self):
        ab.TRANSPORT = None
        ab._CLIENT[0] = None
        shutil.rmtree(self.d)

    def p(self, name):
        return os.path.join(self.d, name)

    def doc(self, name):
        return nse.read_json(self.p(name))

    def go(self, *argv, **kw):
        code, out, err = run(list(argv), dict(self.env, **kw.pop("env", {})))
        self.outputs.append(out + err)
        return code, out, err

    def prepare(self, now=NOW):
        self.assertEqual(self.go("size", "--plan", PLAN_PATH, "--scrip-offline", SCRIP_JSON, "--now", now,
                                 "--write")[0], 0)
        code, text, err = self.go("risk", "--scrip-offline", SCRIP_JSON, "--now", now, "--no-quote")
        self.assertEqual(code, 0, text + err)

    def assert_no_secrets(self):
        blob = "".join(self.outputs)
        for name in ("broker.json", "orders.json", "risk.json", "paper.json"):
            if os.path.exists(self.p(name)):
                with open(self.p(name)) as fh:
                    blob += fh.read()
        for s in SECRETS:
            self.assertNotIn(s, blob)


class Paper(Base):
    def test_chain_paper(self):
        self.prepare()
        code, text, _ = self.go("show", "--now", NOW)
        self.assertEqual(code, 0)
        self.assertIn("ORDER 1 of 1, PAPER (local simulated ledger, nothing goes to Angel One)", text)
        self.assertIn("Angel One token 2885", text)
        self.assertIn('"ordertype": "STOPLOSS_LIMIT"', text)
        self.assertIn("not placed as a resting sell", text)
        self.assertEqual(self.go("approve", "--index", "1", "--yes", "yes buy 21 RELIANCE", "--now", NOW)[0], 0)
        code, text, err = self.go("send", "--index", "1", "--now", "2026-10-29 09:42", "--bars", BARS)
        self.assertEqual(code, 0, err)
        self.assertIn("Nothing went to Angel One", text)
        self.assertIn("RELIANCE bought 21 at 1173.50", text)
        self.assertEqual(self.go("send", "--index", "1", "--now", "2026-10-29 09:43", "--bars", BARS)[0], 1)
        rec = [r for r in self.doc("broker.json")["orders"] if r["status"] == "sent"][0]
        self.assertEqual(rec["request"]["would_be"], "placeOrder")
        self.assertEqual(rec["request"]["stop_order"]["ordertype"], "STOPLOSS_LIMIT")
        self.assertEqual(self.doc("broker.json")["orders"][0]["exit_auto"], False)   # the default: ask
        code, text, _ = self.go("protect", "--now", "2026-10-29 09:50")
        self.assertIn("PAPER: the paper ledger models the stop", text)
        code, text, _ = self.go("check", "--now", "2026-10-29 15:20", "--bars", BARS)
        self.assertIn("flat-by 15:05", text)
        o = self.doc("paper.json")["orders"][0]
        self.assertEqual((o["status"], o["fill_price"], o["exit_price"], o["broker"]), ("CLOSED", 1173.5, 1169.3, "angel"))
        self.assert_no_secrets()

    def test_flatten_paper_is_a_marketable_limit_and_never_blocked(self):
        self.prepare()
        self.go("approve", "--index", "1", "--yes", "yes buy 21 RELIANCE", "--now", NOW)
        self.go("send", "--index", "1", "--now", NOW, "--bars", BARS)
        late = "2026-10-29 14:50"
        code, text, err = self.go("flatten", "--symbol", "RELIANCE", "--now", late, "--bars", BARS,
                                  "--scrip-offline", SCRIP_JSON)
        self.assertEqual(code, 0, err)
        self.assertIn("sell 21 RELIANCE at Rs 1,167.20 limit (marketable, intraday)", text)
        order = self.doc("orders.json")["orders"][0]
        self.assertEqual(order["angel"]["body"], angel.order_body("RELIANCE-EQ", "2885", "SELL", 21, 1167.2))
        nse.write_json(self.p("config.json"), {"broker": "angel", "blocked_symbols": ["RELIANCE"],
                                               "daily_loss_limit": 1})
        code, text, _ = self.go("risk", "--scrip-offline", SCRIP_JSON, "--now", late, "--no-quote")
        self.assertEqual(code, 0, text)
        self.assertIn("exit", text)
        self.go("show", "--now", late)
        self.assertEqual(self.go("approve", "--index", "1", "--yes", "yes sell 21 RELIANCE", "--now", late)[0], 0)
        code, text, err = self.go("send", "--index", "1", "--now", late, "--bars", BARS)
        self.assertEqual(code, 0, err)
        o = self.doc("paper.json")["orders"][0]
        self.assertEqual((o["status"], o["exit_price"]), ("CLOSED", 1167.7))

    def test_exit_floor(self):
        self.assertEqual(ab.core.marketable_sell(1170.0, 0.1, 5, 1162.8), (1169.5, "last 1170.00 minus 5 ticks of 0.1"))
        price, note = ab.core.marketable_sell(1160.0, 0.1, 5, 1162.8)
        self.assertEqual(price, 1162.8)
        self.assertIn("under your stop-limit floor", note)


class LiveGates(Base):
    def test_live_flag_without_live_config(self):
        self.prepare()
        self.go("approve", "--index", "1", "--yes", "yes buy 21 RELIANCE", "--now", NOW)
        code, _, err = self.go("send", "--index", "1", "--live", "--now", NOW, env=KEYS)
        self.assertEqual(code, 1)
        self.assertIn("set \"mode\": \"live\" in the config yourself", err)

    def test_live_config_without_flag_or_keys(self):
        nse.write_json(self.p("config.json"), {"mode": "live", "broker": "angel"})
        self.prepare()
        self.go("approve", "--index", "1", "--yes", "yes buy 21 RELIANCE", "--now", NOW)
        code, _, err = self.go("send", "--index", "1", "--now", NOW, env=KEYS)
        self.assertIn("needs --live", err)
        code, _, err = self.go("send", "--index", "1", "--now", NOW, "--live")
        self.assertIn("ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_PIN and ANGEL_TOTP_SECRET", err)
        for cmd in ("protect", "check"):
            self.assertIn("needs --live", self.go(cmd, "--now", NOW, env=KEYS)[2])
        self.assertEqual([r["status"] for r in self.doc("broker.json")["orders"]], ["approved"])


class Live(Base):
    mode = "live"

    def ready(self, exit_yes=None):
        self.prepare()
        self.go("show", "--now", NOW)
        extra = ["--exit-yes", exit_yes] if exit_yes else []
        code, text, _ = self.go("approve", "--index", "1", "--yes", "yes", "--now", NOW, *extra)
        self.assertEqual(code, 0, text)

    def live(self, *argv):
        return self.go(*argv, env=KEYS)

    def test_entry_protect_target_ask_then_exit(self):
        self.ready()
        fake = FakeAngel(ltp=1170.0)
        ab.TRANSPORT = fake
        code, text, err = self.live("send", "--index", "1", "--live", "--now", NOW)
        self.assertEqual(code, 0, err)
        self.assertIn("LIVE: Angel One order 261029000001 (entry, LIMIT 1174.30)", text)
        self.assertIn("There is NO stop yet", text)
        login, placed = fake.calls
        self.assertEqual((login["route"], placed["route"]), ("login", "place"))
        self.assertEqual(placed["body"], self.doc("orders.json")["orders"][0]["angel"]["body"])
        self.assertEqual(placed["headers"]["X-PrivateKey"], API_KEY)
        self.assertEqual(placed["headers"]["Authorization"], "Bearer " + JWT)
        self.assertEqual(placed["url"], angel.ROOT + "/rest/secure/angelbroking/order/v1/placeOrder")

        code, text, _ = self.live("status", "--now", "2026-10-29 09:43")
        self.assertIn("RELIANCE entry 261029000001: complete, filled 21 of 21 at 1174.30", text)
        self.assertIn("FILLED, NOT PROTECTED", text)

        code, text, err = self.live("protect", "--live", "--now", "2026-10-29 09:43")
        self.assertEqual(code, 0, err)
        stop = fake.places()[-1]
        self.assertEqual((stop["variety"], stop["ordertype"], stop["transactiontype"], stop["triggerprice"],
                          stop["price"], stop["quantity"]),
                         ("STOPLOSS", "STOPLOSS_LIMIT", "SELL", "1163.30", "1162.80", "21"))
        n = len(fake.places())
        code, text, _ = self.live("protect", "--live", "--now", "2026-10-29 09:44")
        self.assertIn("already have a stop", text)
        self.assertEqual(len(fake.places()), n)                    # never two stops for the same shares

        code, text, _ = self.live("check", "--live", "--now", "2026-10-29 10:00")
        self.assertIn("holding 21, last 1170.00, stop 1163.30, target 1196.30", text)
        fake.ltp = 1197.0
        code, text, _ = self.live("check", "--live", "--now", "2026-10-29 11:00")
        self.assertIn("target 1196.30 reached (last 1197.00). Ask the user", text)
        self.assertEqual(len(fake.places()), n)                    # not pre-approved: nothing sold
        ex = self.doc("orders.json")["orders"][0]
        self.assertEqual((ex["kind"], ex["qty"], ex["limit"]), ("exit", 21, 1196.5))

        code, text, err = self.live("risk", "--scrip-offline", SCRIP_JSON, "--now", "2026-10-29 11:00", "--no-quote")
        self.assertEqual(code, 0, text + err)
        self.live("show", "--now", "2026-10-29 11:00")
        self.assertEqual(self.live("approve", "--index", "1", "--yes", "yes sell 21 RELIANCE",
                                   "--now", "2026-10-29 11:00")[0], 0)
        fake.calls = []
        code, text, err = self.live("send", "--index", "1", "--live", "--now", "2026-10-29 11:01")
        self.assertEqual(code, 0, err)
        self.assertEqual([c["route"] for c in fake.calls],
                         ["login", "orders", "cancel", "orders", "positions", "place", "orders"])   # then: filled?
        self.assertEqual(fake.calls[2]["body"], {"variety": "STOPLOSS", "orderid": "261029000002"})
        self.assertEqual(fake.places()[-1], angel.order_body("RELIANCE-EQ", "2885", "SELL", 21, 1196.5))
        self.assertIn("exit filled", text)
        self.assertIn("sell 21 RELIANCE at 1196.50 limit", text)
        self.assertNotIn("MARKET", json.dumps([c["body"] for c in fake.calls]))
        code, text, _ = self.live("status", "--now", "2026-10-29 11:05")
        self.assertIn("closed.", text)
        self.assert_no_secrets()

    def test_pre_approved_exit_at_flat_by(self):
        self.ready(exit_yes="yes, exit at the target or flat-by")
        rec = self.doc("broker.json")["orders"][-1]
        self.assertEqual((rec["status"], rec["exit_auto"], rec["exit_reply"]),
                         ("approved", True, "yes, exit at the target or flat-by"))
        fake = FakeAngel(ltp=1180.0)
        ab.TRANSPORT = fake
        self.assertEqual(self.live("send", "--index", "1", "--live", "--now", NOW)[0], 0)
        self.assertEqual(self.live("protect", "--live", "--now", "2026-10-29 09:45")[0], 0)
        fake.calls = []
        code, text, err = self.live("check", "--live", "--now", "2026-10-29 15:06")
        self.assertEqual(code, 0, err)
        self.assertIn("flat-by 15:05 reached (last 1180.00). You pre-approved this exit", text)
        self.assertEqual([c["route"] for c in fake.calls],
                         ["login", "orders", "quote", "orders", "cancel", "orders", "positions", "place", "orders"])
        self.assertEqual(fake.places()[-1]["price"], "1179.50")
        self.assertIn("the RELIANCE exit filled", text)
        st = self.doc("broker.json")
        self.assertEqual(st["orders"][-1]["status"], "exit-filled")
        auto = st["orders"][-2]
        self.assertEqual((auto["status"], auto["kind"], auto["auto_exit"], auto["approved_with"]),
                         ("sent", "exit", True, st["managed"][0]["fingerprint"]))
        self.assertEqual(st["managed"][0]["state"], "exiting")
        self.assert_no_secrets()

    # ---- an exit that does not fill: re-price, then the stop goes back (or a loud warning)

    def auto_trade(self, fake, **cfg):
        if cfg:
            c = self.doc("config.json")
            c.update(cfg)
            nse.write_json(self.p("config.json"), c)
        self.ready(exit_yes="yes, exit at the target or flat-by")
        ab.TRANSPORT = fake
        self.assertEqual(self.live("send", "--index", "1", "--live", "--now", NOW)[0], 0)
        self.assertEqual(self.live("protect", "--live", "--now", "2026-10-29 09:45")[0], 0)
        fake.calls = []
        return self.live("check", "--live", "--now", "2026-10-29 15:06")

    def test_exit_fills_after_two_reprices(self):
        fake = FakeAngel(sell_plan=["open", "open", "complete"], ltps=[1180.0, 1178.0, 1176.0])
        code, text, err = self.auto_trade(fake)
        self.assertEqual(code, 0, err)
        mods = fake.routes("modify")
        self.assertEqual([(m["price"], m["quantity"]) for m in mods], [("1177.00", "21"), ("1174.50", "21")])
        self.assertEqual(mods[0]["variety"], "NORMAL")
        self.assertIn("re-priced 21 to 1177.00 (last 1178.00 minus 10 ticks)", text)
        self.assertIn("re-priced 21 to 1174.50 (last 1176.00 minus 15 ticks)", text)
        self.assertIn("exit filled", text)
        self.assertEqual(len([b for b in fake.places() if b["transactiontype"] == "SELL"]), 1)   # one sell, modified
        self.assertNotIn("MARKET", json.dumps([c["body"] for c in fake.calls]))

    def test_exit_never_fills_stop_goes_back(self):
        fake = FakeAngel(ltp=1170.0, sell_plan=["open"] * 10)
        code, text, err = self.auto_trade(fake, exit_reprice_max=2)
        self.assertEqual(code, 0, err)          # check itself ran; the exit result is in the text and the log
        self.assertEqual(len(fake.routes("modify")), 2)
        exit_id = [r["orderid"] for r in fake.book if r["variety"] == "NORMAL" and r["transactiontype"] == "SELL"][0]
        self.assertIn({"variety": "NORMAL", "orderid": exit_id}, fake.routes("cancel"))
        restop = fake.places()[-1]
        self.assertEqual((restop["variety"], restop["ordertype"], restop["triggerprice"], restop["price"],
                          restop["quantity"]), ("STOPLOSS", "STOPLOSS_LIMIT", "1163.30", "1162.80", "21"))
        self.assertIn("DID NOT FILL AFTER 2 RE-PRICES", text)
        self.assertIn("THE STOP IS BACK", text)
        st = self.doc("broker.json")
        self.assertEqual(st["orders"][-1]["status"], "exit-restopped")
        self.assertEqual(st["managed"][0]["state"], "open")
        self.assertEqual(st["managed"][0]["stops"][-1]["qty"], 21)

    def test_exit_never_fills_below_the_stop_warns_loudly(self):
        fake = FakeAngel(ltp=1160.0, sell_plan=["open"] * 10)
        code, text, err = self.auto_trade(fake, exit_reprice_max=1)
        self.assertEqual(len(fake.routes("modify")), 1)
        self.assertEqual([b for b in fake.routes("cancel") if b["variety"] == "NORMAL"], [])   # the sell keeps working
        self.assertEqual([b for b in fake.places() if b["variety"] == "STOPLOSS"], [])
        self.assertIn("21 RELIANCE SHARES ARE STILL OPEN WITH NO STOP", text)
        self.assertIn("IS STILL WORKING", text)
        self.assertEqual(self.doc("broker.json")["orders"][-1]["status"], "exit-unfilled")

    def test_position_closed_by_the_stop_meanwhile_sells_nothing(self):
        fake = FakeAngel(ltp=1180.0)

        def stop_fills_instead(b):          # the cancel arrives too late: the stop has just filled
            for r in fake.book:
                if r["orderid"] == b["orderid"]:
                    r.update(status="complete", orderstatus="complete", filledshares=r["quantity"])
            return {"orderid": b["orderid"]}
        fake.r_cancel = stop_fills_instead
        code, text, err = self.auto_trade(fake)
        self.assertEqual(code, 0, err)
        self.assertIn("no open RELIANCE position left (net qty 0)", text)
        self.assertEqual([b for b in fake.places() if b["variety"] == "NORMAL"], [])

    def test_drive_exit_never_sells_more_than_held(self):
        class Ops(ab.core.ExitOps):
            def __init__(s):
                s.calls = []

            def poll(s):
                return "open", 5                 # 5 of 21 filled

            def net(s):
                return 6                         # but only 6 still held (a manual sell elsewhere)

            def ltp(s):
                return 1170.0

            def reprice(s, total, price):
                s.calls.append((total, price))

            def cancel(s):
                return True
        ops = Ops()
        res = ab.core.drive_exit(ops, 21, 1169.5, 0.1, 5, 1, None, "RELIANCE", say=lambda m: None)
        self.assertEqual(ops.calls, [(11, 1169.0)])                    # 5 filled + at most the 6 held
        self.assertEqual(res["result"], "unfilled")

    def test_exit_yes_must_be_explicit(self):
        self.prepare()
        self.go("show", "--now", NOW)
        code, text, _ = self.go("approve", "--index", "1", "--yes", "yes", "--now", NOW, "--exit-yes", "sure")
        self.assertEqual(code, 1)
        self.assertIn("Not recorded: the exit pre-approval", text)
        self.assertNotIn("approved", [r["status"] for r in (self.doc("broker.json") or {}).get("orders", [])])

    def test_flatten_never_blocked_live(self):
        fake = FakeAngel(ltp=1166.9)
        fake.book.append(dict(angel.order_body("RELIANCE-EQ", "2885", "BUY", 21, 1174.3), orderid="261029000050",
                              status="complete", orderstatus="complete", filledshares="21", averageprice="1174.30"))
        fake.book.append(dict(angel.order_body("RELIANCE-EQ", "2885", "SELL", 21, 1162.8, variety="STOPLOSS",
                                               ordertype="STOPLOSS_LIMIT", trigger=1163.3),
                              orderid="261029000051", status="trigger pending", orderstatus="trigger pending",
                              filledshares="0", averageprice="0"))
        ab.TRANSPORT = fake
        nse.write_json(self.p("config.json"), {"mode": "live", "broker": "angel", "blocked_symbols": ["RELIANCE"],
                                               "daily_loss_limit": 1, "max_trades_per_day": 1})
        late = "2026-10-29 15:20"
        code, text, err = self.live("flatten", "--symbol", "RELIANCE", "--now", late, "--scrip-offline", SCRIP_JSON)
        self.assertEqual(code, 0, err)
        self.assertIn("at Rs 1,166.40 limit", text)                 # Angel's quote 1166.90 minus 5 ticks
        code, text, err = self.live("risk", "--scrip-offline", SCRIP_JSON, "--now", late, "--no-quote")
        self.assertEqual(code, 0, text + err)
        self.live("show", "--now", late)
        self.assertEqual(self.live("approve", "--index", "1", "--yes", "yes sell 21 RELIANCE", "--now", late)[0], 0)
        code, text, err = self.live("send", "--index", "1", "--live", "--now", late)
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.places()[-1], angel.order_body("RELIANCE-EQ", "2885", "SELL", 21, 1166.4))
        self.assertIn({"variety": "STOPLOSS", "orderid": "261029000051"}, [c["body"] for c in fake.calls])

    def test_stop_cancel_not_confirmed_sells_nothing(self):
        fake = FakeAngel()
        fake.book.append(dict(angel.order_body("RELIANCE-EQ", "2885", "BUY", 21, 1174.3), orderid="1",
                              status="complete", orderstatus="complete", filledshares="21", averageprice="1174.30"))
        fake.book.append(dict(angel.order_body("RELIANCE-EQ", "2885", "SELL", 21, 1162.8, variety="STOPLOSS",
                                               ordertype="STOPLOSS_LIMIT", trigger=1163.3), orderid="2",
                              status="trigger pending", orderstatus="trigger pending", filledshares="0"))
        fake.r_cancel = lambda b: {"orderid": b["orderid"]}            # accepted, but the book never says cancelled
        ab.TRANSPORT = fake
        late = "2026-10-29 15:06"
        self.live("flatten", "--symbol", "RELIANCE", "--now", late, "--price", "RELIANCE=1170", "--scrip-offline",
                  SCRIP_JSON)
        self.live("risk", "--scrip-offline", SCRIP_JSON, "--now", late, "--no-quote")
        self.live("show", "--now", late)
        self.live("approve", "--index", "1", "--yes", "yes sell 21 RELIANCE", "--now", late)
        code, _, err = self.live("send", "--index", "1", "--live", "--now", late)
        self.assertEqual(code, 1)
        self.assertIn("did not confirm their cancel", err)
        self.assertIn("STILL WORKING", err)
        self.assertEqual(fake.places(), [])

    def test_no_answer_is_unknown_and_blocks_a_resend(self):
        self.ready()
        ab.TRANSPORT = FakeAngel(fail={"place": nse.NSEError("could not reach apiconnect.angelone.in: timed out")})
        code, _, err = self.live("send", "--index", "1", "--live", "--now", NOW)
        self.assertEqual(code, 1)
        self.assertIn("MAY have reached Angel One", err)
        code, text, _ = self.live("show", "--now", NOW)
        self.assertIn("UNKNOWN", text)
        code, _, err = self.live("send", "--index", "1", "--live", "--now", NOW)
        self.assertIn("last: unknown", err)

    def test_rejection_is_definite_and_secrets_are_scrubbed(self):
        self.ready()
        msg = "Invalid apikey %s for %s with pin %s / %s" % (API_KEY, JWT, PIN, SECRET)
        ab.TRANSPORT = FakeAngel(fail={"place": Reject("AB1004", msg)})
        code, text, err = self.live("send", "--index", "1", "--live", "--now", NOW)
        self.assertEqual(code, 1)
        self.assertIn("NOT placed", err)
        self.assertEqual(self.doc("broker.json")["orders"][-1]["status"], "failed")
        self.assert_no_secrets()

    def test_market_order_never_leaves(self):
        self.prepare()
        doc = self.doc("orders.json")
        doc["orders"][0]["angel"]["body"]["ordertype"] = "MARKET"
        nse.write_json(self.p("orders.json"), doc)
        risk = self.doc("risk.json")
        risk["results"][0].update(fingerprint=ab.fingerprint(doc["orders"][0]), verdict="PASS", reasons=[])
        nse.write_json(self.p("risk.json"), risk)                  # even a forged PASS
        self.go("show", "--now", NOW)
        self.assertEqual(self.go("approve", "--index", "1", "--yes", "yes", "--now", NOW)[0], 0)
        fake = FakeAngel()
        ab.TRANSPORT = fake
        code, _, err = self.live("send", "--index", "1", "--live", "--now", NOW)
        self.assertEqual(code, 1)
        self.assertIn("prohibited for algorithmic trading", err)
        self.assertEqual(fake.calls, [])

    def test_read_only_commands(self):
        fake = FakeAngel()
        ab.TRANSPORT = fake
        code, text, _ = self.live("profile")
        self.assertIn("Angel One session OK", text)
        self.assertIn("****4567", text)
        for private in (CLIENT, "TEST USER", "x@example.com", "9999999999"):
            self.assertNotIn(private, text)
        code, text, _ = self.live("funds")
        self.assertIn("Available cash: Rs 98,440.00", text)
        with mock.patch.object(ab, "public_ip", lambda: "203.0.113.7"):
            code, text, _ = self.live("ip")
        self.assertEqual(code, 0)
        self.assertIn("matches your registered static IP 203.0.113.7", text)
        with mock.patch.object(ab, "public_ip", lambda: "198.51.100.4"):
            code, text, _ = self.live("ip")
        self.assertEqual(code, 1)
        self.assertIn("does NOT match", text)
        self.assertEqual({c["route"] for c in fake.calls}, {"login", "profile", "funds"})
        self.assert_no_secrets()


if __name__ == "__main__":
    unittest.main()
