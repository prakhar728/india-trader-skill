"""Intraday momentum (Gao, Han, Li & Zhou 2018, JFE): the first half-hour return predicts the last part of the day.

A lab strategy for india-research (see lab_engine.py for the signals(day, ctx) contract).
Family: time-of-day. Pre-specified from the literature before any look at this dataset.
"""

META = {
    "family": "time-of-day",
    "kind": "intraday",
    "side": "both",
    "rationale": (
        "Gao, Han, Li and Zhou (2018, JFE) find that the first half-hour return of the market predicts the last "
        "half-hour return in the same direction, strongest on volatile and high-volume days. Mechanism: overnight "
        "news is absorbed by informed traders in the first 30 minutes, while a second group (late-informed "
        "investors, and institutions that execute 'at the close' or rebalance to the day's level) only trades "
        "towards the close, pushing prices the same way; on the other side are intraday liquidity providers who "
        "fade the morning move and unwind their inventory into the close. In India the F&O closing auction "
        "(since 2026-08-03) ends continuous trading at 15:15 for F&O stocks and our flat-by is 15:05, so the "
        "tradable 'last' window is 14:30 to 15:05, earlier than in the papers; the hypothesis is that the "
        "late-day flow already shows there."),
    "rules": (
        "At the close of the 14:30 bar only. Signal = the NIFTY 50's first-half-hour return measured as in the paper, "
        "from the previous session's close to the 09:45 bar close (it includes the overnight gap), when "
        "signal=index, or the stock's own previous-close-to-09:45 return when signal=stock. "
        "If |signal| >= thr %: trade the stock in the signal's direction at the 14:30 close; stop 1.0 x the "
        "stock's 09:15-14:30 session range away (a disaster stop, the trade is a timed exit at flat-by 15:05); "
        "target 3 x the stop distance. One trade per stock per day."),
    "default": {"thr": 0.3, "signal": "index"},
    "grid": {"thr": [0.3, 0.5], "signal": ["index", "stock"]},
    "max_trades_per_day": 1,
}

_ENTRY_K = 62      # the bar closing 14:30 (bar k ends 09:15 + 5 x (k + 1); 09:15 + 5 x 63 = 14:30)
_FIRST_K = 5       # bars 0..5 = 09:15 .. 09:45 close


def _first30(bars, prev_close):
    rows = [b for b in bars[:_FIRST_K + 1] if b is not None]
    if len(rows) < 4 or not prev_close or prev_close <= 0:
        return None
    return (rows[-1]["c"] / prev_close - 1.0) * 100.0


def signals(day, ctx):
    if day.k != _ENTRY_K:
        return []
    p = ctx.params
    if p["signal"] == "index":
        if ctx.index is None or len(ctx.index) <= _FIRST_K:
            return []
        sig = _first30(ctx.index, ctx.index_prev_close)
    else:
        sig = _first30(day.bars, (day.prev or {}).get("close"))
    if sig is None or abs(sig) < p["thr"]:
        return []
    rows = [b for b in day.bars if b is not None]
    c = rows[-1]["c"]
    rng = max(b["h"] for b in rows) - min(b["l"] for b in rows)
    if rng <= 0:
        return []
    side = "long" if sig > 0 else "short"
    stop = c - rng if side == "long" else c + rng
    target = c + 3 * rng if side == "long" else c - 3 * rng
    return [{"side": side, "entry_time": day.time, "entry": c, "stop": stop, "target": target,
             "reason": "%s first-30m %+.2f%% -> %s into the close" % (p["signal"], sig, side)}]
