---
name: india-scanner
description: >-
  Chart Scanner for NSE. Scans hundreds or thousands of Indian stock charts for
  setups: opening-range breakouts, bull flags, 20-day breakouts, oversold
  bounces. Builds the universe from NSE's own lists (the Nifty 500 by default,
  or every series-EQ stock in EQUITY_L.csv), drops BE/BZ trade-to-trade and SME
  stocks (no intraday there), marks F&O stocks, filters by price (Rs 50) and
  turnover (Rs 10 crore), runs a pre-market scan of daily closes (20-day
  closing-high breakout, bull flag, reclaimed the 20-day average, oversold,
  oversold bounce) and an intraday opening-range breakout scan on 5-minute bars
  in IST (range 09:15 to 09:30), then writes trader/watchlist.json for
  india-news and india-entry. Free Yahoo data (.NS), or Dhan's market feed and
  intraday charts when your Dhan Data API keys are set. Use when the user says
  "scan the market", "scan Nifty 500", "find me setups", "what is breaking out
  on NSE", "opening range breakouts", "ORB scan", "scan for bull flags",
  "oversold stocks", "pre-market scan", "build my watchlist", "what should I
  look at today", "F&O stocks breaking out", or at the start of an Indian
  trading day before /india-news.
argument-hint: "[universe | daily | orb] [--at HH:MM] [--universe nifty500|all]"
---

# india-scanner

The Chart Scanner is the first step of a day trade on NSE. It reads free
charts and lists the ones that match a rule. That is all it does. A scan is a
list of charts that match a rule, not a list of stocks to buy, and a setup on
a chart is not a prediction. SEBI's own studies found that about 7 in 10
individual intraday traders in the cash segment lost money; the scanner does
not change that. It only saves you from flipping through 500 charts by hand.

**Not financial advice.** Nothing in this pack places an order without your
typed "yes" for that exact order, and this skill never places, sizes or checks
an order at all.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                          # Nifty 500, series EQ -> trader/universe.txt
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe --universe all           # every EQ stock on NSE (~2,370)
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                             # pre-market -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --symbols RELIANCE,TCS,INFY # or --watchlist FILE, --limit 50
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --universe all --fno-only   # F&O stocks only
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                               # after 09:35 IST -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:15                    # cut the clock at 10:15 IST (replay today)
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --source yahoo --json
```

Flags: `--universe nifty500|all|FILE` (default nifty500), `--min-price 50`
(rupees), `--min-turnover` (rupees crore: daily 10, orb 0), `--fno-only`,
`--or-minutes 15`, `--max-confirm 200` (orb stage 2), `--max-missing 2`
(5-minute bars a chart may miss and stay usable), `--source auto|yahoo|dhan`,
`--now "2026-10-29 10:15"` (IST) to pin the clock, `--offline PATH` to read
saved Yahoo files, `--equity-list / --index-list / --fo-list CSV` to read
NSE's lists from disk, `--no-write`, `--show N`, `--json`. Exit code 0 when
the scan found something, 1 when it found nothing or could not run (the reason
is one plain line).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/watchlist.json` | written by `daily` and `orb`: `mode`, `as_of`, `universe`, `scanned`, `failed`, `source`, `exchange: NSE`, `hits[]` with plain NSE symbols (`RELIANCE`, `M&M`), `name`, `fno`. india-news and india-entry read `hits[].symbol` |
| `trader/universe.txt` | written by `universe`: one `SYMBOL\|Name[\|F&O]` a line; pass it back with `--universe trader/universe.txt` |
| `trader/config.json` | `no_entries_after` (default 14:30), `data_source` (auto / yahoo / dhan), `special_sessions` (Muhurat) |
| `trader/cache/` | NSE's EQUITY_L.csv, ind_nifty500list.csv, fo_mktlots.csv (refreshed after 20 hours), Dhan's instrument file |

## What is different for India

- **Universe from NSE, not a broker.** `nifty500` is NSE's
  `ind_nifty500list.csv` (501 rows on Oct 29 2026, 496 kept: 3 REITs in
  series RR and 2 stocks in series BE are out). `all` is NSE's `EQUITY_L.csv`: 2,620 rows, 2,368 series EQ kept, 219 BE
  and 33 BZ dropped. ETFs and SME stocks are not in EQUITY_L.
- **Series matters.** Only EQ allows intraday. BE and BZ are trade-to-trade:
  every trade must be delivered. A symbol you pass by hand that is not EQ is
  skipped with the reason.
- **F&O marked.** From NSE's `fo_mktlots.csv` (218 stocks in Oct 2026). F&O
  stocks have NSE's closing auction from 15:15, so their 5-minute bars stop at
  15:15.
- **Rupees, crore, IST.** Prices in rupees (Rs 1,174.30), turnover in crore
  (1 crore = 10 million), every time in IST (UTC+05:30, no daylight saving).
- **Turnover, honestly.** Yahoo's spark answer has daily closes but no daily
  volumes, so the turnover filter is the LAST session's price x volume (during
  the session: today's so far), not a 20-day average. Say so if it matters.

## The daily scan (before the open)

Yahoo's spark endpoint returns closes for 20 symbols a request (it answers 400
above 20), so the Nifty 500 takes 25 requests and `all` takes 119, with a
half-second pause between them. Today's daily bar is left out until the 15:30
close.

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

Hits are listed most rupees traded first.

## The opening-range breakout scan (during the session)

The opening range is the first `--or-minutes` (15) of the session: the 09:15,
09:20 and 09:25 five-minute bars. A hit is the first completed 5-minute bar
after the range to CLOSE above the range high, at that bar's close time (the
earliest is 09:35). Before 09:35, on a weekend or NSE holiday, after the 15:30
close, or after `no_entries_after` (14:30 by default: the setup has expired)
it prints one plain line and writes nothing.

**Yahoo** (no Dhan keys, or `--source yahoo`):
1. Stage 1, every symbol: spark closes of today's 5-minute bars, 20 a
   request. A chart goes on only if a completed bar after the range closed at
   or above the highest close of the range (a necessary condition: nothing is
   lost).
2. Stage 2, each candidate: the v8 chart with real highs and lows, one request
   each, at most `--max-confirm` (200), the most rupees traded first. When it
   stops short it prints PARTIAL with the count it skipped.

**Dhan** (`DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` in your shell, Dhan's paid
Data API plan, `data_source` auto or dhan):
1. Stage 1: one `POST /v2/marketfeed/ohlc` snapshot per 1,000 symbols (one
   request a second). A chart goes on only if the day's high is above the
   day's open (a breakout close beats the range high, which is at least the
   open).
2. Stage 2: `POST /v2/charts/intraday` 5-minute bars per candidate, strongest
   move from the open first, at most `--max-confirm`.

A chart is usable when all three range bars are there and at most
`--max-missing` (2) bars after the range are missing: Yahoo drops a bar here
and there (expect 72 to 75 a session, never assume 75). A bar off the
5-minute grid (the live tick) is dropped, and a bar counts only once it is
complete (start + 5 minutes). Muhurat trading works when you put its hours in
`special_sessions`; the range is then the first 15 minutes of that session.

## Data and limits

- Yahoo's chart and spark endpoints are free, unofficial and can lag by
  minutes. query1 first, query2 on a 429 or 5xx, one retry after a 5-second
  wait on a 429. Bars are 5-minute bars, never tick by tick.
- Dhan's data is real time but needs the paid Data plan; without it Dhan
  answers an error and the scan says so. Keys only ever come from your shell,
  never from the chat or a file.
- Partial results are said as partial: "Fetched 480 of 499 charts (19 failed
  or had no data)". Never round a partial scan up to "the whole market".
- NSE holidays for 2026 are built in. 2027 is not published yet: the scan
  warns and treats weekdays as trading days. Add closures to
  `trader/config.json` `extra_holidays`.

## Steps

1. Before the open (or the evening before): `scan.py daily`. Tell the user the
   counts (fetched, passed the filters, scanned, hits) and the top few by
   setup, with the date of the closes.
2. After 09:35 IST (10:15 is the classic): `scan.py orb`. Tell the user how
   many usable charts broke their range and when, and that a later hit is
   closer to the price now.
3. The user picks one chart. Hand it on: `/india-news SYMBOL` (what is moving
   it, any results date this week), then `/india-entry SYMBOL`. Never pick for
   them.

## Example: Thursday 29 October 2026

Replayed at 10:15 IST over 35 large NSE stocks
(`tests/fixtures/india-scanner/nifty5m-2026-10-29.json.gz`, real Yahoo charts):

```
python3 scan.py orb --offline nifty5m-2026-10-29.json.gz --now "2026-10-29 10:15"
Fetched 35 of 35 charts; 33 usable (2 under the price / turnover floor); 25 candidates, 25 confirmed with real highs and lows.
19 of 33 charts closed above their opening-range high by 10:15 IST:
  09:40  RELIANCE      Rs 1,174.30  range 1,156.20 to 1,170.50  +0.32% over  F&O
  ...
  10:10  SBIN            Rs 961.65  range 955.00 to 961.50  +0.02% over  F&O
```

RELIANCE's range is the 09:15 to 09:25 bars, high 1,170.50, low 1,156.20; the
09:35 bar closed at 1,174.30, 0.32 % over the high, at 09:40. IDEA and YESBANK
were under the Rs 50 floor. The user picks one and Claude hands it to
`/india-news RELIANCE`, then `/india-entry RELIANCE`.

## Rules

- Say what was scanned: the universe, how many symbols, how many charts came
  back, how many were usable, and the time of the data (IST).
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a P&L, a return or a profit figure.
- A hit is stale once the price has moved on. Re-run before acting on it.

## What it never does

It never places, sizes or checks an order, never says which stock to buy,
never calls Yahoo's data real time, never calls `www.nseindia.com/api` (only
NSE's public archive files), and never promises that a setup works.

## Safety

- Not financial advice. Education and tooling only.
- Paper by default (`"mode": "paper"`, a local ledger: Dhan has no paper
  trading). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except india-broker, and only when
  india-risk passed that exact order today and the user typed an explicit
  "yes" for that one order in the chat.
- No F&O, shorting or leverage unless the config allows it and the user
  confirms. Broker keys live in environment variables, never in the chat.
