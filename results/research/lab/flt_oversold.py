"""Regime / context filters on the oversold bounce (india-backtest's oversold_bounce, default variant).

Pre-registered filters (one at a time; the base rule's parameters are frozen at backtest.py's
defaults, chosen before any data: RSI < 30, or_low stop, 2R). Each grid value is one trial.
"""

META = {
    "family": "regime-filter",
    "kind": "intraday",
    "side": "long",
    "rationale": (
        "The oversold bounce was the only base rule positive after charges on the Nifty 50 (Yahoo, 60 days: "
        "+0.21R gross / +0.10R net, 119 trades, t = 1.2) and about break-even on the Tata group. Mechanism of the "
        "base rule: a stock with daily RSI(14) under 30 has had a run of forced and fearful selling (margin "
        "calls, stop-losses, redemptions); when the next morning's first 15 minutes are bought above their high, "
        "the sellers are exhausted and the short-term over-reaction unwinds. Five context filters are "
        "pre-registered, each with a mechanism, one at a time against the unfiltered rule: vix_fall (India VIX "
        "below its previous close at the decision bar: fear subsiding market-wide is the condition for an "
        "over-reaction to unwind rather than deepen); trend_up (yesterday's close still above the stock's 20-day "
        "SMA despite RSI < 30: a sharp dip inside an uptrend is the dip institutions buy, RSI < 30 under the SMA "
        "is a falling knife); gap_small (|open gap| < 0.5 %: a large gap down adds new forced selling and a "
        "large gap up has already delivered the bounce before any entry); sector_agree (the equal-weight move "
        "of the stock's NSE industry peers since yesterday's close is positive at the decision bar: the bounce "
        "is market-wide relief, not a lone stock still under selling); or_narrow (15-minute opening-range width "
        "at most 0.3 x the 14-day daily ATR: a calm open means the forced sellers are done). Honest expectation: "
        "the base sample on 391 sessions x Nifty 100 is the real test; filters that cut it below 100 validate "
        "trades cannot pass whatever they show."),
    "rules": (
        "Base rule (oversold_bounce default): daily RSI(14, Wilder) from the stock's daily closes before today "
        "(up to 60 sessions) is under 30 at yesterday's close. Buy the close of the FIRST bar (closing "
        "09:35-14:30) that closes above the first 15 minutes' (09:15-09:30) high; if an earlier bar already closed "
        "above it, no trade. Stop at the 15-minute low, target 2R, flat by 15:05, one trade per stock per day. "
        "Filter (grid): none | vix_fall (VIX last closed bar < VIX previous close) | trend_up (prev close > SMA20 "
        "of daily closes) | gap_small (|09:15 open / prev close - 1| < 0.5 %) | sector_agree (mean of peers' "
        "close-now / prev-close - 1 > 0; no peers = no trade) | or_narrow (range high - low <= 0.3 x daily "
        "ATR(14)). A trade is skipped when the filter fails or cannot be evaluated."),
    "default": {"filter": "none"},
    "grid": {"filter": ["none", "vix_fall", "trend_up", "gap_small", "sector_agree", "or_narrow"]},
    "max_trades_per_day": 1,
}

OR_BARS = 3
R_MULT = 2.0
ATR_FRAC = 0.3
GAP_MAX = 0.5
RSI_MAX = 30.0


def _daily(day):
    st = day.state
    if "daily" not in st:
        st["daily"] = list(day.daily)
    return st["daily"]


def _rsi_last(closes, n=14):
    """Wilder's RSI after the last close, as backtest.rsi_series computes it (None until n+1 closes)."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


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
    st = day.state
    if "rsi" not in st:
        st["rsi"] = _rsi_last([r["c"] for r in _daily(day)])
    rsi = st["rsi"]
    if rsi is None or rsi >= RSI_MAX:
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
    stop = ol
    entry = b["c"]
    if stop >= entry:
        return []
    return [{"side": "long", "entry_time": day.time, "entry": entry, "stop": stop,
             "target": entry + R_MULT * (entry - stop),
             "reason": "daily RSI %.1f, close %.2f above the 15-min high %.2f [%s]" % (rsi, entry, oh, ctx.params["filter"])}]
