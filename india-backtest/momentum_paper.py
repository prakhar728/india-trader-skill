#!/usr/bin/env python3
"""Paper portfolio for the monthly momentum rotation, forward in time. Paper only: it never talks to a broker.

The rule is swing.py's momentum_rotation, unchanged: at the last close of each month rank every stock in
the point-in-time universe (pit100 by default: the 100 NSE stocks with the highest median traded value
over the last 126 sessions, known at that close) that closes above its 200-day SMA by its 12-1 month
return (close 21 sessions ago / close 252 sessions ago - 1); hold the top 10. At the next session's open
sell what left the top 10 and buy the newcomers with (equity at the month-end close) / 10 each.
Holdings that stay are not re-weighted. No stops. Delivery (CNC) charges and the backtest's slippage
(0.10 % for pit100 stocks, 0.25 % otherwise) are charged on every paper fill.

    python3 momentum_paper.py init --capital 100000 [--universe pit100] [--top 10] [--broker angel]
    python3 momentum_paper.py plan [--date 2026-10-30]     after a month-end close, once that day's
                                                           bhavcopy is in: backtest.py swing pit-fetch,
                                                           then swing pit-build
    python3 momentum_paper.py fill [--date 2026-11-02]     on or after the next session's open
    python3 momentum_paper.py status                       holdings marked to the latest close vs Nifty 50
                                                           and NIFTYBEES bought at the first fill's open

Fill prices: that day's open from the cached NSE bhavcopy (official, after ~18:00 IST), else Yahoo's
daily bar (SYMBOL.NS, delayed). Ledger: trader/paper/momentum.json.
"""

import argparse
import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "india-common"))
import nse  # noqa: E402
import pit  # noqa: E402
import swing  # noqa: E402


class PaperError(nse.NSEError):
    pass


def ledger_path():
    return os.path.join(nse.trader_dir(), "paper", "momentum.json")


def load_ledger():
    L = nse.read_json(ledger_path())
    if not L:
        raise PaperError("no paper portfolio yet: run `momentum_paper.py init --capital 100000`")
    return L


def save_ledger(L):
    os.makedirs(os.path.dirname(ledger_path()), exist_ok=True)
    nse.write_json(ledger_path(), L)


def today():
    return nse.now_ist().date().isoformat()


# ================================================================ prices

def bhav_row(d, sym):
    """[symbol, series, open, high, low, close, prev_close, volume, value, isin] from the cached bhavcopy."""
    doc = pit.read_gz(pit.day_path(d))
    if not doc:
        return None
    for r in doc["rows"]:
        if r[0] == sym and r[1] in ("EQ", "BE", "BZ"):
            return r
    return None


def yahoo_chart(sym, rng):
    """(meta, daily bars) from Yahoo; indices (^NSEI) as they are, stocks as SYMBOL.NS."""
    url = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1d" % (
        nse.YAHOO_HOSTS[0], swing.ysym(sym).replace("^", "%5E"), rng)
    return nse.parse_yahoo_chart(nse.http_get(url))


def yahoo_day(sym, d):
    """(open, high, low, close) of Yahoo's daily bar for date d, or None."""
    try:
        _, bars = yahoo_chart(sym, "1mo")
    except nse.NSEError:
        return None
    for b in bars:
        if b["t"].date().isoformat() == d:
            return b["o"], b["h"], b["l"], b["c"]
    return None


def day_bar(sym, d):
    """(open, high, low, close, source) for sym on date d: the bhavcopy, else Yahoo."""
    r = bhav_row(d, sym)
    if r:
        return r[2], r[3], r[4], r[5], "NSE bhavcopy"
    y = yahoo_day(sym, d)
    if y:
        return y + ("Yahoo",)
    return None


def latest_close(sym):
    """(date, close, source): the newest cached bhavcopy close, or Yahoo's last price if newer."""
    sessions = pit.cached_sessions()
    best = None
    for d in reversed(sessions[-5:]):
        r = bhav_row(d, sym)
        if r:
            best = (d, r[5], "NSE bhavcopy")
            break
    try:
        meta, bars = yahoo_chart(sym, "5d")
        if bars:
            yd = bars[-1]["t"].date().isoformat()
            px = meta.get("regularMarketPrice") or bars[-1]["c"]
            if best is None or yd > best[0]:
                best = (yd, float(px), "Yahoo (delayed)")
    except nse.NSEError:
        pass
    return best


# ================================================================ the rule

def pick(L, d):
    """The month-end list and the top N at d's close, from trader/cache/pit_universe.json + pit_daily/."""
    cfg = L["config"]
    U = nse.read_json(pit.universe_path()) or {}
    lists = U.get(cfg["universe"]) or {}
    if d not in lists:
        have = sorted(lists)[-1] if lists else "none"
        raise PaperError("no %s list for %s (latest %s): run `backtest.py swing pit-fetch` and `swing pit-build` "
                         "after that day's bhavcopy is out (~18:00 IST), and check %s is a month-end"
                         % (cfg["universe"], d, have, d))
    a = argparse.Namespace(offline=None)
    uni, _, missing, _, mem, _ = swing.load_pit(cfg["universe"], a, d, d)
    p = {"top": cfg["top"], "look": cfg["look"], "skip": cfg["skip"]}
    top = swing.rotation_pick(uni, d, p, mem)
    stale = sorted(s for s in lists[d] if s in uni and d not in uni[s].idx)
    if len(stale) > len(lists[d]) // 2:
        raise PaperError("the adjusted series stop before %s: rerun `backtest.py swing pit-build`" % d)
    closes = dict((s, uni[s].c[uni[s].idx[d]]) for s in uni if d in uni[s].idx)
    return top, closes, missing


def equity_at(L, closes):
    inv = sum(h["qty"] * closes.get(s, h["last"]) for s, h in L["holdings"].items())
    return L["cash"] + inv, inv


# ================================================================ commands

def cmd_init(a):
    if os.path.exists(ledger_path()) and not a.reset:
        raise PaperError("a paper portfolio already exists at %s (pass --reset to start over)" % ledger_path())
    L = {"created": nse.iso_ist(nse.now_ist().replace(microsecond=0)),
         "rule": swing.STRATEGIES["momentum_rotation"]["rules"],
         "config": {"capital": a.capital, "universe": a.universe, "top": a.top, "look": 252, "skip": 21,
                    "broker": a.broker, "slip_core": swing.DEFAULTS["slip_core"],
                    "slip_other": swing.DEFAULTS["slip_other"]},
         "cash": a.capital, "holdings": {}, "pending": None, "trades": [], "rebalances": [], "bench": None}
    save_ledger(L)
    print("paper portfolio: %s, %s top %d, %s delivery charges; ledger %s"
          % (nse.inr(a.capital), a.universe, a.top, a.broker, ledger_path()))
    return 0


def cmd_plan(a):
    L = load_ledger()
    d = a.date or today()
    if L.get("pending") and not a.force:
        raise PaperError("orders from %s are still pending: run `fill` first (or --force to replace them)"
                         % L["pending"]["signal_date"])
    nxt = nse.next_session(datetime.datetime.combine(datetime.date.fromisoformat(d), datetime.time(23, 59)))
    if nxt is None or nxt[0].date().isoformat()[:7] == d[:7]:
        if not a.force:
            raise PaperError("%s is not the last session of its month (the rule only rebalances then)" % d)
    top, closes, missing = pick(L, d)
    eq, inv = equity_at(L, closes)
    keep = set(s for _, s in top)
    sells = [{"symbol": s, "why": "left the top %d" % L["config"]["top"]} for s in sorted(L["holdings"]) if s not in keep]
    buys = [{"symbol": s, "momentum_pct": round(100 * m, 2), "rank": k + 1,
             "why": "12-1 month momentum %+.1f%%, rank %d, above its 200-day SMA" % (100 * m, k + 1)}
            for k, (m, s) in enumerate(top) if s not in L["holdings"]]
    L["pending"] = {"signal_date": d, "fill_on": nxt[0].date().isoformat() if nxt else None,
                    "equity_at_signal": round(eq, 2), "sells": sells, "buys": buys,
                    "top": [[s, round(100 * m, 2)] for m, s in top]}
    save_ledger(L)
    print("Momentum rotation, %s top %d, signal at the %s close (equity %s)" % (
        L["config"]["universe"], L["config"]["top"], d, nse.inr(eq)))
    print("Fill at the open of %s.\n" % L["pending"]["fill_on"])
    print("  rank  symbol         12-1 mom   close")
    for k, (m, s) in enumerate(top):
        tag = "hold" if s in L["holdings"] else "BUY"
        print("  %4d  %-13s %+8.1f%%  %8.2f  %s" % (k + 1, s, 100 * m, closes.get(s, 0), tag))
    for s in sells:
        print("        %-13s                    SELL (%s)" % (s["symbol"], s["why"]))
    if len(top) < L["config"]["top"]:
        print("\n  only %d stocks qualify (above the 200-day SMA): the rest stays in cash" % len(top))
    if missing:
        print("\n  note: %d list members had no usable series and were not ranked: %s"
              % (len(missing), ", ".join(sorted(missing)[:8])))
    return 0


def cmd_fill(a):
    L = load_ledger()
    P = L.get("pending")
    if not P:
        print("nothing pending")
        return 0
    d = a.date or today()
    if P["fill_on"] and d < P["fill_on"]:
        raise PaperError("these orders fill at the open of %s, not %s" % (P["fill_on"], d))
    cfg = L["config"]
    U = nse.read_json(pit.universe_path()) or {}
    core = set(U.get("pit100", {}).get(P["signal_date"], ()))
    broker, out, left = cfg["broker"], [], []

    def slip(s):
        return (cfg["slip_core"] if s in core else cfg["slip_other"]) / 100.0

    for o in P["sells"]:
        s, h = o["symbol"], L["holdings"].get(o["symbol"])
        if not h:
            continue
        b = day_bar(s, d)
        if not b or b[1] == b[2]:
            left.append(o)
            out.append("  %-13s SELL not filled (%s): retry next session" % (s, "no bar" if not b else "locked at a circuit"))
            continue
        px = b[0] * (1 - slip(s))
        sv = h["qty"] * px
        ch = nse.delivery_charges(0.0, sv, "NSE", broker, dp_debits=1)["total"]
        L["cash"] += sv - ch
        net = sv - ch - h["qty"] * h["fill"] - h["buy_charges"]
        L["trades"].append({"symbol": s, "side": "SELL", "date": d, "qty": h["qty"], "open": b[0], "fill": round(px, 4),
                            "charges": round(ch, 2), "net_pnl": round(net, 2), "entry_date": h["entry_date"],
                            "source": b[4], "why": o["why"]})
        del L["holdings"][s]
        out.append("  %-13s SELL %4d @ %9.2f (open %.2f, %s)  charges %s  P&L %s" % (
            s, h["qty"], px, b[0], b[4], nse.inr(ch), nse.inr(net)))
    for o in P["buys"]:
        s = o["symbol"]
        if s in L["holdings"]:
            continue
        b = day_bar(s, d)
        if not b or b[1] == b[2]:
            out.append("  %-13s BUY skipped (%s), as in the backtest" % (s, "no bar" if not b else "locked at a circuit"))
            continue
        px = b[0] * (1 + slip(s))
        budget = min(P["equity_at_signal"] / cfg["top"], L["cash"])
        qty = int(budget / (px * 1.0015))
        if qty < 1:
            out.append("  %-13s BUY skipped: too little cash for one share" % s)
            continue
        bv = qty * px
        ch = nse.delivery_charges(bv, 0.0, "NSE", broker)["total"]
        L["cash"] -= bv + ch
        L["holdings"][s] = {"qty": qty, "fill": round(px, 4), "entry_date": d, "buy_charges": round(ch, 2),
                            "last": b[3], "why": o["why"]}
        L["trades"].append({"symbol": s, "side": "BUY", "date": d, "qty": qty, "open": b[0], "fill": round(px, 4),
                            "charges": round(ch, 2), "source": b[4], "why": o["why"]})
        out.append("  %-13s BUY  %4d @ %9.2f (open %.2f, %s)  %s + charges %s" % (
            s, qty, px, b[0], b[4], nse.inr(bv), nse.inr(ch)))
    if L.get("bench") is None:
        L["bench"] = {"date": d}
        for sym in (swing.NIFTY, swing.BEES):
            y = yahoo_day(sym, d)
            L["bench"][sym] = y[0] if y else None
    L["rebalances"].append({"signal_date": P["signal_date"], "filled": d, "top": P["top"],
                            "sells": len(P["sells"]) - len(left), "buys": len(P["buys"])})
    L["pending"] = {"signal_date": P["signal_date"], "fill_on": None, "equity_at_signal": P["equity_at_signal"],
                    "sells": left, "buys": [], "top": P["top"]} if left else None
    save_ledger(L)
    print("Paper fills, %s (paper only: nothing was sent to a broker)" % d)
    print("\n".join(out) or "  nothing to do")
    print("  cash left %s" % nse.inr(L["cash"]))
    return 0


def cmd_status(a):
    L = load_ledger()
    cfg = L["config"]
    rows, inv, asof = [], 0.0, None
    for s in sorted(L["holdings"]):
        h = L["holdings"][s]
        lc = latest_close(s)
        if lc:
            h["last"], asof = lc[1], max(asof or lc[0], lc[0])
        v = h["qty"] * h["last"]
        inv += v
        rows.append("  %-13s %4d  bought %9.2f on %s  now %9.2f  %+6.1f%%" % (
            s, h["qty"], h["fill"], h["entry_date"], h["last"], 100 * (h["last"] / h["fill"] - 1)))
    eq = L["cash"] + inv
    save_ledger(L)
    print("Momentum paper portfolio (%s top %d), started with %s" % (cfg["universe"], cfg["top"], nse.inr(cfg["capital"])))
    print("\n".join(rows) or "  no holdings yet")
    print("  cash %s; equity %s (%+.2f%%)%s" % (
        nse.inr(L["cash"]), nse.inr(eq), 100 * (eq / cfg["capital"] - 1), " as of %s" % asof if asof else ""))
    B = L.get("bench") or {}
    for sym in (swing.NIFTY, swing.BEES):
        lc = latest_close(sym) if B.get(sym) else None
        if lc:
            print("  %-10s %+6.2f%% since the open of %s" % (sym, 100 * (lc[1] / B[sym] - 1), B["date"]))
    if L.get("pending"):
        print("  pending: %d sells, %d buys from the %s signal" % (
            len(L["pending"]["sells"]), len(L["pending"]["buys"]), L["pending"]["signal_date"]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Paper portfolio for the monthly momentum rotation (paper only).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--capital", type=float, default=100000.0)
    p.add_argument("--universe", choices=("pit100", "pit500"), default="pit100")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--broker", choices=("angel", "dhan"), default="angel")
    p.add_argument("--reset", action="store_true")
    p = sub.add_parser("plan")
    p.add_argument("--date")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("fill")
    p.add_argument("--date")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    try:
        return {"init": cmd_init, "plan": cmd_plan, "fill": cmd_fill, "status": cmd_status}[a.cmd](a)
    except nse.NSEError as e:
        print("momentum_paper: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
