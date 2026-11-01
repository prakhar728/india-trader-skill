#!/usr/bin/env python3
"""The parts of india-broker both brokers share. Standard library only.

dhan.py (Dhan) and angel_broker.py (Angel One SmartAPI) differ only in how a
live order reaches the broker. Everything that keeps an order safe is here,
once: the ticket, the order fingerprint (same function as india-risk), the
"READY only with a same-day risk PASS" rule, the verbatim yes and its number
checks, the 10-minute single-use approval, the paper/live gate, the local
paper ledger and its simulated fills, the marketable-limit exit price, and the
trader/broker.json log (which refuses to save if a secret would land in it).

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "india-common"))
    import nse

A Broker object names the broker for the messages ("Dhan", "dhan.py") and
says which block of an order is its request ("dhan" or "angel"), how to tell
whether its keys are there, which secrets must never be printed or saved, and
how to describe its request on a ticket.
"""

import datetime
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "india-common"))
import nse  # noqa: E402

# The fields that make an order THIS order. Same list in india-risk/check.py.
FP_FIELDS = ("symbol", "exchange", "series", "security_id", "side", "qty", "entry", "stop", "target",
             "product", "order_type", "flat_by", "kind", "dhan", "angel", "limit")
APPROVAL_MINUTES = 10
FRESH_MINUTES = 15
EXIT_LIMIT_TICKS = 5                 # a marketable sell: last price minus this many ticks
STOP_LIMIT_BUFFER_TICKS = 5          # a stop-limit's limit: the stop minus this many ticks
YES_WORDS = ("yes", "y")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if"}


class BrokerError(Exception):
    pass


class Broker(object):
    """What differs between the brokers, for the shared code."""

    def __init__(self, key, name, script, id_label, has_keys, keys_hint, secrets, file_secrets=None,
                 describe=None, paper_note=None, read_flag="--live-account"):
        self.key, self.name, self.script, self.id_label = key, name, script, id_label
        self.has_keys = has_keys             # () -> bool
        self.keys_hint = keys_hint           # the message when live mode has no keys
        self.secrets = secrets               # () -> [str]: never printed
        self.file_secrets = file_secrets or secrets   # () -> [str]: broker.json refuses to save them
        self.describe = describe             # (order, mode) -> [lines] for the ticket
        self.paper_note = paper_note or ("india-broker paper ledger: simulated fills from public prices, "
                                         "not a broker.")
        self.read_flag = read_flag

    def scrub(self, text):
        text = str(text)
        for secret in self.secrets() or []:
            if secret and len(secret) > 3:
                text = text.replace(secret, "***")
        return text


# ---------------------------------------------------------------- helpers

def fingerprint(order):
    """Same function as india-risk/check.py: the exact order."""
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


def kind_of(order):
    return order.get("kind") or ("entry" if str(order.get("side")).lower() == "buy" else "exit")


def marketable_sell(last, tick, ticks=EXIT_LIMIT_TICKS, floor=None):
    """A sell LIMIT that should fill now without being a market order: the last price minus a few
    ticks, rounded DOWN to the tick, never under the floor (the stop-limit price). (price, note)."""
    tick = float(tick)
    p = nse.round_down(float(last) - ticks * tick, tick)
    p = max(p, tick)
    if floor is not None and p < floor - 1e-9:
        return round(floor, 2), ("the last price %s minus %d ticks is under your stop-limit floor %s: the limit is "
                                 "%s and may not fill. To sell lower, pass --limit PRICE yourself"
                                 % (p2(last), ticks, p2(floor), p2(floor)))
    return p, "last %s minus %d ticks of %s" % (p2(last), ticks, tick)


def stop_limit_price(stop, tick, ticks=STOP_LIMIT_BUFFER_TICKS):
    """The limit of a stop-limit sell: the stop minus a few ticks, rounded DOWN to the tick."""
    return max(nse.round_down(float(stop) - ticks * float(tick), float(tick)), float(tick))


# ---------------------------------------------------------------- driving an exit to a fill

EXIT_REPRICE_MAX = 5                 # re-prices of an unfilled exit before falling back to the stop
EXIT_POLLS = 5                       # order-status reads per round ...
EXIT_POLL_SECONDS = 2.0              # ... this far apart (about 10 s a round)


class ExitOps(object):
    """What drive_exit needs from a broker. poll() -> (state, filled) with state "filled", "open" or
    "dead"; net() -> net open quantity; ltp() -> last price or None; reprice(total_qty, price) -> note;
    cancel() -> True once the exit is confirmed cancelled; restop(qty) -> order id, or None when the
    broker side cannot re-place a stop."""
    restop = None


def drive_exit(ops, qty, limit, tick, ticks=EXIT_LIMIT_TICKS, reprice_max=EXIT_REPRICE_MAX, stop=None,
               symbol="", sleep=None, polls=EXIT_POLLS, every=EXIT_POLL_SECONDS, say=print):
    """Follow a marketable LIMIT exit until it fills. Not filled after a round of polls: re-price what is
    left (never more than the net open quantity) at a fresh last price minus N ticks, N growing each
    round. Still not filled after reprice_max re-prices: if the price is above the original stop, cancel
    the exit and re-place the stop for what is left; otherwise leave the sell working. Either way, say so
    LOUDLY. Returns {"result": filled | flat | restopped | unfilled | unknown, "steps": [...]}."""
    steps, price, last, filled = [], float(limit), None, 0
    for rnd in range(reprice_max + 1):
        state = "open"
        for i in range(polls):
            if sleep:
                sleep(every)
            try:
                state, filled = ops.poll()
            except Exception as e:                      # cannot even read it: stop and say so
                say("WARNING: COULD NOT READ THE EXIT ORDER FOR %s (%s). CHECK IT IN YOUR BROKER'S APP NOW: THE "
                    "SHARES MAY HAVE NO STOP." % (symbol, str(e)[:120]))
                return {"result": "unknown", "steps": steps}
            if state == "filled" or filled >= qty:
                steps.append({"round": rnd, "filled": filled, "price": price})
                return {"result": "filled", "steps": steps, "price": price}
            if state == "dead":
                break
        net = ops.net()
        if net <= 0:
            ops.cancel()
            steps.append({"round": rnd, "note": "no open position left", "filled": filled})
            say("%s: no open position left (net 0): the exit is cancelled, nothing more to sell." % symbol)
            return {"result": "flat", "steps": steps}
        if rnd == reprice_max:
            break
        try:
            last = ops.ltp() or last
        except Exception:
            pass
        n = ticks * (rnd + 2)
        new = max(nse.round_down((last if last is not None else price) - n * tick, tick), tick)
        sell = min(qty - filled, net)                   # never more than is held: no short
        note = ops.reprice(filled + sell, new)
        steps.append({"round": rnd + 1, "price": new, "qty": sell, "last": last, "ticks": n, "note": note})
        say("%s: exit not filled; re-priced %d to %s (last %s minus %d ticks)%s."
            % (symbol, sell, p2(new), p2(last) if last is not None else "?", n, (": " + note) if note else ""))
        qty, price = filled + sell, new
    left = max(min(qty - filled, ops.net()), 0)
    try:
        last = ops.ltp() or last
    except Exception:
        pass
    if left <= 0:
        return {"result": "flat", "steps": steps}
    if ops.restop and stop and last is not None and last > stop:
        if ops.cancel():
            left = ops.net()
            if left <= 0:
                return {"result": "flat", "steps": steps}
            oid = ops.restop(left)
            steps.append({"restop": oid, "qty": left, "trigger": stop})
            say("WARNING: THE %s EXIT DID NOT FILL AFTER %d RE-PRICES. IT IS CANCELLED AND THE STOP IS BACK: %d "
                "SHARES, TRIGGER %s (order %s). YOU ARE STILL IN THE TRADE: SELL THEM IN THE APP IF YOU WANT OUT."
                % (symbol, reprice_max, left, p2(stop), oid))
            return {"result": "restopped", "steps": steps}
        say("WARNING: THE %s EXIT DID NOT FILL AND COULD NOT BE CANCELLED. %d SHARES HAVE NO STOP; THE LIMIT SELL AT "
            "%s MAY STILL BE WORKING. ACT IN YOUR BROKER'S APP NOW." % (symbol, left, p2(price)))
        return {"result": "unfilled", "steps": steps}
    say("WARNING: %d %s SHARES ARE STILL OPEN WITH NO STOP. THE LIMIT SELL AT %s IS STILL WORKING BUT HAS NOT FILLED "
        "(LAST %s%s). SELL THEM IN YOUR BROKER'S APP NOW." % (left, symbol, p2(price),
                                                            p2(last) if last is not None else "UNKNOWN",
                                                            (", AT OR UNDER THE STOP %s" % p2(stop)) if stop else ""))
    return {"result": "unfilled", "steps": steps}


# ---------------------------------------------------------------- files

class Paths(object):
    def __init__(self, a):
        td = nse.trader_dir()
        self.orders = getattr(a, "orders", None) or os.path.join(td, "orders.json")
        self.config = getattr(a, "config", None) or os.path.join(td, "config.json")
        self.risk = getattr(a, "risk", None) or os.path.join(td, "risk.json")
        self.broker = getattr(a, "broker", None) or os.path.join(td, "broker.json")
        self.paper = getattr(a, "paper", None) or os.path.join(td, "paper.json")
        self.prices = getattr(a, "prices", None) or os.path.join(td, "prices.json")
        self.plan = os.path.join(os.path.dirname(self.orders), "plan.json")


def load_state(path):
    st = nse.read_json(path, {}) or {}
    st.setdefault("orders", [])
    st.setdefault("shown", [])
    st.setdefault("snapshots", [])
    st.setdefault("days", {})
    return st


def save_state(path, st, bk):
    blob = json.dumps(st)
    for secret in bk.file_secrets() or []:
        if secret and secret in blob:
            raise BrokerError("refusing to write broker.json: it would contain a secret (%s key)" % bk.name)
    nse.write_json(path, st)


def log(st, now, **rec):
    rec = dict({"at": nse.iso_ist(now), "date": now.date().isoformat()}, **rec)
    st["orders"].append(rec)
    return rec


def latest(st, fp, today, statuses=None):
    found = None
    for r in st["orders"]:
        if r.get("fingerprint") == fp and r.get("date") == today and (statuses is None or r.get("status") in statuses):
            found = r
    return found


def day_summary(st, paper, today, mode):
    used, n = 0.0, 0
    for r in st["orders"]:
        if r.get("date") == today and r.get("status") == "sent" and r.get("kind") == "entry" and r.get("mode") == mode:
            used += _f(r.get("max_loss_all_in")) or 0.0
            n += 1
    realised = 0.0
    if mode == "paper":
        realised = sum(float(o["pnl"]) for o in (paper or {}).get("orders", [])
                       if o.get("date") == today and o.get("pnl") is not None)
    st["days"][today] = {"mode": mode, "entries_sent": n, "risk_used": round(used, 2),
                         "realised": round(realised, 2)}
    return st["days"][today]


# ---------------------------------------------------------------- the ticket

def ticket_line(o):
    """buy 21 RELIANCE at Rs 1,174.30 limit, stop Rs 1,163.30, target Rs 1,196.30, max loss ~ Rs 257 incl. charges,
    flat by 15:05"""
    side = str(o.get("side", "")).lower()
    sym = o.get("symbol", "?")
    if kind_of(o) == "exit":
        if str(o.get("order_type")).upper() == "LIMIT" and o.get("limit") is not None:
            how = "at %s limit (marketable, intraday)" % nse.inr(o["limit"])
        else:
            how = "at market (intraday)"
        return "%s %s %s %s, to close your position, flat by %s" % (side, o.get("qty"), sym, how,
                                                                  o.get("flat_by") or "15:05")
    how = ("at %s limit" % nse.inr(o["entry"])) if str(o.get("order_type")).upper() == "LIMIT" else "at market"
    parts = ["%s %s %s %s" % (side, o.get("qty"), sym, how)]
    if o.get("stop") is not None:
        parts.append("stop %s" % nse.inr(o["stop"]))
    if o.get("target") is not None:
        parts.append("target %s" % nse.inr(o["target"]))
    if o.get("max_loss_all_in") is not None:
        parts.append("max loss ~ %s incl. charges" % nse.inr(round(o["max_loss_all_in"]), 0))
    parts.append("flat by %s" % (o.get("flat_by") or "15:05"))
    return ", ".join(parts)


def order_numbers(o):
    nums = []
    for k in ("qty", "entry", "stop", "target", "max_loss", "max_loss_all_in", "value", "limit"):
        v = _f(o.get(k))
        if v is not None:
            nums.append(v)
    if _f(o.get("max_loss_all_in")) is not None:
        nums.append(float(round(o["max_loss_all_in"])))
    return nums


def parse_yes(reply, order, shown=False, batch_symbols=(), script="dhan.py"):
    """Is this reply an explicit yes for THIS one order? (ok, why).
    It must start with yes, and either name the symbol and the quantity, or be a yes to the ticket
    that was just shown. Every number in it must be one of this order's numbers."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, go, do it)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = str(order.get("symbol", "")).upper()
    tokens = set(re.findall(r"[A-Z0-9&][A-Z0-9&\-]*", raw.upper()))
    others = {s.upper() for s in batch_symbols} - {sym}
    if tokens & others:
        return False, "the reply names %s, but this order is for %s" % (", ".join(sorted(tokens & others)), sym)
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", raw)]
    want = order_numbers(order)
    bad = [n for n in nums if not any(abs(n - w) < 0.005 for w in want)]
    if bad:
        return False, "the reply says %s, but this order is: %s" % (", ".join("%g" % n for n in bad), ticket_line(order))
    names_both = sym in tokens and any(abs(n - float(order.get("qty") or -1)) < 1e-9 for n in nums)
    if not names_both and not shown:
        return False, ("the reply does not name %s and the quantity %s, and this ticket was not shown today: "
                       "show it first (%s show), then ask" % (sym, order.get("qty"), script))
    return True, "explicit yes for: " + ticket_line(order)


def parse_exit_yes(reply):
    """The optional pre-approval to exit at the target / flat-by without asking again. (ok, why)."""
    raw = (reply or "").strip()
    words = re.findall(r"[a-z'\-]+", raw.lower())
    if not words or words[0] not in YES_WORDS or "?" in raw:
        return False, "it must start with \"yes\""
    if DOUBT_WORDS & set(words[1:]) - {"stop"}:
        return False, "the reply has a condition or a doubt in it"
    if not ({"exit", "sell", "out", "close"} & set(words)):
        return False, "it must say exit (or sell / close / out), e.g. \"yes, exit at the target or flat-by\""
    return True, ""


def risk_verdict(risk, fp, today):
    if not risk:
        return None, "india-risk has not checked these orders (run check.py)"
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today: run check.py again" % risk.get("date")
    for r in risk.get("results", []):
        if r.get("fingerprint") == fp:
            return r, None
    return None, "this exact order was not in the last risk check (it changed or is new): run check.py again"


def status_of(order, risk, st, today, script="dhan.py"):
    """(ready, label, risk_result)."""
    fp = fingerprint(order)
    rv, why = risk_verdict(risk, fp, today)
    if kind_of(order) == "entry":
        if rv is None:
            return False, "NOT READY: " + why, None
        if rv.get("verdict") != "PASS":
            return False, "BLOCKED by india-risk: " + "; ".join(rv.get("reasons", [])), rv
    elif rv is not None and rv.get("verdict") != "PASS":
        return False, "BLOCKED by india-risk (format, body or short sale): " + "; ".join(rv.get("reasons", [])), rv
    rec = latest(st, fp, today, ("approved", "declined", "sent", "rejected", "failed", "unknown"))
    if rec:
        s = rec.get("status")
        if s == "approved":
            return False, "APPROVED by you at %s: waiting for `%s send`" % (rec["at"][11:16], script), rv
        if s == "unknown":
            return False, ("UNKNOWN: the send at %s got no answer. Check `%s status` before anything else"
                           % (rec["at"][11:16], script)), rv
        if s in ("failed", "declined", "rejected") and risk and str(risk.get("checked_at") or "") > rec["at"]:
            pass                                   # checked again since: a fresh yes may follow
        else:
            return False, "DONE: %s at %s" % (s, rec["at"][11:16]), rv
    if rv is None:
        return True, "READY (exit: getting out is never blocked; no risk PASS needed), waiting for your yes", rv
    return True, "READY: india-risk PASS at %s, waiting for your yes" % str(risk.get("checked_at") or "")[11:16], rv


def render(o, idx, total, mode, risk, st, today, bk):
    ready, label, rv = status_of(o, risk, st, today, bk.script)
    acct = ("PAPER (local simulated ledger, nothing goes to %s)" % bk.name if mode == "paper"
            else "LIVE %s ACCOUNT, REAL MONEY" % bk.name.upper())
    lines = ["ORDER %d of %d, %s" % (idx, total, acct), "  " + ticket_line(o)]
    lines.append("  %s %s, %s, series %s, %s %s %s, fingerprint %s"
                 % (o.get("product"), o.get("order_type"), o.get("exchange"), o.get("series") or "-",
                    bk.name, bk.id_label, o.get("security_id"), fingerprint(o)))
    if o.get("why"):
        lines.append("  why: " + o["why"])
    lines.append("  " + label)
    for w in (rv or {}).get("warnings", []):
        lines.append("  note: " + w)
    lines.extend(bk.describe(o, mode))
    if ready:
        lines.append("  Reply \"yes\" (or \"yes buy %s %s\") to place this one order, or \"no\" to skip it."
                     % (o.get("qty"), o.get("symbol")))
        lines.append("  Claude Code then asks once more before it runs `%s send`." % bk.script)
    return "\n".join(lines), ready


# ---------------------------------------------------------------- mode gate

def gate(cfg, a, bk, writes=True):
    """paper or live. Live needs mode live in config AND --live AND the keys."""
    mode = str(cfg.get("mode") or "paper").lower()
    flag = bool(getattr(a, "live", False))
    if mode not in ("paper", "live"):
        raise BrokerError("config mode must be paper or live, got %r" % mode)
    if flag and mode != "live":
        raise BrokerError("--live given but trader/config.json says \"mode\": \"%s\". Going live is your own "
                          "decision: set \"mode\": \"live\" in the config yourself first." % mode)
    if mode == "live" and writes and not flag:
        raise BrokerError("config is in LIVE mode: this command touches your real account, so it needs --live "
                          "on the command as well")
    if mode == "live" and not bk.has_keys():
        raise BrokerError(bk.keys_hint)
    return mode


# ---------------------------------------------------------------- paper ledger

def load_paper(path, cfg, note=None):
    pp = nse.read_json(path, None)
    if pp is None:
        cap = float(cfg.get("capital") or 100000)
        pp = {"note": note or "india-broker paper ledger: simulated fills from public prices, not a broker.",
              "start_cash": cap, "cash": cap, "orders": []}
    pp.setdefault("orders", [])
    return pp


def load_bars(a, sym, exchange, now):
    """1-minute bars for paper fills: --bars FILE (Yahoo chart JSON), else Yahoo unless --offline. [] when none."""
    if getattr(a, "bars", None):
        _, bars = nse.parse_yahoo_chart(nse.read_maybe_gz(a.bars))
        return bars
    if getattr(a, "offline", False):
        return []
    try:
        _, bars = nse.yahoo_bars(sym, "1d", "1m", exchange)
        return bars
    except nse.NSEError:
        return []


def step_of(bars):
    if len(bars) < 2:
        return datetime.timedelta(minutes=1)
    diffs = sorted((bars[i + 1]["t"] - bars[i]["t"]) for i in range(len(bars) - 1))
    return diffs[len(diffs) // 2]


def price_args(a):
    out = {}
    for item in (getattr(a, "price", None) or []):
        if "=" in item:
            s, p = item.split("=", 1)
            out[s.strip().upper()] = float(p)
        else:
            out["*"] = float(item)
    return out


def latest_price(a, sym, exchange, now, paths, bars=None):
    """(price, source) or (None, None): --price, then closed 1-minute bars, then a fresh prices.json."""
    given = price_args(a)
    if sym in given or "*" in given:
        return given.get(sym, given.get("*")), "--price"
    bars = bars if bars is not None else load_bars(a, sym, exchange, now)
    if bars:
        step = step_of(bars)
        closed = [b for b in bars if b["t"] + step <= now]
        if closed:
            return closed[-1]["c"], "bar %s close" % closed[-1]["t"].strftime("%H:%M")
    doc = nse.read_json(paths.prices, {}) or {}
    prices = doc.get("prices") if isinstance(doc.get("prices"), dict) else doc
    v = prices.get(sym) if isinstance(prices, dict) else None
    t = None
    if isinstance(v, dict):
        t = parse_iso_ist(v.get("time") or v.get("fetched"))
        v = v.get("price")
    det = (doc.get("detail") or {}).get(sym) or {}
    t = t or parse_iso_ist(det.get("time")) or parse_iso_ist(det.get("fetched")) or parse_iso_ist(doc.get("as_of"))
    if _f(v) and t and -60 <= (now - t).total_seconds() <= FRESH_MINUTES * 60:
        return float(v), "prices.json %s" % t.strftime("%H:%M")
    return None, None


def close_paper(o, price, at, reason, pp, exchange="NSE"):
    o["status"] = "CLOSED"
    o["exit_price"] = round(price, 2)
    o["exit_at"] = nse.iso_ist(at)
    o["exit_reason"] = reason
    ch = nse.charges(o["qty"] * o["fill_price"], o["qty"] * o["exit_price"], exchange)["total"]
    o["charges"] = ch
    o["pnl"] = round(o["qty"] * (o["exit_price"] - o["fill_price"]) - ch, 2)
    pp["cash"] = round(pp["cash"] + o["qty"] * o["exit_price"] - ch, 2)


def fill_paper(o, price, at, pp):
    o["status"] = "TRADED"
    o["fill_price"] = round(price, 2)
    o["filled_at"] = nse.iso_ist(at)
    pp["cash"] = round(pp["cash"] - o["qty"] * o["fill_price"], 2)


def simulate(o, bars, now, pp, events, price=None, price_src=None):
    """Walk closed bars after the order was placed: fill, then stop / target / flat-by.
    Conservative: a bar that touches both the stop and the target counts as the stop."""
    placed = parse_iso_ist(o["placed_at"])
    flat = datetime.datetime.combine(placed.date(), nse.hhmm(o.get("flat_by") or "15:05"))
    step = step_of(bars) if bars else datetime.timedelta(minutes=1)
    start = placed.replace(second=0, microsecond=0)
    if placed > start:
        start += datetime.timedelta(minutes=1)
    for b in bars:
        if b["t"] < start or b["t"] + step > now or b["t"].date() != placed.date():
            continue
        if o["status"] == "PENDING":
            if b["t"] >= flat:
                o["status"] = "CANCELLED"
                o["exit_reason"] = "entry not filled by flat-by %s" % flat.strftime("%H:%M")
                events.append("%s entry not filled by %s: cancelled" % (o["symbol"], flat.strftime("%H:%M")))
                return
            if o["order_type"] == "MARKET":
                px = b["o"]
            elif b["l"] <= o["entry"] + 1e-9:
                px = min(o["entry"], b["o"])
            else:
                continue
            fill_paper(o, px, b["t"], pp)
            events.append("%s bought %d at %s (%s bar)" % (o["symbol"], o["qty"], p2(o["fill_price"]), b["t"].strftime("%H:%M")))
            if o.get("stop") and b["l"] <= o["stop"] + 1e-9:
                close_paper(o, min(o["stop"], px), b["t"], "stop", pp)
                events.append("%s stopped out at %s in the fill bar" % (o["symbol"], p2(o["exit_price"])))
                return
            continue
        if o["status"] == "TRADED":
            if b["t"] >= flat:
                close_paper(o, b["o"], b["t"], "flat-by %s" % flat.strftime("%H:%M"), pp)
            elif o.get("stop") and b["l"] <= o["stop"] + 1e-9:
                close_paper(o, min(o["stop"], b["o"]), b["t"], "stop", pp)
            elif o.get("target") and b["h"] >= o["target"] - 1e-9:
                close_paper(o, max(o["target"], b["o"]), b["t"], "target", pp)
            else:
                continue
            events.append("%s sold %d at %s: %s, P&L %s after %s charges"
                          % (o["symbol"], o["qty"], p2(o["exit_price"]), o["exit_reason"], nse.inr(o["pnl"]),
                             nse.inr(o["charges"])))
            return
    # no bars (or no more bars): the latest price alone
    if price is not None and now >= start:
        if o["status"] == "PENDING" and now < flat:
            if o["order_type"] == "MARKET" or price <= o["entry"] + 1e-9:
                fill_paper(o, min(price, o["entry"]) if o["order_type"] == "LIMIT" else price, now, pp)
                events.append("%s bought %d at %s (%s)" % (o["symbol"], o["qty"], p2(o["fill_price"]), price_src))
        elif o["status"] == "TRADED":
            if now >= flat:
                close_paper(o, price, now, "flat-by %s" % flat.strftime("%H:%M"), pp)
            elif o.get("stop") and price <= o["stop"] + 1e-9:
                close_paper(o, price, now, "stop", pp)
            elif o.get("target") and price >= o["target"] - 1e-9:
                close_paper(o, o["target"], now, "target", pp)
            if o["status"] == "CLOSED":
                events.append("%s sold %d at %s (%s): %s, P&L %s" % (o["symbol"], o["qty"], p2(o["exit_price"]),
                                                                     price_src, o["exit_reason"], nse.inr(o["pnl"])))
    if o["status"] == "PENDING" and now >= flat:
        o["status"] = "CANCELLED"
        o["exit_reason"] = "entry not filled by flat-by %s" % flat.strftime("%H:%M")
        events.append("%s entry not filled by %s: cancelled" % (o["symbol"], flat.strftime("%H:%M")))
    if o["status"] == "TRADED" and now >= flat:
        events.append("%s is still open after flat-by %s and there is no price: run paper-check with --price"
                      % (o["symbol"], flat.strftime("%H:%M")))


def paper_events_to_log(st, now, pp, before):
    """Log fills and closes that happened in this pass to broker.json."""
    for o in pp["orders"]:
        prev = before.get(o["id"])
        if prev == o["status"]:
            continue
        if o["status"] in ("TRADED", "CLOSED") and prev in (None, "PENDING"):
            log(st, now, status="filled", mode="paper", symbol=o["symbol"], side="buy", qty=o["qty"],
                price=o["fill_price"], fill_at=o["filled_at"], paper_id=o["id"], fingerprint=o["fingerprint"])
        if o["status"] == "CLOSED":
            log(st, now, status="closed", mode="paper", symbol=o["symbol"], side="sell", qty=o["qty"],
                price=o["exit_price"], reason=o["exit_reason"], pnl=o["pnl"], charges=o["charges"],
                paper_id=o["id"], fingerprint=o["fingerprint"])
        if o["status"] == "CANCELLED":
            log(st, now, status="cancelled", mode="paper", symbol=o["symbol"], reason=o.get("exit_reason"),
                paper_id=o["id"], fingerprint=o["fingerprint"])


def send_paper(a, paths, cfg, st, now, order, base, bk):
    pp = load_paper(paths.paper, cfg, bk.paper_note)
    sym = order["symbol"]
    exch = order.get("exchange", "NSE")
    blk = order.get(bk.key) or {}
    if kind_of(order) == "exit":
        bars = load_bars(a, sym, exch, now)
        price, src = latest_price(a, sym, exch, now, paths, bars)
        limit = _f(order.get("limit")) if str(order.get("order_type")).upper() == "LIMIT" else None
        before = dict((o["id"], o["status"]) for o in pp["orders"])
        events = []
        for o in pp["orders"]:               # catch up first: the stop or target may already have filled
            if o["symbol"] == sym and o["status"] in ("PENDING", "TRADED"):
                simulate(o, bars, now, pp, events, None, None)
        paper_events_to_log(st, now, pp, before)
        for e in events:
            print("PAPER (catching up): " + e)
        opened = [o for o in pp["orders"] if o["symbol"] == sym and o["status"] == "TRADED"]
        pending = [o for o in pp["orders"] if o["symbol"] == sym and o["status"] == "PENDING"]
        if opened and price is None:
            raise BrokerError("not sent: no price for %s to close at (pass --price %s=P, or --bars)" % (sym, sym))
        if opened and limit is not None and price < limit - 1e-9:
            nse.write_json(paths.paper, pp)
            raise BrokerError("PAPER: not filled: the last price %s (%s) is under your sell limit %s. Run flatten "
                              "again for a fresh limit" % (p2(price), src, p2(limit)))
        left = int(order["qty"])
        for o in pending:
            o["status"] = "CANCELLED"
            o["exit_reason"] = "cancelled by flatten"
        for o in opened:
            if left <= 0:
                break
            close_paper(o, price, now, "flatten (%s)" % src, pp, exch)
            left -= o["qty"]
        nse.write_json(paths.paper, pp)
        log(st, now, status="sent", response={"paper": "closed at %s (%s)" % (p2(price) if price else "-", src)}, **base)
        for o in opened:
            log(st, now, status="closed", mode="paper", symbol=sym, side="sell", qty=o["qty"], price=o["exit_price"],
                reason=o["exit_reason"], pnl=o["pnl"], charges=o["charges"], paper_id=o["id"],
                fingerprint=o["fingerprint"])
        print("PAPER: closed %s %s at %s (%s)." % (sym, order["qty"], p2(price) if price else "-", src))
        return 0
    n = sum(1 for o in pp["orders"] if o.get("date") == now.date().isoformat()) + 1
    po = {"id": "P-%s-%d" % (now.strftime("%Y%m%d"), n), "date": now.date().isoformat(),
          "fingerprint": base["fingerprint"], "symbol": sym, "exchange": exch, "security_id": order.get("security_id"),
          "side": "buy", "qty": int(order["qty"]), "order_type": order.get("order_type", "LIMIT"),
          "entry": order.get("entry"), "stop": order.get("stop"), "target": order.get("target"),
          "flat_by": order.get("flat_by") or "15:05", "status": "PENDING", "placed_at": nse.iso_ist(now),
          "correlationId": (blk.get("body") or {}).get("correlationId")}
    if bk.key != "dhan":
        po.pop("correlationId")
        po["broker"] = bk.key
    pp["orders"].append(po)
    request = {"paper": True, "would_be": blk.get("endpoint") or blk.get("route"), "body": blk.get("body")}
    if blk.get("stop_order"):
        request["stop_order"] = blk["stop_order"]
    log(st, now, status="sent", broker_order_id=po["id"], request=request,
        response={"orderId": po["id"], "orderStatus": "PENDING"}, **base)
    events = []
    bars = load_bars(a, sym, exch, now)
    price, src = latest_price(a, sym, exch, now, paths, bars)
    simulate(po, bars, now, pp, events, price, src)
    paper_events_to_log(st, now, {"orders": [po]}, {po["id"]: "PENDING"})
    nse.write_json(paths.paper, pp)
    print("PAPER order %s: %s. Nothing went to %s." % (po["id"], ticket_line(order), bk.name))
    for e in events:
        print("  " + e)
    if po["status"] == "PENDING":
        print("  Pending: the price has not come to %s yet%s. `%s paper-check` fills it from the latest bars."
              % (p2(po["entry"]) if po["entry"] else "market", (" (last %s, %s)" % (p2(price), src)) if price else "",
                 bk.script))
    return 0


# ---------------------------------------------------------------- commands every broker shares

def clock(a):
    return nse.parse_now(getattr(a, "now", None))


def load_all(a, bk):
    paths = Paths(a)
    doc = nse.read_json(paths.orders)
    if doc is None:
        raise BrokerError("no orders at %s (run india-budget, or %s flatten, first)" % (paths.orders, bk.script))
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise BrokerError("no orders in %s" % paths.orders)
    cfg = nse.read_json(paths.config, {}) or {}
    risk = nse.read_json(paths.risk, None)
    st = load_state(paths.broker)
    return paths, orders, cfg, risk, st


def pick(orders, index):
    if index is None:
        raise BrokerError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise BrokerError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def cmd_show(a, bk):
    paths, orders, cfg, risk, st = load_all(a, bk)
    now = clock(a)
    today = now.date().isoformat()
    mode = str(cfg.get("mode") or "paper").lower()
    picks = [(a.index, pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    out, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), mode, risk, st, today, bk)
        out.append(text)
        any_ready = any_ready or ready
        if ready:
            st["shown"].append({"at": nse.iso_ist(now), "date": today, "index": i, "fingerprint": fingerprint(o)})
    save_state(paths.broker, st, bk)
    if a.json:
        print(json.dumps([{"index": i, "ready": status_of(o, risk, st, today, bk.script)[0], "ticket": ticket_line(o),
                           "fingerprint": fingerprint(o), bk.key: o.get(bk.key)} for i, o in picks], indent=2))
    else:
        print("\n\n".join(out))
        print("\nOne order at a time: a yes covers one ticket, never the batch. Not financial advice.")
    return 0 if any_ready else 1


def cmd_approve(a, bk, extra=None):
    """extra(order, a) -> dict of more fields for the approval record (or raises / returns None)."""
    paths, orders, cfg, risk, st = load_all(a, bk)
    now = clock(a)
    today = now.date().isoformat()
    order = pick(orders, a.index)
    ready, label, rv = status_of(order, risk, st, today, bk.script)
    if not ready:
        raise BrokerError("order %d is not ready: %s" % (a.index, label))
    fp = fingerprint(order)
    shown = any(s.get("fingerprint") == fp and s.get("date") == today for s in st["shown"])
    ok, why = parse_yes(a.yes, order, shown, [o.get("symbol", "") for o in orders], bk.script)
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
        return 1
    more = extra(order, a) if extra else {}
    if more is None:
        return 1
    mode = str(cfg.get("mode") or "paper").lower()
    log(st, now, status="approved", index=a.index, fingerprint=fp, symbol=order.get("symbol"), side=order.get("side"),
        qty=order.get("qty"), kind=kind_of(order), max_loss_all_in=order.get("max_loss_all_in"),
        ticket=ticket_line(order), reply=a.yes, mode=mode, **more)
    save_state(paths.broker, st, bk)
    print("Recorded your yes for order %d: %s." % (a.index, ticket_line(order)))
    print("It is good for %d minutes. Next: %s send --index %d%s"
          % (int(cfg.get("approval_minutes") or APPROVAL_MINUTES), bk.script, a.index, " --live" if mode == "live" else ""))
    return 0


def cmd_decline(a, bk):
    paths, orders, cfg, risk, st = load_all(a, bk)
    now = clock(a)
    order = pick(orders, a.index)
    log(st, now, status="declined", index=a.index, fingerprint=fingerprint(order), symbol=order.get("symbol"),
        side=order.get("side"), ticket=ticket_line(order), reply=a.reply or "no")
    save_state(paths.broker, st, bk)
    print("Recorded: you said no to order %d (%s). Nothing was sent." % (a.index, ticket_line(order)))
    return 0


def cmd_send(a, bk, send_live):
    """The gates every send goes through, then paper or send_live(a, paths, cfg, st, now, order, base, approval)."""
    paths, orders, cfg, risk, st = load_all(a, bk)
    now = clock(a)
    today = now.date().isoformat()
    mode = gate(cfg, a, bk, writes=True)
    order = pick(orders, a.index)
    fp = fingerprint(order)
    kind = kind_of(order)
    rv, why = risk_verdict(risk, fp, today)
    if kind == "entry" and (rv is None or rv.get("verdict") != "PASS"):
        raise BrokerError("not sent: %s" % (why or "india-risk BLOCKED this order: " + "; ".join(rv.get("reasons", []))))
    rec = latest(st, fp, today, ("approved", "declined", "sent", "rejected", "failed", "unknown"))
    if not rec or rec.get("status") != "approved":
        raise BrokerError("not sent: no yes recorded today for this exact order%s. Show the ticket, ask the user, "
                          "and record the reply with `approve`" % ((" (last: %s)" % rec["status"]) if rec else ""))
    age = (now - parse_iso_ist(rec["at"])).total_seconds() / 60.0
    limit = float(cfg.get("approval_minutes") or APPROVAL_MINUTES)
    if age > limit or age < -1:
        raise BrokerError("not sent: the yes from %s is %.0f minutes old (stale; good for %g). Show the ticket and "
                          "ask again" % (rec["at"][11:16], age, limit))
    if rec.get("mode") and rec["mode"] != mode:
        raise BrokerError("not sent: the yes was given in %s mode, this is %s mode. Ask again" % (rec["mode"], mode))
    if kind == "entry":
        st_ = nse.market_status(now, cfg)
        flat = nse.flat_by(cfg, now.date())
        if st_["state"] != "open" or now.time() >= flat:
            raise BrokerError("not sent: the market is %s / it is at or past flat-by %s IST. No new entries"
                              % (st_["state"], flat.strftime("%H:%M")))
    base = dict(index=a.index, fingerprint=fp, symbol=order.get("symbol"), side=order.get("side"),
                qty=order.get("qty"), kind=kind, max_loss_all_in=order.get("max_loss_all_in"),
                ticket=ticket_line(order), mode=mode)
    if mode == "paper":
        code = send_paper(a, paths, cfg, st, now, order, base, bk)
    else:
        code = send_live(a, paths, cfg, st, now, order, base, rec)
    paper = nse.read_json(paths.paper, {}) or {}
    day_summary(st, paper, today, mode)
    save_state(paths.broker, st, bk)
    d = st["days"][today]
    print("Today (%s): %d entries sent, %s of risk used, realised %s." % (mode, d["entries_sent"],
                                                                         nse.inr(d["risk_used"]), nse.inr(d["realised"])))
    return code


def cmd_paper_check(a, bk):
    paths = Paths(a)
    cfg = nse.read_json(paths.config, {}) or {}
    now = clock(a)
    pp = load_paper(paths.paper, cfg, bk.paper_note)
    st = load_state(paths.broker)
    live = [o for o in pp["orders"] if o["status"] in ("PENDING", "TRADED")]
    if not live:
        print("PAPER: nothing open or pending.")
        return 0
    before = dict((o["id"], o["status"]) for o in pp["orders"])
    events = []
    bars_cache = {}
    for o in live:
        key = (o["symbol"], o.get("exchange", "NSE"))
        if key not in bars_cache:
            bars_cache[key] = load_bars(a, o["symbol"], key[1], now)
        bars = bars_cache[key]
        price, src = latest_price(a, o["symbol"], key[1], now, paths, bars)
        simulate(o, bars, now, pp, events, price, src)
    paper_events_to_log(st, now, pp, before)
    nse.write_json(paths.paper, pp)
    day_summary(st, pp, now.date().isoformat(), "paper")
    save_state(paths.broker, st, bk)
    for e in events:
        print("PAPER: " + e)
    for o in pp["orders"]:
        if o["status"] in ("PENDING", "TRADED"):
            print("PAPER: %s %s %d %s, stop %s, target %s, flat by %s"
                  % (o["id"], o["status"].lower(), o["qty"], o["symbol"], p2(o["stop"]), p2(o["target"]), o["flat_by"]))
    d = st["days"].get(now.date().isoformat(), {})
    print("Cash %s. Realised today %s." % (nse.inr(pp["cash"]), nse.inr(d.get("realised", 0))))
    return 0


def print_paper_ledger(paths, cfg, now, bk):
    pp = load_paper(paths.paper, cfg, bk.paper_note)
    rows = [o for o in pp["orders"] if o.get("date") == now.date().isoformat()]
    print("PAPER ledger (%s), cash %s:" % (paths.paper, nse.inr(pp["cash"])))
    for o in rows:
        print("  %s %-9s %d %s entry %s fill %s exit %s %s pnl %s" % (
            o["id"], o["status"], o["qty"], o["symbol"], o.get("entry"), o.get("fill_price", "-"),
            o.get("exit_price", "-"), o.get("exit_reason", ""), nse.inr(o["pnl"]) if o.get("pnl") is not None else "-"))
    if not rows:
        print("  nothing today")
    print("(paper mode; add %s to read your real %s account instead)" % (bk.read_flag, bk.name))
    return 0


def cancel_paper(paths, cfg, st, now, order_id, bk):
    pp = load_paper(paths.paper, cfg, bk.paper_note)
    for o in pp["orders"]:
        if o["id"] == order_id and o["status"] == "PENDING":
            o["status"] = "CANCELLED"
            o["exit_reason"] = "cancelled by you"
            nse.write_json(paths.paper, pp)
            log(st, now, status="cancelled", mode="paper", symbol=o["symbol"], paper_id=o["id"],
                fingerprint=o["fingerprint"])
            save_state(paths.broker, st, bk)
            print("PAPER: cancelled %s." % o["id"])
            return 0
    raise BrokerError("no pending paper order %s (a filled one is closed with flatten)" % order_id)


def cmd_log(a, bk):
    paths = Paths(a)
    st = load_state(paths.broker)
    now = clock(a)
    today = now.date().isoformat()
    rows = [r for r in st["orders"] if a.all or r.get("date") == today]
    for r in rows:
        extra = (" id %s" % r["broker_order_id"]) if r.get("broker_order_id") else ""
        reply = (' reply "%s"' % r["reply"]) if r.get("reply") else ""
        pnl = (" pnl %s" % nse.inr(r["pnl"])) if r.get("pnl") is not None else ""
        print("%s %-9s %-5s %s%s%s%s" % (r.get("at"), r.get("status"), r.get("mode", ""),
                                         r.get("ticket") or "%s %s %s" % (r.get("side", ""), r.get("qty", ""), r.get("symbol")),
                                         reply, pnl, extra))
    if not rows:
        print("Nothing logged%s." % ("" if a.all else " today"))
    d = st["days"].get(today)
    if d:
        print("Today (%s): %d entries sent, %s risk used, realised %s." % (d["mode"], d["entries_sent"],
                                                                          nse.inr(d["risk_used"]), nse.inr(d["realised"])))
    return 0


# ---------------------------------------------------------------- the exit ticket (flatten)

def paper_position(paths, cfg, st, now, sym, bk):
    """(qty, security_id) held in the paper ledger, or None after cancelling a lone pending entry."""
    pp = load_paper(paths.paper, cfg, bk.paper_note)
    opened = [o for o in pp["orders"] if o["symbol"] == sym and o["status"] == "TRADED"]
    pend = [o for o in pp["orders"] if o["symbol"] == sym and o["status"] == "PENDING"]
    if not opened and pend:
        for o in pend:
            o["status"] = "CANCELLED"
            o["exit_reason"] = "cancelled by flatten"
            log(st, now, status="cancelled", mode="paper", symbol=sym, paper_id=o["id"], fingerprint=o["fingerprint"],
                reason="flatten before fill")
        nse.write_json(paths.paper, pp)
        save_state(paths.broker, st, bk)
        print("PAPER: %s had only a pending entry; cancelled it. Nothing to sell." % sym)
        return None
    return sum(o["qty"] for o in opened), (opened or pend or [{}])[0].get("security_id")


def exit_price(a, cfg, now, paths, sym, exch, tick, floor, live_ltp=None):
    """(limit, note) for the marketable sell. --limit wins (rounded down to the tick); then the last
    price from --price, the broker's own quote (live), Yahoo 1-minute bars or a fresh prices.json."""
    ticks = int(cfg.get("exit_limit_ticks") or EXIT_LIMIT_TICKS)
    if getattr(a, "limit", None) is not None:
        lim = nse.round_down(float(a.limit), tick)
        return lim, "your limit %s" % p2(lim)
    given = price_args(a)
    price, src = None, None
    if sym in given or "*" in given:
        price, src = given.get(sym, given.get("*")), "--price"
    elif live_ltp is not None:
        try:
            price = live_ltp()
            src = "broker quote"
        except Exception:                    # a quote failure must not stop getting out
            price = None
    if price is None:
        price, src = latest_price(a, sym, exch, now, paths)
    if price is None:
        raise BrokerError("no price for %s to set the sell limit: pass --price %s=P (the last price in your broker's "
                          "app) or --limit P" % (sym, sym))
    lim, note = marketable_sell(price, tick, ticks, floor)
    return lim, "%s (%s)" % (note, src)


def plan_for(paths, sym):
    plan = nse.read_json(paths.plan, {}) or {}
    return plan if str(plan.get("symbol", "")).upper() == sym else {}


def write_exit(paths, now, mode, order, bk):
    old = nse.read_json(paths.orders, {}) or {}
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    nse.write_json(paths.orders, {"made": nse.iso_ist(now), "made_by": "india-broker flatten", "status": "proposed",
                                  "mode": mode, "note": "Exit ticket. Getting out is never blocked; it still needs your yes.",
                                  "orders": [order]})
    print("Flatten %s: %s." % (order["symbol"], ticket_line(order)))
    print("Wrote it to %s%s." % (paths.orders, (" (replaced %d earlier proposal%s)" % (replaced, "" if replaced == 1 else "s"))
                                 if replaced else ""))
    print("Next: india-risk check.py (an exit always passes the day limits), %s show, the user's yes, "
          "%s approve, %s send%s." % (bk.script, bk.script, bk.script, " --live" if mode == "live" else ""))


def tick_for(paths, sym, scrip_fn, price_hint=None):
    """The stock's tick: today's plan, else the broker's instrument file, else NSE's band."""
    plan = plan_for(paths, sym)
    if _f(plan.get("tick")):
        return float(plan["tick"]), None
    try:
        row = (scrip_fn() or {}).get(sym) or {}
    except (nse.NSEError, ValueError, OSError):
        row = {}
    if row.get("tick"):
        return float(row["tick"]), row
    return nse.band_tick(price_hint or _f(plan.get("entry")) or 1000.0), row or None


def files_args(p):
    p.add_argument("--orders")
    p.add_argument("--config")
    p.add_argument("--risk")
    p.add_argument("--broker")
    p.add_argument("--paper")
    p.add_argument("--prices")
    p.add_argument("--now", help="pretend it is this IST time")


def price_opts(p):
    p.add_argument("--price", action="append", metavar="SYM=P", help="a price for paper fills / the exit limit")
    p.add_argument("--bars", metavar="FILE", help="Yahoo chart JSON of 1-minute bars for paper fills")
    p.add_argument("--offline", action="store_true", help="no Yahoo fetch for paper fills")

