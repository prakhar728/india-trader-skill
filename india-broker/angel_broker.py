#!/usr/bin/env python3
"""Order tickets, your yes, and Angel One (SmartAPI) for india-broker. Standard library only.

The Angel One twin of dhan.py, with the same safety rules (they live in
ticket_core.py, shared): an order goes to Angel One only when india-risk
passed THIS exact order today, the user's yes for it was recorded word for
word in the last 10 minutes (one yes, one send), Claude Code's permission
prompt for the command was allowed, and, live only, "mode": "live" in
trader/config.json AND --live on the command AND the keys in the .env.

    python3 angel_broker.py show                          # every ticket, READY or not
    python3 angel_broker.py approve --index 1 --yes "yes buy 21 RELIANCE" \\
                                    [--exit-yes "yes, exit at the target or flat-by"]
    python3 angel_broker.py send --index 1 [--live]       # the NORMAL LIMIT INTRADAY entry
    python3 angel_broker.py status [--angel]              # fills; says when the entry has filled
    python3 angel_broker.py protect [--symbol S] --live   # the exchange-held STOPLOSS_LIMIT sell
    python3 angel_broker.py check [--live]                # one pass: target / flat-by / stop
    python3 angel_broker.py watch [--every 30] [--live]   # check in a loop until flat
    python3 angel_broker.py flatten --symbol RELIANCE     # the exit ticket (marketable LIMIT)
    python3 angel_broker.py orders | trades | positions | funds [--angel]
    python3 angel_broker.py profile | ip                  # session check; static IP vs config
    python3 angel_broker.py cancel --order-id ID [--variety STOPLOSS] --live
    python3 angel_broker.py paper-check | decline --index N | log [--all]

Angel One has no one-cancels-other order this pack can use (ROBO bracket
orders are not confirmed for NSE intraday via the API in 2026), and Angel's
April 2026 notice prohibits MARKET and IOC orders for algorithmic trading.
So a live trade is three steps:

  1. send     the LIMIT INTRADAY buy.
  2. protect  once it has filled (status tells you), the STOPLOSS_LIMIT sell
              for the filled quantity: held by the exchange, trigger = the
              stop, limit a few ticks under it.
  3. check    watches the last price (Angel's quote). At the target, or at
              flat-by, it cancels the stop and then sells with a marketable
              LIMIT (the last price minus a few ticks, never under the
              stop-limit floor). That sell needs the user's yes, unless they
              pre-approved "exit at the target / flat-by" when they approved
              the entry (--exit-yes, recorded word for word; default: ask).

The target is NOT placed as a resting limit sell next to the stop: both are
INTRADAY sells of the same shares, and if both filled the account would be
short.

Keys come from the .env in the trading folder (ANGEL_API_KEY,
ANGEL_CLIENT_CODE, ANGEL_PIN, ANGEL_TOTP_SECRET), never from chat. Each
command logs in with a fresh TOTP; the session token lives only in memory.
Nothing secret is printed or written to trader/broker.json.
"""

import argparse
import datetime
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "india-common"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse  # noqa: E402
import angel  # noqa: E402
import ticket_core as core  # noqa: E402
from ticket_core import BrokerError, Paths, fingerprint, _f, p2, kind_of, log, ticket_line  # noqa: E402

# Tests inject a fake HTTP transport here (same signature as nse.http_get).
TRANSPORT = None
public_ip = angel.public_ip          # tests swap this too
ORDER_GAP = 0.12                     # Angel: at most 9-10 orders a second
CANCEL_CHECKS = 5                    # re-reads of the order book to confirm a stop's cancel
# Order-book status words. From Angel's SmartAPI docs, NOT from the SDK: unverified.
FILLED = ("complete",)
DEAD = ("cancelled", "rejected")
_CLIENT = [None]


# ---------------------------------------------------------------- keys and secrets

def has_keys():
    return not angel.credentials()[1]


def _secrets():
    vals, _ = angel.credentials()
    out = [vals.get(k) for k in angel.SECRET_KEYS]
    c = _CLIENT[0]
    if c is not None:
        out += [c.jwt, c.feed_token]
    return [x for x in out if x]


def _file_secrets():
    # A 4-digit PIN can match an order id or a price by chance; it is never logged, so only the
    # long secrets make broker.json refuse to save.
    return [x for x in _secrets() if len(x) >= 8]


def clean(text):
    text = str(text)
    if _CLIENT[0] is not None:
        text = _CLIENT[0]._clean(text)
    return ANGEL.scrub(angel.scrub(text))


def describe(o, mode):
    blk = o.get("angel") or {}
    lines = ["  Angel One request%s: %s" % (" (sent only in live mode)" if mode == "paper" else "", blk.get("route"))]
    lines.append("    " + json.dumps(blk.get("body")))
    if kind_of(o) == "entry":
        lines.append("  then, once the entry has filled (`angel_broker.py protect`), the stop held at the exchange:")
        lines.append("    " + json.dumps(blk.get("stop_order")))
        lines.append("  The target is watched by `angel_broker.py check`, not placed as a resting sell (a resting")
        lines.append("  target and the stop could both fill and leave you short). At the target or at flat-by it")
        lines.append("  cancels the stop and sells with a marketable LIMIT, after asking you, unless you")
        lines.append("  pre-approve that exit now: \"yes, exit at the target or flat-by\". Default: ask.")
    else:
        lines.append("  At send it first cancels this stock's open stop orders, then sells the net quantity")
        lines.append("  Angel One reports at this limit (Angel One takes no market orders from algos).")
    lines.append("    (your API key and session token go in headers from .env and are never shown)")
    return lines


ANGEL = core.Broker(
    key="angel", name="Angel One", script="angel_broker.py", id_label="token", has_keys=has_keys,
    keys_hint=("live mode needs ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_PIN and ANGEL_TOTP_SECRET in the .env "
               "in your trading folder (never paste them in the chat)"),
    secrets=_secrets, file_secrets=_file_secrets, describe=describe, read_flag="--angel")


def client():
    vals, missing = angel.credentials()
    if missing:
        raise BrokerError("set %s in the .env in your trading folder (never in the chat)" % ", ".join(missing))
    c = angel.Angel(vals["ANGEL_API_KEY"], vals["ANGEL_CLIENT_CODE"], vals["ANGEL_PIN"], vals["ANGEL_TOTP_SECRET"],
                    transport=TRANSPORT)
    _CLIENT[0] = c
    return c


def angel_error(e):
    """An Angel One failure in plain words. The error codes are not matched (not verified)."""
    msg = clean(e)
    if re.search(r"\bIP\b|static", msg, re.I):
        return ("Angel One refused this from this machine's IP (%s). Order calls must come from the static IP "
                "registered on your SmartAPI app: run `angel_broker.py ip`. The order was NOT placed." % msg[:200])
    if re.search(r"HTTP 429|rate limit|too many|exceed", msg, re.I):
        return ("Angel One's rate limit: too many requests (orders: at most 9-10 a second). Wait a few seconds. "
                "Nothing was retried automatically. (%s)" % msg[:200])
    if re.search(r"HTTP 401|HTTP 403|invalid token|session|jwt|totp", msg, re.I):
        return ("Angel One refused the login or the session (%s). Check ANGEL_* in your .env (the TOTP secret is "
                "the one from SmartAPI's Enable TOTP page) and the machine's clock; never paste them in the chat."
                % msg[:200])
    return "Angel One: %s" % msg[:300]


def definite(e):
    """True when Angel One clearly answered no (so nothing was placed); False when the answer is unknown."""
    msg = str(e)
    return bool(re.match(r"Angel (?!sent)", msg) or re.search(r"HTTP 4\d\d", msg))


_last_order_call = [0.0]


def _pace():
    if TRANSPORT is None:
        wait = ORDER_GAP - (time.time() - _last_order_call[0])
        if wait > 0:
            time.sleep(wait)
    _last_order_call[0] = time.time()


def place(c, body):
    angel.check_body(body)
    _pace()
    return c.place(body)


def cancel(c, variety, orderid):
    _pace()
    return c.cancel(variety, orderid)


def read(fn):
    try:
        return fn()
    except angel.AngelError as e:
        raise BrokerError(angel_error(e))


# ---------------------------------------------------------------- order book and positions
# Field names (orderid, status / orderstatus, filledshares, averageprice, symboltoken, variety,
# transactiontype, netqty, producttype, realised) are from Angel's SmartAPI docs, not the SDK.

def ostatus(row):
    return str((row or {}).get("orderstatus") or (row or {}).get("status") or "").strip().lower()


def oid(row):
    return str((row or {}).get("orderid") or "")


def filled_qty(row):
    n = int(_f((row or {}).get("filledshares")) or 0)
    if not n and ostatus(row) in FILLED:
        n = int(_f((row or {}).get("quantity")) or 0)
    return n


def norm_positions(rows):
    out = []
    for p in rows if isinstance(rows, list) else []:
        ts = str(p.get("tradingsymbol") or "").upper()
        out.append({"symbol": ts[:-3] if ts.endswith("-EQ") else ts, "securityId": str(p.get("symboltoken")),
                    "productType": str(p.get("producttype") or "").upper(), "netQty": int(_f(p.get("netqty")) or 0),
                    "realizedProfit": _f(p.get("realised")), "unrealizedProfit": _f(p.get("unrealised"))})
    return out


def snapshot_positions(st, now, rows):
    st["snapshots"].append({"at": nse.iso_ist(now), "date": now.date().isoformat(), "what": "positions",
                            "positions": rows})
    st["snapshots"] = st["snapshots"][-200:]


def managed(st, today, sym=None, open_only=True):
    out = []
    for t in st.setdefault("managed", []):
        if t.get("date") != today or (sym and t.get("symbol") != sym):
            continue
        if open_only and t.get("state") not in ("open", "exiting"):
            continue
        out.append(t)
    return out


def protected_qty(t, retry=False):
    skip = ("cancelled", "rejected", "failed") + (("unknown",) if retry else ())
    return sum(int(s.get("qty") or 0) for s in t.get("stops", []) if s.get("status") not in skip)


def refresh(t, by_id):
    """Update a managed trade from the order book."""
    e = by_id.get(str(t.get("entry_order_id")))
    if e:
        t["entry_status"] = ostatus(e)
        t["filled"] = filled_qty(e)
        if _f(e.get("averageprice")):
            t["avg"] = _f(e.get("averageprice"))
    for s in t.get("stops", []) + t.get("exits", []):
        r = by_id.get(str(s.get("orderid")))
        if r:
            s["status"] = ostatus(r)
            s["filled"] = filled_qty(r)
    out = sum(int(s.get("filled") or 0) for s in t.get("stops", []) + t.get("exits", [])
              if s.get("status") in FILLED)
    if t.get("filled") and out >= t["filled"]:
        t["state"] = "closed"
    elif t.get("entry_status") in DEAD and not t.get("filled"):
        t["state"] = "closed"
    return t


def book(c):
    rows = read(c.orders) or []
    return rows, dict((oid(r), r) for r in rows if oid(r))


# ---------------------------------------------------------------- exit orders

def exit_order(sym, exch, sid, series, tsym, qty, limit, tick, flat, why):
    body = angel.order_body(tsym, sid, "SELL", qty, limit, exchange=exch)
    return {"symbol": sym, "exchange": exch, "series": series or "EQ", "security_id": str(sid), "side": "sell",
            "qty": int(qty), "product": "INTRADAY", "order_type": "LIMIT", "limit": limit, "tick": tick,
            "flat_by": flat, "kind": "exit", "why": why, "angel": {"route": "placeOrder", "body": body}}


def floor_for(st, today, sym, paths, tick, cfg):
    """The stop-limit price of today's stop for sym: an exit is never priced under it."""
    for t in reversed(st.get("managed", [])):
        if t.get("date") == today and t.get("symbol") == sym and _f((t.get("stop_order") or {}).get("price")):
            return _f(t["stop_order"]["price"])
    plan = core.plan_for(paths, sym)
    if _f(plan.get("stop")):
        return core.stop_limit_price(plan["stop"], tick, int(cfg.get("stop_limit_buffer_ticks")
                                                              or core.STOP_LIMIT_BUFFER_TICKS))
    return None


def confirm_cancelled(c, ids):
    """Re-read the order book until every id is cancelled, rejected or complete. (ok, statuses)."""
    seen = {}
    for i in range(CANCEL_CHECKS):
        _, by = book(c)
        seen = dict((x, ostatus(by.get(x))) for x in ids)
        if all(s in DEAD + FILLED for s in seen.values()):
            return True, seen
        if TRANSPORT is None:
            time.sleep(0.4 * (i + 1))
    return False, seen


class AngelExit(core.ExitOps):
    """drive_exit's view of one Angel One exit sell (see ticket_core.drive_exit)."""

    def __init__(self, c, body, oid_, token, stop_body=None):
        self.c, self.body, self.oid, self.token = c, dict(body), oid_, str(token)
        self.base = 0                    # shares filled by earlier exit orders that died
        self.state, self.cur = "open", 0
        if stop_body:
            self.restop = lambda qty: str((place(c, dict(stop_body, quantity=str(int(qty)))) or {}).get("orderid") or "")

    def poll(self):
        _, by = book(self.c)
        r = by.get(self.oid)
        st = ostatus(r)
        self.cur = filled_qty(r)
        self.state = "filled" if st in FILLED else ("dead" if st in DEAD else "open")
        return self.state, self.base + self.cur

    def net(self):
        return sum(p["netQty"] for p in norm_positions(read(self.c.positions) or [])
                   if p["securityId"] == self.token and p["productType"] == "INTRADAY")

    def ltp(self):
        return angel.ltps(self.c.quote([self.token], "LTP")).get(self.token)

    def reprice(self, total, price):
        if self.state == "dead":         # rejected or cancelled: a new sell for what is left
            self.base += self.cur
            data = place(self.c, dict(self.body, quantity=str(int(total - self.base)), price=angel.money(price)))
            self.oid, self.cur, self.state = str((data or {}).get("orderid") or ""), 0, "open"
            return "re-placed as order %s" % self.oid
        b = self.body
        mb = {"variety": "NORMAL", "orderid": self.oid, "ordertype": "LIMIT", "producttype": b["producttype"],
              "duration": "DAY", "price": angel.money(price), "quantity": str(int(total - self.base)),
              "tradingsymbol": b["tradingsymbol"], "symboltoken": b["symboltoken"], "exchange": b["exchange"]}
        _pace()
        self.c.modify(mb)
        return "modifyOrder %s" % self.oid

    def cancel(self):
        if self.state in ("filled", "dead"):
            return True
        cancel(self.c, "NORMAL", self.oid)
        return confirm_cancelled(self.c, [self.oid])[0]


def stop_for(st, today, sym):
    for t in reversed(st.get("managed", [])):
        if t.get("date") == today and t.get("symbol") == sym and t.get("stop_order"):
            return t["stop_order"]
    return None


def follow_exit(c, paths, st, now, order, sell, oid_, base):
    """Poll the exit sell, re-price it, and if it never fills put the stop back (or warn loudly)."""
    cfg = nse.read_json(paths.config, {}) or {}
    stop_body = stop_for(st, now.date().isoformat(), order["symbol"])
    ops = AngelExit(c, sell, oid_, order["security_id"], stop_body)
    tick = _f(order.get("tick")) or nse.band_tick(_f(sell["price"]))
    try:
        res = core.drive_exit(ops, int(sell["quantity"]), _f(sell["price"]), tick,
                              int(cfg.get("exit_limit_ticks") or core.EXIT_LIMIT_TICKS),
                              int(cfg.get("exit_reprice_max") if cfg.get("exit_reprice_max") is not None
                                  else core.EXIT_REPRICE_MAX),
                              _f((stop_body or {}).get("triggerprice")), order["symbol"],
                              time.sleep if TRANSPORT is None else None)
    except (angel.AngelError, BrokerError) as e:
        print("WARNING: FOLLOWING THE %s EXIT FAILED (%s). THE SHARES MAY HAVE NO STOP AND NO WORKING SELL: CHECK THE "
              "ANGEL ONE APP NOW." % (order["symbol"], angel_error(e)))
        res = {"result": "unknown", "steps": []}
    log(st, now, status="exit-" + res["result"], mode="live", kind="exit-follow", symbol=order["symbol"],
        broker_order_id=ops.oid, fingerprint=base.get("fingerprint"), steps=res["steps"])
    for t in managed(st, now.date().isoformat(), order["symbol"]):
        for s_ in res["steps"]:
            if s_.get("restop"):
                t.setdefault("stops", []).append({"orderid": s_["restop"], "qty": s_["qty"], "at": nse.iso_ist(now),
                                                  "status": "sent"})
                t["state"] = "open"
        if res["result"] == "unfilled":
            t["state"] = "exiting"
    if res["result"] == "filled":
        print("LIVE: the %s exit filled at %s or better." % (order["symbol"], p2(res.get("price") or _f(sell["price"]))))
    return 0 if res["result"] in ("filled", "flat") else 1


def send_live_exit(c, paths, st, now, order, base, body):
    """Get out: cancel the stock's open stop orders (and confirm), read the position, sell what is held
    at the ticket's marketable limit. Never a market order."""
    token = str(order.get("security_id"))
    steps = []
    try:
        rows, _ = book(c)
        stops = [r for r in rows if str(r.get("symboltoken")) == token
                 and str(r.get("transactiontype", "")).upper() == "SELL"
                 and ("STOPLOSS" in str(r.get("variety", "")).upper() or "STOPLOSS" in str(r.get("ordertype", "")).upper())
                 and ostatus(r) not in FILLED + DEAD]
        for r in stops:
            resp = cancel(c, r.get("variety") or "STOPLOSS", oid(r))
            steps.append({"cancel": oid(r), "variety": r.get("variety") or "STOPLOSS", "response": resp})
        if stops:
            ok, seen = confirm_cancelled(c, [oid(r) for r in stops])
            steps.append({"confirmed": seen})
            if not ok:
                log(st, now, status="failed", error="stop cancel not confirmed: %s" % seen, steps=steps, **base)
                core.save_state(paths.broker, st, ANGEL)
                raise BrokerError("the stop order(s) did not confirm their cancel (%s). Nothing was sold: selling now "
                                  "could leave you short if a stop also fills. Your stop is most likely STILL "
                                  "WORKING (status above), so the shares are still protected. Run `angel_broker.py "
                                  "orders`, then flatten again." % ", ".join("%s %s" % kv for kv in seen.items()))
        pos = norm_positions(read(c.positions) or [])
        snapshot_positions(st, now, pos)
        net = sum(p["netQty"] for p in pos if p["securityId"] == token and p["productType"] == "INTRADAY")
        qty = min(int(order["qty"]), max(net, 0))
        if qty <= 0:
            log(st, now, status="sent", response={"note": "no open position left to sell"}, steps=steps, **base)
            print("LIVE: no open %s position left (net qty %d). Cancelled %d stop order(s)."
                  % (order["symbol"], net, len(stops)))
            return 0
        sell = dict(body, quantity=str(qty))
        data = place(c, sell)
    except angel.AngelError as e:
        msg = angel_error(e)
        log(st, now, status="failed" if definite(e) else "unknown", error=msg, steps=steps, **base)
        core.save_state(paths.broker, st, ANGEL)
        raise BrokerError(msg + "\nIf you are still in the position, close it in the Angel One app now.")
    oid_ = str((data or {}).get("orderid") or "")
    log(st, now, status="sent" if oid_ else "unknown", broker_order_id=oid_ or None,
        request={"route": "placeOrder", "body": sell}, response=data, steps=steps, **base)
    for t in managed(st, now.date().isoformat(), order["symbol"]):
        t.setdefault("exits", []).append({"orderid": oid_ or None, "qty": qty, "price": order.get("limit"),
                                          "at": nse.iso_ist(now), "status": "sent" if oid_ else "unknown",
                                          "reason": base.get("reason") or "exit"})
        t["state"] = "exiting"
    print("LIVE: sell %d %s at %s limit, Angel One order %s. Cancelled %d stop order(s) first."
          % (qty, order["symbol"], p2(_f(sell["price"])), oid_ or "(no id: check `angel_broker.py orders`)",
             len(stops)))
    if not oid_:
        print("WARNING: NO ORDER ID CAME BACK FOR THE %s SELL AND THE STOP IS CANCELLED. CHECK THE ANGEL ONE APP NOW."
              % order["symbol"])
        return 1
    return follow_exit(c, paths, st, now, order, sell, oid_, base)


# ---------------------------------------------------------------- send (the entry)

def send_live(a, paths, cfg, st, now, order, base, approval=None):
    blk = order.get("angel") or {}
    body = dict(blk.get("body") or {})
    if not body:
        raise BrokerError("not sent: this order has no Angel One request (is config \"broker\" dhan? then use "
                          "dhan.py; or run india-budget again with \"broker\": \"angel\")")
    try:
        angel.check_body(body)
        if kind_of(order) == "entry":
            angel.check_body(blk.get("stop_order") or {})
    except angel.AngelError as e:
        raise BrokerError("not sent: %s" % e)
    c = client()
    try:
        c.ensure()
    except angel.AngelError as e:
        raise BrokerError("not sent: the Angel One login failed, nothing was placed. %s" % angel_error(e))
    if kind_of(order) == "exit":
        return send_live_exit(c, paths, st, now, order, base, body)
    if not blk.get("stop_order"):
        raise BrokerError("not sent: the entry has no stop order to place after it fills (run india-budget again)")
    try:
        data = place(c, body)
    except angel.AngelError as e:
        msg = angel_error(e)
        ok = definite(e)
        log(st, now, status="failed" if ok else "unknown", request={"route": "placeOrder", "body": body},
            error=msg, **base)
        core.save_state(paths.broker, st, ANGEL)
        if ok:
            raise BrokerError(msg + " The order was NOT placed.")
        raise BrokerError("%s\nThe order MAY have reached Angel One. Run `angel_broker.py status` before anything "
                          "else; do not send again." % msg)
    oid_ = str((data or {}).get("orderid") or "")
    if not oid_:
        log(st, now, status="unknown", request={"route": "placeOrder", "body": body}, response=data, **base)
        core.save_state(paths.broker, st, ANGEL)
        raise BrokerError("Angel One answered without an order id: the order MAY be in. Run `angel_broker.py "
                          "status` before anything else; do not send again.")
    log(st, now, status="sent", broker_order_id=oid_, request={"route": "placeOrder", "body": body},
        response={"orderid": oid_, "uniqueorderid": (data or {}).get("uniqueorderid")}, **base)
    so = blk["stop_order"]
    st.setdefault("managed", []).append({
        "fingerprint": base["fingerprint"], "date": now.date().isoformat(), "symbol": order["symbol"],
        "exchange": order.get("exchange", "NSE"), "token": str(order.get("security_id")),
        "tradingsymbol": body.get("tradingsymbol"), "series": order.get("series"), "qty": int(order["qty"]),
        "entry": order.get("entry"), "stop": order.get("stop"), "target": order.get("target"),
        "tick": order.get("tick"), "flat_by": order.get("flat_by") or "15:05", "entry_order_id": oid_,
        "stop_order": so, "stops": [], "exits": [], "filled": 0, "entry_status": "sent", "state": "open",
        "exit_auto": bool((approval or {}).get("exit_auto")), "exit_reply": (approval or {}).get("exit_reply")})
    print("LIVE: Angel One order %s (entry, LIMIT %s): %s" % (oid_, body.get("price"), ticket_line(order)))
    print("There is NO stop yet. When `angel_broker.py status` says the entry has filled, run "
          "`angel_broker.py protect --symbol %s --live`: STOPLOSS_LIMIT SELL, trigger %s, limit %s."
          % (order["symbol"], so.get("triggerprice"), so.get("price")))
    print("Exits at the target %s / flat-by %s: %s." % (p2(order["target"]), order.get("flat_by") or "15:05",
                                                       "pre-approved by you (check sells without asking again)"
                                                       if (approval or {}).get("exit_auto") else
                                                       "check asks you first"))
    return 0


# ---------------------------------------------------------------- commands

def approval_extra(order, a):
    reply = getattr(a, "exit_yes", None)
    if not reply:
        return {"exit_auto": False}
    if kind_of(order) != "entry":
        print("Not recorded: --exit-yes belongs to an entry (it pre-approves that trade's exit).")
        return None
    ok, why = core.parse_exit_yes(reply)
    if not ok:
        print("Not recorded: the exit pre-approval %s. Ask the user again, or leave it out (default: ask at the "
              "target and at flat-by)." % why)
        return None
    return {"exit_auto": True, "exit_reply": reply}


def cmd_approve(a):
    code = core.cmd_approve(a, ANGEL, approval_extra)
    if code == 0:
        print("Exits at the target / flat-by: %s." % ("pre-approved (recorded word for word)" if getattr(a, "exit_yes", None)
                                                     else "you will be asked (the default)"))
    return code


def cmd_status(a):
    paths = Paths(a)
    cfg = nse.read_json(paths.config, {}) or {}
    now = core.clock(a)
    mode = str(cfg.get("mode") or "paper").lower()
    if mode == "paper" and not a.angel:
        return core.print_paper_ledger(paths, cfg, now, ANGEL)
    st = core.load_state(paths.broker)
    c = client()
    rows, by = book(c)
    today = now.date().isoformat()
    if getattr(a, "order_id", None):
        print(clean(json.dumps(by.get(str(a.order_id)) or {"note": "no order %s in today's order book" % a.order_id},
                               indent=2)))
    trades = managed(st, today, open_only=False)
    for t in trades:
        refresh(t, by)
        prot = protected_qty(t)
        print("%s entry %s: %s, filled %d of %d%s" % (t["symbol"], t["entry_order_id"], t.get("entry_status") or "?",
                                                      t.get("filled") or 0, t["qty"],
                                                      (" at %s" % p2(t["avg"])) if t.get("avg") else ""))
        for s in t.get("stops", []):
            print("  stop %s: %s, %s shares" % (s.get("orderid") or "-", s.get("status"), s.get("qty")))
        for s in t.get("exits", []):
            print("  exit %s: %s, %s shares at %s" % (s.get("orderid") or "-", s.get("status"), s.get("qty"),
                                                    p2(s["price"]) if s.get("price") else "-"))
        if t["state"] == "closed":
            print("  closed.")
        elif (t.get("filled") or 0) > prot:
            print("  FILLED, NOT PROTECTED: %d shares have no stop. Next: `angel_broker.py protect --symbol %s "
                  "--live` (STOPLOSS_LIMIT SELL, trigger %s, limit %s)."
                  % (t["filled"] - prot, t["symbol"], t["stop_order"].get("triggerprice"),
                     t["stop_order"].get("price")))
        elif t.get("filled"):
            print("  protected by the stop. `angel_broker.py check --live` watches the target %s and flat-by %s."
                  % (p2(t["target"]), t["flat_by"]))
        else:
            print("  waiting for the entry to fill.")
    if not trades:
        print("No Angel One trade sent from this pack today.")
    print("Today's order book: %d order(s)." % len(rows))
    for r in rows:
        print("  %s %-5s %-4s %s %s %s qty %s filled %s price %s trigger %s: %s %s" % (
            oid(r), r.get("variety"), r.get("transactiontype"), r.get("tradingsymbol"), r.get("ordertype"),
            r.get("producttype"), r.get("quantity"), r.get("filledshares"), r.get("price"), r.get("triggerprice"),
            ostatus(r), clean(r.get("text") or "")))
    core.save_state(paths.broker, st, ANGEL)
    return 0


def cmd_protect(a):
    paths = Paths(a)
    cfg = nse.read_json(paths.config, {}) or {}
    now = core.clock(a)
    mode = core.gate(cfg, a, ANGEL, writes=True)
    if mode == "paper":
        print("PAPER: the paper ledger models the stop already; `angel_broker.py paper-check` fills it. Nothing to place.")
        return 0
    st = core.load_state(paths.broker)
    today = now.date().isoformat()
    sym = nse.clean_symbol(a.symbol) if a.symbol else None
    trades = managed(st, today, sym)
    if not trades:
        raise BrokerError("no open Angel One entry from today%s to protect" % (" for %s" % sym if sym else ""))
    c = client()
    _, by = book(c)
    placed = 0
    for t in trades:
        refresh(t, by)
        if t["state"] != "open":
            continue
        f, prot = int(t.get("filled") or 0), protected_qty(t, a.retry)
        if f <= 0:
            print("%s: the entry %s is %s, nothing filled yet: no stop to place. Run `angel_broker.py status` again."
                  % (t["symbol"], t["entry_order_id"], t.get("entry_status")))
            continue
        want = f - prot
        if want <= 0:
            print("%s: all %d filled shares already have a stop." % (t["symbol"], f))
            continue
        body = dict(t["stop_order"], quantity=str(want))
        base = dict(fingerprint=t["fingerprint"], symbol=t["symbol"], side="sell", qty=want, kind="stop", mode="live")
        try:
            data = place(c, body)
        except angel.AngelError as e:
            msg = angel_error(e)
            state = "failed" if definite(e) else "unknown"
            t["stops"].append({"orderid": None, "qty": want, "at": nse.iso_ist(now), "status": state})
            log(st, now, status="protect-" + state, request={"route": "placeOrder", "body": body}, error=msg, **base)
            core.save_state(paths.broker, st, ANGEL)
            raise BrokerError("%s\nYou hold %d %s WITHOUT a stop%s. Place it in the Angel One app, or flatten."
                              % (msg, want, t["symbol"], "" if state == "failed" else
                                 " unless this one reached Angel One: check `angel_broker.py orders`; if it is not "
                                 "there, run protect again with --retry"))
        oid_ = str((data or {}).get("orderid") or "")
        t["stops"].append({"orderid": oid_ or None, "qty": want, "at": nse.iso_ist(now),
                           "status": "sent" if oid_ else "unknown"})
        log(st, now, status="protect", broker_order_id=oid_ or None, request={"route": "placeOrder", "body": body},
            response=data, **base)
        placed += 1
        print("LIVE: stop placed at the exchange for %d %s: STOPLOSS_LIMIT SELL, trigger %s, limit %s, Angel One "
              "order %s." % (want, t["symbol"], body["triggerprice"], body["price"], oid_ or "(no id: check orders)"))
        if t.get("entry_status") not in FILLED:
            print("  The entry is only partly filled (%d of %d): run protect again after more fills, or cancel the "
                  "rest of the entry." % (f, t["qty"]))
    core.save_state(paths.broker, st, ANGEL)
    return 0


def _flat_time(t, cfg, d):
    return min(nse.hhmm(t.get("flat_by") or "15:05"), nse.flat_by(cfg, d))


def cmd_check(a):
    paths = Paths(a)
    cfg = nse.read_json(paths.config, {}) or {}
    now = core.clock(a)
    mode = core.gate(cfg, a, ANGEL, writes=True)
    if mode == "paper":
        return core.cmd_paper_check(a, ANGEL)
    st = core.load_state(paths.broker)
    today = now.date().isoformat()
    sym = nse.clean_symbol(a.symbol) if getattr(a, "symbol", None) else None
    trades = managed(st, today, sym)
    if not trades:
        print("LIVE: no open Angel One trade from today to watch.")
        return 0
    c = client()
    _, by = book(c)
    for t in trades:
        refresh(t, by)
    held = [t for t in trades if t["state"] == "open" and t.get("filled")]
    given = core.price_args(a)
    ltp = {}
    if held:
        try:
            ltp = angel.ltps(c.quote(sorted(set(t["token"] for t in held)), "LTP"))
        except angel.AngelError as e:
            print("No price from Angel One's quote (%s)." % angel_error(e))
    ticks = int(cfg.get("exit_limit_ticks") or core.EXIT_LIMIT_TICKS)
    for t in trades:
        flat = _flat_time(t, cfg, now.date())
        if t["state"] == "closed":
            print("%s: closed." % t["symbol"])
            continue
        if t["state"] == "exiting":
            print("%s: the exit sell is in (%s). `angel_broker.py status` shows whether it filled."
                  % (t["symbol"], ", ".join("%s %s" % (s.get("orderid"), s.get("status")) for s in t["exits"])))
            continue
        f = int(t.get("filled") or 0)
        if f <= 0:
            if now.time() >= flat:
                print("%s: the entry %s has not filled and it is past flat-by %s: cancel it with "
                      "`angel_broker.py cancel --order-id %s --live`." % (t["symbol"], t["entry_order_id"],
                                                                           flat.strftime("%H:%M"), t["entry_order_id"]))
            else:
                print("%s: waiting for the entry %s to fill (%s)." % (t["symbol"], t["entry_order_id"],
                                                                      t.get("entry_status")))
            continue
        if protected_qty(t) < f:
            print("%s: WARNING %d filled shares have no stop: `angel_broker.py protect --symbol %s --live`."
                  % (t["symbol"], f - protected_qty(t), t["symbol"]))
        last = given.get(t["symbol"], given.get("*")) or ltp.get(t["token"])
        reason = None
        if last is not None and last >= float(t["target"]) - 1e-9:
            reason = "target %s" % p2(t["target"])
        elif now.time() >= flat:
            reason = "flat-by %s" % flat.strftime("%H:%M")
        if not reason:
            print("%s: holding %d, last %s, stop %s, target %s, flat by %s."
                  % (t["symbol"], f, p2(last) if last is not None else "?", p2(t["stop"]), p2(t["target"]),
                     flat.strftime("%H:%M")))
            continue
        if last is None:
            print("%s: %s reached but there is no price to set the sell limit: run `angel_broker.py flatten --symbol "
                  "%s --price %s=P --live` with the price from the app." % (t["symbol"], reason, t["symbol"],
                                                                           t["symbol"]))
            continue
        tick = _f(t.get("tick")) or nse.band_tick(last)
        limit, how = core.marketable_sell(last, tick, ticks, _f(t["stop_order"].get("price")))
        exited = sum(int(s.get("qty") or 0) for s in t.get("exits", []) if s.get("status") not in DEAD + ("failed",))
        order = exit_order(t["symbol"], t["exchange"], t["token"], t.get("series"), t["tradingsymbol"], f - exited,
                           limit, tick, t["flat_by"], "%s reached: closes the %s position; limit = %s"
                           % (reason, t["symbol"], how))
        if t.get("exit_auto"):
            print("%s: %s reached (last %s). You pre-approved this exit (\"%s\"): cancelling the stop, then selling %d "
                  "at %s limit." % (t["symbol"], reason, p2(last), t.get("exit_reply"), order["qty"], p2(limit)))
            base = dict(index=None, fingerprint=fingerprint(order), symbol=t["symbol"], side="sell", qty=order["qty"],
                        kind="exit", ticket=ticket_line(order), mode="live", auto_exit=True,
                        approved_with=t["fingerprint"], reason=reason)
            send_live_exit(c, paths, st, now, order, base, order["angel"]["body"])
        else:
            pos = norm_positions(read(c.positions) or [])
            snapshot_positions(st, now, pos)        # india-risk reads it: this sell only closes what you hold
            net = sum(p["netQty"] for p in pos if p["securityId"] == t["token"] and p["productType"] == "INTRADAY")
            if 0 < net < order["qty"]:
                order = exit_order(t["symbol"], t["exchange"], t["token"], t.get("series"), t["tradingsymbol"], net,
                                   limit, tick, t["flat_by"], order["why"])
            core.write_exit(paths, now, "live", order, ANGEL)
            print("%s: %s reached (last %s). Ask the user: \"Sell %d %s at %s limit to close? Reply yes or no.\" "
                  "Then india-risk check.py, show, approve, send --live." % (t["symbol"], reason, p2(last),
                                                                             order["qty"], t["symbol"], p2(limit)))
    core.save_state(paths.broker, st, ANGEL)
    return 0


def cmd_watch(a):
    """check every --every seconds until nothing is open or --until (default flat-by + 10 minutes)."""
    paths = Paths(a)
    cfg = nse.read_json(paths.config, {}) or {}
    every = max(int(a.every or 30), 10)
    until = nse.hhmm(a.until) if a.until else (datetime.datetime.combine(datetime.date.today(), nse.flat_by(cfg))
                                               + datetime.timedelta(minutes=10)).time()
    passes = 0
    while True:
        cmd_check(a)
        passes += 1
        st = core.load_state(paths.broker)
        now = core.clock(a)
        mode = str(cfg.get("mode") or "paper").lower()
        if mode == "live" and not managed(st, now.date().isoformat()):
            print("Nothing open: watch stops.")
            return 0
        if getattr(a, "now", None) or now.time() >= until or (a.passes and passes >= a.passes):
            return 0
        time.sleep(every)


def cmd_flatten(a):
    paths = Paths(a)
    cfg = nse.read_json(paths.config, {}) or {}
    now = core.clock(a)
    mode = core.gate(cfg, a, ANGEL, writes=False)
    st = core.load_state(paths.broker)
    sym = nse.clean_symbol(a.symbol)
    exch = str(cfg.get("exchange") or "NSE").upper()
    sid, qty, source, c = None, None, "", None
    if mode == "paper":
        held = core.paper_position(paths, cfg, st, now, sym, ANGEL)
        if held is None:
            return 0
        qty, sid = held
        source = "the paper ledger"
    else:
        c = client()
        pos = norm_positions(read(c.positions) or [])
        snapshot_positions(st, now, pos)
        mine = [p for p in pos if p["symbol"] == sym and p["productType"] == "INTRADAY"]
        qty = sum(p["netQty"] for p in mine)
        sid = mine[0]["securityId"] if mine else None
        source = "Angel One getPosition"
    if a.qty is not None:
        qty = int(a.qty)
    if not qty or qty <= 0:
        core.save_state(paths.broker, st, ANGEL)
        raise BrokerError("nothing to flatten: no open %s position in %s" % (sym, source))
    scrip_fn = lambda: angel.scrip_master(offline=getattr(a, "scrip_offline", None), exchange=exch)  # noqa: E731
    tick, row = core.tick_for(paths, sym, scrip_fn)
    if not row:
        try:
            row = scrip_fn().get(sym) or {}
        except (nse.NSEError, ValueError, OSError):
            row = {}
    sid = sid or row.get("token")
    if not sid:
        raise BrokerError("no Angel One token for %s" % sym)
    tsym = row.get("trading_symbol") or sym + "-EQ"
    floor = floor_for(st, now.date().isoformat(), sym, paths, tick, cfg)
    ltp = (lambda: angel.ltps(c.quote([sid], "LTP"))[str(sid)]) if c is not None else None
    limit, how = core.exit_price(a, cfg, now, paths, sym, exch, tick, floor, ltp)
    plan = core.plan_for(paths, sym)
    flat = plan.get("flat_by") or nse.flat_by(cfg, now.date()).strftime("%H:%M")
    order = exit_order(sym, exch, sid, row.get("series"), tsym, qty, limit, tick, flat,
                       "flat by %s IST: closes the %s intraday position (qty from %s); limit = %s"
                       % (flat, sym, source, how))
    core.save_state(paths.broker, st, ANGEL)
    core.write_exit(paths, now, mode, order, ANGEL)
    print("Sell limit %s: %s. A marketable LIMIT: Angel One takes no market orders from algos." % (p2(limit), how))
    if mode == "live":
        print("At send it first cancels this stock's open stop orders (and checks they are cancelled), then sells the "
              "net quantity Angel One reports at this limit.")
    return 0


def cmd_read(a):
    """orders, trades, positions, funds, profile, ip."""
    paths = Paths(a)
    cfg = nse.read_json(paths.config, {}) or {}
    now = core.clock(a)
    mode = str(cfg.get("mode") or "paper").lower()
    if a.cmd == "ip":
        mine = public_ip()
        want = str(cfg.get("static_ip") or "").strip()
        print("This machine's public IP: %s" % (mine or "unknown (no answer from api.ipify.org)"))
        if not want:
            print("No \"static_ip\" in trader/config.json. Put the static IP you registered on your SmartAPI app there; "
                  "Angel One's SDK has no call to read it back, so this can only compare.")
            return 1
        if mine and mine == want:
            print("It matches your registered static IP %s: order calls can go from here." % want)
            return 0
        print("It does NOT match your registered static IP %s: Angel One will refuse orders from here (reads still "
              "work)." % want)
        return 1
    if a.cmd in ("orders", "trades", "positions", "funds") and mode == "paper" and not a.angel:
        return core.print_paper_ledger(paths, cfg, now, ANGEL)
    c = client()
    if a.cmd == "profile":
        data = read(c.profile) or {}
        code = str(data.get("clientcode") or "")
        print("Angel One session OK (logged in with TOTP): client %s, exchanges %s, products %s."
              % (("*" * max(len(code) - 4, 0) + code[-4:]) if code else "?", data.get("exchanges"), data.get("products")))
        print("(name, e-mail and phone are not shown)")
        return 0
    if a.cmd == "funds":
        data = read(c.funds) or {}
        keep = dict((k, data.get(k)) for k in ("net", "availablecash", "availableintradaypayin", "availablelimitmargin",
                                               "utiliseddebits", "utilisedpayout") if k in data)
        if _f(data.get("availablecash")) is not None:
            print("Available cash: %s (Angel One getRMS availablecash)" % nse.inr(_f(data["availablecash"])))
        print(clean(json.dumps(keep, indent=2)))
        _snapshot(paths, now, "funds", keep)
        return 0
    if a.cmd == "positions":
        pos = norm_positions(read(c.positions) or [])
        for p in pos:
            print("  %s %s net %d realised %s unrealised %s" % (p["symbol"], p["productType"], p["netQty"],
                                                              p["realizedProfit"], p["unrealizedProfit"]))
        if not pos:
            print("No positions today.")
        st = core.load_state(paths.broker)
        snapshot_positions(st, now, pos)
        core.save_state(paths.broker, st, ANGEL)
        return 0
    rows = read(c.orders if a.cmd == "orders" else c.trades) or []
    for r in rows:
        print("  %s %s %s %s %s qty %s filled %s price %s avg %s: %s" % (
            oid(r), r.get("variety") or "", r.get("transactiontype"), r.get("tradingsymbol"), r.get("ordertype") or "",
            r.get("quantity") or r.get("fillsize"), r.get("filledshares") or r.get("fillsize"), r.get("price") or "",
            r.get("averageprice") or r.get("fillprice") or "", ostatus(r) or r.get("fillid") or ""))
    if not rows:
        print("Nothing in today's %s." % ("order book" if a.cmd == "orders" else "trade book"))
    return 0


def _snapshot(paths, now, what, data):
    st = core.load_state(paths.broker)
    st["snapshots"].append({"at": nse.iso_ist(now), "date": now.date().isoformat(), "what": what, "data": data})
    st["snapshots"] = st["snapshots"][-200:]
    core.save_state(paths.broker, st, ANGEL)


def cmd_cancel(a):
    paths = Paths(a)
    cfg = nse.read_json(paths.config, {}) or {}
    now = core.clock(a)
    mode = core.gate(cfg, a, ANGEL, writes=True)
    st = core.load_state(paths.broker)
    if mode == "paper":
        return core.cancel_paper(paths, cfg, st, now, a.order_id, ANGEL)
    c = client()
    variety = a.variety
    if not variety:
        _, by = book(c)
        variety = (by.get(str(a.order_id)) or {}).get("variety")
        if not variety:
            raise BrokerError("order %s is not in today's order book; pass --variety NORMAL or STOPLOSS" % a.order_id)
    try:
        resp = cancel(c, variety, a.order_id)
    except angel.AngelError as e:
        raise BrokerError(angel_error(e))
    for t in st.get("managed", []):
        for s in t.get("stops", []):
            if str(s.get("orderid")) == str(a.order_id):
                s["status"] = "cancel sent"
    log(st, now, status="cancel", mode="live", broker_order_id=a.order_id,
        request={"route": "cancelOrder", "body": {"variety": variety, "orderid": str(a.order_id)}}, response=resp)
    core.save_state(paths.broker, st, ANGEL)
    print("LIVE: cancel %s (%s) sent to Angel One: %s. `angel_broker.py status` confirms it."
          % (a.order_id, variety, (resp or {}).get("orderid") or "accepted"))
    if variety.upper() == "STOPLOSS":
        print("That was a stop: the position it protected (if any) now has none.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Tickets, your yes, and Angel One SmartAPI (india-broker).")
    sub = ap.add_subparsers(dest="cmd")
    files, prices = core.files_args, core.price_opts

    p = sub.add_parser("show")
    files(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve")
    files(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--yes", required=True, help="the user's reply, word for word")
    p.add_argument("--exit-yes", help="the user's separate yes to exit at the target / flat-by without asking again")
    p = sub.add_parser("decline")
    files(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("send")
    files(p)
    prices(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--live", action="store_true")
    p = sub.add_parser("protect")
    files(p)
    p.add_argument("--symbol")
    p.add_argument("--retry", action="store_true", help="ignore a stop whose send got no answer (check orders first)")
    p.add_argument("--live", action="store_true")
    for name in ("check", "watch"):
        p = sub.add_parser(name)
        files(p)
        prices(p)
        p.add_argument("--symbol")
        p.add_argument("--live", action="store_true")
        if name == "watch":
            p.add_argument("--every", type=int, default=30, help="seconds between checks (at least 10)")
            p.add_argument("--until", help="stop watching at this IST time (default flat-by + 10 minutes)")
            p.add_argument("--passes", type=int, help="stop after this many checks")
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
    p.add_argument("--scrip-offline", help="Angel One's OpenAPIScripMaster.json from disk")
    p = sub.add_parser("status")
    files(p)
    p.add_argument("--angel", action="store_true", help="paper mode: read the real Angel One account anyway")
    p.add_argument("--order-id")
    for name in ("orders", "trades", "positions", "funds", "profile", "ip"):
        p = sub.add_parser(name)
        files(p)
        p.add_argument("--angel", action="store_true", help="paper mode: read the real Angel One account anyway")
    p = sub.add_parser("cancel")
    files(p)
    p.add_argument("--order-id", required=True)
    p.add_argument("--variety", choices=["NORMAL", "STOPLOSS", "AMO"])
    p.add_argument("--live", action="store_true")
    p = sub.add_parser("log")
    files(p)
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    fn = {"show": lambda x: core.cmd_show(x, ANGEL), "approve": cmd_approve,
          "decline": lambda x: core.cmd_decline(x, ANGEL), "send": lambda x: core.cmd_send(x, ANGEL, send_live),
          "protect": cmd_protect, "check": cmd_check, "watch": cmd_watch,
          "paper-check": lambda x: core.cmd_paper_check(x, ANGEL), "flatten": cmd_flatten, "status": cmd_status,
          "cancel": cmd_cancel, "log": lambda x: core.cmd_log(x, ANGEL)}.get(a.cmd, cmd_read)
    try:
        return fn(a)
    except (BrokerError, nse.NSEError) as e:
        print("angel_broker.py: %s" % clean(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
