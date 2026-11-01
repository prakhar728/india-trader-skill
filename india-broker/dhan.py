#!/usr/bin/env python3
"""Order tickets, your yes, and Dhan for india-broker. Standard library only.

This is the only script in the pack that can place an order. It does so only
when ALL of these hold, every time, for one order:

  1. india-risk passed THIS exact order today (fingerprint match in trader/risk.json;
     an exit that only sells what you hold does not need it: getting out is never blocked)
  2. you said yes to THIS order in the chat, and `approve --yes "<your words>"` recorded
     it word for word within the last 10 minutes (approval_minutes)
  3. you allowed the `dhan.py send` command on Claude Code's own permission prompt
  4. live only: "mode": "live" in trader/config.json AND --live on the command AND
     DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN in your shell

    python3 dhan.py show                         # every ticket, READY or not
    python3 dhan.py approve --index 1 --yes "yes buy 21 RELIANCE"
    python3 dhan.py send --index 1               # paper (default): a local simulated fill
    python3 dhan.py send --index 1 --live        # live: POST /v2/super/orders on Dhan
    python3 dhan.py paper-check                  # paper: fills, stop, target, flat-by
    python3 dhan.py flatten --symbol RELIANCE    # the sell ticket that gets you out (marketable LIMIT)
    python3 dhan.py status | trades | positions | funds | log
    python3 dhan.py cancel --order-id ID [--leg ENTRY_LEG] --live
    python3 dhan.py ip | profile                 # static IP whitelist, token validity, Data plan

Modes. paper (default): nothing goes to Dhan; trader/paper.json is a local
ledger filled from the latest price (Yahoo 1-minute bars, trader/prices.json,
or --price). Dhan has no paper trading: its sandbox is an integration-test
box, not a market. To use it: DHAN_BASE_URL=https://sandbox.dhan.co/v2 with
a sandbox token from cloud.dhanhq.co (never your live token); super orders
are not sandbox-enabled, so entries go there as plain /orders requests.
live: real orders on your Dhan account.

Exits are a marketable LIMIT sell (the last price minus a few ticks, never
under the stop-limit floor), not MARKET: brokers apply market-price
protection to API market orders, and the pack sends no market orders.

The approval, paper and log logic shared with angel_broker.py (Angel One)
lives in ticket_core.py next to this file.

Everything is logged to trader/broker.json: approvals (your words), sends
(the request body WITHOUT your client id), Dhan's responses, fills, closes,
and the day's risk used. The access token is never printed or written.
Times are IST; --now "2026-10-29 09:41" pins the clock.
"""

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "india-common"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse  # noqa: E402
import ticket_core as core  # noqa: E402
from ticket_core import (BrokerError, FP_FIELDS, APPROVAL_MINUTES, FRESH_MINUTES, Paths,  # noqa: E402,F401
                         fingerprint, _f, p2, parse_iso_ist, kind_of, log, latest, day_summary, ticket_line,
                         order_numbers, risk_verdict, load_bars, step_of, price_args, latest_price, close_paper,
                         fill_paper, simulate, paper_events_to_log)

# Tests inject a fake HTTP transport here (same signature as nse.http_get).
TRANSPORT = None
ORDER_GAP = 0.11                     # Dhan order APIs: 10 a second
OPEN_STATES = ("TRANSIT", "PENDING", "PART_TRADED")
PAPER_NOTE = ("india-broker paper ledger: simulated fills from public prices, not a broker. "
              "Dhan has no paper trading.")


# ---------------------------------------------------------------- Dhan specifics

def scrub(text):
    """Never let the token (or the client id) reach the screen or a file."""
    text = str(text)
    cred = nse.dhan_credentials()
    if cred:
        for secret in (cred[1], cred[0]):
            if secret and len(secret) > 3:
                text = text.replace(secret, "***")
    return text


def dhan_error(e):
    """A Dhan failure in plain words, with what to do."""
    msg = scrub(e)
    if "DH-901" in msg or "HTTP 401" in msg:
        return ("Dhan says the access token is invalid or expired (DH-901). Tokens last 24 hours: on web.dhan.co "
                "go to My Profile > Access DhanHQ APIs, generate a new token, and in your shell (never in the "
                "chat) run: export DHAN_ACCESS_TOKEN=<new token>. Then try again.")
    if "DH-904" in msg or "HTTP 429" in msg:
        return ("Dhan's rate limit (DH-904): too many requests. Wait a few seconds; order APIs allow 10 a second. "
                "Nothing was retried automatically.")
    if re.search(r"\bIP\b|static ip|whitelist", msg, re.I):
        return ("Dhan refused this request from this machine's IP (%s). Order APIs need a whitelisted static IP "
                "(SEBI retail-algo rules): run `dhan.py ip` to see what is registered, and set it on web.dhan.co "
                "> My Profile > Access DhanHQ APIs. The order was NOT placed." % msg[:200])
    for code, what in (("DH-905", "bad input"), ("DH-906", "order error")):
        if code in msg:
            return "Dhan rejected it (%s, %s): %s. The order was NOT placed." % (code, what, msg[:300])
    if "DH-902" in msg:
        return "Dhan says this account has no API access for that (DH-902): %s" % msg[:200]
    return "Dhan: %s" % msg[:300]


def _secrets():
    cred = nse.dhan_credentials()
    return [cred[1], cred[0]] if cred else []


def _file_secrets():
    cred = nse.dhan_credentials()
    return [cred[1]] if cred else []


def describe(o, mode):
    dh = o.get("dhan") or {}
    lines = ["  Dhan request%s: %s" % (" (sent only in live mode)" if mode == "paper" else "", dh.get("endpoint"))]
    lines.append("    " + json.dumps(dh.get("body")))
    lines.append("    (+ dhanClientId from your shell at send time; the token goes in a header and is never shown)")
    if kind_of(o) == "entry" and dh.get("endpoint") == "POST /v2/super/orders":
        lines.append("  A Dhan super order: the entry, then a target leg and a stop-loss leg; when one leg")
        lines.append("  fills the other is cancelled. Dhan squares off intraday at 15:10 anyway.")
    return lines


DHAN = core.Broker(
    key="dhan", name="Dhan", script="dhan.py", id_label="security_id",
    has_keys=lambda: bool(nse.dhan_credentials()),
    keys_hint=("live mode needs DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN exported in your shell "
               "(never paste the token in the chat)"),
    secrets=_secrets, file_secrets=_file_secrets, describe=describe, paper_note=PAPER_NOTE, read_flag="--dhan")


def parse_yes(reply, order, shown=False, batch_symbols=()):
    return core.parse_yes(reply, order, shown, batch_symbols, DHAN.script)


def status_of(order, risk, st, today):
    return core.status_of(order, risk, st, today, DHAN.script)


def render(o, idx, total, mode, risk, st, today):
    return core.render(o, idx, total, mode, risk, st, today, DHAN)


def load_state(path):
    return core.load_state(path)


def save_state(path, st):
    core.save_state(path, st, DHAN)


def load_paper(path, cfg):
    return core.load_paper(path, cfg, PAPER_NOTE)


def gate(cfg, a, writes=True):
    return core.gate(cfg, a, DHAN, writes)


def client():
    if not nse.dhan_credentials():
        raise BrokerError("set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in your shell (never in the chat)")
    return nse.Dhan.from_env(transport=TRANSPORT)


_last_order_call = [0.0]


def order_call(d, method, path, body=None):
    """An order API call, paced to Dhan's 10 a second."""
    if TRANSPORT is None:
        wait = ORDER_GAP - (time.time() - _last_order_call[0])
        if wait > 0:
            time.sleep(wait)
    _last_order_call[0] = time.time()
    return d.call(method, path, body)


def read_call(d, path):
    try:
        return d.call("GET", path)
    except nse.NSEError as e:
        raise BrokerError(dhan_error(e))


# ---------------------------------------------------------------- commands

def cmd_show(a):
    return core.cmd_show(a, DHAN)


def cmd_approve(a):
    return core.cmd_approve(a, DHAN)


def cmd_decline(a):
    return core.cmd_decline(a, DHAN)


def cmd_send(a):
    return core.cmd_send(a, DHAN, send_live)


def cmd_paper_check(a):
    return core.cmd_paper_check(a, DHAN)


def cmd_log(a):
    return core.cmd_log(a, DHAN)


SANDBOX_HOST = "sandbox.dhan.co"


def is_sandbox(d):
    return SANDBOX_HOST in d.base


def sandbox_entry(body):
    """The super order's entry leg as a plain /orders request."""
    keep = ("correlationId", "transactionType", "exchangeSegment", "productType", "orderType",
            "securityId", "quantity", "price")
    out = dict((k, body[k]) for k in keep if k in body)
    out["validity"] = "DAY"
    return out


def send_live(a, paths, cfg, st, now, order, base, approval=None):
    if not (order.get("dhan") or {}).get("body"):
        raise BrokerError("not sent: this order has no Dhan request (is config \"broker\" angel? then use "
                          "angel_broker.py, or run india-budget again)")
    d = client()
    body = dict((order.get("dhan") or {}).get("body") or {})
    body.pop("dhanClientId", None)
    if kind_of(order) == "exit":
        return send_live_exit(d, paths, st, now, order, base, body)
    path = "/super/orders"
    if is_sandbox(d):
        # Dhan's sandbox has no super orders (docs.dhanhq.co, Sandbox: only /orders and
        # friends are sandbox-enabled), so the entry goes as a plain LIMIT order there.
        path = "/orders"
        body = sandbox_entry(body)
        print("SANDBOX (%s): super orders are not sandbox-enabled, so this sends the entry as a plain "
              "LIMIT order to /orders. The stop and target legs are not tested here." % d.base)
    try:
        resp = order_call(d, "POST", path, dict(body, dhanClientId=d.client_id))
    except nse.NSEError as e:
        msg = dhan_error(e)
        definite = bool(re.search(r"DH-9\d\d|HTTP 4\d\d", scrub(e)))
        log(st, now, status="failed" if definite else "unknown", request={"method": "POST", "path": "/v2" + path,
                                                                           "body": body},
            error=msg, **base)
        save_state(paths.broker, st)
        if definite:
            raise BrokerError(msg)
        raise BrokerError("%s\nThe order MAY have reached Dhan. Run `dhan.py status` before anything else; "
                          "do not send again." % msg)
    status = "rejected" if str(resp.get("orderStatus", "")).upper() == "REJECTED" else "sent"
    log(st, now, status=status, broker_order_id=resp.get("orderId"),
        request={"method": "POST", "path": "/v2" + path, "body": body}, response=resp, **base)
    print("LIVE: Dhan order %s, status %s: %s" % (resp.get("orderId"), resp.get("orderStatus"), ticket_line(order)))
    if status == "rejected":
        print("Dhan REJECTED it. `dhan.py status` shows why (omsErrorDescription).")
        return 1
    print("Check it with `dhan.py status`. Be flat by %s: `dhan.py flatten --symbol %s`."
          % (order.get("flat_by") or "15:05", order["symbol"]))
    return 0


class DhanExit(core.ExitOps):
    """drive_exit's view of one Dhan exit sell. GET /orders/{id} (orderStatus, filledQty) and
    PUT /orders/{id} (modify) follow dhanhq.co/docs/v2; the modify quantity's meaning after a partial
    fill is not verified. No stop is re-placed here (a plain Dhan stop-loss body is not verified): an
    unfilled exit is left working and the user is told loudly."""

    def __init__(self, d, sell, oid_, sid, exch):
        self.d, self.sell, self.oid, self.sid, self.exch = d, dict(sell), str(oid_), str(sid), exch
        self.base, self.cur, self.state = 0, 0, "open"

    def poll(self):
        r = self.d.call("GET", "/orders/%s" % self.oid) or {}
        r = r[0] if isinstance(r, list) and r else r
        st = str(r.get("orderStatus", "")).upper()
        self.cur = int(_f(r.get("filledQty")) or 0)
        if st == "TRADED" and not self.cur:
            self.cur = int(_f(r.get("quantity")) or 0)
        self.state = "filled" if st == "TRADED" else ("dead" if st in ("REJECTED", "CANCELLED", "EXPIRED") else "open")
        return self.state, self.base + self.cur

    def net(self):
        pos = self.d.call("GET", "/positions") or []
        return sum(int(_f(p.get("netQty")) or 0) for p in (pos if isinstance(pos, list) else [])
                   if str(p.get("securityId")) == self.sid and str(p.get("productType", "")).upper() == "INTRADAY")

    def ltp(self):
        return _dhan_ltp(self.d, self.sid, self.exch)

    def reprice(self, total, price):
        if self.state == "dead":
            self.base += self.cur
            resp = order_call(self.d, "POST", "/orders", dict(self.sell, quantity=int(total - self.base), price=price,
                                                              dhanClientId=self.d.client_id))
            self.oid, self.cur, self.state = str(resp.get("orderId")), 0, "open"
            return "re-placed as order %s" % self.oid
        order_call(self.d, "PUT", "/orders/%s" % self.oid, {
            "dhanClientId": self.d.client_id, "orderId": self.oid, "orderType": "LIMIT", "legName": "",
            "quantity": int(total - self.base), "price": price, "disclosedQuantity": 0, "triggerPrice": 0,
            "validity": "DAY"})
        return "PUT /v2/orders/%s" % self.oid

    def cancel(self):
        if self.state in ("filled", "dead"):
            return True
        order_call(self.d, "DELETE", "/orders/%s" % self.oid)
        return True


def send_live_exit(d, paths, st, now, order, base, body):
    """Get out: cancel the stock's open super-order legs, read the position, sell what is held at the
    ticket's marketable limit."""
    sid = str(order.get("security_id"))
    steps = []
    try:
        supers = d.call("GET", "/super/orders") or []
        for so in supers if isinstance(supers, list) else []:
            if str(so.get("securityId")) != sid:
                continue
            oid, s = so.get("orderId"), str(so.get("orderStatus", "")).upper()
            if s in OPEN_STATES:
                r = order_call(d, "DELETE", "/super/orders/%s/ENTRY_LEG" % oid)
                steps.append({"cancel": oid, "leg": "ENTRY_LEG", "response": r})
            elif s == "TRADED":
                for leg in so.get("legDetails") or []:
                    if leg.get("legName") in ("TARGET_LEG", "STOP_LOSS_LEG") and \
                            str(leg.get("orderStatus", "")).upper() in OPEN_STATES:
                        r = order_call(d, "DELETE", "/super/orders/%s/%s" % (oid, leg["legName"]))
                        steps.append({"cancel": oid, "leg": leg["legName"], "response": r})
        pos = d.call("GET", "/positions") or []
        net = sum(int(_f(p.get("netQty")) or 0) for p in (pos if isinstance(pos, list) else [])
                  if str(p.get("securityId")) == sid and str(p.get("productType", "")).upper() ==
                  str(order.get("product") or "INTRADAY").upper())
        qty = min(int(order["qty"]), max(net, 0))
        if qty <= 0:
            log(st, now, status="sent", response={"note": "no open position left to sell"}, steps=steps, **base)
            print("LIVE: no open %s position left (net qty %d). Cancelled %d open leg(s)." % (order["symbol"], net, len(steps)))
            return 0
        sell = dict(body, quantity=qty)
        resp = order_call(d, "POST", "/orders", dict(sell, dhanClientId=d.client_id))
    except nse.NSEError as e:
        msg = dhan_error(e)
        log(st, now, status="failed", error=msg, steps=steps, **base)
        save_state(paths.broker, st)
        raise BrokerError(msg + "\nIf you are still in the position, close it in the Dhan app now.")
    log(st, now, status="sent", broker_order_id=resp.get("orderId"),
        request={"method": "POST", "path": "/v2/orders", "body": sell}, response=resp, steps=steps, **base)
    how = ("at %s limit" % p2(sell["price"])) if str(sell.get("orderType")).upper() == "LIMIT" else "at market"
    print("LIVE: sell %d %s %s, Dhan order %s (%s). Cancelled %d open leg(s) first."
          % (qty, order["symbol"], how, resp.get("orderId"), resp.get("orderStatus"), len(steps)))
    if str(sell.get("orderType")).upper() != "LIMIT" or not resp.get("orderId"):
        return 0
    cfg = nse.read_json(paths.config, {}) or {}
    ops = DhanExit(d, sell, resp["orderId"], order.get("security_id"), order.get("exchange") or "NSE")
    try:
        res = core.drive_exit(ops, qty, float(sell["price"]), _f(order.get("tick")) or nse.band_tick(sell["price"]),
                              int(cfg.get("exit_limit_ticks") or core.EXIT_LIMIT_TICKS),
                              int(cfg.get("exit_reprice_max") if cfg.get("exit_reprice_max") is not None
                                  else core.EXIT_REPRICE_MAX), None, order["symbol"],
                              time.sleep if TRANSPORT is None else None)
    except nse.NSEError as e:
        print("WARNING: FOLLOWING THE %s EXIT FAILED (%s). THE SHARES MAY HAVE NO STOP AND NO WORKING SELL: CHECK THE "
              "DHAN APP NOW." % (order["symbol"], dhan_error(e)))
        res = {"result": "unknown", "steps": []}
    log(st, now, status="exit-" + res["result"], mode="live", kind="exit-follow", symbol=order["symbol"],
        broker_order_id=ops.oid, fingerprint=base.get("fingerprint"), steps=res["steps"])
    return 0 if res["result"] in ("filled", "flat") else 1


def _dhan_ltp(d, sid, exch):
    """Dhan's own last price (needs the Data plan). Raises on any failure; the caller falls back."""
    data = d.ltp([sid], "%s_EQ" % exch)
    return float((data.get(str(sid)) or {})["last_price"])


def cmd_flatten(a):
    paths = Paths(a)
    cfg = nse.read_json(paths.config, {}) or {}
    now = core.clock(a)
    mode = gate(cfg, a, writes=False)
    st = load_state(paths.broker)
    sym = nse.clean_symbol(a.symbol)
    exch = str(cfg.get("exchange") or "NSE").upper()
    sid, series, qty, source, d = None, None, None, "", None
    if mode == "paper":
        held = core.paper_position(paths, cfg, st, now, sym, DHAN)
        if held is None:
            return 0
        qty, sid = held
        source = "the paper ledger"
    else:
        d = client()
        pos = read_call(d, "/positions") or []
        keep = []
        for p in pos if isinstance(pos, list) else []:
            keep.append({"symbol": str(p.get("tradingSymbol") or "").upper(), "securityId": str(p.get("securityId")),
                         "productType": p.get("productType"), "netQty": p.get("netQty"), "buyAvg": p.get("buyAvg"),
                         "realizedProfit": p.get("realizedProfit"), "unrealizedProfit": p.get("unrealizedProfit"),
                         "positionType": p.get("positionType")})
        st["snapshots"].append({"at": nse.iso_ist(now), "date": now.date().isoformat(), "what": "positions",
                                "positions": keep})
        mine = [p for p in keep if (p["symbol"] == sym or p["symbol"].startswith(sym + "-"))
                and str(p.get("productType")).upper() == "INTRADAY"]
        qty = sum(int(_f(p["netQty"]) or 0) for p in mine)
        sid = mine[0]["securityId"] if mine else None
        for p in mine:
            p["symbol"] = sym
        source = "Dhan GET /v2/positions"
    if a.qty is not None:
        qty = int(a.qty)
    if not qty or qty <= 0:
        save_state(paths.broker, st)
        raise BrokerError("nothing to flatten: no open %s position in %s" % (sym, source))
    scrip_fn = lambda: nse.scrip_master(offline=getattr(a, "scrip_offline", None), exchange=exch)  # noqa: E731
    tick, row = core.tick_for(paths, sym, scrip_fn)
    if not sid:
        try:
            row = row or scrip_fn()[sym]
            sid, series = row["security_id"], row["series"]
        except (nse.NSEError, KeyError, TypeError):
            raise BrokerError("no Dhan security_id for %s" % sym)
    plan = core.plan_for(paths, sym)
    floor = None
    if _f(plan.get("stop")):
        floor = core.stop_limit_price(plan["stop"], tick, int(cfg.get("stop_limit_buffer_ticks")
                                                              or core.STOP_LIMIT_BUFFER_TICKS))
    ltp = (lambda: _dhan_ltp(d, sid, exch)) if d is not None else None
    limit, how = core.exit_price(a, cfg, now, paths, sym, exch, tick, floor, ltp)
    flat = plan.get("flat_by") or nse.flat_by(cfg, now.date()).strftime("%H:%M")
    body = {"transactionType": "SELL", "exchangeSegment": exch + "_EQ", "productType": "INTRADAY",
            "orderType": "LIMIT", "validity": "DAY", "securityId": str(sid), "quantity": int(qty), "price": limit,
            "correlationId": ("india-flat-%s-%s" % (re.sub(r"[^A-Za-z0-9_-]", "", sym), now.strftime("%H%M")))[:30]}
    order = {"symbol": sym, "exchange": exch, "series": series, "security_id": str(sid), "side": "sell",
             "qty": int(qty), "product": "INTRADAY", "order_type": "LIMIT", "limit": limit, "tick": tick,
             "flat_by": flat, "kind": "exit",
             "why": "flat by %s IST: closes the %s intraday position (qty from %s); limit = %s"
                    % (flat, sym, source, how),
             "dhan": {"endpoint": "POST /v2/orders", "body": body}}
    save_state(paths.broker, st)
    core.write_exit(paths, now, mode, order, DHAN)
    print("Sell limit %s: %s. A marketable LIMIT, not MARKET (market-price protection on API orders)."
          % (p2(limit), how))
    if mode == "live":
        print("At send it first cancels this stock's open super-order legs (DELETE /v2/super/orders/{id}/{leg}), "
              "then sells the net quantity Dhan reports at this limit.")
    return 0


def _print_json(x):
    print(scrub(json.dumps(x, indent=2)))


def _snapshot(paths, now, what, data):
    st = load_state(paths.broker)
    st["snapshots"].append({"at": nse.iso_ist(now), "date": now.date().isoformat(), "what": what, "data": data})
    st["snapshots"] = st["snapshots"][-200:]
    save_state(paths.broker, st)


def cmd_read(a):
    """status, trades, positions, funds, ip, profile."""
    paths = Paths(a)
    cfg = nse.read_json(paths.config, {}) or {}
    now = core.clock(a)
    mode = str(cfg.get("mode") or "paper").lower()
    if a.cmd in ("status", "trades", "positions", "funds") and mode == "paper" and not a.dhan:
        return core.print_paper_ledger(paths, cfg, now, DHAN)
    d = client()
    if a.cmd == "status":
        data = read_call(d, "/orders/%s" % a.order_id) if a.order_id else read_call(d, "/super/orders")
    elif a.cmd == "trades":
        data = read_call(d, "/trades")
    elif a.cmd == "positions":
        data = read_call(d, "/positions")
    elif a.cmd == "funds":
        data = read_call(d, "/fundlimit")
        # Dhan's docs spell it "availabelBalance".
        bal = data.get("availabelBalance", data.get("availableBalance")) if isinstance(data, dict) else None
        if bal is not None:
            print("Available balance: %s (Dhan /v2/fundlimit)" % nse.inr(float(bal)))
    elif a.cmd == "ip":
        data = read_call(d, "/ip/getIP")
        print("Static IPs registered with Dhan: primary %s (next change %s), secondary %s"
              % (data.get("primaryIP") or "none", data.get("modifyDatePrimary") or "-", data.get("secondaryIP") or "none"))
        print("Order APIs only accept requests from these. Compare with this machine's public IP.")
    else:
        data = read_call(d, "/profile")
        print("Token valid until %s. Data plan %s (until %s). Segments %s."
              % (data.get("tokenValidity"), data.get("dataPlan"), data.get("dataValidity"), data.get("activeSegment")))
        data = dict((k, v) for k, v in data.items() if k != "dhanClientId")
    _print_json(data)
    if a.cmd in ("status", "positions", "funds", "trades"):
        if a.cmd == "positions" and isinstance(data, list):
            data = [dict(p, symbol=str(p.get("tradingSymbol") or "").upper()) for p in data]
            for p in data:
                p.pop("dhanClientId", None)
            st = load_state(paths.broker)
            st["snapshots"].append({"at": nse.iso_ist(now), "date": now.date().isoformat(), "what": "positions",
                                    "positions": data})
            save_state(paths.broker, st)
        else:
            if isinstance(data, dict):
                data = dict((k, v) for k, v in data.items() if k != "dhanClientId")
            elif isinstance(data, list):
                data = [dict((k, v) for k, v in x.items() if k != "dhanClientId") if isinstance(x, dict) else x for x in data]
            _snapshot(paths, now, a.cmd, data)
    return 0


def cmd_cancel(a):
    paths = Paths(a)
    cfg = nse.read_json(paths.config, {}) or {}
    now = core.clock(a)
    mode = gate(cfg, a, writes=True)
    st = load_state(paths.broker)
    if mode == "paper":
        return core.cancel_paper(paths, cfg, st, now, a.order_id, DHAN)
    d = client()
    path = "/super/orders/%s/%s" % (a.order_id, a.leg) if a.leg else "/orders/%s" % a.order_id
    try:
        resp = order_call(d, "DELETE", path)
    except nse.NSEError as e:
        raise BrokerError(dhan_error(e))
    log(st, now, status="cancel", mode="live", broker_order_id=a.order_id, request={"method": "DELETE", "path": "/v2" + path},
        response=resp)
    save_state(paths.broker, st)
    print("LIVE: cancel %s: %s" % (path, (resp or {}).get("orderStatus", "accepted")))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Tickets, your yes, and Dhan (india-broker).")
    sub = ap.add_subparsers(dest="cmd")

    files = core.files_args
    prices = core.price_opts

    p = sub.add_parser("show")
    files(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve")
    files(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--yes", required=True, help="the user's reply, word for word")
    p = sub.add_parser("decline")
    files(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("send")
    files(p)
    prices(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--live", action="store_true")
    p = sub.add_parser("paper-check")
    files(p)
    prices(p)
    p = sub.add_parser("flatten")
    files(p)
    prices(p)
    p.add_argument("--symbol", required=True)
    p.add_argument("--qty", type=int)
    p.add_argument("--limit", type=float, help="your own sell limit instead of last price minus a few ticks")
    p.add_argument("--live", action="store_true")
    p.add_argument("--scrip-offline")
    for name in ("status", "trades", "positions", "funds", "ip", "profile"):
        p = sub.add_parser(name)
        files(p)
        p.add_argument("--dhan", action="store_true", help="paper mode: read the real Dhan account anyway")
        if name == "status":
            p.add_argument("--order-id")
    p = sub.add_parser("cancel")
    files(p)
    p.add_argument("--order-id", required=True)
    p.add_argument("--leg", choices=["ENTRY_LEG", "TARGET_LEG", "STOP_LOSS_LEG"])
    p.add_argument("--live", action="store_true")
    p = sub.add_parser("log")
    files(p)
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    if a.cmd in ("ip", "profile"):
        a.dhan = True
    fn = {"show": cmd_show, "approve": cmd_approve, "decline": cmd_decline, "send": cmd_send,
          "paper-check": cmd_paper_check, "flatten": cmd_flatten, "cancel": cmd_cancel, "log": cmd_log}.get(a.cmd, cmd_read)
    try:
        return fn(a)
    except (BrokerError, nse.NSEError) as e:
        print("dhan.py: %s" % scrub(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
