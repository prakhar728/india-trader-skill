#!/usr/bin/env python3
"""Angel One SmartAPI client for the india-* skills. Standard library only.

Keys come from the environment or a .env (nse.load_env), never from chat:
    ANGEL_API_KEY       the SmartAPI app's API key (X-PrivateKey)
    ANGEL_CLIENT_CODE   your Angel One client ID
    ANGEL_PIN           your login PIN (sent as "password")
    ANGEL_TOTP_SECRET   the base32 secret from SmartAPI's Enable TOTP page

Routes and headers are the ones in Angel's official SDK
(github.com/angel-one/smartapi-python, SmartApi/smartConnect.py). A session
lasts until midnight. Order calls must come from the static IP registered on
the SmartAPI app; other calls don't need it. Orders are capped at 9 a second.

Instruments: OpenAPIScripMaster.json. NSE cash rows have exch_seg NSE and a
symbol ending -EQ (BE / BZ trade-to-trade rows end -BE / -BZ); token is NSE's
own security id (RELIANCE-EQ = 2885) and tick_size is in paise.

Orders (SDK README body shape, every value a string): variety NORMAL with
ordertype LIMIT for entries and exits, variety STOPLOSS with ordertype
STOPLOSS_LIMIT and a triggerprice for the exchange-held stop. Angel's April
2026 notice prohibits MARKET and IOC orders for algorithmic trading, so
order_body() refuses them; ROBO (bracket) is not used (not confirmed for NSE
intraday via the API).
"""

import base64
import hashlib
import hmac
import json
import os
import socket
import struct
import time

import nse

ROOT = "https://apiconnect.angelone.in"
SCRIP_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
ROUTES = {
    "login": "/rest/auth/angelbroking/user/v1/loginByPassword",
    "logout": "/rest/secure/angelbroking/user/v1/logout",
    "profile": "/rest/secure/angelbroking/user/v1/getProfile",
    "funds": "/rest/secure/angelbroking/user/v1/getRMS",
    "place": "/rest/secure/angelbroking/order/v1/placeOrder",
    "modify": "/rest/secure/angelbroking/order/v1/modifyOrder",
    "cancel": "/rest/secure/angelbroking/order/v1/cancelOrder",
    "orders": "/rest/secure/angelbroking/order/v1/getOrderBook",
    "trades": "/rest/secure/angelbroking/order/v1/getTradeBook",
    "order": "/rest/secure/angelbroking/order/v1/details/",
    "positions": "/rest/secure/angelbroking/order/v1/getPosition",
    "ltp": "/rest/secure/angelbroking/order/v1/getLtpData",
    "quote": "/rest/secure/angelbroking/market/v1/quote",
    "candles": "/rest/secure/angelbroking/historical/v1/getCandleData",
}
ENV_KEYS = ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET")
SECRET_KEYS = ("ANGEL_API_KEY", "ANGEL_PIN", "ANGEL_TOTP_SECRET")
INTERVALS = {1: "ONE_MINUTE", 3: "THREE_MINUTE", 5: "FIVE_MINUTE", 10: "TEN_MINUTE",
             15: "FIFTEEN_MINUTE", 30: "THIRTY_MINUTE", 60: "ONE_HOUR"}


class AngelError(nse.NSEError):
    pass


# ---------------------------------------------------------------- TOTP (RFC 6238)

def totp(secret, at=None, step=30, digits=6):
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8))
    counter = int((time.time() if at is None else at) // step)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


# ---------------------------------------------------------------- credentials

def credentials(env=None):
    if env is None:
        nse.load_env()
        env = os.environ
    vals = dict((k, (env.get(k) or "").strip()) for k in ENV_KEYS)
    missing = [k for k, v in vals.items() if not v]
    return vals, missing


def scrub(text, env=None):
    """Remove every secret (and any JWT) from a string before it is printed or logged."""
    env = os.environ if env is None else env
    out = str(text)
    for k in SECRET_KEYS + ("ANGEL_JWT",):
        v = env.get(k)
        if v and len(v) >= 4:
            out = out.replace(v, "***")
    return out


def _local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def public_ip():
    try:
        return nse.http_get("https://api.ipify.org", timeout=10).decode().strip()
    except nse.NSEError:
        return ""


# ---------------------------------------------------------------- client

class Angel(object):
    def __init__(self, api_key, client_code, pin, totp_secret, transport=None, root=None,
                 public=None, local=None, mac="00:00:00:00:00:00"):
        self.api_key, self.client_code = api_key, client_code
        self._pin, self._secret = pin, totp_secret
        self.transport = transport or nse.http_get
        self.root = root or ROOT
        self.public = public if public is not None else (public_ip() if transport is None else "1.1.1.1")
        self.local = local or (_local_ip() if transport is None else "127.0.0.1")
        self.mac = mac
        self.jwt = None
        self.feed_token = None

    @classmethod
    def from_env(cls, **kw):
        vals, missing = credentials()
        if missing:
            raise AngelError("missing in .env or the shell: %s" % ", ".join(missing))
        return cls(vals["ANGEL_API_KEY"], vals["ANGEL_CLIENT_CODE"], vals["ANGEL_PIN"],
                   vals["ANGEL_TOTP_SECRET"], **kw)

    def headers(self):
        h = {"Content-Type": "application/json", "Accept": "application/json",
             "X-UserType": "USER", "X-SourceID": "WEB", "X-ClientLocalIP": self.local,
             "X-ClientPublicIP": self.public, "X-MACAddress": self.mac, "X-PrivateKey": self.api_key}
        if self.jwt:
            h["Authorization"] = "Bearer " + self.jwt
        return h

    def call(self, route, body=None, method="POST", suffix=""):
        url = self.root + ROUTES[route] + suffix
        try:
            raw = self.transport(url, headers=self.headers(), data=body if method == "POST" else None,
                                 method=method)
        except nse.NSEError as e:
            raise AngelError(self._clean(str(e)))
        try:
            doc = json.loads(raw or b"{}")
        except ValueError:
            raise AngelError("Angel sent something that is not JSON: %s" % self._clean(raw[:200]))
        if isinstance(doc, dict) and doc.get("status") is False:
            raise AngelError("Angel %s: %s" % (doc.get("errorcode") or "error", self._clean(doc.get("message"))))
        return doc

    def _clean(self, text):
        out = str(text)
        for v in (self.api_key, self._pin, self._secret, self.jwt):
            if v and len(v) >= 4:
                out = out.replace(v, "***")
        return out

    def login(self):
        doc = self.call("login", {"clientcode": self.client_code, "password": self._pin,
                                  "totp": totp(self._secret)})
        data = doc.get("data") or {}
        if not data.get("jwtToken"):
            raise AngelError("login answered without a session token")
        self.jwt = data["jwtToken"]
        self.feed_token = data.get("feedToken")
        return True

    def ensure(self):
        if not self.jwt:
            self.login()

    def get(self, route, suffix=""):
        self.ensure()
        return self.call(route, method="GET", suffix=suffix).get("data")

    def post(self, route, body):
        self.ensure()
        return self.call(route, body).get("data")

    def profile(self):
        return self.get("profile")

    def funds(self):
        return self.get("funds")

    def positions(self):
        return self.get("positions") or []

    def orders(self):
        return self.get("orders") or []

    def trades(self):
        return self.get("trades") or []

    def place(self, body):
        """placeOrder. Returns data ({"orderid", "uniqueorderid"}). Refuses MARKET / IOC."""
        check_body(body)
        return self.post("place", body) or {}

    def modify(self, body):
        """modifyOrder (SDK route). Body fields per Angel's docs, not the SDK: variety, orderid, ordertype,
        producttype, duration, price, quantity, tradingsymbol, symboltoken, exchange (unverified)."""
        check_body(body)
        return self.post("modify", body) or {}

    def cancel(self, variety, orderid):
        return self.post("cancel", {"variety": variety, "orderid": str(orderid)}) or {}

    def quote(self, tokens, mode="LTP", exchange="NSE"):
        return self.post("quote", {"mode": mode, "exchangeTokens": {exchange: [str(t) for t in tokens]}})

    def candles(self, token, start, end, minutes=5, exchange="NSE"):
        data = self.post("candles", {"exchange": exchange, "symboltoken": str(token),
                                     "interval": INTERVALS[minutes],
                                     "fromdate": start.strftime("%Y-%m-%d %H:%M"),
                                     "todate": end.strftime("%Y-%m-%d %H:%M")})
        return candle_bars(data or [])

    def logout(self):
        if self.jwt:
            try:
                self.call("logout", {"clientcode": self.client_code})
            finally:
                self.jwt = None


def ltps(data):
    """quote(mode LTP) data -> {token: ltp}. Field names (fetched, symbolToken, ltp) are from
    Angel's SmartAPI docs, not the SDK: unverified, so both spellings of the token key are read."""
    out = {}
    for r in (data or {}).get("fetched") or []:
        tok = r.get("symbolToken", r.get("symboltoken"))
        try:
            out[str(tok)] = float(r.get("ltp"))
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------- order bodies

NO_ALGO_TYPES = ("MARKET", "STOPLOSS_MARKET")
NO_ALGO_DURATIONS = ("IOC",)


def money(x):
    return "%.2f" % float(x)


def order_body(tradingsymbol, token, side, qty, price, exchange="NSE", product="INTRADAY",
               variety="NORMAL", ordertype="LIMIT", trigger=None):
    """One placeOrder body in the SDK README's shape (strings throughout)."""
    b = {"variety": variety, "tradingsymbol": tradingsymbol, "symboltoken": str(token),
         "transactiontype": side.upper(), "exchange": exchange.upper(), "ordertype": ordertype,
         "producttype": product.upper(), "duration": "DAY", "price": money(price),
         "squareoff": "0", "stoploss": "0", "quantity": str(int(qty))}
    if trigger is not None:
        b["triggerprice"] = money(trigger)
    check_body(b)
    return b


def check_body(b):
    """Refuse what Angel prohibits for algo orders (MARKET, IOC) and what this pack does not use (ROBO)."""
    if str(b.get("ordertype", "")).upper() in NO_ALGO_TYPES:
        raise AngelError("%s orders are prohibited for algorithmic trading on Angel One (April 2026 "
                         "notice): use a LIMIT price" % b.get("ordertype"))
    if str(b.get("duration", "")).upper() in NO_ALGO_DURATIONS:
        raise AngelError("IOC orders are prohibited for algorithmic trading on Angel One (April 2026 notice)")
    if str(b.get("variety", "")).upper() == "ROBO":
        raise AngelError("ROBO (bracket) orders are not confirmed for NSE intraday via the API: not used")
    return b


def candle_bars(rows):
    """[[iso time with +05:30, o, h, l, c, v], ...] -> nse bars (naive IST)."""
    import datetime
    out = []
    for r in rows:
        t = datetime.datetime.fromisoformat(r[0])
        if t.tzinfo is not None:
            t = nse.ist_from_utc(t.astimezone(datetime.timezone.utc))
        out.append({"t": t, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
                    "v": int(r[5] or 0)})
    return out


# ---------------------------------------------------------------- instruments

SERIES = ("EQ", "BE", "BZ")


def parse_scrip(data, exchange="NSE"):
    """OpenAPIScripMaster.json -> {SYMBOL: {token, security_id, trading_symbol, series, tick, lot, cas}}
    for the -EQ rows, plus -BE / -BZ (trade-to-trade) rows so they can be refused by name.
    An EQ row wins if a name has two. security_id = token (NSE's own id)."""
    rows = json.loads(data) if isinstance(data, (bytes, str)) else data
    out = {}
    for r in rows:
        sym = r.get("symbol", "")
        series = sym.rsplit("-", 1)[-1] if "-" in sym else ""
        if r.get("exch_seg") != exchange or series not in SERIES:
            continue
        if out.get(r["name"], {}).get("series") == "EQ":
            continue
        out[r["name"]] = {"token": r["token"], "security_id": r["token"], "trading_symbol": sym,
                          "series": series,
                          "tick": round(float(r.get("tick_size") or 0) / 100.0, 4) or None,
                          "lot": int(float(r.get("lotsize") or 1)), "cas": bool(r.get("is_cas_enabled"))}
    return out


def _fill(table):
    """A cache written before series / security_id existed still works."""
    for row in (table or {}).values():
        row.setdefault("security_id", row.get("token"))
        row.setdefault("series", str(row.get("trading_symbol", "")).rsplit("-", 1)[-1])
    return table


def scrip_master(base=None, offline=None, exchange="NSE"):
    slim = os.path.join(nse.trader_dir(base), "cache", "angel-%s-equity.json" % exchange.lower())
    if not offline:
        try:
            if time.time() - os.path.getmtime(slim) < 20 * 3600:
                return _fill(nse.read_json(slim))
        except OSError:
            pass
    raw = nse.read_maybe_gz(offline) if offline else nse.http_get(SCRIP_URL, timeout=180)
    table = parse_scrip(raw, exchange)
    if not offline:
        nse.write_json(slim, table)
    return table


# ---------------------------------------------------------------- NFO options (read-only: for candles)

NFO_OPTIONS_URL = SCRIP_URL
OPTION_TYPES = ("OPTIDX", "OPTSTK")


def parse_nfo_options(data, names=None):
    """OpenAPIScripMaster.json -> [{token, symbol, name, expiry (YYYY-MM-DD), strike (rupees),
    type CE/PE, lot, tick, kind}] for live NFO index and stock options. In the file the
    strike and tick_size are in paise (NIFTY 23000 CE has strike 2300000.000000) and
    expiry reads like 03NOV2026. names = only these underlyings (NIFTY, SBIN, ...)."""
    import datetime
    rows = json.loads(data) if isinstance(data, (bytes, str)) else data
    want = set(n.upper() for n in names) if names else None
    out = []
    for r in rows:
        if r.get("exch_seg") != "NFO" or r.get("instrumenttype") not in OPTION_TYPES:
            continue
        name = (r.get("name") or "").upper()
        if want is not None and name not in want:
            continue
        sym = r.get("symbol") or ""
        typ = sym[-2:]
        if typ not in ("CE", "PE"):
            continue
        try:
            exp = datetime.datetime.strptime(r["expiry"], "%d%b%Y").date().isoformat()
            strike = round(float(r["strike"]) / 100.0, 2)
        except (KeyError, ValueError):
            continue
        out.append({"token": str(r["token"]), "symbol": sym, "name": name, "expiry": exp, "strike": strike,
                    "type": typ, "lot": int(float(r.get("lotsize") or 1)),
                    "tick": round(float(r.get("tick_size") or 5) / 100.0, 4) or 0.05,
                    "kind": r["instrumenttype"]})
    out.sort(key=lambda o: (o["name"], o["expiry"], o["strike"], o["type"]))
    return out


def nfo_options(base=None, offline=None, names=None):
    """Live NFO options from Angel's instrument file, cached slim for 20 hours under
    trader/cache/angel-nfo-options.json. offline = a saved OpenAPIScripMaster.json."""
    slim = os.path.join(nse.trader_dir(base), "cache", "angel-nfo-options.json")
    rows = None
    if not offline:
        try:
            if time.time() - os.path.getmtime(slim) < 20 * 3600:
                rows = nse.read_json(slim)
        except OSError:
            pass
    if rows is None:
        raw = nse.read_maybe_gz(offline) if offline else nse.http_get(SCRIP_URL, timeout=180)
        rows = parse_nfo_options(raw)
        if not offline:
            nse.write_json(slim, rows)
    if names:
        want = set(n.upper() for n in names)
        rows = [r for r in rows if r["name"] in want]
    return rows


# Index tokens on exchange NSE (instrumenttype AMXIDX in the instrument file).
INDEX_TOKENS = {"NIFTY": "99926000", "INDIAVIX": "99926017"}
