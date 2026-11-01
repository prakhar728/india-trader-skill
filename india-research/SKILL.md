---
name: india-research
description: >-
  Strategy Researcher for NSE: hunts for real, repeatable intraday edges AFTER
  Indian costs that beat simply holding the index, and is built so it cannot
  fool itself. Every hypothesis is logged with its mechanism before it is
  tested (append-only, hash-chained registry, every parameter combination
  counted as a trial); each dataset is split once into explore (oldest ~60 %),
  validate (~20 %, anchored walk-forward) and a LOCKED holdout (latest ~20 %)
  that the data loader physically withholds and that runs once per idea. Eight
  gates decide: at least 100 trades, a day-clustered bootstrap 95 % CI of net
  R and rupees per trade above zero, a day-clustered t-stat, the Deflated
  Sharpe Ratio (Bailey & Lopez de Prado) using the registry's trial count, a
  cost stress (charges x1.5, slippage x2), breadth across stocks and months,
  beating buy-and-hold of the universe and the Nifty 50, and no look-ahead
  (the engine streams bars and trips on any peek). Tests long AND short
  intraday equity (sell first, cover by flat-by; STT on the sell leg, stamp on
  the buy), mean reversion, sector lead-lag (NSE Nifty 500 industry groups and
  return correlations), time-of-day and regime filters with NIFTY and India
  VIX context, and records NSE announcements, board meetings, corporate
  actions, insider and bulk/block deals with price snapshots for future event
  studies. Never places an order. Use when the user says "research
  strategies", "find an edge", "test an idea on X", "why doesn't intraday
  work", "is there any strategy that works", "test shorting", "mean
  reversion", "lead-lag", "sector peers of X", "record events", "deflated
  Sharpe", "how many trials", "run the holdout", or "research report".
argument-hint: "[init | log --id X --from-example short_orb_breakdown | pipeline X | gate X | holdout X | peers SYM | events record | report]"
---

# india-research

The backtester (`india-backtest`) answers "how did these seven rules do?". This skill
answers a harder question: **is there any rule at all that earns money after Indian
costs, more than just holding the index, and that is not a fluke of the data or of how
many things we tried?** A negative result, honestly reported with the number of trials,
is a success. Nothing here places, modifies or cancels an order. Not financial advice;
past results are not a forecast and nothing here promises any daily or monthly return.

It is designed to be driven by the `strategy-researcher` subagent
(`agents/strategy-researcher.md`, installed to `~/.claude/agents/`), but every command
works by hand.

## The loop

1. **Log first.** `research.py log --id <id> ...` records the hypothesis, the
   *mechanism* (why someone should be on the other side of the trade), the exact rules,
   the parameter grid and the dataset BEFORE any result exists. Status `idea`.
2. **Explore** on the oldest ~60 % of sessions. `research.py explore <id>` runs every
   grid combination; each counts as a trial in the registry (re-running the same code and
   grid is not counted twice; a changed file or grid is). Status `tested`.
3. **Validate** on the next ~20 %: an anchored walk-forward in folds (default 2). Each
   fold trades the variant that was best on everything before it; the first validate run
   freezes the lab file (any later change means a new id).
4. **Gate** (`research.py gate <id>`): eight PASS/FAIL lines with numbers. All eight on
   validate -> `candidate`; otherwise `rejected`.
5. **Holdout, once** (`research.py holdout <id>`): only a candidate, only with the frozen
   file. A claim is written to the registry before a single holdout bar is read; a second
   run for the same id is refused forever. Gates again -> `holdout-passed` /
   `holdout-failed`.
6. **Report** every result, failures included, with the total trial count
   (`research.py report`). The report re-states every gate's DSR at TODAY's trial count
   next to the DSR as gated, so an old gate faces the bar it would face now.
7. **Paper only.** `research.py promote <id>` moves a holdout-passed idea to `paper`:
   the local simulated ledger of india-broker. Going live is the user's decision, never
   research's.

`research.py pipeline <id>` = explore + validate + gate.

### Rules that keep the count and the holdout honest

- **Log every look.** Anything measured outside `explore` (a table in a scratch script,
  a lag correlation, a daily-bar study, an india-backtest or options run you read before
  deciding what to log) is a trial too: `research.py note --looks N --dataset KEY
  --family F --why "what was looked at and how it was counted"`. It is append-only, adds
  N to `total_trials`, and every later DSR uses the new total. Count generously: every
  statistic you read that could have changed what you test next is a look.
- **Never measure on validate or holdout dates outside the pipeline.** The lab loader
  withholds them, and india-backtest (`backtest.py run|compare`, `backtest.py options
  run|compare`) now withholds every session inside a locked holdout by default (one line
  on stderr says how many); `--allow-holdout` includes them and the output records
  `holdout_included: true`. Do not pass it for research. Scratch scripts must cut raw
  bars at the split's cutoff before building anything (as `lab_engine.load_dataset`
  does). india-backtest's swing.py does not withhold yet: keep swing runs off research
  windows by hand (`--to` before the validate start).
- **Exposure is recorded.** A new split stores `exposed_before`: every
  `trader/backtest/*.json` artefact and every other split whose explore/validate windows
  already overlapped its validate / holdout dates for its symbols. For splits made before
  that scan existed, `research.py exposure-note --dataset KEY --source "..." --why "..."
  [--artefact FILE ...]` records the finding once, append-only. `research.py splits` and
  the report show both. A future holdout run on an exposed family is confirmation, not
  discovery: say so in the memo.
- **Portfolio capital cap (default ON).** The engine streams each session across all
  stocks in decision-time order and takes a trade only while the open positions plus it
  fit `capital` (Rs 1,00,000; trader/config.json) at entry value and `max_positions` (5);
  the rest are skipped and counted (`capital_skips`, printed with each run and stored in
  the run files and registry records). Per-trade R, rupees, the daily-sum t-stat and the
  benchmark gate are then what an account of that size could have done.
  `--no-capital-cap` (a global flag) gives per-trade stats only, recorded in the run;
  use it only when the question is per-trade and say so. Run files made before
  2026-10-29 have no cap: ignore their account-level totals.
- **Never-sized stocks are reported.** A stock whose every price is above the position
  cap (BOSCHLTD above Rs 25,000) can never trade; explore/validate print it as a note
  and store it in the run (`never_sized`).

## Commands

```
python3 research.py init                                    # trader/research/ + the 3 example strategies
python3 research.py log --id short_orb_v2 --from-example short_orb_breakdown --universe nifty50
python3 research.py log --id fade_gap_v1 --family mean-reversion --rationale "..." --rules "..." \
        --grid '{"gap": [1.0, 2.0]}' --universe nifty100 --source yahoo     # then write lab/fade_gap_v1.py
python3 research.py explore|validate|gate|pipeline <id> [--folds 3]
python3 research.py holdout <id>                            # once
python3 research.py promote <id> | reject <id> --why "..."
python3 research.py list [--json] | show <id> | verify | splits
python3 research.py note --looks 12 --dataset angel:nifty100:5m --family mean-reversion --why "..."
python3 research.py exposure-note --dataset angel:nifty100:5m --source "audit 2026-10-29" --why "..." \
        [--artefact 2026-10-29-nifty50-all.json ...]                  # once per dataset and source
python3 research.py --no-capital-cap pipeline <id>          # per-trade stats only (recorded)
python3 research.py peers TATAPOWER [--top 10]
python3 research.py events record|show|stats
python3 research.py report                                  # report.md + research-dash.json
```
Global flags: `--trader-dir`, `--now "YYYY-MM-DD HH:MM"`, `--list-file` (an NSE index
CSV; stored as an absolute path so the id re-runs from any folder), `--sectors` (the
Nifty 500 list CSV), `--no-capital-cap`. `log` takes `--universe` (nifty50, nifty100,
nifty500, tata, a file, or SYM,SYM), `--source yahoo|angel` (the bar caches under
`trader/cache/bars/`, filled by india-backtest) or `--bars DIR[,DIR]`.

Research reads bars from the caches only; refresh them with india-backtest
(`backtest.py compare --universe nifty50` for Yahoo, `--source angel --days 365` for a
year from Angel One). The NIFTY 50 and India VIX bars come from the Angel cache
(`NIFTY-5m`, `INDIAVIX-5m`), filled by `backtest.py options fetch`.

## Files (trader/research/)

| file | what |
|---|---|
| `experiments.jsonl` | the registry: one JSON line per event (log, explore, validate, gate, status, holdout_claim, holdout, note = side looks, exposure_note), each carrying the hash of the line before. `verify` checks the chain; any edit or deletion breaks it and every write stops. Trials = explore combinations + `note` looks. |
| `splits.json` | per dataset (`yahoo:nifty50:5m`, ...): explore / validate / holdout date windows, made the first time a dataset is used and never changed; new splits also carry `exposed_before` (informational). |
| `lab/<id>.py` | the strategies (plain Python, see below). |
| `runs/<id>-<split>.json` | every trade of a run, the cost-stress trades, the benchmark series, the look-ahead audit. |
| `events/YYYY-MM.jsonl` | the event recorder's store (append-only). |
| `report.md`, `research-dash.json` | the report and the dashboard rows (`rs_experiments`, `rs_meta`). |

## Writing a lab strategy

A file `trader/research/lab/<id>.py` with `META` and `signals(day, ctx)`. The engine
(`lab_engine.py`, whose docstring is the full contract) calls `signals` once per closed
5-minute bar inside the entry window (09:30-14:30 by default), for one stock and one
session, with ONLY the bars that have closed. Return the trades to open at the close of
the last visible bar:

```python
META = {"family": "mean-reversion", "kind": "intraday", "side": "both",
        "rationale": "why it should work", "rules": "the exact rules",
        "default": {"k": 2.0}, "grid": {"k": [1.5, 2.0, 2.5]}, "max_trades_per_day": 1}

def signals(day, ctx):
    b = day.bars[-1]                      # the bar that just closed (day.time = its end, "HH:MM")
    return [{"side": "short", "entry_time": day.time, "entry": b["c"],
             "stop": b["c"] + 2.0, "target": b["c"] - 3.0, "reason": "..."}]
```

`day`: `symbol, date, k, time, bars, tick, prev, daily, sector, avg_value20, state`.
`ctx`: `params, time, window, index (NIFTY 50), index_prev_close, vix (India VIX),
vix_prev_close, peers (sector peers' bars, same cut), sector, events`. Helpers in
`lab_engine`: `vwap`, `atr`, `end_time`, `slot_of`. Bars are read-only; missing Yahoo
bars are `None`.

The engine then does what the live pack does (india-backtest's code, imported): stop and
target to the tick (longs round DOWN, shorts round UP), 1 tick of slippage, a gap through
the stop at the open, a bar touching both counts as the stop, flat by 15:05, sizing like
india-budget (max loss incl. charges, 25 % position cap, max order value, cash buffer),
Dhan's intraday charges, one position per stock at a time.

### Shorts (intraday equity, series EQ)

Sell at the decision bar's close minus slippage; buy to cover by flat-by. Stop above
(rounded UP), target below (rounded UP so it stays reachable). Charges use
`nse.charges(buy_value=cover, sell_value=entry)`: STT 0.025 % on the sell leg and stamp
duty 0.003 % on the buy leg, whatever their order. Intraday shorting of EQ stocks is
allowed in India (it must be squared off the same day; BE/BZ cannot be shorted
intraday). Research never changes `allow_short` in `trader/config.json`.

### Examples (india-research/lab/, copied by `init`)

- `short_orb_breakdown`: first close under the 15/30-minute range low, stop at the
  midpoint or the high, 1.5R/2R. 8 combinations.
- `vwap_reversion`: fade a close at least k x ATR(14, 5-minute) away from VWAP back to
  VWAP, both sides, from 10:00. 3 combinations.
- `sector_lead_lag`: the sector's most-traded stock (20-session average traded value,
  fixed before the day) moves at least move_pct % in 15 minutes; a peer that moved less
  than a third of that follows it. 4 combinations.

## The gates

| gate | passes when |
|---|---|
| min_trades | at least 100 trades (intraday) / 30 (swing) on the split |
| bootstrap_ci | the lower end of the 95 % CI of mean net R AND of mean net rupees per trade is above 0 (2,000 resamples of whole days, fixed seed) |
| t_stat | t of the DAILY net R sums >= 2 (trades on one day are not independent) |
| dsr | Deflated Sharpe Ratio >= 0.95: the chance the true per-trade Sharpe beats the best of N lucky trials, N = every trial in the registry (explore combinations + logged side looks), corrected for skew and fat tails. `gate`, `show`, `list` and the report also show it re-stated at today's N |
| cost_stress | still positive with charges x1.5 and slippage x2 |
| breadth | positive on >= 60 % of the stocks that traded and >= 60 % of the months |
| benchmark | daily Sharpe above buy-and-hold of the same universe (equal weight) and of NIFTY 50 on the same days, and positive |
| no_look_ahead | 0 peeks, every trade decided on bars closed by its entry, and the loader's newest bar before the cutoff |

A short history cannot pass easily: with Yahoo's 60 days the validate split is 12
sessions. Use Angel One for a year or more when an idea deserves it (a new dataset gets
its own split; another dataset's holdout dates are removed for shared symbols).

## No look-ahead and the locked holdout, technically

- The loader drops every bar (5-minute, daily, NIFTY, VIX, events) on or after the
  split's cutoff before sessions are built, and asserts it. An explore run cannot hold a
  validate or holdout bar; a validate run cannot hold a holdout bar.
- The holdout loads only with a `HoldoutTicket`, which re-reads the registry and exists
  only while there is exactly one claim and no result for that id.
- `day.bars`, `ctx.index`, `ctx.vix` and every peer's bars are views cut at the decision
  bar; reading past it raises `LookAheadError` and the engine fails the run even if the
  strategy swallowed the error. Returning a trade dated to an earlier bar at which the
  strategy did not want it (a back-dated decision) also fails. Entries are only at the
  last closed bar's close.

## Peers

`research.py peers TATAPOWER` prints the stock's NSE industry group (from the Nifty 500
list's Industry column) with each member's daily-return correlation, and the most
correlated stocks among everything in the bar caches. Holdout dates are left out unless
`--include-holdout`.

## The event recorder

`research.py events record` reads NSE's public feeds on `nsearchives.nseindia.com`
(Online_announcements, Board_Meetings, Corporate_action, Financial_Results,
InsiderTrading RSS; bulk.csv and block.csv at most once an hour), stores each new item
for Nifty 500 stocks (`--universe all` for every EQ stock) with OUR detection time, and
fills price snapshots when they come due: at detection, +5m, +15m, +60m, the reaction
session's close and the next session's close. Outside market hours the clock starts at
the next 09:15 open. Intraday snapshots come from Yahoo 1-minute bars (about 5 sessions
kept; older ones are marked missed), closes from Yahoo daily bars, with the Angel 5-minute
cache as a fallback. `lag_min` = minutes from the filing's time stamp to our detection:
the very first run back-fills whatever the feeds hold, hours late, so event strategies
must filter on it. Idempotent (ids never stored twice) and locked against two runs at
once; at most 40 symbols are priced per run (oldest first).

Schedule it every 5 minutes. It must run on this machine (it writes `trader/`):

```
# crontab -e  (macOS: give cron Full Disk Access, or use a launchd agent with StartInterval 300)
*/5 * * * 1-5 cd /path/to/trading && /usr/bin/python3 india-research/research.py events record >> trader/research/events/cron.log 2>&1
0 9,18 * * 0,6 cd /path/to/trading && /usr/bin/python3 india-research/research.py events record >> trader/research/events/cron.log 2>&1
```
Inside a Claude Code session, `/loop 5m python3 india-research/research.py events record`
works while the session is open. Claude Code's `/schedule` runs routines in the cloud,
which cannot see this machine's `trader/` folder, so it is not a fit for the recorder.

**Event strategies cannot be tested until enough events exist.** The intraday gates
need 100 trades on the validate split, which is ~20 % of the dates, so a category needs
about **500 complete events over at least 60 sessions** (`research.py events stats`
says when a category is TESTABLE). Until then, event ideas stay `idea`.

## Data rules

Plain GETs only: Yahoo's chart JSON, `nsearchives.nseindia.com` / `archives.nseindia.com`
files and RSS. Research itself never calls Angel One: it reads the bar caches. When the
caches are refreshed from Angel One (read-only candle routes via india-backtest), keep to
one request every 3 seconds and back off 60 seconds on a 403 (`backtest.py options fetch`
paces this way; plain `backtest.py --source angel` paces at about one a second, so fetch
a few symbols at a time). Never `www.nseindia.com/api` or anything needing browser
cookies. Angel One keys stay in `.env` and are never printed.

## Caveats

Survivorship (today's index lists applied to the past), Yahoo's short 5-minute history,
bars that are the feed's and not NSE's tape, model fills (real fills in a fast market
can be worse), no circuit-limit or liquidity checks, and the DSR's own assumptions
(the trial-SR variance falls back to the null 1/T, never below it). Swing experiments
can be logged and gated (30-trade minimum) but this engine runs intraday only. Research
never places an order; a backtest is not a forecast. Not financial advice.
