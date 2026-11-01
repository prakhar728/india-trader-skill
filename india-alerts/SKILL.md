---
name: india-alerts
description: >-
  Price Alerts for NSE. Rings when a price, a stop, the pre-open or the
  opening bell hits. Turns the trade plan into three alerts in one go (the
  stop, the target and "flat by" at your flat-by time, 15:05 IST by default,
  never later than Dhan's 15:10 auto square-off), plus above / below / stop /
  target levels, percent moves from the previous close, any IST time of day,
  the 09:00 pre-open call auction and the 09:15 opening bell, each ringing
  once so you are not spammed. Knows NSE hours, the 2026 NSE holiday list and
  special sessions like Muhurat trading (Sunday 2026-11-08), works out IST
  itself, fetches quotes from Yahoo (SYMBOL.NS, one at a time, delayed) or
  from Dhan's LTP API (one call for every symbol) when your Dhan keys are
  set, and writes trader/prices.json for india-budget and india-risk. Use
  when the user says "alert me when", "tell me if RELIANCE hits 1,200",
  "watch my stop", "watch my trade", "remind me to square off by 3:05", "is
  the market open", "is NSE open today", "when does the market open", "is it
  a holiday", "Muhurat trading timing", "get me a price", "what is INFY at",
  or right after /india-exit writes a plan.
argument-hint: "[symbol] [price]"
---

# india-alerts

Price Alerts watches the numbers so the user does not have to stare at a
chart. An alert is information, never an order: when a stop alert rings,
nothing is sold by this skill. With a Dhan super order the stop-loss and the
target legs already sit at the broker and do the work; the alerts tell the
user what happened and when it is time to be flat.

Not financial advice. Nothing in this pack places an order without the
user's typed "yes" for that exact order.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
It imports `../india-common/nse.py`, so keep the two folders side by side.

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock                          # pre-open / open / closed, next open (IST)
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan                # stop, target, flat-by from trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell             # 09:15
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --pre-open                 # 09:00
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --stop 1163.3
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --target 1185.2
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --at 15:05 --label "flat by" --before-close --symbol RELIANCE
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol INFY --pct 2
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check                          # exit 0 = something rang
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote RELIANCE --write         # -> trader/prices.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote --holdings --write       # plan, paper ledger and watchlist symbols
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset reliance-plan-stop
```

`--now "2026-10-29 15:05"` pins the clock (IST); `--offline DIR` reads saved
quote files instead of the network; `--source yahoo|dhan|auto` overrides the
config's `data_source`. Symbols can be typed as `reliance`, `RELIANCE.NS` or
`NSE:RELIANCE`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the trade from india-exit, for `add --from-plan` |
| `trader/config.json` | `flat_by`, `data_source`, `extra_holidays`, `special_sessions` |
| `trader/alerts.json` | the rules (`alerts.example.json` in this folder) |
| `trader/state/alerts.json` | which rules rang and when (IST), so each rings once |
| `trader/prices.json` | the latest quotes, merged in, for india-budget and india-risk |
| `trader/paper.json`, `trader/watchlist.json` | symbols for `quote --holdings` |

`TRADER_DIR` (or `--trader-dir`) points it at another trader/ folder.

## Rule types

| type | rings when | rings again |
| --- | --- | --- |
| `above` / `target` | price at or above `price` | after `reset`, or on its own if `"rearm": true` and the price fell back |
| `below` / `stop` | price at or below `price` | same |
| `pct_move` | moved `pct` percent from the previous close (or `ref`), optional `direction` up or down | once per day at most |
| `time` | the IST clock passed `at` ("15:05") on a trading day | once per trading day |
| `pre_open` | NSE's 09:00-09:15 pre-open call auction is running | once per trading day |
| `opening_bell` | NSE's normal market (or a special session) is open | once per trading day |

A rule with `"date"` is watched on that IST date only; `--from-plan` rules
carry the plan's date, so yesterday's stop never rings today. The flat-by
time comes from the plan, else `flat_by` in config, through `nse.flat_by`:
default 15:05 and never later than 15:10, when Dhan squares off intraday
positions itself. A `time` rule with `"before_close": true` stays at least 5
minutes before a special session's close (Muhurat trading is one hour).

## Quotes

- `yahoo`: `query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS`, an
  unofficial free JSON endpoint, one request per symbol with a half-second
  pause, then `query2` on an error. A 404 is Yahoo's "No data found" (a wrong
  or renamed symbol, e.g. TATAMOTORS after the demerger) and is not retried.
  Delayed and unofficial: fine for alerts and for checking a size, not for
  timing an order.
- `dhan`: `POST https://api.dhan.co/v2/marketfeed/ltp` with every security id
  in ONE call (up to 1,000 ids, one request a second), when
  `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` are set in the shell (never pasted
  in chat). Needs Dhan's paid Data API plan. Security ids come from Dhan's
  instrument file via `nse.scrip_master()` (cached for 20 hours in
  `trader/cache/`). LTP has no previous close, so a `pct_move` rule needs a
  `"ref"` price (`--ref`) when Dhan is the source.
- `auto` (default): Dhan when the keys are set, else Yahoo.
- Each price in `trader/prices.json` keeps its quote time and when it was
  fetched (IST, `+05:30`); india-risk only uses a fresh one.

## The NSE clock

From `nse.py`: IST is UTC+05:30 all year (no daylight saving). Pre-open
09:00-09:15 (orders 09:00-09:08, matching to 09:15), normal market 09:15 to
15:30. F&O stocks stop continuous trading at 15:15 for the closing auction
(since 2026-08-03). Closed on weekends and NSE's 2026 cash-market holidays
(Republic Day, Holi, Good Friday, Independence Day, Gandhi Jayanti, Dussehra,
Diwali Balipratipada on Nov 10, Guru Nanak Jayanti on Nov 24, Christmas and
the rest of the official list). `clock` says pre-open, open or closed, why
(weekend, the holiday's name, before the open, after the close) and the next
open in IST.

- **Muhurat trading**: Sunday 2026-11-08 (Diwali). NSE announces the hour
  separately, so it is not hard-coded: `clock` reminds the user, and once NSE
  publishes it, add `"special_sessions": {"2026-11-08": ["18:00", "19:00"]}`
  (with the real times) to `trader/config.json`. The opening bell and flat-by
  alerts then follow that session.
- **2027**: NSE publishes the list in December. Until then a 2027 weekday is
  treated as a trading day and `clock` warns; add closures to
  `extra_holidays` in config.

## Steps

1. Right after india-exit writes the plan, run `add --from-plan`. Confirm the
   three alerts back to the user: the stop, the target and the flat-by time.
2. Run `check` while the trade is open, every few minutes from a scheduled
   task or `/loop 3m`. Each run fetches only the symbols in today's rules and
   rings each alert once. A check reads the latest delayed quote; it is not a
   live feed, and a price can pass a level between two checks.
3. Tell the user what rang in one line each. A stop or target alert means the
   super order's leg has probably filled: check the position in Dhan and
   record the fill with india-broker. "Flat by" means close anything still
   open: india-broker `flatten`, then india-risk, then the user's yes. Never
   act for them.

## Example (Thursday 2026-10-29)

Plan: RELIANCE long at 1,170.60, stop 1,163.30 (opening-range midpoint,
rounded down to the 0.10 tick), target 1,185.20 (2R), flat by 15:05.
`add --from-plan` writes `reliance-plan-stop`, `reliance-plan-target` and
`reliance-flat-by` for that day. A `check` at 15:06 with Yahoo's 15:29 quote
of Rs 1,167.40:

```
NSE OPEN (2026-10-29 15:06 IST, normal market).
ALERT Flat by: it is 15:06 IST (set for 15:05), NSE closes at 15:30. If RELIANCE is still open,
close it: india-broker flatten --symbol RELIANCE builds the sell ticket, then india-risk and your
yes. Dhan squares off intraday positions itself at 15:10.
```

It rings once. `clock` at 16:00 says: closed, after the close, next open
2026-10-30 09:15 IST (Friday), plus the Muhurat reminder.

## What is different from the US version

NSE hours in IST (no daylight saving to work out), special sessions
(Muhurat) instead of half-day early closes, a pre-open alert, flat-by capped
at Dhan's 15:10 square-off, rupees with Indian digit grouping, Dhan's LTP API
as the second source (one call for all symbols), and `trader/paper.json` (the
local paper ledger) instead of a broker portfolio file. The clock, holidays
and quotes come from the shared `india-common/nse.py`.

## Rules

- An alert is a fact about a price or the clock. Say it as a fact.
- Never present a quote as live. Yahoo is delayed; Dhan LTP is a snapshot.
- Confirm every new rule back to the user: symbol, type, level, day.

## What it never does

It never places, cancels or changes an order, never claims a quote is live,
never predicts where a price is going, and never calls NSE's website APIs.

## Safety

- Not financial advice. Education and tooling only; an alert is not a
  recommendation.
- Paper mode by default (`"mode": "paper"`, a local ledger: Dhan has no paper
  trading). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except india-broker, and only when
  india-risk passed that exact order today AND the user typed an explicit
  "yes" for that one order in the chat.
- Dhan keys live in environment variables, never in the chat or in files.
