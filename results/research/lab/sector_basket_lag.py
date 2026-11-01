"""Sector basket lead-lag: a laggard follows a broad 15-minute move of its sector peers.

A lab strategy for india-research (see lab_engine.py for the signals(day, ctx) contract).
The sector proxy is the equal-weighted basket of the stock's NSE industry peers in the
universe (a causal stand-in for the sector index), not a single leader.
"""

META = {
    "family": "lead-lag",
    "kind": "intraday",
    "side": "both",
    "rationale": (
        "Sector-wide information (index futures flow, a macro print, a policy headline) is priced first in the "
        "sector's liquid names and the sector index futures; the less-watched members of the same NSE industry "
        "group adjust with a delay of minutes because their market makers re-quote slowly and retail attention "
        "arrives late (Lo-MacKinlay lead-lag; Hou 2007 industry information diffusion). A member that has not "
        "moved while most of its peers did should drift the sector's way over the next 15-60 minutes. Using the "
        "basket of peers rather than one leader filters single-stock news out of the signal: a trade needs at "
        "least two thirds of the peers moving the same way."),
    "rules": (
        "From the bar closing 09:35 to 14:30, one trade per stock per day. Peers = the stock's NSE industry "
        "group inside the universe with a positive 20-session average traded value and bars at both ends of the "
        "window; at least 3 peers. Basket move = mean of the peers' 15-minute returns (close now vs close three "
        "bars earlier); at least two thirds of the peers must share its sign. Signal when |basket move| >= "
        "move_pct % and the stock's own 15-minute return is less than a third of it in the same direction. "
        "Long (basket up) at the close; stop = entry - 2 x ATR(14, 5-minute); target = entry + r x 2 x ATR. "
        "Mirror for a falling basket (short, stop above)."),
    "default": {"move_pct": 0.4, "r": 1.0},
    "grid": {"move_pct": [0.4, 0.7], "r": [1.0, 2.0]},
    "max_trades_per_day": 1,
}

MIN_PEERS = 3
ATR_N = 14
STOP_ATR = 2.0


def _ret(bars, k):
    if k < 3:
        return None
    a, b = bars[k - 3], bars[k]
    if a is None or b is None or not a["c"]:
        return None
    return 100.0 * (b["c"] / a["c"] - 1)


def signals(day, ctx):
    from lab_engine import atr
    peers = ctx.peers
    if not peers or len(peers) < MIN_PEERS:
        return []
    k = day.k
    m = _ret(day.bars, k)
    if m is None:
        return []
    moves = []
    for sym, pv in peers.items():
        if not pv.avg_value20:
            continue
        r = _ret(pv.bars, k)
        if r is not None:
            moves.append(r)
    if len(moves) < MIN_PEERS:
        return []
    bm = sum(moves) / len(moves)
    p = ctx.params
    if abs(bm) < p["move_pct"]:
        return []
    agree = sum(1 for r in moves if (r > 0) == (bm > 0) and r != 0)
    if agree * 3 < len(moves) * 2:
        return []
    if not (m < bm / 3.0 if bm > 0 else m > bm / 3.0):
        return []
    a = atr(day.bars[max(0, k - ATR_N):k + 1], ATR_N)
    if not a or a <= 0:
        return []
    c = day.bars[k]["c"]
    risk = STOP_ATR * a
    why = "basket %+.2f%% in 15 min (%d/%d peers agree), %s only %+.2f%%" % (bm, agree, len(moves), day.symbol, m)
    if bm > 0:
        return [{"side": "long", "entry_time": day.time, "entry": c, "stop": c - risk,
                 "target": c + p["r"] * risk, "reason": why}]
    return [{"side": "short", "entry_time": day.time, "entry": c, "stop": c + risk,
             "target": c - p["r"] * risk, "reason": why}]
