"""Relative-weakness short at 10:15: a stock far below its sector peers in the first hour keeps underperforming.

A lab strategy for india-research (see lab_engine.py for the signals(day, ctx) contract).
"""

META = {
    "family": "lead-lag",
    "kind": "intraday",
    "side": "short",
    "rationale": (
        "A stock that is down k x its daily ATR against its own sector peers by 10:15, and below its VWAP, is "
        "being sold on stock-specific information or flow (a downgrade, a block, a fund's exit) rather than "
        "with the market. Institutions split such orders over the session (VWAP / TWAP algos), so the "
        "underperformance continues into the afternoon: slow information diffusion plus persistent informed "
        "selling. The other side is the pairs / sector trader who buys the laggard expecting it to catch up "
        "with its peers, and the retail dip buyer. Short the stock only, not the spread (no hedge cost)."),
    "rules": (
        "At the bar closing 10:15 only: stock return = close / 09:15 open - 1; peer return = the mean of the "
        "same for the stock's sector peers in the universe (at least 2 peers). Relative = stock - peer. ATR% = "
        "ATR(14) of the daily bars before today / the previous close. Sell at the 10:15 close if relative <= "
        "-k x ATR% and the close is below the session VWAP. Stop = the day's high so far, at least 0.5 % above "
        "the entry. Target = r x the risk below the entry. One trade per stock per day; covered by flat-by 15:05."),
    "default": {"k": 1.0, "r": 2.0, "at": "10:15", "min_stop_pct": 0.5},
    "grid": {"k": [0.5, 1.0], "r": [1.5, 2.5]},
    "max_trades_per_day": 1,
}


def _vwap(bars):
    pv = vol = 0.0
    for b in bars:
        if b is None:
            continue
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        pv += tp * b["v"]
        vol += b["v"]
    if vol <= 0:
        cs = [b["c"] for b in bars if b is not None]
        return sum(cs) / len(cs) if cs else None
    return pv / vol


def _daily_atr_pct(daily, n=14):
    rows = [d for d in daily if d]
    if len(rows) < n + 1:
        return None
    rows = rows[-(n + 1):]
    trs = []
    for i in range(1, len(rows)):
        pc = rows[i - 1]["c"]
        b = rows[i]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    last = rows[-1]["c"]
    if not trs or last <= 0:
        return None
    return (sum(trs) / len(trs)) / last


def _peer_ret(ctx):
    rets = []
    for pv in ctx.peers.values():
        bars = pv.bars
        if len(bars) == 0 or bars[0] is None or bars[-1] is None:
            continue
        o = bars[0]["o"]
        if o > 0:
            rets.append(bars[-1]["c"] / o - 1.0)
    if len(rets) < 2:
        return None
    return sum(rets) / len(rets)


def signals(day, ctx):
    p = ctx.params
    if day.time != p["at"]:
        return []
    bars = day.bars
    if bars[0] is None or bars[-1] is None:
        return []
    o = bars[0]["o"]
    if o <= 0:
        return []
    atrp = _daily_atr_pct(day.daily)
    if atrp is None:
        return []
    pr = _peer_ret(ctx)
    if pr is None:
        return []
    b = bars[-1]
    rel = (b["c"] / o - 1.0) - pr
    if rel > -p["k"] * atrp:
        return []
    vw = _vwap(bars)
    if vw is None or b["c"] >= vw:
        return []
    entry = b["c"]
    hi = max(x["h"] for x in bars if x is not None)
    stop = max(hi, entry * (1 + p["min_stop_pct"] / 100.0))
    risk = stop - entry
    return [{"side": "short", "entry_time": day.time, "entry": entry, "stop": stop,
             "target": entry - p["r"] * risk,
             "reason": "relative %.2f%% vs peers %.2f%% (ATR %.2f%%), below VWAP %.2f"
                       % (rel * 100.0, pr * 100.0, atrp * 100.0, vw)}]
