# Options backtest: SBIN

stock monthly options, 2025-10-08 to 2026-10-28 (259 sessions, 14 expiries). Made 2026-10-29T17:58:51+05:30. **Modelled option prices, anchored to real bhavcopy prices; see the validation file. Never places an order. Not financial advice.**

| strategy | trades | win % | Rs/trade net | ret % on premium | total Rs | PF | max DD % of Rs 1L | ruin windows % | expiry-day Rs/trade | other-day Rs/trade | OOS Rs/trade | clamp changed % | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| opt_pdh_pdl | 128 | 40.6 | -176 | -0.4 | -22516 | 0.89 | 39.3 | 2.1 | -1772 | -55 | -41 | 9.4 | no edge after costs: +3.5 % on premium at model mid prices, but the spread and charges make it -Rs 176 a trade; the Rs 1,00,000 account fell over 30 % in 2 % of 20-session windows |
| opt_no_expiry | 188 | 30.9 | -883 | -5.5 | -166085 | 0.55 | 177.3 | 10.0 | - | -883 | -552 | 12.2 | no edge, even before costs (-1.9 % on premium at model mid prices; -Rs 883 a trade net); the Rs 1,00,000 account fell over 30 % in 10 % of 20-session windows; a Rs 1,00,000 account would have been wiped out by 2026-03-23 |
| opt_orb | 201 | 29.9 | -1003 | -6.2 | -201572 | 0.52 | 207.9 | 14.2 | -2730 | -883 | -1089 | 13.4 | no edge, even before costs (-2.5 % on premium at model mid prices; -Rs 1,003 a trade net); the Rs 1,00,000 account fell over 30 % in 14 % of 20-session windows; a Rs 1,00,000 account would have been wiped out by 2026-05-22 |
| opt_orb30 | 188 | 32.4 | -716 | -4.5 | -134587 | 0.63 | 139.9 | 4.2 | -371 | -739 | -1118 | 12.2 | no edge, even before costs (-0.8 % on premium at model mid prices; -Rs 716 a trade net); the Rs 1,00,000 account fell over 30 % in 4 % of 20-session windows; a Rs 1,00,000 account would have been wiped out by 2026-08-17 |

## Rules

- **opt_pdh_pdl**: The first bar (closing 09:30 to 14:30) whose close crosses above the previous session's high (the bar before closed at or under it) buys the ATM call; crossing below the previous low buys the ATM put. Premium stop -25 %, target +50 % (premium variants as opt_orb, no midpoint stop).
- **opt_no_expiry**: opt_orb's rules, skipping every expiry day of the contract it would trade (for contrast).
- **opt_orb**: Opening range = the underlying's 09:15-09:30 high and low. The first 5-minute bar (closing 09:35 to 14:30) that closes above the high buys the ATM call; the first that closes below the low buys the ATM put. One trade a day. ATM = nearest strike to the underlying at the signal, nearest expiry (expiry day included). Premium stop -25 %, target +50 % (variants: stop -25/-35/-50 % x target +30/+50/+100 %; underlying back to the range midpoint as the stop; trailing stops; flat by 14:45 on expiry day). Flat by 15:05 (15:00 on expiry day).
- **opt_orb30**: Same as opt_orb with the 09:15-09:45 range (signals close 09:50 to 14:30).

## Model vs the bhavcopy close (traded contracts)

```
{
 "expiry_day": {
  "contracts": 11,
  "median_abs_err_pct_vs_close": 52.8,
  "median_abs_err_pct_vs_last": 40.0,
  "median_signed_err_pct_vs_last": 27.5
 },
 "other_day": {
  "contracts": 354,
  "median_abs_err_pct_vs_close": 6.7,
  "median_abs_err_pct_vs_last": 7.2,
  "median_signed_err_pct_vs_last": 1.4
 }
}
```

## Caveats

- Option prices are MODELLED (Black-Scholes, r=0.055, q=0.012, trading-time years), anchored each day to the contract's real bhavcopy open (implied vol) and clipped into its real day range. Intraday smile and skew moves, vol crush and expiry-day gamma are only approximated; see `options validate`.
- The reality clamp uses the day's real high and low, which are not time-stamped: a target below the real day high may have traded before the entry. The clamp can only make a modelled result less optimistic about prices that never traded; it cannot make the path right.
- The volatility anchor is the contract's first trade (bhavcopy open) against the underlying's 09:15 open; for thinly traded stock options the first trade can come much later.
- Fills: entry at the modelled premium + 1.5 % half-spread (at least a tick), stop and flat exits the same worse, targets as limits. Real spreads widen in fast markets and on expiry afternoons.
- Sizing: 20 % of a Rs 1,00,000 account in premium, whole lots (bhavcopy lot size that day), at least one lot or skipped; the account does not compound.
- Sample: 259 sessions, 14 expiries (2025-10-08 to 2026-10-28); the out-of-sample part is the last third of the expiries.
- Charges: nse.option_charges: brokerage Rs 20 an executed order; STT 0.15 % of the sell premium (0.1 % before 2026-04-01); NSE txn 0.03553 % of premium; SEBI Rs 10/crore; stamp 0.003 % of the buy premium; GST 18 % on brokerage + txn + SEBI (Angel One / Zerodha / Dhan pricing pages, checked 2026-10-29). Not modelled: income tax, DP charges, margin rules, freeze limits, circuit filters.
- Model vs the real last traded price at the close, on the contracts traded: other days median abs 7.2 % (signed 1.4 %: negative = the model is cheaper), expiry days median abs 40.0 % (signed 27.5 %). Expiry-day afternoons are where the model is least reliable.
- A backtest is not a forecast and never places an order. F&O stays blocked in india-risk by default (allow_fno false). Not financial advice.
