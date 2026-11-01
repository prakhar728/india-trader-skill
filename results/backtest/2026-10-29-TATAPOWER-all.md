# Backtest: TATAPOWER, orb15, orb30, pdh, gap_go, vwap_pullback, orb_pullback, oversold_bounce

1 stocks, 2026-08-04 to 2026-10-29 (60 sessions), source Yahoo (.NS), slippage 1 tick(s). Made 2026-10-29T17:09:15+05:30.

| strategy | trades | win % | avg R gross | avg R net | Rs/trade | PF | OOS variant | OOS trades | OOS net R | verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| orb30 | 32 | 43.8 | +0.12 | -0.10 | -14 | 0.84 | r=1 stop=or_low | 11 | -0.16 | no edge after charges: +0.12R gross becomes -0.10R net |
| orb15 | 35 | 42.9 | +0.10 | -0.14 | -10 | 0.87 | r=1 stop=or_low | 12 | -0.20 | no edge after charges: +0.10R gross becomes -0.14R net |
| vwap_pullback | 47 | 38.3 | +0.07 | -0.62 | -31 | 0.43 | r=1.5 | 15 | -0.27 | no edge after charges: +0.07R gross becomes -0.62R net |
| oversold_bounce | 4 | 50.0 | +0.66 | +0.53 | +201 | 8.41 | r=2 rsi=30 stop=or_low (default) | 1 | +0.78 | too few trades to judge (4) |
| pdh | 13 | 53.8 | +0.23 | +0.01 | +32 | 1.47 | r=2 stop=swing (default) | 6 | +0.05 | too few trades to judge (13) |
| orb_pullback | 25 | 40.0 | -0.01 | -0.25 | -24 | 0.69 | r=2 stop=or_mid (default) | 9 | -0.03 | too few trades to judge (25) |
| gap_go | 0 | - | - | - | - | - | gap=1 r=2 stop=or_low (default) | 0 | - | never fired: nothing to judge |

## Rules

- **orb30**: Opening range = the first six 5-minute bars (09:15 to 09:45). Buy the close of the first bar that closes above the range high (closing 09:50 to 14:30). Stop at the range midpoint (variant: range low). Target 2R (variants 1R, 1.5R, 3R).
- **orb15**: Opening range = the high and low of the 09:15, 09:20 and 09:25 bars. Buy the close of the first bar that closes above the range high (bar closing 09:35 to 14:30). Stop at the range midpoint (variant: range low). Target 2R (variants 1R, 1.5R, 3R).
- **vwap_pullback**: Session VWAP from 09:15 (typical price x volume). From 10:00: once a bar has closed above VWAP, wait for a bar whose low touches VWAP; buy the close of the next bar that closes back above VWAP (within 3 bars of the touch, else start over), closing 10:00 to 14:30. Stop one tick under the pullback's low. Target 2R (variants 1R, 1.5R, 3R).
- **oversold_bounce**: Daily RSI(14, Wilder) under 30 at yesterday's close. Today buy the close of the first bar that closes above the first 15 minutes' high (closing 09:35 to 14:30). Stop at the 15-minute low (variant: its midpoint). Target 2R (variants 1.5R, 3R).
- **pdh**: Buy the close of the first bar (closing 09:30 to 14:30) that crosses above the previous session's high: it closes above it and the bar before closed at or below it (a stock that opens above and stays above never crosses). Stop at the lowest low of the trigger bar and the two bars before it (variant: the day's low so far). Target 2R (variants 1R, 1.5R, 3R).
- **orb_pullback**: After a bar closes above the 15-minute range high (the ORB trigger), wait for a bar whose low comes back to the range high; buy the close of the first bar from then on that closes above the range high (closing 09:30 to 14:30). A close under the range midpoint first cancels the day. Stop at the range midpoint (variant: the pullback's low). Target 2R (variants 1.5R, 3R).
- **gap_go**: The day opens at least 1 % above the previous close (variant 2 %) and the first 15 minutes' low stays above the previous close. Buy the close of the first bar that closes above the first 15 minutes' high (closing 09:35 to 14:30). Stop at the 15-minute low (variant: its midpoint). Target 2R (variants 1.5R, 3R).

## Caveats

- Sample: 60 sessions (2026-08-04 to 2026-10-29), 60 stock-days. A few dozen sessions can flatter or bury any rule; the out-of-sample part is only 20 sessions.
- Survivorship: the universe is today's list (TATAPOWER) applied to the past; stocks that dropped out of it are missing, and stocks that joined after a big run are in.
- Data: Yahoo (.NS) 5-minute bars. 123 bars were missing inside the sessions used and 0 short session(s) were left out; F&O stocks have no 15:20 bar since NSE's closing auction. Bars are the feed's, not NSE's tape.
- Fills: entry at the trigger bar's close + 1 tick(s), stops and flat-by exits 1 tick(s) worse, gaps through the stop at the open; targets fill exactly. Real fills in a fast market can be worse.
- Charges: nse.charges: Dhan's intraday equity schedule (brokerage Rs 20 or 0.03 % an order, STT 0.025 % on the sell, NSE txn 0.0030699 %, SEBI, stamp 0.003 % on the buy, GST 18 %), on the shares india-budget would size (max loss Rs 500 a trade incl. charges, capital Rs 1,00,000).
- Long only, one trade per stock per strategy per day; no news, circuit limits, or liquidity checks.
- A backtest is not a forecast and never places an order. Not financial advice.
