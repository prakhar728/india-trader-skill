"""End-of-day reversion of the sector-relative laggard: buy the stock that lags its NSE industry
peers by >= th % at 14:00-14:30, hold into the close (flat-by 15:05).

A lab strategy for india-research (see lab_engine.py for the signals(day, ctx) contract).
Long only: an explore-only prototype (gross, no charges) showed the short side of sector
leaders at ~0 while laggards drifted back ~+0.05 %; logged as long-only so the trial count stays
at two. Costs: the stop is 1 % away so sizing hits the 25 % position cap (Rs 25k), risk ~Rs 250,
charges ~Rs 27 = ~0.1 R.
"""

META = {
    "family": "mean-reversion",
    "kind": "intraday",
    "side": "long",
    "rationale": (
        "A stock that has fallen far more than its own industry group by early afternoon has absorbed "
        "stock-specific selling (a block, a downgrade, stop cascades) that its peers did not. Intraday "
        "liquidity providers who bought that flow carry inventory they want flat by the close, and index / "
        "sector-basket desks that execute at the close buy the whole basket, laggard included, so the relative "
        "move is expected to partly revert in the last hour (inventory unwinding plus closing-basket demand). "
        "The relative measure strips the market and sector move so the bet is on the idiosyncratic overreaction only."),
    "rules": (
        "Bars closing 14:00 to 14:30 only. Stock return = close / previous close - 1. Peer return = the mean of the "
        "same for the stock's NSE industry peers in the universe (at least 3 peers with a bar). Relative = stock - "
        "peer, in %. Buy the first close where relative <= -th. Stop 1.0 % below the entry, target 1.0 % above; "
        "otherwise exit at flat-by 15:05. One trade per stock per day."),
    "default": {"th": 2.0},
    "grid": {"th": [2.0, 2.5]},
    "max_trades_per_day": 1,
}

_K_FIRST = 56        # the bar closing 14:00
_K_LAST = 62         # the bar closing 14:30


def signals(day, ctx):
    k = day.k
    if k < _K_FIRST or k > _K_LAST or day.state.get("done"):
        return []
    b = day.bars[-1]
    if b is None or not day.prev or not day.prev.get("close"):
        return []
    rets = []
    for sym, pv in ctx.peers.items():
        if k >= len(pv.bars):
            continue
        pb = pv.bars[k]
        pc = (pv.prev or {}).get("close")
        if pb is None or not pc:
            continue
        rets.append(100.0 * (pb["c"] / pc - 1))
    if len(rets) < 3:
        return []
    c = b["c"]
    rel = 100.0 * (c / day.prev["close"] - 1) - sum(rets) / len(rets)
    if rel > -ctx.params["th"]:
        return []
    day.state["done"] = True
    return [{"side": "long", "entry_time": day.time, "entry": c, "stop": c * 0.99, "target": c * 1.01,
             "reason": "%s %+.2f%% vs %d peers' mean at %s" % (day.symbol, rel, len(rets), day.time)}]
