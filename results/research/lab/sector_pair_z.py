"""Intraday pairs inside a sector: z-score of the log price ratio over a rolling 30-bar window,
enter at |z| >= z, each leg a separate trade (two sets of charges), target = the ratio's mean.

A lab strategy for india-research (see lab_engine.py for the signals(day, ctx) contract).
The engine runs one stock at a time, so each leg is emitted by the stock that is rich (short) or
cheap (long) against its fixed partner; both legs fire on the same bar because both see the same
ratio. Costs: the stop is 1 % away so sizing hits the Rs 25k position cap, risk ~Rs 250 per leg,
charges ~Rs 27 per leg (~Rs 53 per pair-trade on Rs 50k gross).
"""

META = {
    "family": "mean-reversion",
    "kind": "intraday",
    "side": "both",
    "rationale": (
        "Two stocks in the same NSE industry share the sector's news and index flows; their intraday price "
        "ratio moves mostly because order flow hits one name first (a block in one, a basket order sized "
        "differently, a stop cascade in the more volatile leg). Arbitrage desks and the slower leg's own "
        "liquidity providers close the temporary divergence, so a ratio stretched two standard deviations "
        "from its recent intraday mean is expected to drift back. Trading both legs removes the market and "
        "sector direction; the bet is only on the relative mis-pricing."),
    "rules": (
        "Fixed pairs inside one NSE industry: HDFCBANK/ICICIBANK, KOTAKBANK/AXISBANK, SBIN/BANKBARODA, "
        "BAJFINANCE/BAJAJFINSV, TCS/INFY, HCLTECH/WIPRO, NTPC/POWERGRID, TATAPOWER/ADANIPOWER, JSWSTEEL/TATASTEEL, "
        "HINDALCO/VEDL, SUNPHARMA/CIPLA, HINDUNILVR/ITC, BPCL/IOC, ULTRACEMCO/AMBUJACEM, MARUTI/M&M. "
        "Bars closing 10:30 to 14:30. r = ln(stock close / partner close) per bar; mean m and standard deviation s "
        "of the last 30 bars (at least 27 present). z = (r_now - m) / s. If z >= zt: sell the stock, target = the "
        "price where r = m with the partner unchanged, stop 1.0 % above. If z <= -zt: buy, target likewise, stop "
        "1.0 % below. The partner emits the mirror leg on the same bar. Flat-by 15:05. One trade per stock per day."),
    "default": {"zt": 2.0},
    "grid": {"zt": [2.0, 2.5]},
    "max_trades_per_day": 1,
}

_PAIRS = [("HDFCBANK", "ICICIBANK"), ("KOTAKBANK", "AXISBANK"), ("SBIN", "BANKBARODA"), ("BAJFINANCE", "BAJAJFINSV"),
          ("TCS", "INFY"), ("HCLTECH", "WIPRO"), ("NTPC", "POWERGRID"), ("TATAPOWER", "ADANIPOWER"),
          ("JSWSTEEL", "TATASTEEL"), ("HINDALCO", "VEDL"), ("SUNPHARMA", "CIPLA"), ("HINDUNILVR", "ITC"),
          ("BPCL", "IOC"), ("ULTRACEMCO", "AMBUJACEM"), ("MARUTI", "M&M")]
PARTNER = {}
for _a, _b in _PAIRS:
    PARTNER[_a] = _b
    PARTNER[_b] = _a

_K_FIRST = 15        # the bar closing 10:30
_K_LAST = 62         # the bar closing 14:30
_W = 30
_MIN_W = 27


def _ratio_series(day, pbars):
    """Causal: extends the per-day log-ratio list kept in day.state up to day.k."""
    import math
    st = day.state
    lr = st.setdefault("lr", [])
    for i in range(len(lr), day.k + 1):
        b = day.bars[i]
        pb = pbars[i] if i < len(pbars) else None
        lr.append(math.log(b["c"] / pb["c"]) if (b and pb and b["c"] > 0 and pb["c"] > 0) else None)
    return lr


def signals(day, ctx):
    import math
    k = day.k
    partner = PARTNER.get(day.symbol)
    if partner is None or partner not in ctx.peers or day.state.get("done"):
        return []
    pv = ctx.peers[partner]
    lr = _ratio_series(day, pv.bars)
    if k < _K_FIRST or k > _K_LAST or lr[k] is None:
        return []
    win = [x for x in lr[k - _W + 1:k + 1] if x is not None]
    if len(win) < _MIN_W:
        return []
    m = sum(win) / len(win)
    s = math.sqrt(sum((x - m) ** 2 for x in win) / len(win))
    if s <= 0:
        return []
    z = (lr[k] - m) / s
    zt = ctx.params["zt"]
    c = day.bars[k]["c"]
    target = c * math.exp(m - lr[k])            # the price at which r = m, partner unchanged
    if z >= zt:
        day.state["done"] = True
        return [{"side": "short", "entry_time": day.time, "entry": c, "stop": c * 1.01, "target": target,
                 "reason": "%s/%s z=%+.2f (ratio %.2f%% above its 30-bar mean)" % (day.symbol, partner, z, 100 * (lr[k] - m))}]
    if z <= -zt:
        day.state["done"] = True
        return [{"side": "long", "entry_time": day.time, "entry": c, "stop": c * 0.99, "target": target,
                 "reason": "%s/%s z=%+.2f (ratio %.2f%% below its 30-bar mean)" % (day.symbol, partner, z, 100 * (lr[k] - m))}]
    return []
