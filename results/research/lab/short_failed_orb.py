"""Failed opening-range breakout short: trapped breakout buyers.

A lab strategy for india-research (see lab_engine.py for the signals(day, ctx) contract).
"""

META = {
    "family": "short-failed-breakout",
    "kind": "intraday",
    "side": "short",
    "rationale": (
        "Breakout traders (and the stop orders of earlier shorts) buy the first close above the opening-range "
        "high. When the next bars close back INSIDE the range within 30 minutes, those buyers are under water "
        "with a stop just under the breakout bar; the first close back inside turns their buying into forced "
        "selling, while the earlier range low is the next cluster of stops. The other side is the momentum "
        "buyer who chased the breakout and must exit; the edge is paid for by his stop. The long ORB itself "
        "lost after charges on this universe, which is consistent with breakouts failing more often than not."),
    "rules": (
        "Opening range = first or_minutes (15 or 30) of 5-minute bars from 09:15. A breakout = a bar closing "
        "above the range high (entries 09:30-14:30). The failure = the FIRST later bar (at most 6 bars = 30 "
        "minutes after the breakout) that closes back below the range high: sell at its close. Stop = the "
        "highest high since the breakout bar, at least 0.4 % above the entry (charges would eat a tighter "
        "stop). Target = r x the risk below the entry. If the failure comes more than 6 bars after the "
        "breakout, no trade that day. One trade per stock per day; covered by flat-by 15:05."),
    "default": {"or_minutes": 15, "r": 2.0, "max_wait": 6, "min_stop_pct": 0.4},
    "grid": {"or_minutes": [15, 30], "r": [1.5, 2.5]},
    "max_trades_per_day": 1,
}


def signals(day, ctx):
    p = ctx.params
    n = p["or_minutes"] // 5
    bars = day.bars
    k = day.k
    if k <= n:
        return []
    rng = bars[:n]
    if any(b is None for b in rng):
        return []
    oh = max(b["h"] for b in rng)
    st = day.state
    if "done" in st:
        return []
    bo = st.get("breakout")           # index of the breakout close
    if bo is None:
        # look for the first close above the range high among bars n..k
        for j in range(n, k + 1):
            b = bars[j]
            if b is not None and b["c"] > oh:
                st["breakout"] = bo = j
                break
        if bo is None or bo == k:
            return []
    b = bars[k]
    if k - bo > p["max_wait"]:
        st["done"] = True
        return []
    if b["c"] >= oh:
        return []
    hi = max(x["h"] for x in bars[bo:k + 1] if x is not None)
    entry = b["c"]
    stop = max(hi, entry * (1 + p["min_stop_pct"] / 100.0))
    risk = stop - entry
    st["done"] = True
    return [{"side": "short", "entry_time": day.time, "entry": entry, "stop": stop,
             "target": entry - p["r"] * risk,
             "reason": "failed %d-min breakout: close %.2f back under range high %.2f (trap high %.2f)"
                       % (p["or_minutes"], entry, oh, hi)}]
