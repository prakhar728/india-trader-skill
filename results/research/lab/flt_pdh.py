"""Regime / context filters on the previous-day-high breakout (india-backtest's pdh, default variant).

Pre-registered filters (one at a time; the base rule's parameters are frozen at backtest.py's
defaults, chosen before any data: swing stop, 2R). Each grid value is one trial.
"""

META = {
    "family": "regime-filter",
    "kind": "intraday",
    "side": "long",
    "rationale": (
        "The PDH breakout lost -0.15R gross / -0.43R net on the Nifty 50 (Yahoo, 60 days) and -0.20R gross on the "
        "Tata group: the worst of the long breakout rules. A filter can only help if it removes losers for a "
        "REASON, so five context filters are pre-registered, each with a mechanism, and tested one at a time "
        "against the unfiltered rule on the same data: vix_fall (India VIX below its previous close at the "
        "decision bar: implied vol being sold means the market is adding risk, the follow-through a breakout "
        "needs); trend_up (yesterday's close above the stock's 20-day SMA: a new high in an uptrend is "
        "continuation, in a downtrend it is a bear-market rally sold by trapped holders at yesterday's high); "
        "gap_small (|open gap| < 0.5 %: a big gap has already discounted the news and gap-faders sell into it); "
        "sector_agree (the equal-weight move of the stock's NSE industry peers since yesterday's close is "
        "positive at the decision bar: a breakout the sector shares is information, one the sector does not "
        "share is idiosyncratic noise); or_narrow (15-minute opening-range width at most 0.3 x the 14-day daily "
        "ATR: compression precedes expansion). The honest expectation is that most filters change little: the "
        "base rule's gross expectancy is clearly negative and the filters are coarse."),
    "rules": (
        "Base rule (pdh default): buy the close of the first bar (closing 09:30-14:30) that crosses above the "
        "previous session's high: it closes above it and the previous (non-missing) bar closed at or below it. "
        "Only the first such crossing in the window is eligible (a crossing the filter rejects is not retried). "
        "Stop at the lowest low of the trigger bar and the two bars before it, target 2R, flat by 15:05, one "
        "trade per stock per day. Filter (grid): none | vix_fall (VIX last closed bar < VIX previous close) | "
        "trend_up (prev close > SMA20 of daily closes) | gap_small (|09:15 open / prev close - 1| < 0.5 %) | "
        "sector_agree (mean of peers' close-now / prev-close - 1 > 0; no peers = no trade) | or_narrow (09:15-"
        "09:30 range high - low <= 0.3 x daily ATR(14)). A trade is skipped when the filter fails or cannot be "
        "evaluated."),
    "default": {"filter": "none"},
    "grid": {"filter": ["none", "vix_fall", "trend_up", "gap_small", "sector_agree", "or_narrow"]},
    "max_trades_per_day": 1,
}

OR_BARS = 3
R_MULT = 2.0
ATR_FRAC = 0.3
GAP_MAX = 0.5


def _daily(day):
    st = day.state
    if "daily" not in st:
        st["daily"] = list(day.daily)
    return st["daily"]


def _daily_atr(rows, n=14):
    rows = rows[-(n + 1):]
    if len(rows) < n + 1:
        return None
    trs = []
    for i in range(1, len(rows)):
        pc = rows[i - 1]["c"]
        b = rows[i]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    return sum(trs) / len(trs)


def _trend_up(rows):
    rows = rows[-20:]
    if len(rows) < 20:
        return None
    return rows[-1]["c"] > sum(r["c"] for r in rows) / 20.0


def _gap_pct(day):
    b0 = day.bars[0]
    pc = day.prev["close"] if day.prev else None
    if b0 is None or not pc:
        return None
    return 100.0 * (b0["o"] / pc - 1)


def _vix_fall(ctx):
    if ctx.vix is None or ctx.vix_prev_close is None or len(ctx.vix) == 0:
        return None
    b = ctx.vix[-1]
    if b is None:
        return None
    return b["c"] < ctx.vix_prev_close


def _sector_agree(ctx):
    rets = []
    for sym, p in ctx.peers.items():
        if not p.prev or not p.prev.get("close") or len(p.bars) == 0:
            continue
        b = p.bars[-1]
        if b is None:
            continue
        rets.append(b["c"] / p.prev["close"] - 1)
    if not rets:
        return None
    return sum(rets) / len(rets) > 0


def _or_width(day):
    rng = day.bars[:OR_BARS]
    if len(rng) < OR_BARS or any(b is None for b in rng):
        return None
    return max(b["h"] for b in rng) - min(b["l"] for b in rng)


def _passes(name, day, ctx):
    if name == "none":
        return True
    if name == "vix_fall":
        return _vix_fall(ctx) is True
    if name == "trend_up":
        return _trend_up(_daily(day)) is True
    if name == "gap_small":
        g = _gap_pct(day)
        return g is not None and abs(g) < GAP_MAX
    if name == "sector_agree":
        return _sector_agree(ctx) is True
    if name == "or_narrow":
        a = _daily_atr(_daily(day))
        w = _or_width(day)
        return a is not None and a > 0 and w is not None and w <= ATR_FRAC * a
    return False


def signals(day, ctx):
    if not day.prev or not day.prev.get("high"):
        return []
    pdh = day.prev["high"]
    bars = day.bars
    k = day.k
    b = bars[k]
    if b is None or day.state.get("done"):
        return []
    prev_c = None
    for j in range(k - 1, -1, -1):
        if bars[j] is not None:
            prev_c = bars[j]["c"]
            break
    if prev_c is None or not (prev_c <= pdh < b["c"]):
        return []
    day.state["done"] = True                    # the first in-window crossing is the only candidate
    if not _passes(ctx.params["filter"], day, ctx):
        return []
    lows = [x["l"] for x in bars[max(0, k - 2):k + 1] if x is not None]
    stop = min(lows)
    entry = b["c"]
    if stop >= entry:
        return []
    return [{"side": "long", "entry_time": day.time, "entry": entry, "stop": stop,
             "target": entry + R_MULT * (entry - stop),
             "reason": "close %.2f crossed the prior day's high %.2f [%s]" % (entry, pdh, ctx.params["filter"])}]
