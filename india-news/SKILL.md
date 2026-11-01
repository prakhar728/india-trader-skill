---
name: india-news
description: >-
  News Monitor for NSE stocks. Tracks what moves the Indian stocks you watch
  and flags it: NSE corporate announcements (Regulation 30 filings: results,
  board meeting outcomes, orders won, acquisitions, pledges, SEBI or tax
  orders, management changes, credit ratings, fund raising), upcoming board
  meetings ("results this week"), ex-dates for dividends, splits, bonuses and
  buybacks, results filings, Yahoo Finance headlines per stock (.NS), and how
  far each stock is from its previous close. Shows only what is new since the
  last check, newest first, in IST, tags every item and never repeats one.
  Use when the user says "any news on RELIANCE", "why is TCS moving", "what's
  the news on my watchlist", "morning briefing", "pre-market news", "any
  results this week", "check NSE announcements", "corporate actions", "ex-date",
  "did anything break", "monitor the news", or after /india-scanner finds
  setups.
argument-hint: "[SYMBOLS]"
---

# india-news

A chart can look perfect and still be about to gap on a results day, an
ex-date or a SEBI order. Before a setup becomes a trade, this skill reads the
feeds that carry those first, for every stock on the watchlist, and tells you
only what is new. It also prints how far each stock has moved today, so a
filing and a move sit side by side, and lists the results dates and ex-dates
coming up this week.

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                          # watchlist + plan + paper positions
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers RELIANCE TCS --hours 12
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers INFY,HDFCBANK --all --json
python3 "${CLAUDE_SKILL_DIR}/news.py" --no-yahoo                # NSE filings only
```

Flags: `--hours 48` (ignore older items), `--days-ahead 7` (the results-date
and ex-date window), `--all` (show seen items too), `--no-nse`, `--no-yahoo`,
`--no-moves`, `--move-pct 3`, `--now "2026-10-29 15:40"` (IST, pins the
clock), `--dry-run` (leave the state alone), `--json`, `--offline DIR[,DIR]`.
Exit code 0 when something new was shown, 1 when nothing new, 2 when there
are no symbols.

## What it reads and writes

| file / feed | what |
| --- | --- |
| `--tickers`, else `trader/watchlist.json` | the scanner's hits (india-scanner); plain NSE symbols like `RELIANCE`, `M&M` |
| `trader/plan.json` | the trade you are planning (india-exit) |
| `trader/paper.json` | open paper positions (india-broker's local ledger) |
| NSE `Online_announcements.xml` | every corporate announcement, the whole market, one request |
| NSE `Board_Meetings.xml` | board meetings coming up, with the meeting date and purpose |
| NSE `Corporate_action.xml` | ex-dates and record dates: dividends, splits, bonuses, buybacks |
| NSE `Financial_Results.xml` | results filings (XBRL) |
| NSE `EQUITY_L.csv` | company names, to tie each filing to its symbol (cached in `trader/cache/`) |
| Yahoo Finance headline RSS | `https://feeds.finance.yahoo.com/rss/2.0/headline?s=RELIANCE.NS&region=IN&lang=en-IN` |
| Yahoo Finance chart | the last price against the previous close (unofficial, can lag) |
| `trader/state/news.json` | what has been shown (kept 30 days) |

The NSE feeds live on `nsearchives.nseindia.com/content/RSS/`: public files,
read with a plain GET and an honest User-Agent, one request per feed per
check (not per stock), a pause between calls. Never `www.nseindia.com/api`.

## How a filing finds its stock

NSE's feeds carry the company name, not the symbol. The tool matches:

1. the company name against NSE's EQUITY_L.csv (case, punctuation, "Limited"
   and "Ltd" ignored): "Reliance Industries Limited" is RELIANCE;
2. else the filing's file name prefix:
   `.../corporate/GKENERGY_29102026152857_...pdf` is GKENERGY;
3. else a symbol at the start of the text ("PSUBANK    : ...").

When the name and the file name disagree, the name wins. On Oct 29 2026 JSW
Steel's newspaper notice was filed as `Supriya_29102026...pdf` (SUPRIYA is a
different listed company), and Reliance's Regulation 30 disclosure as
`kavinavora_...pdf`. ETFs, mutual-fund NAV notices and debt-only issuers have
no EQ symbol and are left out.

## The tags

Every item lists the symbols it touches and its tags: `results`, `board
meeting`, `dividend`, `split`, `bonus`, `buyback`, `order win`,
`acquisition`, `pledge`, `regulatory` (SEBI, RBI, tax or other orders,
USFDA), `management change`, `rating` (credit ratings), `block/bulk deal`,
`fund raising` (QIP, preferential issue, NCDs), `analyst` (investor meets,
calls, upgrades), `insider` (SAST / PIT), `legal`, `risk` (default,
insolvency, fraud, fire, shutdown, suspension) and `macro`. NSE filings marked
`!` carry results, an acquisition, a pledge, a regulatory action, a
management change, a block or bulk deal, or a risk word. Yahoo headlines are
never marked `!`: they are leads.

**Coming up** lists, every check (not only when new), each watched stock's
board meeting for results within `--days-ahead` (7) days as `RESULTS THIS
WEEK`, other board meetings, and ex-dates (today, tomorrow, in N days).

## The rules

- **Headlines are leads, not facts.** Before telling the user something
  happened, open the filing (the PDF or XBRL link) or the story and read it.
  Quote the source and link it. Never summarise a story you could not read.
- **Filings beat headlines.** What the company filed with NSE is what the
  company said.
- **Yahoo's .NS feeds are thin and sometimes off.** On Oct 29 2026 the
  RELIANCE.NS and HDFCBANK.NS feeds were empty, and TCS.NS carried a story
  about the "Thoroughbred Championship Series". Say when a feed is empty, and
  ignore a story that is plainly about something else.
- **MOVING** marks a stock 3 % or more from its previous close
  (`--move-pct`). A move with no filing and no headline is worth saying out
  loud too.
- **Gap risk.** A results day or an ex-date inside the trade can jump past any
  stop. An intraday trade is flat by 15:05 (Dhan squares off at 15:10), but
  results often come out during market hours in India: say so.
- **No predictions.** Report what was filed or published and how far the price
  is from the previous close. Never say what the stock will do next.

## What is different for India

- NSE's own RSS feeds are the filing source (the US pack read its regulator's
  per-company feeds); one fetch covers the whole market.
- Times: NSE stamps items in IST as `29-Oct-2026 15:29:10`; Yahoo stamps UTC.
  Everything prints in IST (UTC+05:30).
- Board meetings stand in for an earnings calendar: Indian companies must tell
  the exchange the board meeting date before results.
- Corporate actions bring the ex-date: the price drops by the dividend that
  morning, which is not a breakdown.

## Steps

1. Run the check: `python3 "${CLAUDE_SKILL_DIR}/news.py"`. Exit code 1 means
   nothing new.
2. Read the items that matter first: every `!` filing, anything in Coming up
   for the stock being traded, every MOVING stock.
3. Write a short brief per stock: what happened, the source and link, the move,
   and one line on why it matters to the setup (for example "results on
   Tuesday: an overnight gap can jump past any stop; today's trade is in and
   out before 15:05").
4. If a filing changes the picture, say so before `/india-entry` and
   `/india-exit`. Never act on a headline alone.

During the session: `/loop 15m /india-news`, or a scheduled task each morning
around 08:45 IST before the pre-open.

## Example: Thursday 29 October 2026, after the close

```
python3 news.py --tickers RELIANCE INFY KAMATHOTEL SUPREMEIND --hours 24 --move-pct 2
RELIANCE    Rs 1,167.40 +0.81% vs previous close Rs 1,158.00 at 15:29 IST
INFY        Rs 1,146.50 +2.08% vs previous close Rs 1,123.15 at 15:29 IST  MOVING

Coming up (gap risk: a results day or an ex-date can jump past any stop):
  SUPREMEIND  ex-date Fri 2026-10-30 (tomorrow): INTERIM DIVIDEND - RS 12 PER SHARE
  KAMATHOTEL  RESULTS THIS WEEK: board meeting Tue 2026-11-03 (in 5 days): Financial Results

  2026-10-29 15:24 IST  INFY        Yahoo Finance   Winners of Qorus-Infosys Finacle Banking Innovation Awards 2026 Announced
! 2026-10-29 15:11 IST  KAMATHOTEL  NSE board mtg   Board meeting 03-Nov-2026: Financial Results
  2026-10-29 08:55 IST  RELIANCE    NSE filing      [Other Restructuring] Disclosure under Regulation 30 ...
```

The brief: "Infosys is up 2.1 % on the day with an awards press release and
nothing material filed. Kamat Hotels reports on Tuesday. Supreme Industries
goes ex-dividend (Rs 12) tomorrow, so tomorrow's open drops by about that much.
Reliance filed the voluntary strike-off of a Hamleys subsidiary: routine."

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, and never tells the user to buy or sell because of a
  headline or a filing.
- Never hammers a server: one request per NSE feed per check, one per stock on
  Yahoo, a pause between calls.

## Safety

Not financial advice: education and tooling. This skill only reads and
reports. Nothing places an order except `/india-broker`, after `/india-risk`
passes and the user types an explicit "yes" for that order.
