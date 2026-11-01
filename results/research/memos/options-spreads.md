# Research memo, 2026-10-29: defined-risk NIFTY weekly option spreads

Research only. F&O stays blocked in the live pack; no orders, no config changes. Not financial advice; a backtest is not a forecast.

**Data**: NSE F&O bhavcopy (slim, NIFTY nearest 2 expiries, ±15 strikes) 2025-04-01..2026-10-28 (389 sessions, 84 weekly expiries; 129 days back-filled this session by plain GETs), Angel NIFTY + India VIX 5-minute bars. Split by expiry, locked in `splits.json` as `bhav:nifty-weekly`: explore 2025-04-01..2026-03-10 (233 sessions, 50 expiries), validate 2026-03-11..2026-07-07 (78, 17), holdout 2026-07-08..2026-10-28 (78, 17, **never opened**: nothing became a candidate).
**Engine**: `trader/research/lab/_spreads.py` imports `options.py` (Black-Scholes per bar, IV from the contract's real bhavcopy open, reality clamp) and `nse.option_charges` (Rs 20/order, STT 0.1 % -> 0.15 % on sold premium from 2026-04-01, exchange, SEBI, stamp, GST; STT 0.125 % on a long leg exercised ITM). Slippage 0.5 % a leg a side (min 1 tick). **Real, not modelled**: hold-to-expiry settlement (bhavcopy intrinsic), daily marks and pre-expiry exits (bhavcopy close/settle). **Modelled**: every entry and intraday exit. A leg whose own IV anchor was unusable or inverted the vertical was repriced at the ATM IV (20 of 67 vertical trades, 5 of 51 condors).
**Margin (conservative, stated, no broker calculator)**: 2 % exposure of notional per short leg (hedged or not) + SEBI's extra 2 % ELM per short leg open on expiry day + max loss. One Rs 1L account: one vertical through expiry (max Rs 85k), one condor if closed the session before expiry (Rs 85k), **no condor into expiry day (Rs 1.2-1.4L)**. Nothing naked.

Tried (hypothesis -> result; W = win %, Rs = net per trade, DD = max drawdown % of Rs 1L, ruin = 20-session windows losing >30 %):

| id (best explore variant) | split | trades | W | Rs/trade | total Rs | DD % | worst day | ruin % | model -10 % / +10 % |
|---|---|---|---|---|---|---|---|---|---|
| nifty_vert_0dte: expiry-day 09:30 put vertical 0.5σ, wing 100, to settlement (buyers' lottery premium + put skew) | explore | 50 | 88 | +701 | +35,054 | 5.6 | -5,625 | 0 | - |
| | validate | 17 | 76 | +26 | +436 | 6.8 | -5,856 | 0 | -64 / +126 |
| nifty_ic_pre_expiry: iron condor dte=2, 10:15, 1σ, closed at the real close before expiry (VRP, steepest theta) | explore | 45 | 76 | +366 | +16,459 | 5.6 | -3,090 | 0 | - |
| | validate | 6 | 100 | +319 | +1,912 | 0.8 | -518 | 0 | +225 / +407 |
| nifty_ic_vix_filter: same, only when VIX > its 20-session median (fear premium) | explore | 21 | 76 | +115 | +2,410 | 3.0 | -1,884 | 0 | - |
| | validate | 1 | - | +221 | +221 | 0.3 | -276 | 0 | +205 / +340 |
| nifty_debit_orb: the losing ATM ORB buy with a 200-pt short leg, stop -25 % / target +50 % (cost capped) | explore | 224 | 38 | -464 | -1,04,036 | 150 | -11,036 | 37 | - |
| | validate | 77 | 33 | -1,521 | -1,17,130 | 152 | -8,246 | 75 | -1,717 / -1,326 |

Other variants: 0DTE ORB-directional vertical +543 (0.5σ) / +260 (1σ) explore; 1σ 09:30 +306. Condor dte=1 -40 to -50 a trade (charges Rs 196 a condor = 15 % of a Rs 1,324 credit). Debit wing 100 and flat-exit variants -1,036 to -1,231. Expiry-day debit trades -1,697 vs -459 on other days.

**Gates** (validate): all four REJECTED. nifty_vert_0dte 2/8 (the explore edge did not hold: CI [-0.21, +0.19] R, cost stress -Rs 302, below Nifty). nifty_ic_pre_expiry 5/8 (fails min_trades 6 < 30, t 1.9, DSR 0.83); the 11 missing validate expiries are the slim cache lacking a wing strike, not the rule. nifty_ic_vix_filter 5/8 (1 trade). nifty_debit_orb 1/8 (CI [-0.15, -0.03] R).

**Trials**: 14 this session, 29 in the registry (chain verified OK).
**Survived**: none. No holdout run.
**Tail risk**: credit spreads over explore+validate: worst day -Rs 5,856 (a full-loss settlement), worst week -Rs 5,856, max DD 6.8 %, 0 % ruin windows, worst 20-session loss 6.5 % (vertical) / 5.6 % (condor). Debit spreads: worst week -Rs 28,305, 42 % of windows lost >30 %, worst 20-session loss 89 %: the account dies like the plain buys.
**Confidence**: low. One trade per expiry gives 17 validate trades at most (the 30/100-trade gates are unreachable with 84 expiries); the 0DTE edge (+701) collapsed to +26 out of sample and is inside a 10 % model error; the condor's +319 rests on 6 trades. Costs matter: four legs cost ~15 % of a condor's credit. Entry fills are modelled (median error 6 %, expiry day unvalidated). Period: Nifty +8 % with no crash; a gap-through-the-wing day costs the full Rs 6-7k.

**Answers**: (1) Selling defined-risk premium was positive on explore and not proven on validate; a condor does not fit Rs 1L through expiry. (2) dte=2 > dte=1 (negative) ; 10:15 > 09:30 on the condor; 09:30 put vertical > ORB-timed on expiry day in explore only. VIX filter cut trades and Rs/trade. (3) Capping the buy's cost does not flip it: -464 to -1,521 a trade. (4) Above.

**Next ideas**: (a) re-cache ±25 strikes and rerun the dte=2 condor as a v2 so every expiry trades, then wait for ~150 expiries before the gates can speak; (b) gap-day vertical: sell the side away from a >0.7 % overnight gap at dte<=2 (overnight hedging demand overprices that side's IV); (c) Friday-close condor bought back Monday 09:30 (weekend calendar theta vs trading-time repricing); (d) record real 5-minute bars of live expiry-day contracts weekly so expiry afternoons stop being unvalidated.
