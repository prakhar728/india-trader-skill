"""Regime / context filters on the long 15-minute ORB (india-backtest's orb15, default variant).

Pre-registered filters (one at a time; the base rule's parameters are frozen at backtest.py's
defaults, chosen before any data: or_mid stop, 2R). Each grid value is one trial.
"""

META = {
    "family": "regime-filter",
    "kind": "intraday",
    "side": "long",
    "rationale": (
        "The 15-minute ORB lost -0.10R gross / -0.33R net on the Nifty 50 (Yahoo, 60 days) and on the Tata group. "
        "A filter can only help if it removes losers for a REASON, so five context filters are pre-registered, "
        "each with a mechanism, and tested one at a time against the unfiltered rule on the same data: "
        "vix_fall (India VIX below its previous close at the decision bar: implied vol being sold means the "
        "market is adding risk, the follow-through a long breakout needs); trend_up (yesterday's close above the "
        "stock's 20-day SMA: breakouts in an uptrend have institutional accumulation and trend-followers behind "
        "them, breakouts in a downtrend are sold into by holders exiting); gap_small (|open gap| < 0.5 %: a big "
        "gap has already discounted the news and gap-faders sell into it, a breakout from a flat open is fresh "
        "intraday flow); sector_agree (the equal-weight move of the stock's NSE industry peers since yesterday's "
        "close is positive at the decision bar: a breakout the sector shares is information, one the sector "
        "does not share is idiosyncratic noise); or_narrow (opening-range width at most 0.3 x the 14-day daily "
        "ATR: a compressed range gives a tighter stop, so more R per rupee of move, and precedes expansion). "
        "The honest expectation is that most filters change little: the base rule's gross expectancy is "
        "negative and the filters are coarse."),
    "rules": (
        "Base rule (orb15 default): opening range = high/low of the 09:15, 09:20, 09:25 bars. Buy the close of "
        "the FIRST bar (closing 09:35-14:30) that closes above the range high; if an earlier bar already closed "
        "above it, no trade that day. Stop at the range midpoint, target 2R, flat by 15:05, one trade per stock "
        "per day. Filter (grid): none | vix_fall (VIX last closed bar < VIX previous close) | trend_up "
        "(prev close > SMA20 of daily closes) | gap_small (|09:15 open / prev close - 1| < 0.5 %) | sector_agree "
        "(mean of peers' close-now / prev-close - 1 > 0; no peers = no trade) | or_narrow (range high - low <= "
        "0.3 x daily ATR(14)). A trade is skipped when the filter fails or cannot be evaluated."),
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


def _passes(name, day, ctx, or_width):
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
        return a is not None and a > 0 and or_width <= ATR_FRAC * a
    return False


def signals(day, ctx):
    bars = day.bars
    k = day.k
    if k < OR_BARS:
        return []
    rng = bars[:OR_BARS]
    if any(b is None for b in rng):
        return []
    oh = max(b["h"] for b in rng)
    ol = min(b["l"] for b in rng)
    for j in range(OR_BARS, k):                 # the FIRST close above the high only
        b = bars[j]
        if b is not None and b["c"] > oh:
            return []
    b = bars[k]
    if b["c"] <= oh:
        return []
    if not _passes(ctx.params["filter"], day, ctx, oh - ol):
        return []
    stop = (oh + ol) / 2.0
    entry = b["c"]
    if stop >= entry:
        return []
    return [{"side": "long", "entry_time": day.time, "entry": entry, "stop": stop,
             "target": entry + R_MULT * (entry - stop),
             "reason": "close %.2f above the 15-min range high %.2f [%s]" % (entry, oh, ctx.params["filter"])}]
