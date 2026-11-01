# Research memo, 2026-10-29: intraday short side on NSE cash equities (Nifty 100)

Research only; nothing was ordered, no config touched. Not financial advice; a backtest is not a forecast.

**Data**: frozen Angel One dataset (`trader/cache/research-dataset.json`, all 224 file sha256s verified, no calls made). Universe: Nifty 100 as tradables (99 traded; indices as context only). Shared split `angel:nifty100:5m`: explore 2025-04-01..2026-03-11 (234 sessions), validate 2026-03-12..2026-07-08 (78), holdout 2026-07-09..2026-10-29 (78, **never opened**). Issues read: feed-wide zero-volume bar on 2026-09-07 and the Muhurat short session (harmless); ENRIN/TATACAP/TMCV/VAML are short series (kept, fewer sessions).
**Engine**: lab_engine (sell first, cover by 15:05, 1 tick slippage, Dhan intraday charges: STT on the sell leg, stamp on the buy; capital Rs 1L, max loss Rs 500, 25 % cap). Validate = anchored walk-forward, 2 folds, variant chosen on the data before each fold.

Tried (hypothesis -> result). W = win %, R = net R/trade, Rs = net rupees/trade; explore shows the best of the grid, validate the walk-forward.

| id | mechanism | split | trades | W | net R | Rs/trade | gates | main failure |
|---|---|---|---|---|---|---|---|---|
| short_failed_orb: close above OR15/30 high then back inside within 30 min; stop = trap high (>= 0.4 %), target 1.5/2.5R (4 combos) | trapped breakout buyers forced out | explore | 5,837 | 40.9 | -0.235 | -25 | - | gross R +0.003: no edge before costs |
| | | validate | 2,044 | 38.0 | -0.224 | -23 | 2/8 | CI [-0.31, -0.14]; charges = 0.21R (R only Rs 115 on a tight stop) |
| short_orb_index_cond: OR breakdown only with NIFTY under its VWAP / own OR low and sector peers down from the open; stop OR high, 2R (4) | index-level sell programmes persist for hours | explore | 3,059 | 47.5 | -0.024 | -7 | - | best gross +0.068R, t -1.9 |
| | | validate | 1,016 | 42.4 | -0.162 | -60 | 2/8 | 74 % of trades flat at 15:05, 2 % reach 2R; CI [-0.37, +0.04] |
| short_gap_fade: gap up >= 0.75/1.5 %, first close under the 15-min low by 11:30; stop OR15 high; target gap-fill or 2R (4) | pre-open over-reaction, retail gap buyers trapped | explore | 314 | 42.7 | -0.101 | -44 | - | gross -0.049R; gap-fill target almost never reached |
| | | validate | 211 | 36.5 | -0.241 | -87 | 2/8 | 0 % targets, 71 % flat, 29 % stops |
| short_rel_weak: at 10:15 stock vs sector peers <= -0.5/1.0 x daily ATR and under VWAP; stop day high (>= 0.5 %), 1.5/2.5R (4) | informed, VWAP-sliced selling continues | explore | 1,294 | 42.6 | -0.083 | -26 | - | gross -0.006R |
| | | validate | 358 | 46.9 | -0.082 | -21 | 2/8 | 85 % flat; CI [-0.18, +0.02]; k=1 cuts trades to 178 and loses more |
| short_late_low_break: 13:30-14:30 new session low under VWAP with NIFTY below its open / VWAP; stop 1-hour high (>= 0.5 %), 1R/2R (4) | MIS auto-square-off unwind of intraday longs on weak days | explore | 2,520 | 40.8 | -0.157 | -26 | - | gross -0.001R |
| | | validate | 722 | 51.8 | -0.003 | +4 | 2/8 | CI [-0.19, +0.17], t -0.03, cost stress -0.09R, Sharpe 0.39 < universe 1.34 |

Every id passed only min_trades and no_look_ahead (0 peeks, loader cut at 2026-07-09). None became a candidate; no holdout was run.

**Trials**: 20 this session (5 ids x 4 combos), 63 in the registry (chain verified OK, 91 records). Luck alone would now show a per-trade SR of ~0.59; the best short idea's SR was -0.004.

**What the numbers say**
1. The signals carry no gross edge: gross R per trade on validate is +0.005, -0.090, -0.178, -0.015 and -0.001. Charges then take 0.05-0.22 R (Rs 18-26 a trade on ~Rs 24k positions). The 0.4-0.5 % stop floors make R Rs 115-185; wider stops (OR-30 high, Rs 300-420 R) cut the drag to 0.05-0.09 R but the targets stop being reached (2-25 % hit rate) and most trades die at flat-by.
2. The index/sector conditioning did what it should on explore (gross from +0.00 to +0.07R, 2.7x fewer trades) but did not survive validate (Mar-Jul 2026: universe equal-weight +8.3 %, Nifty +2.7 %), and the engine's flat-by 15:05 means a sell programme that works into the close is cut short.
3. The late-session short is the only one near zero net in a rising validate period (+Rs 4 a trade, W 52 %) but has no statistical content (t -0.03) and fails cost stress.
4. Explore (Apr 2025-Mar 2026, which includes a correction) was already negative for all five, so this is not only "shorts in a bull market".

**Survived**: none.
**Confidence**: high that these five rules, as written, lose after Dhan intraday charges on Nifty 100 5-minute bars (2,000-6,500 explore trades, 211-2,044 validate trades, CIs mostly entirely below zero). Low confidence about the mechanisms themselves beyond the ~1.3-year window: one regime, model fills, no circuit/liquidity checks, flat-by 15:05 before the 15:15-15:20 square-off wave.

**Next ideas** (each with its mechanism; not yet logged, no trials spent)
- (a) Short only where the stock's OWN sector index (NIFTYBANK, NIFTYIT, ...) is under its OR low, using the cached index bars through the engine (needs a `ctx.sector_index` view in lab_engine so the holdout cut applies); peers' equal-weight return is a noisy proxy for a cap-weighted sector.
- (b) Late-session short with a later flat-by (15:15) in a one-off engine variant, to test the square-off mechanism where the flow actually is; needs the engine's window made a META option before it is a fair test.
- (c) Gap-up fade conditioned on NIFTY's gap being small (stock gap - index gap >= 1 %): the stock-specific over-reaction, not the market's.
- (d) Stop width as a design constraint: anything with a stop under ~1 % of price pays >= 0.1 R in charges on Rs 25k positions; future short ideas should state the target in rupees against Rs 26 before being logged.
