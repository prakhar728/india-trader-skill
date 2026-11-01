"""VWAP reversion, both sides: fade a stretch of k x ATR away from VWAP, target VWAP.

A lab strategy for india-research (see lab_engine.py for the signals(day, ctx) contract).
"""

META = {
    "family": "mean-reversion",
    "kind": "intraday",
    "side": "both",
    "rationale": (
        "Intraday prices in liquid NSE stocks are pushed away from VWAP by impatient order flow (market orders, "
        "stop cascades, retail momentum around the open) and pulled back by liquidity providers and by "
        "institutions that execute against VWAP and buy below it / sell above it. When the stretch is large "
        "relative to the stock's own recent 5-minute range (ATR), the expected drift back toward VWAP may exceed "
        "the round-trip cost. The breakout rules that failed are the opposite bet, which is a weak hint that "
        "fading extremes deserves a test."),
    "rules": (
        "From the bar closing 10:00 to 14:30. VWAP = cumulative typical price x volume from 09:15. ATR = mean "
        "true range of the last 14 five-minute bars of the day. If the bar closes at least k x ATR ABOVE VWAP: "
        "sell its close, stop stop_atr x ATR above the entry, target VWAP. If at least k x ATR BELOW: buy, stop "
        "stop_atr x ATR below, target VWAP. At most two trades per stock per day, one at a time."),
    "default": {"k": 2.0, "stop_atr": 1.0},
    "grid": {"k": [1.5, 2.0, 2.5], "stop_atr": [1.0]},
    "max_trades_per_day": 2,
}

_FIRST_K = 8          # the bar closing 10:00


def _vwap(day):
    """Running VWAP kept in day.state (calls come in time order, so this is causal)."""
    st = day.state
    j = st.get("j", -1)
    pv, vol, tps, n = st.get("pv", 0.0), st.get("vol", 0.0), st.get("tps", 0.0), st.get("n", 0)
    for i in range(j + 1, day.k + 1):
        b = day.bars[i]
        if b is None:
            continue
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        pv += tp * b["v"]
        vol += b["v"]
        tps += tp
        n += 1
    st.update(j=day.k, pv=pv, vol=vol, tps=tps, n=n)
    if not n:
        return None
    return pv / vol if vol > 0 else tps / n


def _atr(bars, n=14):
    rows = [b for b in bars if b is not None][-(n + 1):]
    trs = []
    for i in range(1, len(rows)):
        pc = rows[i - 1]["c"]
        b = rows[i]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    return sum(trs) / len(trs) if trs else None


def signals(day, ctx):
    vw = _vwap(day)
    if day.k < _FIRST_K or vw is None:
        return []
    a = _atr(day.bars[max(0, day.k - 15):])
    if not a:
        return []
    p = ctx.params
    c = day.bars[-1]["c"]
    dev = c - vw
    if dev >= p["k"] * a:
        return [{"side": "short", "entry_time": day.time, "entry": c, "stop": c + p["stop_atr"] * a, "target": vw,
                 "reason": "close %.2f is %.1f ATR above VWAP %.2f" % (c, dev / a, vw)}]
    if -dev >= p["k"] * a:
        return [{"side": "long", "entry_time": day.time, "entry": c, "stop": c - p["stop_atr"] * a, "target": vw,
                 "reason": "close %.2f is %.1f ATR below VWAP %.2f" % (c, -dev / a, vw)}]
    return []
