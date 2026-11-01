---
name: india-entry
description: >-
  Entry Timer for NSE stocks. Watches a setup bar by bar and tells you the
  moment to get in. Reads 1-minute or 5-minute bars (Yahoo SYMBOL.NS, free,
  or Dhan's intraday charts API when your Dhan keys are set), builds the
  opening range (the first 15 minutes from 09:15 IST: the 09:15, 09:20 and
  09:25 bars) and calls the entry on the first completed bar that CLOSES
  above the range high, or above a level you give it (like a 20-day high
  from the scanner), with an optional volume check. Says waiting and how
  far, triggered with the time and the price, expired, or not open yet,
  inside your entry window (09:30 to 14:30 IST by default), knows NSE
  holidays and Muhurat trading, and writes trader/entry.json for
  /india-exit. Long side only. It never places an order. Use when the user
  says "tell me when to get in", "watch RELIANCE", "has it broken out", "did
  it break the opening range", "ORB on HDFCBANK", "entry signal", "time my
  entry", "check my watchlist", or after /india-scanner and /india-news,
  before /india-exit.
argument-hint: "[SYMBOL] [--setup orb|level] [--interval 5m|1m]"
---

# india-entry

The Entry Timer turns a setup into one moment: the bar that confirms it. It
reads bars, 1-minute or 5-minute, and waits for each one to finish. A price
that pokes above the high in the middle of a bar is not an entry. A bar that
CLOSES above it is. Then it says the time and the price in IST, writes them
down for the Exit Planner, and stops. It never buys anything. Day trading
loses most people money, and a clean signal does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE                  # 15-minute opening range, 5-minute bars
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --write          # also save trader/entry.json
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --setup level --level 1182.10
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --interval 1m --min-volume-x 1.5
python3 "${CLAUDE_SKILL_DIR}/entry.py" watch RELIANCE --until 14:30    # one line per closed bar, stops on the trigger
python3 "${CLAUDE_SKILL_DIR}/entry.py" watchlist                       # every hit in trader/watchlist.json
```

Other flags: `--or-minutes N` (default 15), `--json`, `--offline FILE` (a
saved Yahoo chart JSON, `.json` or `.json.gz`, or a folder of
`<SYMBOL>.json`), `--now "2026-10-29 09:40"` (IST) to replay a day. `watch`
with `--now` or `--offline` runs on a simulated clock and replays the day bar
by bar without waiting. Symbols are NSE symbols: `RELIANCE`, `M&M`,
`BAJAJ-AUTO` (a `.NS` suffix or `NSE:` prefix is fine).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/watchlist.json` | the scanner's hits (`hits[].symbol`), for `watchlist` |
| `trader/config.json` | `no_entries_before` (09:30), `no_entries_after` (14:30), `flat_by` (15:05), `data_source` (auto / yahoo / dhan), `extra_holidays`, `special_sessions`. Optional |
| Dhan `/v2/charts/intraday` | when `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` are set and `data_source` is auto or dhan (needs Dhan's paid Data plan). If Dhan fails under auto, it says so and uses Yahoo |
| Yahoo chart API | `SYMBOL.NS`, `range=1d&interval=5m` (or `1m`): free, unofficial, can lag |
| `trader/entry.json` | with `check --write`, or when `watch` triggers: symbol, exchange, setup, side long, status, time, bar, price, level, opening range, source |

`TRADER_DIR` moves the whole `trader/` folder.

## The rule

- **Opening range (orb).** The high and low of the first 15 minutes: the
  09:15, 09:20 and 09:25 five-minute bars (or fifteen 1-minute bars). The
  NSE pre-open call auction (09:00 to 09:08) is not part of it. The level to
  beat is the range high.
- **Level.** A price you give, like a 20-day high from `/india-scanner`.
- **The trigger** is the first completed bar after the range that CLOSES
  above the level. A wick above is not a trigger. A close exactly at the
  level is not above it.
- **The time** is that bar's close time (the 09:35 bar closes at 09:40 IST).
  The price is its close.
- **Only closed bars count.** A bar is closed when the clock passes its start
  plus 5 (or 1) minutes and the feed has caught up to that moment (it waits a
  minute at most). Yahoo's live tick, like 15:29:03, is never a bar.
- **Volume, if you want it.** `--min-volume-x 1.5` also needs the trigger
  bar's volume at 1.5 times the range bars' average. Yahoo's 09:15
  five-minute NSE bar often reports volume 0, so bars with no volume are left
  out of that average.
- **Time limits.** No entries before 09:30 or after 14:30 IST (config), and
  never at or after the flat-by time (15:05 by default, because Dhan squares
  off intraday positions at 15:10).
- **The calendar.** NSE's 2026 holidays come from `india-common/nse.py`.
  Weekends and holidays say the NSE is closed and when it opens next. 2027 is
  not published yet: it warns and treats weekdays as open. Muhurat trading
  (Sunday 2026-11-08) has no announced hour in the pack: until the user adds
  `"special_sessions": {"2026-11-08": ["18:00", "19:00"]}` it says so. On a
  special session the range starts at its open and new entries stop 30
  minutes before its flat-by time.
- **What it says.** Waiting (and how far the last close is from the level),
  triggered, expired, or no data. Before 09:15: not open yet. Before 09:30:
  the range is still forming.

## Steps

1. Get the symbol from the user, or run `entry.py watchlist` after
   `/india-scanner`. Check `/india-news` for it first (results day, a block
   deal, a circuit).
2. Run `entry.py check SYMBOL`. Tell the user the status in one line:
   waiting and how far, triggered at what time and price, or expired.
3. To wait for it, Claude can run `entry.py watch SYMBOL --until 14:30` in
   the background, or `entry.py check SYMBOL --write` on a `/loop` every 5
   minutes. `watch` wakes just after each bar closes, prints one line per
   bar, and exits 0 on the trigger (it writes `trader/entry.json`) or 1 when
   the setup expires. Keep the computer awake while it watches: a sleeping
   laptop pauses it, and it catches up, late, when it wakes.
4. When it triggers, say the time and the price, and that nothing was placed.
   If the trigger is more than a bar old, say that too: a late entry is a
   different trade.
5. Hand off to `/india-exit` to set the stop and the target BEFORE anything
   is bought.

## Example: RELIANCE, Thursday 2026-10-29

Real Yahoo 5-minute bars. The opening range is Rs 1,156.20 to Rs 1,170.50. At
09:38 the 09:35 bar is still open, so it waits:

```
WAITING  last close Rs 1,169.50 (09:30 bar), Rs 1.00 under the range high Rs 1,170.50 (-0.09%).
The 09:35 bar is still forming (Rs 1,174.30 so far, closes 09:40); it does not count until it closes.
```

At 09:40 the bar has closed:

```
RELIANCE  Thu 2026-10-29 09:40 IST  opening-range breakout (15-minute range) on 5-minute bars (Yahoo, free, can lag)
Opening range 09:15-09:30: high Rs 1,170.50, low Rs 1,156.20 (3 of 3 bars).
TRIGGERED 09:40 IST at Rs 1,174.30: the 09:35 bar (bar 4) closed above the range high Rs 1,170.50 (+0.32%).
This is the moment to get in, if you take the trade. Nothing was placed.
Next: set the stop and target BEFORE you buy: /india-exit (exit.py plan, reads trader/entry.json).
```

With `--min-volume-x 1` the 09:35 bar is too thin (0.74x the range's
volume) and the trigger moves to 10:00 IST at Rs 1,173.80 (the 09:55 bar,
2.26x). On 1-minute bars the same day triggers at 09:34 IST at Rs 1,171.30.

## Rules

- Bar by bar, on 1-minute or 5-minute bars. Yahoo is unofficial and can lag;
  Dhan needs the user's keys and Data plan. Say which one answered.
- A signal is information, not an order, and not a promise. Nothing here
  promises winning trades or money.
- Long side only. This pack plans longs only. If the user wants a short, say
  that (an intraday short also needs `allow_short` and a broker that allows it).
- One trigger per setup per day: the first close above. Later closes do not
  reset it.
- Never call an entry early because a bar looks like it will close above.
- Only EQ-series stocks trade intraday; BE and BZ are trade-to-trade.
  `/india-risk` blocks those, so do not chase one here.

## What it never does

It never places an order, never sizes a position, never sets the stop (that
is `/india-exit`), never moves the level after the fact, and never counts a
bar that has not closed. It never runs `/india-broker` from a background
watch: nobody is there to say yes.

## Safety

- Not financial advice. Education and tooling only; a trigger is not a
  recommendation.
- Paper mode by default (`"mode": "paper"`, a local ledger: Dhan has no paper
  trading). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except india-broker, and only when
  india-risk passed that exact order today AND the user typed an explicit
  "yes" for that one order in the chat.
- Dhan keys live in environment variables, never in the chat or in files.
