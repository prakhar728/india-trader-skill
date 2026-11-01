#!/usr/bin/env python3
"""Position sizing for india-budget. Standard library only.

Sizes ONE intraday trade from trader/plan.json (written by india-exit) to your
capital and your max loss per trade, then writes one order to
trader/orders.json in the pack's contract shape, with the exact body of
Dhan's super order (POST /v2/super/orders: entry, target leg, stop-loss leg).
It never places anything: india-risk checks the order and india-broker asks
you yes or no.

    python3 size.py                                  # trader/plan.json + trader/config.json
    python3 size.py --write                          # write trader/orders.json
    python3 size.py --market --write                 # a MARKET entry instead of LIMIT at the plan's entry
    python3 size.py --limit 1174.50 --write          # a LIMIT entry at 1174.50
    python3 size.py --now "2026-10-29 09:41" --json  # pin the clock (IST)
    python3 size.py --scrip-offline api-scrip-master.csv   # Dhan's instrument file from disk

How many shares. Whole shares only. The size is the smallest of four limits:

    max loss       qty x (entry - stop) + round-trip charges at the stop
                   must fit max_loss_per_trade (charges only with include_charges)
    position cap   capital x position_cap_pct / entry
    max order      max_order_value / entry
    cash           (capital - cash buffer) / entry   (--cash caps it further)

Zero shares (the stop is too far away for even one share) is a plain refusal:
no order is written, the stop is never moved to make it fit.

The entry it sizes at: a LIMIT order's price (the plan's entry by default,
rounded UP to the stock's tick if it is off-tick). A MARKET order: the higher
of the plan's entry and a fresh price (under 15 minutes old) in
trader/prices.json, because a price that ran up since the plan means a bigger
loss at the same stop.

The stock's security_id, series and tick come from Dhan's instrument file
(nse.scrip_master, cached for 20 hours in trader/cache/). Only series EQ can
be traded intraday: BE and BZ are trade-to-trade and refused.

Angel One ("broker": "angel" in trader/config.json): the token, series and
tick come from Angel's scrip master (angel.scrip_master; --scrip-offline
reads OpenAPIScripMaster.json from disk) and the order carries an "angel"
block instead of "dhan": the placeOrder body of the NORMAL LIMIT INTRADAY
entry, and the STOPLOSS / STOPLOSS_LIMIT SELL stop that india-broker places
once the entry fills (trigger = the stop, limit = the stop minus
stop_limit_buffer_ticks ticks, default 5). Every value is a string, as in
Angel's SDK. No MARKET entries on Angel: its April 2026 notice prohibits
market orders for algorithmic trading.
"""

import argparse
import datetime
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "india-common"))
import nse  # noqa: E402
import angel  # noqa: E402

DEFAULTS = {
    "capital": 100000, "cash_buffer_pct": 10,
    "max_loss_per_trade": 500, "daily_loss_limit": 1500, "max_trades_per_day": 3,
    "min_reward_risk": 1.5, "position_cap_pct": 25, "max_order_value": 50000,
    "include_charges": True,
    "no_entries_before": "09:30", "no_entries_after": "14:30", "flat_by": "15:05",
    "product": "INTRADAY", "exchange": "NSE",
    "allow_leverage": False, "allow_short": False, "allow_fno": False,
    "blocked_symbols": [], "mode": "paper", "data_source": "auto",
    "extra_holidays": {}, "special_sessions": {},
    "broker": "dhan", "stop_limit_buffer_ticks": 5,
}
BROKER_NAMES = {"dhan": "Dhan", "angel": "Angel One"}
FRESH_MINUTES = 15
LIMIT_NAMES = {
    "max_loss_per_trade": "max loss per trade",
    "position_cap_pct": "position cap",
    "max_order_value": "max order value",
    "cash": "cash after the buffer",
}
SETUP_WORDS = {"or-mid": "opening-range breakout", "orb": "opening-range breakout"}
CORR_OK = re.compile(r"[^A-Za-z0-9 _-]")


class SizeError(Exception):
    pass


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def p2(x):
    return "%.2f" % float(x)


# ---------------------------------------------------------------- prices.json

def parse_iso_ist(text):
    """An ISO time -> naive IST. No offset means IST already; Z or an offset is converted."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = nse.ist_from_utc((dt - dt.utcoffset()).replace(tzinfo=None))
    return dt


def price_from(doc, sym):
    """(price, time IST or None) for sym from a prices.json doc. Accepts
    {"prices": {SYM: p}, "detail": {SYM: {"time"|"fetched"}}, "as_of"} and
    {SYM: {"price": p, "time": t}}."""
    if not isinstance(doc, dict):
        return None, None
    prices = doc.get("prices") if isinstance(doc.get("prices"), dict) else doc
    v = prices.get(sym)
    t = None
    if isinstance(v, dict):
        t = parse_iso_ist(v.get("time") or v.get("fetched"))
        v = v.get("price", v.get("last_price"))
    p = _f(v)
    if p is None or p <= 0:
        return None, None
    det = (doc.get("detail") or {}).get(sym) or {}
    t = t or parse_iso_ist(det.get("time")) or parse_iso_ist(det.get("fetched")) or parse_iso_ist(doc.get("as_of"))
    return p, t


def fresh_price(doc, sym, now, minutes=FRESH_MINUTES):
    """(price, time, fresh)."""
    p, t = price_from(doc, sym)
    if p is None:
        return None, None, False
    if t is None:
        return p, None, False
    age = (now - t).total_seconds()
    return p, t, -60 <= age <= minutes * 60


# ---------------------------------------------------------------- the size

def shares_for(rupees, per_share):
    """Whole shares that fit rupees at per_share, in exact paise."""
    r, p = int(round(float(rupees) * 100)), int(round(float(per_share) * 100))
    if r <= 0 or p <= 0:
        return 0
    return r // p


def loss_all_in(qty, entry, stop, exchange="NSE", include=True):
    """(price loss, charges, total) if qty fills at entry and exits at the stop."""
    loss = round(qty * int(round((entry - stop) * 100)) / 100.0, 2)
    ch = nse.round_trip_cost(qty, entry, stop, exchange) if (include and qty > 0) else 0.0
    return loss, ch, round(loss + ch, 2)


def shares_by_loss(max_loss, entry, stop, exchange="NSE", include=True):
    """The most whole shares whose loss at the stop, charges included, fits max_loss."""
    risk = entry - stop
    if risk <= 0 or max_loss <= 0:
        return 0
    q = shares_for(max_loss, round(risk, 2))
    while q > 0 and loss_all_in(q, entry, stop, exchange, include)[2] > max_loss + 1e-9:
        q -= 1
    return q


def size_trade(plan, cfg, scrip, today=None, limit=None, market=False, price=None, cash=None):
    """Pure function: one plan + rules + Dhan's instrument table -> the sizing (a dict).
    qty may be 0; then "refusal" says why and no order is made."""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    try:
        sym = nse.clean_symbol(plan.get("symbol") or "")
    except nse.NSEError:
        raise SizeError("the plan has no NSE symbol (run india-exit)")
    exchange = str(plan.get("exchange") or c.get("exchange") or "NSE").upper()
    if exchange not in ("NSE", "BSE"):
        raise SizeError("exchange must be NSE or BSE, got %r" % exchange)
    side = str(plan.get("side") or "long").lower()
    if side not in ("long", "buy"):
        raise SizeError("this pack sizes long (buy) intraday trades only; the plan says %r" % side)
    if today and plan.get("date") and str(plan["date"]) != today:
        raise SizeError("the plan is for %s and today is %s (IST): make a new plan with india-exit"
                        % (plan["date"], today))
    entry_plan, stop, target = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("entry", entry_plan), ("stop", stop), ("target", target)):
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s (run india-exit): a super order needs entry, "
                            "stop and target" % name)
    if sym in {str(b).upper() for b in (c.get("blocked_symbols") or [])}:
        raise SizeError("%s is on your blocked_symbols list" % sym)
    product = str(c.get("product") or "INTRADAY").upper()
    if product not in ("INTRADAY", "CNC"):
        raise SizeError("product %s uses leverage; this pack sizes INTRADAY (or CNC) only" % product)
    broker = str(c.get("broker") or "dhan").lower()
    if broker not in BROKER_NAMES:
        raise SizeError("config \"broker\" must be \"dhan\" or \"angel\", got %r" % broker)
    if broker == "angel" and market:
        raise SizeError("Angel One prohibits MARKET orders for algorithmic trading (its April 2026 notice): "
                        "size a LIMIT entry (drop --market, or pass --limit PRICE)")
    if broker == "angel" and exchange != "NSE":
        raise SizeError("Angel One orders in this pack are NSE cash only, not %s" % exchange)

    row = (scrip or {}).get(sym)
    if not row:
        raise SizeError("%s is not in %s's %s equity list (check the symbol, or refresh the instrument "
                        "file)" % (sym, BROKER_NAMES[broker], exchange))
    ok, why = nse.intraday_allowed(row.get("series"))
    if not ok and product == "INTRADAY":
        raise SizeError("%s: %s. No order." % (sym, why))
    tick = row.get("tick") or nse.band_tick(entry_plan)

    notes = []
    if market:
        ref, ref_from = round(entry_plan, 2), "plan"
        if price is not None and price > entry_plan + 1e-9:
            ref, ref_from = round(price, 2), "price"
        order_type = "MARKET"
    else:
        want = float(limit) if limit is not None else entry_plan
        ref = nse.round_up(want, tick)
        if abs(ref - want) > 1e-9:
            notes.append("the entry %s is off the %s tick (%s): the limit is %s"
                         % (p2(want), sym, tick, p2(ref)))
        ref_from = "limit" if limit is not None else "plan"
        order_type = "LIMIT"
    stop_t, target_t = nse.round_down(stop, tick), nse.round_down(target, tick)
    if abs(stop_t - stop) > 1e-9:
        notes.append("the stop %s is off the tick: rounded DOWN to %s (further away, so the risk shown is honest)"
                     % (p2(stop), p2(stop_t)))
    if abs(target_t - target) > 1e-9:
        notes.append("the target %s is off the tick: rounded DOWN to %s" % (p2(target), p2(target_t)))
    stop, target = stop_t, target_t
    if stop >= ref:
        raise SizeError("the stop %s is not below the entry %s; a long trade needs its stop underneath"
                        % (p2(stop), p2(ref)))
    if target <= ref:
        raise SizeError("the target %s is not above the entry %s: the move has already happened. "
                        "Skip it or make a new plan with india-exit" % (p2(target), p2(ref)))
    risk_ps = round(ref - stop, 2)
    reward_ps = round(target - ref, 2)
    include = bool(c.get("include_charges", True))

    capital = float(c.get("capital") or 0)
    if capital <= 0:
        raise SizeError("no capital: set \"capital\" in trader/config.json (or pass --capital)")
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    buffer = round(capital * buf_pct / 100.0, 2)
    available = capital - buffer
    cash_how = "capital %s minus the %s buffer" % (nse.inr(capital, 0), nse.inr(buffer, 0))
    if cash is not None and cash - buffer < available:
        available = cash - buffer
        cash_how = "cash %s minus the %s buffer" % (nse.inr(cash, 0), nse.inr(buffer, 0))
    available = max(round(available, 2), 0.0)

    limits = []
    max_loss = _f(c.get("max_loss_per_trade"))
    if max_loss:
        n = shares_by_loss(max_loss, ref, stop, exchange, include)
        limits.append({"key": "max_loss_per_trade", "shares": n,
                       "how": "%s / %s risk a share%s" % (nse.inr(max_loss, 0), p2(risk_ps),
                                                          " + charges at the stop" if include else "")})
    cap_pct = _f(c.get("position_cap_pct"))
    if cap_pct:
        cap = round(capital * cap_pct / 100.0, 2)
        limits.append({"key": "position_cap_pct", "shares": shares_for(cap, ref),
                       "how": "%s%% of %s = %s / %s" % (("%g" % cap_pct), nse.inr(capital, 0), nse.inr(cap, 0), p2(ref))})
    max_order = _f(c.get("max_order_value"))
    if max_order:
        limits.append({"key": "max_order_value", "shares": shares_for(max_order, ref),
                       "how": "%s / %s" % (nse.inr(max_order, 0), p2(ref))})
    limits.append({"key": "cash", "shares": shares_for(available, ref),
                   "how": "%s = %s / %s" % (cash_how, nse.inr(available, 0), p2(ref))})
    qty = max(0, min(l["shares"] for l in limits))
    decided = [l["key"] for l in limits if l["shares"] == qty][0]
    for l in limits:
        l["name"] = LIMIT_NAMES[l["key"]]
        l["binds"] = l["key"] == decided

    loss = loss_all_in(qty, ref, stop, exchange, include)[0]
    ch_stop_any = nse.round_trip_cost(qty, ref, stop, exchange) if qty else 0.0
    reward = round(qty * int(round(reward_ps * 100)) / 100.0, 2)
    rr = round(reward_ps / risk_ps, 2)
    refusal = None
    if qty == 0:
        if decided == "max_loss_per_trade":
            one = loss_all_in(1, ref, stop, exchange, include)
            refusal = ("one share risks %s at the stop (entry %s minus stop %s%s), more than your %s max loss "
                       "per trade. No order: skip this trade, or plan a closer stop with india-exit."
                       % (nse.inr(one[2]), p2(ref), p2(stop), (" plus %s charges" % nse.inr(one[1])) if include else "",
                          nse.inr(max_loss, 0)))
        else:
            refusal = ("one share of %s at %s is more than your %s (%s). No order."
                       % (sym, p2(ref), LIMIT_NAMES[decided], [l["how"] for l in limits if l["key"] == decided][0]))
    elif _f(c.get("min_reward_risk")) and rr < float(c["min_reward_risk"]) - 1e-9:
        refusal = ("reward:risk %.2f (target %s, entry %s, stop %s) is under your minimum %g, so india-risk "
                   "would block it. No order." % (rr, p2(target), p2(ref), p2(stop), float(c["min_reward_risk"])))
    buf = int(c.get("stop_limit_buffer_ticks") if c.get("stop_limit_buffer_ticks") is not None else 5)
    stop_limit = max(nse.round_down(stop - buf * tick, tick), tick)
    return {
        "broker": broker, "trading_symbol": row.get("trading_symbol") or (sym + "-EQ"),
        "stop_limit": stop_limit, "stop_limit_buffer_ticks": buf,
        "symbol": sym, "exchange": exchange, "series": row.get("series"), "security_id": str(row["security_id"]),
        "tick": tick, "date": plan.get("date"), "setup": plan.get("stop_rule") or plan.get("setup") or "",
        "entry_plan": entry_plan, "entry": ref, "entry_from": ref_from, "stop": stop, "target": target,
        "risk_per_share": risk_ps, "reward_risk": rr, "order_type": order_type, "product": product,
        "capital": capital, "cash_buffer": buffer, "cash_available": available,
        "limits": limits, "qty": qty, "decided_by": decided, "value": round(qty * ref, 2),
        "max_loss": loss, "charges_at_stop": round(ch_stop_any, 2), "max_loss_all_in": round(loss + ch_stop_any, 2),
        "include_charges": include, "reward_at_target": reward,
        "flat_by": nse.flat_by(c).strftime("%H:%M") if not plan.get("flat_by")
        else min(nse.hhmm(plan["flat_by"]), nse.flat_by(c)).strftime("%H:%M"),
        "max_loss_per_trade": max_loss, "notes": notes, "refusal": refusal,
    }


def correlation_id(sym, date, n):
    d = str(date or "").replace("-", "")
    tail = "-%s-%d" % (d, n)
    head = "india-" + CORR_OK.sub("", sym)
    return (head[: 30 - len(tail)] + tail)[:30]


def angel_block(s):
    """Angel One: the entry (placeOrder NORMAL LIMIT INTRADAY) and the exchange-held stop india-broker places
    after the entry fills (STOPLOSS / STOPLOSS_LIMIT SELL). Strings throughout, as in Angel's SDK README."""
    entry = angel.order_body(s["trading_symbol"], s["security_id"], "BUY", s["qty"], s["entry"],
                             exchange=s["exchange"], product=s["product"])
    stop = angel.order_body(s["trading_symbol"], s["security_id"], "SELL", s["qty"], s["stop_limit"],
                            exchange=s["exchange"], product=s["product"], variety="STOPLOSS",
                            ordertype="STOPLOSS_LIMIT", trigger=s["stop"])
    return {"route": "placeOrder", "body": entry, "stop_order": stop}


def make_order(s, n=1):
    """The one order in the contract shape, with Dhan's super-order body (or, broker angel, the Angel One
    block). No dhanClientId and no Angel key: the broker adds those from the environment at send time."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    body = {
        "transactionType": "BUY", "exchangeSegment": s["exchange"] + "_EQ", "productType": s["product"],
        "orderType": s["order_type"], "securityId": s["security_id"], "quantity": int(s["qty"]),
        "price": s["entry"] if s["order_type"] == "LIMIT" else 0,
        "targetPrice": s["target"], "stopLossPrice": s["stop"], "trailingJump": 0,
        "correlationId": correlation_id(s["symbol"], s["date"], n),
    }
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "day trade")
    why = ("%s on %s: %d shares, decided by your %s (max loss %s at the stop incl. %s charges)"
           % (setup, s["symbol"], s["qty"], LIMIT_NAMES[s["decided_by"]], nse.inr(s["max_loss_all_in"]),
              nse.inr(s["charges_at_stop"])))
    order = {
        "symbol": s["symbol"], "exchange": s["exchange"], "series": s["series"], "security_id": s["security_id"],
        "side": "buy", "qty": int(s["qty"]), "entry": s["entry"], "stop": s["stop"], "target": s["target"],
        "tick": s["tick"], "product": s["product"], "order_type": s["order_type"], "flat_by": s["flat_by"],
        "value": s["value"], "max_loss": s["max_loss"], "charges_at_stop": s["charges_at_stop"],
        "max_loss_all_in": s["max_loss_all_in"], "reward_at_target": s["reward_at_target"],
        "reward_risk": s["reward_risk"], "decided_by": s["decided_by"], "kind": "entry", "why": why,
    }
    if s.get("broker") == "angel":
        order["angel"] = angel_block(s)
    else:
        order["dhan"] = {"endpoint": "POST /v2/super/orders", "body": body}
    return order


def report(s, order, notes=()):
    angel_ = s.get("broker") == "angel"
    lines = ["%s buy (%s), entry %s, stop %s, target %s, tick %s, series %s, %s %s"
             % (s["symbol"], s["exchange"], p2(s["entry_plan"]), p2(s["stop"]), p2(s["target"]), s["tick"],
                s["series"], "Angel One token" if angel_ else "Dhan security_id", s["security_id"])]
    for n in list(notes) + s["notes"]:
        lines.append("Note: " + n)
    src = {"plan": "the plan's entry", "price": "the latest price, above the plan's entry",
           "limit": "your limit price"}[s["entry_from"]]
    lines.append("Sized at %s (%s, %s order). Risk %s a share. Reward:risk %.2f."
                 % (p2(s["entry"]), src, s["order_type"], p2(s["risk_per_share"]), s["reward_risk"]))
    lines.append("")
    lines.append("Limits (whole shares; the smallest one decides):")
    for l in s["limits"]:
        lines.append("  %-22s %-58s %5d%s" % (l["name"], l["how"], l["shares"], "  <- decides" if l["binds"] else ""))
    lines.append("")
    if order is None:
        lines.append("No order: " + s["refusal"])
        lines.append("Not financial advice. Nothing is placed without your \"yes\".")
        return "\n".join(lines)
    lines.append("Size: %d shares of %s = %s at %s." % (s["qty"], s["symbol"], nse.inr(s["value"]), p2(s["entry"])))
    lines.append("Max loss at the stop: %d x %s = %s + %s round-trip charges = %s."
                 % (s["qty"], p2(s["risk_per_share"]), nse.inr(s["max_loss"]), nse.inr(s["charges_at_stop"]),
                    nse.inr(s["max_loss_all_in"])))
    lines.append("At the target: %s before charges (a target is a plan, not a forecast)." % nse.inr(s["reward_at_target"]))
    lines.append("A stop is an order, not a promise: in a fast market it can fill below %s." % p2(s["stop"]))
    lines.append("")
    if angel_:
        blk = order["angel"]
        lines.append("Proposed Angel One orders (placeOrder; nothing has been placed):")
        lines.append("  1. the entry, sent on your yes:")
        lines.append("     " + json.dumps(blk["body"]))
        lines.append("  2. after the entry fills (india-broker `protect`), the stop held at the exchange:")
        lines.append("     " + json.dumps(blk["stop_order"]))
        lines.append("A stop-limit: it triggers at %s and sells no lower than %s (%d ticks under). In a gap below "
                     "%s it may not fill at all." % (p2(s["stop"]), p2(s["stop_limit"]), s["stop_limit_buffer_ticks"],
                                                     p2(s["stop_limit"])))
        lines.append("The target %s is not a resting order: india-broker watches it and sells with a marketable "
                     "LIMIT (no market orders on Angel One)." % p2(s["target"]))
        lines.append("Flat by %s IST (Angel One's own intraday auto square-off time is not verified here; the pack "
                     "keeps %s)." % (s["flat_by"], s["flat_by"]))
    else:
        lines.append("Proposed Dhan super order (POST /v2/super/orders; nothing has been placed):")
        lines.append("  " + json.dumps(order["dhan"]["body"]))
        lines.append("Flat by %s IST (Dhan squares off intraday at 15:10)." % s["flat_by"])
    lines.append("")
    lines.append("Next: india-risk checks it, then india-broker shows the ticket and asks you yes or no.")
    lines.append("Not financial advice. Nothing is placed without your \"yes\".")
    return "\n".join(lines)


def sends_today(broker_doc, sym, today):
    n = 0
    for r in (broker_doc or {}).get("orders", []):
        if r.get("date") == today and str(r.get("symbol", "")).upper() == sym and r.get("status") == "sent":
            n += 1
    return n


def main(argv=None):
    td = nse.trader_dir()
    ap = argparse.ArgumentParser(description="Size an intraday trade to your capital and max loss (india-budget).")
    ap.add_argument("--plan", default=os.path.join(td, "plan.json"))
    ap.add_argument("--config", default=os.path.join(td, "config.json"))
    ap.add_argument("--prices", default=os.path.join(td, "prices.json"))
    ap.add_argument("--broker", default=os.path.join(td, "broker.json"), help="for the correlationId counter")
    ap.add_argument("--capital", type=float, help="override capital in the config")
    ap.add_argument("--cash", type=float, help="cash you actually have; caps the size")
    ap.add_argument("--limit", type=float, metavar="PRICE", help="LIMIT entry at PRICE (default: the plan's entry)")
    ap.add_argument("--market", action="store_true", help="a MARKET entry instead of LIMIT")
    ap.add_argument("--scrip-offline", metavar="PATH",
                    help="the broker's instrument file from disk (Dhan's api-scrip-master.csv, or Angel One's "
                         "OpenAPIScripMaster.json when config broker is angel)")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-29 09:41\"")
    ap.add_argument("--write", nargs="?", const=os.path.join(td, "orders.json"), metavar="PATH")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.market and a.limit is not None:
            raise SizeError("--market and --limit together make no sense: pick one")
        now = nse.parse_now(a.now)
        today = now.date().isoformat()
        cfg = dict(DEFAULTS)
        cfg.update(nse.read_json(a.config, {}) or {})
        if a.capital:
            cfg["capital"] = a.capital
        plan = nse.read_json(a.plan)
        if plan is None:
            raise SizeError("no plan at %s (run india-exit first)" % a.plan)
        exchange = str(plan.get("exchange") or cfg.get("exchange") or "NSE").upper()
        if str(cfg.get("broker") or "dhan").lower() == "angel":
            scrip = angel.scrip_master(offline=a.scrip_offline, exchange=exchange)
        else:
            scrip = nse.scrip_master(offline=a.scrip_offline, exchange=exchange)
        notes, price = [], None
        sym = nse.clean_symbol(plan.get("symbol") or "X")
        pdoc = nse.read_json(a.prices) if a.prices and os.path.exists(a.prices) else None
        if pdoc and a.market:
            p, t, fresh = fresh_price(pdoc, sym, now)
            if p is not None and fresh:
                price = p
                if p > (_f(plan.get("entry")) or 0):
                    notes.append("%s last %s (%s IST) is above the plan's entry: sized at %s"
                                 % (sym, p2(p), t.strftime("%H:%M"), p2(p)))
            elif p is not None:
                notes.append("%s in prices.json is older than %d minutes: not used" % (sym, FRESH_MINUTES))
        s = size_trade(plan, cfg, scrip, today, a.limit, a.market, price, a.cash)
        n = sends_today(nse.read_json(a.broker, {}) if os.path.exists(a.broker) else {}, s["symbol"], today) + 1
        order = make_order(s, n)
        if order and a.write:
            nse.write_json(a.write, {
                "made": nse.iso_ist(now), "made_by": "india-budget", "status": "proposed",
                "mode": cfg.get("mode", "paper"),
                "note": "Proposal only. Nothing is placed until india-risk passes it and you say yes.",
                "source": "plan.json %s %s" % (s["symbol"], s["date"] or today), "orders": [order]})
    except (SizeError, nse.NSEError) as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps({"sizing": s, "orders": [order] if order else [], "notes": notes}, indent=2))
    else:
        print(report(s, order, notes))
        if order and a.write:
            print("Wrote 1 proposed order to %s" % a.write)
    return 0 if order else 1


if __name__ == "__main__":
    sys.exit(main())
