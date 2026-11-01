# Research memo, 2026-10-29: intraday time-of-day effects (NSE, Nifty 100)

Research only. No orders, no config changes. Not financial advice; a backtest is not a forecast.

**Data**: frozen Angel One dataset (`trader/cache/research-dataset.json`, 224 files, all sha256 verified), Nifty 100 5-minute bars with NIFTY 50 and India VIX context, offline. Shared split `angel:nifty100:5m` (created through `research.py explore`): explore 2025-04-01..2026-03-11 (234 sessions, 99 stocks with bars: VAML has none before 2026-03-12), validate 2026-03-12..2026-07-08 (78), holdout 2026-07-09..2026-10-29 (78, **locked, never opened**). The Yahoo nifty50 holdout dates (2026-10-13..29) are also withheld for the 50 shared symbols (inside the Angel holdout anyway). Effects were measured on the explore split only, with day-clustered t-stats (one observation per day = the cross-sectional mean of signalled stock-days). Cost bar: a Rs 25,000 position pays ~Rs 26 a round trip = 0.104 %, so a rule needs a raw signed move of ~0.21 % per trade before it is worth a trial.

## Per-idea table (explore split, 22,830 stock-days unless stated)

| idea (mechanism) | measurement | signed mean / trade | t (day) | vs 0.21 % bar | outcome |
|---|---|---|---|---|---|
| Intraday momentum (Gao-Han-Li-Zhou 2018: late-informed / rebalancing flow repeats the first half-hour into the close) | NIFTY prev-close->09:45 sign -> stock 14:30->15:05, \|sig\|>=0.3 % | +0.029 % (99 days) | +1.2 | no | **rule tested: tod_intraday_momentum, REJECTED 2/8** |
| same, stock's own first half-hour | \|sig\|>=0.5 %, 14:30->15:05 | +0.006 % | +0.7 | no | inside the same rule (grid signal=stock) |
| same, index-level one obs/day | \|sig\|>=0.5 %, NIFTY 14:30->15:05 | -0.010 % (54 days) | -0.3 | no | effect does not transfer; at >=1 % it flips (-0.14 %, 10 days) |
| Opening-auction aftermath: reversal of the first 5-min bar | -sign(bar1) x 09:20->10:00, \|bar1\|>=0.5 % | -0.006 % | -0.4 | no | not built |
| Opening-auction aftermath: fade the overnight gap | -sign(gap) x 09:15 open->09:30, \|gap\|>=1 % / >=2 % | +0.15 % / +0.26 % | +2.7 / +2.4 | real, but before the first allowed entry | not tradable: from the 09:30 close on it is +0.01 %/-0.02 % (t<1.1); a teammate's `short_gap_fade` tested the tradable version (rejected) |
| Lunch drift / continuation (thin book carries the morning move) | sign(09:45->12:00) x 12:00->13:30, \|am\|>=1 % | +0.041 % | +2.1 | no (Rs +10 gross, -16 net) | not built |
| Lunch reversal | unconditional 12:00->13:30 | +0.003 % | +0.2 | no | nothing there |
| Pre-close positioning 14:30->15:05 (pre-CAS period) | unconditional; sign(13:30->14:30) x last, >=0.5 % | +0.033 %; -0.001 % | +2.4; -0.1 | no | drift into the close exists but is Rs 8 on Rs 25k |
| Closing auction natural experiment (CAS since 2026-08-03) | before/after comparison | - | - | - | **untestable now**: every post-2026-08-03 session is inside the locked holdout; explore and validate are both pre-CAS |
| Day of week / Tuesday weekly expiry (hedging flows) | unsigned 14:00->15:05 by weekday | Mon +0.09 %, Tue -0.03 %, Wed +0.07 %, Fri -0.05 % (44-48 days each) | 2.4 / -0.8 / 2.4 / -1.5 | no | Tuesday shows nothing special; Mon/Wed full-day +0.33 %/+0.21 % is beta in an up-year, not an intraday edge |
| Regime filters on momentum (VIX>=15, \|gap\|>=0.5 %, small gap) | signed last window | -0.01 % / -0.02 % / +0.06 % | <1.5 | no | not built |

Rule result: `tod_intraday_momentum` (4 combinations: thr 0.3/0.5 x signal index/stock; enter at the 14:30 close in the signal's direction, disaster stop one session-range away, timed exit at 15:05). Explore: best variant signal=index thr=0.3, 9,446 trades, gross R +0.012, net R -0.047, -Rs 17 a trade. Validate (2 anchored folds, 78 sessions): 5,793 trades, win 42 %, net -Rs 12 a trade, CI of net R [-0.056, -0.004], daily t -2.3, DSR 0.00, cost stress -Rs 1.46 L, breadth 9 % of stocks, Sharpe -4.1 vs universe +1.3 -> REJECTED. The first explore of this id was wasted on a bug (14:30 bar indexed as k=63 instead of 62: 0 trades); the fix was re-explored and counted again, so the id carries 8 trials.

**Trials**: 8 this session (4 of them the bug), 83 in the registry at the time of writing (teammates add in parallel); chain verified OK.
**Survived**: none. No holdout run.
**Confidence**: medium that there is nothing tradable here at Rs 25k size: 234 sessions x 99 stocks give tight day-clustered errors, and every effect is an order of magnitude under the cost bar except the gap fade, which is spent by 09:30. Low on anything post-CAS (not observable). One regime only (Nifty up ~8 %, no crash), so the weekday numbers are not stable.

**Next ideas** (each needs the mechanism to survive the 0.21 % bar first):
1. Closing-auction study once the holdout window is legitimately released or new post-CAS sessions accrue outside it: compare 14:30->15:05 drift and 15:00->15:15 imbalance in F&O vs non-F&O Nifty 100 names (index-fund / closing-price flow now routed to the CAS).
2. Gap fade at the 09:15 print itself (pre-open order at the auction price, exit 09:30): the +0.15-0.26 % is there, but it needs a model of auction fills and partial allocations, and the data to test it is the pre-open order book, not 5-minute bars.
3. Lunch continuation only on idiosyncratic moves >=1.5 % with a news filter (events recorder, when ~500 events exist): +0.08 % raw is still under the bar, so only worth it if the event-conditioned subset is 3x larger.
4. Monday 14:00->15:05 drift as a filter on someone else's long rule rather than a rule of its own (Rs 23 gross on Rs 25k).

Sources: Gao, Han, Li, Zhou (2018) "Market intraday momentum", JFE 129(2) 394-414; NSE Closing Auction Session for F&O stocks effective 2026-08-03 (15:15-15:35, continuous trading ends 15:15; outlookmoney.com, fundsindia.com, geojit support notes).
