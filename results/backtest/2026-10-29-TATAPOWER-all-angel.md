# Backtest: TATAPOWER, orb15, orb30, pdh, gap_go, vwap_pullback, orb_pullback, oversold_bounce

1 stocks, 2025-05-12 to 2026-10-29 (365 sessions), source Angel One SmartAPI, slippage 1 tick(s). Made 2026-10-29T17:08:39+05:30.

| strategy | trades | win % | avg R gross | avg R net | Rs/trade | PF | OOS variant | OOS trades | OOS net R | verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| pdh | 99 | 37.4 | -0.09 | -0.38 | -21 | 0.75 | r=1 stop=day_low | 28 | +0.04 | no edge, even before charges (-0.09R gross, -0.38R net) |
| orb30 | 154 | 37.7 | -0.05 | -0.23 | -31 | 0.67 | r=3 stop=or_low | 56 | -0.24 | no edge, even before charges (-0.05R gross, -0.23R net) |
| orb15 | 184 | 34.2 | -0.11 | -0.30 | -43 | 0.58 | r=3 stop=or_low | 60 | -0.25 | no edge, even before charges (-0.11R gross, -0.30R net) |
| orb_pullback | 127 | 36.2 | -0.05 | -0.25 | -36 | 0.62 | r=3 stop=or_mid | 46 | -0.31 | no edge, even before charges (-0.05R gross, -0.25R net) |
| vwap_pullback | 264 | 27.3 | -0.28 | -0.84 | -41 | 0.36 | r=1 | 88 | -0.80 | no edge, even before charges (-0.28R gross, -0.84R net) |
| gap_go | 5 | 60.0 | -0.01 | -0.09 | -51 | 0.49 | gap=1 r=2 stop=or_low (default) | 1 | +1.01 | too few trades to judge (5) |
| oversold_bounce | 9 | 55.6 | +0.12 | -0.00 | +38 | 1.56 | r=2 rsi=30 stop=or_low (default) | 7 | +0.13 | too few trades to judge (9) |

## Rules

- **pdh**: Buy the close of the first bar (closing 09:30 to 14:30) that crosses above the previous session's high: it closes above it and the bar before closed at or below it (a stock that opens above and stays above never crosses). Stop at the lowest low of the trigger bar and the two bars before it (variant: the day's low so far). Target 2R (variants 1R, 1.5R, 3R).
- **orb30**: Opening range = the first six 5-minute bars (09:15 to 09:45). Buy the close of the first bar that closes above the range high (closing 09:50 to 14:30). Stop at the range midpoint (variant: range low). Target 2R (variants 1R, 1.5R, 3R).
- **orb15**: Opening range = the high and low of the 09:15, 09:20 and 09:25 bars. Buy the close of the first bar that closes above the range high (bar closing 09:35 to 14:30). Stop at the range midpoint (variant: range low). Target 2R (variants 1R, 1.5R, 3R).
- **orb_pullback**: After a bar closes above the 15-minute range high (the ORB trigger), wait for a bar whose low comes back to the range high; buy the close of the first bar from then on that closes above the range high (closing 09:30 to 14:30). A close under the range midpoint first cancels the day. Stop at the range midpoint (variant: the pullback's low). Target 2R (variants 1.5R, 3R).
- **vwap_pullback**: Session VWAP from 09:15 (typical price x volume). From 10:00: once a bar has closed above VWAP, wait for a bar whose low touches VWAP; buy the close of the next bar that closes back above VWAP (within 3 bars of the touch, else start over), closing 10:00 to 14:30. Stop one tick under the pullback's low. Target 2R (variants 1R, 1.5R, 3R).
- **gap_go**: The day opens at least 1 % above the previous close (variant 2 %) and the first 15 minutes' low stays above the previous close. Buy the close of the first bar that closes above the first 15 minutes' high (closing 09:35 to 14:30). Stop at the 15-minute low (variant: its midpoint). Target 2R (variants 1.5R, 3R).
- **oversold_bounce**: Daily RSI(14, Wilder) under 30 at yesterday's close. Today buy the close of the first bar that closes above the first 15 minutes' high (closing 09:35 to 14:30). Stop at the 15-minute low (variant: its midpoint). Target 2R (variants 1.5R, 3R).

## Caveats

- Sample: 365 sessions (2025-05-12 to 2026-10-29), 365 stock-days. A few dozen sessions can flatter or bury any rule; the out-of-sample part is only 122 sessions.
- Survivorship: the universe is today's list (TATAPOWER) applied to the past; stocks that dropped out of it are missing, and stocks that joined after a big run are in.
- Data: Angel One SmartAPI 5-minute bars. 134 bars were missing inside the sessions used and 1 short session(s) were left out; F&O stocks have no 15:20 bar since NSE's closing auction. Bars are the feed's, not NSE's tape.
- Fills: entry at the trigger bar's close + 1 tick(s), stops and flat-by exits 1 tick(s) worse, gaps through the stop at the open; targets fill exactly. Real fills in a fast market can be worse.
- Charges: nse.charges: Dhan's intraday equity schedule (brokerage Rs 20 or 0.03 % an order, STT 0.025 % on the sell, NSE txn 0.0030699 %, SEBI, stamp 0.003 % on the buy, GST 18 %), on the shares india-budget would size (max loss Rs 500 a trade incl. charges, capital Rs 1,00,000).
- Long only, one trade per stock per strategy per day; no news, circuit limits, or liquidity checks.
- A backtest is not a forecast and never places an order. Not financial advice.
