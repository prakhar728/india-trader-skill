# Backtest: RELIANCE, orb15, orb30, pdh, gap_go, vwap_pullback, orb_pullback, oversold_bounce

1 stocks, 2025-05-12 to 2026-10-29 (365 sessions), source Angel One SmartAPI, slippage 1 tick(s). Made 2026-10-29T17:09:16+05:30.

| strategy | trades | win % | avg R gross | avg R net | Rs/trade | PF | OOS variant | OOS trades | OOS net R | verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| orb15 | 203 | 31.5 | -0.15 | -0.39 | -43 | 0.48 | r=2 stop=or_low | 56 | -0.11 | no edge, even before charges (-0.15R gross, -0.39R net) |
| orb30 | 182 | 31.3 | -0.09 | -0.32 | -34 | 0.55 | r=2 stop=or_low | 51 | -0.12 | no edge, even before charges (-0.09R gross, -0.32R net) |
| pdh | 107 | 31.8 | -0.16 | -0.52 | -39 | 0.50 | r=1 stop=day_low | 36 | -0.22 | no edge, even before charges (-0.16R gross, -0.52R net) |
| orb_pullback | 150 | 34.7 | -0.11 | -0.36 | -39 | 0.51 | r=2 stop=or_mid (default) | 40 | -0.38 | no edge, even before charges (-0.11R gross, -0.36R net) |
| vwap_pullback | 263 | 28.5 | -0.19 | -0.91 | -35 | 0.30 | r=1 | 85 | -0.82 | no edge, even before charges (-0.19R gross, -0.91R net) |
| gap_go | 6 | 50.0 | +0.12 | +0.02 | +2 | 1.04 | gap=1 r=2 stop=or_low (default) | 1 | +0.87 | too few trades to judge (6) |
| oversold_bounce | 7 | 57.1 | +0.09 | -0.02 | +19 | 1.20 | r=2 rsi=30 stop=or_low (default) | 2 | +0.45 | too few trades to judge (7) |

## Rules

- **orb15**: Opening range = the high and low of the 09:15, 09:20 and 09:25 bars. Buy the close of the first bar that closes above the range high (bar closing 09:35 to 14:30). Stop at the range midpoint (variant: range low). Target 2R (variants 1R, 1.5R, 3R).
- **orb30**: Opening range = the first six 5-minute bars (09:15 to 09:45). Buy the close of the first bar that closes above the range high (closing 09:50 to 14:30). Stop at the range midpoint (variant: range low). Target 2R (variants 1R, 1.5R, 3R).
- **pdh**: Buy the close of the first bar (closing 09:30 to 14:30) that crosses above the previous session's high: it closes above it and the bar before closed at or below it (a stock that opens above and stays above never crosses). Stop at the lowest low of the trigger bar and the two bars before it (variant: the day's low so far). Target 2R (variants 1R, 1.5R, 3R).
- **orb_pullback**: After a bar closes above the 15-minute range high (the ORB trigger), wait for a bar whose low comes back to the range high; buy the close of the first bar from then on that closes above the range high (closing 09:30 to 14:30). A close under the range midpoint first cancels the day. Stop at the range midpoint (variant: the pullback's low). Target 2R (variants 1.5R, 3R).
- **vwap_pullback**: Session VWAP from 09:15 (typical price x volume). From 10:00: once a bar has closed above VWAP, wait for a bar whose low touches VWAP; buy the close of the next bar that closes back above VWAP (within 3 bars of the touch, else start over), closing 10:00 to 14:30. Stop one tick under the pullback's low. Target 2R (variants 1R, 1.5R, 3R).
- **gap_go**: The day opens at least 1 % above the previous close (variant 2 %) and the first 15 minutes' low stays above the previous close. Buy the close of the first bar that closes above the first 15 minutes' high (closing 09:35 to 14:30). Stop at the 15-minute low (variant: its midpoint). Target 2R (variants 1.5R, 3R).
- **oversold_bounce**: Daily RSI(14, Wilder) under 30 at yesterday's close. Today buy the close of the first bar that closes above the first 15 minutes' high (closing 09:35 to 14:30). Stop at the 15-minute low (variant: its midpoint). Target 2R (variants 1.5R, 3R).

## Caveats

- Sample: 365 sessions (2025-05-12 to 2026-10-29), 365 stock-days. A few dozen sessions can flatter or bury any rule; the out-of-sample part is only 122 sessions.
- Survivorship: the universe is today's list (RELIANCE) applied to the past; stocks that dropped out of it are missing, and stocks that joined after a big run are in.
- Data: Angel One SmartAPI 5-minute bars. 134 bars were missing inside the sessions used and 1 short session(s) were left out; F&O stocks have no 15:20 bar since NSE's closing auction. Bars are the feed's, not NSE's tape.
- Fills: entry at the trigger bar's close + 1 tick(s), stops and flat-by exits 1 tick(s) worse, gaps through the stop at the open; targets fill exactly. Real fills in a fast market can be worse.
- Charges: nse.charges: Dhan's intraday equity schedule (brokerage Rs 20 or 0.03 % an order, STT 0.025 % on the sell, NSE txn 0.0030699 %, SEBI, stamp 0.003 % on the buy, GST 18 %), on the shares india-budget would size (max loss Rs 500 a trade incl. charges, capital Rs 1,00,000).
- Long only, one trade per stock per strategy per day; no news, circuit limits, or liquidity checks.
- A backtest is not a forecast and never places an order. Not financial advice.
