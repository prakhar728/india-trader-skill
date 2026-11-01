"""Sector lead-lag: a laggard follows its sector leader's 15-minute move.

A lab strategy for india-research (see lab_engine.py for the signals(day, ctx) contract).
Peers come from NSE's Nifty 500 list (Industry column), restricted to the universe.
"""

META = {
    "family": "lead-lag",
    "kind": "intraday",
    "side": "both",
    "rationale": (
        "News and order flow about a sector reach its bellwether first: it is the most traded and the first "
        "name institutions and index desks hit. Smaller or less-watched members of the same NSE industry group "
        "react later (slow information diffusion: Lo-MacKinlay lead-lag, Hou 2007 on industry information). If "
        "the delay lasts more than one 5-minute bar, a laggard that has not yet moved should drift the leader's "
        "way. The leader is fixed BEFORE the day: the sector member with the highest average traded value over "
        "the previous 20 sessions."),
    "rules": (
        "From the bar closing 09:35 to 14:30. Leader = the sector peer (or the stock itself) with the highest "
        "20-session average traded value; the leader itself is never traded. Leader move = its close now vs "
        "three bars (15 minutes) earlier. If the leader rose at least move_pct % and the stock rose less than a "
        "third of that: buy the stock's close, stop under the lowest low of its last three bars, target r x "
        "risk. Mirror for a falling leader (sell, stop over the highest high). One trade per stock per day."),
    "default": {"move_pct": 0.5, "r": 1.5},
    "grid": {"move_pct": [0.5, 0.8], "r": [1.5, 2.0]},
    "max_trades_per_day": 1,
}


def _move(bars, k):
    if k < 3:
        return None
    a, b = bars[k - 3], bars[k]
    if a is None or b is None or not a["c"]:
        return None
    return 100.0 * (b["c"] / a["c"] - 1)


def signals(day, ctx):
    peers = ctx.peers
    if not peers:
        return []
    if "leader" not in day.state:                 # fixed once a day, from the 20 sessions before today
        rows = [(pv.avg_value20 or 0.0, sym) for sym, pv in peers.items()]
        rows.append((day.avg_value20 or 0.0, day.symbol))
        best = max(rows)
        day.state["leader"] = best[1] if best[0] > 0 else None
    leader = day.state["leader"]
    if leader is None or leader == day.symbol or leader not in peers:
        return []
    k = day.k
    lm = _move(peers[leader].bars, k)
    m = _move(day.bars, k)
    if lm is None or m is None:
        return []
    p = ctx.params
    c = day.bars[k]["c"]
    last3 = [b for b in day.bars[k - 2:k + 1] if b is not None]
    if lm >= p["move_pct"] and m < lm / 3.0:
        stop = min(b["l"] for b in last3)
        if stop >= c:
            return []
        return [{"side": "long", "entry_time": day.time, "entry": c, "stop": stop, "target": c + p["r"] * (c - stop),
                 "reason": "leader %s %+.2f%% in 15 min, %s only %+.2f%%" % (leader, lm, day.symbol, m)}]
    if lm <= -p["move_pct"] and m > lm / 3.0:
        stop = max(b["h"] for b in last3)
        if stop <= c:
            return []
        return [{"side": "short", "entry_time": day.time, "entry": c, "stop": stop, "target": c - p["r"] * (stop - c),
                 "reason": "leader %s %+.2f%% in 15 min, %s only %+.2f%%" % (leader, lm, day.symbol, m)}]
    return []
