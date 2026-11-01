---
name: india-broker
description: >-
  Order Executor for Dhan (DhanHQ API v2) and Angel One (SmartAPI). Sends
  approved intraday orders to your Dhan or Angel One account, or to a local
  paper ledger by default, never without your yes. Shows each order as a plain ticket (buy 21 RELIANCE at Rs
  1,174.30 limit, stop, target, max loss incl. charges, flat by 15:05) plus
  the exact Dhan super-order request, records your yes word for word, then
  places it with POST /v2/super/orders (live) or a simulated fill (paper,
  from Yahoo 1-minute bars). Watches paper positions for stop, target and
  flat-by; builds the sell that gets you out (cancels the super order's open
  legs or Angel's stop, sells the net quantity with a marketable LIMIT, never
  MARKET); Angel One trades go in two steps (LIMIT entry, then the
  exchange-held STOPLOSS_LIMIT once filled) and the target is watched; reads status, trades, positions,
  funds, your whitelisted static IP and token validity; handles expired
  tokens (DH-901), rejected orders (DH-905/906), rate limits (DH-904) and
  static-IP refusals; logs every approval, send, response, fill and the
  day's risk to trader/broker.json. Covers Dhan API setup (enable API on
  web.dhan.co, 24-hour token, static IP, SEBI retail-algo rules) and Angel
  One SmartAPI setup (TOTP, static IP, .env keys). Use when
  the user says "place the order", "buy it", "execute", "send it to Dhan",
  "send it to Angel", "connect Angel One", "protect my position", "place the stop",
  "close my position", "flatten", "get me out", "square off", "is my order
  filled", "my positions", "connect Dhan", "set up Dhan API", "paper trade",
  or after /india-risk passes.
argument-hint: "[order number]"
---

# india-broker

The Order Executor is the only skill in this pack that can place an order,
so it is the strictest. It speaks to two brokers: **Dhan** (`dhan.py`, the
default) and **Angel One** (`angel_broker.py`, when `trader/config.json` says
`"broker": "angel"`; see "Angel One" below). Both share `ticket_core.py`: the
same tickets, gates, yes rules, paper ledger and log. Unlike the US pack, where a broker's MCP server
placed the order, here `dhan.py` itself talks to Dhan's REST API, so the
safety lives in the script, in the order of the steps below, and in Claude
Code's permission prompt. An order reaches Dhan only when, every time, for
one order:

1. **india-risk passed this exact order today** (fingerprint match in
   `trader/risk.json`). An exit that only sells what you hold skips this: getting
   out is never blocked.
2. **The user typed an explicit yes for this one order**, and
   `dhan.py approve --yes "<their words, verbatim>"` recorded it. A yes is good
   for 10 minutes (`approval_minutes`), today only, for one send.
3. **Claude Code's own permission prompt** for the `dhan.py send` command was allowed.
4. **Live only**: `"mode": "live"` in `trader/config.json` AND `--live` on the
   command AND `DHAN_CLIENT_ID` / `DHAN_ACCESS_TOKEN` in the user's shell.

Not financial advice. Nothing is placed without the user's "yes".

`${CLAUDE_SKILL_DIR}` below is this skill's folder.

```bash
python3 "${CLAUDE_SKILL_DIR}/dhan.py" show                        # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/dhan.py" approve --index 1 --yes "yes buy 21 RELIANCE"
python3 "${CLAUDE_SKILL_DIR}/dhan.py" send --index 1              # paper (default)
python3 "${CLAUDE_SKILL_DIR}/dhan.py" send --index 1 --live       # live: real money
python3 "${CLAUDE_SKILL_DIR}/dhan.py" paper-check                 # paper fills, stop, target, flat-by
python3 "${CLAUDE_SKILL_DIR}/dhan.py" flatten --symbol RELIANCE   # the exit ticket
python3 "${CLAUDE_SKILL_DIR}/dhan.py" status [--order-id ID] | trades | positions | funds [--dhan]
python3 "${CLAUDE_SKILL_DIR}/dhan.py" cancel --order-id ID [--leg ENTRY_LEG] --live
python3 "${CLAUDE_SKILL_DIR}/dhan.py" ip | profile                # static IP, token validity, Data plan
python3 "${CLAUDE_SKILL_DIR}/dhan.py" decline --index 1 | log [--all]
```

`--now "2026-10-29 09:41"` pins the clock (IST). Paper fills: `--bars FILE`
(Yahoo chart JSON), `--price RELIANCE=1170.5`, or `--offline` (no Yahoo fetch).

## Never add `dhan.py send` (or Angel's order commands) to an allow-list

Claude Code asks before it runs a Bash command that is not allowed. That
prompt is gate 3. **Never** add `dhan.py send`, `dhan.py cancel`, or a
wildcard that covers them (such as `Bash(python3:*)`) to `permissions.allow`
in any `.claude/settings*.json`, and never suggest it. The same for Angel One:
**never** allow-list `angel_broker.py send`, `protect`, `flatten`, `cancel`,
or `check` / `watch` (they can sell when the user pre-approved an exit). If the
user wants it explicit, add an ask rule to `.claude/settings.json`:

```json
{"permissions": {"ask": ["Bash(python3 *dhan.py send*)", "Bash(python3 *dhan.py cancel*)",
  "Bash(python3 *angel_broker.py send*)", "Bash(python3 *angel_broker.py protect*)",
  "Bash(python3 *angel_broker.py flatten*)", "Bash(python3 *angel_broker.py cancel*)",
  "Bash(python3 *angel_broker.py check*)", "Bash(python3 *angel_broker.py watch*)"]}}
```

`show`, `approve`, `paper-check`, `status`, `positions`, `funds`, `ip`,
`profile` and `log` are fine to allow; they place nothing.

## Modes

- **paper** (default): nothing goes to Dhan. `trader/paper.json` is a local
  ledger. `send` records the order and fills it from the latest 1-minute bars
  (Yahoo) or a fresh `trader/prices.json` price; `paper-check` walks the bars
  since: a LIMIT buy fills when a bar trades at or below it; then the stop
  (at the stop, or the bar's open if it gapped through), the target, or flat-by
  (the open of the first bar at flat-by). A bar that touches both stop and
  target counts as the stop. Charges come off every close.
- **live**: real orders on the user's Dhan account. Needs all three: the config,
  the flag, the keys.
- **Dhan's sandbox is not paper trading.** Dhan has none (SEBI does not let
  brokers run paper accounts). Its sandbox is an integration-test box where
  you test the plumbing (auth, payloads, responses), not a market. Use it with
  `export DHAN_BASE_URL=https://sandbox.dhan.co/v2` and a sandbox token from
  Dhan Cloud (cloud.dhanhq.co), never your live token, plus `mode: live` and
  `--live`. Super orders are not sandbox-enabled (docs.dhanhq.co, Sandbox), so
  `send` posts the entry there as a plain LIMIT order to `/orders` and says so;
  the stop and target legs are only exercised on the live API.
  Unset `DHAN_BASE_URL` to go back to `https://api.dhan.co/v2`.

## Setup (live)

1. A Dhan trading account with the equity segment active.
2. web.dhan.co > My Profile > **Access DhanHQ APIs**: enable API access and
   generate an **access token**. It is valid for 24 hours: generate a new one
   each trading day.
3. **Whitelist a static IP** in the same place (or `POST /v2/ip/setIP`). Order
   APIs (place, modify, cancel) only accept requests from it; reads do not
   need it. A changed IP is locked for 7 days. A home broadband IP usually is
   not static: use a static-IP service or a small cloud machine.
4. In the user's own shell (never in the chat, never in a file Claude reads):
   `export DHAN_CLIENT_ID=... DHAN_ACCESS_TOKEN=...`. If the user pastes a token in
   the chat, tell them to regenerate it on web.dhan.co.
5. Check: `dhan.py profile` (token validity, Data plan), `dhan.py ip`
   (registered IPs), `dhan.py funds`.
6. Only then, and only if the user asks, they edit `"mode": "live"` themselves.

**SEBI retail-algo context.** Since 2026-04-01 orders through a broker API
must come from a registered static IP, with the 24-hour token; the exchange
algo-ID tagging is done by Dhan. Dhan's order APIs allow 10 orders a second
(250 a minute, 1000 an hour, 7000 a day); `dhan.py` paces order calls and
never retries a send by itself.

## Steps: the entry

1. `dhan.py show`. Only a READY ticket goes to the user. NOT READY or BLOCKED
   goes back to `/india-risk`.
2. Show ONE ticket and ask: "Place this order? Reply yes or no." One order per
   question; never offer "yes to all".
3. `dhan.py approve --index N --yes "<the user's reply, verbatim>"`. It must
   start with "yes"; it may say just "yes" to the ticket just shown, or name the
   stock and the quantity. "sure", "ok", "yes to all", a yes with a doubt, a
   different stock or a different quantity is "Not recorded": ask again. Never
   turn a reply into a yes yourself.
4. `dhan.py send --index N` (add `--live` in live mode). Claude Code's prompt
   appears; the user allows it. Live: `POST /v2/super/orders` with the body from
   the ticket plus `dhanClientId` from the shell.
5. Report what came back: Dhan's orderId and orderStatus, or the paper fill.

## Steps: while open, and out by flat-by

- Paper: `dhan.py paper-check` every few minutes (india-alerts can ring at the
  stop, target and flat-by). Live: the super order's target and stop-loss legs
  do the work; `dhan.py status` shows them.
- By flat-by (15:05; Dhan squares off intraday at 15:10 anyway):
  1. `dhan.py flatten --symbol RELIANCE` writes a SELL **LIMIT** INTRADAY ticket for
     the held quantity (paper ledger, or Dhan `GET /v2/positions` in live mode) at a
     marketable price: the last price minus `exit_limit_ticks` (5) ticks, rounded
     down to the tick, never under the stop-limit floor (the plan's stop minus
     `stop_limit_buffer_ticks`). The last price: `--price RELIANCE=P`, else Dhan's
     LTP (live, needs the Data plan), else Yahoo 1-minute bars or a fresh
     `trader/prices.json`. `--limit P` sets it yourself. No MARKET orders: brokers
     apply market-price protection to API market orders, and the pack sends none.
  2. `/india-risk` (an exit always passes the day limits), `dhan.py show`, the
     user's yes, `dhan.py approve`, `dhan.py send [--live]`.
  3. Live send for an exit first cancels the stock's open super-order legs
     (`DELETE /v2/super/orders/{id}/ENTRY_LEG` if the entry is still pending,
     which cancels all legs; else the TARGET_LEG and STOP_LOSS_LEG), reads
     `GET /v2/positions` again, then `POST /v2/orders` SELL LIMIT (the ticket's
     price) for the net quantity. A limit can sit unfilled if the price falls
     through it: check `dhan.py status`, and flatten again for a fresh limit.
- Emergency: Dhan's `DELETE /v2/positions` exits all positions and cancels all
  orders, and the kill switch (`POST /v2/killswitch`) stops trading for the
  day. `dhan.py` does not call them; the user can, or uses the Dhan app.

## Errors

| Dhan says | what `dhan.py` tells the user |
| --- | --- |
| DH-901 / HTTP 401 | the token expired (24 h): regenerate on web.dhan.co, `export DHAN_ACCESS_TOKEN=...` in the shell |
| DH-904 / HTTP 429 | rate limit: wait; nothing is retried |
| DH-905, DH-906 | Dhan's own message; the order was NOT placed |
| static IP refusal | run `dhan.py ip`, whitelist on web.dhan.co |
| no answer (timeout) | logged as UNKNOWN: check `dhan.py status` first; the ticket stays blocked |

A failed order is not retried without a new risk check and a new yes.

## Dhan endpoints used

Verified against dhanhq.co/docs/v2 on 2026-10-29: `POST /super/orders` (body
fields as written by india-budget; response `orderId`, `orderStatus`),
`GET /super/orders` (with `legDetails`), `DELETE /super/orders/{id}/{leg}`,
`POST /orders` (SELL LIMIT, `validity` DAY), `GET /orders/{id}`,
`DELETE /orders/{id}`, `GET /trades`, `GET /positions` (`netQty`,
`productType`, `realizedProfit`), `GET /fundlimit` (`availabelBalance`, Dhan's
spelling), `GET /ip/getIP`, `GET /profile` (`tokenValidity`, `dataPlan`).
Headers: `access-token`, `client-id`, JSON. Not verified: the exact text of a
static-IP refusal, and
whether a TRADED super order's legs must be cancelled before a separate sell
(we cancel them to be safe).

## Angel One (SmartAPI)

Set `"broker": "angel"` in `trader/config.json`; india-budget then writes an
`angel` block (no `dhan` block) and india-risk checks it. `angel_broker.py`
has the same gates as `dhan.py`: a same-day risk PASS for the exact order,
the user's verbatim yes (10 minutes, one send), Claude Code's prompt, and for
live `"mode": "live"` + `--live` + the keys. Paper mode uses the same local
ledger (`paper-check` / `check` simulate the stop, target and flat-by).

```bash
python3 "${CLAUDE_SKILL_DIR}/angel_broker.py" show
python3 "${CLAUDE_SKILL_DIR}/angel_broker.py" approve --index 1 --yes "yes" [--exit-yes "yes, exit at the target or flat-by"]
python3 "${CLAUDE_SKILL_DIR}/angel_broker.py" send --index 1 [--live]       # the LIMIT entry
python3 "${CLAUDE_SKILL_DIR}/angel_broker.py" status [--angel]              # says when the entry has filled
python3 "${CLAUDE_SKILL_DIR}/angel_broker.py" protect --symbol RELIANCE --live   # the exchange-held stop
python3 "${CLAUDE_SKILL_DIR}/angel_broker.py" check [--live] | watch [--every 30] [--live]
python3 "${CLAUDE_SKILL_DIR}/angel_broker.py" flatten --symbol RELIANCE [--price RELIANCE=P | --limit P]
python3 "${CLAUDE_SKILL_DIR}/angel_broker.py" orders | trades | positions | funds | profile | ip
python3 "${CLAUDE_SKILL_DIR}/angel_broker.py" cancel --order-id ID [--variety STOPLOSS] --live
python3 "${CLAUDE_SKILL_DIR}/angel_broker.py" paper-check | decline --index 1 | log [--all]
```

### Setup (live)

1. On smartapi.angelbroking.com create a SmartAPI app (trading API) and
   register its **static IP**: Angel One accepts order calls only from it
   (reads work from anywhere). Put the same IP in `trader/config.json` as
   `"static_ip"`; `angel_broker.py ip` compares it with this machine's public
   IP (the SDK has no call to read the registered IP back).
2. Enable **TOTP** for the account on SmartAPI and keep the base32 secret
   it shows.
3. In the trading folder (next to `trader/`), the user writes a `.env`
   themselves, never in the chat and never shown to Claude:
   `ANGEL_API_KEY=...`, `ANGEL_CLIENT_CODE=...`, `ANGEL_PIN=...`,
   `ANGEL_TOTP_SECRET=...`. If the user pastes any of these in the chat, tell
   them to rotate it (a new API key, a new TOTP secret, a new PIN).
4. Check: `angel_broker.py profile` (logs in with a fresh TOTP; shows only the
   last 4 of the client code, exchanges, products), `angel_broker.py funds`,
   `angel_broker.py ip`.
5. Each command logs in with a fresh TOTP; the session token stays in memory
   and is never printed or written. A session ends at midnight anyway.

### Rules Angel One sets (April 2026 notice for algorithmic trading)

- **No market orders and no IOC orders.** Entries are LIMIT, the stop is a
  STOPLOSS_LIMIT, exits are a marketable LIMIT (last price minus a few ticks,
  never under the stop-limit floor). india-risk blocks MARKET, STOPLOSS_MARKET,
  IOC and ROBO in an `angel` block; the script refuses them again before sending.
- Orders only from the registered static IP; at most 9-10 orders a second
  (`angel_broker.py` paces them and never retries a send by itself).
- **ROBO (bracket) orders are not used**: not confirmed for NSE intraday via the
  API in 2026 (forum reports of rejections after 31 March 2026).
- Angel One's own intraday auto square-off time is not verified here: the
  pack keeps flat-by 15:05.

### Steps (live): two orders, then the watch

There is no one-cancels-other order to lean on, so:

1. `angel_broker.py show`, ONE ticket, ask "Place this order? Reply yes or no."
   Then ask separately: "When it reaches the target, or at flat-by 15:05, may I
   sell without asking you again? (default: I ask)". Record:
   `approve --index N --yes "<reply>"` and, only if they said yes to the
   second question, `--exit-yes "<that reply, verbatim>"` (it must start with
   yes and say exit / sell / close). The choice is stored in the approval;
   without it every exit asks.
2. `angel_broker.py send --index N --live`: the NORMAL LIMIT INTRADAY buy
   (`placeOrder`). There is **no stop yet**.
3. `angel_broker.py status` until it says **FILLED, NOT PROTECTED**, then at
   once `angel_broker.py protect --symbol RELIANCE --live`: the STOPLOSS /
   STOPLOSS_LIMIT SELL for the filled quantity (trigger = the stop, limit
   `stop_limit_buffer_ticks` ticks under it), held by the exchange. A partial
   fill: protect again after more fills. `protect` never places a second stop
   for shares that already have one.
4. `angel_broker.py check --live` (one pass) or `watch --live` (every 30 s):
   reads the last price from Angel's quote. At the target or at flat-by it
   cancels the stop, confirms the cancel in the order book, reads the net
   intraday quantity, and sells it with a marketable LIMIT, if the exit was
   pre-approved. Otherwise it writes the exit ticket and says so: run
   india-risk, `show`, ask yes, `approve`, `send --live`.
5. Any time: `angel_broker.py flatten --symbol RELIANCE` writes the exit
   ticket (live: net quantity from `getPosition`, price from Angel's quote);
   its send cancels the stock's open stop orders first. Getting out is never
   blocked by the risk limits; it still needs the user's yes.

**When the exit sell does not fill** (the price fell through the limit after
the stop was cancelled), `angel_broker.py` does not walk away: it polls the
sell every 2 s for about 10 s; if it is not filled it re-prices what is left
(`modifyOrder`, never more than the net open quantity from `getPosition`) at a
fresh last price minus 10, 15, 20 ... ticks, up to `exit_reprice_max` (5)
times. Still not filled: if the price is above the original stop it cancels
the sell (confirmed) and **re-places the STOPLOSS_LIMIT** for the shares still
held; if the price is at or under the stop it leaves the sell working. Either
way it prints a WARNING IN CAPITALS telling the user to act in the Angel One
app. If the stop's cancel was never confirmed, nothing was sold and the stop is
still working. `dhan.py` follows a flatten the same way (poll, then re-price
with `PUT /v2/orders/{id}`); it does not re-place a stop (a plain Dhan stop
order body is not verified here), so it leaves the sell working and warns.
`modifyOrder`'s body fields and the meaning of a modified quantity after a
partial fill are from the brokers' docs, not verified.

**Why the target is not a resting sell next to the stop.** Both would be
INTRADAY sells of the same shares. If the price touched the target and then
fell to the stop (or both filled in a fast move), both would fill and the
account would be short the stock. So only the stop rests at the exchange; the
target is watched, and the stop is cancelled (and the cancel confirmed) before
the target sell goes in. If a cancel is not confirmed, nothing is sold.

### Angel One routes used

From the official SDK (smartapi-python, `smartConnect.py`): `loginByPassword`
(client code, PIN, TOTP), `getProfile`, `getRMS`, `placeOrder` (answers
`orderid`, `uniqueorderid`), `cancelOrder` (variety + orderid),
`getOrderBook`, `getTradeBook`, `getPosition`, `market/v1/quote` (LTP mode),
headers `X-PrivateKey`, `X-UserType`, `X-SourceID`, `X-ClientLocalIP`,
`X-ClientPublicIP`, `X-MACAddress`, `Authorization: Bearer`. Not verified (from
Angel's docs, not the SDK): the order-book and position field names
(`status`/`orderstatus`, `filledshares`, `averageprice`, `netqty`, `realised`),
the status words (`complete`, `trigger pending`, `cancelled`, `rejected`), the
quote's `fetched[].symbolToken/ltp`, Angel's error codes, and whether a TOTP can
be reused for two logins inside one 30-second window.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | proposals (india-budget; `flatten` replaces it) |
| `trader/risk.json` | india-risk's verdicts; an entry needs a PASS from today |
| `trader/broker.json` | every shown ticket, approval (verbatim, with the exit choice for Angel), send (body without client id), response, fill, close, positions snapshots, each day's risk used, and (Angel) the managed trades: entry id, stops, exits |
| `trader/paper.json` | the paper ledger: cash, orders, fills, exits, charges, P&L |
| `trader/prices.json` | paper fills when there are no bars |

## What it never does

- Never sends without all the gates, never two orders on one yes, never from a
  scheduled or background task (nobody is there to say yes).
- Never asks for, prints, stores or repeats the token; `broker.json` refuses to
  save if it would contain it, and `dhanClientId` is never written to disk.
- Never switches the config to live. The user does that.
- Never promises a fill: a stop fills at whatever the market gives, a stop-limit or a
  marketable limit can miss in a fast fall.
- Never sends a MARKET or IOC order through Angel One (prohibited for algos), and no
  MARKET exit through Dhan either.

## Safety

- Not financial advice. A PASS is a rules check, not a recommendation.
- Paper by default. Live is the user's own decision, in their own config, with `--live`.
- Nothing is placed without the user's "yes", recorded word for word, and Claude Code's own
  permission prompt for `dhan.py send`.
- No leverage, F&O or short selling unless the config allows it and the user confirms.
- Dhan keys live in the shell environment; Angel One keys in the trading folder's `.env`;
  never in the chat, never printed, never in `broker.json`.
