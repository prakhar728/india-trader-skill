"""Index-conditioned opening-range breakdown short: only when NIFTY and the sector are weak too.

A lab strategy for india-research (see lab_engine.py for the signals(day, ctx) contract).
"""

META = {
    "family": "short-breakout",
    "kind": "intraday",
    "side": "short",
    "rationale": (
        "A single stock's break of its opening-range low is usually noise and gets bought back by VWAP and "
        "arbitrage desks. When the NIFTY 50 is itself below its VWAP (or under its own opening-range low) and "
        "the stock's sector peers are down from the open, the breakdown is part of an index-level sell programme "
        "(FPI basket selling, index-arbitrage unwind, ETF outflow) that is executed over hours, not minutes, so "
        "the move persists. The other side is the dip buyer who treats a sell programme as a stock-specific "
        "dip. Unconditioned, the short ORB was flat (-0.09R) on explore; the filter should remove the noise days."),
    "rules": (
        "Opening range = first or_minutes (15 or 30) bars from 09:15. Sell at the close of the FIRST bar "
        "(09:30-14:30) that closes below the stock's range low AND, at that bar, NIFTY 50 is below its session "
        "VWAP (index_filter=vwap) or below its own opening-range low over the same minutes (index_filter=or_low) "
        "AND the equal-weight return of the stock's sector peers from their 09:15 open is negative. If an earlier "
        "bar already closed under the stock's range low, no trade that day. Stop = the range high (at least "
        "0.4 % above the entry). Target = 2 x the risk. One trade per stock per day; covered by flat-by 15:05."),
    "default": {"or_minutes": 15, "index_filter": "vwap", "r": 2.0, "min_stop_pct": 0.4},
    "grid": {"or_minutes": [15, 30], "index_filter": ["vwap", "or_low"]},
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


def _index_weak(ctx, n, mode):
    ix = ctx.index
    if ix is None or len(ix) <= n:
        return False
    last = ix[-1]
    if last is None:
        return False
    if mode == "vwap":
        vw = _vwap(ix)
        return vw is not None and last["c"] < vw
    rng = ix[:n]
    if any(b is None for b in rng):
        return False
    return last["c"] < min(b["l"] for b in rng)


def _peers_weak(ctx):
    rets = []
    for pv in ctx.peers.values():
        bars = pv.bars
        if len(bars) == 0 or bars[0] is None or bars[-1] is None:
            continue
        o = bars[0]["o"]
        if o > 0:
            rets.append(bars[-1]["c"] / o - 1.0)
    if len(rets) < 2:
        return False
    return sum(rets) / len(rets) < 0


def signals(day, ctx):
    p = ctx.params
    n = p["or_minutes"] // 5
    bars = day.bars
    k = day.k
    if k < n:
        return []
    rng = bars[:n]
    if any(b is None for b in rng):
        return []
    oh = max(b["h"] for b in rng)
    ol = min(b["l"] for b in rng)
    for j in range(n, k):                     # the FIRST close under the low only
        b = bars[j]
        if b is not None and b["c"] < ol:
            return []
    b = bars[k]
    if b["c"] >= ol:
        return []
    if not _index_weak(ctx, n, p["index_filter"]):
        return []
    if not _peers_weak(ctx):
        return []
    entry = b["c"]
    stop = max(oh, entry * (1 + p["min_stop_pct"] / 100.0))
    return [{"side": "short", "entry_time": day.time, "entry": entry, "stop": stop,
             "target": entry - p["r"] * (stop - entry),
             "reason": "close %.2f under the %d-min range low %.2f with NIFTY weak (%s) and peers down"
                       % (entry, p["or_minutes"], ol, p["index_filter"])}]
