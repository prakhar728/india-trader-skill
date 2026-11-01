"""Late-session new-low short on weak days: intraday (MIS) longs are squared off into the close.

A lab strategy for india-research (see lab_engine.py for the signals(day, ctx) contract).
"""

META = {
    "family": "time-of-day",
    "kind": "intraday",
    "side": "short",
    "rationale": (
        "Indian brokers auto-square-off intraday (MIS / intraday product) positions from about 15:15, and "
        "retail intraday is net long. On a day when NIFTY is below its open and a stock makes a NEW session low "
        "after 13:30, the remaining intraday longs are under water with no overnight option: they must sell "
        "before the square-off, and their brokers' risk desks sell for them when margins thin. That forced "
        "flow is one-directional and time-bound (a structural Indian feature), so the last 90 minutes of a weak "
        "day should drift lower than the day's earlier bars. The other side is the intraday long who hoped "
        "for a recovery and the delivery buyer who waits for the close. Flat by 15:05 (the engine's rule) only "
        "catches the start of the wave, which is a conservative test of the mechanism."),
    "rules": (
        "Entries at bars closing 13:30-14:30 only. Regime: NIFTY 50's last close is below its 09:15 open "
        "(nifty_filter=open) or below its session VWAP (nifty_filter=vwap). Sell at the close of the FIRST "
        "bar in the window that closes below the session's low of all earlier bars AND below the stock's "
        "session VWAP. Stop = the highest high of the previous 12 bars (one hour), at least 0.5 % above the "
        "entry. Target = r x the risk below the entry, else covered at flat-by 15:05. One trade per stock per "
        "day."),
    "default": {"nifty_filter": "open", "r": 1.5, "first": "13:30", "last": "14:30", "lookback": 12,
                "min_stop_pct": 0.5},
    "grid": {"nifty_filter": ["open", "vwap"], "r": [1.0, 2.0]},
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


def _nifty_weak(ctx, mode):
    ix = ctx.index
    if ix is None or len(ix) < 2 or ix[0] is None or ix[-1] is None:
        return False
    last = ix[-1]["c"]
    if mode == "open":
        return last < ix[0]["o"]
    vw = _vwap(ix)
    return vw is not None and last < vw


def signals(day, ctx):
    p = ctx.params
    if day.time < p["first"] or day.time > p["last"]:
        return []
    bars = day.bars
    k = day.k
    if k < p["lookback"] + 1 or bars[-1] is None:
        return []
    st = day.state
    if st.get("done"):
        return []
    b = bars[-1]
    earlier = [x for x in bars[:k] if x is not None]
    if not earlier:
        return []
    lo = min(x["l"] for x in earlier)
    if b["c"] >= lo:
        return []
    st["done"] = True                       # only the FIRST new-low close in the window
    vw = _vwap(bars)
    if vw is None or b["c"] >= vw:
        return []
    if not _nifty_weak(ctx, p["nifty_filter"]):
        return []
    entry = b["c"]
    hi = max(x["h"] for x in bars[k - p["lookback"]:k + 1] if x is not None)
    stop = max(hi, entry * (1 + p["min_stop_pct"] / 100.0))
    risk = stop - entry
    return [{"side": "short", "entry_time": day.time, "entry": entry, "stop": stop,
             "target": entry - p["r"] * risk,
             "reason": "new session low %.2f at %s under VWAP %.2f with NIFTY weak (%s)"
                       % (entry, day.time, vw, p["nifty_filter"])}]
