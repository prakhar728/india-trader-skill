"""Open lead-lag: NIFTY 50 moves in the first 15 minutes, a large cap that has not followed catches up.

A lab strategy for india-research (see lab_engine.py for the signals(day, ctx) contract).
One decision per stock per day, at the close of the 09:30 bar only.
"""

META = {
    "family": "lead-lag",
    "kind": "intraday",
    "side": "both",
    "rationale": (
        "At the open the index level is set first: the pre-open auction, index futures and ETF/basket programs "
        "trade the index as a whole, while an individual large cap's first prints can be stale (thin auction, "
        "a few resting limit orders). Index futures lead cash stocks most at the open (Chan 1992; Lo-MacKinlay "
        "lead-lag). If NIFTY has moved at least thr % from its open by 09:30 and a constituent has moved less than "
        "a third of that, sector desks and arbitrageurs should re-quote the laggard towards the index over the "
        "next 30-60 minutes; the other side is the stale resting orders in the laggard's book. Explore "
        "measurement (angel:nifty100:5m explore, close-to-close): laggards after a >= 50 bps index move "
        "earned +15.7 bps in the next 30 min (n=458, day-clustered t=1.2), i.e. barely above the 10.4 bps cost."),
    "rules": (
        "Only at the bar closing 09:30 (the third bar). Index move = NIFTY 09:30 close vs its 09:15 open; "
        "stock move = its 09:30 close vs its 09:15 open. Signal when |index move| >= thr % and the stock's "
        "signed move is below a third of the index move. Long (index up) at the close; stop = the low of the "
        "first three bars; target = entry + r x risk. Mirror for an index fall (short, stop = the high of the "
        "first three bars). Risk must be at least 0.2 % of the entry (tighter stops cost too much in R). "
        "One trade per stock per day."),
    "default": {"thr": 0.5, "r": 1.0},
    "grid": {"r": [1.0, 2.0]},
    "max_trades_per_day": 1,
}

MIN_RISK_PCT = 0.2


def signals(day, ctx):
    if day.k != 2:
        return []
    ix = ctx.index
    if ix is None or len(ix) < 3:
        return []
    i0, i2 = ix[0], ix[2]
    b0, b2 = day.bars[0], day.bars[2]
    if i0 is None or i2 is None or b0 is None or b2 is None or not i0["o"] or not b0["o"]:
        return []
    im = 100.0 * (i2["c"] / i0["o"] - 1)
    p = ctx.params
    if abs(im) < p["thr"]:
        return []
    sm = 100.0 * (b2["c"] / b0["o"] - 1)
    sg = 1 if im > 0 else -1
    if sg * sm >= abs(im) / 3.0:
        return []
    first3 = [b for b in day.bars[0:3] if b is not None]
    c = b2["c"]
    why = "NIFTY %+.2f%% by 09:30, %s only %+.2f%%" % (im, day.symbol, sm)
    if sg > 0:
        stop = min(b["l"] for b in first3)
        risk = c - stop
        if risk < MIN_RISK_PCT / 100.0 * c:
            return []
        return [{"side": "long", "entry_time": day.time, "entry": c, "stop": stop, "target": c + p["r"] * risk,
                 "reason": why}]
    stop = max(b["h"] for b in first3)
    risk = stop - c
    if risk < MIN_RISK_PCT / 100.0 * c:
        return []
    return [{"side": "short", "entry_time": day.time, "entry": c, "stop": stop, "target": c - p["r"] * risk,
             "reason": why}]
