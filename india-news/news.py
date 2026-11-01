#!/usr/bin/env python3
"""News monitor for india-news. Standard library only.

Watches NSE corporate filings and Yahoo Finance headlines for the NSE stocks
you watch, and prints only what is new since the last check, newest first, in
IST, with how far each stock is from its previous close, the results dates
and ex-dates coming up, and tags on every item.

    python3 news.py                                   # watchlist + plan + paper positions
    python3 news.py --tickers RELIANCE TCS INFY       # or RELIANCE,TCS,INFY
    python3 news.py --tickers TCS --all --hours 72    # everything in the last 3 days, seen or not
    python3 news.py --no-yahoo                        # NSE filings only
    python3 news.py --no-moves                        # skip the price line per stock

Your symbols, when --tickers is not given: every hit in trader/watchlist.json
(india-scanner), the symbol in trader/plan.json (india-exit) and the open
positions in trader/paper.json (india-broker's paper ledger).

Sources (public, no keys, honest User-Agent, a pause between calls):
  - NSE RSS on nsearchives.nseindia.com/content/RSS/, one request per feed for
    the whole watchlist:
      Online_announcements.xml  every corporate announcement (Regulation 30)
      Board_Meetings.xml        board meetings coming up (results dates)
      Corporate_action.xml      ex-dates: dividends, splits, bonuses, buybacks
      Financial_Results.xml     results filings (XBRL)
    An item belongs to a symbol by the company name (matched against NSE's
    EQUITY_L.csv), else by the filing's file name prefix
    (.../corporate/GKENERGY_29102026...pdf -> GKENERGY). The name wins when
    they disagree: a law firm's or secretary's file name can sit in front.
  - Yahoo Finance headline RSS per symbol (.NS):
    https://feeds.finance.yahoo.com/rss/2.0/headline?s=RELIANCE.NS&region=IN&lang=en-IN
  - The move: Yahoo's chart endpoint (unofficial, can lag), the last price
    against the previous close.

NSE's feeds stamp items in IST ("29-Oct-2026 15:29:10"); Yahoo's in UTC
(RFC 822). Everything prints in IST. State: trader/state/news.json remembers
what you have seen (30 days), so an item shows once. Offline: --offline DIR
(comma-separate several) reads the four NSE feeds by their file names,
EQUITY_L.csv, yahoo-<SYMBOL>.xml and chart-<SYMBOL>.json.

Headlines are leads, not facts: open the filing before acting, and treat
nothing here as advice.
"""

import argparse
import datetime as _dt
import email.utils
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "india-common"))
import nse  # noqa: E402

RSS_BASE = "https://nsearchives.nseindia.com/content/RSS/"
NSE_FEEDS = [  # (kind, file name, source label)
    ("announcement", "Online_announcements.xml", "NSE filing"),
    ("board", "Board_Meetings.xml", "NSE board mtg"),
    ("action", "Corporate_action.xml", "NSE corp action"),
    ("results", "Financial_Results.xml", "NSE results"),
]
YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=IN&lang=en-IN"
KEEP_DAYS = 30
MOVE_PCT = 3.0      # a stock this far from the previous close is flagged as moving
DAYS_AHEAD = 7      # "this week": results dates and ex-dates within this many days

KEYWORDS = [
    ("results", r"financial results|\bresults?\b|\bearnings\b|\bQ[1-4]\s*(?:FY)?\s*\d{0,4}\b|\bquarter(?:ly)? "
                r"(?:ended|results|numbers)|\bnet profit\b|\bPAT\b|\bEBITDA\b|\bguidance\b"),
    ("board meeting", r"board meeting|meeting of the board|outcome of (?:the )?board"),
    ("dividend", r"\bdividend\b"),
    ("split", r"\bsplit\b|sub-?division"),
    ("bonus", r"\bbonus\b"),
    ("buyback", r"\bbuy-?back\b"),
    ("order win", r"\border(?:s)? (?:win|won|received|worth|inflow|book)|\b(?:bags?|bagged|secures?|secured|wins?|won)\b"
                  r"[^.|]{0,60}\b(?:orders?|contracts?)\b|letter of (?:award|intent)|\bLoA\b|award of (?:a )?contract|"
                  r"bagging/receiving of orders"),
    ("acquisition", r"\bacqui(?:re|res|red|ring|sition)\b|\bmerger\b|amalgamation|\btakeover\b|open offer|"
                    r"scheme of arrangement|\bdemerger\b|incorporation of|stake (?:in|purchase|acquisition)\b|"
                    r"[\d.]+ ?% stake"),
    ("pledge", r"\bpledge|encumbrance"),
    ("regulatory", r"\bSEBI\b|\bRBI\b|show[- ]cause|\bpenalt(?:y|ies)\b|\bGST\b|tax (?:demand|order)|"
                   r"actions? (?:initiated|taken)|orders? passed|\bNCLT\b|\bCCI\b|\bUSFDA\b|\bFDA\b|warning letter|"
                   r"\binspection\b|\bEIR\b|regulatory"),
    ("management change", r"\bCEO\b|\bCFO\b|\bMD\b|managing director|\bresign|\bappoint|cessation|change in "
                          r"(?:management|directors?|KMP|senior management)|key managerial|\bKMP\b|steps down"),
    ("rating", r"credit rating|\bCRISIL\b|\bICRA\b|CARE Ratings|India Ratings|Brickwork|"
               r"rating (?:upgrade|downgrade|reaffirm|revision|action)"),
    ("block/bulk deal", r"\bblock deals?\b|\bbulk deals?\b|offer for sale|\bOFS\b|stake sale"),
    ("fund raising", r"\bQIP\b|preferential (?:issue|allotment)|rights issue|fund[- ]?rais|\bNCDs?\b|"
                     r"\bdebentures?\b|allotment of (?:equity|shares|securities|warrants)|\bIPO\b"),
    ("analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|price target|target price|\boutperform|\bunderperform|"
                r"analysts?(?:/| or )institutional|investor meet|con\.? ?call|earnings call|\bbrokerage\b"),
    ("insider", r"\bSAST\b|insider trading|\bPIT\b|promoter (?:buy|sell|acqui|stake)"),
    ("legal", r"\blawsuit\b|\bcourt\b|\barbitration\b|\bjudgment\b|\bpetition\b|\blitigation\b|\bFIR\b"),
    ("risk", r"\bdefault|insolvency|\bIBC\b|\bCIRP\b|\bfraud|\braid\b|search and seizure|\bsuspen|delist|"
             r"cyber|ransomware|\bfire\b|(?:workers'?|labou?r) strike|\bstrike at\b|shutdown|natural calamity"),
    ("macro", r"\brepo rate\b|monetary policy|\bCPI\b|\binflation\b|\bGDP\b|\bFIIs?\b|\bFPIs?\b|\bNifty\b|"
              r"\bSensex\b|\bcrude\b|\brupee\b|\btariffs?\b"),
]
IMPORTANT = {"results", "acquisition", "pledge", "regulatory", "management change", "risk", "block/bulk deal"}


class NewsError(Exception):
    pass


# ---------------------------------------------------------------- time

MONTHS = "%d-%b-%Y %H:%M:%S"


def parse_time(value):
    """NSE '29-Oct-2026 15:29:10' (IST), RFC 822 (Yahoo, UTC) or ISO -> naive IST, or None."""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in (MONTHS, "%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return _dt.datetime.strptime(value, fmt)
        except ValueError:
            pass
    d = None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        return d.replace(microsecond=0)   # no zone: taken as IST
    return nse.ist_from_utc(d.astimezone(_dt.timezone.utc).replace(tzinfo=None)).replace(microsecond=0)


def parse_date(text):
    try:
        return _dt.datetime.strptime(text.strip(), "%d-%b-%Y").date()
    except (ValueError, AttributeError):
        return None


def fmt_ist(t):
    return t.strftime("%Y-%m-%d %H:%M IST") if t else "time unknown"


# ---------------------------------------------------------------- inputs

def load_json(path):
    try:
        return nse.read_json(path, None)
    except nse.NSEError:
        return None


def watch_from_files(watchlist=None, plan=None, paper=None):
    """{SYMBOL: name or None}: the scanner's hits, the planned trade and open paper positions."""
    base = nse.trader_dir()
    out = {}
    wl = load_json(watchlist or os.path.join(base, "watchlist.json")) or {}
    for h in wl.get("hits", []) if isinstance(wl, dict) else []:
        sym = h.get("symbol") or h.get("ticker")
        if sym:
            out[_clean(sym)] = h.get("name")
    pl = load_json(plan or os.path.join(base, "plan.json")) or {}
    if isinstance(pl, dict) and pl.get("symbol"):
        out.setdefault(_clean(pl["symbol"]), pl.get("name"))
    pp = load_json(paper or os.path.join(base, "paper.json")) or {}
    positions = pp.get("positions", []) if isinstance(pp, dict) else []
    if isinstance(positions, dict):
        positions = [dict(v, symbol=k) if isinstance(v, dict) else {"symbol": k} for k, v in positions.items()]
    for p in positions:
        if not isinstance(p, dict):
            continue
        sym = p.get("symbol") or p.get("ticker")
        qty = p.get("qty", p.get("quantity", 1))
        if sym and qty:
            out.setdefault(_clean(sym), p.get("name"))
    return out


def _clean(sym):
    try:
        return nse.clean_symbol(sym)
    except nse.NSEError:
        return str(sym).strip().upper()


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, offline=None, pause=nse.PAUSE):
        self.offline = [d for d in (offline or "").split(",") if d] if isinstance(offline, str) else offline
        self.pause = pause
        self._last = 0.0

    def find(self, name):
        for d in self.offline or []:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
        return None

    def get(self, url, name):
        if self.offline:
            path = self.find(name)
            if not path:
                raise NewsError("offline: no file %s" % name)
            return nse.read_maybe_gz(path)
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        try:
            return nse.http_get(url)
        except nse.NSEError as exc:
            raise NewsError(str(exc))


# ---------------------------------------------------------------- symbols from names

def norm_name(name):
    n = (name or "").lower().replace("&", " and ")
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    w = n.split()
    while w and w[-1] in ("limited", "ltd"):
        w.pop()
    if w and w[0] == "the":
        w = w[1:]
    return " ".join(w)


class Resolver(object):
    """Company name / file name -> NSE symbol, from EQUITY_L."""

    def __init__(self, equity):
        self.equity = equity or {}
        self.by_name = {}
        for sym, row in self.equity.items():
            self.by_name.setdefault(norm_name(row["name"]), sym)

    def name_of(self, sym):
        row = self.equity.get(sym)
        return row["name"] if row else None

    def symbol(self, company, link="", description=""):
        by_name = self.by_name.get(norm_name(company))
        m = re.search(r"/corporate/([A-Za-z0-9&\-]+?)_\d{8}", link or "")
        by_file = m.group(1).upper() if m and m.group(1).upper() in self.equity else None
        m = re.match(r"\s*([A-Z0-9&\-]{2,20})\s+:\s", description or "")
        by_desc = m.group(1) if m and m.group(1) in self.equity else None
        return by_name or by_file or by_desc


# ---------------------------------------------------------------- parsing

def _text(el, name):
    c = el.find(name)
    return " ".join((c.text or "").split()) if c is not None else ""


def _xml(data, what):
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("%s is not valid XML: %s" % (what, exc))


def _id(*parts):
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def parse_nse_feed(data, kind, resolver, watch=None):
    """One NSE RSS feed -> items for the watched symbols (all symbols when watch is None)."""
    label = dict((k, s) for k, _, s in NSE_FEEDS)[kind]
    root = _xml(data, "NSE's %s feed" % kind)
    out = []
    for it in root.iter("item"):
        title, link, desc = _text(it, "title"), _text(it, "link"), _text(it, "description")
        company = title
        extra = {}
        if kind == "action":
            m = re.match(r"(.*?)\s*-\s*Ex-Date:\s*(\S+)", title)
            if m:
                company, extra["ex_date"] = m.group(1), m.group(2)
        sym = resolver.symbol(company, link, desc)
        if sym is None or (watch is not None and sym not in watch):
            continue
        fields = dict((k.strip().upper(), v.strip()) for k, _, v in
                      (p.partition(":") for p in desc.split("|")) if _)
        if kind == "announcement":
            body, _, subject = desc.partition("|SUBJECT:")
            subject = subject.strip()
            headline = ("[%s] " % subject if subject else "") + body.strip()
            m = re.search(r"to be held on (\d{2}-[A-Za-z]{3}-\d{4})", desc)
            if m:
                extra["meeting_date"] = m.group(1)
        elif kind == "board":
            purpose = desc.split("|")[0].strip()
            md = fields.get("MEETING DATE") or ""
            m = re.search(r"(\d{2}-[A-Za-z]{3}-\d{4})", md or desc)
            extra["meeting_date"] = m.group(1) if m else None
            extra["purpose"] = purpose
            headline = "Board meeting %s: %s" % (extra["meeting_date"] or "(date not given)", purpose)
        elif kind == "action":
            purpose = fields.get("PURPOSE", desc)
            extra["purpose"] = purpose
            headline = "Ex-date %s: %s" % (extra.get("ex_date", "?"), purpose)
            if fields.get("RECORD DATE") and fields["RECORD DATE"] != "-":
                headline += " (record date %s)" % fields["RECORD DATE"]
        else:
            headline = "Results filed: %s, %s, %s, period ended %s" % (
                fields.get("RELATING TO", "?"), fields.get("AUDITED/UNAUDITED", "?"),
                fields.get("CONSOLIDATED/NON-CONSOLIDATED", "?"), fields.get("PERIOD ENDED", "?"))
        out.append(dict({"id": "nse:%s:%s" % (kind, _id(kind, title, link, desc)), "source": label, "kind": kind,
                         "ticker": sym, "company": company, "title": headline, "summary": desc, "link": link,
                         "time": parse_time(_text(it, "pubDate"))}, **extra))
    return out


def parse_yahoo_rss(data, ticker):
    root = _xml(data, "Yahoo's feed for %s" % ticker)
    out = []
    for it in root.iter("item"):
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        out.append({"id": "rss:" + guid, "source": "Yahoo Finance", "kind": "headline", "ticker": ticker,
                    "title": _text(it, "title"), "summary": _text(it, "description"), "link": link,
                    "time": parse_time(_text(it, "pubDate"))})
    return out


def parse_chart(data, ticker):
    """Yahoo chart JSON -> {"ticker", "price", "prev_close", "pct", "time"} or raise."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        meta = doc["chart"]["result"][0]["meta"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise NewsError("chart for %s is not usable" % ticker)
    price = meta.get("regularMarketPrice")
    prev = meta.get("previousClose") or meta.get("chartPreviousClose")
    if not price or not prev:
        raise NewsError("chart for %s has no price" % ticker)
    when = meta.get("regularMarketTime")
    return {"ticker": ticker, "price": round(float(price), 2), "prev_close": round(float(prev), 2),
            "pct": round((float(price) / float(prev) - 1) * 100, 2),
            "time": nse.ist_from_epoch(when) if when else None}


# ---------------------------------------------------------------- tagging

def mention_patterns(watch):
    pats = {}
    for t, name in watch.items():
        e = re.escape(t)
        alts = [r"\(%s\)" % e, r"\b(?:NSE|BSE)\s*:\s*%s\b" % e, r"\b%s\.(?:NS|BO)\b" % e]
        short = re.sub(r"\s+(?:limited|ltd\.?)$", "", (name or "").strip(), flags=re.I)
        if len(short) >= 4:
            alts.append(r"\b%s\b" % re.escape(short).replace(r"\ ", r"[\s-]"))
        pats[t] = re.compile("|".join(alts), re.I)
    return pats


def tag(items, watch):
    pats = mention_patterns(watch)
    for it in items:
        hay = "%s %s" % (it["title"], it.get("summary") or "")
        touches = {it["ticker"]} if it["ticker"] in watch else set()
        if it["kind"] == "headline":
            for t, rx in pats.items():
                if rx.search(hay):
                    touches.add(t)
        it["touches"] = sorted(touches)
        tags = [label for label, rx in KEYWORDS if re.search(rx, hay, re.I)]
        if it["kind"] == "results" and "results" not in tags:
            tags.insert(0, "results")
        if it["kind"] == "board" and "board meeting" not in tags:
            tags.insert(0, "board meeting")
        it["tags"] = tags
        it["important"] = bool(set(tags) & IMPORTANT) and it["kind"] != "headline"
    return items


def dedupe(items):
    """One item per story: same id, or same link, or same title the same day."""
    seen, out = {}, []
    for it in items:
        keys = [it["id"], it["link"] if it["kind"] == "headline" else "",
                "%s|%s|%s" % (it["kind"], re.sub(r"\W+", " ", it["title"].lower()).strip(),
                              it["time"].date() if it["time"] else "")]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1(it["id"].encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def collect(fetcher, watch, resolver, nse_feeds=True, yahoo=True, errors=None):
    errors = [] if errors is None else errors
    items = []
    if nse_feeds:
        for kind, fname, _ in NSE_FEEDS:
            try:
                items += parse_nse_feed(fetcher.get(RSS_BASE + fname, fname), kind, resolver, watch)
            except NewsError as exc:
                errors.append("NSE %s: %s" % (fname, exc))
    if yahoo:
        for t in sorted(watch):
            try:
                items += parse_yahoo_rss(fetcher.get(YAHOO % urllib.parse.quote(nse.yahoo_symbol(t)),
                                                     "yahoo-%s.xml" % t), t)
            except (NewsError, nse.NSEError) as exc:
                errors.append("Yahoo headlines for %s: %s" % (t, exc))
    return items


def collect_moves(fetcher, watch, errors=None):
    errors = [] if errors is None else errors
    out = []
    for t in sorted(watch):
        try:
            data = None
            last = None
            for host in nse.YAHOO_HOSTS:
                try:
                    data = fetcher.get(nse.yahoo_chart_url(t, "1d", "5m", host), "chart-%s.json" % t)
                    break
                except NewsError as exc:
                    last = exc
                    if fetcher.offline or not re.search(r"HTTP (429|5\d\d)", str(exc)):
                        raise
            if data is None:
                raise last
            out.append(parse_chart(data, t))
        except (NewsError, nse.NSEError) as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("price for %s: %s" % (t, exc))
    return out


def upcoming(items, watch, today, days_ahead=DAYS_AHEAD):
    """Results dates and ex-dates from today to today + days_ahead, one line per event."""
    events = {}
    for it in items:
        if it["ticker"] not in watch:
            continue
        if it.get("meeting_date"):
            d = parse_date(it["meeting_date"])
            hay = "%s %s" % (it.get("purpose") or "", it.get("summary") or "")
            what = "results" if re.search(r"result", hay, re.I) else "board meeting"
            key = (it["ticker"], "meeting", d)
        elif it.get("ex_date"):
            d = parse_date(it["ex_date"])
            what = "ex-date"
            key = (it["ticker"], "ex", d, it.get("purpose"))
        else:
            continue
        if d is None or d < today or (d - today).days > days_ahead:
            continue
        old = events.get(key)
        purpose = it.get("purpose") or ""
        if old and (old["what"] == "results", bool(old["purpose"])) >= (what == "results", bool(purpose)):
            continue
        events[key] = {"ticker": it["ticker"], "what": what, "date": d.isoformat(),
                       "days": (d - today).days, "purpose": purpose, "link": it["link"]}
    return sorted(events.values(), key=lambda e: (e["date"], e["ticker"]))


def select(items, state, now, hours=48, show_all=False):
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    ahead = now + _dt.timedelta(minutes=10)   # a pinned --now: nothing from after it
    out = []
    for it in items:
        if it["time"] is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        fp = fingerprint(it)
        it["new"] = fp not in seen
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: (i["time"] or now), reverse=True)
    return out


def remember(items, state, now):
    """Mark every item read as seen (kept KEEP_DAYS days), so none is shown twice."""
    seen = state.setdefault("seen", {})
    stamp = nse.iso_ist(now)
    for it in items:
        seen.setdefault(fingerprint(it), stamp)
    cutoff = nse.iso_ist(now - _dt.timedelta(days=KEEP_DAYS))
    state["seen"] = dict((k, v) for k, v in seen.items() if v >= cutoff)
    state["last_check"] = stamp


def render_moves(moves, move_pct=MOVE_PCT):
    out = []
    for m in moves:
        flag = "  MOVING" if abs(m["pct"]) >= move_pct else ""
        when = (" at %s IST" % m["time"].strftime("%H:%M")) if m.get("time") else ""
        out.append("%-11s %s %+.2f%% vs previous close %s%s%s" % (
            m["ticker"], nse.inr(m["price"]), m["pct"], nse.inr(m["prev_close"]), when, flag))
    return out


def render_upcoming(events):
    out = []
    for e in events:
        d = _dt.date.fromisoformat(e["date"])
        when = "%s %s" % (d.strftime("%a"), e["date"])
        if e["what"] == "results":
            head = "RESULTS THIS WEEK: board meeting %s" % when
        elif e["what"] == "board meeting":
            head = "board meeting %s" % when
        else:
            head = "ex-date %s" % when
        soon = " (today)" if e["days"] == 0 else (" (tomorrow)" if e["days"] == 1 else " (in %d days)" % e["days"])
        out.append("  %-11s %s%s%s" % (e["ticker"], head, soon, (": " + e["purpose"]) if e["purpose"] else ""))
    return out


def render(items, errors, now, moves=None, events=None, move_pct=MOVE_PCT):
    out = render_moves(moves or [], move_pct)
    if out:
        out.append("")
    ev = render_upcoming(events or [])
    if ev:
        out.append("Coming up (gap risk: a results day or an ex-date can jump past any stop):")
        out += ev
        out.append("")
    if not items:
        out.append("Nothing new.")
    for it in items:
        mark = "!" if it.get("important") else " "
        who = ",".join(it["touches"]) or it["ticker"]
        out.append("%s %s  %-11s %-15s %s" % (mark, fmt_ist(it["time"]), who, it["source"], it["title"]))
        extra = []
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        extra.append(it["link"])
        out.append("      " + "  ".join(x for x in extra if x))
    for e in errors:
        out.append("note: %s" % e)
    out.append("Checked %s. Headlines are leads, not facts; open the filing before acting. "
               "Not financial advice." % fmt_ist(now))
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = dict(it)
        d["time"] = nse.iso_ist(it["time"]) if it["time"] else None
        out.append(d)
    return out


def load_equity(fetcher, errors):
    try:
        if fetcher.offline:
            path = fetcher.find("EQUITY_L.csv")
            if not path:
                raise NewsError("offline: no file EQUITY_L.csv")
            return nse.parse_equity_list(nse.read_maybe_gz(path).decode("utf-8", "replace"))
        return nse.equity_list()
    except (NewsError, nse.NSEError, OSError) as exc:
        errors.append("NSE's EQUITY_L.csv could not be read (%s): NSE filings are matched by file name only" % exc)
        return {}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tickers", nargs="*", help="NSE symbols (default: watchlist + plan + paper positions)")
    ap.add_argument("--watchlist")
    ap.add_argument("--plan")
    ap.add_argument("--paper")
    ap.add_argument("--state", help="default trader/state/news.json")
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this (48)")
    ap.add_argument("--days-ahead", type=int, default=DAYS_AHEAD, help="results dates / ex-dates window (7)")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--no-nse", action="store_true", help="skip NSE filings")
    ap.add_argument("--no-yahoo", action="store_true", help="skip Yahoo headlines")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per stock")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--offline", metavar="DIR[,DIR]")
    ap.add_argument("--now", help="pin the clock, IST: \"2026-10-29 15:30\"")
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    known = watch_from_files(args.watchlist, args.plan, args.paper)
    if args.tickers:
        syms = [s for t in args.tickers for s in re.split(r"[,\s]+", t) if s]
        try:
            watch = dict((nse.clean_symbol(s), None) for s in syms)
        except nse.NSEError as exc:
            print("news: %s" % exc, file=sys.stderr)
            return 2
        watch.update(dict((t, known[t]) for t in watch if known.get(t)))
    else:
        watch = known
    if not watch:
        print("news: no symbols. Pass --tickers, or run /india-scanner first (it writes "
              "trader/watchlist.json).", file=sys.stderr)
        return 2
    try:
        now = nse.parse_now(args.now).replace(microsecond=0)
    except nse.NSEError as exc:
        print("news: %s" % exc, file=sys.stderr)
        return 2
    state_path = args.state or os.path.join(nse.trader_dir(), "state", "news.json")
    state = load_json(state_path) or {}
    errors = []
    fetcher = Fetcher(args.offline)
    equity = load_equity(fetcher, errors)
    resolver = Resolver(equity)
    for t in watch:
        if not watch[t]:
            watch[t] = resolver.name_of(t)
        if equity and t not in equity:
            errors.append("%s is not in NSE's equity list (EQUITY_L.csv)" % t)
    items = collect(fetcher, watch, resolver, nse_feeds=not args.no_nse, yahoo=not args.no_yahoo, errors=errors)
    items = dedupe(tag(items, watch))
    events = upcoming(items, watch, now.date(), args.days_ahead)
    shown = select(items, state, now, hours=args.hours, show_all=args.all)
    moves = [] if args.no_moves else collect_moves(fetcher, watch, errors)
    if args.json:
        mv = [dict(m, time=nse.iso_ist(m["time"]) if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": nse.iso_ist(now), "symbols": sorted(watch), "moves": mv,
                          "upcoming": events, "items": _json_ready(shown), "notes": errors},
                         indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(render(shown, errors, now, moves, events, args.move_pct))
    if not args.dry_run:
        remember(items, state, now)
        nse.write_json(state_path, state)
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())
