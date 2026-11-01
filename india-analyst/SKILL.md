---
name: india-analyst
description: >-
  The Results Analyst for NSE stocks. Reads a company's quarterly results
  straight from the XBRL file it files with NSE (SEBI's integrated filing,
  Ind-AS, NBFC, bank and non-Ind-AS formats, and the older in-bse-fin
  results XBRL): revenue from operations, total income, expenses, profit
  before tax, net profit and the owners' share for consolidated results,
  basic and diluted EPS, standalone vs consolidated, the period and the
  fiscal quarter (Q2 FY27). Watches NSE's results feeds for new filings
  (optionally only your watchlist), keeps every quarter it reads in
  trader/results/SYMBOL.json and compares quarter on quarter and year on
  year once it has the quarters, saying plainly when it does not yet. For
  banks it adds net interest income, provisions and gross / net NPA. Reads an
  earnings call (concall) transcript when you paste one. Use when the user
  says "how were Hero's results", "Q2 results", "analyse the quarterly
  numbers", "what changed this quarter", "is revenue growing", "did profit
  fall", "consolidated vs standalone", "which companies declared results
  today", "results on my watchlist", "sum up the concall", or before trading
  a stock around its results day.
argument-hint: "[symbol] [--xbrl link]"
---

# india-analyst

Results days move Indian stocks more than anything else on the calendar,
and the press release puts the best number first. This skill reads the
numbers the company actually filed with NSE, in machine-readable XBRL, lines
them up quarter by quarter and says plainly what changed. For a day trader it
answers two questions before a trade: what did the last results say, and is
the next results day (or board meeting) inside this trade?

Tool in this folder (standard library Python, no keys, imports
`../india-common/nse.py`):

```bash
python3 "${CLAUDE_SKILL_DIR}/results.py" --help
```

## Where the numbers come from

All plain GETs to `nsearchives.nseindia.com` / `archives.nseindia.com` with an
honest User-Agent, half a second apart. Never `www.nseindia.com/api`.

| feed | what it lists |
| --- | --- |
| `content/RSS/Integrated_Filing_Financials.xml` | results filed under SEBI's integrated filing: each item links to the XBRL (`INTEGRATED_FILING_INDAS_...`, `_BANKING_`, `_NBFC_INDAS_`, `_NONINDAS_`) |
| `content/RSS/Integrated_Filing.xml` | the same plus governance filings (skipped); its XBRL links miss `/xbrl/` and the tool fixes them |
| `content/RSS/Financial_Results.xml` | the older results feed (`INDAS_...xml`, `in-bse-fin` taxonomy); it stopped updating in 2026 |
| `content/RSS/Online_announcements.xml` | "has submitted to the Exchange, the financial results" board-meeting outcomes (PDF), often an hour before the XBRL |

Inside a filing: context `OneD` is the reported period (a quarter, or a half
year for companies that report half-yearly), `FourD` the year to date (the
same three months in a Q1 filing, six months in Q2, nine in Q3, the whole
year in a Q4 filing). The dates come from `DateOfStartOfReportingPeriod` /
`DateOfEndOfReportingPeriod` in each context (in the older files the context
dates can be wrong). Amounts are in rupees whatever `LevelOfRounding` says;
the tool shows Rs crore. `NatureOfReportStandaloneConsolidated` says which
basis it is: companies with subsidiaries file two XBRLs, one each.

| line | Ind-AS / NBFC tag | bank tag |
| --- | --- | --- |
| revenue | `RevenueFromOperations` | `InterestEarned` (shown as "interest earned") |
| total income | `Income` | `Income` |
| expenses | `Expenses` | `ExpenditureExcludingProvisionsAndContingencies` |
| profit before tax | `ProfitBeforeTax` | `ProfitLossFromOrdinaryActivitiesBeforeTax` |
| net profit | `ProfitLossForPeriod` | `ProfitLossForThePeriod` |
| owners' share (consolidated) | `ProfitOrLossAttributableToOwnersOfParent` | `ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates` |
| basic / diluted EPS | `...EarningsLossPerShareFromContinuingAndDiscontinuedOperations` | `...EarningsPerShareAfterExtraordinaryItems` |

Banks also get net interest income, operating profit before provisions,
provisions, gross and net NPA %. Insurers file a premium-based format the
tool does not read: it says so and gives the link, so read the PDF.

## The store: trader/results/SYMBOL.json

A filing does NOT carry last year's comparison columns (the PDF does, the
XBRL does not). So every filing the tool reads is kept in
`trader/results/<SYMBOL>.json`, one row per basis and period, and the
comparisons grow as the store does:

- **Quarter on quarter**: from a Q2 filing alone, because the half-year to
  date minus the quarter is the quarter before. That quarter is marked
  `derived`; its EPS is approximate.
- **Year on year**: only once last year's same quarter is in the store. Until
  then the tool says "No year-earlier quarter in the store yet" and how to add
  it (`show SYMBOL --xbrl <link or file>` with last year's filing, or simply
  run `latest --save` every results season).
- A later filing for the same period (a revision) replaces the earlier one; a
  filed quarter always beats a derived one.

`trader/state/results.json` remembers which feed items were shown, so
`latest` shows each new filing once. `--metrics FILE` merges one row per
company into a JSON file; `metrics.example.json` here is a real run on
2026-10-29 for Hero MotoCorp, GMR Airports, ESAF Small Finance Bank, Emkay
Global (NBFC) and Monolithisch India (non-Ind-AS, half-yearly).

## Steps

1. New results, all or just the user's symbols:

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/results.py" latest                       # list what is new
   python3 "${CLAUDE_SKILL_DIR}/results.py" latest --watchlist --save    # watchlist + paper ledger symbols
   python3 "${CLAUDE_SKILL_DIR}/results.py" latest --symbol HEROMOTOCO --save
   ```

   Company names in the feeds are matched to symbols through NSE's
   `EQUITY_L.csv` (cached in `trader/cache/`). `--save` reads each XBRL (at
   most `--limit`, default 10) and stores it. Exit code 1 means nothing new.
2. One company, from the store or from a filing:

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/results.py" show HEROMOTOCO
   python3 "${CLAUDE_SKILL_DIR}/results.py" show HEROMOTOCO --xbrl https://nsearchives.nseindia.com/corporate/xbrl/INTEGRATED_FILING_INDAS_1748300_28102026090830_WEB.xml
   python3 "${CLAUDE_SKILL_DIR}/results.py" show GMRAIRPORT --basis standalone
   ```

   `--xbrl` takes a link or a saved file (repeat it for several),
   `--json` prints the store, `--no-save` reads without storing.
3. Write the summary: one line on the quarter (revenue, profit, EPS, growth
   when the store has it), one on margins, one on anything unusual (exceptional
   items, a big share of associates, owners' share far from net profit, a
   derived quarter, no year-earlier data yet), and the link. Plain words, real
   numbers, no adjectives the numbers do not support.
4. If the next results day falls inside the planned trade (an overnight hold,
   or results announced during market hours), say it before /india-exit sets
   the stop: a stop does not protect against a gap. Board-meeting dates are in
   NSE's `Board_Meetings.xml` feed and in /india-news.

## Concall transcripts

The tool does not fetch transcripts. Companies post the recording or
transcript link on NSE ("Analysts/Institutional Investor Meet/Con. Call
Updates") and on their investor-relations page. When the user pastes a
transcript (or a PDF), read it and sum up what management said changed:
guidance (volume, margin, capex), demand by segment, pricing, costs, anything
they declined to answer, and any number that disagrees with the filing. Quote
short phrases; say which figures came from the call and which from the XBRL.

## Example: Hero MotoCorp, Q2 FY27 (filed 2026-10-28)

```
HERO MOTOCORP LIMITED (HEROMOTOCO), consolidated results. Rs crore except EPS.
period     dates                       revenue  tot income         PBT  net profit      owners  EPS dil  rev QoQ  rev YoY
Q2 FY27    2026-07-01..2026-09-30    14,970.50   15,172.00    1,944.01    1,860.55    1,861.33    92.91   +14.0%        -
H1 FY27    2026-04-01..2026-09-30    28,096.85   28,758.09    3,908.62    3,278.48    3,273.69   163.44        -        -
Q1 FY27    2026-04-01..2026-06-30    13,126.35   13,586.09    1,964.61    1,417.93    1,412.36    70.53        -        -  derived
```

Then: "No year-earlier quarter (ending 2025-09-30) in the store yet, so no
year-on-year growth." GMR Airports the same day: consolidated net profit
Rs 65.38 cr but the owners' share is a loss of Rs 14.92 cr (minority
shareholders of subsidiaries take the difference), and Rs 54.84 cr of it is
the share of associates and joint ventures: the tool flags both.

## What is different from the US version

NSE's results XBRL instead of the US regulator's company-facts API: one
filing per basis per quarter, no comparison columns inside it, so a local
store builds the history. No fourth-quarter derivation is needed (Indian Q4
filings carry the quarter itself); the quarter derived here is Q1 from a Q2
filing. Rupees in crore, Indian fiscal year (April to March), standalone and
consolidated side by side, banks' NPA lines.

## What it never does

- Never says a stock is cheap, expensive, a buy or a sell. It reports what
  the company filed.
- Never invents a number the filing does not have, or a year-on-year change
  the store cannot support.
- Never calls NSE's website APIs or any host other than NSE's archives.

## Safety

Not financial advice: education and tooling only. This skill only reads
filings. Nothing places an order except /india-broker, after /india-risk
passes and the user types an explicit "yes" for that order.
