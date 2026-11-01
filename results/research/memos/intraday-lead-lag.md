# Research memo, 2026-10-29: intraday sector lead-lag and peer patterns

Research only; no orders, no config changes. Not financial advice; a backtest is not a forecast.

**Data**: the frozen Angel One dataset (`trader/cache/research-dataset.json`, 112 symbols, sha256s verified, 0 mismatches; issues: one short session 2025-10-21, ~35 zero-volume bars per stock on 2026-09-07, ENRIN 338 sessions). Shared key `angel:nifty100:5m` (100 Nifty 100 tradables, Nifty 500 industry groups as peers): explore 2025-04-01..2026-03-11 (234 sessions), validate 2026-03-12..2026-07-08 (78), holdout 2026-07-09..2026-10-29 (78, **locked, never opened**). Sector indices are not visible to `ctx`, so strategies use the equal-weight industry peer basket as the causal sector proxy; the measurement below used the real indices on explore dates only.
**Cost yardstick**: Rs 26 round trip on a Rs 25k position = 10.4 bps, plus 1 tick slippage a side.

## 1. The lead-lag measurement (explore only, `scratchpad/leadlag_measure.py`)

- **Power group pairwise 5-minute return cross-correlation** (lead -> lag, first half | second half of explore): contemporaneous 0.22-0.67 (NIFTYENERGY->TATAPOWER 0.67|0.65, ADANIGREEN<->ADANIENSOL 0.67|0.67, NTPC->POWERGRID 0.48|0.43). **Lag 1: +0.00 to +0.05** (NIFTYENERGY->TATAPOWER +0.044|+0.025, ADANIGREEN->POWERGRID +0.020|+0.041, NTPC->POWERGRID +0.020|+0.036, NTPC->TATAPOWER +0.024|+0.027; the reverse TATAPOWER->NTPC +0.012|+0.014). Lags 2-4: within +-0.04 with sign flips between halves. A 0.03 lag-1 correlation on ~12 bps 5-minute moves is ~0.3 bps of expected drift: not measurable against 10.4 bps.
- **Sector index -> laggard constituents** (index 15-min move >= 2 sigma, member moved < 1/3 of it; next 15 / 30 / 60 min signed, bps, day-clustered t): NIFTYBANK -0.2/+1.7/+3.3; NIFTYIT +2.9/+2.6/+4.7; NIFTYENERGY -0.4/+1.0/+1.8; NIFTYAUTO +0.2/+0.8/+3.3; NIFTYMETAL +2.0/+1.8/-0.9; NIFTYPHARMA +0.7/+0.2/-2.1; NIFTYFMCG +0.8/+2.0/+2.8; NIFTYPSUBANK -0.5/-3.4/-4.2. All |t| < 2, all below cost. At a bigger threshold (NIFTYENERGY >= 40 bps -> power laggards) +6.5/+14.5/+17.2 bps, t 0.9-1.3, n 198.
- Members that had **already moved** with the index: -1 to -2 bps over the next 15 min with t -2.0 to -3.4 (NIFTYENERGY -2.0, t -3.4; NIFTYIT t -2.8): the lag is, if anything, a small reversal of the movers, statistically there and economically nil.
- **NIFTY -> large caps at the open** (NIFTY 09:15 open -> 09:30 close): at >= 30 bps laggards +2.2 bps over 30 min (t 0.4); at >= 50 bps +15.7 bps over 30 min (t 1.2, n 458), +6.5 at 60 min, +16.8 to the close (t 0.7).
- **First-hour relative strength vs the industry basket -> afternoon**: quintile spread (long Q5, short Q1) -1.4 bps a leg (t -0.8) for 10:15->15:05; -1.9 bps stock-minus-basket; -0.5 bps for 13:00->15:05. Nil.
- Scale: median |15-min| move of the power stocks 11-17 bps, |60-min| 21-34 bps. The whole typical move is 1-3x the round-trip cost.

## 2. Engine tests (per-idea table)

| id | hypothesis / rules | explore (best variant) | validate (walk-forward, 2 folds) | gates |
|---|---|---|---|---|
| `sector_basket_lag` (4 combos) | industry peer basket moved >= 0.4 / 0.7 % in 15 min with >= 2/3 of peers agreeing, stock < 1/3 of it -> follow; stop 2 x ATR(14), target r = 1 / 2 | 0.7 %, r=2: 653 trades, win 43 %, gross +0.10 R, **net -0.03 R**, -Rs 10/trade; 0.4 %: 4,242 trades, net -0.13 to -0.14 R | 373 trades, net -0.10 R, -Rs 0.7/trade, CI [-0.27, +0.08] R; cost stress -0.18 R; 36/72 symbols, 2/5 months positive | 2/8, rejected |
| `open_index_laggard` (2 combos) | NIFTY >= 0.5 % from its open by 09:30, stock < 1/3 of it -> follow at 09:30; stop = low/high of the first 3 bars (>= 0.2 %), target r = 1 / 2 | r=1: 384 trades, win 58 %, gross +0.13 R, **net -0.07 R**, -Rs 14/trade | 307 trades, net -0.38 R, -Rs 53/trade, CI [-0.58, -0.12] R; 0/4 months positive | 2/8, rejected |
| power leader -> laggard | measured only: lag-1 correlations <= 0.05 (above); the Power subset of `sector_basket_lag`: 142 explore trades net -0.04 R (gross +0.14 R), 107 validate trades net -0.15 R | not logged: no stable lag large enough to pay for a trial | - | - |
| RS persistence | measured only: quintile spread -1.4 bps a leg | not logged | - | - |

Pattern in both engine tests: a small positive gross R on explore (+0.10 to +0.13) that costs eat, then a worse validate. The lag exists at the few-bps level and is not tradable in cash equity at Dhan's charges.

**Trials**: 6 this session (this family), 91 in the registry (22 experiments, chain verified OK). **Survived**: none. No holdout opened.

## 3. Tata Power

- Peers (NSE industry "Power" inside the Nifty 100): NTPC, POWERGRID, ADANIPOWER, ADANIGREEN, ADANIENSOL. Daily-return correlation (holdout dates removed): NIFTYENERGY 0.69, NTPC 0.60, PFC 0.59, JINDALSTEL 0.54, ADANIPOWER 0.49, ADANIENSOL 0.44, POWERGRID 0.42. Traded value ~Rs 193 cr a session on explore (ADANIPOWER 398, NTPC 371, POWERGRID 360): it is the second-smallest name in its group, which fits it being a follower.
- 5-minute: contemporaneous correlation 0.65 with NIFTYENERGY, 0.41 with NTPC. It is slightly **led** rather than leading (NTPC->TATAPOWER lag 1 +0.024|+0.027 vs TATAPOWER->NTPC +0.012|+0.014; NIFTYENERGY->TATAPOWER +0.044|+0.025), but the magnitudes are 0.02-0.04.
- Conditional, TATAPOWER lagging a leader's 15-min move (explore, next 15/30/60 min, bps): NTPC >= 40 bps: 0.0 / +3.6 (t 1.8) / **+12.0 (t 2.5, n 360, 129 days)**; NTPC >= 60 bps: +1.8 / +9.8 / +24.0 (t 2.3, n 106, 46 days); POWERGRID >= 40: -0.3 / +1.5 / +6.5 (t 2.1); NIFTYENERGY >= 30: -3.7 / +13.9 / +20.7 (t <= 1.5, 39 days); NIFTY >= 30: -1.8 / -1.1 / +10.2 (t < 1); ADANIPOWER >= 60: ~0.
- Verdict: the NTPC -> TATAPOWER 60-minute drift is the only number in this whole family above the 10.4 bps cost with t > 2, but it is one stock, explore only, found after ~20 looks (7 leads x 3 horizons), and nothing shows in the first 15-30 minutes. A TATAPOWER-only rule cannot reach the 100-trade validate gate (78 sessions, one trade a day), so it was not logged. In the engine, `sector_basket_lag` traded TATAPOWER 32 times on explore (+0.09 R) and 22 times on validate (-0.32 R). **Nothing tradable exists for Tata Power on its own after costs.** Watch item only: NTPC >= 0.6 % in 15 min with TATAPOWER flat, 60-minute horizon, to be tested as one power-group rule over all five followers (so it can reach 100 trades), 2 combos at most.

## 4. Confidence and next ideas

**Confidence** in the negative result: medium. Samples are large (thousands of events, 234 explore and 78 validate sessions, 100 stocks); effect sizes are 0-5 bps against a 10.4 bps cost; both engine tests are net negative on explore and validate; the two positive spots (open laggards +15.7 bps, NTPC->TATAPOWER +12 bps) have t 1.2-2.5 after many looks. Regime caveat: Nifty +8 % over the period, no crash day; lead-lag may widen in a panic. The DSR bar is now 91 trials.

**Next ideas** (each with its mechanism):
1. Power-group 60-minute follow after an NTPC >= 0.6 % 15-min move (slow diffusion from the PSU bellwether to the four other followers; one rule, all followers, target 1 R with a 2 x ATR stop as the hold proxy). The only measured spot above cost; 2 combos.
2. Movers' 15-minute reversal (t -2 to -3.4, 1-2 bps): the robust statistical signal here, tradable only where the round trip costs < 3 bps, i.e. not in cash equity at these charges; worth noting for the options / futures teams, not for us.
3. Daily-horizon sector lead-lag (Hou 2007 works at weekly horizons): sector index close-to-close -> laggard next day. Swing, the engine does not run it; log as an idea only.
4. Event-driven lag (NSE announcements with `lag_min`): wait for ~500 complete events before testing.

Files: `trader/research/lab/sector_basket_lag.py`, `trader/research/lab/open_index_laggard.py`, `trader/research/runs/sector_basket_lag-*.json`, `trader/research/runs/open_index_laggard-*.json`; measurement script and output in the session scratchpad (`leadlag_measure.py`, `measure_out.txt`, `tatapower_measure.py`).
