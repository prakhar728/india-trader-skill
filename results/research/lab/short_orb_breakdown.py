"""Short opening-range breakdown: the mirror image of the long ORB that lost after costs.

A lab strategy for india-research (see lab_engine.py for the signals(day, ctx) contract).
Copy it to trader/research/lab/<new id>.py to change it: a changed strategy is a new id.
"""

META = {
    "family": "short-breakout",
    "kind": "intraday",
    "side": "short",
    "rationale": (
        "The long 15/30-minute ORB lost after charges on Nifty 50 and the Tata group. If part of that loss is "
        "an asymmetry rather than pure noise, the short side may differ: Indian large caps tend to fall faster "
        "than they rise (sell programmes from FPIs and index arbitrage unwind into weakness, and stops cluster "
        "under the opening range), so a decisive break of the first 15-30 minutes' low may travel further per "
        "rupee of risk than a break of the high. Shorting EQ intraday needs no borrow, and STT is charged on the "
        "sell leg either way, so costs are the same as the long."),
    "rules": (
        "Opening range = high and low of the first or_minutes (15 or 30) of 5-minute bars from 09:15. Sell at "
        "the close of the FIRST bar (closing 09:30-14:30) that closes below the range low; if an earlier bar "
        "already closed below it, no trade that day. Stop at the range midpoint (variant: the range high). "
        "Target r x the risk below the entry. One trade per stock per day; covered by flat-by 15:05."),
    "default": {"or_minutes": 15, "stop": "or_mid", "r": 2.0},
    "grid": {"or_minutes": [15, 30], "stop": ["or_mid", "or_high"], "r": [1.5, 2.0]},
    "max_trades_per_day": 1,
}


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
    stop = (oh + ol) / 2.0 if p["stop"] == "or_mid" else oh
    entry = b["c"]
    return [{"side": "short", "entry_time": day.time, "entry": entry, "stop": stop,
             "target": entry - p["r"] * (stop - entry),
             "reason": "close %.2f under the %d-min range low %.2f" % (entry, p["or_minutes"], ol)}]
