---
name: india-budget
description: >-
  Budget Manager for NSE/BSE intraday trades. Sizes every position to your
  capital and your max loss per trade, in rupees, with Dhan's round-trip
  charges (brokerage, STT, exchange, SEBI, stamp duty, GST) counted in the
  loss at the stop. Reads the trade plan from india-exit (entry, stop,
  target), finds the whole shares that fit inside four limits (max loss per
  trade, position cap, max order value, cash after your buffer), says which
  limit decided it, looks up the stock's Dhan security_id, series and tick in
  Dhan's instrument file, and writes ONE order to trader/orders.json with the
  exact Dhan super-order body (entry, target leg, stop-loss leg), or, with
  "broker": "angel", Angel One's token and tick from its scrip master and the
  Angel placeOrder bodies (LIMIT entry plus the STOPLOSS_LIMIT stop). A stop too
  far away for even one share is a plain refusal, not a smaller order. BE/BZ
  (trade-to-trade) stocks are refused. Nothing is placed. Use when the user
  says "how many shares", "size this trade", "how much should I buy", "I
  have 1 lakh", "I only want to risk 500 a trade", "set my rules", "keep 10%
  in cash", "position size", or after /india-exit writes a plan.
argument-hint: "[capital in rupees]"
---

# india-budget

The Budget Manager turns a plan ("buy RELIANCE at 1174.30, stop 1163.30")
into an exact order ("buy 21 shares"), inside rules the user sets once. The
stop decides the size: the further away the stop, the fewer shares. In India
the costs of a round trip are large enough to matter on a small intraday
trade, so the loss at the stop includes them by default. It writes a
proposal only: india-risk checks it and india-broker asks the user yes or no.

Not financial advice. Nothing is placed without the user's "yes". Most
people who day trade lose money; sizing to a max loss keeps each loss small,
it does not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder.

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                         # trader/plan.json + trader/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                 # write trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 1174.50 --write # LIMIT entry at 1174.50 instead of the plan's
python3 "${CLAUDE_SKILL_DIR}/size.py" --market --write        # MARKET entry (sized at the higher of plan / fresh price)
python3 "${CLAUDE_SKILL_DIR}/size.py" --capital 200000 --json
python3 "${CLAUDE_SKILL_DIR}/size.py" --cash 60000            # the cash you really have caps the size
```

`--now "2026-10-29 09:41"` pins the clock (IST). A plan for another day is
refused: make a new one with india-exit. `--scrip-offline FILE` reads Dhan's
`api-scrip-master.csv` from disk instead of downloading it (it is cached in
`trader/cache/` for 20 hours either way).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | the rules (copy `config.example.json` from this folder) |
| `trader/plan.json` | entry, stop, target, flat-by (india-exit writes it) |
| `trader/prices.json` | optional, MARKET orders only: a fresh (under 15 min) higher price means a bigger risk |
| `trader/cache/dhan-nse-equity.json` | Dhan's instrument file, slimmed: security_id, series, tick |
| `trader/broker.json` | read only, to number the order's `correlationId` |
| `trader/orders.json` | the one proposed order, written with `--write` |

## Step 1: set the rules once

If `trader/config.json` does not exist, copy `config.example.json` there and
ask the user for each value in plain words. The keys (defaults in brackets):

- **capital** (100000): the rupees this is for. Only money they can afford to lose.
- **cash_buffer_pct** (10): what always stays in cash.
- **max_loss_per_trade** (500): the most one trade may lose at the stop, charges included.
- **include_charges** (true): count Dhan's round-trip charges in that loss. Keep it on.
- **position_cap_pct** (25) and **max_order_value** (50000): the biggest single position.
- The day limits india-risk holds: **daily_loss_limit** (1500), **max_trades_per_day** (3),
  **min_reward_risk** (1.5), **no_entries_before** / **no_entries_after** ("09:30" / "14:30")
  and **flat_by** ("15:05"), all IST. flat_by is capped at 15:10, Dhan's intraday auto square-off.
- **product** ("INTRADAY"), **exchange** ("NSE"); **allow_leverage**, **allow_short**,
  **allow_fno** (all false): this pack sizes cash-equity longs only.
- **blocked_symbols**: stocks they never want to trade.
- **mode** ("paper"): keep paper until they have watched it work. Live is their own edit.
- **broker** ("dhan"): `dhan` or `angel` (Angel One SmartAPI). With `angel`:
  **stop_limit_buffer_ticks** (5, how far under the stop the stop-limit's limit sits)
  and **static_ip** (the IP registered on the SmartAPI app). **exit_limit_ticks** (5):
  exits on both brokers are a marketable LIMIT at the last price minus this many ticks.
- **data_source** ("auto"): Dhan's Data API when the keys are in the shell, else Yahoo.
- **extra_holidays**, **special_sessions**: e.g. Muhurat trading `{"2026-11-08": ["18:00", "19:00"]}`
  once NSE announces the hour.

## Step 2: size the trade

Whole shares only. The size is the smallest of four limits:

| limit | shares |
| --- | --- |
| max loss per trade | the most shares whose qty x (entry - stop) + round-trip charges at the stop fit `max_loss_per_trade` |
| position cap | capital x `position_cap_pct` / entry |
| max order value | `max_order_value` / entry |
| cash after the buffer | (capital - buffer) / entry, or (`--cash` - buffer) if lower |

The order is a LIMIT at the plan's entry by default (rounded UP to the
stock's tick if it is off-tick). The stop and the target are rounded DOWN to
the tick: the stop further away (the risk shown is honest), the target
reachable. The tick comes from Dhan's instrument file (`SEM_TICK_SIZE`), else
NSE's price band.

It refuses, and writes nothing, when: the stop is too far for even one share
(it never moves the stop to fit), reward:risk is under `min_reward_risk`, the
stop is not below the entry, the target is not above it, the plan is for
another day, the stock is not in Dhan's list, its series is BE or BZ
(trade-to-trade: no intraday), it is blocked, the plan is a short, or the
product is leveraged. Say the reason in one line.

## Step 3: show the user

Show the four limits, which one decided, the shares, the rupees, the loss at
the stop with the charges spelled out, and say plainly that a stop is an
order, not a promise: in a fast market it can fill below the stop, and a
stock locked at its lower circuit cannot be sold at all. Then hand over to
`/india-risk`.

## Example (real bars, RELIANCE, Thursday 2026-10-29)

Opening range 09:15-09:30 high 1170.50, low 1156.20; the 09:35 bar closed
above it at 1174.30. Stop at the range midpoint 1163.35, rounded down to the
0.10 tick: 1163.30. Target 2R: 1196.30. Default config.

| limit | the math | shares |
| --- | --- | --- |
| max loss per trade | Rs 500 / 11.00 + charges at the stop | 40 |
| position cap | 25% of Rs 1,00,000 = Rs 25,000 / 1174.30 | **21 (decides)** |
| max order value | Rs 50,000 / 1174.30 | 42 |
| cash after the buffer | Rs 90,000 / 1174.30 | 76 |

21 shares = Rs 24,660.30. At the stop: 21 x 11.00 = Rs 231.00 + Rs 26.22
charges = Rs 257.22. It writes (trimmed):

```json
{"symbol": "RELIANCE", "exchange": "NSE", "series": "EQ", "security_id": "2885", "side": "buy",
 "qty": 21, "entry": 1174.3, "stop": 1163.3, "target": 1196.3, "tick": 0.1, "product": "INTRADAY",
 "order_type": "LIMIT", "flat_by": "15:05", "value": 24660.3, "max_loss": 231.0,
 "charges_at_stop": 26.22, "max_loss_all_in": 257.22, "reward_at_target": 462.0,
 "reward_risk": 2.0, "decided_by": "position_cap_pct",
 "dhan": {"endpoint": "POST /v2/super/orders", "body": {
   "transactionType": "BUY", "exchangeSegment": "NSE_EQ", "productType": "INTRADAY",
   "orderType": "LIMIT", "securityId": "2885", "quantity": 21, "price": 1174.3,
   "targetPrice": 1196.3, "stopLossPrice": 1163.3, "trailingJump": 0,
   "correlationId": "india-RELIANCE-20261029-1"}}}
```

With `max_loss_per_trade` 10 one share already risks Rs 11 + charges: no order.

## Angel One (`"broker": "angel"`)

The size is the same arithmetic and every broker-neutral field of the order
is identical to the Dhan one; only the broker block changes:

- The token (= NSE's security id, RELIANCE 2885), series and tick come from
  Angel's scrip master `OpenAPIScripMaster.json` (cached as
  `trader/cache/angel-nse-equity.json` for 20 hours; `--scrip-offline FILE`
  reads it from disk). Its tick is in paise in the file, rupees here. A
  `-BE` / `-BZ` row is trade-to-trade and refused like on Dhan.
- The order carries `"angel": {"route": "placeOrder", "body": ..., "stop_order": ...}`
  and no `dhan` block. Values are strings, as in Angel's SDK:
  - `body`: `variety` NORMAL, `tradingsymbol` RELIANCE-EQ, `symboltoken` 2885,
    BUY, `ordertype` LIMIT at the entry, `producttype` INTRADAY, `duration` DAY,
    `squareoff`/`stoploss` "0", the quantity.
  - `stop_order`: `variety` STOPLOSS, `ordertype` STOPLOSS_LIMIT, SELL,
    `triggerprice` = the stop, `price` = the stop minus `stop_limit_buffer_ticks`
    ticks (default 5), rounded down to the tick. For RELIANCE: trigger 1163.30,
    limit 1162.80. india-broker places it only after the entry fills.
- **No `--market` on Angel One**: its April 2026 notice prohibits market (and
  IOC) orders for algorithmic trading, so `--market` is refused. The target is
  not a resting order: india-broker watches it.
- A stop-limit sells no lower than its limit: a fill down there costs qty x
  buffer more (Rs 10.50 for 21 RELIANCE); in a gap below the limit it may not
  fill at all. Say this when you show the size.
- Angel One's own intraday auto square-off time is not verified here: flat-by
  stays 15:05.

## What is different from the US pack

Rupees and IST; charges in the max loss; Dhan's instrument file for the
security_id, series and tick; a Dhan super order (one request carries the
entry, target and stop-loss legs) instead of a bracket; LIMIT by default;
`dhanClientId` is never written to disk (the broker adds it from the shell
at send time). The long-term weights / 13F split of the US pack is dropped:
this pack is intraday only.

## Rules

- Proposals only. This skill never talks to the broker.
- Never size money the user has not said is for this. Ask for capital and max loss.
- Every number comes from the config, the plan, the prices or Dhan's instrument file.
- No projections: a target is a plan, not a forecast.
- Yahoo prices are unofficial and can lag; Dhan's price is the one that fills.

## Safety

- Not financial advice. A size is arithmetic, not a recommendation.
- Paper by default (`"mode": "paper"`). Going live is the user's own edit to their config.
- Nothing in this pack places an order except india-broker, and only after india-risk passed that
  exact order today, the user typed an explicit "yes" for it, and they allowed the command on
  Claude Code's own permission prompt.
- No leverage, F&O or short selling unless the config allows it and the user confirms.
- Dhan keys live in the shell environment, Angel One keys in the trading folder's `.env`;
  never in the chat.
