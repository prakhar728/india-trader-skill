#!/usr/bin/env python3
"""Pre-trade risk check for india-risk. Standard library only.

Reads the proposed orders (trader/orders.json) and checks each one against
your rules (trader/config.json), the broker's instrument file (security_id /
token, series, tick: Dhan's, or Angel One's scrip master when config "broker"
is "angel"), fresh prices (trader/prices.json), the day's circuit band (Dhan's
/marketfeed/quote, only when your Dhan keys and Data plan are there; skipped
for Angel One), and what was sent today (trader/broker.json, trader/paper.json). Each order gets PASS
or BLOCK with the reasons. The verdicts go to trader/risk.json with a
fingerprint of each exact order, so india-broker only shows an order as READY
if THIS order passed today.

    python3 check.py                                  # the trader/ files, writes trader/risk.json
    python3 check.py --now "2026-10-29 09:41"         # pin the clock (IST)
    python3 check.py --json --no-write
    python3 check.py --scrip-offline api-scrip-master.csv --no-quote

Entries and exits. An ENTRY opens a position (a buy, or a sell that would go
short). An EXIT only sells what you hold. Getting out is never blocked: an
exit is checked for format, the Dhan body and short selling only; every other
rule becomes a warning for it.

What blocks an entry: format; product (INTRADAY, CNC only if config product
is CNC; MARGIN/MTF/CO/BO need allow_leverage); F&O (allow_fno); shorts
(allow_short); the symbol not in Dhan's NSE/BSE equity list or a different
security_id; series not EQ (BE/BZ are trade-to-trade); a price off the
stock's tick; entry or target outside today's circuit band (when known);
blocked_symbols; max_order_value; position_cap_pct; cash after the buffer;
no stop or a stop above the entry; the loss at the stop (plus round-trip
charges when include_charges) over max_loss_per_trade; reward:risk under
min_reward_risk; today's risk used plus this one over daily_loss_limit, or
the day's realised loss already at the limit; max_trades_per_day; outside
no_entries_before / no_entries_after (IST), at or after flat_by, or the
market closed (NSE calendar, special sessions from config); a duplicate
entry; a Dhan super-order body that does not say the same as the order.

Angel One (config "broker": "angel"): the order's "angel" block must say the
same as the order (placeOrder NORMAL LIMIT INTRADAY DAY, tradingsymbol
SYMBOL-EQ, symboltoken = the scrip master's token, string values), an entry
must carry its STOPLOSS / STOPLOSS_LIMIT SELL stop order (trigger = the stop,
limit under it, on the tick), and nothing may be MARKET, STOPLOSS_MARKET, IOC
or ROBO: Angel's April 2026 notice prohibits market and IOC orders for
algorithmic trading.

Exit code 0 when every order passes, 1 when any is blocked.
"""

import argparse
import datetime
import hashlib
import json
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
    "blocked_symbols": [], "mode": "paper", "broker": "dhan",
}
# The fields that make an order THIS order. Same list in india-broker/ticket_core.py.
FP_FIELDS = ("symbol", "exchange", "series", "security_id", "side", "qty", "entry", "stop", "target",
             "product", "order_type", "flat_by", "kind", "dhan", "angel", "limit")
BROKER_NAMES = {"dhan": "Dhan", "angel": "Angel One"}
ID_LABELS = {"dhan": "security_id", "angel": "token"}
FRESH_MINUTES = 15
PAISA = 0.005
LEVERAGE_PRODUCTS = ("MARGIN", "MTF", "CO", "BO")
FNO_SEG = re.compile(r"_(FNO|COMM|CURRENCY)$")
CORR_BAD = re.compile(r"[^A-Za-z0-9 _-]")


class RiskError(Exception):
    pass


def fingerprint(order):
    """The exact order: same function in india-broker/dhan.py."""
    core = dict((k, order[k]) for k in FP_FIELDS if order.get(k) is not None)
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def p2(x):
    return "%.2f" % float(x)


def parse_iso_ist(text):
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = nse.ist_from_utc((dt - dt.utcoffset()).replace(tzinfo=None))
    return dt


def load_prices(doc):
    """prices.json -> ({SYM: price}, {SYM: IST time or None})."""
    if not isinstance(doc, dict):
        return {}, {}
    prices = doc.get("prices") if isinstance(doc.get("prices"), dict) else doc
    det = doc.get("detail") or {}
    as_of = parse_iso_ist(doc.get("as_of"))
    out, times = {}, {}
    for s, v in prices.items():
        t = None
        if isinstance(v, dict):
            t = parse_iso_ist(v.get("time") or v.get("fetched"))
            v = v.get("price", v.get("last_price"))
        if _f(v) is None:
            continue
        s = str(s).upper()
        d = det.get(s) or {}
        out[s] = float(v)
        times[s] = t or parse_iso_ist(d.get("time")) or parse_iso_ist(d.get("fetched")) or as_of
    return out, times


# ---------------------------------------------------------------- today's book

def today_book(broker, paper, today, mode):
    """What was sent today in this mode: entries (with their max loss), realised P&L, open longs."""
    entries, sent_keys = [], set()
    realised = 0.0
    held = {}
    for r in (broker or {}).get("orders", []):
        if r.get("date") != today or (r.get("mode") or "paper") != mode:
            continue
        if r.get("status") == "sent" and r.get("kind", "entry") == "entry":
            entries.append(r)
            sent_keys.add(str(r.get("symbol", "")).upper())
    if mode == "paper":
        by_fp = {}
        for o in (paper or {}).get("orders", []):
            if o.get("date") != today:
                continue
            if o.get("pnl") is not None:
                realised += float(o["pnl"])
                by_fp[o.get("fingerprint")] = float(o["pnl"])
            if o.get("status") == "TRADED" and o.get("side", "buy") == "buy":
                s = str(o.get("symbol", "")).upper()
                held[s] = held.get(s, 0) + int(o.get("qty") or 0)
        for e in entries:
            if e.get("fingerprint") in by_fp:
                e["_pnl"] = by_fp[e["fingerprint"]]
    else:
        snap = None
        for r in (broker or {}).get("snapshots", []):
            if r.get("date") == today and r.get("what") == "positions":
                snap = r
        if snap:
            for p in snap.get("positions", []):
                q = int(_f(p.get("netQty")) or 0)
                s = str(p.get("symbol") or p.get("tradingSymbol") or "").upper()
                if q > 0 and s:
                    held[s] = held.get(s, 0) + q
                realised += (_f(p.get("realizedProfit")) or 0.0)
    used = 0.0
    for e in entries:
        ml = _f(e.get("max_loss_all_in")) or 0.0
        if e.get("_pnl") is not None and -e["_pnl"] > ml:
            ml = -e["_pnl"]                # a stop that filled worse counts at what it really lost
        used += ml
    return {"entries": entries, "sent_symbols": sent_keys, "risk_used": round(used, 2),
            "realised": round(realised, 2), "held": held}


# ---------------------------------------------------------------- the window

def window_reasons(now, c):
    st = nse.market_status(now, c)
    reasons, warns = [], []
    if st.get("warning"):
        warns.append(st["warning"])
    if st["state"] != "open":
        why = st.get("why") or ("pre-open" if st["state"] == "pre-open" else "outside 09:15-15:30")
        reasons.append("window: the market is closed now (%s IST, %s)%s"
                       % (now.strftime("%a %Y-%m-%d %H:%M"), why,
                          ("; next open " + st["next_open"]) if st.get("next_open") else ""))
        return reasons, warns
    t = now.time().replace(second=0, microsecond=0)
    d = now.date()
    special = nse.special_session(d, c)
    before = c.get("no_entries_before")
    after = c.get("no_entries_after")
    flat = nse.flat_by(c, d)
    if special:
        warns.append("special session today %s-%s IST" % (special[0].strftime("%H:%M"), special[1].strftime("%H:%M")))
    else:
        if before and t < nse.hhmm(before):
            reasons.append("window: no new entries before %s IST (it is %s)" % (before, t.strftime("%H:%M")))
        if after and t > nse.hhmm(after):
            reasons.append("window: no new entries after %s IST (it is %s)" % (after, t.strftime("%H:%M")))
    if t >= flat:
        reasons.append("window: it is %s, at or after your flat-by %s IST: no new entries"
                       % (t.strftime("%H:%M"), flat.strftime("%H:%M")))
    return reasons, warns


# ---------------------------------------------------------------- Dhan body

def body_mismatches(o):
    """The Dhan request body must say the same as the order fields."""
    out = []
    dh = o.get("dhan")
    if not isinstance(dh, dict) or not isinstance(dh.get("body"), dict):
        return ["dhan: the order has no Dhan request body (run india-budget again)"]
    b = dh["body"]
    kind = o.get("kind") or ("entry" if o.get("side") == "buy" else "exit")
    if "dhanClientId" in b:
        out.append("dhan: the body carries a dhanClientId; it must never be on disk (the broker adds it "
                   "from your shell at send time)")
    want = {"transactionType": str(o.get("side", "")).upper(),
            "exchangeSegment": "%s_EQ" % str(o.get("exchange") or "NSE").upper(),
            "productType": str(o.get("product") or "").upper(),
            "orderType": str(o.get("order_type") or "").upper(),
            "securityId": str(o.get("security_id") or ""), "quantity": o.get("qty")}
    if kind == "entry":
        if dh.get("endpoint") != "POST /v2/super/orders":
            out.append("dhan: an entry goes to POST /v2/super/orders, not %r" % dh.get("endpoint"))
        want.update(targetPrice=o.get("target"), stopLossPrice=o.get("stop"))
        if want["orderType"] == "LIMIT":
            want["price"] = o.get("entry")
        if _f(b.get("trailingJump")):
            out.append("dhan: trailingJump %s moves the stop; this pack sends 0" % b.get("trailingJump"))
    else:
        if dh.get("endpoint") != "POST /v2/orders":
            out.append("dhan: an exit goes to POST /v2/orders, not %r" % dh.get("endpoint"))
        want["validity"] = "DAY"
        if want["orderType"] == "LIMIT":
            want["price"] = o.get("limit")
    for k, v in want.items():
        got = b.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if _f(got) is None or abs(_f(got) - float(v)) > PAISA:
                out.append("dhan: body %s is %r but the order says %r" % (k, got, v))
        elif str(got) != str(v):
            out.append("dhan: body %s is %r but the order says %r" % (k, got, v))
    cid = b.get("correlationId")
    if cid is not None and (len(str(cid)) > 30 or CORR_BAD.search(str(cid))):
        out.append("dhan: correlationId %r must be at most 30 letters, digits, space, _ or -" % cid)
    return out


# ---------------------------------------------------------------- Angel One body

ANGEL_STR = ("variety", "tradingsymbol", "symboltoken", "transactiontype", "exchange", "ordertype",
             "producttype", "duration", "price", "squareoff", "stoploss", "quantity", "triggerprice")


def _angel_body(b, want, what):
    out = []
    for k, v in b.items():
        if k in ANGEL_STR and not isinstance(v, str):
            out.append("angel: %s %s must be a string (the SDK sends strings), got %r" % (what, k, v))
    if str(b.get("ordertype", "")).upper() in angel.NO_ALGO_TYPES:
        out.append("angel: %s is %s: market orders are prohibited for algorithmic trading on Angel One (April "
                   "2026 notice); use a LIMIT price" % (what, b.get("ordertype")))
    if str(b.get("duration", "")).upper() in angel.NO_ALGO_DURATIONS:
        out.append("angel: %s duration IOC is prohibited for algorithmic trading on Angel One (April 2026 notice)"
                   % what)
    if str(b.get("variety", "")).upper() == "ROBO":
        out.append("angel: %s is ROBO (bracket): not confirmed for NSE intraday via the API, not used" % what)
    for k in ("squareoff", "stoploss"):
        if _f(b.get(k)):
            out.append("angel: %s %s is %r; this pack sends 0 (no ROBO legs)" % (what, k, b.get(k)))
    for k, v in want.items():
        got = b.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if _f(got) is None or abs(_f(got) - float(v)) > PAISA:
                out.append("angel: %s %s is %r but the order says %r" % (what, k, got, v))
        elif str(got) != str(v):
            out.append("angel: %s %s is %r but the order says %r" % (what, k, got, v))
    return out


def angel_mismatches(o, tick=None):
    """The Angel One placeOrder body (and an entry's stop order) must say the same as the order."""
    blk = o.get("angel")
    if not isinstance(blk, dict) or not isinstance(blk.get("body"), dict):
        return ["angel: the order has no Angel One request body (config broker is angel: run india-budget again)"]
    out = []
    if o.get("dhan"):
        out.append("angel: the order also carries a Dhan body; one broker per order")
    if blk.get("route") != "placeOrder":
        out.append("angel: the request goes to placeOrder, not %r" % blk.get("route"))
    kind = o.get("kind") or ("entry" if o.get("side") == "buy" else "exit")
    sym = str(o.get("symbol") or "").upper()
    exch = str(o.get("exchange") or "NSE").upper()
    common = {"tradingsymbol": sym + "-EQ", "symboltoken": str(o.get("security_id") or ""), "exchange": exch,
              "producttype": str(o.get("product") or "").upper(), "duration": "DAY"}
    want = dict(common, variety="NORMAL", transactiontype=str(o.get("side", "")).upper(),
                ordertype=str(o.get("order_type") or "").upper(), quantity=str(o.get("qty")))
    if want["ordertype"] == "LIMIT":
        want["price"] = o.get("entry") if kind == "entry" else o.get("limit")
    out.extend(_angel_body(blk["body"], want, "body"))
    if kind == "entry":
        so = blk.get("stop_order")
        if not isinstance(so, dict):
            out.append("angel: an entry needs its exchange-held stop (stop_order: STOPLOSS / STOPLOSS_LIMIT SELL); "
                       "run india-budget again")
        else:
            out.extend(_angel_body(so, dict(common, variety="STOPLOSS", ordertype="STOPLOSS_LIMIT",
                                            transactiontype="SELL", quantity=str(o.get("qty")),
                                            triggerprice=o.get("stop")), "stop_order"))
            lim, trig = _f(so.get("price")), _f(so.get("triggerprice"))
            if lim is None or lim <= 0 or (trig is not None and lim > trig + PAISA):
                out.append("angel: stop_order price %r must be above 0 and at or under the trigger %r (a sell "
                           "stop-limit)" % (so.get("price"), so.get("triggerprice")))
            elif tick and not nse.on_tick(lim, tick):
                out.append("tick: the stop-limit price %s is not on %s's %s tick" % (p2(lim), sym, tick))
    return out


# ---------------------------------------------------------------- the check

def check(orders, cfg, scrip, now, prices=None, price_times=None, broker=None, paper=None, circuits=None):
    """Pure function. now = naive IST. circuits = {security_id: (lower, upper)} when known.
    scrip = the broker's instrument table {SYMBOL: {security_id, series, tick}} (Dhan's, or Angel One's
    when cfg broker is angel: its token is the security_id)."""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    bkey = str(c.get("broker") or "dhan").lower()
    bname, id_label = BROKER_NAMES.get(bkey, bkey), ID_LABELS.get(bkey, "id")
    today = now.date().isoformat()
    mode = str(c.get("mode") or "paper").lower()
    prices = prices or {}
    circuits = circuits or {}
    book = today_book(broker, paper, today, mode)
    top = []
    if bkey not in BROKER_NAMES:
        top.append("config broker %r is not dhan or angel" % bkey)
    if mode == "live":
        top.append("LIVE mode: these orders use real money")
    win, win_warn = window_reasons(now, c)
    top.extend(win_warn)
    blocked = {str(b).upper() for b in (c.get("blocked_symbols") or [])}
    capital = float(c.get("capital") or 0)
    buffer = capital * float(c.get("cash_buffer_pct") or 0) / 100.0
    include = bool(c.get("include_charges", True))
    batch_entries, batch_risk, batch_value = 0, 0.0, 0.0
    seen = set()
    results = []

    for i, o in enumerate(orders, 1):
        reasons, warns, info, later = [], [], [], []
        sym = str(o.get("symbol") or "").strip().upper()
        side = str(o.get("side") or "").lower()
        exch = str(o.get("exchange") or "NSE").upper()
        product = str(o.get("product") or "").upper()
        otype = str(o.get("order_type") or "").upper()
        qty = o.get("qty")
        entry, stop, target = _f(o.get("entry")), _f(o.get("stop")), _f(o.get("target"))
        held = book["held"].get(sym, 0)
        kind = o.get("kind")
        if side == "sell" and isinstance(qty, int) and 0 < qty <= held:
            kind = "exit"
        elif side == "sell":
            kind = "entry"                          # a sell of shares you do not hold = a short
        else:
            kind = "entry"
        seg = str(((o.get("dhan") or {}).get("body") or {}).get("exchangeSegment") or "")
        aex = str(((o.get("angel") or {}).get("body") or {}).get("exchange") or "").upper()
        if aex in ("NFO", "BFO", "MCX", "CDS", "NCDEX"):
            seg = "%s_FNO" % aex

        # format
        if not sym:
            reasons.append("format: no symbol")
        if side not in ("buy", "sell"):
            reasons.append("format: side must be buy or sell, got %r" % o.get("side"))
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            reasons.append("format: qty must be a whole number of shares above 0, got %r" % (qty,))
        if exch not in ("NSE", "BSE"):
            reasons.append("format: exchange must be NSE or BSE, got %r" % exch)
        if otype not in ("LIMIT", "MARKET"):
            reasons.append("format: order_type must be LIMIT or MARKET, got %r" % otype)
        if otype == "LIMIT" and kind == "entry" and not entry:
            reasons.append("format: a LIMIT order needs an entry price")
        if otype == "LIMIT" and kind == "exit" and o.get("kind") == "exit" and not _f(o.get("limit")):
            reasons.append("format: a LIMIT exit needs its limit price")
        if bkey == "angel":
            if otype == "MARKET":
                reasons.append("angel: MARKET orders are prohibited for algorithmic trading on Angel One (April 2026 "
                               "notice): use a LIMIT price")
            if exch != "NSE":
                reasons.append("angel: this pack sends Angel One orders for NSE cash only, not %s" % exch)
            row_tick = ((scrip or {}).get(sym) or {}).get("tick") or _f(o.get("tick"))
            reasons.extend(angel_mismatches(o, row_tick))
        else:
            reasons.extend(body_mismatches(o))

        # asset: F&O, product, short
        if FNO_SEG.search(seg) or re.search(r"\d{2}[A-Z]{3}\d*(CE|PE|FUT)$", sym):
            if not c.get("allow_fno"):
                reasons.append("asset: %s is a derivative (F&O); allow_fno is false" % sym)
            else:
                reasons.append("asset: F&O orders are not supported by this pack's sizing or checks")
        if product in LEVERAGE_PRODUCTS and not c.get("allow_leverage"):
            reasons.append("product: %s is leveraged; allow_leverage is false (use INTRADAY)" % product)
        elif product == "CNC" and str(c.get("product", "INTRADAY")).upper() != "CNC":
            later.append("product: CNC (delivery) but your config says product INTRADAY")
        elif product not in ("INTRADAY", "CNC") + LEVERAGE_PRODUCTS:
            reasons.append("product: unknown product %r" % product)
        if side == "sell" and kind == "entry" and not c.get("allow_short"):
            reasons.append("short: selling %s %s you do not hold (%d held); short selling is off"
                           % (qty, sym, held))

        # Dhan's instrument file: listed, security_id, series, tick
        row = (scrip or {}).get(sym)
        tick = _f(o.get("tick")) or None
        if sym and row is None:
            later.append("symbol: %s is not in %s's %s equity list" % (sym, bname, exch))
        elif row:
            if str(row.get("security_id")) != str(o.get("security_id")):
                reasons.append("symbol: %s has %s %s %s, the order says %s"
                               % (sym, bname, id_label, row.get("security_id"), o.get("security_id")))
            ok, why = nse.intraday_allowed(row.get("series"))
            if not ok and product == "INTRADAY":
                later.append("series: %s %s" % (sym, why))
            if o.get("series") and o.get("series") != row.get("series"):
                later.append("series: the order says %s, %s's list says %s" % (o.get("series"), bname,
                                                                             row.get("series")))
            tick = row.get("tick") or tick
        if tick is None and entry:
            tick = nse.band_tick(entry)
        if tick:
            for name, v in (("entry", entry if otype == "LIMIT" else None), ("stop", stop), ("target", target),
                            ("limit", _f(o.get("limit")) if otype == "LIMIT" else None)):
                if v and not nse.on_tick(v, tick):
                    later.append("tick: the %s %s is not on %s's %s tick (%s rejects it)" % (name, p2(v), sym, tick,
                                                                                          bname))

        # the entry it measures risk from
        ref, ref_from = entry, "limit" if otype == "LIMIT" else "plan"
        live = prices.get(sym)
        lt = (price_times or {}).get(sym)
        fresh = live is not None and lt is not None and -60 <= (now - lt).total_seconds() <= FRESH_MINUTES * 60
        if otype == "MARKET":
            if fresh and (ref is None or live > ref + PAISA):
                ref, ref_from = live, "price"
                info.append("%s last %s is above the plan's entry: risk measured from %s" % (sym, p2(live), p2(live)))
            elif live is not None and not fresh:
                warns.append("the %s price in prices.json is older than %d minutes: not used" % (sym, FRESH_MINUTES))
        value = qty * ref if (isinstance(qty, int) and ref) else None

        # circuit band
        band = circuits.get(str(o.get("security_id")))
        if band and band[0] and band[1]:
            lo, hi = band
            info.append("today's circuit band %s - %s" % (p2(lo), p2(hi)))
            if ref and not lo - PAISA <= ref <= hi + PAISA:
                later.append("circuit: the entry %s is outside today's band %s - %s" % (p2(ref), p2(lo), p2(hi)))
            if target and target > hi + PAISA:
                later.append("circuit: the target %s is above the upper circuit %s: it cannot fill" % (p2(target), p2(hi)))
            if stop and stop < lo - PAISA:
                warns.append("the stop %s is below the lower circuit %s: if it locks at the lower circuit the "
                             "stop cannot fill at all" % (p2(stop), p2(lo)))

        if sym in blocked:
            later.append("blocked: %s is on your blocked_symbols list" % sym)

        max_loss = all_in = rr = None
        if kind == "entry" and side == "buy":
            # size, position, cash
            if value is not None:
                mo = _f(c.get("max_order_value"))
                if mo and value > mo + PAISA:
                    later.append("size: %s is over your %s max order value" % (nse.inr(value), nse.inr(mo, 0)))
                cap = capital * float(c.get("position_cap_pct") or 100) / 100.0
                if capital and value > cap + PAISA:
                    later.append("position: %s is %.1f%% of your %s capital, over your %g%% cap"
                                 % (nse.inr(value), 100.0 * value / capital, nse.inr(capital, 0),
                                    float(c.get("position_cap_pct"))))
                if capital and batch_value + value > capital - buffer + PAISA:
                    later.append("cash: %s needs more than the %s left after your %g%% buffer; that would be "
                                 "leverage" % (nse.inr(value), nse.inr(capital - buffer - batch_value),
                                               float(c.get("cash_buffer_pct") or 0)))
            # stop, loss, reward:risk
            if not stop:
                later.append("stop: no stop. Every entry needs one (india-exit, then india-budget)")
            elif ref and stop >= ref - PAISA:
                later.append("stop: the stop %s must be below the entry %s" % (p2(stop), p2(ref)))
            elif ref and isinstance(qty, int):
                risk_ps = round(ref - stop, 2)
                max_loss = round(qty * int(round(risk_ps * 100)) / 100.0, 2)
                ch = nse.round_trip_cost(qty, ref, stop, exch)
                all_in = round(max_loss + ch, 2)
                counted = all_in if include else max_loss
                cap_l = _f(c.get("max_loss_per_trade"))
                if cap_l and counted > cap_l + PAISA:
                    later.append("loss: %d x %s = %s%s at the stop, over your %s max loss per trade"
                                 % (qty, p2(risk_ps), nse.inr(max_loss),
                                    (" + %s charges = %s" % (nse.inr(ch), nse.inr(all_in))) if include else "",
                                    nse.inr(cap_l, 0)))
                min_rr = _f(c.get("min_reward_risk"))
                if not target:
                    if min_rr:
                        later.append("reward:risk: no target to measure it against")
                elif target <= ref + PAISA:
                    later.append("target: the target %s must be above the entry %s" % (p2(target), p2(ref)))
                else:
                    rr = round((target - ref) / risk_ps, 2)
                    if min_rr and rr < min_rr - 1e-9:
                        later.append("reward:risk: %.2f (target %s, entry %s, stop %s) is under your minimum %g"
                                     % (rr, p2(target), p2(ref), p2(stop), min_rr))
                info.append("entry %s (%s), stop %s, target %s: risk %s a share"
                            % (p2(ref), {"limit": "limit price", "plan": "plan's entry", "price": "latest price"}[ref_from],
                               p2(stop), p2(target) if target else "none", p2(risk_ps)))
                info.append("max loss %s + %s charges = %s (limit %s), reward:risk %s"
                            % (nse.inr(max_loss), nse.inr(ch), nse.inr(all_in),
                               nse.inr(cap_l or 0, 0), ("%.2f" % rr) if rr is not None else "n/a"))
                so = (o.get("angel") or {}).get("stop_order") or {}
                if bkey == "angel" and _f(so.get("price")) and _f(so.get("price")) < stop - PAISA:
                    info.append("Angel stop-limit: trigger %s, limit %s: a fill down at the limit would lose %s more "
                                "(or, in a gap, it may not fill at all)"
                                % (p2(stop), p2(_f(so["price"])), nse.inr(qty * (stop - _f(so["price"])))))
            # the day
            daily = _f(c.get("daily_loss_limit"))
            used = book["risk_used"] + batch_risk
            if daily:
                if -book["realised"] >= daily - PAISA:
                    later.append("daily loss: today's realised loss %s has hit your %s daily loss limit: no more "
                                 "entries today" % (nse.inr(-book["realised"]), nse.inr(daily, 0)))
                elif all_in is not None and used + (all_in if include else max_loss) > daily + PAISA:
                    later.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                 "daily loss limit" % (nse.inr(used), nse.inr(all_in if include else max_loss),
                                                       nse.inr(used + (all_in if include else max_loss)),
                                                       nse.inr(daily, 0)))
            cap_t = int(c.get("max_trades_per_day") or 0)
            n_today = len(book["entries"]) + batch_entries
            if cap_t and n_today >= cap_t:
                later.append("trades: %d entries sent today, your limit is %d a day" % (n_today, cap_t))
            info.append("today before this: %d of %s trades, %s of %s daily loss used, realised %s"
                        % (n_today, cap_t or "unlimited", nse.inr(used), nse.inr(daily or 0, 0),
                           nse.inr(book["realised"])))
            later.extend(win)
            if sym in book["sent_symbols"]:
                later.append("duplicate: a %s entry was already sent today" % sym)
            if sym in seen:
                later.append("duplicate: a second %s entry in this batch" % sym)
            seen.add(sym)

        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("exit, not blocked: " + x for x in later)
            info.append("exit: sells %s of the %d %s you hold. Getting out is never blocked by a day limit."
                        % (qty, held, sym))
        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS" and kind == "entry":
            batch_entries += 1
            batch_risk += (all_in if include else max_loss) or 0.0
            batch_value += value or 0.0
        results.append({
            "index": i, "symbol": sym, "side": side, "kind": kind, "qty": qty,
            "entry_ref": round(ref, 2) if ref else None, "entry_ref_from": ref_from, "stop": stop, "target": target,
            "value": round(value, 2) if value is not None else None, "max_loss": max_loss,
            "max_loss_all_in": all_in, "reward_risk": rr, "fingerprint": fingerprint(o),
            "verdict": verdict, "reasons": reasons, "warnings": warns, "info": info})

    return {"date": today, "now": nse.iso_ist(now), "mode": mode, "results": results,
            "pass": sum(1 for r in results if r["verdict"] == "PASS"),
            "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
            "warnings": top,
            "today": {"entries_sent": len(book["entries"]), "risk_used": book["risk_used"],
                      "realised": book["realised"]}}


def report(res):
    lines = ["WARNING: " + w for w in res["warnings"]]
    if lines:
        lines.append("")
    for r in res["results"]:
        lines.append("%-5s #%d %s %s %s shares, %s" % (r["verdict"], r["index"], r["side"], r["symbol"], r["qty"], r["kind"]))
        lines.extend("          " + x for x in r["info"])
        lines.extend("        - " + x for x in r["reasons"])
        lines.extend("        ~ " + x for x in r["warnings"])
    lines.append("")
    lines.append("%d passed, %d blocked at %s IST (%s mode)." % (res["pass"], res["block"], res["now"][11:16],
                                                              res["mode"]))
    if res["block"]:
        lines.append("Blocked orders do not go to the broker. Fix the order or skip it; never loosen a limit to pass it.")
    else:
        lines.append("Every order passed. A PASS is not a yes: india-broker still asks you for each one.")
    lines.append("Not financial advice. Nothing is placed without your \"yes\".")
    return "\n".join(lines)


def fetch_circuits(orders, cfg, transport=None):
    """Today's circuit band from Dhan's /marketfeed/quote, or {} when there are no keys, no Data plan,
    or data_source is yahoo. Never raises."""
    if str((cfg or {}).get("broker") or "dhan").lower() == "angel":
        # Angel's quote FULL mode may carry circuit limits, but the field names are not in the SDK: skipped.
        return {}, None
    if str((cfg or {}).get("data_source", "auto")).lower() == "yahoo" or not nse.dhan_credentials():
        return {}, None
    out = {}
    try:
        d = nse.Dhan.from_env(transport=transport)
        by_seg = {}
        for o in orders:
            if o.get("security_id"):
                by_seg.setdefault("%s_EQ" % str(o.get("exchange") or "NSE").upper(), []).append(o["security_id"])
        for seg, ids in by_seg.items():
            for sid, q in d.quote(ids, seg).items():
                lo, hi = _f(q.get("lower_circuit_limit")), _f(q.get("upper_circuit_limit"))
                if lo and hi:
                    out[str(sid)] = (lo, hi)
    except (nse.NSEError, ValueError, KeyError, TypeError) as e:
        msg = str(e)
        tok = (nse.dhan_credentials() or (None, None))[1]
        if tok:
            msg = msg.replace(tok, "***")
        return {}, "circuit band not checked (Dhan quote: %s)" % msg[:160]
    return out, None


def main(argv=None, transport=None):
    td = nse.trader_dir()
    ap = argparse.ArgumentParser(description="Check proposed orders against your limits (india-risk).")
    ap.add_argument("--orders", default=os.path.join(td, "orders.json"))
    ap.add_argument("--config", default=os.path.join(td, "config.json"))
    ap.add_argument("--prices", default=os.path.join(td, "prices.json"))
    ap.add_argument("--broker", default=os.path.join(td, "broker.json"))
    ap.add_argument("--paper", default=os.path.join(td, "paper.json"))
    ap.add_argument("--out", default=os.path.join(td, "risk.json"))
    ap.add_argument("--scrip-offline", metavar="PATH",
                    help="the broker's instrument file from disk (Dhan's CSV, or Angel One's OpenAPIScripMaster.json)")
    ap.add_argument("--no-quote", action="store_true", help="skip Dhan's circuit-band quote")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-29 09:41\"")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        now = nse.parse_now(a.now)
        doc = nse.read_json(a.orders)
        if doc is None:
            raise RiskError("no orders at %s (run india-budget first)" % a.orders)
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        cfg = nse.read_json(a.config, {}) or {}
        exch = str(orders[0].get("exchange") or cfg.get("exchange") or "NSE").upper()
        if str(cfg.get("broker") or "dhan").lower() == "angel":
            scrip = angel.scrip_master(offline=a.scrip_offline, exchange=exch)
        else:
            scrip = nse.scrip_master(offline=a.scrip_offline, exchange=exch)
        prices, times = load_prices(nse.read_json(a.prices, {}) or {})
        broker = nse.read_json(a.broker, {}) or {}
        paper = nse.read_json(a.paper, {}) or {}
        circuits, cwarn = ({}, None) if a.no_quote else fetch_circuits(orders, cfg, transport)
        res = check(orders, cfg, scrip, now, prices, times, broker, paper, circuits)
        if cwarn:
            res["warnings"].append(cwarn)
        if not a.no_write:
            old = nse.read_json(a.out, {}) or {}
            hist = (old.get("history") or [])[-300:]
            for r in res["results"]:
                hist.append({"at": res["now"], "date": res["date"], "symbol": r["symbol"], "side": r["side"],
                             "fingerprint": r["fingerprint"], "verdict": r["verdict"], "reasons": r["reasons"]})
            rec = dict(res, checked_at=res["now"], orders_file=a.orders, history=hist)
            nse.write_json(a.out, rec)
    except (RiskError, nse.NSEError) as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
