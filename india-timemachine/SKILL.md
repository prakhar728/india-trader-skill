---
name: india-timemachine
description: >-
  Time Machine for NSE day trades. Replays every possible outcome of a trade
  before you risk a rupee: 10,000 simulated sessions plus every past time
  the setup fired. Takes the plan from india-exit (entry, stop, target),
  rebuilds the rest of the day, from the entry to your flat-by time (15:05
  IST by default), 10,000 times from the stock's own 5-minute bars at the
  same time of day over its last 60 sessions, and re-runs the same rule on
  every past session. Copes with Yahoo's missing bars and the F&O closing
  auction (no 15:20 bar). Prints the odds of target first, stop first, or
  neither by flat-by, where the price sits then, the average R gross and
  after Dhan's intraday charges, the history, and one honest verdict, then
  hands off to /india-budget. Use when the user says "run the time machine",
  "replay this trade", "what are the odds", "simulate this trade", "monte
  carlo", "backtest this setup", "how often does this hit the target", "is
  this a coin flip", "is it worth it after charges", or after /india-exit
  writes a plan, before /india-budget.
argument-hint: "[path to plan.json, or SYMBOL]"
---

# india-timemachine

The Time Machine sits between the plan and the money. india-exit has set the
entry, the stop and the target. Before anything is sized or sent, this skill
asks one question: if today plays out like the stock's own last 60 sessions,
how often does this plan hit the target first, the stop first, or neither by
the time you must be flat? It answers with 10,000 replays and with the real
record of the same rule on every past session, in R before and after the
charges an Indian intraday round trip pays. It is a simulation built from the
stock's own recent bars, not a prediction. Day trading loses most people
money, and a good-looking replay does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # trader/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write            # also save trader/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 15:30      # replay to the close, not flat-by
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol RELIANCE --date 2026-10-29 \
    --entry 1174.30 --stop 1163.30 --target 1196.30 --entry-time 09:40
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from           # ASCII fan chart of the saved run
```

Other flags: `--replays N`, `--seed N` (default 19), `--window N` (default 2
bars), `--qty N` (shares for the charges), `--offline FILE` (a saved Yahoo
chart JSON, `.json` or `.json.gz`), `--now "2026-10-29 09:45"` (IST) to pin
the clock.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the plan from india-exit: symbol, date, entry, entry_time / entry_bar, stop, stop_rule, target, tick, flat_by, opening range, charges qty. With no entry time in it, it takes the time from `trader/entry.json` when that is the same symbol, day and price |
| `trader/config.json` | `max_loss_per_trade` (the share count for charges when the plan has none), `no_entries_before` / `no_entries_after` (the history's entry window), `flat_by`, `data_source` |
| Yahoo chart API | one request: 60 days of 5-minute bars for `SYMBOL.NS` (`range=60d&interval=5m`), free and unofficial, can lag |
| Dhan `/v2/charts/intraday` | instead of Yahoo when your Dhan keys are set and `data_source` picks Dhan (one request, 90 days back; needs Dhan's Data plan) |
| `trader/timemachine.json` | with `--write`: `{plan, mc, history}` plus the verdict. `mc` holds the odds, percentiles, gross and net R, the fan-chart bands, 160 sample paths and a 24-bin histogram |

## How it works

1. **The pool.** The last 60 sessions before the plan's date that have at
   least 60 of the 75 five-minute bars from 09:15 to 15:25 IST. Today is
   never in it. Yahoo drops a bar or three a day (72 to 75 a session), and
   F&O stocks have no 15:20 bar since NSE's closing auction started on
   2026-08-03: gaps are normal and never fatal. Every bar becomes four
   ratios to the close of the bar before it (the last one that exists):
   open, high, low and close.
2. **One replay.** Start at the entry price. For each bar left until
   `--until` (default the plan's flat-by, 15:05), pick a past bar from the
   same time of day (plus or minus 2 bars) at random and scale it to the
   current price. A time slot that fewer than half the sessions have (15:20,
   or 15:25 which only some days carry) is skipped and named. Bar by bar it
   checks the stop first, then the target. A bar that touches both counts as
   the stop.
3. **10,000 replays** give four odds. Target first. Stop first. Neither,
   which means the trade was still open at `--until`. Green, which means
   target first or above the entry at `--until`. R is +2 at a 2R target, -1
   at the stop, and otherwise the distance from the entry divided by the
   risk. A gap through the stop still counts as -1R here; a real fill can be
   worse.
4. **After charges.** Each replay's exit also pays Dhan's round-trip charges
   for the plan's share count (from india-exit, or `max_loss_per_trade /
   risk`): net R = ((exit - entry) x shares - charges) / (risk x shares).
   Both averages are printed.
5. **The history.** The plan's own rule (the same opening range, stop rule
   and target R, inside the entry window 09:30 to 14:30) runs again on every
   one of those past sessions. That is what really happened, not a
   simulation. A session with a missing opening-range bar is not tested and
   is counted.
6. **The verdict** is one line from the numbers. It looks only at the
   replays that hit one side and asks how often the target came first,
   against the share that breaks even AFTER charges: |net R at the stop| /
   (net R at the target + |net R at the stop|). At 2R with RELIANCE's
   charges that is 37 %, not the 33 % it would be for free. Less than 5
   points away: "Close to a coin flip" (1R) or "Close to break-even". 5 to
   15 points: "Leans toward". 15 or more: "Strongly toward".
7. **Below the verdict**, when it applies: a line when more than 40 % of the
   replays hit neither, and the average net R when its sign goes against the
   lean.

**The seed.** The draws come from Python's `random.Random(seed)`, seed 19 by
default. Same plan, same data, same seed: the same numbers, on any machine.
Say the seed with every result so the user can repeat it.

**5-minute bars only.** It refuses, in one plain line, when there are fewer
than 20 usable sessions, when the entry bar is not in the data (wrong date,
not closed yet, dropped by the feed), on a special session like Muhurat
trading, or when the plan does not make sense (a stop above the entry, an
entry off the 5-minute grid from 09:15). A live in-progress bar is never used.

## Steps

1. Make sure `trader/plan.json` is today's plan from `/india-exit`. If there
   is none, run that first, or pass the plan as flags.
2. Run `timemachine.py run --write`.
3. Tell the user, in this order: the four odds, the median and the 5 to 95
   range of the price at flat-by, the average R gross and after charges, the
   history line, then the verdict line and any line under it exactly as
   printed. Say the seed.
4. If the verdict is a coin flip, close to break-even or toward the stop, say
   so plainly. Do not dress it up. If the history and the replays disagree,
   say that too.
5. Say once that this is built from the last 60 sessions and is not a
   prediction. Results days, news, circuit limits and F&O expiry can do
   things those sessions never did. Check `/india-news` for the stock first.
6. If the user still wants the trade, hand off to `/india-budget` to size it.

## Example: RELIANCE, Thursday 2026-10-29

The 15-minute opening range is 1,156.20 to 1,170.50. The 09:35 bar closes
above it: entry 1,174.30 at 09:40 IST (bar 4), stop 1,163.30 (the range
midpoint, rounded down to the 0.10 tick), target 1,196.30 (2R, risk 11.00 a
share), flat by 15:05. Offline on the recorded bars, seed 19:

```
10,000 replays of the rest of the session, 09:40 to 15:05 IST (65 five-minute bars), seed 19.
  target first   2.9 %
  stop first     28.9 %
  neither        68.2 %   still open at 15:05
  green          45.3 %   target first, or above the entry at 15:05
  Price at 15:05: median 1173.35, 90 % of replays between 1157.02 and 1190.16.
  Average R: -0.060 gross, -0.174 after charges
History: ... on each of those 59 real sessions fired 28 times: 8 target, 13 stop, 7 neither.
Average R +0.156 gross, -0.031 after charges.
Strongly toward the stop: at 2R (+1.89R after charges, -1.11R at the stop) the target has to come
first in 37 % of the replays that hit one side; it did in 9 % ...
68.2 % of the replays hit neither by 15:05; the price at 15:05 decides those.
```

A 2R target 1.9 % above the entry rarely prints before 15:05 on RELIANCE's
recent days, and the charges turn the history's small edge into a small loss.
That is the honest answer, and it is what the user should hear before
`/india-budget` sizes 45 shares.

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Repeat
  them, do not rewrite them. Never judge a 2R plan by target first vs stop
  first alone: the verdict already weighs the target's R and the charges.
- Simulated odds are not predictions and not promises. Say it once, plainly.
- Yahoo data is free, unofficial and can lag. Bars are 5-minute bars,
  checked bar by bar.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target or date is a new run.

## What it never does

It never places an order, never sizes a position, never changes the plan,
and never calls a result a sure thing. It never uses a bar that has not
closed yet, and never mixes today's bars into the pool.

## Safety

- Not financial advice. Education and tooling only; a replay is not a
  recommendation.
- Paper mode by default (`"mode": "paper"`, a local ledger: Dhan has no paper
  trading). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except india-broker, and only when
  india-risk passed that exact order today AND the user typed an explicit
  "yes" for that one order in the chat.
- Dhan keys live in environment variables, never in the chat or in files.
