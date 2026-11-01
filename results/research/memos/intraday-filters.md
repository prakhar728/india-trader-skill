# Research memo, 2026-10-29: regime and context filters on the intraday base rules

Research only; nothing here places an order. Not financial advice; a backtest is not a forecast.

**Data**: the frozen Angel One dataset (`trader/cache/research-dataset.json`, all 224 sha256s verified, no network calls), key `angel:nifty100:5m` (shared split made by a teammate): explore 2025-04-01..2026-03-11 (234 sessions), validate 2026-03-12..2026-07-08 (78), holdout 2026-07-09..2026-10-29 (78, **never opened**). 99 Nifty 100 stocks traded (one had no sector / bars on some days), NIFTY and India VIX 5-minute context, NSE industry peers. Known data issues: ~35 zero-volume bars per stock around 2026-09-07 (holdout, untouched), Muhurat 2025-10-21 short session (dropped by the session builder).

**Design (pre-registered before any filtered result)**: five base rules frozen at `backtest.py` defaults (orb15/orb30: or_mid stop 2R; pdh: swing stop 2R; gap_go: gap >= 1 %, or_low, 2R; oversold_bounce: RSI14 < 30, or_low, 2R), each as one lab id with a six-value grid `filter = none | vix_fall | trend_up | gap_small (gap_go: gap_mid 1-2.5 %) | sector_agree | or_narrow`. Mechanisms: vix_fall = VIX under its previous close at the decision bar (implied vol being sold = market adding risk); trend_up = prev close above the stock's 20-day SMA (accumulation behind the breakout; proxy for the Nifty trend, which ctx does not expose as daily history); gap_small = |open gap| < 0.5 % (a big gap has discounted the news and attracts faders); sector_agree = equal-weight NSE-industry peers up on the day (shared information vs idiosyncratic noise); or_narrow = opening range <= 0.3 x daily ATR(14) (compression, tighter stop). Pre-results exclusion was skipped: no historical board-meeting file is cached and the event store holds 0 events. Pass criterion for a filter on explore, set before running: net R > base + 0.05 and > 0 with >= 100 trades; only such filters may be combined. 30 trials (5 x 6).

## Per-filter table (explore, 234 sessions; kept % = trades vs the unfiltered rule)

| base rule (net R, trades, none) | filter | trades | kept % | net R/trade | Rs/trade | t |
|---|---|---|---|---|---|---|
| orb15 (-0.232, 11485) | vix_fall | 5565 | 48 | -0.221 | -27 | -14.6 |
| | trend_up | 6291 | 55 | -0.248 | -34 | -17.5 |
| | gap_small | 8273 | 72 | -0.267 | -34 | -21.4 |
| | sector_agree | 7813 | 68 | -0.225 | -29 | -17.6 |
| | or_narrow | 2122 | 18 | -0.333 | -33 | -12.2 |
| orb30 (-0.208, 10172) | vix_fall | 5067 | 50 | -0.206 | -28 | -13.9 |
| | trend_up | 5534 | 54 | -0.229 | -32 | -16.2 |
| | gap_small | 7349 | 72 | -0.246 | -34 | -19.8 |
| | sector_agree | 6888 | 68 | -0.201 | -28 | -15.8 |
| | or_narrow | 1054 | 10 | -0.354 | -35 | -9.6 |
| pdh (-0.325, 6669) | vix_fall | 3412 | 51 | -0.313 | -27 | -14.9 |
| | trend_up | 4020 | 60 | -0.327 | -30 | -17.2 |
| | gap_small | 4860 | 73 | -0.354 | -32 | -20.6 |
| | sector_agree | 4718 | 71 | -0.320 | -28 | -18.1 |
| | or_narrow | 1150 | 17 | -0.273 | -16 | -7.4 |
| gap_go (-0.001, 543) | vix_fall | 341 | 63 | -0.010 | +1 | -0.3 |
| | trend_up | 248 | 46 | +0.003 | +4 | +0.1 |
| | gap_mid | 426 | 78 | -0.016 | -2 | -0.4 |
| | sector_agree | 479 | 88 | +0.022 | +12 | +0.7 |
| | or_narrow | 46 | 8 | +0.084 | +41 | +0.5 |
| oversold_bounce (-0.059, 359) | vix_fall | 161 | 45 | -0.207 | -60 | -3.5 |
| | trend_up | 0 | 0 | - | - | - |
| | gap_small | 216 | 60 | -0.154 | -40 | -2.6 |
| | sector_agree | 206 | 57 | -0.136 | -34 | -2.5 |
| | or_narrow | 58 | 16 | -0.449 | -85 | -3.8 |

Gross R of the unfiltered rules on explore: orb15 -0.045, orb30 -0.037, pdh -0.062, gap_go +0.066, oversold +0.036. No filter lifts any breakout rule's gross R above zero. Nothing met the pre-registered pass criterion (the closest, or_narrow on gap_go, is +0.085 over base on 46 trades, t 0.5), so no two-filter combinations were run.

## Validate (anchored walk-forward, 2 folds, 78 sessions; the fold trades the filter that was best on all prior data = the filter-mining test)

| id | filter chosen (fold 1 / fold 2) | trades | net R/trade | 95 % CI (R) | day-t | cost stress | breadth (symbols / months) | gates |
|---|---|---|---|---|---|---|---|---|
| flt_orb15 | vix_fall / vix_fall | 2293 | -0.233 | [-0.324, -0.144] | -4.8 | -0.327 | 17 % / 0 % | 2/8 rejected |
| flt_orb30 | sector_agree / none | 2933 | -0.169 | [-0.255, -0.085] | -3.8 | -0.256 | 23 % / 0 % | 2/8 rejected |
| flt_pdh | or_narrow / or_narrow | 424 | -0.311 | [-0.433, -0.184] | -4.2 | -0.442 | 28 % / 0 % | 2/8 rejected |
| flt_gapgo | or_narrow / sector_agree | 210 | +0.006 | [-0.242, +0.234] | +0.05 | -0.048 | 48 % / 60 % | 2/8 rejected |
| flt_oversold | none / none | 184 | -0.075 | [-0.258, +0.094] | -0.8 | -0.127 | 46 % / 20 % | 2/8 rejected |

Every id passed only min_trades and no_look_ahead (0 peeks, 0 late decisions, loader cut before 2026-07-09). Benchmark over the validate window: universe equal-weight Sharpe +1.34 (+8.3 %), NIFTY +0.49 (+2.7 %); every filtered rule below both. Trades kept after filtering stayed >= 100 on validate for all ids, so the rejections are not thin-sample artefacts.

**Filter-mining penalty**: 30 combinations this session; registry total 97 trials (team-wide). The DSR gate now demands a per-trade SR that beats 0.537 (what luck alone would show as the best of 97 trials); every id here scores DSR 0.00 (per-trade SR between -0.26 and +0.01). Each filter added by anyone raises this bar for everyone.

## Oversold bounce, the one base rule that had looked positive

- Yahoo 60 days, Nifty 50: +0.10R net, 119 trades, t 1.2. Angel 234 explore sessions, Nifty 100: +0.036R gross / -0.059R net, 359 trades, t -1.4; validate -0.075R on 184 trades. The earlier positive was a thin-sample fluctuation.
- Daily-bar context, 2015-02-03..2026-03-11 (99 stocks, 245,257 stock-days, cut before the validate split; by-hand study, no gates): the unconditional open-to-close return of Nifty 100 stocks is **-9.2 bp/day (day-clustered t -5.2)** while close-to-close is +8.1 bp: the overnight carries the return, the session gives it back. That is a structural headwind for every long intraday rule here. After RSI14 < 30 the open-to-close is +3.7 bp (t -1.6 on daily means, win 47.5 %), positive in 2019-2021 and 2024-2025, negative in 2015-2018, 2022-2023 and early 2026: no stable edge. `trend_up` is vacuous for this rule (RSI < 30 above the SMA20 occurred once in 11 years). Gap buckets: gap <= -0.5 % days bounce +50 bp open-to-close (t +2.9, 1,735 cases) while gap >= +0.5 % days give -15 bp (t -2.5): the pre-registered `gap_small` direction was wrong; the bounce, if any, is a gap-down-into-oversold reversal. Reported as context, not tested, to avoid mining it into the same registry today.

**Trials**: 30 this session; 97 in the registry (chain verified OK, 133 records).
**Survived**: none. No holdout opened.
**Confidence in the negative**: high for ORB15/ORB30/PDH (thousands of trades, day-t -4 to -5 after filtering, every filter and every month negative); medium for gap_go and oversold (184-210 validate trades, CIs straddle zero but cost stress and breadth fail and the explore t is near zero or negative).

**Next ideas** (each needs its own id and raises the DSR bar): (1) oversold + gap-down reversal (buy the first 15-min high after RSI < 30 and a >= 0.5 % gap down: forced morning sellers exhausting into a prior-day oversold tape), only if the daily +50 bp survives the next 60+ sessions of context; (2) the -9 bp intraday drift itself as a short-side context: shorts on the ORB breakdown (teammate family) should be re-read against it; (3) a filter that removes days rather than trades, e.g. no entries in the first 30 minutes after a >1 % NIFTY gap, tested as a time-of-day rule, not a regime overlay; (4) stop chasing overlays on rules whose gross R is negative: no filter in 30 trials moved a breakout rule's gross expectancy above zero.
