---
name: india-risk
description: >-
  Risk Manager for NSE/BSE intraday trades. Checks every order against your
  limits and stops anything over them before it can reach Dhan. A stop is
  required; the loss at the stop plus round-trip charges must fit your max
  loss per trade; reward:risk must meet your minimum; today's risk must fit
  your daily loss limit (and a realised loss at the limit stops the day); no
  more than your trades per day; no entries outside 09:30-14:30 IST, after
  flat-by, or when NSE is closed (holidays, weekends, special sessions). India
  checks: series must be EQ (BE/BZ trade-to-trade is blocked), the stock must
  be in Dhan's instrument file with the same security_id, prices must sit on
  the stock's tick, entry and target inside today's circuit band when Dhan's
  quote is available, product INTRADAY (no leverage, shorts or F&O unless
  enabled), and the Dhan super-order body must say the same as the order.
  With "broker": "angel" it checks Angel One's scrip master (token, series,
  tick) and the Angel placeOrder bodies instead, and blocks MARKET, IOC and
  ROBO orders (prohibited or unconfirmed for algos on Angel One).
  Getting out is never blocked. Prints PASS or BLOCK with reasons and records
  them in trader/risk.json. Use when the user says "check my order", "is this
  safe", "risk check", "does this break my rules", "can I take this trade",
  "how much have I risked today", or before /india-broker, always.
argument-hint: "[path to orders.json]"
---

# india-risk

The Risk Manager is the gate between a proposal and Dhan. Every order written
by india-budget, or by india-broker's `flatten`, passes through it. A BLOCK is
final for that order: it is not shown for a yes and it is not sent. A PASS is
not a yes either: india-broker still asks the user.

Not financial advice. Nothing is placed without the user's "yes".

`${CLAUDE_SKILL_DIR}` below is this skill's folder.

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py"                            # trader/ files; writes trader/risk.json
python3 "${CLAUDE_SKILL_DIR}/check.py" --json --no-write          # look without recording
python3 "${CLAUDE_SKILL_DIR}/check.py" --now "2026-10-29 09:41"   # pin the clock (IST)
python3 "${CLAUDE_SKILL_DIR}/check.py" --no-quote                 # skip Dhan's circuit-band quote
python3 "${CLAUDE_SKILL_DIR}/check.py" --scrip-offline api-scrip-master.csv
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (india-budget, or india-broker `flatten`) |
| `trader/config.json` | the limits (india-budget ships `config.example.json`) |
| `trader/prices.json` | a fresh price (under 15 min) raises the entry a MARKET order is measured from |
| `trader/broker.json` | entries sent today (trades, daily risk used, duplicates); live positions snapshots |
| `trader/paper.json` | paper mode: open positions and today's realised P&L |
| `trader/cache/dhan-nse-equity.json` | Dhan's instrument file: security_id, series, tick |
| `trader/cache/angel-nse-equity.json` | Angel One's scrip master (broker angel): token, series, tick |
| `trader/risk.json` | the verdicts, one fingerprint per exact order, plus a history |

Dhan's `POST /v2/marketfeed/quote` gives `upper_circuit_limit` and
`lower_circuit_limit`. It is used only when `DHAN_CLIENT_ID` and
`DHAN_ACCESS_TOKEN` are in the shell, the account has Dhan's Data plan, and
`data_source` is not `yahoo`. Without them the band check is skipped silently;
if the call fails it is a warning, never a block.

## Entries and exits

An **entry** opens a position: a buy, or a sell of shares you do not hold (a
short). An **exit** only sells what you hold (paper: the ledger; live: the
positions snapshot `dhan.py flatten` or `dhan.py positions` saved). Getting
out must always be possible: an exit is checked for format, the Dhan body and
short selling only. The day limits, window, blocked list and size turn into
warnings for it.

## The checks (an entry is blocked when)

| check | blocks when |
| --- | --- |
| format | no symbol, side not buy/sell, qty not a whole number, exchange not NSE/BSE, order type not LIMIT/MARKET |
| dhan | the super-order body disagrees with the order (side, segment, product, type, securityId, quantity, price, targetPrice, stopLossPrice), a `dhanClientId` on disk, a trailing stop, a bad correlationId, the wrong endpoint |
| product | MARGIN/MTF/CO/BO without `allow_leverage`; CNC while the config says INTRADAY |
| F&O | an NSE_FNO/derivative order (this pack sizes cash equity only) |
| short | selling what you do not hold (`allow_short` false) |
| symbol | not in Dhan's NSE/BSE equity list, or a different security_id |
| series | not EQ: BE and BZ are trade-to-trade, no intraday |
| tick | entry (LIMIT), stop or target off the stock's tick (Dhan would reject it) |
| circuit | entry outside today's band; target above the upper circuit (a stop under the lower circuit is a warning) |
| blocked | the stock is in `blocked_symbols` |
| size / position / cash | over `max_order_value`, over `position_cap_pct` of capital, more than capital minus the buffer (leverage) |
| stop / loss | no stop, a stop at or above the entry, qty x risk + charges over `max_loss_per_trade` |
| reward:risk | (target - entry) / (entry - stop) under `min_reward_risk` |
| daily loss | today's risk used plus this one over `daily_loss_limit`, or today's realised loss already at it |
| trades | `max_trades_per_day` entries already sent today |
| window | before `no_entries_before`, after `no_entries_after`, at/after `flat_by`, or NSE closed (pre-open counts as closed) |
| duplicate | the same stock already entered today, or twice in this batch |

How the numbers work:

- **The entry it measures from**: the limit price for a LIMIT order. For a
  MARKET order, the higher of the plan's entry and a fresh price.
- **Daily loss** counts every entry sent today in the current mode at its full
  max loss incl. charges, win or lose; if the paper ledger shows it really
  lost more (a stop that slipped), the real loss counts. A realised loss at
  the limit ends new entries for the day.
- **The window** is IST from the pack's NSE calendar (2026 holidays; 2027 is
  not published yet, so it warns), plus `extra_holidays` and
  `special_sessions` from the config.

## Steps

1. If the price may have moved, refresh it (india-alerts writes trader/prices.json).
2. Run `check.py`. Exit 0: every order passed; 1: at least one blocked.
3. Tell the user in plain words: the max loss at the stop with charges, reward:risk,
   how much of today's limits it uses. For a BLOCK give the reason and the fix (fewer
   shares, a closer stop from india-exit, wait for the window, or skip it). Never change
   a limit yourself.
4. If the order changes in any way, run the check again: india-broker matches each
   order to its verdict by fingerprint, and a verdict from yesterday does not count.

## Example (RELIANCE, 2026-10-29 09:41 IST, paper)

```
PASS  #1 buy RELIANCE 21 shares, entry
          entry 1174.30 (limit price), stop 1163.30, target 1196.30: risk 11.00 a share
          max loss Rs 231.00 + Rs 26.22 charges = Rs 257.22 (limit Rs 500), reward:risk 2.00
          today before this: 0 of 3 trades, Rs 0.00 of Rs 1,500 daily loss used, realised Rs 0.00
```

At 14:41 the same order is blocked: `window: no new entries after 14:30 IST`.
The 14:50 sell from `dhan.py flatten` passes as an exit.

## Angel One (`"broker": "angel"`)

The same checks, with Angel One's scrip master (`OpenAPIScripMaster.json`;
`--scrip-offline FILE` reads it from disk) in place of Dhan's: the order's
`security_id` must be Angel's token for the symbol (RELIANCE 2885), the series
EQ, prices on Angel's tick. Instead of the Dhan body it checks the `angel`
block, and blocks the order when:

| check | blocks when |
| --- | --- |
| angel body | it disagrees with the order: `variety` NORMAL, `tradingsymbol` SYMBOL-EQ, `symboltoken`, `transactiontype`, `exchange` NSE, `ordertype`, `producttype`, `duration` DAY, `price` (the entry, or an exit's `limit`), `quantity`; a value that is not a string; `squareoff`/`stoploss` not 0; the route not `placeOrder`; a `dhan` block next to it |
| angel stop | an entry without `stop_order`, or one that is not STOPLOSS / STOPLOSS_LIMIT SELL for the same quantity with `triggerprice` = the stop and a limit above 0, at or under the trigger and on the tick |
| no market | `order_type` MARKET, `ordertype` MARKET or STOPLOSS_MARKET, `duration` IOC: Angel One's April 2026 notice prohibits market and IOC orders for algorithmic trading |
| no ROBO | `variety` ROBO: bracket orders are not confirmed for NSE intraday via the API |
| exchange | anything but NSE (the pack's Angel support is NSE cash) |

These apply to exits too (an Angel exit must be a LIMIT), but an exit is
still never blocked by the day limits, the window or the blocked list.
The circuit-band check is skipped for Angel One: its quote's FULL mode may
carry the limits, but the field names are not in the SDK, so the pack does
not guess. The info lines show what the stop-limit's limit could cost
beyond the stop.

## What is different from the US pack

Rupees, IST and the NSE calendar; charges in the loss; Dhan's instrument
file (series, security_id, tick) and circuit band; product/leverage/F&O
rules for Indian brokers; the Dhan body cross-check. The pattern-day-trader
check has no Indian equivalent and is gone.

## What it never does

It never places an order, never edits an order to make it pass, never
loosens a limit, never blocks a sell that only gets the user out, and never
treats a PASS as the user's yes.

## Safety

- Not financial advice. A PASS is a rules check, not a recommendation.
- Paper by default. Live is the user's own edit to `"mode"` in their config.
- Nothing is placed without the user's "yes", recorded by india-broker, and Claude Code's own
  permission prompt for the send command.
- Dhan keys live in the shell environment, Angel One keys in the trading folder's `.env`;
  never in the chat.
