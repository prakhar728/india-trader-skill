# Options backtest: RELIANCE

stock monthly options, 2025-10-08 to 2026-10-28 (259 sessions, 14 expiries). Made 2026-10-29T17:58:51+05:30. **Modelled option prices, anchored to real bhavcopy prices; see the validation file. Never places an order. Not financial advice.**

| strategy | trades | win % | Rs/trade net | ret % on premium | total Rs | PF | max DD % of Rs 1L | ruin windows % | expiry-day Rs/trade | other-day Rs/trade | OOS Rs/trade | clamp changed % | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| opt_no_expiry | 220 | 32.7 | -450 | -3.1 | -98980 | 0.73 | 112.0 | 10.0 | - | -450 | +373 | 10.9 | no edge after costs: +0.5 % on premium at model mid prices, but the spread and charges make it -Rs 450 a trade; the Rs 1,00,000 account fell over 30 % in 10 % of 20-session windows; a Rs 1,00,000 account would have been wiped out by 2026-09-22 |
| opt_orb30 | 217 | 35.9 | -383 | -2.4 | -83023 | 0.76 | 117.8 | 7.9 | -5143 | -177 | +122 | 8.8 | no edge after costs: +1.2 % on premium at model mid prices, but the spread and charges make it -Rs 383 a trade; the Rs 1,00,000 account fell over 30 % in 8 % of 20-session windows; a Rs 1,00,000 account would have been wiped out by 2026-08-26 |
| opt_pdh_pdl | 151 | 36.4 | -823 | -4.9 | -124315 | 0.59 | 137.9 | 7.5 | -2643 | -680 | -276 | 13.9 | no edge, even before costs (-1.3 % on premium at model mid prices; -Rs 823 a trade net); the Rs 1,00,000 account fell over 30 % in 8 % of 20-session windows; a Rs 1,00,000 account would have been wiped out by 2026-03-30 |
| opt_orb | 233 | 31.3 | -650 | -4.1 | -151406 | 0.64 | 163.3 | 15.0 | -4033 | -450 | -444 | 12.0 | no edge, even before costs (-0.6 % on premium at model mid prices; -Rs 650 a trade net); the Rs 1,00,000 account fell over 30 % in 15 % of 20-session windows; a Rs 1,00,000 account would have been wiped out by 2026-05-05 |

## Rules

- **opt_no_expiry**: opt_orb's rules, skipping every expiry day of the contract it would trade (for contrast).
- **opt_orb30**: Same as opt_orb with the 09:15-09:45 range (signals close 09:50 to 14:30).
- **opt_pdh_pdl**: The first bar (closing 09:30 to 14:30) whose close crosses above the previous session's high (the bar before closed at or under it) buys the ATM call; crossing below the previous low buys the ATM put. Premium stop -25 %, target +50 % (premium variants as opt_orb, no midpoint stop).
- **opt_orb**: Opening range = the underlying's 09:15-09:30 high and low. The first 5-minute bar (closing 09:35 to 14:30) that closes above the high buys the ATM call; the first that closes below the low buys the ATM put. One trade a day. ATM = nearest strike to the underlying at the signal, nearest expiry (expiry day included). Premium stop -25 %, target +50 % (variants: stop -25/-35/-50 % x target +30/+50/+100 %; underlying back to the range midpoint as the stop; trailing stops; flat by 14:45 on expiry day). Flat by 15:05 (15:00 on expiry day).

## Model vs the bhavcopy close (traded contracts)

```
{
 "expiry_day": {
  "contracts": 6,
  "median_abs_err_pct_vs_close": 62.0,
  "median_abs_err_pct_vs_last": 16.7,
  "median_signed_err_pct_vs_last": -2.8
 },
 "other_day": {
  "contracts": 350,
  "median_abs_err_pct_vs_close": 9.3,
  "median_abs_err_pct_vs_last": 8.6,
  "median_signed_err_pct_vs_last": 0.8
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
- Model vs the real last traded price at the close, on the contracts traded: other days median abs 8.6 % (signed 0.8 %: negative = the model is cheaper), expiry days median abs 16.7 % (signed -2.8 %). Expiry-day afternoons are where the model is least reliable.
- A backtest is not a forecast and never places an order. F&O stays blocked in india-risk by default (allow_fno false). Not financial advice.
