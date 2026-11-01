# Independent audit of the strategy-research program, 2026-10-29

Auditor: not a member of the research team. Read-only on every research artefact and on the data; no broker, Angel or Yahoo call; no holdout bar read (every raw file was cut at the dataset's holdout start before sessions were built). Scratch scripts: `scratchpad/audit/{repro.py, repro_spreads.py, holdout_probe.py, engine_check.py, drift.py, costs.py, trials.py}`. Not financial advice.

## Verdict per checklist item

| # | item | verdict | changes a conclusion? |
|---|---|---|---|
| 1 | Integrity | **OK** with one process **ISSUE (medium, forward-looking)** | no |
| 2 | Trial accounting | **ISSUE (low)**: ~300 unlogged looks; true N ≈ 400 vs 97 | no |
| 3 | Reproduce | **OK**: 6 ids reproduced exactly, trade for trade | no |
| 4 | Engine correctness | **OK** on 19,885 trades; one modelling gap (**ISSUE, medium**: no portfolio capital cap) | no |
| 5 | Costs | **OK**; at Rs 0 brokerage nothing becomes statistically positive | no |
| 6 | Overnight vs intraday | **OK**: verified; robust 2015-2026; weak inside the research window | no, but the memo overstates its relevance |
| 7 | Options research | **OK**; margin estimate conservative; two charge constants unverifiable offline | no |
| 8 | Other | pre-research backtests already covered the holdout windows (**ISSUE, medium**) | no (nothing reached the holdout) |

## 1. Integrity

- `research.py verify`: OK, 133 records, hash chain intact, 97 trials. Every id: log -> explore (-> status tested) -> validate -> gate -> status rejected; `tod_intraday_momentum` has two explore records (a bug fix, both counted, 8 trials). No `holdout_claim`/`holdout` op, no `runs/*-holdout.json`, no candidate.
- `splits.json`: three keys; windows as reported (angel 234/78/78, bhav 233/78/78 by expiry, yahoo 36/12/12). Muhurat 2025-10-21 (12-17 bars on the grid) is dropped by the 60-bar rule; the Sunday budget session 2026-02-01 (75 bars) is included as a normal session, which is right.
- Loader probe (`holdout_probe.py`): explore holds bars to 2026-03-11 (cutoff 03-12), validate to 2026-07-08 (cutoff 07-09); 0 sessions, 0 index days, 0 daily rows inside the holdout in either. `load_dataset(..., "holdout")` without a ticket, with a forged `HoldoutTicket` (no claim in the registry) and with a ticket-shaped fake object: all refused. The spreads loader (`_spreads.Data`) refuses likewise and only loads chains for already-cut dates. `peers.holdout_ranges()` excludes all three holdouts.
- **ISSUE (medium, process)**: the holdout windows are "never opened" by the research loaders, but not unseen. (a) The `yahoo:nifty50:5m` split (made 18:40) put its explore+validate (2026-08-04..2026-10-12, 48 sessions, 50 symbols) inside the later `angel:nifty100:5m` holdout (2026-07-09..10-29, made 19:27); `locked_ranges()` only protects after a split exists. Three ids (`short_orb_breakdown`, `vwap_reversion`, `sector_lead_lag`) were explored and validated on 62 % of the Angel holdout sessions for half its symbols. (b) The pre-research backtests in `trader/backtest/` (made 17:08-17:58) ran the seven long rules on Nifty 50 and Tata over 2026-08-04..10-29, on RELIANCE/TATAPOWER (Angel) over 2025-05-12..2026-10-29, the ATM option buys on NIFTY over 2025-10-08..2026-10-28 and the swing rules to 2026-10-29. So the behaviour of ORB/PDH/gap-go/VWAP/oversold and of ATM NIFTY buys over both holdouts is already known to the team. No current conclusion depends on a holdout, so nothing changes now; the evidential value of a future holdout run for those families is reduced and should be stated.

## 2. Trial accounting

Logged: 97 (sums per memo check: yahoo 15 + options 14 + time-of-day 8 + short 20 + mean-reversion 4 + lead-lag 6 + filters 30 = 97). Unlogged looks counted from the memos (`trials.py`): time-of-day 24, lead-lag 127 (pairwise lag correlations 60, sector->laggard 27, movers/open/RS 17, Tata Power 21, re-reads 2), mean-reversion prototypes 12, filters' daily study 10, options "other variants" 6, the pre-research backtests 104 + 16. Lower bound ~300, so the true number of looks is ~400.

Deflated Sharpe bar (E[max SR] of N unskilled trials, registry trial-SR variance 0.0454): 0.537 at N=97, 0.636 at N≈400, 0.694 at N=1,000. Re-gating every id at N=400 (same T, skew, kurtosis): every DSR is 0.000 except `nifty_ic_pre_expiry` (1.12 on T=6: 0.83 as gated at N=29 -> 0.37). Note `short_orb_breakdown` was gated at N=8 (DSR 0.92, the closest any id came); at N>=97 it is 0.000. The per-trade SR a strategy would need for DSR>=0.95 at N≈400 is 0.66-0.86 (a t of 10-50 on these T). Conclusion unchanged: the true count makes the bar higher, and nothing was near it.

## 3. Reproduction (no registry write; writers monkey-patched to raise)

| id | split | registry | reproduced |
|---|---|---|---|
| short_late_low_break | explore best | 2,520 tr, -0.157R, -Rs 25.96 | identical (all 4 variants) |
| | validate | 722 tr, -0.0029R, +Rs 4.34, +Rs 3,134; stress -0.090R | identical; 0 trade mismatches |
| sector_basket_lag | validate | 373 tr, -0.0986R, -Rs 0.69, -Rs 256; stress -0.183R | identical |
| flt_gapgo | validate | 210 tr, +0.0064R, +Rs 6.38, +Rs 1,341; stress -0.048R | identical |
| eod_sector_laggard | validate | 269 tr, -0.1337R, -Rs 32.70, -Rs 8,795; stress -0.200R | identical |
| nifty_vert_0dte | explore / validate | 50 tr +Rs 701 / 17 tr +0.0118R +Rs 25.63 | identical, 17/17 trades |
| nifty_ic_pre_expiry | explore / validate | 45 tr +Rs 366 / 6 tr +Rs 318.6 | identical, 6/6 trades |

Folds, chosen variants, bootstrap CIs and every gate's pass/fail match; DSR values differ only through N (97 now vs N at gating).

## 4. Engine correctness

`engine_check.py` re-derived all 19,885 validate trades (18 equity ids) from the raw 5-minute bars with an independent implementation: signal = close of the bar ending at `time` (0 mismatches), tick from `angel-nse-equity.json` (0), entry = signal ± 1 tick (0), stop/target on the tick grid and on the correct side (0), independent exit walk incl. gap-through-stop at the open and both-touch = stop (0 outcome/price/time mismatches), flat exits on the 15:00 bar (slot 69; slots 70-74 are never used, so the CAS-era 73-bar sessions change nothing), charges = `nse.charges` with the right legs (0), shorts' STT = round(0.025 % x entry value) and stamp = round(0.003 % x cover value) (0), caps (position <= Rs 25,000, loss at stop incl. charges <= Rs 500, and qty+1 would breach one of them: 0 violations), P&L arithmetic (0).
- Both-touch rule: 4 of 19,885 trades had stop and target in the same bar; coin-flipping them adds Rs 40-368 per id. Immaterial.
- Entry at the signal close + tick vs the next bar's open + tick: -Rs 0.57 to +Rs 0.87 a trade. Immaterial.
- Timezone: every session's first bar is 09:15 IST; Muhurat 2025-10-21 bars sit at 13:45-14:45 on the grid and are dropped; its daily bar still supplies the official prev close for 10-23.
- Corporate actions: Angel's 5m and daily series are both split/bonus-adjusted and consistent (HDFCBANK 2025-08-25/26, BAJFINANCE 2025-06-13/16: no fake gap; no |close/prev-1| > 20 % artefact in 2025-26 beyond real events). 99 stock-days (0.3 %) differ > 1 % between the daily close and the 5m last close: official closing price vs last trade; `prev.close` uses the official one. Side effect: adjusted prices put pre-split days in a lower tick band and understate traded value (volume is unadjusted); not material.
- Stocks above the Rs 25k cap (BOSCHLTD) are silently never traded ("zero shares fit the budget").
- **ISSUE (medium, both directions)**: no portfolio-level capital constraint. Peak simultaneous positions / peak deployed capital on Rs 1,00,000: `tod_intraday_momentum` 98 / Rs 21.8 L, `flt_orb15` 62 / Rs 14.6 L, `short_orb_index_cond` 66 / Rs 14.4 L, `open_index_laggard` 57 / Rs 13.5 L, `short_late_low_break` 45 / Rs 10.5 L. Per-trade R and Rs are unaffected and all signs stand, but "total Rs", the daily-sum t-stat and the benchmark gate's return-on-capital are computed as if capital were unlimited; a future positive result would need a concurrency rule before any account-level number is believed.

## 5. Costs

Confirmed per trade: Rs 19.9-26.0 on Rs 18.7k-24.3k positions (brokerage min(Rs 20, 0.03 %) per order, STT 0.025 % on the sell, NSE 0.0030699 %, SEBI 0.0001 %, stamp 0.003 % on the buy, GST 18 %): Dhan's intraday equity schedule. Lower-cost scenarios on the validate trades (`costs.py`):
- Rs 0 brokerage: mean Rs/trade turns positive for 6 of 18 ids (`short_orb_breakdown` +45, `flt_gapgo` +23, `short_late_low_break` +21, `sector_basket_lag` +16, `sector_lead_lag` +8, `tod` +1) but **no** 95 % day-clustered CI lower bound is above zero (best -8.1).
- Zero charges (gross after 1-tick slippage): 8 of 18 positive in mean; the largest gross edges are +Rs 54 (`short_orb_breakdown`, 12 Yahoo sessions) and +Rs 30 (`short_late_low_break`, +0.14R, t ≈ 1).
- Binding constraint: absence of edge for 10 of 18 (gross <= 0); for the rest a gross edge of 0.03-0.2R that is not distinguishable from zero and smaller than Rs 25. A cheaper broker does not rescue any family.

## 6. Overnight vs intraday (daily bars, 2015-01-02..2026-03-11, cross-sectional mean per day, day-clustered t)

Nifty 100: open->close **-9.3 bp (t -5.2)**, close->close **+8.0 bp (t +3.8)**, close->open +17.5 bp (t +14.7). Matches the memo (-9.2 / +8.1). Robust: excluding |cc| > 20 % or > 10 % gives -9.4 / -10.2 bp; open->close is negative in every calendar year 2015-2026 (-4.0 to -18.2 bp); NIFTY 50 index -7.4 bp (t -4.6) / +4.3 / +11.7. Open source: Angel's daily open equals the 5m 09:15 bar's open (mean -0.17 bp, 7 of 22,736 days > 1 %); daily close vs 5m last close +0.96 bp (official close vs last trade); open->close from daily and from 5m bars agree (-2.6 vs -3.7 bp in 2025-26); both series are adjusted the same way. Nothing manufactures the effect. Caveats the memo should carry: inside the research window (2025-04-01..2026-03-11) it is only -3.0 bp (t -0.6; NIFTY -1.5 bp), so it does not explain the intraday losses in this sample, and 1.4 of those 3.7 bp sit in the first 5-minute bar. A naive buy-close/sell-open earns +17.5 bp/day gross (2015-26; +8.2 bp in 2025-26) against a Dhan delivery round trip of 28.2 bp on Rs 25k (23.7 bp on Rs 1 L; Angel 50.7 / 29.3 bp): -6 to -20 bp/day net, untradable. Implication: long intraday rules face a structural headwind on average and short rules a tailwind (both still lost); nothing here is a capturable return.

## 7. Options

Real: settlement (bhavcopy intrinsic), prev-close exits and daily marks; modelled: entries (Black-Scholes anchored on the contract's bhavcopy open, clamped to the day's real range); 10 of 17 validate verticals had a leg repriced at the ATM vol; ±10 % model shift moves the 0DTE validate from -Rs 64 to +Rs 126 a trade, i.e. inside model error. Margin: 2 % exposure per short leg (+2 % expiry-day ELM) + max loss gives Rs 62-70k for one 100-point NIFTY vertical (lot 65); real SPAN+exposure for a hedged vertical is lower, so the sizing is conservative (fewer lots), not flattering. `bhav:nifty-weekly` holdout never read (validate data ends 2026-07-07, cutoff 07-08). Unverifiable offline: STT 0.15 % on sold premium from 2026-04-01 and NSE options txn 0.03553 % (NSE's published rate since 2024-10-01 is 0.03503 %); both differences are a rupee or less a trade and err on the conservative side.

## Prioritised fixes (describe only; nothing applied)

1. **Holdout provenance** (`india-backtest/backtest.py` `compute()` after `data = restrict(...)`, `options.py` `run_underlying()` after `build_days`, `swing.py` equivalent): read `lab_engine.read_splits()` and drop sessions inside any dataset's holdout for the symbols requested unless `--allow-holdout` is passed, printing what was dropped. And in `lab_engine.ensure_split()`: when a new split is made, scan `trader/backtest/*.json` metas and the other splits' explore/validate windows for overlap with the new holdout and write the overlap into the split record's `notes` so the report shows it.
2. **Portfolio capital cap** (`lab_engine.run_variants`/`run_strategy`): after streaming, re-walk each day's trades across symbols in entry-time order and drop those that would push deployed capital above `capital x (1 - cash_buffer_pct/100)`; record `skips["capital"]`. Report total Rs only from the capped set.
3. **Re-gate at the final N** (`research.py cmd_report`): recompute every id's DSR at `total_trials(recs)` and show both the gated and the current value; and add a `research.py note --looks N --why ...` op whose count feeds `total_trials` so side measurements raise the bar.
4. **Reproducibility** (`research.py cmd_log`, line 367): store `list_file` as `os.path.abspath(a.list_file)`; today five ids carry a cwd-relative path and cannot be re-run from elsewhere. Also log the options ids with the dataset the engine actually uses (`bhav:nifty-weekly`, not `angel:NIFTY:5m`).
5. **Transparency**: list symbols never sized (price above the position cap) in the run file; add 2026-02-01 to a special-sessions table in `nse.py` so the live calendar agrees with the data.
6. **Charges constants** (`nse.OPTION_CHARGES`): cite the source for the 0.15 % STT change and use NSE's 0.03503 %; immaterial to results.

## Can the program's conclusions be trusted?

Yes, for what they claim. Every number I re-derived matched: the hash chain is intact, six ids reproduce to the rupee across explore, walk-forward validate, stress and gates; 19,885 trades agree with an independent simulator on entry, exit, tick, sizing and Dhan's charges including the short side's STT/stamp legs; the holdout loaders cannot be opened, and the overnight-drift finding is real in the daily data (though weak in the tested window). The two conservative rules (both-touch = stop, close + tick entries) cost under Rs 1 a trade, so no edge was hidden by the engine, and removing brokerage, or all charges, turns no family into a statistically positive one: the binding constraint is the absence of a gross edge, with Dhan's Rs 25 as the second blow. What the memos understate is the search: about 400 looks, not 97, which only makes the rejections firmer; and the holdout windows, while never loaded by the research code, were already covered by the team's earlier backtests and by the Yahoo split, so a future holdout run on the breakout/ATM-buy families would be confirmation, not discovery. The per-trade verdicts stand; account-level totals should be ignored until a capital cap exists.
