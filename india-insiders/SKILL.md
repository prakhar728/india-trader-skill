---
name: india-insiders
description: >-
  Who is trading in Indian stocks. Watches the insider-trading disclosures
  promoters, directors, KMPs and designated persons must file under SEBI's
  PIT Regulations (NSE's InsiderTrading RSS, each item an XBRL file) and
  NSE's daily bulk and block deals. Alerts once per new disclosure and turns
  each into a clean line: who, their category (promoter, promoter group,
  director, KMP...), buy / sell / pledge, quantity, rupee value and price,
  mode (market purchase, off market, ESOP, pledge creation, gift, inter-se
  transfer), holding before and after, trade dates and how many days it took
  to disclose, tagged PROMOTER OPEN-MARKET BUY when that is what it is and
  "noise" when it is ESOP, pledge or gift. Lists the day's bulk and block
  deals with filters, flags names you follow (well-known investors, funds,
  a promoter's holding company), and checks everything touching your
  watchlist or paper positions. Use when the user says "is the promoter
  buying", "insider trading in X", "who bought X", "did any promoter buy
  shares", "bulk deals today", "block deals", "what did <investor> buy",
  "follow <investor>", "alert me when insiders trade my stocks", "SAST / PIT
  disclosures", or "run the insider skill".
argument-hint: "[insider|deals|watch] [--symbol X]"
---

# india-insiders

India's version of tracking who is trading. Two public sources:

1. **SEBI PIT disclosures** (Prohibition of Insider Trading Regulations,
   Regulation 7(2)): promoters, promoter group, directors, KMPs and designated
   persons must tell the company within two trading days of trading more than
   Rs 10 lakh in a quarter, and the company must tell the exchange within two
   more. NSE lists them in an RSS feed; each item is an XBRL file.
2. **Bulk and block deals**: a bulk deal is a client trading more than 0.5% of
   a company's shares in a day; a block deal is a single trade of Rs 10 crore
   or more in the block-deal windows. NSE publishes the day's list after the
   close, with the client's name.

These are public disclosures, often filed days after the trade. There is no
inside information here, only public filings read quickly. They say what
someone already did, not what will happen next. Say that every time.

**What people look at.** Open-market buying by promoters (a market purchase,
with their own money, at the market price) is the signal people watch: it is
rare and costly to fake. Most rows are noise: ESOP allotments and sales by
employee trusts, pledge creation and release (a loan, not a trade),
inter-se transfers between promoter entities, gifts, off-market moves,
debentures and ADRs. The tool tags each row so the user sees which is which.

Not financial advice. Nothing in this pack places an order without the
user's typed "yes".

Tool in this folder (standard library Python, imports `../india-common/nse.py`):

```bash
python3 "${CLAUDE_SKILL_DIR}/insiders.py" --help
```

## What it reads and writes

| source | what |
| --- | --- |
| `https://nsearchives.nseindia.com/content/RSS/InsiderTrading.xml` | the current PIT feed: `SYMBOL|Company|Original|Regulation 7 (2)|IT_..._WebXMLFile_....xml|...` per item |
| `.../corporate/xbrl/IT_<id>_WebXMLFile_<time>.xml` | one disclosure (PIT V2.0, `in-bse-co` taxonomy): one context per person and trade |
| `.../content/RSS/Insider_Trading.xml` | the older feed (`in-capmkt` taxonomy, `IT_..._WEB.xml`); it stopped at May 2026, read with `--legacy` |
| `.../content/equities/bulk.csv`, `block.csv` | the latest day's bulk and block deals |
| `trader/state/insiders.json` | the files already read and the rows already shown, so each alerts once |
| `trader/config.json` | `follow_names`: client names to flag in deals |
| `trader/watchlist.json`, `trader/paper.json`, `trader/plan.json` | symbols for `watch` |

All plain GETs to NSE's archive hosts with an honest User-Agent, half a
second apart; never `www.nseindia.com/api`. Holding percentages: PIT V2.0
files carry a fraction (0.4556 means 45.56%), the older files a percentage;
the tool shows both as percent.

## Step 1: new insider disclosures

```bash
python3 "${CLAUDE_SKILL_DIR}/insiders.py" insider                                # new since the last check
python3 "${CLAUDE_SKILL_DIR}/insiders.py" insider --promoters-only --buys --open-market
python3 "${CLAUDE_SKILL_DIR}/insiders.py" insider --symbol INFY --all            # everything in the feed for INFY
python3 "${CLAUDE_SKILL_DIR}/insiders.py" insider --min-value 1000000            # Rs 10 lakh or more
```

Each disclosure row rings once (`--all` shows them again, `--dry-run`
records nothing). Exit code 1 means nothing new. The feed holds the latest
few dozen filings, so check every hour or two on weekdays (`/loop 1h`).

## Step 2: bulk and block deals

```bash
python3 "${CLAUDE_SKILL_DIR}/insiders.py" deals                                  # biggest first
python3 "${CLAUDE_SKILL_DIR}/insiders.py" deals --min-value 50000000 --buys      # Rs 5 crore or more
python3 "${CLAUDE_SKILL_DIR}/insiders.py" deals --follow "Serum Institute"       # saved in follow_names
python3 "${CLAUDE_SKILL_DIR}/insiders.py" deals --following                      # only followed names
python3 "${CLAUDE_SKILL_DIR}/insiders.py" deals --unfollow "Serum Institute"
```

A followed name matches anywhere in the client name, case-insensitive, and
its deal alerts once. The tool says when the file is not today's (NSE
replaces it after each close). Much of the bulk list is high-frequency
trading firms buying and selling the same stock the same day; a matched
BUY and SELL by one client is churn, not a position.

## Step 3: what touches my stocks

```bash
python3 "${CLAUDE_SKILL_DIR}/insiders.py" watch            # watchlist + paper positions + plan
python3 "${CLAUDE_SKILL_DIR}/insiders.py" watch --symbol STLTECH --symbol INFY
```

Shows every disclosure in the current feed and every deal in the latest
files for those symbols, marking the ones not seen before as NEW.

## Example (2026-10-29)

```
NEW INSIDER SKMEGGPROD (SKM Egg Products Export (India) Limited): SKM SHREE SHIVKUMAR, Promoter and
Director, BUY 10,000 equity for Rs 19,05,094 (Rs 190.51 each), market purchase on NSE, 2026-10-27;
holding 45.56% -> 45.58%; disclosed 2 days after the trade.
    [PROMOTER OPEN-MARKET BUY]
NEW INSIDER CAMLINFINE (Camlin Fine Sciences Limited): Ashish Subhash Dandekar, Promoter and Director,
PLEDGE 810,000 equity for Rs 6,78,37,500 ..., pledge creation, 2026-10-27 ...
    [noise: pledge]
NEW INSIDER INFY (Infosys Limited): Mayank Ranjan, Designated Person, SELL 4,000 any other instrument
(adrs) for Rs 47,38,714, market sale, 2026-10-26; disclosed 3 days after the trade.
    [not shares: Any other instrument (ADRs)]
```

And from the 28 October block deals, with `--follow "Serum Institute"`:
`BLOCK 2026-10-28 STLTECH SELL 601,900 @ 887.00 Rs 53,38,85,300 SERUM
INSTITUTE OF INDIA PRIVATE LIMITED <- FOLLOW`, matched by Poonawalla
Aviation buying the same 601,900 shares: a transfer between two
Poonawalla-group companies, not a vote on the stock.

Then: "These are public disclosures filed after the trade. A promoter buying
10,000 shares says what he did on 27 October, not what the price will do."

If the user wants to act on it, it goes the long way: a disclosure is days
old, so it is a reason to look at a chart, not a setup. /india-scanner and
/india-entry decide if there is a trade today; then /india-exit,
/india-budget, /india-risk, and /india-broker only after the user's "yes".

## What is different from the US version

The US original read Congress members' STOCK Act reports (PDFs, 30-45 days
late, amounts as ranges). India has no such list for lawmakers; the closest
public record of who is trading is SEBI's PIT disclosures (exact quantities
and rupee values, 2 to 4 days late) and the exchange's bulk / block deal
lists (next day, with client names). Both are machine-readable, so no PDF
reading and no manual route.

## What it never does

- Never says or implies anyone has inside information, or that copying a
  promoter or a famous investor beats the market. No return claims.
- Never treats a pledge, an ESOP or an inter-se transfer as a buy signal.
- Never places an order, never calls NSE's website APIs, never needs a login.

## Safety

- Not financial advice. Education and tooling only; nothing here is a
  recommendation to buy or sell anything.
- Paper mode by default (`"mode": "paper"`, a local ledger: Dhan has no paper
  trading).
- Nothing in this pack places an order except /india-broker, and only after
  /india-risk passes AND the user types an explicit "yes" for that order.
- Insider and deal data are PUBLIC disclosures, filed after the trade. This
  skill reads them fast; it has no inside information, and it says so.
