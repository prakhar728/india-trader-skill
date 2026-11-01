---
name: india-exit
description: >-
  Exit Planner for NSE day trades. Sets your stop and your target before you
  enter, so you know when to get out. Reads the entry from india-entry (or a
  price you give), puts the stop at the opening-range midpoint by default
  (or the range low, your price, a percent, or a multiple of the ATR),
  rounds it DOWN to the stock's NSE tick (Dhan's instrument file when it is
  cached, else NSE's price band: 0.01 below Rs 250, 0.05 to Rs 1,000, 0.10
  to Rs 5,000 ...), and sets the target at 2R (or 1R, or your price), also
  rounded down. Shows Dhan's round-trip intraday charges per share at the
  stop and at the target and the R after costs. Refuses a stop at or above
  the entry, a target at or below it, or a stop too tight to survive the
  spread, and says why. Flat by 15:05 IST (Dhan squares off at 15:10).
  Writes trader/plan.json for the Time Machine, the Budget Manager, Risk and
  Price Alerts. Long side only. Use when the user says "where's my stop",
  "set my stop and target", "plan the exit", "when do I get out", "stop
  loss", "what's my R", "risk reward", "how much are the charges", or right
  after /india-entry triggers, before /india-timemachine.
argument-hint: "[--stop or-mid|or-low|price:X|pct:X|atr:K] [--target 2R|1R|price:X]"
---

# india-exit

The Exit Planner decides how a trade ends before it starts. The stop is where
the setup has failed. The target is where you take what you came for. Both
are set BEFORE the entry, written down and handed on, so nobody has to decide
anything in the middle of a fast move. A stop is a plan, not a guarantee: a
gap, a circuit or a fast market can fill past it. Day trading loses most
people money, and a neat plan does not change that. In India the charges are
part of the plan: STT, exchange fees, GST and brokerage come off every round
trip, so the plan shows the R after costs too.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan                          # trader/entry.json: midpoint stop, 2R target
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop or-mid --target 1R --write
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop atr:1.5           # needs the session's bars (Yahoo or --offline)
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol RELIANCE --entry 1174.30 --or-high 1170.50 --or-low 1156.20
python3 "${CLAUDE_SKILL_DIR}/exit.py" show                          # the current trader/plan.json
```

Other flags: `--flat-by 14:45`, `--tick 0.05`, `--scrip FILE` (Dhan's
api-scrip-master.csv), `--qty N` (shares for the charges line), `--json`,
`--now "2026-10-29 09:41"` (IST), `--entry PRICE` with a waiting entry.json
to plan ahead, `--setup level`, `--offline FILE` (a saved Yahoo chart JSON
for an ATR stop).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/entry.json` | the trigger from india-entry: symbol, entry price, time, bar, opening range |
| `trader/config.json` | `flat_by` (15:05, never after 15:10), `min_reward_risk` (1.5: a note here, india-risk enforces it), `max_loss_per_trade` (500: the reference quantity for charges) |
| `trader/cache/dhan-nse-equity.json` | Dhan's instrument file as india-common caches it: the stock's real tick. Never downloaded here |
| Yahoo chart API | only for an `atr:K` stop: the session's bars up to the entry bar |
| `trader/plan.json` | with `--write`: entry, stop, target, tick, risk a share, R, stop and target rules, flat by, opening range, charges. india-timemachine, india-budget, india-risk and india-alerts read it |

## Stops and targets

| stop | where it goes |
| --- | --- |
| `or-mid` | the opening-range midpoint (the default for an opening-range breakout) |
| `or-low` | the opening-range low: wider, so fewer shares for the same max loss |
| `price:X` | your price |
| `pct:X` | X percent under the entry |
| `atr:K` | K times the 14-bar ATR under the entry, on the session's bars up to the entry bar. With fewer than 14 bars by then it averages all of them and says so |

A level setup has no opening range to lean on, so it needs a stop you name.

- **Ticks.** NSE prices move in ticks. The stop rounds DOWN to the tick
  (further from the entry, so the risk shown is never smaller than the real
  one). The target is entry + R x risk, also rounded DOWN to the tick, so
  it is a price the market can print and the R is never flattered. `2R` by
  default; `1R`, `1.5R` or `price:X` also work.
- **Refused, with the reason.** A stop at or above the entry. A target at or
  below it. A risk under the larger of 3 ticks and 0.05% of the price: too
  tight to survive the spread, normal wiggle would stop you out. An
  entry.json that has not triggered (unless you give `--entry`). A short:
  this pack plans longs only.
- **Charges.** Dhan's intraday rates (brokerage the lower of Rs 20 or 0.03%
  an order, STT 0.025% on the sell, exchange fees, SEBI fee, stamp duty
  0.003% on the buy, GST 18%) for one round trip, at the stop and at the
  target, for the shares your `max_loss_per_trade` would buy at this risk
  (or `--qty`). It prints them per share, and the R after costs:
  net R = ((exit - entry) x shares - charges) / (risk x shares).
- **Flat by.** 15:05 IST by default (or `flat_by` in the config), never
  later than Dhan's 15:10 auto square-off. Out by then, win or lose, no
  overnight hold: an intraday position left open is squared off by Dhan at
  its price, with a call-and-trade fee. An entry at or after the flat-by
  time is refused.

## Steps

1. Run it right after `/india-entry` triggers. Before that, `--entry PRICE`
   plans ahead at the level you expect.
2. Use the default stop for an opening-range breakout unless the user names
   another. A level setup needs one: ask.
3. Run `exit.py plan --write`. Read back the entry, the stop and its rule,
   the target and its rule, the risk a share, reward:risk, the charges and
   the R after costs, and flat by.
4. If it refuses, say the reason and the fix (a wider stop, a farther
   target). Never loosen anything on your own.
5. Say once that the stop is a plan, not a guarantee.
6. Hand off to `/india-timemachine` to replay the plan, then `/india-budget`
   to size it to the max loss per trade. Offer `/india-alerts` for the stop,
   the target and the flat-by time.

## Example: RELIANCE, Thursday 2026-10-29

The entry is Rs 1,174.30 at 09:40 IST (the 09:35 bar, bar 4) and the opening
range is Rs 1,156.20 to Rs 1,170.50. `exit.py plan --write`:

```
Exit plan: RELIANCE long (NSE), Thu 2026-10-29, opening-range breakout on 5-minute bars
  Entry         Rs 1,174.30 at 09:40 IST (bar 4)
  Stop          Rs 1,163.30  opening-range midpoint: (Rs 1,170.50 + Rs 1,156.20) / 2 = 1163.35, rounded down to the 0.1 tick
  Target        Rs 1,196.30  2R (two times the risk)
  Risk a share  Rs 11.00 (0.94% of the entry), tick 0.1
  Reward:risk   2.0
  Charges       round trip for 45 shares (max_loss_per_trade Rs 500.00 / risk 11): Rs 56.18 at the stop (Rs 1.25 a share), Rs 56.75 at the target (Rs 1.26 a share)
  After costs   target +1.89R, stop -1.11R, reward:risk 1.69
  Flat by       15:05 IST, win or lose: no overnight hold (Dhan squares off at 15:10)
```

A 1R target would be Rs 1,185.30. On a Rs 11.92 stock like IDEA the tick is
0.01: the same rule on its 2026-10-29 range (11.78 to 11.90) puts the stop at
11.84 and the 2R target at 12.08.

## Rules

- The stop and the target are set before the entry, never after.
- Never move a stop further from the entry once the trade is on.
- Plain numbers only: entry, stop, target, risk a share, reward:risk,
  charges. The charges line is costs, not a result.
- Nothing here promises winning trades or money. A 2R target is not a
  prediction that the price gets there.
- Long side only. This pack plans longs only.
- The tick from NSE's price band is a fallback: NSE sets each stock's tick
  monthly from its last month-end close. Near a band edge (Rs 250, Rs 1,000,
  Rs 5,000) say so, or pass `--tick` / `--scrip`.

## What it never does

It never places an order, never sizes a position (that is `/india-budget`),
never widens a stop to make a plan pass, and never calls a stop a guarantee.

## Safety

- Not financial advice. Education and tooling only; a plan is not a
  recommendation.
- Paper mode by default (`"mode": "paper"`, a local ledger: Dhan has no paper
  trading). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except india-broker, and only when
  india-risk passed that exact order today AND the user typed an explicit
  "yes" for that one order in the chat.
- Dhan keys live in environment variables, never in the chat or in files.
