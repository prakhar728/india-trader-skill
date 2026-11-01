#!/usr/bin/env python3
"""Who is trading: SEBI insider-trading disclosures and NSE bulk / block deals,
for india-insiders. Standard library only.

    python3 insiders.py insider                         # new PIT disclosures since the last check
    python3 insiders.py insider --promoters-only --buys --min-value 1000000
    python3 insiders.py insider --symbol INFY --all
    python3 insiders.py deals                           # the day's bulk and block deals
    python3 insiders.py deals --min-value 50000000 --buys
    python3 insiders.py deals --follow "Serum Institute" # remember a name (trader/config.json follow_names)
    python3 insiders.py deals --unfollow "Serum Institute"
    python3 insiders.py watch                           # anything touching your watchlist / paper positions

Sources (plain GET, nsearchives.nseindia.com only, half a second apart):
  content/RSS/InsiderTrading.xml      SEBI PIT Regulation 7(2) disclosures; each item links
                                      to an XBRL file (PIT V2.0, in-bse-co taxonomy)
  content/RSS/Insider_Trading.xml     the older feed (in-capmkt taxonomy); it stopped at
                                      May 2026 and is read only with --legacy
  content/equities/bulk.csv           the day's bulk deals (a client trading over 0.5% of a
                                      company's shares in a day), posted after the close
  content/equities/block.csv          the day's block deals (one trade of Rs 10 crore or
                                      more in the block-deal windows)

Each disclosure row gives the person, their category (promoter, promoter group,
director, KMP, designated person...), Buy / Sell / Pledge / Revoke / Invoke,
quantity, value, the mode (market purchase, off market, ESOP, pledge creation,
gift, inter-se transfer...), holding before and after, the trade dates and when
the company was told. Holding percentages: the PIT V2.0 files carry a fraction
(0.4556 = 45.56%), the older files a percentage; both are shown as percent.

These are public disclosures, filed days after the trade (insiders have two
trading days to tell the company, the company two more to tell NSE). They say
what someone already did, not what will happen next.
"""

import argparse
import csv
import datetime
import io
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "india-common"))
import nse  # noqa: E402

RSS = "https://nsearchives.nseindia.com/content/RSS/"
FEED = RSS + "InsiderTrading.xml"
LEGACY_FEED = RSS + "Insider_Trading.xml"
BULK = "https://nsearchives.nseindia.com/content/equities/bulk.csv"
BLOCK = "https://nsearchives.nseindia.com/content/equities/block.csv"
ALLOWED_HOSTS = ("nsearchives.nseindia.com", "archives.nseindia.com")
XBRLI = "{http://www.xbrl.org/2003/instance}"
OPEN_MARKET = ("market purchase", "market sale", "open market")
NOISE = ("esop", "pledge", "revoke", "invoke", "gift", "inter-se", "inter se", "off market", "allotment", "bonus",
         "transmission", "rights", "preferential", "employee")


class InsiderError(Exception):
    pass


# ---------------------------------------------------------------- PIT XBRL

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _num(text):
    try:
        return float(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_pit(data, source=None):
    """A PIT disclosure XBRL -> {symbol, company, rows: [...]}."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise InsiderError("not an XBRL file (%s)%s" % (e, (": " + source) if source else ""))
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    new_schema = "in-bse-co" in text[:3000]
    facts = {}
    for e in root:
        ref = e.get("contextRef")
        if ref:
            facts.setdefault(ref, {}).setdefault(_local(e.tag), (e.text or "").strip())
    head = facts.get("MainI") or facts.get("OneI") or {}
    if not head:
        raise InsiderError("no company block (MainI / OneI) in this file%s" % ((": " + source) if source else ""))
    symbol = head.get("Symbol") or head.get("NSESymbol")
    fname = (source or "").rstrip("/").rsplit("/", 1)[-1]
    out = {"symbol": symbol, "company": head.get("NameOfTheCompany"), "isin": head.get("ISINCode") or head.get("ISIN"),
           "regulation": head.get("DisclosureUnderRegulation") or head.get("RegulationOfInsiderTrading"),
           "filed": head.get("DateOfFiling"), "revised": head.get("RevisedFilling") == "true",
           "schema": "PIT V2.0 (in-bse-co)" if new_schema else "PIT 2018 (in-capmkt)", "source": source, "rows": []}
    scale = 100.0 if new_schema else 1.0
    for cid in sorted(facts, key=lambda c: (len(c), c)):
        f = facts[cid]
        if cid in ("MainI", "OneI") or not f.get("NameOfThePerson"):
            continue

        def pct(k):
            v = _num(f.get(k))
            return None if v is None else round(v * scale, 4)
        typ = (f.get("SecuritiesAcquiredOrDisposedTransactionType") or "").strip()
        value = _num(f.get("SecuritiesAcquiredOrDisposedValueOfSecurity"))
        if value is None:
            value = _num(f.get("ValueInAggregate"))
        instrument = f.get("TypeOfInstrument") or ""
        if f.get("TypeOfInstrumentOthers"):
            instrument += " (%s)" % f["TypeOfInstrumentOthers"]
        row = {"id": "%s#%s" % (fname, cid), "symbol": symbol, "company": out["company"],
               "person": " ".join(f["NameOfThePerson"].split()), "category": f.get("CategoryOfPerson") or "",
               "instrument": instrument, "type": typ, "mode": f.get("ModeOfAcquisitionOrDisposal") or "",
               "qty": _num(f.get("SecuritiesAcquiredOrDisposedNumberOfSecurity")), "value": value,
               "before_qty": _num(f.get("SecuritiesHeldPriorToAcquisitionOrDisposalNumberOfSecurity")),
               "before_pct": pct("SecuritiesHeldPriorToAcquisitionOrDisposalPercentageOfShareholding"),
               "after_qty": _num(f.get("SecuritiesHeldPostAcquistionOrDisposalNumberOfSecurity")),
               "after_pct": pct("SecuritiesHeldPostAcquistionOrDisposalPercentageOfShareholding"),
               "from": f.get("DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate"),
               "to": f.get("DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyToDate"),
               "intimated": f.get("DateOfIntimationToCompany"), "filed": out["filed"],
               "exchange": f.get("ExchangeOnWhichTheTradeWasExecuted") or "",
               "note": " ".join((f.get("DetailsOfChangeInHoldingOfSecuritiesOfPromotersEmployeeOrDirectorOfAListed"
                                   "CompanyAndOtherSuchPersonsTextBlock") or "").split())[:200]}
        row.update(classify(row))
        out["rows"].append(row)
    return out


def classify(row):
    """Tags: promoter? open market? the signal people watch, or mostly noise."""
    cat, mode, typ = row["category"].lower(), row["mode"].lower(), row["type"].lower()
    promoter = "promoter" in cat
    insider = promoter or any(w in cat for w in ("director", "kmp", "key managerial"))
    open_market = any(w in mode for w in OPEN_MARKET) and "off market" not in mode
    equity = row["instrument"].lower().startswith("equity")
    noise = [w for w in NOISE if w in mode or w in typ]
    if typ in ("pledge", "revoke", "invoke"):
        noise.append(typ)
    if re.search(r"employee|welfare trust|stock option|esop", row["person"], re.I):
        noise.append("employee trust")
    if promoter and typ == "buy" and open_market and equity:
        tag = "PROMOTER OPEN-MARKET BUY"
    elif promoter and typ == "sell" and open_market and equity:
        tag = "PROMOTER OPEN-MARKET SELL"
    elif insider and open_market and equity and typ in ("buy", "sell"):
        tag = "INSIDER OPEN-MARKET " + typ.upper()
    elif noise:
        tag = "noise: " + ", ".join(sorted(set(noise)))
    elif not equity:
        tag = "not shares: " + row["instrument"]
    else:
        tag = ""
    return {"promoter": promoter, "open_market": open_market, "tag": tag}


def lag_days(row):
    """Days from the last trade date to the NSE filing (or to telling the company)."""
    try:
        to = datetime.date.fromisoformat(row["to"])
    except (TypeError, ValueError):
        return None
    for k in ("filed", "intimated"):
        try:
            return (datetime.date.fromisoformat(str(row.get(k))[:10]) - to).days
        except (TypeError, ValueError):
            continue
    return None


def describe(row):
    qty = "{:,.0f}".format(row["qty"]) if row.get("qty") is not None else "?"
    s = "%s (%s): %s, %s, %s %s %s" % (row["symbol"], row["company"], row["person"], row["category"] or "category ?",
                                         (row["type"] or "?").upper(), qty, row["instrument"].lower() or "securities")
    if row.get("value"):
        s += " for %s" % nse.inr(row["value"], 0)
        if row.get("qty"):
            s += " (%s each)" % nse.inr(row["value"] / row["qty"])
    if row.get("mode"):
        s += ", %s" % row["mode"].lower()
    if row.get("exchange") and row["exchange"] not in ("NA", ""):
        s += " on %s" % row["exchange"]
    if row.get("from"):
        s += ", %s" % (row["from"] if row["from"] == row.get("to") else "%s to %s" % (row["from"], row.get("to")))
    if row.get("before_pct") is not None and row.get("after_pct") is not None:
        s += "; holding %.2f%% -> %.2f%%" % (row["before_pct"], row["after_pct"])
    lag = lag_days(row)
    if lag is not None:
        s += "; disclosed %d day%s after the trade" % (lag, "" if lag == 1 else "s")
    if row.get("note"):
        s += ". Note: %s" % row["note"]
    return s + "."


# ---------------------------------------------------------------- RSS

def parse_rss(data):
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise InsiderError("the RSS feed is not valid XML: %s" % e)
    out, seen = [], set()
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        desc = (it.findtext("description") or "").strip()
        if not link or link in seen:
            continue
        seen.add(link)
        parts = desc.split("|")
        sym = parts[0].strip() if len(parts) > 3 and re.match(r"^[A-Z0-9&-]+$", parts[0].strip()) else None
        out.append({"company": " ".join((it.findtext("title") or "").split()), "link": link, "symbol": sym,
                    "revision": len(parts) > 2 and parts[2].strip().lower() == "revision",
                    "published": rss_time(it.findtext("pubDate") or "")})
    return out


def rss_time(text):
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M"):
        try:
            return nse.iso_ist(datetime.datetime.strptime(text.strip(), fmt))
        except ValueError:
            continue
    return text.strip()


# ---------------------------------------------------------------- bulk / block deals

def parse_deals(text, kind):
    rows = []
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        r = dict((str(k).strip(), (v or "").strip()) for k, v in r.items() if k)
        try:
            qty = float(r.get("Quantity Traded", "").replace(",", ""))
            price = float(r.get("Trade Price / Wght. Avg. Price", "").replace(",", ""))
        except ValueError:
            continue
        try:
            d = datetime.datetime.strptime(r.get("Date", ""), "%d-%b-%Y").date().isoformat()
        except ValueError:
            d = r.get("Date", "")
        rows.append({"kind": kind, "date": d, "symbol": r.get("Symbol", ""), "security": r.get("Security Name", ""),
                     "client": " ".join(r.get("Client Name", "").split()), "side": r.get("Buy/Sell", "").upper(),
                     "qty": qty, "price": price, "value": round(qty * price, 2),
                     "remarks": "" if r.get("Remarks") in (None, "-", "") else r["Remarks"]})
    return rows


def follow_match(client, names):
    c = client.lower()
    return [n for n in names if n.strip() and n.strip().lower() in c]


def deal_line(d, follow=()):
    s = "%-5s %s %-12s %-4s %12s @ %-10s %16s  %s" % (
        d["kind"].upper(), d["date"], d["symbol"], d["side"], "{:,.0f}".format(d["qty"]), "%.2f" % d["price"],
        nse.inr(d["value"], 0), d["client"])
    if follow:
        s += "   <- FOLLOW: %s" % ", ".join(follow)
    return s


# ---------------------------------------------------------------- IO

class Source(object):
    """Network (plain GET, a pause between calls) or saved files (--offline DIR, repeatable)."""

    def __init__(self, offline=None, pause=None):
        self.offline = offline or []
        self.pause = nse.PAUSE if pause is None else pause
        self.calls = 0

    def get(self, url):
        name = url.rstrip("/").rsplit("/", 1)[-1]
        if self.offline:
            for d in self.offline:
                for p in (os.path.join(d, name), os.path.join(d, name + ".gz")):
                    if os.path.exists(p):
                        return nse.read_maybe_gz(p)
            raise InsiderError("no saved copy of %s in %s" % (name, ", ".join(self.offline)))
        u = urllib.parse.urlsplit(url)
        if u.scheme != "https" or u.netloc not in ALLOWED_HOSTS:
            raise InsiderError("only https links on NSE's archive hosts are fetched, not %s" % url)
        if self.calls and self.pause:
            time.sleep(self.pause)
        self.calls += 1
        try:
            return nse.http_get(url, timeout=60)
        except nse.NSEError as e:
            raise InsiderError(str(e))


def _paths(base=None):
    d = nse.trader_dir(base)
    return {"dir": d, "config": os.path.join(d, "config.json"), "state": os.path.join(d, "state", "insiders.json"),
            "watchlist": os.path.join(d, "watchlist.json"), "paper": os.path.join(d, "paper.json"),
            "plan": os.path.join(d, "plan.json")}


def symbols_in(node, depth=0):
    out = []
    if depth > 4:
        return out
    if isinstance(node, dict):
        for k in ("symbol", "ticker"):
            if isinstance(node.get(k), str):
                out.append(node[k])
        pos = node.get("positions")
        if isinstance(pos, dict):
            out += [k for k, v in pos.items() if not isinstance(v, dict) or (v.get("qty") or v.get("quantity"))]
        for k, v in node.items():
            if isinstance(v, (list, dict)) and not (k == "positions" and isinstance(v, dict)):
                out += symbols_in(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            out += [v] if isinstance(v, str) else symbols_in(v, depth + 1)
    clean = []
    for s in out:
        try:
            c = nse.clean_symbol(s)
        except nse.NSEError:
            continue
        if c not in clean:
            clean.append(c)
    return clean


def watched_symbols(paths):
    syms = []
    for k in ("watchlist", "paper", "plan"):
        for s in symbols_in(nse.read_json(paths[k], None)):
            if s not in syms:
                syms.append(s)
    return syms


def load_disclosures(src, state, symbols=None, legacy=False, errors=None):
    """Feed items -> parsed rows, fetching only XBRL files not read before (kept in the state)."""
    files = state.setdefault("files", {})
    feeds = [FEED] + ([LEGACY_FEED] if legacy else [])
    rows = []
    for url in feeds:
        try:
            items = parse_rss(src.get(url))
        except InsiderError as e:
            if errors is not None:
                errors.append("%s: %s" % (url.rsplit("/", 1)[-1], e))
            continue
        for it in items:
            if symbols and it["symbol"] and it["symbol"] not in symbols:
                continue
            fname = it["link"].rsplit("/", 1)[-1]
            if fname not in files:
                try:
                    f = parse_pit(src.get(it["link"]), it["link"])
                except InsiderError as e:
                    if errors is not None:
                        errors.append("%s: %s" % (fname, e))
                    continue
                for r in f["rows"]:
                    r["filed"] = r.get("filed") or it["published"][:10]
                    r["published"] = it["published"]
                    r["link"] = it["link"]
                    r["revision"] = it["revision"] or f["revised"]
                files[fname] = {"symbol": f["symbol"], "published": it["published"], "rows": f["rows"]}
            for r in files[fname]["rows"]:
                if symbols and r["symbol"] not in symbols:
                    continue
                rows.append(r)
    rows.sort(key=lambda r: (r.get("published") or "", r["id"]), reverse=True)
    return rows


def filter_rows(rows, a):
    out = []
    for r in rows:
        if getattr(a, "promoters_only", False) and not r["promoter"]:
            continue
        if getattr(a, "buys", False) and r["type"].lower() != "buy":
            continue
        if getattr(a, "sells", False) and r["type"].lower() != "sell":
            continue
        if getattr(a, "min_value", None) and (r.get("value") or 0) < a.min_value:
            continue
        if getattr(a, "open_market", False) and not r["open_market"]:
            continue
        out.append(r)
    return out


def fetch_deals(src, errors, which=("bulk", "block")):
    deals = []
    for kind, url in (("bulk", BULK), ("block", BLOCK)):
        if kind not in which:
            continue
        try:
            deals += parse_deals(src.get(url).decode("utf-8", "replace"), kind)
        except InsiderError as e:
            errors.append("%s deals: %s" % (kind, e))
    return deals


# ---------------------------------------------------------------- commands

def cmd_insider(a, paths, cfg):
    src = Source(a.offline)
    state = nse.read_json(paths["state"], {}) or {}
    rung = state.setdefault("rung", {})
    want = set(nse.clean_symbol(s) for s in (a.symbol or [])) or None
    errors = []
    now = nse.parse_now(a.now)
    rows = filter_rows(load_disclosures(src, state, want, a.legacy, errors), a)
    before = set(rung)
    new = [r for r in rows if a.all or r["id"] not in rung]
    for r in new:
        rung.setdefault(r["id"], nse.iso_ist(now))
    if not a.dry_run:
        state["last_check"] = nse.iso_ist(now)
        nse.write_json(paths["state"], state)
    if a.json:
        print(json.dumps({"rows": new, "errors": errors}, indent=2))
        return 0 if new else 1
    if not new:
        print("No new insider disclosures%s since the last check." % (" for " + ", ".join(sorted(want)) if want else ""))
    for r in new:
        print("%sINSIDER %s" % ("" if r["id"] in before else "NEW ", describe(r)))
        if r["tag"]:
            print("    [%s]" % r["tag"])
        print("    %s%s" % (r.get("link", ""), "  (revised filing)" if r.get("revision") else ""))
    for e in errors:
        print("error: " + e)
    if new:
        print("Public disclosures, filed days after the trade: they say what someone already did. Open-market "
              "promoter buying is the line people watch; ESOP, pledge, gift and inter-se entries are mostly noise.")
    return 0 if new else 1


def cmd_deals(a, paths, cfg):
    names = list(cfg.get("follow_names") or [])
    changed = False
    for n in a.follow or []:
        if n.strip() and n.strip().lower() not in [x.lower() for x in names]:
            names.append(n.strip())
            changed = True
    for n in a.unfollow or []:
        keep = [x for x in names if x.lower() != n.strip().lower()]
        changed = changed or len(keep) != len(names)
        names = keep
    if changed:
        doc = nse.read_json(paths["config"], {}) or {}
        doc["follow_names"] = names
        nse.write_json(paths["config"], doc)
        print("Following %s (saved in %s as follow_names)." % (", ".join(names) or "nobody", paths["config"]))
    src = Source(a.offline)
    errors = []
    which = ("bulk",) if a.bulk_only else ("block",) if a.block_only else ("bulk", "block")
    deals = fetch_deals(src, errors, which)
    now = nse.parse_now(a.now)
    want = set(nse.clean_symbol(s) for s in (a.symbol or []))
    shown = []
    for d in deals:
        if want and d["symbol"] not in want:
            continue
        if a.buys and d["side"] != "BUY" or a.sells and d["side"] != "SELL":
            continue
        if a.min_value and d["value"] < a.min_value:
            continue
        d["follow"] = follow_match(d["client"], names)
        if a.following and not d["follow"]:
            continue
        shown.append(d)
    state = nse.read_json(paths["state"], {}) or {}
    rung = state.setdefault("deals_rung", {})
    alerts = []
    for d in shown:
        if d["follow"]:
            key = "%s|%s|%s|%s|%s|%s" % (d["kind"], d["date"], d["symbol"], d["client"], d["side"], int(d["qty"]))
            if key not in rung:
                rung[key] = nse.iso_ist(now)
                alerts.append(d)
    if not a.dry_run:
        nse.write_json(paths["state"], state)
    if a.json:
        print(json.dumps({"deals": shown, "followed_new": alerts, "follow_names": names, "errors": errors}, indent=2))
        return 0 if shown else 1
    dates = sorted(set(d["date"] for d in deals))
    if dates:
        print("NSE bulk and block deals for %s (%d in the files, %d shown)." % (", ".join(dates), len(deals), len(shown)))
        if dates[-1] != now.date().isoformat():
            print("These are not today's (%s): NSE replaces the files after each day's close." % now.date().isoformat())
    for d in alerts:
        print("FOLLOWED NAME " + deal_line(d, d["follow"]))
    for d in sorted(shown, key=lambda x: (-x["value"])):
        print(deal_line(d, d["follow"]))
    if not shown:
        print("No deals match.")
    for e in errors:
        print("error: " + e)
    print("Bulk and block deals are public trade reports. A known name buying says what they did yesterday, "
          "not what the price will do.")
    return 0 if shown else 1


def cmd_watch(a, paths, cfg):
    syms = set(nse.clean_symbol(s) for s in (a.symbol or [])) or set(watched_symbols(paths))
    if not syms:
        raise InsiderError("no symbols in trader/watchlist.json, trader/paper.json or trader/plan.json "
                           "(or pass --symbol)")
    src = Source(a.offline)
    state = nse.read_json(paths["state"], {}) or {}
    rung = state.setdefault("rung", {})
    errors = []
    now = nse.parse_now(a.now)
    rows = load_disclosures(src, state, syms, a.legacy, errors)
    deals = [d for d in fetch_deals(src, errors) if d["symbol"] in syms]
    names = list(cfg.get("follow_names") or [])
    new_ids = set(r["id"] for r in rows if r["id"] not in rung)
    for r in rows:
        rung.setdefault(r["id"], nse.iso_ist(now))
    if not a.dry_run:
        nse.write_json(paths["state"], state)
    if a.json:
        print(json.dumps({"symbols": sorted(syms), "insider": rows, "deals": deals, "errors": errors}, indent=2))
        return 0 if rows or deals else 1
    print("Watching %s." % ", ".join(sorted(syms)))
    for r in rows:
        print("%sINSIDER %s" % ("NEW " if r["id"] in new_ids else "", describe(r)))
        if r["tag"]:
            print("    [%s]" % r["tag"])
    for d in deals:
        print("DEAL " + deal_line(d, follow_match(d["client"], names)))
    if not rows and not deals:
        print("Nothing in the current insider feed or the latest bulk / block deals touches these symbols.")
    for e in errors:
        print("error: " + e)
    return 0 if rows or deals else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Insider disclosures and bulk / block deals on NSE (india-insiders).")
    ap.add_argument("--trader-dir", help="the trader/ folder (default $TRADER_DIR or ./trader)")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--symbol", action="append", help="only this symbol (repeat)")
        p.add_argument("--offline", action="append", metavar="DIR", help="read saved feeds / files from DIR")
        p.add_argument("--now", help="pretend it is this IST time")
        p.add_argument("--dry-run", action="store_true", help="do not record what was shown")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("insider", help="new SEBI PIT insider-trading disclosures")
    common(p)
    p.add_argument("--promoters-only", action="store_true")
    p.add_argument("--buys", action="store_true")
    p.add_argument("--sells", action="store_true")
    p.add_argument("--open-market", action="store_true", help="only market purchases and sales")
    p.add_argument("--min-value", type=float, help="rupees")
    p.add_argument("--all", action="store_true", help="everything in the feed, not only new")
    p.add_argument("--legacy", action="store_true", help="also read the older Insider_Trading.xml feed")
    p = sub.add_parser("deals", help="the day's bulk and block deals")
    common(p)
    p.add_argument("--buys", action="store_true")
    p.add_argument("--sells", action="store_true")
    p.add_argument("--min-value", type=float, help="rupees")
    p.add_argument("--bulk-only", action="store_true")
    p.add_argument("--block-only", action="store_true")
    p.add_argument("--follow", action="append", metavar="NAME", help="flag deals by this client (saved)")
    p.add_argument("--unfollow", action="append", metavar="NAME")
    p.add_argument("--following", action="store_true", help="only deals by names you follow")
    p = sub.add_parser("watch", help="insider rows and deals touching your watchlist and paper positions")
    common(p)
    p.add_argument("--legacy", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    paths = _paths(a.trader_dir)
    try:
        cfg = nse.read_json(paths["config"], {}) or {}
        return {"insider": cmd_insider, "deals": cmd_deals, "watch": cmd_watch}[a.cmd](a, paths, cfg)
    except (InsiderError, nse.NSEError) as e:
        print("insiders.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
