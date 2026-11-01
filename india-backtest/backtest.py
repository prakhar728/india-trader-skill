#!/usr/bin/env python3
"""Backtester for india-backtest. Standard library only.

Which intraday strategies are available, and how good are they, honestly, after
Indian costs? Runs long-only rules on many NSE stocks' 5-minute bars, sizes
every trade the way india-budget would, takes off the round trip's charges
(nse.charges), and reports R gross and net, rupees, drawdowns and an
out-of-sample walk-forward check. A backtest never places an order.

    python3 backtest.py list
    python3 backtest.py run --strategy orb15 --universe nifty50
    python3 backtest.py run --strategy orb15,pdh --universe RELIANCE,TCS --variants
    python3 backtest.py run --strategy all --universe tata --write
    python3 backtest.py compare --universe nifty50 --days 60 --write
    python3 backtest.py compare --universe TATAPOWER --source angel --days 365
    python3 backtest.py run --strategy orb15 --universe RELIANCE --offline tests/fixtures/common
    python3 backtest.py options fetch|run|validate|compare ...   (options.py: ATM option buying, modelled)
    python3 backtest.py swing fetch|run|compare|list ...          (swing.py: multi-day strategies, delivery costs)

The engine (shared rules with the live pack):
  * A session is 09:15 to flat-by (nse.flat_by(cfg), 15:05 by default). Only
    5-minute bars on the 09:15 grid count; Yahoo drops a bar here and there and
    F&O stocks have no 15:20 bar (NSE's closing auction): a missing bar is
    skipped, a session with fewer than 60 of its 75 bars is left out.
  * Entries: the trigger bar's CLOSE must fall inside the entry window
    (no_entries_before 09:30 to no_entries_after 14:30, never after flat-by).
    One trade per stock per strategy per day.
  * Prices: the stop and the target round DOWN to the stock's tick (the cached
    instrument file's tick when there is one, else NSE's price band). The fill
    is the trigger close + slippage (1 tick by default, --slippage-ticks). The
    target is planned from the trigger close: close + R x (close - stop).
    A stop closer than the larger of 3 ticks and 0.05 % of the price is refused
    (india-exit's rule), as is a size of zero shares.
  * Exits, bar by bar after the entry bar: a bar that OPENS at or below the stop
    fills at its open - slippage (gap through the stop); a bar whose low touches
    the stop fills at the stop - slippage; else a bar whose high reaches the
    target fills at the target (a limit: no slippage, no gap bonus). A bar that
    touches both counts as the stop. Still open at flat-by: out at the close of
    the last bar that ends by flat-by (the 15:00 bar's close at 15:05), minus
    slippage. This is the time machine's history rule, plus slippage.
  * Size: the shares india-budget would buy: the fewest of max_loss_per_trade
    (loss at the stop plus charges), position_cap_pct of capital,
    max_order_value and capital minus the cash buffer.
  * R = the fill minus the stop (rupees a share at risk). Gross R ignores
    charges; net R = (P&L - charges) / (R x shares). Rupees are net.
  * Walk-forward: the first 2/3 of the sessions choose the best variant (by
    average net R, at least 10 trades); the last 1/3 reports only that variant.

Data: Yahoo (60 days of 5-minute bars, plus a year of daily bars for the
prior-day levels and the daily RSI), or Angel One SmartAPI getCandleData
(5-minute bars in chunks, as many days as you ask for, about 1 request a
second, cached and resumable). Bars are cached under trader/cache/bars/ and
merged on every fetch, so the history grows and reruns need no network.

Research holdouts: sessions inside a locked india-research holdout (trader/research/
splits.json, matched by symbol) are withheld by default, before --days / --from / --to
pick the window, with a one-line note on stderr; --allow-holdout includes them and the
output records "holdout_included": true. A holdout measured here is no longer unseen.

Survivorship: the universe is today's index list applied to the past. 60 days
is a small sample. Not financial advice.
"""

import argparse
import bisect
import csv
import datetime
import gzip
import io
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "india-common"))
import nse  # noqa: E402
import holdout  # noqa: E402  (india-common/holdout.py: research holdouts withheld by default)

INTERVAL = 5
OPEN_MIN = 9 * 60 + 15
SLOTS = 75                      # 09:15 ... 15:25
MIN_SESSION_BARS = 60
MIN_TICKS = 3                   # india-exit: a stop must be at least 3 ticks ...
MIN_RISK_PCT = 0.05             # ... and 0.05 % of the price away
MIN_IS_TRADES = 10
SPLIT = 2.0 / 3.0
REQUEST_GAP = 1.05              # seconds between network requests (Angel allows ~3/s; we keep to 1)
ANGEL_CHUNK_DAYS = 90           # calendar days per 5-minute getCandleData request
BACKOFF = (3, 9, 27, 60)
TATA_EXTRA = ("TCS", "TITAN", "TRENT", "VOLTAS")
INDEX_LISTS = {"nifty50": "ind_nifty50list.csv", "nifty100": "ind_nifty100list.csv",
               "nifty500": "ind_nifty500list.csv"}
INDEX_BASE = "https://nsearchives.nseindia.com/content/indices/"
TIME_BUCKETS = (("09:30-10:00", 570, 600), ("10:00-11:00", 600, 660), ("11:00-12:00", 660, 720),
                ("12:00-13:00", 720, 780), ("13:00-14:30", 780, 871))
HIST_EDGES = (-1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0)
DEFAULTS = {"capital": 100000, "cash_buffer_pct": 10, "max_loss_per_trade": 500, "position_cap_pct": 25,
            "max_order_value": 50000, "include_charges": True, "no_entries_before": "09:30",
            "no_entries_after": "14:30", "flat_by": "15:05", "exchange": "NSE"}


class BTError(nse.NSEError):
    pass


def clock(m):
    return "%02d:%02d" % divmod(int(m), 60)


def hm_min(text):
    t = nse.hhmm(text)
    return t.hour * 60 + t.minute


def slot_end(k):
    return OPEN_MIN + INTERVAL * (k + 1)


# ---------------------------------------------------------------- sessions

class Session(object):
    """One stock's regular session: bars[k] for slot k (09:15 + 5k), None when missing."""
    __slots__ = ("symbol", "date", "bars", "n", "prev", "rsi", "tick")

    def __init__(self, symbol, date, bars, prev=None, rsi=None, tick=None):
        self.symbol, self.date, self.bars = symbol, date, bars
        self.n = sum(1 for b in bars if b is not None)
        self.prev, self.rsi, self.tick = prev, rsi, tick


def rsi_series(closes, n=14):
    """Wilder's RSI after each close (None until n+1 closes), as india-scanner computes it."""
    out = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    out[n] = 100 - 100 / (1 + ag / al) if al else 100.0
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
        out[i + 1] = 100 - 100 / (1 + ag / al) if al else 100.0
    return out


def build_sessions(symbol, bars, daily=None, now=None, cfg=None, tick=None):
    """5-minute bars (+ optional daily bars) -> (sessions, notes). Bars on the 09:15 grid
    only, complete by `now`. Prior-day high/low/close and the daily RSI(14) come from the
    daily bars when given, else from the 5-minute sessions themselves."""
    days = {}
    off_grid = 0
    step = datetime.timedelta(minutes=INTERVAL)
    for b in bars:
        t = b["t"]
        mins = t.hour * 60 + t.minute - OPEN_MIN
        if t.second or mins % INTERVAL or mins < 0 or mins >= SLOTS * INTERVAL:
            off_grid += 1
            continue
        if now is not None and t + step > now:
            continue
        days.setdefault(t.date(), {})[mins // INTERVAL] = {
            "o": round(b["o"], 2), "h": round(b["h"], 2), "l": round(b["l"], 2), "c": round(b["c"], 2),
            "v": int(b.get("v") or 0)}
    ctx = {}
    for d, D in days.items():
        ks = sorted(D)
        ctx[d] = (max(D[k]["h"] for k in ks), min(D[k]["l"] for k in ks), D[ks[-1]]["c"])
    for b in daily or []:
        d = b["t"].date()
        if now is not None and (d > now.date() or (d == now.date() and now.time() < nse.CLOSE)):
            continue
        ctx[d] = (round(b["h"], 2), round(b["l"], 2), round(b["c"], 2))
    cdates = sorted(ctx)
    rsis = rsi_series([ctx[d][2] for d in cdates])
    sessions, short, special = [], [], []
    missing = 0
    for d in sorted(days):
        D = days[d]
        if nse.special_session(d, cfg):
            special.append(d.isoformat())
            continue
        if len(D) < MIN_SESSION_BARS:
            short.append({"date": d.isoformat(), "bars": len(D)})
            continue
        missing += SLOTS - len(D)
        i = bisect.bisect_left(cdates, d) - 1
        prev = rsi = None
        if i >= 0:
            pd = cdates[i]
            prev = {"date": pd.isoformat(), "high": ctx[pd][0], "low": ctx[pd][1], "close": ctx[pd][2]}
            rsi = rsis[i]
        sessions.append(Session(symbol, d.isoformat(), [D.get(k) for k in range(SLOTS)], prev, rsi, tick))
    return sessions, {"short": short, "special": special, "missing_bars": missing, "off_grid": off_grid,
                      "daily_rows": len(daily or [])}


# ---------------------------------------------------------------- strategy helpers

class Window(object):
    """The entry window and the last bar before flat-by, in slots."""

    def __init__(self, cfg):
        c = dict(DEFAULTS)
        c.update(cfg or {})
        self.before = hm_min(c["no_entries_before"])
        fb = nse.flat_by(c)
        self.flat = fb.hour * 60 + fb.minute
        self.cutoff = min(hm_min(c["no_entries_after"]), self.flat)
        self.last = (self.flat - OPEN_MIN) // INTERVAL - 1      # 15:05 -> the 15:00 bar (69)

    def ok(self, k, earliest=None):
        end = slot_end(k)
        return max(self.before, earliest or 0) <= end <= self.cutoff


def opening_range(s, n):
    rows = s.bars[:n]
    if any(b is None for b in rows):
        return None
    return max(b["h"] for b in rows), min(b["l"] for b in rows)


def first_close_above(s, level, start, win, earliest=None):
    for k in range(start, win.last + 1):
        b = s.bars[k]
        if b is None:
            continue
        if slot_end(k) > win.cutoff:
            break
        if win.ok(k, earliest) and b["c"] > level:
            return k
    return None


def signal(k, s, stop, r, why):
    return {"k": k, "price": s.bars[k]["c"], "stop": stop, "r": r, "why": why}


# ---------------------------------------------------------------- strategies
# Each is fn(session, params, window) -> signal dict or a short "why not" string.

def strat_orb(s, p, win):
    n = p["or_minutes"] // INTERVAL
    orr = opening_range(s, n)
    if orr is None:
        return "no opening range (a bar is missing)"
    oh, ol = orr
    k = first_close_above(s, oh, n, win)
    if k is None:
        return None
    stop = (oh + ol) / 2.0 if p["stop"] == "or_mid" else ol
    return signal(k, s, stop, p["r"], "close %.2f above the %d-min range high %.2f" % (s.bars[k]["c"], p["or_minutes"], oh))


def strat_pdh(s, p, win):
    if not s.prev:
        return "no prior day"
    pdh = s.prev["high"]
    prev_c = None
    for k in range(0, win.last + 1):
        b = s.bars[k]
        if b is None:
            continue
        if slot_end(k) > win.cutoff:
            break
        crossed = prev_c is not None and prev_c <= pdh < b["c"]
        prev_c = b["c"]
        if crossed and win.ok(k):
            if p["stop"] == "swing":
                lows = [x["l"] for x in s.bars[max(0, k - 2):k + 1] if x is not None]
            else:
                lows = [x["l"] for x in s.bars[:k + 1] if x is not None]
            return signal(k, s, min(lows), p["r"], "close %.2f crossed the prior day's high %.2f" % (b["c"], pdh))
    return None


def strat_gap_go(s, p, win):
    if not s.prev:
        return "no prior day"
    if s.bars[0] is None:
        return "no 09:15 bar"
    pc = s.prev["close"]
    gap = 100.0 * (s.bars[0]["o"] / pc - 1)
    if gap < p["gap"]:
        return None
    orr = opening_range(s, 3)
    if orr is None:
        return "no opening range (a bar is missing)"
    oh, ol = orr
    if ol <= pc:
        return None
    k = first_close_above(s, oh, 3, win)
    if k is None:
        return None
    stop = ol if p["stop"] == "or_low" else (oh + ol) / 2.0
    return signal(k, s, stop, p["r"], "gap %+.1f%%, range held over %.2f, close %.2f above %.2f" % (gap, pc, s.bars[k]["c"], oh))


def strat_vwap_pullback(s, p, win):
    pv = vol = 0.0
    tp_sum, tp_n = 0.0, 0
    above, touch, pb_low = False, None, None
    for k in range(0, win.last + 1):
        b = s.bars[k]
        if b is None:
            continue
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        pv += tp * b["v"]
        vol += b["v"]
        tp_sum += tp
        tp_n += 1
        vw = pv / vol if vol > 0 else tp_sum / tp_n
        end = slot_end(k)
        if end > win.cutoff:
            break
        if end < 600:                      # the trend is judged from 10:00
            continue
        if touch is None:
            if above and b["l"] <= vw:
                touch, pb_low = k, b["l"]
            elif b["c"] > vw:
                above = True
            continue
        pb_low = min(pb_low, b["l"])
        if b["c"] > vw and win.ok(k, 600):
            tick = s.tick or nse.band_tick(b["c"])
            return signal(k, s, pb_low - tick, p["r"],
                          "the bar closing %s touched VWAP; closed back above %.2f (pullback low %.2f)"
                          % (clock(slot_end(touch)), vw, pb_low))
        if k - touch >= 3:
            above, touch, pb_low = b["c"] > vw, None, None
    return None


def strat_orb_pullback(s, p, win):
    orr = opening_range(s, 3)
    if orr is None:
        return "no opening range (a bar is missing)"
    oh, ol = orr
    mid = (oh + ol) / 2.0
    broke, retest, pb_low = None, False, None
    for k in range(3, win.last + 1):
        b = s.bars[k]
        if b is None:
            continue
        if slot_end(k) > win.cutoff:
            break
        if broke is None:
            if b["c"] > oh:
                broke = k
            continue
        if b["c"] < mid:
            return None                      # the breakout failed before any re-entry
        if not retest and b["l"] <= oh:
            retest, pb_low = True, b["l"]
        if retest:
            pb_low = min(pb_low, b["l"])
            if b["c"] > oh and win.ok(k):
                stop = mid if p["stop"] == "or_mid" else pb_low
                return signal(k, s, stop, p["r"], "broke out at %s, pulled back to %.2f, closed back above at %.2f"
                              % (clock(slot_end(broke)), pb_low, b["c"]))
    return None


def strat_oversold(s, p, win):
    if s.rsi is None:
        return "no daily RSI yet (needs 15 daily closes)"
    if s.rsi >= p["rsi"]:
        return None
    orr = opening_range(s, 3)
    if orr is None:
        return "no opening range (a bar is missing)"
    oh, ol = orr
    k = first_close_above(s, oh, 3, win)
    if k is None:
        return None
    stop = ol if p["stop"] == "or_low" else (oh + ol) / 2.0
    return signal(k, s, stop, p["r"], "daily RSI %.1f, close %.2f above the 15-min high %.2f" % (s.rsi, s.bars[k]["c"], oh))


def grid(**axes):
    keys = list(axes)
    out = [{}]
    for k in keys:
        out = [dict(o, **{k: v}) for o in out for v in axes[k]]
    return out


STRATEGIES = {
    "orb15": {
        "fn": strat_orb, "title": "15-minute opening-range breakout",
        "rules": ("Opening range = the high and low of the 09:15, 09:20 and 09:25 bars. Buy the close of the first "
                  "bar that closes above the range high (bar closing 09:35 to 14:30). Stop at the range midpoint "
                  "(variant: range low). Target 2R (variants 1R, 1.5R, 3R)."),
        "default": {"or_minutes": 15, "stop": "or_mid", "r": 2.0},
        "variants": grid(or_minutes=[15], stop=["or_mid", "or_low"], r=[1.0, 1.5, 2.0, 3.0])},
    "orb30": {
        "fn": strat_orb, "title": "30-minute opening-range breakout",
        "rules": ("Opening range = the first six 5-minute bars (09:15 to 09:45). Buy the close of the first bar that "
                  "closes above the range high (closing 09:50 to 14:30). Stop at the range midpoint (variant: range "
                  "low). Target 2R (variants 1R, 1.5R, 3R)."),
        "default": {"or_minutes": 30, "stop": "or_mid", "r": 2.0},
        "variants": grid(or_minutes=[30], stop=["or_mid", "or_low"], r=[1.0, 1.5, 2.0, 3.0])},
    "pdh": {
        "fn": strat_pdh, "title": "previous-day-high breakout",
        "rules": ("Buy the close of the first bar (closing 09:30 to 14:30) that crosses above the previous "
                  "session's high: it closes above it and the bar before closed at or below it (a stock that opens "
                  "above and stays above never crosses). Stop at the lowest low of the trigger bar and the two bars "
                  "before it (variant: the day's low so far). Target 2R (variants 1R, 1.5R, 3R)."),
        "default": {"stop": "swing", "r": 2.0},
        "variants": grid(stop=["swing", "day_low"], r=[1.0, 1.5, 2.0, 3.0])},
    "gap_go": {
        "fn": strat_gap_go, "title": "gap and go",
        "rules": ("The day opens at least 1 % above the previous close (variant 2 %) and the first 15 minutes' low "
                  "stays above the previous close. Buy the close of the first bar that closes above the first "
                  "15 minutes' high (closing 09:35 to 14:30). Stop at the 15-minute low (variant: its midpoint). "
                  "Target 2R (variants 1.5R, 3R)."),
        "default": {"gap": 1.0, "stop": "or_low", "r": 2.0},
        "variants": grid(gap=[1.0, 2.0], stop=["or_low", "or_mid"], r=[1.5, 2.0, 3.0])},
    "vwap_pullback": {
        "fn": strat_vwap_pullback, "title": "VWAP pullback",
        "rules": ("Session VWAP from 09:15 (typical price x volume). From 10:00: once a bar has closed above VWAP, "
                  "wait for a bar whose low touches VWAP; buy the close of the next bar that closes back above VWAP "
                  "(within 3 bars of the touch, else start over), closing 10:00 to 14:30. Stop one tick under the "
                  "pullback's low. Target 2R (variants 1R, 1.5R, 3R)."),
        "default": {"r": 2.0},
        "variants": grid(r=[1.0, 1.5, 2.0, 3.0])},
    "orb_pullback": {
        "fn": strat_orb_pullback, "title": "opening-range breakout, then pullback",
        "rules": ("After a bar closes above the 15-minute range high (the ORB trigger), wait for a bar whose low comes "
                  "back to the range high; buy the close of the first bar from then on that closes above the range "
                  "high (closing 09:30 to 14:30). A close under the range midpoint first cancels the day. Stop at "
                  "the range midpoint (variant: the pullback's low). Target 2R (variants 1.5R, 3R)."),
        "default": {"stop": "or_mid", "r": 2.0},
        "variants": grid(stop=["or_mid", "pb_low"], r=[1.5, 2.0, 3.0])},
    "oversold_bounce": {
        "fn": strat_oversold, "title": "oversold bounce",
        "rules": ("Daily RSI(14, Wilder) under 30 at yesterday's close. Today buy the close of the first bar that "
                  "closes above the first 15 minutes' high (closing 09:35 to 14:30). Stop at the 15-minute low "
                  "(variant: its midpoint). Target 2R (variants 1.5R, 3R)."),
        "default": {"rsi": 30, "stop": "or_low", "r": 2.0},
        "variants": grid(rsi=[30], stop=["or_low", "or_mid"], r=[1.5, 2.0, 3.0])},
}
ORDER = ["orb15", "orb30", "pdh", "gap_go", "vwap_pullback", "orb_pullback", "oversold_bounce"]


def variant_label(name, p):
    d = STRATEGIES[name]["default"]
    parts = []
    for k in sorted(p):
        if k == "or_minutes":
            continue
        parts.append("%s=%g" % (k, p[k]) if isinstance(p[k], (int, float)) else "%s=%s" % (k, p[k]))
    return " ".join(parts) + (" (default)" if p == d else "")


def parse_strategies(text):
    if not text or text == "all":
        return list(ORDER)
    out = []
    for s in text.split(","):
        s = s.strip().lower().replace("-", "_")
        if s not in STRATEGIES:
            raise BTError("unknown strategy %r: pick from %s (or all)" % (s, ", ".join(ORDER)))
        if s not in out:
            out.append(s)
    return out


# ---------------------------------------------------------------- sizing (india-budget's rules)

def shares_for(rupees, per_share):
    r, p = int(round(float(rupees) * 100)), int(round(float(per_share) * 100))
    return r // p if r > 0 and p > 0 else 0


def shares_by_loss(max_loss, entry, stop, exchange="NSE", include=True):
    risk = entry - stop
    if risk <= 0 or max_loss <= 0:
        return 0
    q = shares_for(max_loss, round(risk, 2))
    while q > 0:
        loss = q * round(risk, 2) + (nse.round_trip_cost(q, entry, stop, exchange) if include else 0.0)
        if loss <= max_loss + 1e-9:
            break
        q -= 1
    return q


def size(entry, stop, cfg):
    c = dict(DEFAULTS)
    c.update(cfg or {})
    capital = float(c["capital"])
    limits = {"max_loss_per_trade": shares_by_loss(float(c["max_loss_per_trade"]), entry, stop, c["exchange"],
                                                   bool(c.get("include_charges", True)))}
    if c.get("position_cap_pct"):
        limits["position_cap_pct"] = shares_for(capital * float(c["position_cap_pct"]) / 100.0, entry)
    if c.get("max_order_value"):
        limits["max_order_value"] = shares_for(float(c["max_order_value"]), entry)
    limits["cash"] = shares_for(capital * (1 - float(c.get("cash_buffer_pct") or 0) / 100.0), entry)
    q = min(limits.values())
    return q, [k for k, v in limits.items() if v == q][0]


# ---------------------------------------------------------------- the engine

def simulate(s, sig, win, cfg, slip_ticks=1):
    """A signal -> a trade dict, or (None, why) when it cannot be taken."""
    price = round(sig["price"], 2)
    tick = s.tick or nse.band_tick(price)
    stop = nse.round_down(sig["stop"], tick)
    if stop <= 0 or stop >= price:
        return None, "stop not under the entry"
    if price - stop < max(MIN_TICKS * tick, price * MIN_RISK_PCT / 100.0) - 1e-9:
        return None, "stop too tight (under 3 ticks or 0.05 %)"
    target = nse.round_down(price + sig["r"] * (price - stop), tick)
    if target <= price:
        return None, "target not above the entry"
    slip = round(slip_ticks * tick, 2)
    fill = round(price + slip, 2)
    risk = round(fill - stop, 2)
    qty, decided = size(fill, stop, cfg)
    if qty <= 0:
        return None, "zero shares fit the budget"
    k = sig["k"]
    outcome = exit_px = exit_k = None
    for i in range(k + 1, win.last + 1):
        b = s.bars[i]
        if b is None:
            continue
        if b["o"] <= stop:
            outcome, exit_px, exit_k = "stop_gap", round(b["o"] - slip, 2), i
            break
        if b["l"] <= stop:
            outcome, exit_px, exit_k = "stop", round(stop - slip, 2), i
            break
        if b["h"] >= target:
            outcome, exit_px, exit_k = "target", target, i
            break
    if outcome is None:
        exit_k = max(i for i in range(k, win.last + 1) if s.bars[i] is not None)
        outcome, exit_px = "flat", round(s.bars[exit_k]["c"] - slip, 2)
    exit_px = max(exit_px, 0.01)
    ex = (cfg or {}).get("exchange") or "NSE"
    ch = nse.charges(qty * fill, qty * exit_px, ex)["total"]
    gross = round((exit_px - fill) * qty, 2)
    net = round(gross - ch, 2)
    return {"symbol": s.symbol, "date": s.date, "time": clock(slot_end(k)), "signal": price, "entry": fill,
            "stop": stop, "target": target, "tick": tick, "risk": risk, "qty": qty, "sized_by": decided,
            "exit": exit_px, "exit_time": clock(slot_end(exit_k)) if outcome != "flat" else clock(win.flat),
            "outcome": outcome, "R": round((exit_px - fill) / risk, 4), "R_net": round(net / (risk * qty), 4),
            "pnl": gross, "charges": ch, "pnl_net": net, "why": sig["why"]}, None


def run_symbol(sessions, name, params, win, cfg, slip_ticks=1):
    fn = STRATEGIES[name]["fn"]
    trades, skips = [], {}
    for s in sessions:
        sig = fn(s, params, win)
        if sig is None:
            continue
        if isinstance(sig, str):
            skips[sig] = skips.get(sig, 0) + 1
            continue
        t, why = simulate(s, sig, win, cfg, slip_ticks)
        if t is None:
            skips[why] = skips.get(why, 0) + 1
        else:
            trades.append(t)
    return trades, skips


# ---------------------------------------------------------------- metrics

def _r(x, n=3):
    return None if x is None else round(x, n)


def max_streak(trades):
    best = cur = 0
    for t in trades:
        cur = cur + 1 if t["pnl_net"] <= 0 else 0
        best = max(best, cur)
    return best


def drawdown(values):
    peak = cum = dd = 0.0
    for v in values:
        cum += v
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def histogram(rs):
    labels = ["< %g" % HIST_EDGES[0]] + ["%g to %g" % (a, b) for a, b in zip(HIST_EDGES, HIST_EDGES[1:])] + \
             [">= %g" % HIST_EDGES[-1]]
    counts = [0] * len(labels)
    for r in rs:
        counts[bisect.bisect_right(HIST_EDGES, r)] += 1
    return [{"bucket": l, "lo": (None if i == 0 else HIST_EDGES[i - 1]),
             "hi": (None if i == len(labels) - 1 else HIST_EDGES[i]), "trades": c}
            for i, (l, c) in enumerate(zip(labels, counts))]


def small(trades):
    if not trades:
        return {"trades": 0, "win_pct": None, "avg_R": None, "avg_R_net": None, "expectancy_inr": None,
                "total_inr": 0.0, "total_R_net": 0.0}
    n = len(trades)
    return {"trades": n, "win_pct": round(100.0 * sum(1 for t in trades if t["pnl_net"] > 0) / n, 1),
            "avg_R": _r(sum(t["R"] for t in trades) / n), "avg_R_net": _r(sum(t["R_net"] for t in trades) / n),
            "expectancy_inr": round(sum(t["pnl_net"] for t in trades) / n, 2),
            "total_inr": round(sum(t["pnl_net"] for t in trades), 2),
            "total_R_net": round(sum(t["R_net"] for t in trades), 2)}


def metrics(trades, sessions=None, stock_days=None, detail=True):
    trades = sorted(trades, key=lambda t: (t["date"], t["time"], t["symbol"]))
    m = small(trades)
    n = len(trades)
    if not n:
        m.update(sessions=sessions, stock_days=stock_days)
        return m
    wins = sum(t["pnl_net"] for t in trades if t["pnl_net"] > 0)
    losses = -sum(t["pnl_net"] for t in trades if t["pnl_net"] < 0)
    rn = [t["R_net"] for t in trades]
    sd = statistics.stdev(rn) if n > 1 else None
    days = sorted(set(t["date"] for t in trades))
    m.update({
        "win_pct_gross": round(100.0 * sum(1 for t in trades if t["R"] > 0) / n, 1),
        "profit_factor": round(wins / losses, 2) if losses > 0 else None,
        "profit_factor_gross": (lambda w, l: round(w / l, 2) if l > 0 else None)(
            sum(t["pnl"] for t in trades if t["pnl"] > 0), -sum(t["pnl"] for t in trades if t["pnl"] < 0)),
        "t_stat": round(m["avg_R_net"] / (sd / math.sqrt(n)), 2) if sd else None,
        "total_charges_inr": round(sum(t["charges"] for t in trades), 2),
        "avg_charges_R": _r(sum(t["R"] - t["R_net"] for t in trades) / n),
        "outcomes": dict((o, sum(1 for t in trades if t["outcome"] == o)) for o in ("target", "stop", "stop_gap", "flat")),
        "max_consecutive_losses": max_streak(trades),
        "max_drawdown_R": round(drawdown(rn), 2),
        "max_drawdown_inr": round(drawdown([t["pnl_net"] for t in trades]), 2),
        "sessions": sessions, "stock_days": stock_days, "days_with_trade": len(days),
        "days_with_trade_pct": round(100.0 * len(days) / sessions, 1) if sessions else None,
        "stock_day_fire_pct": round(100.0 * n / stock_days, 1) if stock_days else None,
        "first": trades[0]["date"], "last": trades[-1]["date"],
    })
    if detail:
        cum, cum_inr, curve = 0.0, 0.0, []
        for d in days:
            day = [t for t in trades if t["date"] == d]
            cum += sum(t["R_net"] for t in day)
            cum_inr += sum(t["pnl_net"] for t in day)
            curve.append({"date": d, "cum_R_net": round(cum, 3), "cum_inr": round(cum_inr, 2), "trades": len(day)})
        m["equity_curve"] = curve
        m["histogram_R_net"] = histogram(rn)
        bt = []
        for label, lo, hi in TIME_BUCKETS:
            sel = [t for t in trades if lo <= hm_min(t["time"]) < hi]
            bt.append(dict(small(sel), bucket=label))
        m["by_entry_time"] = bt
        rows = {}
        for t in trades:
            rows.setdefault(t["symbol"], []).append(t)
        m["by_stock"] = sorted((dict(small(v), symbol=k) for k, v in rows.items()),
                               key=lambda r: (-r["avg_R_net"], -r["trades"], r["symbol"]))
    return m


def verdict(full, oos, sessions):
    n = full.get("trades") or 0
    if n == 0:
        return "never fired: nothing to judge"
    g, net = full["avg_R"], full["avg_R_net"]
    o = (oos or {}).get("avg_R_net")
    on = (oos or {}).get("trades") or 0
    if n < 30:
        return "too few trades to judge (%d)" % n
    if net <= 0:
        if g > 0:
            return "no edge after charges: %+.2fR gross becomes %+.2fR net" % (g, net)
        return "no edge, even before charges (%+.2fR gross, %+.2fR net)" % (g, net)
    if o is None or on < 10:
        return "small positive edge (%+.2fR net), but too few out-of-sample trades to confirm" % net
    if o <= 0:
        return "positive in the full sample (%+.2fR net) but it did not hold out of sample (%+.2fR)" % (net, o)
    t = full.get("t_stat") or 0
    if t >= 2 and n >= 200:
        return ("positive edge after charges (%+.2fR net, t=%.1f, held out of sample at %+.2fR); %d sessions is "
                "still a short history" % (net, t, o, sessions or 0))
    return "small positive edge, thin sample (%+.2fR net, t=%.1f; out of sample %+.2fR)" % (net, t, o)


# ---------------------------------------------------------------- backtest over a universe

def split_dates(dates):
    dates = sorted(dates)
    if len(dates) < 3:
        return dates, []
    cut = int(round(len(dates) * SPLIT))
    return dates[:cut], dates[cut:]


def backtest(universe_sessions, names, cfg=None, slip_ticks=1, with_trades=True):
    """{SYMBOL: [Session]} -> the per-strategy results (pure, no I/O)."""
    win = Window(cfg)
    dates = sorted(set(s.date for ss in universe_sessions.values() for s in ss))
    stock_days = sum(len(ss) for ss in universe_sessions.values())
    is_dates, oos_dates = split_dates(dates)
    is_set = set(is_dates)
    sd_is = sum(1 for ss in universe_sessions.values() for s in ss if s.date in is_set)
    out = {}
    for name in names:
        spec = STRATEGIES[name]
        rows, default_trades, skips = [], [], {}
        for p in spec["variants"]:
            tr = []
            for sym in sorted(universe_sessions):
                t, sk = run_symbol(universe_sessions[sym], name, p, win, cfg, slip_ticks)
                tr += t
                if p == spec["default"]:
                    for k, v in sk.items():
                        skips[k] = skips.get(k, 0) + v
            ins = [t for t in tr if t["date"] in is_set]
            oos = [t for t in tr if t["date"] not in is_set]
            rows.append({"label": variant_label(name, p), "params": p, "default": p == spec["default"],
                         "full": metrics(tr, len(dates), stock_days, detail=False),
                         "in_sample": small(ins), "out_of_sample": small(oos), "_oos": oos})
            if p == spec["default"]:
                default_trades = tr
        ok = [r for r in rows if r["in_sample"]["trades"] >= MIN_IS_TRADES]
        chosen = max(ok, key=lambda r: (r["in_sample"]["avg_R_net"], r["in_sample"]["trades"])) if ok else \
            [r for r in rows if r["default"]][0]
        full = metrics(default_trades, len(dates), stock_days)
        oos_m = metrics(chosen["_oos"], len(oos_dates), stock_days - sd_is)
        default_row = [r for r in rows if r["default"]][0]
        wf = {"in_sample": {"from": is_dates[0] if is_dates else None, "to": is_dates[-1] if is_dates else None,
                            "sessions": len(is_dates)},
              "out_of_sample": {"from": oos_dates[0] if oos_dates else None,
                                "to": oos_dates[-1] if oos_dates else None, "sessions": len(oos_dates)},
              "chosen": chosen["label"], "chosen_params": chosen["params"],
              "chosen_by": ("best in-sample average net R with at least %d trades" % MIN_IS_TRADES) if ok else
              "default (no variant had %d in-sample trades)" % MIN_IS_TRADES,
              "chosen_in_sample": chosen["in_sample"], "chosen_out_of_sample": oos_m,
              "default_out_of_sample": default_row["out_of_sample"]}
        for r in rows:
            del r["_oos"]
        res = {"title": spec["title"], "rules": spec["rules"], "default_variant": variant_label(name, spec["default"]),
               "full": full, "variants": rows, "walk_forward": wf, "skipped": skips,
               "verdict": verdict(full, oos_m, len(dates))}
        if with_trades:
            res["trades"] = sorted(default_trades, key=lambda t: (t["date"], t["time"], t["symbol"]))
        out[name] = res
    meta = {"sessions": len(dates), "stock_days": stock_days, "from": dates[0] if dates else None,
            "to": dates[-1] if dates else None, "entry_window": [clock(win.before), clock(win.cutoff)],
            "flat_by": clock(win.flat), "slippage_ticks": slip_ticks,
            "split": {"in_sample": [is_dates[0], is_dates[-1], len(is_dates)] if is_dates else None,
                      "out_of_sample": [oos_dates[0], oos_dates[-1], len(oos_dates)] if oos_dates else None}}
    return out, meta


# ---------------------------------------------------------------- universe

def parse_index_csv(text):
    """An NSE index list CSV -> [(symbol, company name)] for series EQ, plus {symbol: series} dropped."""
    kept, dropped = [], {}
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        sym = (r.get("Symbol") or "").strip()
        if not sym:
            continue
        series = (r.get("Series") or "EQ").strip()
        if series not in nse.INTRADAY_SERIES:
            dropped[sym] = series
            continue
        kept.append((sym, (r.get("Company Name") or "").strip()))
    return kept, dropped


def index_list(which, list_file=None):
    if list_file:
        return nse.read_maybe_gz(list_file).decode("utf-8-sig", "replace")
    name = INDEX_LISTS[which]
    path = os.path.join(nse.trader_dir(), "cache", name)
    try:
        return nse.cached(path, INDEX_BASE + name).decode("utf-8-sig", "replace")
    except nse.NSEError as e:
        raise BTError("could not read NSE's %s list (%s)" % (which, e))


def tata_group(rows):
    """Every Nifty 500 series-EQ stock whose company name says Tata, plus TCS, TITAN, TRENT, VOLTAS."""
    have = dict(rows)
    out = sorted(s for s, n in rows if "tata" in n.lower())
    missing = []
    for s in TATA_EXTRA:
        if s in have and s not in out:
            out.append(s)
        elif s not in have:
            missing.append(s)
    return sorted(out), missing


def resolve_universe(text, list_file=None):
    """-> (name, [symbols], notes)."""
    t = (text or "nifty50").strip()
    low = t.lower()
    notes = []
    if low in INDEX_LISTS:
        kept, dropped = parse_index_csv(index_list(low, list_file))
        if dropped:
            notes.append("left out (not series EQ, no intraday): %s" % ", ".join("%s (%s)" % kv for kv in sorted(dropped.items())))
        return low, [s for s, _ in kept], notes
    if low == "tata":
        kept, _ = parse_index_csv(index_list("nifty500", list_file))
        syms, missing = tata_group(kept)
        if missing:
            notes.append("not in the Nifty 500 series-EQ list: %s" % ", ".join(missing))
        return "tata", syms, notes
    if os.path.isfile(t):
        raw = nse.read_maybe_gz(t).decode("utf-8-sig", "replace")
        if "Symbol" in raw.splitlines()[0]:
            kept, dropped = parse_index_csv(raw)
            return os.path.splitext(os.path.basename(t))[0], [s for s, _ in kept], notes
        syms = []
        for line in raw.replace(",", "\n").splitlines():
            line = line.split("#")[0].strip()
            if line:
                syms.append(nse.clean_symbol(line))
        return os.path.splitext(os.path.basename(t))[0], list(dict.fromkeys(syms)), notes
    try:
        syms = [nse.clean_symbol(x) for x in t.split(",") if x.strip()]
    except nse.NSEError as e:
        raise BTError("--universe wants nifty50, nifty100, nifty500, tata, a file or SYM,SYM (%s)" % e)
    if not syms:
        raise BTError("--universe is empty")
    return ("-".join(syms) if len(syms) <= 3 else "%d-stocks" % len(syms)), list(dict.fromkeys(syms)), notes


# ---------------------------------------------------------------- bar stores and fetching

def safe_get(url, headers=None, timeout=30, data=None, method=None, retries=0):
    """nse.http_get with a usable error on any HTTP status: nse.http_get's own HTTPError branch
    formats its message as `"..." % (tuple).strip()`, which raises AttributeError instead of
    NSEError (seen live on Angel's 403 rate-limit answer). Same headers, same gzip handling."""
    try:
        return nse.http_get(url, headers=headers, timeout=timeout, data=data, method=method, retries=retries)
    except AttributeError:
        h = {"User-Agent": nse.UA, "Accept": "*/*"}
        h.update(headers or {})
        body = None if data is None else (data if isinstance(data, bytes) else json.dumps(data).encode())
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h, method=method),
                                        timeout=timeout) as resp:
                raw = resp.read()
                return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise nse.NSEError(("HTTP %d from %s %s" % (e.code, urllib.parse.urlsplit(url).netloc, detail)).strip())
        except (urllib.error.URLError, OSError) as e:
            raise nse.NSEError("could not reach %s: %s" % (urllib.parse.urlsplit(url).netloc, e))


def _http(url, headers=None):
    """Swapped out in tests."""
    return safe_get(url, headers=headers, timeout=30, retries=0)


def _sleep(sec):
    time.sleep(sec)


_last_request = [0.0]


def polite():
    wait = REQUEST_GAP - (time.time() - _last_request[0])
    if wait > 0:
        _sleep(wait)
    _last_request[0] = time.time()


def bars_dir(source):
    return os.path.join(nse.trader_dir(), "cache", "bars", source)


def store_path(source, sym, interval):
    return os.path.join(bars_dir(source), "%s-%s.json.gz" % (sym, interval))


def read_store(path):
    try:
        with open(path, "rb") as fh:
            doc = json.loads(gzip.decompress(fh.read()))
    except (OSError, ValueError):
        return None
    doc["bars"] = [{"t": datetime.datetime.fromisoformat(r[0]), "o": r[1], "h": r[2], "l": r[3], "c": r[4],
                    "v": r[5]} for r in doc.get("bars") or []]
    return doc


def write_store(path, doc, bars):
    rows = [[b["t"].isoformat(), round(b["o"], 2), round(b["h"], 2), round(b["l"], 2), round(b["c"], 2),
             int(b.get("v") or 0)] for b in sorted(bars, key=lambda b: b["t"])]
    out = dict(doc, bars=rows)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(gzip.compress(json.dumps(out, separators=(",", ":")).encode("utf-8")))
    os.replace(tmp, path)


def merge(old, new):
    d = dict((b["t"], b) for b in old or [])
    for b in new:
        d[b["t"]] = b
    return [d[t] for t in sorted(d)]


def complete_only(bars, now, interval):
    """Drop today's bars while the session is still running (they are incomplete)."""
    if now.time() >= nse.CLOSE:
        return bars
    return [b for b in bars if b["t"].date() < now.date()] if interval == "5m" else \
        [b for b in bars if b["t"].date() < now.date()]


def last_complete_session(now, cfg=None):
    d = now.date()
    if nse.is_trading_day(d, cfg) and now.time() >= nse.CLOSE:
        return d
    return nse.previous_trading_day(d, cfg)


def fresh(doc, now, cfg=None):
    if not doc or not doc.get("fetched"):
        return False
    f = datetime.datetime.fromisoformat(doc["fetched"][:19])
    last = last_complete_session(now, cfg)
    return f >= datetime.datetime.combine(last, nse.CLOSE)


def yahoo_get(sym, rng, interval):
    last = None
    for attempt in range(len(BACKOFF) + 1):
        for host in nse.YAHOO_HOSTS:
            polite()
            try:
                return _http(nse.yahoo_chart_url(sym, rng, interval, host))
            except nse.NSEError as e:
                last = e
                if "HTTP 404" in str(e):
                    raise BTError("Yahoo does not know %s.NS" % sym)
        if attempt < len(BACKOFF):
            print("  Yahoo said %s for %s; waiting %ds" % (str(last)[:60], sym, BACKOFF[attempt]), file=sys.stderr)
            _sleep(BACKOFF[attempt])
    raise BTError("Yahoo did not answer for %s (%s)" % (sym, last))


def load_yahoo(sym, now, cfg=None, refresh=False):
    """-> (5m bars, daily bars, notes) from the cache, refreshed from Yahoo when stale."""
    out, notes = {}, []
    for interval, rng in (("5m", "60d"), ("1d", "1y")):
        path = store_path("yahoo", sym, interval)
        doc = read_store(path)
        if doc and fresh(doc, now, cfg) and not refresh:
            out[interval] = doc["bars"]
            continue
        try:
            meta, bars = nse.parse_yahoo_chart(yahoo_get(sym, rng, interval))
        except nse.NSEError as e:
            if doc:
                notes.append("%s %s: Yahoo failed (%s), used the cache from %s" % (sym, interval, e, doc["fetched"]))
                out[interval] = doc["bars"]
                continue
            if interval == "1d":
                notes.append("%s: no daily bars (%s): prior-day levels come from the 5-minute bars" % (sym, e))
                out[interval] = []
                continue
            raise BTError(str(e))
        if interval == "5m":
            bars = nse.on_grid(bars, INTERVAL)
        bars = complete_only(bars, now, interval)
        allb = merge(doc["bars"] if doc else [], bars)
        write_store(path, {"symbol": sym, "source": "yahoo", "interval": interval, "fetched": nse.iso_ist(now),
                           "first": allb[0]["t"].date().isoformat() if allb else None}, allb)
        out[interval] = allb
    return out["5m"], out["1d"], notes


# Angel One ----------------------------------------------------------

def angel_module():
    try:
        import angel  # noqa: E402  (india-common/angel.py)
    except ImportError:
        raise BTError("india-common/angel.py is missing: copy it next to nse.py to use --source angel")
    return angel


def angel_call(fn, what):
    last = None
    for attempt in range(len(BACKOFF) + 1):
        polite()
        try:
            return fn()
        except nse.NSEError as e:
            last = e
            msg = str(e).lower()
            if "invalid" in msg and "token" in msg and "symbol" in msg:
                raise
            if attempt < len(BACKOFF):
                print("  Angel: %s (%s); waiting %ds" % (str(e)[:80], what, BACKOFF[attempt]), file=sys.stderr)
                _sleep(BACKOFF[attempt])
    raise BTError("Angel kept failing for %s: %s" % (what, last))


def load_angel(sym, now, sessions_wanted, client=None, scrip=None, cfg=None):
    """5-minute bars from Angel's getCandleData in ANGEL_CHUNK_DAYS chunks, newest first, cached
    and resumable; daily bars in one call. Read-only: candles only, never an order route."""
    angel = angel_module()
    scrip = scrip if scrip is not None else angel.scrip_master()
    row = scrip.get(sym)
    if not row:
        raise BTError("%s is not in Angel's NSE equity list" % sym)
    token = row["token"]
    cal_days = int(math.ceil(sessions_wanted * 1.5)) + 20
    want_from = now.date() - datetime.timedelta(days=cal_days)
    last = last_complete_session(now, cfg)
    path = store_path("angel", sym, "5m")
    doc = read_store(path) or {"symbol": sym, "source": "angel", "interval": "5m", "token": token,
                               "covered": None, "bars": []}
    bars = doc["bars"]
    cov = doc.get("covered")
    client = client or angel.Angel.from_env()
    requests = 0

    def fetch(a, b):
        start = datetime.datetime.combine(a, nse.OPEN)
        end = datetime.datetime.combine(b, nse.CLOSE)
        return angel_call(lambda: client.candles(token, start, end, minutes=5), "%s %s..%s" % (sym, a, b))

    def save():
        write_store(path, dict(doc, bars=None, covered=cov, fetched=nse.iso_ist(now), token=token), bars)

    # forward: from the end of the cache (or the start) to the last complete session
    fwd_from = (datetime.date.fromisoformat(cov[1]) + datetime.timedelta(days=1)) if cov else \
        max(want_from, last - datetime.timedelta(days=ANGEL_CHUNK_DAYS - 1))
    a = fwd_from
    while a <= last:
        b = min(last, a + datetime.timedelta(days=ANGEL_CHUNK_DAYS - 1))
        got = complete_only(nse.on_grid(fetch(a, b), INTERVAL), now, "5m")
        requests += 1
        bars = merge(bars, got)
        cov = [min(cov[0], a.isoformat()) if cov else a.isoformat(), b.isoformat()]
        save()
        a = b + datetime.timedelta(days=1)
    # backward: older chunks until want_from (stop when Angel has nothing older)
    while cov and datetime.date.fromisoformat(cov[0]) > want_from:
        b = datetime.date.fromisoformat(cov[0]) - datetime.timedelta(days=1)
        a = max(want_from, b - datetime.timedelta(days=ANGEL_CHUNK_DAYS - 1))
        got = nse.on_grid(fetch(a, b), INTERVAL)
        requests += 1
        bars = merge(bars, got)
        cov = [a.isoformat(), cov[1]]
        save()
        if not got:
            cov = [want_from.isoformat(), cov[1]]
            save()
            break
    # daily bars for the prior-day levels and RSI: one request, refreshed once a session
    dpath = store_path("angel", sym, "1d")
    ddoc = read_store(dpath)
    if not (ddoc and fresh(ddoc, now, cfg)):
        def daily():
            data = client.post("candles", {"exchange": "NSE", "symboltoken": str(token), "interval": "ONE_DAY",
                                           "fromdate": (datetime.datetime.combine(want_from, nse.OPEN)
                                                        - datetime.timedelta(days=60)).strftime("%Y-%m-%d %H:%M"),
                                           "todate": datetime.datetime.combine(last, nse.CLOSE).strftime("%Y-%m-%d %H:%M")})
            return angel.candle_bars(data or [])
        try:
            dbars = angel_call(daily, "%s daily" % sym)
            requests += 1
            dbars = [dict(b, t=datetime.datetime.combine(b["t"].date(), nse.OPEN)) for b in dbars]
            write_store(dpath, {"symbol": sym, "source": "angel", "interval": "1d", "fetched": nse.iso_ist(now)},
                        merge(ddoc["bars"] if ddoc else [], dbars))
            ddoc = read_store(dpath)
        except BTError:
            pass
    return bars, (ddoc or {}).get("bars") or [], {"requests": requests, "covered": cov}


# Offline ------------------------------------------------------------

def _read_any(path):
    raw = nse.read_maybe_gz(path)
    doc = json.loads(raw)
    if isinstance(doc, dict) and "chart" in doc:
        return nse.parse_yahoo_chart(doc)[1]
    if isinstance(doc, dict) and "bars" in doc:
        return [{"t": datetime.datetime.fromisoformat(r[0]), "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]}
                if isinstance(r, list) else dict(r, t=datetime.datetime.fromisoformat(r["t"])) for r in doc["bars"]]
    raise BTError("%s is not a Yahoo chart or a bar store" % path)


def find_offline(dirs, sym, interval):
    names = (["yahoo-%s.NS-60d5m.json.gz", "yahoo-%s.NS-60d5m.json", "%s-60d5m.json.gz", "%s-60d5m.json",
              "%s-5m.json.gz", "%s-5m.json", "%s.json.gz", "%s.json"] if interval == "5m" else
             ["yahoo-%s.NS-1y1d.json.gz", "yahoo-%s.NS-1y1d.json", "%s-1d.json.gz", "%s-1d.json"])
    for d in dirs:
        for n in names:
            p = os.path.join(d, n % sym)
            if os.path.isfile(p):
                return p
    return None


def load_offline(dirs, sym):
    p = find_offline(dirs, sym, "5m")
    if not p:
        raise BTError("no 5-minute bars for %s in %s" % (sym, ", ".join(dirs)))
    d = find_offline(dirs, sym, "1d")
    return _read_any(p), (_read_any(d) if d else []), {"file": p, "daily_file": d}


def cached_tick_table():
    """The exchange's per-stock tick from an instrument file already in trader/cache (never downloads)."""
    for name, key in (("dhan-nse-equity.json", "tick"), ("angel-nse-equity.json", "tick")):
        p = os.path.join(nse.trader_dir(), "cache", name)
        try:
            t = nse.read_json(p)
        except nse.NSEError:
            t = None
        if t:
            return dict((s, r.get(key)) for s, r in t.items() if r.get(key)), name
    return {}, None


# ---------------------------------------------------------------- loading a universe

def load_universe(symbols, a, now, cfg):
    """-> ({SYM: [Session]}, info)."""
    out, info = {}, {"missing": {}, "notes": [], "short_sessions": 0, "missing_bars": 0, "special": [],
                     "requests": 0, "daily_from": {}}
    ticks, tick_src = ({}, None) if a.offline else cached_tick_table()
    info["tick_source"] = tick_src or "NSE price bands"
    dirs = [d for d in (a.offline or "").replace(os.pathsep, ",").split(",") if d]
    client = scrip = None
    for i, sym in enumerate(symbols):
        try:
            if dirs:
                b5, b1, extra = load_offline(dirs, sym)
            elif a.source == "angel":
                if client is None:
                    am = angel_module()
                    scrip = am.scrip_master()
                    client = am.Angel.from_env(transport=safe_get, public=am.public_ip() or "", local=am._local_ip())
                    client.login()
                print("  [%d/%d] %s (Angel)" % (i + 1, len(symbols), sym), file=sys.stderr)
                b5, b1, extra = load_angel(sym, now, a.days, client, scrip, cfg)
                info["requests"] += extra["requests"]
            else:
                if len(symbols) > 1:
                    print("  [%d/%d] %s" % (i + 1, len(symbols), sym), file=sys.stderr)
                b5, b1, notes = load_yahoo(sym, now, cfg, a.refresh)
                info["notes"] += notes
        except nse.NSEError as e:
            msg = str(e)
            if client is not None:
                msg = angel_module().scrub(msg)
            info["missing"][sym] = msg
            continue
        ss, n = build_sessions(sym, b5, b1, now, cfg, ticks.get(sym))
        if not ss:
            info["missing"][sym] = "no full sessions in the data"
            continue
        info["short_sessions"] += len(n["short"])
        info["missing_bars"] += n["missing_bars"]
        info["special"] += n["special"]
        info["daily_from"][sym] = "daily bars" if n["daily_rows"] else "5-minute bars"
        out[sym] = ss
    if client is not None:
        try:
            client.logout()
        except nse.NSEError:
            pass
    return out, info


def restrict(universe_sessions, days=None, date_from=None, date_to=None):
    dates = sorted(set(s.date for ss in universe_sessions.values() for s in ss
                       if (not date_from or s.date >= date_from) and (not date_to or s.date <= date_to)))
    if days:
        dates = dates[-days:]
    keep = set(dates)
    return dict((k, [s for s in v if s.date in keep]) for k, v in universe_sessions.items()
                if any(s.date in keep for s in v))


# ---------------------------------------------------------------- reports

def caveats(meta):
    sp = meta.get("split") or {}
    out = [
        "Sample: %d sessions (%s to %s), %d stock-days. A few dozen sessions can flatter or bury any rule; the "
        "out-of-sample part is only %s sessions." % (meta["sessions"], meta["from"], meta["to"], meta["stock_days"],
                                                      (sp.get("out_of_sample") or [0, 0, 0])[2]),
        "Survivorship: the universe is today's list (%s) applied to the past; stocks that dropped out of it are "
        "missing, and stocks that joined after a big run are in." % meta["universe"],
        "Data: %s 5-minute bars. %d bars were missing inside the sessions used and %d short session(s) were left "
        "out; F&O stocks have no 15:20 bar since NSE's closing auction. Bars are the feed's, not NSE's tape."
        % (meta["source"], meta.get("missing_bars", 0), meta.get("short_sessions", 0)),
        "Fills: entry at the trigger bar's close + %d tick(s), stops and flat-by exits %d tick(s) worse, gaps "
        "through the stop at the open; targets fill exactly. Real fills in a fast market can be worse."
        % (meta["slippage_ticks"], meta["slippage_ticks"]),
        "Charges: %s, on the shares india-budget would size (max loss %s a trade incl. charges, capital %s)."
        % (meta["charges_model"], nse.inr(meta["sizing"]["max_loss_per_trade"], 0), nse.inr(meta["sizing"]["capital"], 0)),
        "Long only, one trade per stock per strategy per day; no news, circuit limits, or liquidity checks.",
        "A backtest is not a forecast and never places an order. Not financial advice.",
    ]
    return out


def fr(x, fmt="%+.2f"):
    return "-" if x is None else fmt % x


def strategy_lines(name, r, show_variants=False):
    f, wf = r["full"], r["walk_forward"]
    o = wf["chosen_out_of_sample"]
    L = ["", "%s: %s  [%s]" % (name, r["title"], r["default_variant"]), "  Rules: " + r["rules"]]
    if not f["trades"]:
        L.append("  Never fired in this period.")
    else:
        L += [
            "  %d trades (%s %% of stock-days; a trade on %d of %d sessions). Win %.1f %% after charges."
            % (f["trades"], f["stock_day_fire_pct"], f["days_with_trade"], f["sessions"], f["win_pct"]),
            "  Average R %s gross, %s net (charges cost %.2fR a trade). Expectancy %s a trade, %s in all. "
            "Profit factor %s net (%s gross)."
            % (fr(f["avg_R"]), fr(f["avg_R_net"]), f["avg_charges_R"], nse.inr(f["expectancy_inr"]),
               nse.inr(f["total_inr"]), fr(f["profit_factor"], "%.2f"), fr(f["profit_factor_gross"], "%.2f")),
            "  Outcomes: %(target)d target, %(stop)d stop, %(stop_gap)d gapped through the stop, %(flat)d out at flat-by."
            % f["outcomes"],
            "  Worst losing streak %d; max drawdown %.1fR / %s."
            % (f["max_consecutive_losses"], f["max_drawdown_R"], nse.inr(f["max_drawdown_inr"])),
            "  By entry time: " + "; ".join("%s n=%d %s" % (b["bucket"], b["trades"], fr(b["avg_R_net"]))
                                            for b in f["by_entry_time"] if b["trades"]),
        ]
        bs = f["by_stock"]
        if len(bs) > 1:
            L.append("  Best stocks: " + ", ".join("%s %s (%d)" % (x["symbol"], fr(x["avg_R_net"]), x["trades"]) for x in bs[:5]))
            L.append("  Worst stocks: " + ", ".join("%s %s (%d)" % (x["symbol"], fr(x["avg_R_net"]), x["trades"])
                                                    for x in list(reversed(bs))[:5]))
    ins, oos = wf["in_sample"], wf["out_of_sample"]
    if oos["sessions"]:
        ci = wf["chosen_in_sample"]
        L.append("  Walk-forward: %s to %s (%d sessions) picked %s (%s net in-sample, %d trades); on %s to %s "
                 "(%d sessions) it made %s net over %d trades (%s a trade). The default made %s there."
                 % (ins["from"], ins["to"], ins["sessions"], wf["chosen"], fr(ci["avg_R_net"]), ci["trades"],
                    oos["from"], oos["to"], oos["sessions"], fr(o.get("avg_R_net")), o["trades"],
                    nse.inr(o["expectancy_inr"]) if o.get("expectancy_inr") is not None else "-",
                    fr(wf["default_out_of_sample"]["avg_R_net"])))
    if show_variants:
        L.append("  %-30s %6s %6s %8s %8s %8s %8s" % ("variant", "trades", "win%", "R gross", "R net", "IS net", "OOS net"))
        for v in r["variants"]:
            vf = v["full"]
            L.append("  %-30s %6d %6s %8s %8s %8s %8s" % (v["label"], vf["trades"], fr(vf.get("win_pct"), "%.1f"),
                                                       fr(vf.get("avg_R")), fr(vf.get("avg_R_net")),
                                                       fr(v["in_sample"]["avg_R_net"]), fr(v["out_of_sample"]["avg_R_net"])))
    if r["skipped"]:
        L.append("  Not taken: " + "; ".join("%s x%d" % kv for kv in sorted(r["skipped"].items(), key=lambda kv: -kv[1])))
    L.append("  Verdict: " + r["verdict"])
    return L


def ranking(results):
    """By out-of-sample average net R; strategies with fewer than 10 out-of-sample trades go last."""
    def key(n):
        o = results[n]["walk_forward"]["chosen_out_of_sample"]
        v = o.get("avg_R_net")
        return ((o.get("trades") or 0) < MIN_IS_TRADES, -(v if v is not None else -99), n)
    return sorted(results, key=key)


def compare_table(results):
    head = "%-16s %6s %6s %8s %8s %10s %5s  %-26s %5s %8s  %s" % (
        "strategy", "trades", "win%", "R gross", "R net", "Rs/trade", "PF", "OOS variant (chosen in-sample)",
        "OOS n", "OOS R", "verdict")
    L = [head, "-" * len(head)]
    for n in ranking(results):
        r = results[n]
        f, o = r["full"], r["walk_forward"]["chosen_out_of_sample"]
        L.append("%-16s %6d %6s %8s %8s %10s %5s  %-26s %5d %8s  %s" % (
            n, f["trades"], fr(f.get("win_pct"), "%.1f"), fr(f.get("avg_R")), fr(f.get("avg_R_net")),
            fr(f.get("expectancy_inr"), "%+.0f"), fr(f.get("profit_factor"), "%.2f"),
            r["walk_forward"]["chosen"].replace(" (default)", "*")[:26], o["trades"], fr(o.get("avg_R_net")),
            r["verdict"]))
    L.append("(R gross/net, win%, Rs/trade and PF: the default variant over the whole period. OOS: the variant the "
             "first 2/3 of sessions picked, on the last 1/3 only; * = default. Sorted by OOS R; under 10 OOS "
             "trades sorts last.)")
    return L


def markdown(doc):
    m = doc["meta"]
    L = ["# Backtest: %s, %s" % (m["universe"], ", ".join(doc["strategies"])), "",
         "%d stocks, %s to %s (%d sessions), source %s, slippage %d tick(s). Made %s." % (
             len(m["symbols"]), m["from"], m["to"], m["sessions"], m["source"], m["slippage_ticks"], doc["made"]), "",
         "| strategy | trades | win % | avg R gross | avg R net | Rs/trade | PF | OOS variant | OOS trades | OOS net R | verdict |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for n in doc["ranking"]:
        r = doc["strategies"][n]
        f, o = r["full"], r["walk_forward"]["chosen_out_of_sample"]
        L.append("| %s | %d | %s | %s | %s | %s | %s | %s | %d | %s | %s |" % (
            n, f["trades"], fr(f.get("win_pct"), "%.1f"), fr(f.get("avg_R")), fr(f.get("avg_R_net")),
            fr(f.get("expectancy_inr"), "%+.0f"), fr(f.get("profit_factor"), "%.2f"), r["walk_forward"]["chosen"],
            o["trades"], fr(o.get("avg_R_net")), r["verdict"]))
    L += ["", "## Rules", ""] + ["- **%s**: %s" % (n, doc["strategies"][n]["rules"]) for n in doc["ranking"]]
    L += ["", "## Caveats", ""] + ["- " + c for c in m["caveats"]]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- commands

def cmd_list(a):
    print("Strategies (long only, 5-minute bars, entries 09:30-14:30, flat by 15:05):")
    for n in ORDER:
        s = STRATEGIES[n]
        print("")
        print("%s: %s" % (n, s["title"]))
        print("  " + s["rules"])
        print("  Default: %s. %d variants for the walk-forward: %s." % (
            variant_label(n, s["default"]).replace(" (default)", ""), len(s["variants"]),
            "; ".join(variant_label(n, v).replace(" (default)", "") for v in s["variants"])))
    print("")
    print("Add one: write fn(session, params, window) -> signal and register it in STRATEGIES.")
    return 0


def compute(a, names, command):
    now = nse.parse_now(a.now) if a.now else nse.now_ist().replace(microsecond=0)
    try:
        cfg = dict(DEFAULTS)
        cfg.update(nse.load_config(a.config))
    except nse.NSEError as e:
        raise BTError(str(e))
    if a.capital:
        cfg["capital"] = a.capital
    if a.max_loss:
        cfg["max_loss_per_trade"] = a.max_loss
    uname, symbols, notes = resolve_universe(a.universe, a.list_file)
    if a.source == "yahoo" and not a.offline and a.days > 60:
        notes.append("Yahoo keeps 60 days of 5-minute bars: more than 60 sessions come only from what the cache "
                     "has gathered on earlier runs (or use --source angel)")
    data, info = load_universe(symbols, a, now, cfg)
    # Sessions inside a locked india-research holdout (trader/research/splits.json) are withheld
    # BEFORE --days/--from/--to pick the window, unless --allow-holdout: measuring a rule there
    # would spend the holdout outside the research pipeline.
    data, held = holdout.withhold(data, allow=getattr(a, "allow_holdout", False))
    line = holdout.note(held)
    if line:
        print(line, file=sys.stderr)
        notes.append(line)
    data = restrict(data, a.days, a.date_from, a.date_to)
    if not data:
        raise BTError("no bars for any of %s%s%s" % (", ".join(symbols[:10]), (": " + "; ".join(
            "%s: %s" % kv for kv in list(info["missing"].items())[:5])) if info["missing"] else "",
            (" (%d session(s) withheld as research holdout; --allow-holdout includes them)" % held["withheld_sessions"])
            if held["withheld_sessions"] else ""))
    results, meta = backtest(data, names, cfg, a.slippage_ticks, with_trades=True)
    source = "offline files" if a.offline else ("Angel One SmartAPI" if a.source == "angel" else "Yahoo (.NS)")
    meta.update({"universe": uname, "symbols": sorted(data), "symbols_requested": len(symbols),
                 "symbols_missing": info["missing"], "source": source,
                 "charges_model": "nse.charges: Dhan's intraday equity schedule (brokerage Rs 20 or 0.03 % an order, "
                                  "STT 0.025 % on the sell, NSE txn 0.0030699 %, SEBI, stamp 0.003 % on the buy, GST 18 %)",
                 "sizing": dict((k, cfg.get(k)) for k in ("capital", "cash_buffer_pct", "max_loss_per_trade",
                                                         "position_cap_pct", "max_order_value", "include_charges")),
                 "tick_source": info["tick_source"], "short_sessions": info["short_sessions"],
                 "missing_bars": info["missing_bars"], "special_sessions_left_out": sorted(set(info["special"])),
                 "prior_day_from": info["daily_from"], "notes": notes + info["notes"],
                 "network_requests": info["requests"], "now": nse.iso_ist(now),
                 "holdout_included": held["holdout_included"], "holdout_withheld": held})
    meta["caveats"] = caveats(meta)
    return {"created_by": "india-backtest", "command": command, "made": nse.iso_ist(now), "meta": meta,
            "strategies": results, "ranking": ranking(results)}


def write_out(doc, name):
    d = os.path.join(nse.trader_dir(), "backtest")
    if not name:
        tag = "all" if set(doc["strategies"]) == set(ORDER) else "-".join(doc["strategies"])
        name = "%s-%s-%s" % (doc["made"][:10], doc["meta"]["universe"], tag)
        if doc["meta"]["source"].startswith("Angel"):
            name += "-angel"
    path = os.path.join(d, name + ".json")
    nse.write_json(path, doc)
    with open(os.path.join(d, name + ".md"), "w", encoding="utf-8") as fh:
        fh.write(markdown(doc))
    return path


def header(doc):
    m = doc["meta"]
    L = ["Backtest: %s, %d stock(s), %s to %s (%d sessions), source %s, slippage %d tick(s), entries %s-%s, flat by %s."
         % (m["universe"], len(m["symbols"]), m["from"], m["to"], m["sessions"], m["source"], m["slippage_ticks"],
            m["entry_window"][0], m["entry_window"][1], m["flat_by"]),
         "Sized like india-budget: capital %s, max loss %s a trade incl. charges. Walk-forward: in-sample %s to %s, "
         "out-of-sample %s to %s." % (nse.inr(m["sizing"]["capital"], 0), nse.inr(m["sizing"]["max_loss_per_trade"], 0),
                                      *(m["split"]["in_sample"][:2] + m["split"]["out_of_sample"][:2]
                                        if m["split"]["out_of_sample"] else ["-"] * 4))]
    if m["symbols_missing"]:
        L.append("No data for %d symbol(s): %s" % (len(m["symbols_missing"]), "; ".join(
            "%s (%s)" % (k, v[:60]) for k, v in sorted(m["symbols_missing"].items())[:8])))
    for n in m["notes"][:6]:
        L.append("Note: " + n)
    return L


def emit(doc, a, lines):
    path = write_out(doc, a.write or None) if a.write is not None else None
    if a.json:
        print(json.dumps(doc, indent=1, default=str))
        return
    print("\n".join(lines))
    print("")
    print("Caveats:")
    for c in doc["meta"]["caveats"]:
        print("  - " + c)
    if path:
        print("Wrote %s (and the .md summary beside it)." % path)


def cmd_run(a):
    names = parse_strategies(a.strategy)
    doc = compute(a, names, "run")
    lines = header(doc)
    for n in names:
        lines += strategy_lines(n, doc["strategies"][n], a.variants)
    if len(names) > 1:
        lines += [""] + compare_table(doc["strategies"])
    emit(doc, a, lines)
    return 0


def cmd_compare(a):
    names = parse_strategies(a.strategy or "all")
    doc = compute(a, names, "compare")
    lines = header(doc) + [""] + compare_table(doc["strategies"])
    if a.variants:
        for n in doc["ranking"]:
            lines += strategy_lines(n, doc["strategies"][n], True)
    emit(doc, a, lines)
    return 0


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if args[:1] == ["swing"]:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import swing  # noqa: E402  (india-backtest/swing.py: multi-day strategies on daily bars)
        return swing.main(args[1:])
    if args[:1] == ["options"]:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import options  # noqa: E402  (india-backtest/options.py: the options backtest)
        return options.main(args[1:])
    ap = argparse.ArgumentParser(description="Backtest intraday strategies on NSE stocks (india-backtest). "
                                             "Never places an order.")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list", help="the strategies and their rules")

    def common(p):
        p.add_argument("--universe", default="nifty50",
                       help="nifty50 | nifty100 | nifty500 | tata | FILE | SYM,SYM (default nifty50)")
        p.add_argument("--days", type=int, default=60, help="the last N sessions (default 60)")
        p.add_argument("--source", default="yahoo", choices=["yahoo", "angel"])
        p.add_argument("--offline", metavar="DIR[,DIR]", help="read saved bars from these folders, no network")
        p.add_argument("--list-file", help="an NSE index list CSV to use instead of downloading it")
        p.add_argument("--from", dest="date_from", help="first session, YYYY-MM-DD")
        p.add_argument("--to", dest="date_to", help="last session, YYYY-MM-DD")
        p.add_argument("--slippage-ticks", type=int, default=1)
        p.add_argument("--capital", type=float)
        p.add_argument("--max-loss", type=float, help="max loss per trade in rupees (default config, 500)")
        p.add_argument("--config", help="default trader/config.json (TRADER_DIR)")
        p.add_argument("--refresh", action="store_true", help="refetch even when the cache is fresh")
        p.add_argument("--variants", action="store_true", help="print every parameter variant")
        p.add_argument("--json", action="store_true")
        p.add_argument("--write", nargs="?", const="", metavar="NAME",
                       help="save trader/backtest/<NAME>.json and .md (default <date>-<universe>-<strategies>)")
        p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-10-29 16:00"')
        p.add_argument("--allow-holdout", action="store_true",
                       help="include sessions inside india-research's locked holdouts (withheld by default; "
                            "recorded as holdout_included in the output)")

    p = sub.add_parser("run", help="backtest one or more strategies")
    p.add_argument("--strategy", default="orb15", help="orb15[,pdh,...] or all")
    common(p)
    p = sub.add_parser("compare", help="all strategies on one universe and period, ranked out of sample")
    p.add_argument("--strategy", default="all")
    common(p)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    if getattr(a, "days", None) is not None and a.days < 3:
        print("backtest.py: --days must be at least 3", file=sys.stderr)
        return 1
    if getattr(a, "slippage_ticks", 0) < 0:
        print("backtest.py: --slippage-ticks cannot be negative", file=sys.stderr)
        return 1
    try:
        return {"list": cmd_list, "run": cmd_run, "compare": cmd_compare}[a.cmd](a)
    except nse.NSEError as e:
        print("backtest.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
