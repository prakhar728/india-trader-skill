# Research memo, 2026-10-29: intraday mean reversion on NSE cash equities (Nifty 100)

Research only; no orders, no config changes. Not financial advice; a backtest is not a forecast.

**Data**: frozen Angel One dataset (`trader/cache/research-dataset.json`, 112 sha256s verified, 0 mismatches), 100 Nifty 100 stocks as tradables, NIFTY as context; sector peers = NSE Nifty 500 industry groups. Manifest issues are uniform (one zero-volume bar on 2026-09-07, the 2025-10-21 Muhurat session) so no series was excluded; TATACAP / TMCV / VAML / ENRIN are short listings and simply trade fewer sessions. Shared split `angel:nifty100:5m`: explore 2025-04-01..2026-03-11 (234 sessions), validate 2026-03-12..2026-07-08 (78), holdout 2026-07-09..2026-10-29 (78, never opened). No Angel or Yahoo calls.

**Sizing and costs (engine defaults, no `trader/config.json`)**: capital Rs 1L, max loss Rs 500 incl. charges, 25 % position cap. Both logged rules use a 1 % stop so the cap binds: average position Rs 24.1k, risk ~Rs 240, Dhan round trip Rs 25.7 (brokerage 15, STT 6, exchange 1.5, stamp 1, GST 3) = 0.107 % of the position = 0.11 R. Charges are proportional (Rs 53 on Rs 50k), so a bigger position does not lower their share: a rule needs a gross edge above ~0.11 % a trade (0.21 % for a two-leg pair) to break even.

**Method**: one explore-only prototype pass (dates capped at 2026-03-11, gross moves, no charges, in `scratchpad/proto_mr.py`) to check signal counts and bug-free logic for four ideas; the two with a visible gross edge went through `research.py log / pipeline`; the two with none were not logged (no trials spent). 4 trials this session, 77 in the registry (chain verified OK).

## Per-idea table

| idea (mechanism) | split | trades | win % | net R/trade | Rs/trade | charges % of gross | gates | main failure |
|---|---|---|---|---|---|---|---|---|
| **eod_sector_laggard**: buy the stock >= th % below its industry peers' mean at 14:00-14:30, stop/target 1 %, hold to 15:05 (liquidity providers' inventory unwind + closing-basket demand) | explore th=2 | 1,039 | 40.0 | -0.084 | -20.5 | 507 % (gross +Rs 5/trade) | - | - |
| | explore th=2.5 | 591 | 42.3 | -0.067 | -16.5 | 284 % (gross +Rs 9/trade) | - | - |
| | validate (th=2.5) | 269 | 36.4 | -0.134, CI [-0.22, -0.04] | -32.7, CI [-54, -11] | gross -Rs 7/trade; charges 369 % of \|gross\| | 2/8 (min_trades, no_look_ahead) | gross edge ~0 vs 0.11 R charges; 0 of 5 months positive; Sharpe -5.3 vs universe +1.34 |
| **sector_pair_z**: 15 same-industry pairs, z of ln(A/B) over 30 bars, enter \|z\| >= zt at 10:30-14:30, target = ratio mean, stop 1 %, both legs as separate trades (arbitrage desks close transient order-flow divergences) | explore zt=2 | 5,908 | 51.5 | -0.111 | -26.8 | gross -Rs 1/leg; charges 26x \|gross\| | - | - |
| | explore zt=2.5 | 3,184 | 47.2 | -0.104 | -25.0 | gross +Rs 0.8/leg; charges 33x gross | - | - |
| | validate (zt=2.5) | 1,108 | 48.8 | -0.114, CI [-0.14, -0.09] | -27.7, CI [-35, -21] | gross -Rs 2/leg; charges = the whole loss | 2/8 (min_trades, no_look_ahead) | zero gross edge, two legs = Rs 51 a pair-trade; 3 of 30 symbols, 0 of 5 months positive |
| **gap fade** (not logged): fade a >= g % open gap vs prev close, Nifty \|gap\| < 0.3 %, gap against the prior day's direction (overnight single-stock overreaction) | explore prototype, gross to 15:00 | 330 (g=1) / 146 (g=1.5) | 49 / 47 | - | gross -0.03 % / -0.12 % of position (= -Rs 7 / -Rs 29) | charges > 100 % | not run | gaps against the prior trend CONTINUE; without the trend filter +0.075 % (n=808, t=1.3) still under the 0.107 % round trip. A short gap fade is also a teammate's id (`short_gap_fade`) |
| **vwap_range_day** (not logged): fade >= k ATR stretch from VWAP after 11:00 only when NIFTY is inside its 09:15-10:15 range (quiet-day liquidity provision) | explore prototype, gross to 15:00 | 11,863 (k=2.5) / 3,575 (3.0) / 1,106 (3.5) | 50 / 50 / 49 | - | gross -0.02 % / -0.01 % / -0.015 % of position | charges > 100 % | not run | no gross reversion at all; identical on trend days (-0.015 / -0.02 / -0.06 %). Same conclusion as the example `vwap_reversion` (-0.67 R) for a different reason: the stretch does not revert, the earlier loss was stops |

Explore prototype extras: the EOD short side (sector leaders at 14:00) was -0.01 % gross (n=1,110 at 2 %), hence long-only; the laggard long was +0.04..+0.06 % gross (t 3.2) on explore but -0.03 % on validate, so even the gross effect did not hold.

## Verdict

**Survived**: none. No holdout run. Both logged ids rejected on validate with 2/8 gates; CIs are entirely negative, so this is not a near miss.

**Confidence**: high that these rules lose after charges (269 and 1,108 validate trades, tight day-clustered CIs, 5 months all negative); medium-high that the whole family is dead at this cost structure: across four designs the gross per-trade edge measured 0 to +6 bps while one round trip costs 10.7 bps and scales with position size. The period (Apr 2025 - Jul 2026, Nifty +8 %, no crash) had few extreme days, and the engine has no liquidity or circuit checks, so real fills would be worse, not better.

**Next ideas** (each with its mechanism; none fits the current intraday engine, so they are proposals, not trials):
1. Closing-auction reversion: the laggard's reversion may sit in the 15:05-15:30 window (index/closing-basket demand) that flat-by 15:05 forbids; measurable from bars without trading, but trading it needs a flat-by change, which is the user's call, not research's.
2. Overnight laggard reversion (swing): buy the >= 2.5 % sector laggard at 15:00, sell at the next open (overnight inventory unwinding without the intraday STT/brokerage of two trades a day) — needs the swing engine.
3. Pairs held to z = 0 across sessions (swing): the intraday 30-bar z is noise (49 % win, zero gross); a daily-bar z over 20 sessions with a 1-5 day hold gives the arbitrage mechanism time to act and amortises the Rs 51 pair charge.
4. Extreme idiosyncratic moves only (>= 5 % vs peers, circuit-band proximity): ~1.3 events a session on explore, so ~100 validate trades would need a full year of validate data; park until the dataset is longer.
