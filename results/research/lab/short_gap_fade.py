"""Gap-up fade on large caps: the opening auction over-reacts, the first 15-minute low breaks.

A lab strategy for india-research (see lab_engine.py for the signals(day, ctx) contract).
"""

META = {
    "family": "mean-reversion",
    "kind": "intraday",
    "side": "short",
    "rationale": (
        "NSE's pre-open call auction and the first minutes are thin: overnight news and retail market orders "
        "set a gap-up open that is often more than the information warrants (over-reaction), and large caps "
        "have the depth for institutions to sell into it during the day. When the gap buyers cannot even hold "
        "the first 15-minute low, they are trapped above the market and their exits plus the gap-fill sellers "
        "push the price back toward the previous close. The other side is the retail trader who bought the "
        "gap at the open on the news. Gap fills are documented in US and Indian equities; the question is "
        "whether the net after Indian charges and slippage is positive on Nifty 100 names."),
    "rules": (
        "Gap = the 09:15 bar's open over the previous session's close, at least gap_pct %. Opening range = "
        "the first 15 minutes (3 bars). Sell at the close of the FIRST bar closing 09:30-11:30 that closes "
        "below the 15-minute range low. Stop = the 15-minute range high (at least 0.4 % above the entry). "
        "Target: 'gapfill' = the previous close, only if it lies at least 0.8 x the risk below the entry "
        "(else no trade); '2r' = 2 x the risk below the entry. One trade per stock per day; covered by "
        "flat-by 15:05."),
    "default": {"gap_pct": 1.0, "target": "2r", "last_entry": "11:30", "min_stop_pct": 0.4},
    "grid": {"gap_pct": [0.75, 1.5], "target": ["gapfill", "2r"]},
    "max_trades_per_day": 1,
}

_N = 3   # 15 minutes


def signals(day, ctx):
    p = ctx.params
    bars = day.bars
    k = day.k
    if k < _N or day.time > p["last_entry"]:
        return []
    prev = day.prev
    if not prev or not prev.get("close") or bars[0] is None:
        return []
    pc = prev["close"]
    gap = bars[0]["o"] / pc - 1.0
    if gap * 100.0 < p["gap_pct"]:
        return []
    rng = bars[:_N]
    if any(b is None for b in rng):
        return []
    oh = max(b["h"] for b in rng)
    ol = min(b["l"] for b in rng)
    for j in range(_N, k):                     # the FIRST close under the low only
        b = bars[j]
        if b is not None and b["c"] < ol:
            return []
    b = bars[k]
    if b["c"] >= ol:
        return []
    entry = b["c"]
    stop = max(oh, entry * (1 + p["min_stop_pct"] / 100.0))
    risk = stop - entry
    if p["target"] == "gapfill":
        if entry - pc < 0.8 * risk:
            return []
        target = pc
    else:
        target = entry - 2.0 * risk
    return [{"side": "short", "entry_time": day.time, "entry": entry, "stop": stop, "target": target,
             "reason": "gap up %.2f%% then close %.2f under the 15-min low %.2f (target %s)"
                       % (gap * 100.0, entry, ol, p["target"])}]
