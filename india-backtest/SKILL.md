---
name: india-backtest
description: >-
  Backtester for NSE intraday strategies. Answers "which strategies are
  available and how good are they, honestly, after Indian costs?" across
  many NSE stocks and days. Runs seven long-only rules (15- and 30-minute
  opening-range breakout, previous-day-high breakout, gap and go, VWAP
  pullback, ORB pullback, oversold bounce) on 5-minute bars from Yahoo (60
  days) or Angel One SmartAPI (a year or more), with the live pack's rules:
  entries 09:30-14:30 IST, flat by 15:05, stops and targets rounded down to
  the NSE tick, 1 tick of slippage, a gap through the stop filled at the
  open, a bar touching both counted as the stop, every trade sized like
  india-budget and charged Dhan's intraday round-trip costs. Reports win %,
  average R gross and net, rupees per trade, profit factor, losing streaks,
  drawdown, results by entry time and by stock, every parameter variant and a
  walk-forward out-of-sample check, then one honest verdict per strategy.
  Universes: Nifty 50 / 100 / 500, the Tata group, a file or a list of
  symbols. Use when the user says "backtest", "backtest ORB", "which strategy
  works", "test ORB on Nifty 50", "how good is this strategy", "does the
  opening range breakout make money", "compare strategies", "what's the best
  intraday strategy", "is VWAP pullback any good", "backtest Tata Power",
  "is there an edge after charges", "walk-forward", or "out of sample".
  Also backtests BUYING OPTIONS: ATM Nifty weekly and stock monthly (SBIN,
  RELIANCE, HDFCBANK) calls and puts on opening-range or previous-day
  breakouts with a premium stop and target and ~20 % of a Rs 1 lakh account,
  with option prices modelled (Black-Scholes) but anchored every day to NSE's
  real F&O bhavcopy, clipped into each contract's real day range, validated
  against Angel's real 5-minute option bars, and charged option costs. Use for
  "backtest Nifty options", "does buying weekly options work", "options ORB",
  "test this options strategy from Twitter", "expiry day trading", "ATM call
  put breakout", "Claude trades Nifty options". And SWING (multi-day)
  strategies on 10+ years of daily bars with delivery (CNC) costs, a
  Rs 1 lakh compounding portfolio and the Nifty 50 / NIFTYBEES as the
  benchmark: RSI(2) pullback, daily oversold bounce, 20-day breakout,
  52-week high on volume, 50/200 MA cross, monthly momentum rotation,
  gap-up volume drift, daily bull flag. Use for "swing trading backtest",
  "does momentum work in India", "can I beat the Nifty", "should I just buy
  an index fund", "positional strategy", "delivery trading", "RSI 2",
  "golden cross", "52-week high", "monthly rotation", "CAGR and drawdown".
argument-hint: "[list | run --strategy orb15 --universe nifty50 | compare --universe tata | options compare | swing compare]"
---

# india-backtest

The other skills in this pack plan one trade at a time. This one steps back
and asks whether the rules themselves are worth trading: it replays each
strategy on every stock and every session in the data, exactly as the live
pack would have taken it (same entry window, same ticks, same flat-by, same
sizing, same charges), and reports the result in R and in rupees, before and
after costs, with an out-of-sample check so a lucky parameter does not pass
for an edge. Most intraday rules lose money after Indian costs, and this
skill will usually say so. **A backtest never places an order. Not financial
advice.** Nothing here is sent to a broker; the pack only ever places an order
after your typed "yes" in india-broker, and this skill is not on that path.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
Run from the folder that holds `trader/` and your `.env` (Angel keys).

```bash
python3 "${CLAUDE_SKILL_DIR}/backtest.py" list                                   # strategies, rules, variants
python3 "${CLAUDE_SKILL_DIR}/backtest.py" run --strategy orb15 --universe nifty50
python3 "${CLAUDE_SKILL_DIR}/backtest.py" run --strategy orb15,pdh --universe RELIANCE,TCS --variants
python3 "${CLAUDE_SKILL_DIR}/backtest.py" compare --universe nifty50 --write    # all strategies, ranked
python3 "${CLAUDE_SKILL_DIR}/backtest.py" compare --universe tata --write       # the Tata group
python3 "${CLAUDE_SKILL_DIR}/backtest.py" compare --universe TATAPOWER --source angel --days 365 --write
python3 "${CLAUDE_SKILL_DIR}/backtest.py" run --strategy all --universe mylist.txt --json
python3 "${CLAUDE_SKILL_DIR}/backtest.py" run --strategy orb15 --universe RELIANCE --offline DIR --to 2026-10-28
```

Options: `--days N` (the last N sessions, default 60), `--source yahoo|angel`,
`--offline DIR[,DIR]` (saved bars, no network), `--from/--to YYYY-MM-DD`,
`--slippage-ticks N` (default 1), `--capital`, `--max-loss`, `--config`,
`--refresh` (refetch even if the cache is fresh), `--variants`, `--json`,
`--write [NAME]`, `--now "YYYY-MM-DD HH:MM"`, `--allow-holdout`.

Research holdouts: sessions inside a locked india-research holdout
(`trader/research/splits.json`, matched by symbol) are withheld by default, before
`--days/--from/--to` pick the window, with one line on stderr saying how many;
`--allow-holdout` includes them and the output records `holdout_included: true`. A
holdout measured here stops being unseen, so research never passes it. (Swing runs do
not withhold yet.)

## The strategies (long only, 5-minute bars)

| name | rule | stop (variants) | target (variants) |
|---|---|---|---|
| `orb15` | first bar closing above the 09:15-09:30 range high | range midpoint (range low) | 2R (1, 1.5, 3) |
| `orb30` | same with the 09:15-09:45 range | range midpoint (range low) | 2R (1, 1.5, 3) |
| `pdh` | first bar that crosses above the previous day's high (its close above, the bar before at or below), closing 09:30 or later | low of the last 3 bars (day's low so far) | 2R (1, 1.5, 3) |
| `gap_go` | opens >= 1 % (2 %) above the previous close, first 15 minutes' low holds above it, then a close above the first 15 minutes' high | 15-min low (15-min mid) | 2R (1.5, 3) |
| `vwap_pullback` | from 10:00, after a close above session VWAP, a bar's low touches VWAP; the next close back above VWAP within 3 bars | one tick under the pullback low | 2R (1, 1.5, 3) |
| `orb_pullback` | after an `orb15` trigger, a bar's low comes back to the range high; first close back above it (a close under the range midpoint first cancels) | range midpoint (pullback low) | 2R (1.5, 3) |
| `oversold_bounce` | daily RSI(14, Wilder) < 30 at yesterday's close; first close above the first 15 minutes' high | 15-min low (15-min mid) | 2R (1.5, 3) |

Add a strategy: write `fn(session, params, window)` returning
`{"k": bar, "price": close, "stop": price, "r": R, "why": text}` (or None, or a
short reason string) and register it in `STRATEGIES` with its rules, default
and variants; `list`, `run`, `compare` and the JSON pick it up.

## The engine (the live pack's rules)

- Session 09:15 to flat-by (`nse.flat_by(cfg)`, 15:05, never after Dhan's
  15:10). The trigger bar's close must fall inside the entry window
  (`no_entries_before` 09:30 to `no_entries_after` 14:30). One trade per stock
  per strategy per day.
- The stop and target round **down** to the stock's tick (the cached
  instrument file's tick, else NSE's price band). The target is planned from
  the trigger close. A stop closer than 3 ticks or 0.05 % is refused, as in
  india-exit.
- Fill = trigger close + slippage (1 tick). Exits bar by bar after the entry
  bar: opens at or under the stop -> out at that open minus slippage (gap
  through the stop); low touches the stop -> stop minus slippage; high reaches
  the target -> the target exactly. **A bar touching both counts as the
  stop.** Still open at flat-by -> the close of the last bar ending by 15:05
  (the 15:00 bar), minus slippage.
- Missing bars are skipped (Yahoo sends 72-75 a session; F&O stocks have no
  15:20 bar since NSE's closing auction). A session with under 60 bars, and a
  special session such as Muhurat trading, is left out. A missing
  opening-range bar skips that day for the range strategies.
- Size: the shares india-budget would buy: the fewest of `max_loss_per_trade`
  (loss at the stop plus charges), `position_cap_pct` of `capital`,
  `max_order_value` and capital minus the cash buffer (trader/config.json;
  defaults Rs 1,00,000, Rs 500, 25 %, Rs 50,000, 10 %). Charges are
  `nse.charges` on the real buy and sell value.
- R = fill minus stop. Gross R ignores charges; net R = (P&L - charges) /
  (R x shares). Rupee figures are net.

## What it reports

Per strategy: trades, win % (after charges), average R gross and net, the
charges' cost in R, expectancy and total in rupees, profit factor (net and
gross), outcomes (target, stop, gap through the stop, flat-by), the worst
losing streak, max drawdown in R and rupees, % of sessions and stock-days with
a trade, results by entry time (09:30-10:00, 10-11, 11-12, 12-13, 13-14:30), the
best and worst five stocks, every variant (`--variants`), and the
**walk-forward**: the first 2/3 of the sessions choose the variant with the
best average net R (at least 10 trades); only that variant's result on the
last 1/3 is reported as out of sample. `compare` ranks strategies by that
out-of-sample net R (strategies with under 10 out-of-sample trades last).

Verdicts: "never fired", "too few trades to judge" (under 30), "no edge, even
before charges", "no edge after charges" (gross positive, net not), "positive
... but it did not hold out of sample", "small positive edge, thin sample", or
"positive edge after charges" only with 200+ trades, t >= 2 and a positive
out-of-sample result.

`--write` saves `trader/backtest/<date>-<universe>-<strategies>[-angel].json`
and a markdown summary beside it. The JSON has `meta` (universe, symbols, date
range, sessions, split, source, slippage, charges model, sizing, data notes,
caveats), `ranking`, and per strategy `full` (all the numbers above plus
`equity_curve` [{date, cum_R_net, cum_inr, trades}], `histogram_R_net`
buckets, `by_entry_time`, `by_stock` rows), `variants` (full / in-sample /
out-of-sample for each), `walk_forward`, `skipped` reasons, `verdict` and the
default variant's `trades`.

## Data

- **Yahoo** (default): `SYMBOL.NS` 60 days of 5-minute bars plus a year of
  daily bars (prior-day high/low/close and the daily RSI), one symbol at a
  time, about a second apart, backing off on 429.
- **Angel One** (`--source angel`): SmartAPI `getCandleData`, 5-minute bars in
  90-day chunks back to `--days` sessions, at most about one request a second,
  backing off on errors (Angel rate-limits with HTTP 403), saved after every
  chunk so an interrupted run resumes. Daily bars in one call. Logs in with the
  `ANGEL_*` keys in `.env` (never printed); the token comes from Angel's
  instrument file. Read-only: only the candle route is ever called.
- Every fetch is merged into `trader/cache/bars/<source>/<SYMBOL>-5m.json.gz`
  (and `-1d`), so the history grows past Yahoo's 60 days over time and reruns
  the same day need no network. Today's bars are kept only after 15:30.
- Universes: `nifty50`, `nifty100`, `nifty500` (NSE's
  `ind_nifty*list.csv` on nsearchives, series EQ only), `tata` (every Nifty 500
  EQ stock whose company name says Tata, plus TCS, TITAN, TRENT, VOLTAS; Tata
  Motors is TMPV and TMCV since its demerger), a file of symbols, or `SYM,SYM`.

## Options: buying ATM Nifty / stock options (`backtest.py options`)

Tests setups like the viral "Claude trades Nifty weekly options on a Rs 1L
account: range breakouts, ~20 % position size, ATM options, strict stop and
target". **A backtest never places an order. Not financial advice.** F&O stays
blocked in india-risk by default (`allow_fno` false); nothing here changes that.

```bash
python3 "${CLAUDE_SKILL_DIR}/backtest.py" options fetch --underlying NIFTY --days 260   # bhavcopy + Angel 5m + VIX
python3 "${CLAUDE_SKILL_DIR}/backtest.py" options fetch --underlying SBIN,RELIANCE,HDFCBANK --no-bhav
python3 "${CLAUDE_SKILL_DIR}/backtest.py" options run --underlying NIFTY --strategy all --variants
python3 "${CLAUDE_SKILL_DIR}/backtest.py" options validate        # the honesty check (live contracts)
python3 "${CLAUDE_SKILL_DIR}/backtest.py" options compare         # NIFTY + 3 stocks, writes the dash JSON
python3 "${CLAUDE_SKILL_DIR}/backtest.py" options list
```

Options: `--strategy opt_orb[,...]|all`, `--days N`, `--from/--to`,
`--capital 100000`, `--alloc-pct 20`, `--slip-pct` (half-spread a side, % of
premium: 0.5 Nifty, 1.5 stocks; at least one 0.05 tick), `--r 0.055`,
`--q 0.012`, `--vix-drift`, `--roll-on-expiry`, `--expiry-flat 15:00`,
`--json`, `--no-write`, `--now`, `--allow-holdout` (sessions inside a research
holdout for the underlying, e.g. `bhav:nifty-weekly` for NIFTY, are withheld by default).

### Strategies (signals on the underlying's real 5-minute bars, IST)

| name | signal | contract |
|---|---|---|
| `opt_orb` (the thread's setup, default) | first 5-min close above the 09:15-09:30 high buys the ATM call, below the low buys the ATM put; one trade a day, closing 09:35-14:30 | ATM strike at the signal, nearest expiry incl. expiry day |
| `opt_orb30` | same with the 09:15-09:45 range | same |
| `opt_pdh_pdl` | close crossing above the previous day's high (call) / below its low (put) | same |
| `opt_no_expiry` | `opt_orb` but never on expiry day (contrast) | same |

Exits on the **premium**: default stop -25 %, target +50 %. Variants: stop
-25/-35/-50 % x target +30/+50/+100 %; the underlying back at the range
midpoint as the stop; trailing stops (trail 20 % after +30 %, 25 % after
+50 %); flat by 14:45 on expiry day. Flat by 15:05 (15:00 on expiry day).
A bar touching stop and target counts as the stop; a gap through the stop
fills at the modelled open.

### The method: real-price-anchored simulation

Expired contracts have no free intraday history (Angel serves only live
contracts), so the option's 5-minute price is modelled, but tied to real prices:

1. **NSE F&O bhavcopy** (UDiFF, `nsearchives.nseindia.com/content/fo/`, plain
   GET, about a second apart): every contract's real open, high, low, close
   and lot size each day. Only the rows needed are kept (Nifty: nearest two
   expiries plus this and next month's monthly, +-15 strikes; stocks: nearest
   two expiries, +-15 strikes) as `trader/cache/fo_bhav/<date>.json.gz`; the
   zips are never stored. A 404 (holiday) is remembered; reruns resume.
2. **Angel One** 5-minute bars of the underlying (Nifty 50 token 99926000) and
   India VIX (99926017), cached in `trader/cache/bars/angel/`, at least 3 s
   between calls, 60 s wait on a 403 (max 3 retries). Read-only.
3. **Price** = Black-Scholes (call/put, r and dividend yield q configurable,
   time in trading minutes to 15:30 on expiry day). The volatility is
   **implied from that contract's bhavcopy open** against the underlying's
   09:15 open (fallback: bhavcopy close vs the underlying close; fallback: India
   VIX x the median IV/VIX ratio). `--vix-drift` moves it with VIX intraday.
4. **Reality clamp**: each modelled price is clipped into the contract's real
   day low/high. A target above the real high is never hit; a stop below the
   real low never fills there. Trades whose result the clamp changed are
   flagged (`clamp_changed`) and their share reported.
5. **Costs**: `nse.option_charges` (Rs 20 an order, STT 0.15 % of the sell
   premium, 0.1 % before 2026-04-01, NSE txn 0.03553 %, SEBI, stamp 0.003 % buy,
   GST 18 %), plus the half-spread each side.
6. **Size**: `alloc-pct` of capital in premium, whole lots (the bhavcopy lot
   that day: Nifty 75, then 65), at least one lot or skipped (counted). Results
   per lot are reported for every signal so sizing hides nothing.

### The honesty checks

- `options validate` picks live contracts (Nifty's two nearest weeklies and
  the monthly, ATM +-2 strikes; the stocks' current month ATM +-1), fetches
  Angel's real 5-minute bars (`exchange NFO`) and compares the model bar by bar:
  median absolute % error (raw, clamped, with VIX drift), by moneyness and by
  trading days to expiry, and whether a -25 % / +50 % trade entered at 09:35,
  11:00 and 13:00 ends the same way. No expiry day can be validated (expired
  contracts are gone).
- Every run also compares the model's last-bar price with the bhavcopy close
  and last price of every traded contract, expiry days included
  (`meta.model_vs_bhav_close`).
- Walk-forward: the first ~2/3 of the expiry cycles choose the variant (best
  average net rupees a trade, at least 10 trades); only the last ~1/3 is
  reported as out of sample. Verdicts as above, in rupees a trade, with the
  ruin statistic appended.
- Account risk on Rs 1L: max drawdown in rupees and %, worst day, longest
  losing streak, and the share of 20-session windows where the account fell
  more than 30 %. Expiry day vs other days, calls vs puts and by month.

### Output

`trader/backtest/<date>-options-<UNDERLYING>.json` + `.md` (meta, ranking,
per strategy full metrics, buckets, variants, walk-forward, trades with their
contract, IV and source, real day range, unclamped outcome),
`<date>-options-validation.json` + `.md`, and the dashboard-ready
`<date>-options-dash.json` with flat rows: `opt_strategies`, `opt_equity`,
`opt_by_bucket`, `opt_validation`, `opt_meta`.

## Swing: multi-day strategies vs holding the index (`backtest.py swing`)

Asks the question a swing trader should ask first: **after Indian delivery costs, do these
strategies beat simply holding a Nifty index fund over 10+ years?** **Not financial advice.
A backtest never places an order**; nothing here goes near india-broker, which only ever
sends an order after your typed "yes".

```bash
python3 "${CLAUDE_SKILL_DIR}/backtest.py" swing list
python3 "${CLAUDE_SKILL_DIR}/backtest.py" swing fetch --universe nifty100 --years 12   # Yahoo daily, cached
python3 "${CLAUDE_SKILL_DIR}/backtest.py" swing fetch --universe nifty500               # resumable
python3 "${CLAUDE_SKILL_DIR}/backtest.py" swing run --strategy momentum_rotation --variants
python3 "${CLAUDE_SKILL_DIR}/backtest.py" swing run --strategy all --sizing risk --broker angel
python3 "${CLAUDE_SKILL_DIR}/backtest.py" swing compare --universe nifty100   # JSON + .md + dash JSON
python3 "${CLAUDE_SKILL_DIR}/backtest.py" swing compare --universe nifty500   # the survivorship check
```

Options: `--universe nifty100|nifty500|nifty50|pit100|pit500|FILE|SYM,SYM` (default nifty100),
`--years 12` (simulate from Jan 1 of this year - 11; fetch adds ~14 months of warm-up),
`--from/--to`, `--capital 100000`, `--max-positions 10`, `--sizing equal|risk`,
`--risk-pct 1`, `--broker dhan|angel`, `--slippage-core 0.10`, `--slippage-other 0.25`
(% a side), `--offline DIR`, `--core-list CSV`, `--no-extras`, `--variants`, `--json`,
`--write` (run), `--no-write` (compare), `--now`.

### Strategies (long only, daily bars, signal on the close, entry at the next open)

| name | entry | exit | ranked by | variants |
|---|---|---|---|---|
| `pullback_rsi2` | close > 200-day SMA, RSI(2) < 10 | close > 5-day SMA or 10 sessions | lowest RSI(2) | RSI 5 / 15, a 3 x ATR stop |
| `oversold_bounce_d` | close > 200-day SMA, RSI(14) < 30 | RSI(14) > 50 or 15 sessions | lowest RSI(14) | RSI 25 / 35, 10 sessions |
| `breakout_20d` | close above the prior 20 closes (scanner's rule) | close below the prior 10-day low | 6-month return | 55-day, 20-day exit |
| `high_52w` | new 52-week closing high on >= 1.5x 50-day volume | close < 50-day SMA | volume multiple | 2x, no filter, 20-day SMA |
| `ma_cross` | 50-day SMA crosses above the 200-day | crosses back below | 6-month return | 20/100, 50/150 |
| `momentum_rotation` | month-end: top 10 by 12-1 month return among stocks above their 200-day SMA | left the top 10 (first session of the month) | the momentum | top 5 / 20, 6-1 month |
| `gap_volume_drift` | gap >= 4 %, volume >= 3x, close in the top half (a results-drift proxy) | stop at the gap day's low, else 20 sessions | volume multiple | 10 / 40 sessions, 3 % gap |
| `bull_flag_d` | india-scanner's bull flag at yesterday's close, today closes above the flag high | stop at the flag low, target 2R, or 20 sessions | the pole's run | 3R, 10 sessions |

Benchmarks on the same dates: `bench_nifty50` (^NSEI) and `bench_niftybees` (NIFTYBEES), buy and hold.

### The engine

- **No look-ahead**: a signal uses data up to that day's close; the entry fills at the next
  session's open + slippage (0.10 % a side for Nifty 100 stocks, 0.25 % for the rest). Stops are
  checked on each day's low; an open at or under the stop fills at that open (gap through), else
  at the stop, minus slippage. Targets are limits. A day touching both counts as the stop. An
  entry that opens under its stop is skipped. Close-based and time exits fill at the next open.
  A day with no range (high = low: locked at a circuit) is untradeable: the entry is dropped,
  exits and stops wait for the next day with a range.
- **Portfolio**: Rs 1,00,000, at most 10 positions, one per stock, compounding, cash earns 0.
  Equal weight = last close's equity / 10; risk sizing = 1 % of equity to the initial stop (2 x
  ATR(14) as a notional stop when the strategy has none), capped at equal weight. More signals
  than free slots: the strategy's score decides.
- **Delivery costs** (`nse.delivery_charges`, checked on the brokers' pricing pages 2026-10-29):
  STT 0.1 % on the buy AND the sell, NSE txn 0.0030699 %, SEBI Rs 10/crore, stamp 0.015 % on the
  buy, GST 18 %, and the **DP charge once per scrip per sell day** (Dhan Rs 12.50 + GST; Angel One
  Rs 20 + GST). Brokerage: Dhan Rs 0; Angel One the lower of Rs 20 or 0.1 % an order (min Rs 5).
  On a Rs 10,000 position a round trip costs about 0.37 % at Dhan before slippage, 0.57 % with it.
  Every compare also runs the default variants on the other broker and before costs.
- **Walk-forward**: every variant runs over 2015 to today; the best in-sample (2015-2021) Sharpe
  with 30+ trades is chosen, and only its 2022-to-today result is reported as out of sample.
- **Regimes**: each day takes the Nifty's state at the previous close: up (above a rising 200-day
  SMA), down (below a falling one), sideways; and India VIX >= 20 (high) or below.

### What it reports

Per strategy: CAGR, total return, max drawdown % and its length (calendar days, peak to recovery
or to today), Sharpe-like (daily returns, rf = 0), exposure %, trades, win %, average trade %
after costs, average hold, profit factor, t-stat, best / worst year, a by-year table against the
Nifty, a by-regime table (annualised), costs as % of gross profit, the out-of-sample CAGR and
drawdown, the verdict (as above, in CAGR terms: "no edge after costs", "positive ... but it did
not hold out of sample", "positive after costs ... and held out of sample" only with 200+ trades
and t >= 2) and one line against the index: "beat Nifty by X % a year with Y pts deeper
drawdown" or "did not beat simply holding the index".

Writes `trader/backtest/<date>-swing-<universe>.json` + `.md` and the dashboard-ready
`<date>-swing-dash.json` (all universes run that day; compact, under 1 MB) with flat rows:
`sw_strategies`, `sw_equity` (weekly; fortnightly if needed for size), `sw_years`,
`sw_regimes`, `sw_meta`, and `sw_bias` when a point-in-time run has its survivor-biased twin that day.

### Data

Yahoo chart JSON, `SYMBOL.NS`, interval 1d with period1/period2 (range=max returns monthly bars),
one symbol at a time ~0.6 s apart, backing off on HTTP 429, cached in `trader/cache/daily/`
(refreshed once a session; an interrupted fetch resumes). Yahoo's OHLC is split- and
bonus-adjusted (checked on RELIANCE's 1:1 bonus, ex 2024-10-28) but **not dividend-adjusted**:
the strategies earn price returns only. That is why there are two benchmarks: ^NSEI (the price
index, no dividends: like for like) and NIFTYBEES (the ETF you can buy: its price carries the
reinvested dividends less expenses, about 1.2 % a year more). Bad prints (1-3 bars 40 % away and
straight back) are dropped; an open 35 %+ from the last close (impossible under NSE's price
bands: a demerger such as Tata Motors -> TMPV or Vedanta in 2026) back-adjusts earlier prices.

### Point-in-time universes: `--universe pit100 | pit500` (survivorship removed)

```bash
python3 "${CLAUDE_SKILL_DIR}/backtest.py" swing pit-fetch               # NSE CM bhavcopies 2013-11 .. today (~1 s a day, resumable)
python3 "${CLAUDE_SKILL_DIR}/backtest.py" swing pit-fetch --probe-gaps  # weekend sessions (Muhurat, Budget days) the prev closes reveal
python3 "${CLAUDE_SKILL_DIR}/backtest.py" swing pit-fetch --ca          # NSE corporate-action (Bc) files, every 3rd session
python3 "${CLAUDE_SKILL_DIR}/backtest.py" swing pit-build               # trader/cache/pit_universe.json + cache/pit_daily/
python3 "${CLAUDE_SKILL_DIR}/backtest.py" swing pit-check               # adjusted series vs Yahoo's split-adjusted closes
python3 "${CLAUDE_SKILL_DIR}/backtest.py" swing compare --universe pit100   # + side by side with today's nifty100 run
python3 "${CLAUDE_SKILL_DIR}/backtest.py" swing compare --universe pit500
```

- **Source**: NSE's own capital-market bhavcopy, every trading day, plain GET on
  nsearchives.nseindia.com (never the cookie API): legacy `content/historical/EQUITIES/YYYY/MON/
  cmDDMONYYYYbhav.csv.zip` up to **2024-07-05**, UDiFF `content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip`
  from **2024-07-08** (NSE back-filled UDiFF to 2024-01-01; earlier dates 404). A 404 is a holiday.
  Only slim rows of series EQ / BE / BZ are cached (gzipped JSON per day, `trader/cache/cm_bhav/`).
- **Universe**: at every month-end, series-EQ stocks with an INE ISIN (no ETFs or fund units),
  >= 200 sessions of history and a close >= Rs 20, ranked by the **median daily traded value of the
  last 126 sessions**; top 100 = `pit100`, top 500 = `pit500`. A list is known at that close and
  governs every signal until the next month-end. Stocks that later delisted, merged or collapsed stay
  in for the months they qualified. Renames (same ISIN, new symbol: ZOMATO -> ETERNAL) continue one
  series under the newest symbol.
- **Engine**: a stock can only be **bought** on a signal date when it is in that month's list
  (momentum rotation ranks members only); **exits are always allowed**. Slippage 0.10 % while in that
  month's top 100, 0.25 % otherwise. A held stock whose bars end (delisted, merged, suspended for
  good) is **sold at its last close**: fair for a merger or buy-out, optimistic for a failure
  (counted and reported as `delisted` exits). Benchmarks unchanged (^NSEI, NIFTYBEES from Yahoo).
- **Corporate actions**: the bhavcopy is unadjusted and NSE's PREVCLOSE is **not** adjusted on
  ex-dates either (checked: INFY bonus 2014, RELIANCE bonuses 2017 / 2024, HDFCBANK split 2019, IRCTC
  split 2021, Tata Motors demerger 2025: prev close = the raw last close in legacy, Pd and UDiFF). So
  adjustments come from NSE's corporate-action list (the Bc file in the daily PR zip,
  `archives/equities/bhavcopy/pr/PRddmmyy.zip`): bonus a:b, split and consolidation by the purpose's
  exact ratio (accepted only when the ex-day gap agrees), rights and demergers by the ex-day open /
  last close (NSE's special pre-open price) when it fell 3 %+. Not dividend-adjusted (price returns,
  like ^NSEI). `pit-check` lines every series up against Yahoo's split-adjusted closes.
- `compare --universe pit100` reads that day's `nifty100` JSON (run it first) and adds the side by
  side; the dash JSON gains `sw_bias` rows (strategy, universe_pair, cagr_biased, cagr_pit,
  inflation_pp, oos_biased, oos_pit, plus max DD, Sharpe, excess).

What it found (2015-01-01 to 2026-10-29, default variants, after Dhan delivery costs; CAGR %,
survivor-biased -> point-in-time; Nifty 50 price index +9.0, NIFTYBEES +10.1):

| strategy | Nifty 100 -> pit100 | OOS | Nifty 500 -> pit500 | OOS |
|---|---|---|---|---|
| momentum_rotation | +27.6 -> +11.2 | +18.8 -> +14.5 | +40.0 -> +26.9 | +30.5 -> +31.8 |
| breakout_20d | +16.0 -> +1.4 | +7.8 -> -4.8 | +22.5 -> +2.2 | +20.1 -> -0.3 |
| high_52w | +12.6 -> +5.2 | +14.1 -> +0.4 | +20.4 -> +6.2 | +16.9 -> +4.5 |
| ma_cross | +12.3 -> +3.1 | +13.5 -> +8.9 | +18.7 -> +9.8 | +10.0 -> +8.6 |
| oversold_bounce_d | +0.8 -> +1.3 | +1.4 -> +1.4 | +7.7 -> +6.5 | +8.9 -> +7.7 |
| others | all negative either way | | | |

Survivorship inflated breakouts by 15-20 points a year and momentum by 13-16; only momentum
rotation still beats the index on point-in-time data (pit500 with deeper drawdowns: 53 %).

### Read this before believing any number

- **SURVIVORSHIP BIAS IS LARGE.** The universe is today's Nifty 100 / 500 list applied to
  2015-2026: stocks that left the index or collapsed are missing, stocks that joined after a huge
  run (BSE, IRFC, Mazagon Dock, CG Power...) are in from the start. Momentum and breakout results
  are the most inflated. Treat every nifty100 / nifty500 CAGR as an upper bound and use the
  point-in-time runs (`--universe pit100 | pit500`) for the honest number.
- **Taxes are not modelled.** Short-term capital gains (STCG) tax on gains held under a year and
  LTCG above the exemption apply to every profitable sale; a strategy that turns its book over
  many times a year pays far more than buy-and-hold. Not tax advice.
- Dividends are left out of the strategies' returns (see Data); cash earns 0.
- Fills at the open with fixed slippage; no circuit limits, no impact cost for size; Rs 1 lakh
  split ten ways makes fixed charges (the DP charge) a real drag.
- A strategy that beat the index here can still lose to it from here. Not financial advice.

## Paper-trading the momentum rotation forward (`momentum_paper.py`)

The one swing rule that beat the index on the survivorship-free list, run forward on paper so the result
cannot be fitted. Same rule as `momentum_rotation` (pit100, top 10, 12-1 month momentum, above the
200-day SMA), same slippage, delivery charges for the broker you pick. Paper only: it never imports a
broker module or sends an order.

```bash
python3 "${CLAUDE_SKILL_DIR}/momentum_paper.py" init --capital 100000 [--universe pit100] [--top 10] [--broker angel]
# each month's last session, after ~18:00 IST (when NSE's bhavcopy is out):
python3 "${CLAUDE_SKILL_DIR}/backtest.py" swing pit-fetch && python3 "${CLAUDE_SKILL_DIR}/backtest.py" swing pit-build
python3 "${CLAUDE_SKILL_DIR}/momentum_paper.py" plan                  # the new top 10, sells and buys
python3 "${CLAUDE_SKILL_DIR}/momentum_paper.py" fill                  # on or after the next session's open
python3 "${CLAUDE_SKILL_DIR}/momentum_paper.py" status                # vs Nifty 50 and NIFTYBEES since the first fill
```

`plan` refuses a date that is not its month's last session (NSE's calendar decides; pit.py counts the last
cached session as a month-end when the next session is in a new month). `fill` uses that day's open from
the cached bhavcopy, else Yahoo's daily bar; a buy locked at a circuit is skipped (as in the backtest), a
sell that cannot fill stays pending. With Rs 1 lakh and 10 slots a stock above ~Rs 10,000 a share cannot
be bought and its slot stays in cash. Ledger: `trader/paper/momentum.json`.

## Reading the results honestly

- 60 sessions is a short, single-regime sample; the out-of-sample third is 20
  sessions. Setups that fire rarely (gap and go, oversold bounce) cluster in a
  few market days, so their trades are not independent.
- Survivorship: today's index list is applied to the past.
- Yahoo bars are not NSE's tape (dropped bars, small price differences from
  Angel's, about 1 basis point on average).
- Slippage is an assumption; a fast market fills worse. Stops are assumed to
  trigger on the bar's low; real stop-loss orders can slip further.
- Charges are Dhan's intraday schedule; other brokers differ slightly.
- A strategy that wins here can still lose live. Not financial advice.

## How it fits the pack

india-scanner finds setups, india-entry times the entry, india-exit sets the
stop and target, india-timemachine replays one plan, india-budget sizes it,
india-risk checks it and india-broker sends it after your "yes". This skill
sits beside that pipeline: it tells you which of those setups have earned a
place in it. With identical rules (orb15, midpoint stop, 2R, no slippage, the
time machine's sizing) it reproduces the time machine's history replay trade
for trade.

## Differences from the US pack

The US pack has no backtester across a universe; the time machine's history
replay was the nearest thing. This one is India-only: IST sessions, NSE ticks
and holidays, the F&O closing auction, Dhan's charges, india-budget's sizing,
Nifty universes, and Angel One SmartAPI for a longer history.
