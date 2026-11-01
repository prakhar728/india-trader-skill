#!/usr/bin/env python3
"""Swing (multi-day) strategy backtester for india-backtest (run as `backtest.py swing ...`).
Standard library only.

The question it answers, honestly: after Indian DELIVERY costs, and against simply holding a
Nifty index fund, do these swing strategies work on NSE over 10+ years?

    python3 backtest.py swing list
    python3 backtest.py swing fetch --universe nifty100 [--years 12]
    python3 backtest.py swing run --strategy pullback_rsi2 [--variants] [--universe nifty100]
    python3 backtest.py swing run --strategy all --sizing risk --broker angel
    python3 backtest.py swing compare --universe nifty100        (writes the JSON, .md and dash JSON)

Data. Yahoo's chart JSON, one symbol at a time (`SYMBOL.NS`, interval 1d, period1/period2:
range=max only returns monthly bars), about 0.6 s apart, backing off on HTTP 429, cached per
symbol in trader/cache/daily/ (refreshed once a session; an interrupted fetch resumes). Yahoo's
daily OHLC and volume are split- and bonus-ADJUSTED (checked: RELIANCE's 1:1 bonus, ex 2024-10-28,
shows 1,327.85 for 2024-10-25, not the 2,655 it traded at), but NOT dividend-adjusted: every
stock return here is a PRICE return, dividends received while holding are left out. Benchmarks:
the Nifty 50 price index (^NSEI, no dividends: like for like with the strategies) and NIFTYBEES
(Nippon's Nifty 50 ETF, what a person can actually buy; Yahoo lists no distributions in the period,
so its price carries the reinvested dividends, less the fund's expenses: about 1.2 % a year more
than the price index since 2020). A short run of bars (1 to 3) that jumps more than 40 % away and
straight back (seen: NIFTYBEES 2019-12-19/20 at a tenth of its price) is dropped as a bad print. A
stock that OPENS 35 % or more away from its last close (impossible under NSE's price bands: it is a
demerger such as Tata Motors -> TMPV on 2025-10-14 or Vedanta on 2026-04-30, which Yahoo does not
adjust) has its earlier prices back-adjusted by that gap, as if the value split off was kept.
Smaller demergers (ITC Hotels, 2025-01-06, about 2 %) stay unadjusted, a small loss on that day.

Rules of the engine (no look-ahead):
  * Signals are computed on a day's CLOSE from data up to that close. Entries fill at the NEXT
    session's OPEN plus slippage (0.10 % a side for Nifty 100 stocks, 0.25 % for the rest).
  * Stops (when a strategy has one) are checked on each day's LOW from the entry day on; an open
    at or under the stop (a gap through it) fills at that open minus slippage, else the stop minus
    slippage. Targets are limits: the target exactly (or the open if it opens above). A day that
    touches both counts as the stop. An entry whose open is already at or under its stop is
    skipped. Close-based exits (and time exits) fill at the next open minus slippage.
  * A day with no range (high = low: locked at its upper or lower circuit) is untradeable: no
    entry fills (the signal is dropped), exits and stops wait for the next day with a range.
  * Portfolio: Rs 1,00,000 (--capital), at most 10 positions (--max-positions), one position per
    stock, compounding. Equal weight = the last close's equity / max positions (default), or risk
    sizing (--sizing risk): 1 % of equity (--risk-pct) at risk to the initial stop (strategies
    without a stop use a notional 2 x ATR(14) under the signal close for sizing only), capped at
    the equal weight. Cash earns 0. When more signals than free slots, the strategy's score ranks
    them (each strategy documents its score); ties by symbol.
  * Costs: nse.delivery_charges (Dhan by default, --broker angel): STT 0.1 % both sides, NSE txn,
    SEBI, stamp 0.015 % on the buy, GST, DP charge once per scrip per sell day; today's schedule
    applied to the whole history.
  * Walk-forward: every variant runs over the whole period; the variant with the best in-sample
    (2015-2021) Sharpe and at least 30 in-sample trades is chosen and only its 2022-to-today
    result is reported as out of sample (if the data starts after 2016 the split is 2/3 : 1/3).
  * Regimes: each day takes the Nifty 50's state at the previous close: up (above its 200-day
    SMA, the SMA higher than 20 sessions earlier), down (below it, the SMA lower), else sideways;
    and India VIX at the previous close >= 20 (high) or under 20 (normal).

Survivorship: with --universe nifty100 / nifty500 the universe is TODAY's index list applied to the
past. Stocks that fell out of the Nifty 100 / 500 (and the ones that collapsed) are missing; stocks
that joined after a big run are in. Over 10+ years this flatters every long strategy, momentum and
breakouts most. --universe pit100 / pit500 removes that bias (pit.py): at each month-end the top 100 /
500 series-EQ stocks by 6-month median traded value from NSE's own daily bhavcopies, delisted and merged
names included, prices back-adjusted with NSE's corporate-action list. A stock can only be BOUGHT on a
signal date when it is in that month's list (rotation ranks members only); exits are always allowed; a
held stock whose bars end (delisted, merged) is sold at its last close (optimistic for failures).
compare --universe pit100 also writes the side-by-side against that day's nifty100 run (sw_bias).

    python3 backtest.py swing pit-fetch            (NSE CM bhavcopies 2013-11 .. today, ~1 s a day, resumable)
    python3 backtest.py swing pit-fetch --ca       (NSE's corporate-action Bc files, every 3rd session)
    python3 backtest.py swing pit-build            (trader/cache/pit_universe.json + cache/pit_daily/)
    python3 backtest.py swing compare --universe pit100      (then pit500)

Taxes are not modelled (STCG / LTCG apply). A backtest is not a forecast and never places an
order. Not financial advice.
"""

import argparse
import bisect
import datetime
import json
import math
import os
import statistics
import sys
import time
import urllib.parse
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "india-common"))
sys.path.insert(0, HERE)
import nse  # noqa: E402
import backtest as bt  # noqa: E402

NIFTY, BEES, VIX = "^NSEI", "NIFTYBEES", "^INDIAVIX"
BENCH = (("bench_nifty50", NIFTY), ("bench_niftybees", BEES))
YEARS = 12
WARMUP_DAYS = 420               # calendar days fetched before the simulation starts (200/252-day indicators)
IS_END = "2021-12-31"
MIN_IS_TRADES = 30
MIN_OOS_TRADES = 10
SPLIT = 2.0 / 3.0
REQUEST_GAP = 0.6               # seconds between Yahoo requests
BACKOFF = (5, 15, 45, 90)       # waits on 429 / errors
SPIKE = 0.40                    # a 1-3 bar jump of 40 % that reverts = a bad print
BREAK = 0.35                    # an OPEN 35 % away from the last close: impossible under NSE's price bands,
                                # so a demerger or an unadjusted split; earlier prices are back-adjusted
VIX_HIGH = 20.0
TREND_SLOPE = 20                # sessions for the 200-day SMA's slope
DASH_BYTES = 950000
DEFAULTS = {"capital": 100000.0, "max_positions": 10, "sizing": "equal", "risk_pct": 1.0, "broker": "dhan",
            "slip_core": 0.10, "slip_other": 0.25, "costs": True, "exchange": "NSE"}


class SwingError(nse.NSEError):
    pass


def _r(x, n=2):
    return None if x is None else round(x, n)


def as_date(s):
    return datetime.date.fromisoformat(s[:10])


# ================================================================ data: Yahoo daily bars

def ysym(sym):
    return sym if sym.startswith("^") else nse.yahoo_symbol(sym)


def stem(sym):
    return sym.replace("^", "IDX_")


def daily_dir():
    return os.path.join(nse.trader_dir(), "cache", "daily")


def store_path(sym):
    return os.path.join(daily_dir(), stem(sym) + ".json.gz")


def chart_url(sym, p1, p2, host=nse.YAHOO_HOSTS[0]):
    return ("https://%s.finance.yahoo.com/v8/finance/chart/%s?period1=%d&period2=%d&interval=1d"
            "&events=div%%2Csplits" % (host, urllib.parse.quote(ysym(sym)), p1, p2))


def _http(url):
    """Swapped out in tests."""
    return bt.safe_get(url, timeout=40, retries=0)


def _sleep(sec):
    time.sleep(sec)


_last = [0.0]


def polite():
    wait = REQUEST_GAP - (time.time() - _last[0])
    if wait > 0:
        _sleep(wait)
    _last[0] = time.time()


def yahoo_daily(sym, p1, p2, log=None):
    """Raw chart JSON bytes, polite, backing off on 429 and errors; a 404 is final."""
    last = None
    for attempt in range(len(BACKOFF) + 1):
        for host in nse.YAHOO_HOSTS:
            polite()
            try:
                return _http(chart_url(sym, p1, p2, host))
            except nse.NSEError as e:
                last = e
                if "HTTP 404" in str(e):
                    raise SwingError("Yahoo does not know %s" % ysym(sym))
        if attempt < len(BACKOFF):
            if log:
                log("  Yahoo said %s for %s; waiting %ds" % (str(last)[:60], sym, BACKOFF[attempt]))
            _sleep(BACKOFF[attempt])
    raise SwingError("Yahoo did not answer for %s (%s)" % (sym, last))


def rows_from_chart(raw):
    """Yahoo chart JSON -> ([[date, o, h, l, c, v]], events) one row a day (the last wins)."""
    doc = json.loads(raw) if isinstance(raw, (bytes, str)) else raw
    _, bars = nse.parse_yahoo_chart(doc)
    res = ((doc.get("chart") or {}).get("result") or [{}])[0]
    ev = res.get("events") or {}
    splits = sorted((nse.ist_from_epoch(int(k)).date().isoformat(), v.get("splitRatio"))
                    for k, v in (ev.get("splits") or {}).items())
    out = {}
    for b in bars:
        d = b["t"].date().isoformat()
        out[d] = [d, round(b["o"], 4), round(b["h"], 4), round(b["l"], 4), round(b["c"], 4), int(b["v"] or 0)]
    return [out[d] for d in sorted(out)], {"splits": splits, "dividends": len(ev.get("dividends") or {})}


def clean_rows(rows, index=False, breaks=True):
    """Drop bad bars -> (rows, notes). Non-positive prices go; high/low are widened to cover the
    open and close; a stock's zero-volume bar with o=h=l=c (Yahoo's filler on a closed day) goes;
    a run of 1-3 bars more than 40 % away from the bar before that comes straight back goes.
    Corporate-action breaks: a stock that OPENS 35 % or more away from the previous close cannot
    have traded there (NSE's price bands and F&O dynamic bands stop it), so it is a demerger
    (Tata Motors -> TMPV 2025-10-14, Vedanta 2026-04-30) or a split Yahoo did not adjust; every
    earlier OHLC is multiplied by open / previous close, as if the value split off was kept. When
    the median volume of the 20 sessions after jumps by more than 0.7 / factor (a split that
    multiplied the share count), earlier volumes are divided by the factor too. breaks=False skips that
    (the bhavcopy PIT series are already adjusted from NSE's corporate-action list, and a real 35 % gap,
    such as a relisting after a suspension, must stay a loss)."""
    notes = {"fixed_high_low": 0, "filler": 0, "bad_prints": [], "big_moves": []}
    keep = []
    for r in rows:
        d, o, h, l, c, v = r
        if min(o, h, l, c) <= 0:
            continue
        if not index and v == 0 and o == h == l == c:
            notes["filler"] += 1
            continue
        if h < max(o, c) or l > min(o, c):
            notes["fixed_high_low"] += 1
            h, l = max(h, o, c), min(l, o, c)
        keep.append([d, o, h, l, c, v])
    out = []
    i = 0
    while i < len(keep):
        if not out:
            out.append(keep[i])
            i += 1
            continue
        prev = out[-1][4]
        jump = keep[i][4] / prev - 1
        if abs(jump) > SPIKE:
            back = None
            for j in range(i + 1, min(i + 4, len(keep))):
                if abs(keep[j][4] / prev - 1) < 0.2 and abs(keep[j][4] / keep[j - 1][4] - 1) > SPIKE:
                    back = j
                    break
            if back is not None:
                notes["bad_prints"] += [keep[x][0] for x in range(i, back)]
                i = back
                continue
            notes["big_moves"].append([keep[i][0], round(100 * jump, 1)])
        out.append(keep[i])
        i += 1
    notes["breaks"] = []
    if not index and breaks:
        for k in range(len(out) - 1, 0, -1):
            f = out[k][1] / out[k - 1][4]
            if abs(f - 1) >= BREAK:
                kind = "demerger or other"
                before = sorted(r[5] for r in out[max(0, k - 20):k])
                after = sorted(r[5] for r in out[k:k + 20])
                vb, va = (before[len(before) // 2] if before else 0), (after[len(after) // 2] if after else 0)
                split = f < 1 and 1 / f >= 1.9 and vb > 0 and va / vb > 0.7 / f
                if split:
                    kind = "split (volume x%.2f too)" % (1 / f)
                notes["breaks"].append([out[k][0], round(f, 4), kind])
                for j in range(k):
                    vol = int(round(out[j][5] / f)) if split else out[j][5]
                    out[j] = [out[j][0]] + [round(x * f, 4) for x in out[j][1:5]] + [vol]
        notes["breaks"].reverse()
    return out, notes


def fetch_from(now, years):
    return datetime.date(now.year - years + 1, 1, 1) - datetime.timedelta(days=WARMUP_DAYS)


def fetch_symbol(sym, now, years, refresh=False, log=None):
    """Cache one symbol's daily bars. -> 'cached' | 'fetched'. Resumable: a fresh cache is kept."""
    path = store_path(sym)
    doc = read_store(path)
    want = fetch_from(now, years).isoformat()
    if doc and not refresh and bt.fresh(doc, now) and doc.get("from", "9999") <= want:
        return "cached", doc
    p1 = nse.epoch_from_ist(datetime.datetime.combine(as_date(want), datetime.time(0, 0)))
    p2 = nse.epoch_from_ist(now)
    raw = yahoo_daily(sym, p1, p2, log)
    rows, ev = rows_from_chart(raw)
    if now.time() < nse.CLOSE:
        rows = [r for r in rows if r[0] < now.date().isoformat()]
    if not rows:
        raise SwingError("Yahoo sent no daily bars for %s" % ysym(sym))
    doc = {"symbol": sym, "yahoo": ysym(sym), "source": "yahoo chart 1d (split-adjusted OHLC, not dividend-adjusted)",
           "fetched": nse.iso_ist(now), "from": want, "first": rows[0][0], "last": rows[-1][0],
           "splits": ev["splits"], "dividends": ev["dividends"], "bars": rows}
    write_store(path, doc)
    return "fetched", doc


def write_store(path, doc):
    import gzip
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(gzip.compress(json.dumps(doc, separators=(",", ":")).encode("utf-8")))
    os.replace(tmp, path)


def read_store(path):
    try:
        return json.loads(nse.read_maybe_gz(path))
    except (OSError, ValueError):
        return None


def offline_rows(dirs, sym):
    """A recorded Yahoo chart (yahoo-RELIANCE.NS-1d.json.gz, yahoo-NSEI-1d.json.gz) or a cache doc."""
    names = [n % (ysym(sym).lstrip("^")) for n in ("yahoo-%s-1d.json.gz", "yahoo-%s-1d.json")] + \
            [stem(sym) + ".json.gz"]
    for d in dirs:
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                doc = json.loads(nse.read_maybe_gz(p))
                if "chart" in doc:
                    return rows_from_chart(doc)[0]
                return doc["bars"]
    return None


# ================================================================ indicators

def sma(xs, n):
    out = [None] * len(xs)
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rsi(closes, n):
    """Wilder's RSI (the same as backtest.rsi_series and india-scanner)."""
    return bt.rsi_series(closes, n)


def atr(h, l, c, n=14):
    out = [None] * len(c)
    if len(c) < n + 1:
        return out
    tr = [None] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    a = sum(tr[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(c)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def prior_extreme(xs, n, highest=True):
    """out[i] = max (or min) of xs[i-n .. i-1], the n bars BEFORE i (None until there are n)."""
    out = [None] * len(xs)
    dq = deque()
    for i in range(len(xs)):
        if i >= n:
            while dq[0] < i - n:
                dq.popleft()
            out[i] = xs[dq[0]]
        while dq and ((xs[dq[-1]] <= xs[i]) if highest else (xs[dq[-1]] >= xs[i])):
            dq.pop()
        dq.append(i)
    return out


def prior_mean(xs, n):
    """out[i] = mean of xs[i-n .. i-1]."""
    out = [None] * len(xs)
    cs = [0.0]
    for x in xs:
        cs.append(cs[-1] + x)
    for i in range(n, len(xs)):
        out[i] = (cs[i] - cs[i - n]) / n
    return out


def momentum(c, look, skip):
    """out[i] = c[i-skip] / c[i-look] - 1 (12-1 month: look 252, skip 21)."""
    out = [None] * len(c)
    for i in range(look, len(c)):
        out[i] = c[i - skip] / c[i - look] - 1
    return out


class Series(object):
    """One symbol's daily bars, oldest first, with memoised indicators."""

    def __init__(self, sym, rows, core=True):
        self.sym, self.core = sym, core
        self.dates = [r[0] for r in rows]
        self.o = [float(r[1]) for r in rows]
        self.h = [float(r[2]) for r in rows]
        self.l = [float(r[3]) for r in rows]
        self.c = [float(r[4]) for r in rows]
        self.v = [float(r[5]) for r in rows]
        self.idx = dict((d, i) for i, d in enumerate(self.dates))
        self._m = {}

    def ind(self, name, *a):
        key = (name,) + a
        if key not in self._m:
            if name == "sma":
                self._m[key] = sma(self.c, a[0])
            elif name == "rsi":
                self._m[key] = rsi(self.c, a[0])
            elif name == "atr":
                self._m[key] = atr(self.h, self.l, self.c, a[0])
            elif name == "hi_close":
                self._m[key] = prior_extreme(self.c, a[0], True)
            elif name == "lo_low":
                self._m[key] = prior_extreme(self.l, a[0], False)
            elif name == "vol_avg":
                self._m[key] = prior_mean(self.v, a[0])
            elif name == "mom":
                self._m[key] = momentum(self.c, a[0], a[1])
            else:
                raise SwingError("unknown indicator %s" % name)
        return self._m[key]

    def close_on(self, d):
        """The last close on or before date d (None before the first bar)."""
        i = bisect.bisect_right(self.dates, d) - 1
        return self.c[i] if i >= 0 else None


# ================================================================ strategies
# entry(S, i, p) -> None or {"score", "stop" (or None), "target_r" (or None), "why"}: a signal on
# bar i's close. exit(S, i, pos, p) -> None or a reason: a close-based exit on bar i's close.

def _rs(S, i):
    m = S.ind("mom", 126, 0)[i]
    return m if m is not None else -9.0


def e_rsi2(S, i, p):
    s200, r2 = S.ind("sma", 200)[i], S.ind("rsi", 2)[i]
    if s200 is None or r2 is None or not (S.c[i] > s200 and r2 < p["rsi"]):
        return None
    stop = None
    if p.get("stop_atr"):
        a = S.ind("atr", 14)[i]
        if a is None:
            return None
        stop = S.c[i] - p["stop_atr"] * a
    return {"score": -r2, "stop": stop, "why": "close %.2f > SMA200 %.2f, RSI(2) %.1f < %g" % (S.c[i], s200, r2, p["rsi"])}


def x_rsi2(S, i, pos, p):
    s5 = S.ind("sma", 5)[i]
    if s5 is not None and S.c[i] > s5:
        return "close above the 5-day SMA"
    return None


def e_oversold(S, i, p):
    s200, r14 = S.ind("sma", 200)[i], S.ind("rsi", 14)[i]
    if s200 is None or r14 is None or not (S.c[i] > s200 and r14 < p["rsi"]):
        return None
    return {"score": -r14, "stop": None, "why": "close %.2f > SMA200 %.2f, RSI(14) %.1f < %g" % (S.c[i], s200, r14, p["rsi"])}


def x_oversold(S, i, pos, p):
    r14 = S.ind("rsi", 14)[i]
    if r14 is not None and r14 > p["exit_rsi"]:
        return "RSI(14) above %g" % p["exit_rsi"]
    return None


def e_breakout(S, i, p):
    hi = S.ind("hi_close", p["n"])[i]
    if hi is None or not S.c[i] > hi:
        return None
    return {"score": _rs(S, i), "stop": None, "why": "close %.2f above the prior %d-day closing high %.2f" % (S.c[i], p["n"], hi)}


def x_breakout(S, i, pos, p):
    lo = S.ind("lo_low", p["exit_n"])[i]
    if lo is not None and S.c[i] < lo:
        return "close below the %d-day low" % p["exit_n"]
    return None


def e_high52(S, i, p):
    hi, va = S.ind("hi_close", 252)[i], S.ind("vol_avg", 50)[i]
    if hi is None or va is None or not S.c[i] > hi:
        return None
    ratio = S.v[i] / va if va > 0 else 0.0
    if p["vol"] > 0 and (va <= 0 or ratio < p["vol"]):
        return None
    return {"score": ratio, "stop": None, "why": "close %.2f a new 52-week closing high (> %.2f), volume %.1fx its 50-day average"
            % (S.c[i], hi, ratio)}


def x_below_sma(S, i, pos, p):
    s = S.ind("sma", p["exit_sma"])[i]
    if s is not None and S.c[i] < s:
        return "close below the %d-day SMA" % p["exit_sma"]
    return None


def e_ma_cross(S, i, p):
    f, s = S.ind("sma", p["fast"]), S.ind("sma", p["slow"])
    if i < 1 or None in (f[i], s[i], f[i - 1], s[i - 1]):
        return None
    if f[i] > s[i] and f[i - 1] <= s[i - 1]:
        return {"score": _rs(S, i), "stop": None, "why": "SMA%d %.2f crossed above SMA%d %.2f" % (p["fast"], f[i], p["slow"], s[i])}
    return None


def x_ma_cross(S, i, pos, p):
    f, s = S.ind("sma", p["fast"])[i], S.ind("sma", p["slow"])[i]
    if f is not None and s is not None and f < s:
        return "SMA%d crossed back below SMA%d" % (p["fast"], p["slow"])
    return None


def e_gap(S, i, p):
    if i < 1:
        return None
    va = S.ind("vol_avg", 50)[i]
    if va is None or va <= 0:
        return None
    gap = S.o[i] / S.c[i - 1] - 1
    ratio = S.v[i] / va
    if gap * 100 >= p["gap"] and ratio >= p["vol"] and S.c[i] >= (S.h[i] + S.l[i]) / 2.0:
        return {"score": ratio, "stop": S.l[i], "why": "gap %+.1f%% on %.1fx volume, closed %.2f in the top half of %.2f-%.2f"
                % (100 * gap, ratio, S.c[i], S.l[i], S.h[i])}
    return None


def bull_flag(S, i):
    """india-scanner's bull flag on the closes up to bar i-1: up more than 10 % over the 10 sessions
    before the last 5 (c[i-6] / c[i-16] - 1 > 0.10) and the last 5 closes (i-5 .. i-1) inside a 5 %
    range of c[i-1]. -> (flag high, flag low, run) from those 5 bars' highs and lows, or None."""
    if i < 16:
        return None
    run = S.c[i - 6] / S.c[i - 16] - 1
    cl = S.c[i - 5:i]
    if run > 0.10 and (max(cl) - min(cl)) / S.c[i - 1] < 0.05:
        return max(S.h[i - 5:i]), min(S.l[i - 5:i]), run
    return None


def e_bull_flag(S, i, p):
    f = bull_flag(S, i)
    if f is None or not S.c[i] > f[0]:
        return None
    return {"score": f[2], "stop": f[1], "target_r": p["r"],
            "why": "pole %+.1f%%, 5-day flag %.2f-%.2f, close %.2f above the flag high" % (100 * f[2], f[1], f[0], S.c[i])}


def grid(**axes):
    return bt.grid(**axes)


STRATEGIES = {
    "pullback_rsi2": {
        "title": "RSI(2) pullback in an uptrend", "entry": e_rsi2, "exit": x_rsi2,
        "rules": ("Close above its 200-day SMA and RSI(2, Wilder) under 10 (variants 5, 15) -> buy the next open. "
                  "Exit at the next open after a close above the 5-day SMA, or after 10 sessions. No stop by "
                  "default (variant: a stop 3 x ATR(14) under the signal close)."),
        "score": "lowest RSI(2) first (the deepest pullback)",
        "default": {"rsi": 10, "stop_atr": 0, "max_days": 10},
        "variants": [{"rsi": 5, "stop_atr": 0, "max_days": 10}, {"rsi": 10, "stop_atr": 0, "max_days": 10},
                     {"rsi": 15, "stop_atr": 0, "max_days": 10}, {"rsi": 10, "stop_atr": 3, "max_days": 10}]},
    "oversold_bounce_d": {
        "title": "daily oversold bounce in an uptrend", "entry": e_oversold, "exit": x_oversold,
        "rules": ("Close above its 200-day SMA (the trend is still up) and RSI(14, Wilder) under 30 (variants 25, "
                  "35) -> buy the next open. Exit at the next open after RSI(14) closes above 50, or after 15 "
                  "sessions (variant 10). No stop."),
        "score": "lowest RSI(14) first",
        "default": {"rsi": 30, "exit_rsi": 50, "max_days": 15},
        "variants": [{"rsi": 25, "exit_rsi": 50, "max_days": 15}, {"rsi": 30, "exit_rsi": 50, "max_days": 15},
                     {"rsi": 35, "exit_rsi": 50, "max_days": 15}, {"rsi": 30, "exit_rsi": 50, "max_days": 10}]},
    "breakout_20d": {
        "title": "20-day closing-high breakout", "entry": e_breakout, "exit": x_breakout,
        "rules": ("Close above the highest close of the 20 sessions before it (variant 55) -> buy the next open "
                  "(india-scanner's breakout_20d). Trailing exit: the next open after a close below the lowest "
                  "low of the 10 sessions before it (variant 20). No other stop, no time limit."),
        "score": "strongest 6-month return first (relative strength)",
        "default": {"n": 20, "exit_n": 10},
        "variants": grid(n=[20, 55], exit_n=[10, 20])},
    "high_52w": {
        "title": "52-week high on volume", "entry": e_high52, "exit": x_below_sma,
        "rules": ("Close above the highest close of the 252 sessions before it, on volume at least 1.5x its "
                  "50-day average (variants 2x, no volume filter) -> buy the next open. Exit at the next open "
                  "after a close below the 50-day SMA (variant 20-day)."),
        "score": "highest volume multiple first",
        "default": {"vol": 1.5, "exit_sma": 50},
        "variants": [{"vol": 1.5, "exit_sma": 50}, {"vol": 2.0, "exit_sma": 50}, {"vol": 0, "exit_sma": 50},
                     {"vol": 1.5, "exit_sma": 20}]},
    "ma_cross": {
        "title": "50/200-day moving-average cross", "entry": e_ma_cross, "exit": x_ma_cross,
        "rules": ("The 50-day SMA closes above the 200-day SMA after being at or under it the day before -> buy "
                  "the next open. Exit at the next open after the 50-day closes back under the 200-day. "
                  "Variants 20/100 and 50/150."),
        "score": "strongest 6-month return first",
        "default": {"fast": 50, "slow": 200},
        "variants": [{"fast": 50, "slow": 200}, {"fast": 20, "slow": 100}, {"fast": 50, "slow": 150}]},
    "momentum_rotation": {
        "title": "monthly momentum rotation", "kind": "rotation",
        "rules": ("At the last close of each month rank every stock above its 200-day SMA by its 12-1 month "
                  "return (close 21 sessions ago / close 252 sessions ago; variant 6-1: 126 sessions); hold the "
                  "top 10 (variants 5, 20). On the first trading day of the next month sell, at the open, what "
                  "left the top N, and buy the newcomers with equity / N each. Holdings that stay are not "
                  "re-weighted. No stops."),
        "score": "the momentum itself",
        "default": {"top": 10, "look": 252, "skip": 21},
        "variants": grid(top=[5, 10, 20], look=[252, 126], skip=[21])},
    "gap_volume_drift": {
        "title": "gap-up on heavy volume, then drift (results proxy)", "entry": e_gap, "exit": None,
        "rules": ("A proxy for post-results drift (no historical results dates here): the day opens at least 4 % "
                  "above the previous close (variant 3 %), trades at least 3x its 50-day average volume and "
                  "closes in the top half of its range -> buy the next open. Stop at the gap day's low (skipped "
                  "if the next open is already under it). Hold 20 sessions (variants 10, 40), out at the next open."),
        "score": "highest volume multiple first",
        "default": {"gap": 4.0, "vol": 3.0, "max_days": 20},
        "variants": [{"gap": 4.0, "vol": 3.0, "max_days": 10}, {"gap": 4.0, "vol": 3.0, "max_days": 20},
                     {"gap": 4.0, "vol": 3.0, "max_days": 40}, {"gap": 3.0, "vol": 3.0, "max_days": 20}]},
    "bull_flag_d": {
        "title": "daily bull flag breakout", "entry": e_bull_flag, "exit": None,
        "rules": ("india-scanner's bull flag at yesterday's close (up more than 10 % over the 10 sessions before "
                  "the last 5, then the last 5 closes inside a 5 % range); today closes above the flag's highest "
                  "high -> buy the next open. Stop at the flag's lowest low; target 2R from the fill (variant 3R); "
                  "else out at the next open after 20 sessions (variant 10)."),
        "score": "the biggest pole (the 10-session run) first",
        "default": {"r": 2.0, "max_days": 20},
        "variants": [{"r": 2.0, "max_days": 20}, {"r": 3.0, "max_days": 20}, {"r": 2.0, "max_days": 10}]},
}
ORDER = ["pullback_rsi2", "oversold_bounce_d", "breakout_20d", "high_52w", "ma_cross", "momentum_rotation",
         "gap_volume_drift", "bull_flag_d"]


def variant_label(name, p):
    d = STRATEGIES[name]["default"]
    parts = ["%s=%g" % (k, p[k]) for k in sorted(p)]
    return " ".join(parts) + (" (default)" if p == d else "")


def parse_strategies(text):
    if not text or text == "all":
        return list(ORDER)
    out = []
    for s in text.split(","):
        s = s.strip().lower().replace("-", "_")
        if s not in STRATEGIES:
            raise SwingError("unknown swing strategy %r: pick from %s (or all)" % (s, ", ".join(ORDER)))
        if s not in out:
            out.append(s)
    return out


# ================================================================ the engine

class Membership(object):
    """Point-in-time index membership. lists {month_end: [symbols]}: a list is known at that month-end's
    close and applies to every signal from that close until the next month-end (exclusive). core (the
    same shape, optional) marks the stocks that pay the lower Nifty-100 slippage at that time."""

    def __init__(self, lists, core=None, name="pit"):
        self.name = name
        self.ends = sorted(lists)
        self.sets = [frozenset(lists[e]) for e in self.ends]
        self.core_sets = [frozenset(core.get(e, ())) for e in self.ends] if core is not None else None

    def _k(self, d):
        return bisect.bisect_right(self.ends, d) - 1

    def member(self, sym, d):
        k = self._k(d)
        return k >= 0 and sym in self.sets[k]

    def is_core(self, sym, d):
        if self.core_sets is None:
            return self.member(sym, d)
        k = self._k(d)
        return k >= 0 and sym in self.core_sets[k]

    def ever(self):
        out = set()
        for x in self.sets:
            out |= x
        return out


def signals_for(uni, name, p, d_from, d_to, mem=None):
    """{date: [(symbol, signal)]} for signals on closes from d_from to d_to (only while a member of
    `mem`, a Membership, when given)."""
    fn = STRATEGIES[name]["entry"]
    out = {}
    for sym in sorted(uni):
        S = uni[sym]
        lo = bisect.bisect_left(S.dates, d_from)
        hi = bisect.bisect_right(S.dates, d_to)
        for i in range(max(lo, 1), hi):
            if mem is not None and not mem.member(sym, S.dates[i]):
                continue
            sg = fn(S, i, p)
            if sg:
                sg["i"] = i
                out.setdefault(S.dates[i], []).append((sym, sg))
    return out


def month_ends(cal):
    """Calendar dates that are the last session of their month (a later session exists)."""
    return set(cal[k] for k in range(len(cal) - 1) if cal[k + 1][:7] != cal[k][:7])


def rotation_pick(uni, d, p, mem=None):
    """The top N symbols by momentum among those above their 200-day SMA at d's close (and members of
    the point-in-time universe at d, when `mem` is given)."""
    cands = []
    for sym, S in uni.items():
        i = S.idx.get(d)
        if i is None:
            continue
        if mem is not None and not mem.member(sym, d):
            continue
        s200, m = S.ind("sma", 200)[i], S.ind("mom", p["look"], p["skip"])[i]
        if s200 is None or m is None or not S.c[i] > s200:
            continue
        cands.append((m, sym))
    cands.sort(key=lambda x: (-x[0], x[1]))
    return cands[:p["top"]]


class Pos(object):
    __slots__ = ("sym", "qty", "entry_date", "entry_ref", "fill", "stop", "target", "held", "buy_charges", "why",
                 "slip", "last", "signal_date")


def simulate(uni, name, p, cal, cfg=None):
    """One strategy variant over the calendar `cal` (dates of sessions) -> result dict (pure)."""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    spec = STRATEGIES[name]
    rotation = spec.get("kind") == "rotation"
    maxpos = p["top"] if rotation else int(c["max_positions"])
    costs = bool(c["costs"])
    broker, exch = c["broker"], c["exchange"]
    mem = c.get("membership")
    delist = bool(c.get("delist_exit"))
    sigs = {} if rotation else signals_for(uni, name, p, cal[0], cal[-1], mem)
    ends = month_ends(cal) if rotation else set()
    cash = float(c["capital"])
    positions, pending_out, pending_in = {}, {}, []
    trades, equity, invested, skips = [], [], [], {}
    dp_days = set()
    eq_prev = cash
    tot = {"charges": 0.0, "slippage": 0.0}

    def skip(why):
        skips[why] = skips.get(why, 0) + 1

    def slip_of(S, d=None):
        if not costs:
            return 0.0
        core = mem.is_core(S.sym, d) if (mem is not None and d is not None) else S.core
        return (c["slip_core"] if core else c["slip_other"]) / 100.0

    def close(pos, ref, d, reason, slipped=True):
        nonlocal cash
        s = pos.slip if slipped else 0.0
        px = ref * (1 - s)
        sv = pos.qty * px
        ch = 0.0
        if costs:
            first = (d, pos.sym) not in dp_days
            dp_days.add((d, pos.sym))
            ch = nse.delivery_charges(0.0, sv, exch, broker, dp_debits=1 if first else 0)["total"]
        cash += sv - ch
        gross = pos.qty * (ref - pos.entry_ref)
        slip_inr = pos.qty * pos.entry_ref * pos.slip + pos.qty * ref * s
        net = sv - ch - pos.qty * pos.fill - pos.buy_charges
        tot["charges"] += ch
        tot["slippage"] += pos.qty * ref * s
        trades.append({"symbol": pos.sym, "signal_date": pos.signal_date, "entry_date": pos.entry_date,
                       "entry": round(pos.fill, 4), "entry_open": pos.entry_ref, "exit_date": d,
                       "exit": round(px, 4), "exit_ref": round(ref, 4), "qty": pos.qty, "reason": reason,
                       "held": pos.held, "stop": _r(pos.stop, 4), "target": _r(pos.target, 4),
                       "gross_inr": round(gross, 2), "charges_inr": round(pos.buy_charges + ch, 2),
                       "slippage_inr": round(slip_inr, 2), "costs_inr": round(pos.buy_charges + ch + slip_inr, 2),
                       "net_inr": round(net, 2), "ret_pct": round(100.0 * net / (pos.qty * pos.fill), 3),
                       "why": pos.why})
        del positions[pos.sym]

    for k, d in enumerate(cal):
        # 0. a held stock whose bars have ended (delisted, merged, suspended for good): out at its last
        #    close (optimistic for a company that failed; see the caveats)
        if delist:
            for sym in sorted(positions):
                S = uni[sym]
                if S.dates[-1] < d:
                    pending_out.pop(sym, None)
                    close(positions[sym], S.c[-1], S.dates[-1], "delisted")
        # 1. the open: exits decided at yesterday's close
        for sym in sorted(pending_out):
            pos = positions.get(sym)
            if pos is None:
                pending_out.pop(sym)
                continue
            S = uni[sym]
            i = S.idx.get(d)
            if i is None or S.h[i] == S.l[i]:
                continue                                    # no bar, or locked at a circuit: try tomorrow
            close(pos, S.o[i], d, pending_out.pop(sym))
        # 2. the open: entries, best score first, while slots and cash last
        if pending_in:
            pending_in.sort(key=lambda x: (-x[0], x[1]))
            for score, sym, sg in pending_in:
                if sym in positions:
                    continue
                if len(positions) >= maxpos:
                    skip("no free slot")
                    continue
                S = uni[sym]
                i = S.idx.get(d)
                if i is None:
                    skip("no bar on the fill day")
                    continue
                op = S.o[i]
                if S.h[i] == S.l[i]:
                    skip("locked at a circuit on the fill day (no range): no fill")
                    continue
                if sg.get("stop") is not None and op <= sg["stop"]:
                    skip("opened at or under the stop")
                    continue
                s = slip_of(S, d)
                fill = op * (1 + s)
                alloc = eq_prev / maxpos
                if c["sizing"] == "risk" and not rotation:
                    stop = sg.get("stop")
                    if stop is None:
                        a = S.ind("atr", 14)[sg["i"]]
                        stop = S.c[sg["i"]] - 2 * a if a else None
                    if stop is not None and fill - stop > 0:
                        alloc = min(alloc, eq_prev * c["risk_pct"] / 100.0 / (fill - stop) * fill)
                budget = min(alloc, cash)
                qty = int(budget / (fill * (1.0015 if costs else 1.0)))
                if qty < 1:
                    skip("too little cash for one share")
                    continue
                bv = qty * fill
                ch = nse.delivery_charges(bv, 0.0, exch, broker)["total"] if costs else 0.0
                cash -= bv + ch
                tot["charges"] += ch
                tot["slippage"] += qty * op * s
                pos = Pos()
                pos.sym, pos.qty, pos.entry_date, pos.entry_ref, pos.fill = sym, qty, d, op, fill
                pos.stop = sg.get("stop")
                pos.target = (fill + sg["target_r"] * (fill - pos.stop)) if sg.get("target_r") and pos.stop is not None else None
                pos.held, pos.buy_charges, pos.why, pos.slip, pos.last = 0, ch, sg.get("why", ""), s, op
                pos.signal_date = S.dates[sg["i"]] if "i" in sg else None
                positions[sym] = pos
            pending_in = []
        # 3. during the day: stops on the low (a gap through fills at the open), targets on the high
        for sym in sorted(positions):
            pos = positions[sym]
            S = uni[sym]
            i = S.idx.get(d)
            if i is None or S.h[i] == S.l[i]:
                continue                                    # locked at a circuit: nothing trades
            o, h, l = S.o[i], S.h[i], S.l[i]
            if pos.stop is not None:
                if pos.entry_date != d and o <= pos.stop:
                    pending_out.pop(sym, None)
                    close(pos, o, d, "stop_gap")
                    continue
                if l <= pos.stop:
                    pending_out.pop(sym, None)
                    close(pos, pos.stop, d, "stop")
                    continue
            if pos.target is not None:
                if pos.entry_date != d and o >= pos.target:
                    pending_out.pop(sym, None)
                    close(pos, o, d, "target", slipped=False)
                    continue
                if h >= pos.target:
                    pending_out.pop(sym, None)
                    close(pos, pos.target, d, "target", slipped=False)
                    continue
        # 4. the close: mark to market, close-based and time exits for tomorrow's open
        inv = 0.0
        for sym in sorted(positions):
            pos = positions[sym]
            S = uni[sym]
            i = S.idx.get(d)
            if i is not None:
                pos.held += 1
                pos.last = S.c[i]
                if sym not in pending_out and not rotation:
                    why = spec["exit"](S, i, pos, p) if spec.get("exit") else None
                    if why is None and p.get("max_days") and pos.held >= p["max_days"]:
                        why = "time (%d sessions)" % p["max_days"]
                    if why:
                        pending_out[sym] = why
            inv += pos.qty * pos.last
        eq_prev = cash + inv
        equity.append(round(eq_prev, 2))
        invested.append(round(inv, 2))
        # 5. the close: new signals for tomorrow's open
        if k == len(cal) - 1:
            break
        if rotation:
            if d in ends:
                top = rotation_pick(uni, d, p, mem)
                keep = set(sym for _, sym in top)
                for sym in positions:
                    if sym not in keep:
                        pending_out[sym] = "left the top %d" % p["top"]
                for m, sym in top:
                    if sym not in positions:
                        S = uni[sym]
                        pending_in.append((m, sym, {"score": m, "stop": None, "i": S.idx[d],
                                                    "why": "%d-%d momentum %+.1f%%, rank in the top %d"
                                                    % (p["look"], p["skip"], 100 * m, p["top"])}))
        else:
            for sym, sg in sigs.get(d, ()):
                if sym not in positions:
                    pending_in.append((sg["score"], sym, sg))
    open_end = [{"symbol": pos.sym, "entry_date": pos.entry_date, "entry": round(pos.fill, 4), "qty": pos.qty,
                 "last": pos.last, "unrealised_inr": round(pos.qty * (pos.last - pos.fill) - pos.buy_charges, 2)}
                for pos in positions.values()]
    return {"dates": list(cal), "equity": equity, "invested": invested, "trades": trades, "open": open_end,
            "skips": skips, "charges_inr": round(tot["charges"], 2), "slippage_inr": round(tot["slippage"], 2),
            "capital": float(c["capital"])}


# ================================================================ metrics

def curve_stats(dates, eq, base_date, base_eq):
    """CAGR, total return, max drawdown (% and its length in calendar days, peak to recovery or to the
    end), Sharpe-like (daily returns, rf = 0, x sqrt(252)), on equity closes after base_date."""
    if not dates:
        return {"cagr_pct": None, "total_return_pct": None, "max_dd_pct": None, "max_dd_days": None, "sharpe": None}
    years = (as_date(dates[-1]) - as_date(base_date)).days / 365.25
    total = eq[-1] / base_eq - 1
    cagr = (((eq[-1] / base_eq) ** (1.0 / years) - 1) if eq[-1] > 0 else -1.0) if years > 0 else None
    rets, prev = [], base_eq
    for v in eq:
        rets.append(v / prev - 1 if prev else 0.0)
        prev = v
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    sharpe = (statistics.mean(rets) / sd * math.sqrt(252)) if sd > 0 else None
    peak, peak_d = base_eq, base_date
    mdd, mdd_peak, mdd_peak_d, trough_k = 0.0, base_eq, base_date, None
    for k, v in enumerate(eq):
        if v >= peak:
            peak, peak_d = v, dates[k]
        dd = 1 - v / peak
        if dd > mdd:
            mdd, mdd_peak, mdd_peak_d, trough_k = dd, peak, peak_d, k
    days, recovered = 0, None
    if trough_k is not None:
        rec = next((dates[k] for k in range(trough_k + 1, len(eq)) if eq[k] >= mdd_peak), None)
        recovered = rec
        days = (as_date(rec or dates[-1]) - as_date(mdd_peak_d)).days
    return {"cagr_pct": _r(100 * cagr) if cagr is not None else None, "total_return_pct": _r(100 * total),
            "max_dd_pct": _r(100 * mdd), "max_dd_days": days, "max_dd_peak": mdd_peak_d if trough_k is not None else None,
            "max_dd_trough": dates[trough_k] if trough_k is not None else None, "max_dd_recovered": recovered,
            "sharpe": _r(sharpe), "start": dates[0], "end": dates[-1], "base_date": base_date,
            "start_equity": round(base_eq, 2), "end_equity": round(eq[-1], 2)}


def trade_stats(trades):
    n = len(trades)
    if not n:
        return {"trades": 0, "win_pct": None, "avg_trade_pct": None, "avg_hold_days": None, "profit_factor": None,
                "t_stat": None}
    rp = [t["ret_pct"] for t in trades]
    wins = sum(t["net_inr"] for t in trades if t["net_inr"] > 0)
    losses = -sum(t["net_inr"] for t in trades if t["net_inr"] < 0)
    sd = statistics.stdev(rp) if n > 1 else 0.0
    return {"trades": n, "win_pct": _r(100.0 * sum(1 for t in trades if t["net_inr"] > 0) / n, 1),
            "avg_trade_pct": _r(sum(rp) / n, 3), "avg_hold_days": _r(sum(t["held"] for t in trades) / n, 1),
            "profit_factor": _r(wins / losses) if losses > 0 else None,
            "t_stat": _r((sum(rp) / n) / (sd / math.sqrt(n))) if sd > 0 else None,
            "outcomes": dict(sorted(((r, sum(1 for t in trades if t["reason"] == r))
                                     for r in set(t["reason"] for t in trades)), key=lambda kv: -kv[1]))}


def slice_curve(res, d_from, d_to, base):
    """(dates, equity, base_date, base_equity) for the sessions d_from..d_to; the base is the close
    before d_from (or the starting capital)."""
    ds, eq = res["dates"], res["equity"]
    lo = bisect.bisect_left(ds, d_from)
    hi = bisect.bisect_right(ds, d_to)
    if lo == 0:
        return ds[lo:hi], eq[lo:hi], base, res["capital"]
    return ds[lo:hi], eq[lo:hi], ds[lo - 1], eq[lo - 1]


def year_table(dates, eq, base_date, base_eq):
    out, start_v, cur = [], base_eq, None
    for k, d in enumerate(dates):
        y = d[:4]
        if cur is None:
            cur = y
        if y != cur:
            out.append((cur, eq[k - 1] / start_v - 1))
            start_v, cur = eq[k - 1], y
    if dates:
        out.append((cur, eq[-1] / start_v - 1))
    return dict((y, round(100 * r, 2)) for y, r in out)


def regimes(nifty, vix, cal):
    """{date: (trend regime, vix regime)} from the PREVIOUS session's close."""
    out = {}
    s200 = nifty.ind("sma", 200)
    for d in cal:
        i = bisect.bisect_left(nifty.dates, d) - 1          # the session before d
        tr = "sideways"
        if i >= TREND_SLOPE and s200[i] is not None and s200[i - TREND_SLOPE] is not None:
            above, rising = nifty.c[i] > s200[i], s200[i] > s200[i - TREND_SLOPE]
            tr = "up" if above and rising else ("down" if (not above and not rising) else "sideways")
        vr = None
        if vix is not None:
            j = bisect.bisect_left(vix.dates, d) - 1
            if j >= 0:
                vr = "vix_high" if vix.c[j] >= VIX_HIGH else "vix_normal"
        out[d] = (tr, vr)
    return out


def regime_table(dates, eq, base_eq, reg, bench_eq=None, bench_base=None):
    acc = {}
    prev, bprev = base_eq, bench_base
    for k, d in enumerate(dates):
        r = eq[k] / prev - 1
        br = (bench_eq[k] / bprev - 1) if bench_eq else None
        prev = eq[k]
        if bench_eq:
            bprev = bench_eq[k]
        for name in reg.get(d, ()):
            if not name:
                continue
            a = acc.setdefault(name, [0, 1.0, 1.0])
            a[0] += 1
            a[1] *= 1 + r
            if br is not None:
                a[2] *= 1 + br
    out = []
    for name in ("up", "sideways", "down", "vix_normal", "vix_high"):
        if name in acc:
            n, g, bg = acc[name]
            out.append({"regime": name, "days": n, "return_pct_ann": _r(100 * (g ** (252.0 / n) - 1)),
                        "bench_return_pct_ann": _r(100 * (bg ** (252.0 / n) - 1)) if bench_eq else None})
    return out


def weekly(dates, eq, step_weeks=1):
    """The last session of every step_weeks-th ISO week (and the last date)."""
    out, last_key = [], None
    for k, d in enumerate(dates):
        y, w, _ = as_date(d).isocalendar()
        key = (y * 53 + w) // step_weeks
        if last_key is not None and key != last_key:
            out.append((dates[k - 1], eq[k - 1]))
        last_key = key
    if dates:
        out.append((dates[-1], eq[-1]))
    return out


def bench_curve(S, cal, base_date, capital):
    """Buy and hold: capital x close / the close on base_date, on the same dates (no costs)."""
    b0 = S.close_on(base_date) or S.c[0]
    return [round(capital * (S.close_on(d) or b0) / b0, 2) for d in cal]


def wf_split(cal):
    if cal and cal[0] <= "2016-12-31" and cal[-1] >= "2023-06-30":
        return IS_END
    return cal[max(0, int(len(cal) * SPLIT) - 1)] if cal else None


def evaluate(res, is_end, base_date):
    """Full / in-sample / out-of-sample stats of one simulation."""
    tr = res["trades"]
    full = curve_stats(res["dates"], res["equity"], base_date, res["capital"])
    full.update(trade_stats(tr))
    ds, eq, b, be = slice_curve(res, res["dates"][0], is_end, base_date)
    ins = curve_stats(ds, eq, b, be)
    ins.update(trade_stats([t for t in tr if t["exit_date"] <= is_end]))
    ds, eq, b, be = slice_curve(res, "%s~" % is_end, res["dates"][-1], base_date)
    oos = curve_stats(ds, eq, b, be) if ds else curve_stats([], [], None, None)
    oos.update(trade_stats([t for t in tr if t["entry_date"] > is_end]))
    inv = res["invested"]
    full["exposure_pct"] = _r(100.0 * sum(i / e for i, e in zip(inv, res["equity"]) if e > 0) / len(inv)) if inv else None
    return full, ins, oos


def short(st):
    keys = ("cagr_pct", "total_return_pct", "max_dd_pct", "max_dd_days", "sharpe", "trades", "win_pct",
            "avg_trade_pct", "avg_hold_days", "profit_factor", "start", "end")
    return dict((k, st.get(k)) for k in keys)


def verdict(full, oos, gross):
    n = full.get("trades") or 0
    if n == 0:
        return "never fired: nothing to judge"
    if n < 30:
        return "too few trades to judge (%d)" % n
    net, g = full.get("cagr_pct") or 0.0, (gross or {}).get("cagr_pct")
    o, on = oos.get("cagr_pct"), oos.get("trades") or 0
    if net <= 0:
        if g is not None and g > 0:
            return "no edge after costs: %+.1f %% a year before costs becomes %+.1f %% after" % (g, net)
        return "no edge, even before costs (%s %% a year gross, %+.1f %% net)" % ("%+.1f" % g if g is not None else "?", net)
    if o is None or on < MIN_OOS_TRADES:
        return "positive after costs (%+.1f %% a year), but too few out-of-sample trades to confirm" % net
    if o <= 0:
        return ("positive over the whole period (%+.1f %% a year after costs) but it did not hold out of sample "
                "(%+.1f %% a year since %s)" % (net, o, oos.get("start")))
    t = full.get("t_stat") or 0.0
    if t >= 2 and n >= 200:
        return ("positive after costs (%+.1f %% a year, %d trades, t=%.1f) and held out of sample (%+.1f %% a year)"
                % (net, n, t, o))
    return "small positive edge, thin evidence (%+.1f %% a year, t=%.1f; out of sample %+.1f %% a year)" % (net, t, o)


def vs_index(full, nifty, bees):
    s, b, e = full.get("cagr_pct"), nifty.get("cagr_pct"), bees.get("cagr_pct")
    if s is None or b is None:
        return "no comparison (no CAGR)"
    dd = (full.get("max_dd_pct") or 0) - (nifty.get("max_dd_pct") or 0)
    ddt = ("%.0f pts deeper drawdown (%.0f %% vs %.0f %%)" % (dd, full["max_dd_pct"], nifty["max_dd_pct"]) if dd > 0
           else "a %.0f pts shallower drawdown (%.0f %% vs %.0f %%)" % (-dd, full["max_dd_pct"], nifty["max_dd_pct"]))
    if s > b:
        if e is not None and s <= e:
            return ("beat the Nifty 50 price index by %.1f %% a year with %s, but not NIFTYBEES, the index fund with "
                    "dividends (%.1f %% vs %.1f %% a year)" % (s - b, ddt, s, e))
        return "beat Nifty by %.1f %% a year (%.1f %% over NIFTYBEES incl. dividends) with %s" % (
            s - b, (s - e) if e is not None else 0.0, ddt)
    return ("did not beat simply holding the index: %.1f %% a year vs %.1f %% for the Nifty 50 (%s %% NIFTYBEES "
            "incl. dividends), max drawdown %.0f %% vs %.0f %%" % (s, b, "%.1f" % e if e is not None else "-",
                                                                full.get("max_dd_pct") or 0, nifty.get("max_dd_pct") or 0))


# ================================================================ a whole backtest (pure)

def build_calendar(uni, nifty=None):
    """Sessions: every Nifty 50 date, plus any date at least half the listed stocks traded."""
    count, first, last = {}, {}, {}
    for sym, S in uni.items():
        if not S.dates:
            continue
        first[sym], last[sym] = S.dates[0], S.dates[-1]
        for d in S.dates:
            count[d] = count.get(d, 0) + 1
    days = set(nifty.dates) if nifty else set()
    for d, n in count.items():
        if d in days:
            continue
        active = sum(1 for s in first if first[s] <= d <= last[s])
        if n >= max(1, 0.5 * active):
            days.add(d)
    return sorted(days)


def backtest(uni, names, bench, cfg=None, d_from=None, d_to=None, with_trades=True, extras=True, log=None):
    """uni {SYM: Series}, bench {"^NSEI": Series, "NIFTYBEES": Series, "^INDIAVIX": Series or None}
    -> (results, meta, benchmarks). No I/O."""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    nifty = bench.get(NIFTY)
    cal_all = build_calendar(uni, nifty)
    lo = bisect.bisect_left(cal_all, d_from) if d_from else 0
    lo = max(lo, 1)
    hi = bisect.bisect_right(cal_all, d_to) if d_to else len(cal_all)
    cal = cal_all[lo:hi]
    if len(cal) < 30:
        raise SwingError("only %d sessions in the period: fetch more history (swing fetch --years N)" % len(cal))
    base = cal_all[lo - 1]
    is_end = wf_split(cal)
    reg = regimes(nifty, bench.get(VIX), cal) if nifty else {}
    benches = {}
    for bname, sym in BENCH:
        S = bench.get(sym)
        if S is None:
            continue
        eq = bench_curve(S, cal, base, c["capital"])
        res = {"dates": cal, "equity": eq, "invested": eq, "trades": [], "capital": c["capital"]}
        full, ins, oos = evaluate(res, is_end, base)
        benches[bname] = {"symbol": sym, "full": short(full), "in_sample": short(ins), "out_of_sample": short(oos),
                          "by_year": year_table(cal, eq, base, c["capital"]),
                          "by_regime": regime_table(cal, eq, c["capital"], reg), "_eq": eq}
    n_eq = (benches.get("bench_nifty50") or {}).get("_eq")
    nfull = (benches.get("bench_nifty50") or {}).get("full") or {}
    bfull = (benches.get("bench_niftybees") or {}).get("full") or {}
    noos = (benches.get("bench_nifty50") or {}).get("out_of_sample") or {}
    out = {}
    for name in names:
        spec = STRATEGIES[name]
        rows, runs = [], {}
        for p in spec["variants"]:
            if log:
                log("  %s %s" % (name, variant_label(name, p)))
            res = simulate(uni, name, p, cal, c)
            full, ins, oos = evaluate(res, is_end, base)
            key = json.dumps(p, sort_keys=True)
            runs[key] = (res, full, ins, oos)
            rows.append({"label": variant_label(name, p), "params": p, "default": p == spec["default"],
                         "full": short(full), "in_sample": short(ins), "out_of_sample": short(oos)})
        dkey = json.dumps(spec["default"], sort_keys=True)
        if dkey not in runs:
            res = simulate(uni, name, spec["default"], cal, c)
            runs[dkey] = (res,) + evaluate(res, is_end, base)
        ok = [r for r in rows if (r["in_sample"]["trades"] or 0) >= MIN_IS_TRADES and r["in_sample"]["sharpe"] is not None]
        chosen = max(ok, key=lambda r: (r["in_sample"]["sharpe"], r["in_sample"]["cagr_pct"] or -99)) if ok else \
            [r for r in rows if r["default"]][0]
        res, full, ins, oos = runs[dkey]
        cres, cfull, cins, coos = runs[json.dumps(chosen["params"], sort_keys=True)]
        gross = angel = risk = None
        if extras:
            g = simulate(uni, name, spec["default"], cal, dict(c, costs=False))
            gross = short(evaluate(g, is_end, base)[0])
            other = "angel" if c["broker"] == "dhan" else "dhan"
            a = simulate(uni, name, spec["default"], cal, dict(c, broker=other))
            angel = dict(short(evaluate(a, is_end, base)[0]), broker=other, charges_inr=a["charges_inr"])
            if spec.get("kind") != "rotation":
                rk = simulate(uni, name, spec["default"], cal, dict(c, sizing="equal" if c["sizing"] == "risk" else "risk"))
                risk = dict(short(evaluate(rk, is_end, base)[0]), sizing="equal" if c["sizing"] == "risk" else "risk")
        gross_pnl = sum(t["gross_inr"] for t in res["trades"])
        cost_inr = sum(t["costs_inr"] for t in res["trades"])
        full["by_year"] = [{"year": y, "return_pct": v,
                            "bench_return_pct": (benches.get("bench_nifty50") or {}).get("by_year", {}).get(y)}
                           for y, v in sorted(year_table(cal, res["equity"], base, c["capital"]).items())]
        yrs = [r["return_pct"] for r in full["by_year"]]
        full["best_year_pct"] = max(yrs) if yrs else None
        full["worst_year_pct"] = min(yrs) if yrs else None
        full["by_regime"] = regime_table(cal, res["equity"], c["capital"], reg, n_eq, c["capital"])
        full["charges_inr"] = res["charges_inr"]
        full["slippage_inr"] = res["slippage_inr"]
        full["costs_pct_of_gross"] = _r(100.0 * cost_inr / gross_pnl) if gross_pnl > 0 else None
        full["gross_pnl_inr"] = round(gross_pnl, 2)
        full["costs_inr_closed_trades"] = round(cost_inr, 2)
        full["open_at_end"] = res["open"]
        r = {"title": spec["title"], "rules": spec["rules"], "score": spec["score"],
             "default_variant": variant_label(name, spec["default"]), "full": full,
             "in_sample": short(ins), "out_of_sample": short(oos),
             "variants": rows,
             "walk_forward": {"in_sample": [cal[0], is_end], "out_of_sample": [coos.get("start"), cal[-1]],
                              "chosen": chosen["label"], "chosen_params": chosen["params"],
                              "chosen_by": ("best in-sample Sharpe with at least %d in-sample trades" % MIN_IS_TRADES)
                              if ok else "default (no variant had %d in-sample trades)" % MIN_IS_TRADES,
                              "chosen_in_sample": short(cins), "chosen_out_of_sample": short(coos),
                              "default_out_of_sample": short(oos),
                              "bench_out_of_sample": noos},
             "before_costs": gross, "other_broker": angel, "other_sizing": risk,
             "skipped": res["skips"],
             "equity_weekly": [[d, v] for d, v in weekly(cal, res["equity"])],
             "verdict": verdict(full, coos, gross),
             "vs_index": vs_index(full, nfull, bfull)}
        dl = [t for t in res["trades"] if t["reason"] == "delisted"]
        if dl:
            r["delisted_exits"] = len(dl)
            r["delisted_symbols"] = sorted(set(t["symbol"] for t in dl))
        r["excess_cagr_pct"] = _r(full["cagr_pct"] - nfull["cagr_pct"]) if full.get("cagr_pct") is not None and \
            nfull.get("cagr_pct") is not None else None
        if with_trades:
            r["trades"] = res["trades"]
        r["_eq"] = res["equity"]
        out[name] = r
    meta = {"from": cal[0], "to": cal[-1], "base_date": base, "sessions": len(cal), "split": {
        "in_sample": [cal[0], is_end], "out_of_sample": [cal[bisect.bisect_right(cal, is_end)] if
                                                         bisect.bisect_right(cal, is_end) < len(cal) else None, cal[-1]]},
        "capital": c["capital"], "max_positions": c["max_positions"], "sizing": c["sizing"], "risk_pct": c["risk_pct"],
        "broker": c["broker"], "slippage_pct": {"nifty100": c["slip_core"], "other": c["slip_other"]},
        "core_symbols": sum(1 for S in uni.values() if S.core), "_cal": cal}
    for b in benches.values():
        b["equity_weekly"] = [[d, v] for d, v in weekly(cal, b["_eq"])]
    return out, meta, benches


def ranking(results):
    def key(n):
        o = results[n]["walk_forward"]["chosen_out_of_sample"]
        v = o.get("cagr_pct")
        return ((o.get("trades") or 0) < MIN_OOS_TRADES, -(v if v is not None else -999), n)
    return sorted(results, key=key)


# ================================================================ loading

def core_symbols(uname, symbols, a):
    if uname in ("nifty50", "nifty100"):
        return set(symbols), "every symbol is in the Nifty 100"
    path = getattr(a, "core_list", None)
    try:
        if not path and getattr(a, "offline", None):
            cached = os.path.join(nse.trader_dir(), "cache", bt.INDEX_LISTS["nifty100"])
            if not os.path.isfile(cached):
                return set(), "no Nifty 100 list offline: %.2f %% slippage for every symbol" % DEFAULTS["slip_other"]
            path = cached
        kept, _ = bt.parse_index_csv(bt.index_list("nifty100", path))
        core = set(s for s, _ in kept)
        return core, "%d of %d symbols are in today's Nifty 100" % (len(core & set(symbols)), len(symbols))
    except nse.NSEError as e:
        return set(), "the Nifty 100 list could not be read (%s): %.2f %% slippage for all" % (e, DEFAULTS["slip_other"])


def load_rows(sym, dirs):
    if dirs:
        rows = offline_rows(dirs, sym)
        if rows is None:
            raise SwingError("no daily bars for %s in %s" % (sym, ", ".join(dirs)))
        return rows
    doc = read_store(store_path(sym))
    if not doc:
        raise SwingError("not fetched yet (run: backtest.py swing fetch)")
    return doc["bars"]


def load_all(symbols, a, core):
    dirs = [d for d in (getattr(a, "offline", None) or "").replace(os.pathsep, ",").split(",") if d]
    uni, missing, notes = {}, {}, {"bad_prints": {}, "big_moves": {}, "breaks": {}, "fixed_high_low": 0, "filler": 0}
    for sym in symbols:
        try:
            rows = load_rows(sym, dirs)
        except nse.NSEError as e:
            missing[sym] = str(e)
            continue
        rows, n = clean_rows(rows)
        if n["bad_prints"]:
            notes["bad_prints"][sym] = n["bad_prints"]
        if n["big_moves"]:
            notes["big_moves"][sym] = n["big_moves"]
        if n["breaks"]:
            notes["breaks"][sym] = n["breaks"]
        notes["fixed_high_low"] += n["fixed_high_low"]
        notes["filler"] += n["filler"]
        if len(rows) < 30:
            missing[sym] = "under 30 daily bars"
            continue
        uni[sym] = Series(sym, rows, sym in core)
    bench = {}
    for sym in (NIFTY, BEES, VIX):
        try:
            rows, n = clean_rows(load_rows(sym, dirs), index=sym.startswith("^"))
            if n["bad_prints"]:
                notes["bad_prints"][sym] = n["bad_prints"]
            bench[sym] = Series(sym, rows, True)
        except nse.NSEError as e:
            missing[sym] = str(e)
    if NIFTY not in bench:
        raise SwingError("no Nifty 50 (^NSEI) bars: run `backtest.py swing fetch` first")
    return uni, bench, missing, notes


# ================================================================ point-in-time universes (pit.py builds them)

PIT_UNIVERSES = {"pit100": "nifty100", "pit500": "nifty500"}       # PIT universe -> its survivor-biased twin


def pit_files(a):
    """(pit_universe.json path, [dirs holding SYMBOL.json.gz series]); --offline DIRs win over trader/cache."""
    dirs = [d for d in (getattr(a, "offline", None) or "").replace(os.pathsep, ",").split(",") if d]
    for d in dirs:
        for sub in ("", "cache"):
            u = os.path.join(d, sub, "pit_universe.json")
            if os.path.isfile(u):
                return u, [os.path.join(d, sub, "pit_daily"), os.path.join(d, sub)] + dirs
    base = os.path.join(nse.trader_dir(), "cache")
    return os.path.join(base, "pit_universe.json"), [os.path.join(base, "pit_daily")]


def load_pit(uname, a, d_from, d_to):
    """The PIT universe `uname` -> (uni, bench, missing, notes, Membership, info). Series: every stock that
    was a member at any month-end from 45 days before d_from to d_to, from the bhavcopy-adjusted cache
    (no Yahoo, no 35 % break adjustment). Benchmarks: Yahoo (^NSEI, NIFTYBEES, ^INDIAVIX) as always."""
    upath, sdirs = pit_files(a)
    U = nse.read_json(upath)
    if not U or uname not in U:
        raise SwingError("no %s in %s: run `backtest.py swing pit-fetch` then `swing pit-build`" % (uname, upath))
    lists = U[uname]
    core = U.get("pit100") or {}
    mem = Membership(lists, None if uname == "pit100" else core, uname)
    lo = (as_date(d_from) - datetime.timedelta(days=45)).isoformat() if d_from else ""
    hi = d_to or "9999"
    syms = sorted(set(s for e, L in lists.items() if lo <= e <= hi for s in L))
    ever_core = set(s for e, L in core.items() if lo <= e <= hi for s in L)
    uni, missing = {}, {}
    notes = {"bad_prints": {}, "big_moves": {}, "breaks": {}, "fixed_high_low": 0, "filler": 0}
    events, ended, renamed = {}, [], {}
    for sym in syms:
        doc = None
        for d in sdirs:
            pth = os.path.join(d, stem(sym) + ".json.gz")
            if os.path.isfile(pth):
                doc = read_store(pth)
                break
        if not doc:
            missing[sym] = "no PIT series (run swing pit-build)"
            continue
        rows, n = clean_rows(doc["bars"], breaks=False)
        if n["bad_prints"]:
            notes["bad_prints"][sym] = n["bad_prints"]
        notes["fixed_high_low"] += n["fixed_high_low"]
        notes["filler"] += n["filler"]
        if len(rows) < 30:
            missing[sym] = "under 30 daily bars"
            continue
        uni[sym] = Series(sym, rows, sym in ever_core or uname == "pit100")
        if doc.get("events"):
            events[sym] = doc["events"]
        if doc.get("ended"):
            ended.append([sym, doc["last"]])
        if len(doc.get("symbols") or []) > 1:
            renamed[sym] = doc["symbols"]
    dirs = [d for d in (getattr(a, "offline", None) or "").replace(os.pathsep, ",").split(",") if d]
    bench = {}
    for sym in (NIFTY, BEES, VIX):
        try:
            rows, n = clean_rows(load_rows(sym, dirs), index=sym.startswith("^"))
            if n["bad_prints"]:
                notes["bad_prints"][sym] = n["bad_prints"]
            bench[sym] = Series(sym, rows, True)
        except nse.NSEError as e:
            missing[sym] = str(e)
    if NIFTY not in bench:
        raise SwingError("no Nifty 50 (^NSEI) bars: run `backtest.py swing fetch` first")
    months = sorted(e for e in lists if lo <= e <= hi)
    info = {"universe_file": upath, "method": U.get("method"), "source": U.get("source"),
            "bhavcopy_sessions": U.get("sessions"), "month_ends": len(months),
            "first_list": months[0] if months else None, "last_list": months[-1] if months else None,
            "list_size": max((len(lists[e]) for e in months), default=0),
            "ever_members": len(syms), "ended": sorted(ended, key=lambda x: (x[1], x[0])),
            "ended_count": len(ended), "renamed": renamed,
            "corporate_actions": sum(len(v) for v in events.values()),
            "corporate_action_symbols": len(events), "events": events}
    return uni, bench, missing, notes, mem, info


def bias_rows(pit_doc, biased_doc):
    """Side by side: the same strategy on the survivor-biased universe and on its point-in-time twin."""
    pair = "%s→%s" % (biased_doc["meta"]["universe"], pit_doc["meta"]["universe"])
    out = []
    for n in ORDER:
        a, b = biased_doc["strategies"].get(n), pit_doc["strategies"].get(n)
        if not a or not b:
            continue
        fa, fb = a["full"], b["full"]
        oa, ob = a["walk_forward"]["chosen_out_of_sample"], b["walk_forward"]["chosen_out_of_sample"]
        infl = _r(fa["cagr_pct"] - fb["cagr_pct"]) if None not in (fa["cagr_pct"], fb["cagr_pct"]) else None
        out.append({"strategy": n, "universe_pair": pair, "cagr_biased": fa["cagr_pct"], "cagr_pit": fb["cagr_pct"],
                    "inflation_pp": infl, "oos_biased": oa["cagr_pct"], "oos_pit": ob["cagr_pct"],
                    "oos_inflation_pp": _r(oa["cagr_pct"] - ob["cagr_pct"]) if None not in (oa["cagr_pct"], ob["cagr_pct"]) else None,
                    "max_dd_biased": fa["max_dd_pct"], "max_dd_pit": fb["max_dd_pct"],
                    "sharpe_biased": fa["sharpe"], "sharpe_pit": fb["sharpe"],
                    "excess_biased": a.get("excess_cagr_pct"), "excess_pit": b.get("excess_cagr_pct"),
                    "trades_biased": fa["trades"], "trades_pit": fb["trades"],
                    "delisted_exits_pit": b.get("delisted_exits", 0)})
    return out


def find_biased(pit_doc):
    """Today's survivor-biased twin doc (trader/backtest/<date>-swing-nifty100.json), or None."""
    twin = PIT_UNIVERSES.get(pit_doc["meta"]["universe"])
    if not twin:
        return None
    doc = nse.read_json(os.path.join(out_dir(), "%s-swing-%s.json" % (pit_doc["made"][:10], twin)))
    return doc if doc and doc.get("created_by") == "india-backtest swing" else None


def bias_lines(bias):
    L = ["| strategy | CAGR biased % | CAGR PIT % | inflation pp | max DD biased % | max DD PIT % | Sharpe biased | "
         "Sharpe PIT | OOS biased % | OOS PIT % | excess vs Nifty biased | excess vs Nifty PIT | delisted exits |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in bias["rows"]:
        L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %d |" % (
            r["strategy"], fr(r["cagr_biased"]), fr(r["cagr_pit"]), fr(r["inflation_pp"]),
            fr(r["max_dd_biased"], "%.1f"), fr(r["max_dd_pit"], "%.1f"), fr(r["sharpe_biased"], "%.2f"),
            fr(r["sharpe_pit"], "%.2f"), fr(r["oos_biased"]), fr(r["oos_pit"]), fr(r["excess_biased"]),
            fr(r["excess_pit"]), r["delisted_exits_pit"] or 0))
    return L


# ================================================================ reports

def costs_text(broker):
    b = nse.DELIVERY_CHARGES["brokers"][broker]
    brok = "Rs 0 brokerage" if not b["brokerage_pct"] else (
        "brokerage the lower of Rs %g or %g %% an order (min Rs %g)" % (b["brokerage_cap"], b["brokerage_pct"], b["brokerage_min"]))
    return ("nse.delivery_charges, %s delivery (CNC): %s; STT 0.1 %% on buy and sell; NSE txn 0.0030699 %%; SEBI "
            "Rs 10/crore; stamp 0.015 %% on the buy; GST 18 %% on brokerage + txn + SEBI + DP; DP Rs %.2f + GST per "
            "scrip per sell day (pricing pages checked %s; today's schedule applied to the whole history)"
            % ({"dhan": "Dhan", "angel": "Angel One"}[broker], brok, b["dp_per_scrip"], nse.DELIVERY_CHARGES["checked"]))


def pit_caveats(meta):
    pi = meta["pit"]
    return [
        "POINT-IN-TIME universe (%s): at each month-end, series-EQ stocks (INE ISIN, >= 200 sessions of history, "
        "close >= Rs 20) ranked by median daily traded value over ~6 months; the top %d are buyable on signals "
        "until the next month-end; exits are always allowed. %d stocks were members at some point in %s-%s; %d of "
        "them stopped trading before the end (delisted, merged, suspended) and stay in for the months they "
        "qualified. This is a LIQUIDITY universe, not the official Nifty index history (index reconstitutions "
        "also weigh free float and F&O eligibility), so it is close to, not identical with, the Nifty %s." % (
            meta["universe"], pi["list_size"], pi["ever_members"], meta["from"][:4], meta["to"][:4],
            pi["ended_count"], meta["universe"][3:]),
        "DELISTINGS: a position whose stock stops trading is sold at its LAST available close (%s such exits "
        "across the default runs). That is fair for a merger or buy-out but OPTIMISTIC for a company that failed "
        "or was suspended (a real holder might have sold far lower, or not at all)." % meta.get("delisted_exits_total", 0),
        "Prices are NSE's own bhavcopy OHLC (unadjusted; its PREVCLOSE is NOT adjusted on ex-dates either), "
        "back-adjusted for bonuses, splits, consolidations, rights and demergers listed in NSE's corporate-action "
        "(Bc) files: the purpose's exact ratio when the ex-day gap agrees, else the gap snapped to a clean ratio; "
        "rights / demergers by the ex-day open (or close) / last close; Yahoo's split events where no Bc file "
        "listed the action (%d listed adjustments in %d stocks of this universe). Checked against Yahoo's "
        "split-adjusted closes: 92 %% of the 483 stocks both have line up with no level step > 3 %%; the rest "
        "are demergers Yahoo leaves unadjusted, Yahoo's own year-boundary errors and small rights issues. A "
        "few small-cap actions are still missed (e.g. AGIIL 2025-02-07, DWARKESH 2017-08-10 look like unlisted "
        "splits; some rights issues). NOT dividend-adjusted: returns are PRICE returns, like the Nifty 50 price "
        "index (^NSEI); NIFTYBEES (the ETF) includes reinvested dividends less expenses. Benchmarks are Yahoo's, "
        "bought on %s's close, no costs." % (pi["corporate_actions"], pi["corporate_action_symbols"], meta["base_date"]),
        "Costs: %s. Slippage %.2f %% a side for stocks then in the PIT top 100, %.2f %% for the rest. Taxes are "
        "NOT modelled (STCG / LTCG). Not tax advice." % (
            meta["costs_model"], meta["slippage_pct"]["nifty100"], meta["slippage_pct"]["other"]),
    ] + caveats(meta)[3:5] + [
        "Walk-forward: variants chosen on %s to %s (best Sharpe), reported on %s to %s only. Sharpe uses daily "
        "returns, rf = 0." % (meta["split"]["in_sample"][0], meta["split"]["in_sample"][1],
                              meta["split"]["out_of_sample"][0], meta["split"]["out_of_sample"][1]),
        "A backtest is not a forecast and never places an order. Not financial advice.",
    ]


def caveats(meta):
    return [
        "SURVIVORSHIP BIAS: the universe is today's %s list applied to %s-%s. Stocks that dropped out of the index "
        "(or collapsed: Yes Bank, DHFL, Jet Airways...) are missing and stocks that joined after a big run are in. "
        "Over 10+ years this flatters every long-only strategy, momentum and breakouts most; treat every CAGR here "
        "as an upper bound." % (meta["universe"], meta["from"][:4], meta["to"][:4]),
        "Prices are Yahoo's daily bars, split- and bonus-adjusted, NOT dividend-adjusted: the strategies' returns "
        "leave out dividends received while holding (the Nifty 50 yields roughly 1-1.5 %% a year). The like-for-like "
        "benchmark is the Nifty 50 PRICE index (^NSEI, no dividends); NIFTYBEES (the ETF you can buy) includes "
        "reinvested dividends less its expenses. Benchmarks are bought on %s's close, no costs." % meta["base_date"],
        "Costs: %s. Slippage %.2f %% a side for Nifty 100 stocks, %.2f %% for the rest. Taxes are NOT modelled: "
        "short-term (STCG) and long-term (LTCG) capital gains tax apply to every winning sale and would cut a "
        "high-turnover strategy's return far more than buy-and-hold's. Not tax advice."
        % (meta["costs_model"], meta["slippage_pct"]["nifty100"], meta["slippage_pct"]["other"]),
        "Fills: signals on the close, entries at the next open + slippage; stops on the day's low, a gap through "
        "the stop at the open; close-based and time exits at the next open. No circuit limits, no liquidity "
        "check beyond index membership; a 20 % upper circuit could make an entry impossible in real life.",
        "Portfolio: %s, at most %d positions, %s sizing, compounding; cash earns 0 (a liquid fund would add "
        "~6 %% a year on idle cash, which favours low-exposure strategies here)." % (
            nse.inr(meta["capital"], 0), meta["max_positions"], meta["sizing"]),
        "Walk-forward: variants chosen on %s to %s (best Sharpe), reported on %s to %s only. Sharpe uses daily "
        "returns, rf = 0." % (meta["split"]["in_sample"][0], meta["split"]["in_sample"][1],
                              meta["split"]["out_of_sample"][0], meta["split"]["out_of_sample"][1]),
        "Data: %d of %d symbols loaded; bad prints dropped: %s; corporate-action breaks back-adjusted (an open "
        "35 %%+ from the last close, impossible under NSE's price bands: demergers / unadjusted splits): %s. "
        "Stocks listed after the start join once their indicators have enough history." % (
            len(meta["symbols"]), meta["symbols_requested"], ", ".join(
                "%s %s" % (k, ",".join(v)) for k, v in sorted(meta["data_notes"]["bad_prints"].items())) or "none",
            ", ".join("%s %s (x%s, %s)" % (k, b[0], b[1], b[2]) for k, v in sorted(meta["data_notes"]["breaks"].items())
                      for b in v) or "none"),
        "A backtest is not a forecast and never places an order. Not financial advice.",
    ]


def fr(x, fmt="%+.1f"):
    return "-" if x is None else fmt % x


def table(doc):
    m = doc["meta"]
    head = "%-18s %7s %6s %6s %6s %6s %7s %6s %6s %7s %7s %7s  %s" % (
        "strategy", "CAGR%", "maxDD%", "Sharpe", "trades", "win%", "avg%", "hold", "expo%", "OOS%", "xsNifty",
        "angel%", "verdict")
    L = ["Swing backtest: %s, %d stocks, %s to %s (%d sessions); %s, max %d positions, %s sizing, broker %s."
         % (m["universe"], len(m["symbols"]), m["from"], m["to"], m["sessions"], nse.inr(m["capital"], 0),
            m["max_positions"], m["sizing"], m["broker"]), head, "-" * len(head)]
    for bname in ("bench_nifty50", "bench_niftybees"):
        b = doc["benchmarks"].get(bname)
        if b:
            f, o = b["full"], b["out_of_sample"]
            L.append("%-18s %7s %6s %6s %6s %6s %7s %6s %6s %7s %7s %7s  %s" % (
                bname, fr(f["cagr_pct"]), fr(f["max_dd_pct"], "%.1f"), fr(f["sharpe"], "%.2f"), "-", "-", "-", "-",
                "100", fr(o["cagr_pct"]), "-", "-", "buy and hold, no costs"))
    for n in doc["ranking"]:
        r = doc["strategies"][n]
        f, o = r["full"], r["walk_forward"]["chosen_out_of_sample"]
        L.append("%-18s %7s %6s %6s %6d %6s %7s %6s %6s %7s %7s %7s  %s" % (
            n, fr(f["cagr_pct"]), fr(f["max_dd_pct"], "%.1f"), fr(f["sharpe"], "%.2f"), f["trades"],
            fr(f["win_pct"], "%.1f"), fr(f["avg_trade_pct"], "%+.2f"), fr(f["avg_hold_days"], "%.1f"),
            fr(f["exposure_pct"], "%.0f"), fr(o["cagr_pct"]), fr(r["excess_cagr_pct"]),
            fr((r.get("other_broker") or {}).get("cagr_pct")), r["verdict"]))
    L.append("(Default variant over the whole period, after costs. maxDD% = largest peak-to-trough fall of the "
             "account; OOS% = CAGR of the variant chosen on the in-sample years, out of sample only; xsNifty = CAGR "
             "minus the Nifty 50 price index's; angel% = the same default variant on Angel One's charges.)")
    for n in doc["ranking"]:
        L.append("  %-18s vs index: %s" % (n, doc["strategies"][n]["vs_index"]))
    return L


def strategy_lines(name, r, show_variants=False):
    f, wf = r["full"], r["walk_forward"]
    L = ["", "%s: %s  [%s]" % (name, r["title"], r["default_variant"]), "  Rules: " + r["rules"],
         "  Ranked by: " + r["score"]]
    if f["trades"]:
        L += ["  CAGR %s %% (total %s %%), max drawdown %s %% lasting %s days (peak %s, trough %s, recovered %s), "
              "Sharpe %s, exposure %s %%." % (fr(f["cagr_pct"]), fr(f["total_return_pct"], "%+.0f"),
                                              fr(f["max_dd_pct"], "%.1f"), f["max_dd_days"], f["max_dd_peak"],
                                              f["max_dd_trough"], f["max_dd_recovered"] or "not yet",
                                              fr(f["sharpe"], "%.2f"), fr(f["exposure_pct"], "%.0f")),
              "  %d trades, win %s %%, average %s %% a trade after costs, held %s sessions; profit factor %s; t=%s."
              % (f["trades"], fr(f["win_pct"], "%.1f"), fr(f["avg_trade_pct"], "%+.2f"), fr(f["avg_hold_days"], "%.1f"),
                 fr(f["profit_factor"], "%.2f"), fr(f["t_stat"], "%.1f")),
              "  Exits: " + ", ".join("%s %d" % kv for kv in (f.get("outcomes") or {}).items()),
              "  Costs: charges %s + slippage %s = %s %% of the closed trades' gross profit. Before costs %s %% a "
              "year; on %s's charges %s %% a year." % (
                  nse.inr(f["charges_inr"], 0), nse.inr(f["slippage_inr"], 0), fr(f["costs_pct_of_gross"], "%.0f"),
                  fr((r.get("before_costs") or {}).get("cagr_pct")), (r.get("other_broker") or {}).get("broker", "-"),
                  fr((r.get("other_broker") or {}).get("cagr_pct")))]
        if r.get("other_sizing"):
            L.append("  %s sizing instead: CAGR %s %%, max drawdown %s %%, Sharpe %s." % (
                r["other_sizing"]["sizing"], fr(r["other_sizing"]["cagr_pct"]),
                fr(r["other_sizing"]["max_dd_pct"], "%.1f"), fr(r["other_sizing"]["sharpe"], "%.2f")))
        L.append("  Best year %s %%, worst %s %%. By year: %s" % (
            fr(f["best_year_pct"]), fr(f["worst_year_pct"]),
            "; ".join("%s %s (Nifty %s)" % (y["year"], fr(y["return_pct"], "%+.0f"), fr(y["bench_return_pct"], "%+.0f"))
                      for y in f["by_year"])))
        L.append("  By regime (annualised): " + "; ".join(
            "%s %dd %s %% (Nifty %s)" % (x["regime"], x["days"], fr(x["return_pct_ann"], "%+.0f"),
                                         fr(x["bench_return_pct_ann"], "%+.0f")) for x in f["by_regime"]))
    else:
        L.append("  Never fired.")
    ci, co = wf["chosen_in_sample"], wf["chosen_out_of_sample"]
    L.append("  Walk-forward: %s to %s picked [%s] (%s; in-sample CAGR %s %%, Sharpe %s, %s trades); out of sample "
             "%s to %s: CAGR %s %%, max drawdown %s %%, %s trades (default: %s %%; Nifty 50 %s %%)." % (
                 wf["in_sample"][0], wf["in_sample"][1], wf["chosen"], wf["chosen_by"], fr(ci["cagr_pct"]),
                 fr(ci["sharpe"], "%.2f"), ci["trades"], wf["out_of_sample"][0], wf["out_of_sample"][1],
                 fr(co["cagr_pct"]), fr(co["max_dd_pct"], "%.1f"), co["trades"],
                 fr(wf["default_out_of_sample"]["cagr_pct"]), fr(wf["bench_out_of_sample"].get("cagr_pct"))))
    if show_variants:
        L.append("  %-42s %7s %6s %6s %6s %7s %7s" % ("variant", "CAGR%", "maxDD%", "Sharpe", "trades", "IS Shrp", "OOS%"))
        for v in r["variants"]:
            vf = v["full"]
            L.append("  %-42s %7s %6s %6s %6s %7s %7s" % (
                v["label"], fr(vf["cagr_pct"]), fr(vf["max_dd_pct"], "%.1f"), fr(vf["sharpe"], "%.2f"), vf["trades"],
                fr(v["in_sample"]["sharpe"], "%.2f"), fr(v["out_of_sample"]["cagr_pct"])))
    if r["skipped"]:
        L.append("  Signals not taken: " + "; ".join("%s x%d" % kv for kv in sorted(r["skipped"].items(), key=lambda kv: -kv[1])))
    L.append("  Verdict: " + r["verdict"])
    L.append("  Vs the index: " + r["vs_index"])
    return L


def markdown(doc):
    m = doc["meta"]
    bias_note = ("**Survivorship bias: today's index list applied to the past. Taxes not modelled. Never places an "
                 "order. Not financial advice.**")
    if m.get("pit"):
        bias_note = ("**Point-in-time universe: month-end top %d by traded value from NSE bhavcopies, delisted and "
                     "merged stocks included (%d of %d members stopped trading). Taxes not modelled. Never places "
                     "an order. Not financial advice.**" % (m["pit"]["list_size"], m["pit"]["ended_count"],
                                                            m["pit"]["ever_members"]))
    L = ["# Swing backtest: %s" % m["universe"], "",
         "%d stocks, %s to %s (%d sessions). %s start, max %d positions, %s sizing, %s charges. Made %s. %s" % (
             len(m["symbols"]), m["from"], m["to"], m["sessions"], nse.inr(m["capital"], 0), m["max_positions"],
             m["sizing"], m["broker"], doc["made"], bias_note), "",
         "| strategy | CAGR % | max DD % | DD days | Sharpe | trades | win % | avg trade % | hold days | exposure % | "
         "OOS CAGR % | excess vs Nifty % | verdict | vs the index |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for bname in ("bench_nifty50", "bench_niftybees"):
        b = doc["benchmarks"].get(bname)
        if b:
            f = b["full"]
            L.append("| %s (%s) | %s | %s | %s | %s | - | - | - | - | 100 | %s | - | buy and hold | - |" % (
                bname, b["symbol"], fr(f["cagr_pct"]), fr(f["max_dd_pct"], "%.1f"), f["max_dd_days"],
                fr(f["sharpe"], "%.2f"), fr(b["out_of_sample"]["cagr_pct"])))
    for n in doc["ranking"]:
        r = doc["strategies"][n]
        f, o = r["full"], r["walk_forward"]["chosen_out_of_sample"]
        L.append("| %s | %s | %s | %s | %s | %d | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            n, fr(f["cagr_pct"]), fr(f["max_dd_pct"], "%.1f"), f["max_dd_days"], fr(f["sharpe"], "%.2f"), f["trades"],
            fr(f["win_pct"], "%.1f"), fr(f["avg_trade_pct"], "%+.2f"), fr(f["avg_hold_days"], "%.1f"),
            fr(f["exposure_pct"], "%.0f"), fr(o["cagr_pct"]), fr(r["excess_cagr_pct"]), r["verdict"], r["vs_index"]))
    if doc.get("bias"):
        b = doc["bias"]
        L += ["", "## Survivorship: %s (same rules, same dates)" % b["pair"], "",
              "Biased = today's index list applied to the past (Yahoo prices); PIT = the point-in-time universe "
              "(bhavcopy prices). inflation pp = biased CAGR minus PIT CAGR.", ""] + bias_lines(b)
    if m.get("pit"):
        pi = m["pit"]
        L += ["", "## Point-in-time universe", "",
              "- Method: %s" % pi["method"], "- Source: %s; %s" % (pi["source"], pi["bhavcopy_sessions"]),
              "- %d month-end lists (%s to %s); %d stocks ever members; %d stopped trading before the end: %s" % (
                  pi["month_ends"], pi["first_list"], pi["last_list"], pi["ever_members"], pi["ended_count"],
                  ", ".join("%s (%s)" % (s, d) for s, d in pi["ended"])),
              "- Renamed (one chain each, newest symbol): %s" % (", ".join(
                  "%s" % "->".join(v) for k, v in sorted(pi["renamed"].items())) or "none"),
              "- Corporate-action adjustments (NSE Bc corporate-action files, Yahoo split events as a fallback): %d in %d stocks." % (
                  pi["corporate_actions"], pi["corporate_action_symbols"]),
              "- Delisted exits (default variants): %s" % (", ".join(
                  "%s %d" % (n, doc["strategies"][n].get("delisted_exits", 0)) for n in doc["ranking"]))]
    L += ["", "## Rules", ""] + ["- **%s** (%s): %s Ranked by %s." % (n, doc["strategies"][n]["title"],
                                                                     doc["strategies"][n]["rules"],
                                                                     doc["strategies"][n]["score"]) for n in doc["ranking"]]
    L += ["", "## By year (default variants, %)", "",
          "| year | " + " | ".join(doc["ranking"]) + " | Nifty 50 | NIFTYBEES |",
          "|---|" + "---|" * (len(doc["ranking"]) + 2)]
    years = sorted(doc["benchmarks"]["bench_nifty50"]["by_year"])
    for y in years:
        row = []
        for n in doc["ranking"]:
            v = dict((x["year"], x["return_pct"]) for x in doc["strategies"][n]["full"]["by_year"]).get(y)
            row.append(fr(v, "%+.1f"))
        bb = doc["benchmarks"].get("bench_niftybees", {}).get("by_year", {}).get(y)
        L.append("| %s | %s | %s | %s |" % (y, " | ".join(row), fr(doc["benchmarks"]["bench_nifty50"]["by_year"].get(y), "%+.1f"),
                                           fr(bb, "%+.1f")))
    L += ["", "## Details", "", "```"]
    for n in doc["ranking"]:
        L += strategy_lines(n, doc["strategies"][n], True)
    L += ["```", "", "## Caveats", ""] + ["- " + c for c in m["caveats"]]
    return "\n".join(L) + "\n"


# ================================================================ dashboard JSON

def dash(docs, made):
    """Flat rows for a dashboard, from one or more universes' docs; under DASH_BYTES."""
    for step in (1, 2, 3, 4, 8):
        out = _dash(docs, made, step)
        size = len(json.dumps(out, separators=(",", ":")))
        if size <= DASH_BYTES:
            break
    out["sw_meta"][0]["equity_sampling"] = "every %d week(s): the last session of the week" % step
    return out


def _dash(docs, made, step):
    S, E, Y, R, B = [], [], [], [], []
    seen_bench = {}
    for doc in docs:
        m = doc["meta"]
        u = m["universe"]
        B += [dict((k, r[k]) for k in ("strategy", "universe_pair", "cagr_biased", "cagr_pit", "inflation_pp",
                                       "oos_biased", "oos_pit", "max_dd_biased", "max_dd_pit", "sharpe_biased",
                                       "sharpe_pit", "excess_biased", "excess_pit", "delisted_exits_pit"))
              for r in (doc.get("bias") or {}).get("rows", [])]
        nb = doc["benchmarks"].get("bench_nifty50", {})
        bb = doc["benchmarks"].get("bench_niftybees", {})
        for n in doc["ranking"]:
            r = doc["strategies"][n]
            f, o = r["full"], r["walk_forward"]["chosen_out_of_sample"]
            S.append({"id": "%s-%s" % (u, n), "universe": u, "strategy": n, "title": r["title"], "rules": r["rules"],
                      "variant": r["default_variant"], "cagr_pct": f["cagr_pct"], "total_return_pct": f["total_return_pct"],
                      "max_dd_pct": f["max_dd_pct"], "max_dd_days": f["max_dd_days"], "sharpe": f["sharpe"],
                      "exposure_pct": f["exposure_pct"], "trades": f["trades"], "win_pct": f["win_pct"],
                      "avg_trade_pct": f["avg_trade_pct"], "avg_hold_days": f["avg_hold_days"],
                      "profit_factor": f["profit_factor"], "best_year_pct": f["best_year_pct"],
                      "worst_year_pct": f["worst_year_pct"], "oos_cagr_pct": o["cagr_pct"],
                      "oos_max_dd_pct": o["max_dd_pct"], "oos_variant": r["walk_forward"]["chosen"],
                      "bench_cagr_pct": nb.get("full", {}).get("cagr_pct"), "excess_cagr_pct": r["excess_cagr_pct"],
                      "bench_bees_cagr_pct": bb.get("full", {}).get("cagr_pct"),
                      "excess_vs_bees_pct": _r(f["cagr_pct"] - bb["full"]["cagr_pct"]) if bb and f["cagr_pct"] is not None
                      else None,
                      "gross_cagr_pct": (r.get("before_costs") or {}).get("cagr_pct"),
                      "other_broker_cagr_pct": (r.get("other_broker") or {}).get("cagr_pct"),
                      "costs_pct_of_gross": f["costs_pct_of_gross"], "verdict": r["verdict"], "vs_index": r["vs_index"]})
            for d, v in thin(r["equity_weekly"], step):
                E.append({"universe": u, "strategy": n, "date": d, "equity_inr": round(v)})
            for y in f["by_year"]:
                Y.append({"universe": u, "strategy": n, "year": y["year"], "return_pct": y["return_pct"],
                          "bench_return_pct": y["bench_return_pct"]})
            for x in f["by_regime"]:
                R.append({"universe": u, "strategy": n, "regime": x["regime"], "days": x["days"],
                          "return_pct_ann": x["return_pct_ann"], "bench_return_pct_ann": x["bench_return_pct_ann"]})
        for bname, b in doc["benchmarks"].items():
            sig = (bname, json.dumps(b["equity_weekly"]))
            if sig not in seen_bench:                   # the same curve as another universe's: written once
                seen_bench[sig] = u
                for d, v in thin(b["equity_weekly"], step):
                    E.append({"universe": u, "strategy": bname, "date": d, "equity_inr": round(v)})
            for y, v in sorted(b["by_year"].items()):
                Y.append({"universe": u, "strategy": bname, "year": y, "return_pct": v,
                          "bench_return_pct": nb.get("by_year", {}).get(y)})
            for x in b["by_regime"]:
                R.append({"universe": u, "strategy": bname, "regime": x["regime"], "days": x["days"],
                          "return_pct_ann": x["return_pct_ann"], "bench_return_pct_ann": None})
    metas = [d["meta"] for d in docs]
    meta = {"made": made, "universes": ", ".join(m["universe"] for m in metas),
            "period": "; ".join("%s %s to %s (%d sessions)" % (m["universe"], m["from"], m["to"], m["sessions"]) for m in metas),
            "walk_forward": "; ".join("%s in-sample %s to %s, out of sample %s to %s" % (
                m["universe"], m["split"]["in_sample"][0], m["split"]["in_sample"][1], m["split"]["out_of_sample"][0],
                m["split"]["out_of_sample"][1]) for m in metas),
            "symbols": "; ".join("%s %d of %d loaded" % (m["universe"], len(m["symbols"]), m["symbols_requested"]) for m in metas),
            "data_source": ("Yahoo chart JSON, daily bars (.NS), split/bonus-adjusted OHLC, not dividend-adjusted; "
                            "benchmarks ^NSEI (Nifty 50 price index, no dividends) and NIFTYBEES.NS (Nifty 50 ETF, "
                            "dividends reinvested in its price, expenses deducted); India VIX ^INDIAVIX"),
            "costs_model": metas[0]["costs_model"] if metas else "",
            "fills": ("signal on the close, entry at the next open + slippage (0.10 % Nifty 100, 0.25 % others); stops "
                      "on the daily low, a gap through the stop at the open; targets as limits; close/time exits at "
                      "the next open - slippage"),
            "sizing": ("%s start, max %d positions, equal weight (equity / max positions), compounding, cash earns 0; "
                       "risk sizing (1 %% of equity to the stop, capped at equal weight) reported alongside"
                       % (nse.inr(metas[0]["capital"], 0) if metas else "-", metas[0]["max_positions"] if metas else 0)),
            "survivorship_note": ("Survivorship bias: today's Nifty 100 / 500 lists are applied to 2015-2026; stocks that "
                                  "left the index or collapsed are missing. Every strategy CAGR is an upper bound; the "
                                  "Nifty 500 run is a partial check (a wider net, still today's survivors)."),
            "taxes": "STCG / LTCG not modelled (not tax advice)",
            "caveats": metas[0]["caveats"] if metas else []}
    pits = [m for m in metas if m.get("pit")]
    if pits:
        meta["survivorship_note"] = (
            "nifty100 / nifty500 rows: today's index lists applied to 2015-2026 (survivor-biased, Yahoo prices). "
            "pit100 / pit500 rows: point-in-time universes, the month-end top 100 / 500 series-EQ stocks by 6-month "
            "median traded value from NSE's daily bhavcopies, delisted and merged stocks included (exited at their "
            "last close: optimistic for failures). sw_bias holds the inflation per strategy (biased minus PIT CAGR).")
        meta["data_source"] += ("; pit100 / pit500: NSE CM bhavcopy (legacy cm*bhav.csv.zip to 2024-07-05, UDiFF "
                                "after), back-adjusted from NSE's corporate-action (Bc) files, not dividend-adjusted")
        meta["pit"] = "; ".join("%s: %d lists %s to %s, %d stocks ever members, %d stopped trading, %d corporate-action "
                                "adjustments" % (m["universe"], m["pit"]["month_ends"], m["pit"]["first_list"],
                                                 m["pit"]["last_list"], m["pit"]["ever_members"], m["pit"]["ended_count"],
                                                 m["pit"]["corporate_actions"]) for m in pits)
        meta["pit_caveats"] = pits[0]["caveats"]
    if len(docs) > len(set(seen_bench.values())):
        meta["equity_note"] = ("a benchmark equity curve identical across universes appears once in sw_equity, under "
                               "the first universe that has it (%s); sw_years / sw_regimes keep every universe's rows"
                               % ", ".join(sorted(set(seen_bench.values()))))
    out = {"made": made, "sw_strategies": S, "sw_equity": E, "sw_years": Y, "sw_regimes": R, "sw_meta": [meta]}
    if B:
        out["sw_bias"] = B
    return out


def thin(rows, step):
    if step <= 1:
        return rows
    out = rows[::step]
    if rows and out[-1] != rows[-1]:
        out.append(rows[-1])
    return out


# ================================================================ commands

def get_now(a):
    return nse.parse_now(a.now) if getattr(a, "now", None) else nse.now_ist().replace(microsecond=0)


def out_dir():
    return os.path.join(nse.trader_dir(), "backtest")


def compute(a, names):
    now = get_now(a)
    d_from = a.date_from or "%d-01-01" % (now.year - a.years + 1)
    cfg = {"capital": a.capital, "max_positions": a.max_positions, "sizing": a.sizing, "risk_pct": a.risk_pct,
           "broker": a.broker, "slip_core": a.slippage_core, "slip_other": a.slippage_other}
    pit = None
    if (a.universe or "").lower() in PIT_UNIVERSES:
        uname = a.universe.lower()
        uni, bench, missing, dnotes, mem, pit = load_pit(uname, a, d_from, a.date_to)
        symbols, notes = sorted(set(uni) | set(k for k in missing if not k.startswith("^") and k != BEES)), []
        core_note = ("slippage %.2f %% while a stock is in that month's PIT top 100, %.2f %% otherwise"
                     % (a.slippage_core, a.slippage_other))
        cfg.update(membership=mem, delist_exit=True)
    else:
        uname, symbols, notes = bt.resolve_universe(a.universe, a.list_file)
        core, core_note = core_symbols(uname, symbols, a)
        uni, bench, missing, dnotes = load_all(symbols, a, core)
    if not uni:
        raise SwingError("no daily bars for any of %s: run `backtest.py swing fetch --universe %s` first"
                         % (", ".join(symbols[:8]), a.universe))
    log = (lambda msg: print(msg, file=sys.stderr)) if not getattr(a, "quiet", False) else None
    results, meta, benches = backtest(uni, names, bench, cfg, d_from, a.date_to, with_trades=True,
                                      extras=not a.no_extras, log=log)
    meta.update({"universe": uname, "symbols": sorted(uni), "symbols_requested": len(symbols),
                 "symbols_missing": dict((k, v) for k, v in missing.items() if k in symbols),
                 "benchmarks_missing": dict((k, v) for k, v in missing.items() if k not in symbols),
                 "core_note": core_note, "notes": notes, "data_notes": dnotes,
                 "source": "offline files" if a.offline else "Yahoo chart JSON, daily (.NS), trader/cache/daily",
                 "costs_model": costs_text(a.broker),
                 "first_bar": dict((s, S.dates[0]) for s, S in sorted(uni.items())),
                 "now": nse.iso_ist(now)})
    meta.pop("_cal", None)
    if pit is not None:
        meta["source"] = ("NSE CM bhavcopy, adjusted from NSE's corporate-action files (%s); benchmarks Yahoo" % pit["universe_file"])
        meta["pit"] = pit
        meta["delisted_exits_total"] = sum(r.get("delisted_exits", 0) for r in results.values())
        meta["caveats"] = pit_caveats(meta)
    else:
        meta["caveats"] = caveats(meta)
    for b in benches.values():
        b.pop("_eq", None)
    for r in results.values():
        r.pop("_eq", None)
    return {"created_by": "india-backtest swing", "made": nse.iso_ist(now), "meta": meta, "benchmarks": benches,
            "strategies": results, "ranking": ranking(results)}


def write_doc(doc):
    stem = "%s-swing-%s" % (doc["made"][:10], doc["meta"]["universe"])
    path = os.path.join(out_dir(), stem + ".json")
    os.makedirs(out_dir(), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, path)
    with open(os.path.join(out_dir(), stem + ".md"), "w", encoding="utf-8") as fh:
        fh.write(markdown(doc))
    return path


def write_dash(made):
    """Rebuild <date>-swing-dash.json from every <date>-swing-<universe>.json written that day."""
    day = made[:10]
    docs = []
    for f in sorted(os.listdir(out_dir())):
        if f.startswith(day + "-swing-") and f.endswith(".json") and not f.endswith("-dash.json"):
            doc = nse.read_json(os.path.join(out_dir(), f))
            if doc and doc.get("created_by") == "india-backtest swing":
                docs.append(doc)
    order = {"nifty100": 0, "nifty500": 1, "pit100": 2, "pit500": 3}
    docs.sort(key=lambda d: (order.get(d["meta"]["universe"], 9), d["meta"]["universe"]))
    d = dash(docs, made)
    path = os.path.join(out_dir(), "%s-swing-dash.json" % day)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, path)
    return path, os.path.getsize(path)


def cmd_list(a):
    print("Swing strategies (long only, daily bars, signal on the close, entry at the next open):")
    for n in ORDER:
        s = STRATEGIES[n]
        print("\n%s: %s\n  %s\n  Ranked by: %s\n  %d variants: %s" % (
            n, s["title"], s["rules"], s["score"], len(s["variants"]),
            "; ".join(variant_label(n, v) for v in s["variants"])))
    print("\nBenchmarks: bench_nifty50 (^NSEI, price index) and bench_niftybees (NIFTYBEES ETF), buy and hold.")
    return 0


def cmd_fetch(a):
    now = get_now(a)
    uname, symbols, notes = bt.resolve_universe(a.universe, a.list_file)
    todo = [NIFTY, BEES, VIX] + symbols
    if a.limit:
        todo = todo[:a.limit + 3]
    log = lambda msg: print(msg, file=sys.stderr)  # noqa: E731
    got = {"fetched": 0, "cached": 0, "failed": {}}
    log("swing fetch: %s, %d symbols + benchmarks, daily bars from %s" % (uname, len(symbols), fetch_from(now, a.years)))
    for k, sym in enumerate(todo):
        try:
            how, doc = fetch_symbol(sym, now, a.years, a.refresh, log)
            got[how] += 1
            if how == "fetched":
                log("  [%d/%d] %s: %d bars %s to %s%s" % (k + 1, len(todo), sym, len(doc["bars"]), doc["first"],
                                                         doc["last"], (" splits %s" % doc["splits"]) if doc["splits"] else ""))
        except nse.NSEError as e:
            got["failed"][sym] = str(e)[:120]
            log("  [%d/%d] %s: FAILED %s" % (k + 1, len(todo), sym, str(e)[:100]))
    print("swing fetch %s: %d fetched, %d already cached, %d failed%s" % (
        uname, got["fetched"], got["cached"], len(got["failed"]),
        (": " + "; ".join("%s (%s)" % kv for kv in sorted(got["failed"].items())[:10])) if got["failed"] else ""))
    for n in notes:
        print("Note: " + n)
    return 0 if not got["failed"] or got["fetched"] + got["cached"] else 1


def cmd_run(a):
    names = parse_strategies(a.strategy)
    doc = compute(a, names)
    path = write_doc(doc) if a.write else None
    if a.json:
        print(json.dumps(doc, indent=1, default=str))
        return 0
    L = table(doc)
    for n in names:
        L += strategy_lines(n, doc["strategies"][n], a.variants)
    L += ["", "Caveats:"] + ["  - " + c for c in doc["meta"]["caveats"]]
    if path:
        L.append("Wrote %s (and the .md beside it)." % path)
    print("\n".join(L))
    return 0


def cmd_compare(a):
    names = parse_strategies(a.strategy or "all")
    doc = compute(a, names)
    twin = find_biased(doc) if doc["meta"].get("pit") else None
    if twin:
        doc["bias"] = {"pair": "%s→%s" % (twin["meta"]["universe"], doc["meta"]["universe"]),
                       "biased_made": twin["made"], "rows": bias_rows(doc, twin)}
    L = table(doc)
    if doc.get("bias"):
        L += ["", "Survivorship, %s (CAGR %%, biased -> PIT):" % doc["bias"]["pair"]] + [
            "  %-18s %7s -> %7s  inflation %6s pp; OOS %7s -> %7s" % (
                r["strategy"], fr(r["cagr_biased"]), fr(r["cagr_pit"]), fr(r["inflation_pp"]), fr(r["oos_biased"]),
                fr(r["oos_pit"])) for r in doc["bias"]["rows"]]
    if a.variants:
        for n in doc["ranking"]:
            L += strategy_lines(n, doc["strategies"][n], True)
    L += ["", "Caveats:"] + ["  - " + c for c in doc["meta"]["caveats"]]
    if not a.no_write:
        path = write_doc(doc)
        dpath, size = write_dash(doc["made"])
        L.append("Wrote %s (+ .md) and %s (%d KB)." % (path, dpath, size // 1024))
    print("\n".join(L))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="backtest.py swing",
                                 description="Swing (multi-day) backtest on NSE daily bars, delivery costs, vs the "
                                             "Nifty. Never places an order.")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list", help="the strategies, rules, scores and variants")
    p = sub.add_parser("fetch", help="cache Yahoo daily bars for a universe + ^NSEI, NIFTYBEES, ^INDIAVIX")
    p.add_argument("--universe", default="nifty100", help="nifty50 | nifty100 | nifty500 | FILE | SYM,SYM")
    p.add_argument("--years", type=int, default=YEARS, help="years of simulation history (default 12: from 2015)")
    p.add_argument("--list-file", help="an NSE index list CSV instead of downloading it")
    p.add_argument("--refresh", action="store_true", help="refetch even when the cache is fresh")
    p.add_argument("--limit", type=int, help="only the first N symbols (testing)")
    p.add_argument("--now")

    def common(p, strategy_default):
        p.add_argument("--strategy", default=strategy_default, help="pullback_rsi2[,...] or all")
        p.add_argument("--universe", default="nifty100", help="nifty50 | nifty100 | nifty500 | pit100 | pit500 (point-in-time) | FILE | SYM,SYM")
        p.add_argument("--years", type=int, default=YEARS, help="simulate from Jan 1 of (this year - N + 1)")
        p.add_argument("--from", dest="date_from", help="first session, YYYY-MM-DD")
        p.add_argument("--to", dest="date_to", help="last session, YYYY-MM-DD")
        p.add_argument("--capital", type=float, default=DEFAULTS["capital"])
        p.add_argument("--max-positions", type=int, default=DEFAULTS["max_positions"])
        p.add_argument("--sizing", choices=["equal", "risk"], default="equal")
        p.add_argument("--risk-pct", type=float, default=DEFAULTS["risk_pct"])
        p.add_argument("--broker", choices=["dhan", "angel"], default="dhan")
        p.add_argument("--slippage-core", type=float, default=DEFAULTS["slip_core"], help="%% a side, Nifty 100 stocks")
        p.add_argument("--slippage-other", type=float, default=DEFAULTS["slip_other"], help="%% a side, the rest")
        p.add_argument("--offline", metavar="DIR[,DIR]", help="recorded Yahoo charts / cache docs, no network")
        p.add_argument("--list-file", help="an NSE index list CSV instead of downloading it")
        p.add_argument("--core-list", help="the Nifty 100 list CSV that decides the 0.10 %% slippage")
        p.add_argument("--no-extras", action="store_true", help="skip the before-costs / other-broker / other-sizing runs")
        p.add_argument("--variants", action="store_true")
        p.add_argument("--json", action="store_true")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--now")
    p = sub.add_parser("run")
    common(p, "pullback_rsi2")
    p.add_argument("--write", action="store_true", help="save trader/backtest/<date>-swing-<universe>.json + .md")
    p = sub.add_parser("compare", help="all strategies vs the benchmarks; writes the JSON, .md and dash JSON")
    common(p, "all")
    p.add_argument("--no-write", action="store_true")
    sub.add_parser("pit-fetch", help="download NSE's daily CM bhavcopies (pit.py; resumable)", add_help=False)
    sub.add_parser("pit-build", help="build the PIT universe + adjusted series from them (pit.py)", add_help=False)
    sub.add_parser("pit-check", help="compare the PIT series with Yahoo's split-adjusted closes", add_help=False)
    if argv is None:
        argv = sys.argv[1:]
    if argv[:1] and argv[0] in ("pit-fetch", "pit-build", "pit-check"):
        import pit  # noqa: E402  (india-backtest/pit.py)
        try:
            return pit.main(argv)
        except nse.NSEError as e:
            print("backtest.py swing: %s" % e, file=sys.stderr)
            return 1
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    if getattr(a, "max_positions", 1) < 1:
        print("backtest.py swing: --max-positions must be at least 1", file=sys.stderr)
        return 1
    try:
        return {"list": cmd_list, "fetch": cmd_fetch, "run": cmd_run, "compare": cmd_compare}[a.cmd](a)
    except nse.NSEError as e:
        print("backtest.py swing: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
