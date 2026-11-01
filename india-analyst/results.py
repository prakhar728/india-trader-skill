#!/usr/bin/env python3
"""Quarterly results reader for india-analyst. Standard library only.

Reads the XBRL file a listed company files with NSE with its quarterly
results, keeps every quarter it has seen in trader/results/<SYMBOL>.json, and
says what changed: revenue from operations, total income, expenses, profit
before tax, net profit (and the owners' share for consolidated results),
basic and diluted EPS, quarter on quarter and year on year once the store has
the quarters to compare.

    python3 results.py latest                          # new results filings since the last check
    python3 results.py latest --watchlist --save       # only your symbols, fetched and stored
    python3 results.py latest --symbol HEROMOTOCO --save
    python3 results.py show HEROMOTOCO                 # from the store
    python3 results.py show HEROMOTOCO --xbrl https://nsearchives.nseindia.com/corporate/xbrl/INTEGRATED_FILING_INDAS_1748300_28102026090830_WEB.xml
    python3 results.py show GMRAIRPORT --xbrl saved-filing.xml --basis standalone
    python3 results.py show HEROMOTOCO --metrics trader/metrics.json

Sources (plain GET, nsearchives.nseindia.com / archives.nseindia.com only):
  content/RSS/Integrated_Filing_Financials.xml   results filed under SEBI's integrated
                                                 filing (from 2025): links to XBRL
  content/RSS/Integrated_Filing.xml              the same plus governance filings (skipped);
                                                 it reaches further back
  content/RSS/Financial_Results.xml              the older results feed (in-bse-fin XBRL;
                                                 it stopped updating in 2026)
  content/RSS/Online_announcements.xml           "financial results" board-meeting
                                                 outcomes (PDF), often an hour before the XBRL

What a filing holds. Context OneD is the reported period (a quarter, or a half
year for companies that report half-yearly), FourD the year to date (equal to
OneD in the first quarter, the whole year in a Q4 filing). The period comes
from DateOfStartOfReportingPeriod / DateOfEndOfReportingPeriod in each
context. Amounts are in rupees whatever LevelOfRounding says (that is how the
PDF rounds); the tool shows crores. A filing does NOT carry last year's
comparison columns, so year-on-year needs last year's filing in the store.
When the year to date is two quarters, the quarter before is derived (YTD
minus this quarter) and marked "derived"; derived EPS is approximate.

Taxonomies read: in-capmkt (SEBI integrated filing: Ind-AS, NBFC Ind-AS,
banking, non-Ind-AS) and in-bse-fin (the older results XBRL). Insurers'
formats are not read: the tool says so and gives the link.

Numbers only: this tool never says whether a stock is a buy.
"""

import argparse
import datetime
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
FEEDS = {"integrated": RSS + "Integrated_Filing_Financials.xml",
         "integrated_all": RSS + "Integrated_Filing.xml",
         "legacy": RSS + "Financial_Results.xml",
         "announcements": RSS + "Online_announcements.xml"}
ALLOWED_HOSTS = ("nsearchives.nseindia.com", "archives.nseindia.com")
XBRLI = "{http://www.xbrl.org/2003/instance}"

CONCEPTS = {
    "revenue": ["RevenueFromOperations", "InterestEarned"],
    "other_income": ["OtherIncome"],
    "total_income": ["Income", "TotalIncome"],
    "expenses": ["Expenses", "ExpenditureExcludingProvisionsAndContingencies"],
    "finance_costs": ["FinanceCosts"],
    "depreciation": ["DepreciationDepletionAndAmortisationExpense"],
    "exceptional": ["ExceptionalItemsBeforeTax", "ExceptionalItems"],
    "pbt": ["ProfitBeforeTax", "ProfitLossFromOrdinaryActivitiesBeforeTax"],
    "tax": ["TaxExpense"],
    "associates": ["ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod"],
    "net_profit": ["ProfitLossForPeriod", "ProfitLossForThePeriod"],
    "owners_profit": ["ProfitOrLossAttributableToOwnersOfParent",
                      "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates"],
    "eps_basic": ["BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                  "BasicEarningsPerShareAfterExtraordinaryItems",
                  "BasicEarningsLossPerShareFromContinuingOperations"],
    "eps_diluted": ["DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                    "DilutedEarningsPerShareAfterExtraordinaryItems",
                    "DilutedEarningsLossPerShareFromContinuingOperations"],
    # banks
    "interest_expended": ["InterestExpended"],
    "operating_profit": ["OperatingProfitBeforeProvisionAndContingencies"],
    "provisions": ["ProvisionsOtherThanTaxAndContingencies"],
    "gross_npa_pct": ["PercentageOfGrossNpa"],
    "net_npa_pct": ["PercentageOfNpa"],
}
FLOWS = ("revenue", "other_income", "total_income", "expenses", "finance_costs", "depreciation", "exceptional",
         "pbt", "tax", "associates", "net_profit", "owners_profit", "interest_expended", "operating_profit",
         "provisions")
PER_SHARE = ("eps_basic", "eps_diluted")
RATIOS = ("gross_npa_pct", "net_npa_pct")
INSURANCE_HINTS = ("PremiumsEarned", "NetPremium", "GrossPremium", "PolicyholdersAccount")


class ResultsError(Exception):
    pass


# ---------------------------------------------------------------- XBRL

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _num(text):
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def parse_xbrl(data, source=None):
    """One results filing -> dict with the company, the basis and one entry per reported period."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ResultsError("not an XBRL file (%s)%s" % (e, (": " + source) if source else ""))
    if _local(root.tag) != "xbrl":
        raise ResultsError("not an XBRL instance%s" % ((": " + source) if source else ""))
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    contexts = {}
    for c in root.iter(XBRLI + "context"):
        period = c.find(XBRLI + "period")
        dims = c.find(".//" + XBRLI + "scenario") is not None or c.find(".//" + XBRLI + "segment") is not None
        start, end = period.find(XBRLI + "startDate"), period.find(XBRLI + "endDate")
        contexts[c.get("id")] = {"dims": dims, "start": start.text if start is not None else None,
                                 "end": end.text if end is not None else None}
    facts = {}
    ns = set()
    for e in root:
        ref = e.get("contextRef")
        if not ref:
            continue
        ns.add(e.tag[1:].split("}")[0] if e.tag.startswith("{") else "")
        facts.setdefault(ref, {}).setdefault(_local(e.tag), (e.text or "").strip())
    head = facts.get("OneD") or next(iter(facts.values()), {})
    names = set(k for f in facts.values() for k in f)
    if any(any(h in n for h in INSURANCE_HINTS) for n in names):
        raise ResultsError("this looks like an insurer's results (premium-based format), which this tool does not "
                           "read yet. Open the filing's PDF on NSE instead%s" % ((": " + source) if source else ""))
    symbol = head.get("Symbol") or head.get("NSESymbol")
    out = {"symbol": symbol, "name": head.get("NameOfTheCompany") or head.get("NameOfBank"),
           "scrip_code": head.get("ScripCode"), "isin": head.get("ISIN"),
           "family": family(text, names), "taxonomy": sorted(n for n in ns if n)[:3],
           "rounding": head.get("LevelOfRounding") or head.get("LevelOfRoundingUsedInFinancialStatements"),
           "reporting": head.get("ReportingQuarter"), "period_type": head.get("TypeOfReportingPeriod"),
           "board_meeting": head.get("DateOfBoardMeetingWhenFinancialResultsWereApproved"),
           "fy_start": head.get("DateOfStartOfFinancialYear"), "source": source, "periods": []}
    seen = set()
    for cid, cf in facts.items():
        ctx = contexts.get(cid)
        if not ctx or ctx["dims"] or not ctx["start"]:
            continue
        values = {}
        for key, names_ in CONCEPTS.items():
            for n in names_:
                v = _num(cf.get(n))
                if v is not None:
                    values[key] = v
                    break
        if "revenue" not in values and "net_profit" not in values and "pbt" not in values:
            continue
        for k in RATIOS:
            if k in values:
                values[k] = round(values[k] * 100.0, 4)       # filed as a fraction
        if out["family"] == "bank" and "revenue" in values and "interest_expended" in values:
            values["net_interest_income"] = values["revenue"] - values["interest_expended"]
        start = cf.get("DateOfStartOfReportingPeriod") or ctx["start"]
        end = cf.get("DateOfEndOfReportingPeriod") or ctx["end"]
        if (start, end) in seen:
            continue
        seen.add((start, end))
        basis = (cf.get("NatureOfReportStandaloneConsolidated") or head.get("NatureOfReportStandaloneConsolidated")
                 or "Standalone").strip().lower()
        out["basis"] = "consolidated" if basis.startswith("consol") else "standalone"
        if out["basis"] == "standalone":
            values.pop("owners_profit", None)        # banks file it on standalone too; it equals net profit
        elif values.get("owners_profit") == 0 and values.get("net_profit") \
                and not _num(cf.get("ProfitOrLossAttributableToNonControllingInterests")):
            values.pop("owners_profit")              # owners' and minority shares both left at 0: not filed
            out["owners_not_filed"] = True
        out["periods"].append({"context": cid, "start": start, "end": end, "kind": kind(start, end),
                               "audited": cf.get("WhetherResultsAreAuditedOrUnaudited"), "values": values})
    if not out["periods"]:
        raise ResultsError("no profit-and-loss lines found in this filing (taxonomy %s). It may be a governance "
                           "filing or a format this tool does not read%s"
                           % (", ".join(out["taxonomy"]) or "?", (": " + source) if source else ""))
    out.setdefault("basis", "standalone")
    out["periods"].sort(key=lambda p: (p["end"], p["start"]), reverse=True)
    return out


def family(text, names):
    if "IntegratedFinance_Banking" in text or "OperatingProfitBeforeProvisionAndContingencies" in names:
        return "bank"
    if "IFNBFC" in text[:400] or "_NBFC" in text[:2000]:
        return "nbfc"
    if "IFOTB" in text[:400] or "NonIndAS" in text[:3000]:
        return "non-ind-as"
    if "in-bse-fin" in text[:3000]:
        return "ind-as (older in-bse-fin taxonomy)"
    return "ind-as"


def _d(s):
    return datetime.date.fromisoformat(s)


def kind(start, end):
    days = (_d(end) - _d(start)).days + 1
    for lo, hi, name in ((80, 100, "quarter"), (170, 190, "half year"), (260, 280, "nine months"),
                         (355, 370, "year")):
        if lo <= days <= hi:
            return name
    return "%d days" % days


def fiscal_label(start, end):
    """Indian financial year April-March: the quarter ending September 2026 is Q2 FY27."""
    e = _d(end)
    fy = e.year + 1 if e.month >= 4 else e.year
    k = kind(start, end)
    tag = "FY%02d" % (fy % 100)
    if k == "quarter":
        return "Q%d %s" % ({6: 1, 9: 2, 12: 3, 3: 4}.get(e.month, 0), tag)
    return {"half year": "H1 " if e.month == 9 else "H2 ", "nine months": "9M ", "year": ""}.get(k, k + " ") + tag


def derive_previous(filing):
    """YTD minus this quarter = the quarter before, when the YTD is two quarters (a Q2 filing)."""
    qs = [p for p in filing["periods"] if p["kind"] == "quarter"]
    ytd = [p for p in filing["periods"] if p["kind"] == "half year"]
    out = []
    for q in qs:
        for y in ytd:
            if y["end"] != q["end"] or y["start"] >= q["start"]:
                continue
            p_end = (_d(q["start"]) - datetime.timedelta(days=1)).isoformat()
            if kind(y["start"], p_end) != "quarter":
                continue
            vals = {}
            for k in FLOWS + PER_SHARE:
                if k in y["values"] and k in q["values"]:
                    vals[k] = round(y["values"][k] - q["values"][k], 2)
            if "net_interest_income" in y["values"] and "net_interest_income" in q["values"]:
                vals["net_interest_income"] = y["values"]["net_interest_income"] - q["values"]["net_interest_income"]
            out.append({"start": y["start"], "end": p_end, "kind": "quarter", "derived": True, "values": vals})
    return out


# ---------------------------------------------------------------- store

def store_path(symbol, base=None):
    return os.path.join(nse.trader_dir(base), "results", "%s.json" % nse.clean_symbol(symbol))


def load_store(symbol, base=None):
    return nse.read_json(store_path(symbol, base), None) or {"symbol": nse.clean_symbol(symbol), "periods": []}


def merge(store, filing, now=None):
    """Add a filing's periods (and a derived quarter) to the store. A filed period beats a derived
    one; a later filing of the same period (a revision) replaces the earlier one."""
    store["name"] = filing.get("name") or store.get("name")
    store["family"] = filing.get("family")
    rows = dict(((p["basis"], p["start"], p["end"]), p) for p in store.get("periods", []))
    added = 0
    src = {"xbrl": filing.get("source"), "board_meeting": filing.get("board_meeting")}
    for p in filing["periods"]:
        key = (filing["basis"], p["start"], p["end"])
        rows[key] = {"basis": filing["basis"], "start": p["start"], "end": p["end"], "kind": p["kind"],
                     "derived": False, "audited": p.get("audited"), "values": p["values"], "source": src}
        added += 1
    for p in derive_previous(filing):
        key = (filing["basis"], p["start"], p["end"])
        if key in rows and not rows[key].get("derived"):
            continue
        rows[key] = dict(p, basis=filing["basis"], source=dict(src, note="this filing's year to date minus "
                                                                         "its quarter"))
        added += 1
    store["periods"] = sorted(rows.values(), key=lambda r: (r["basis"], r["end"], r["start"]), reverse=True)
    store["updated"] = nse.iso_ist(now or nse.now_ist())
    return added


def save_store(store, base=None):
    nse.write_json(store_path(store["symbol"], base), store)


# ---------------------------------------------------------------- comparisons

def pct(a, b):
    if a is None or b is None or b == 0:
        return None
    return round(100.0 * (a - b) / abs(b), 1)


def _find(rows, start, end, slack=6):
    for r in rows:
        if abs((_d(r["start"]) - start).days) <= slack and abs((_d(r["end"]) - end).days) <= slack:
            return r
    return None


def compare(store, basis):
    """Rows of one basis, newest first, each with its quarter-before and year-earlier matches."""
    rows = [r for r in store.get("periods", []) if r["basis"] == basis]
    out = []
    for r in rows:
        s, e = _d(r["start"]), _d(r["end"])
        prev = None
        if r["kind"] == "quarter":
            pe = s - datetime.timedelta(days=1)
            prev = _find([x for x in rows if x["kind"] == "quarter"], pe - datetime.timedelta(days=(e - s).days), pe, 10)
        yago = _find([x for x in rows if x["kind"] == r["kind"]], s.replace(year=s.year - 1),
                     e.replace(year=e.year - 1, day=min(e.day, 28)) if e.month == 2 else e.replace(year=e.year - 1))
        out.append({"row": r, "prev": prev, "year_ago": yago})
    return out


def _v(r, k):
    return (r or {}).get("values", {}).get(k)


def headline(store, basis):
    """Plain sentences about the newest quarter (or period) of one basis."""
    comp = compare(store, basis)
    filed = [c for c in comp if not c["row"].get("derived")]
    if not filed:
        return []
    cur = next((c for c in filed if c["row"]["kind"] == "quarter"), filed[0])
    r = cur["row"]
    fam = store.get("family") or ""
    rev_word = "interest earned" if fam == "bank" else "revenue from operations"
    profit_key = "owners_profit" if basis == "consolidated" and _v(r, "owners_profit") is not None else "net_profit"
    lines = []
    s = "%s (%s to %s, %s): %s %s" % (fiscal_label(r["start"], r["end"]), r["start"], r["end"], basis, rev_word,
                                      crore(_v(r, "revenue")))
    if cur["prev"]:
        s += " (%s vs the quarter before%s)" % (_pp(pct(_v(r, "revenue"), _v(cur["prev"], "revenue"))),
                                                ", derived" if cur["prev"].get("derived") else "")
    if cur["year_ago"]:
        s += " (%s vs a year earlier)" % _pp(pct(_v(r, "revenue"), _v(cur["year_ago"], "revenue")))
    s += ", %s %s" % ("profit attributable to owners" if profit_key == "owners_profit" else "net profit",
                      crore(_v(r, profit_key)))
    if cur["year_ago"]:
        s += " (%s vs a year earlier)" % _pp(pct(_v(r, profit_key), _v(cur["year_ago"], profit_key)))
    if _v(r, "eps_diluted") is not None:
        s += ", diluted EPS Rs %.2f" % _v(r, "eps_diluted")
    lines.append(s + ".")
    if _v(r, "revenue") and _v(r, "net_profit") is not None:
        m = 100.0 * _v(r, "net_profit") / _v(r, "revenue")
        t = "Net profit is %.1f%% of %s" % (m, rev_word)
        if cur["prev"] and _v(cur["prev"], "revenue"):
            t += " (%.1f%% the quarter before)" % (100.0 * _v(cur["prev"], "net_profit") / _v(cur["prev"], "revenue"))
        lines.append(t + ".")
    if fam == "bank":
        bits = []
        if _v(r, "net_interest_income") is not None:
            bits.append("net interest income %s" % crore(_v(r, "net_interest_income")))
        if _v(r, "operating_profit") is not None:
            bits.append("operating profit before provisions %s" % crore(_v(r, "operating_profit")))
        if _v(r, "provisions") is not None:
            bits.append("provisions %s" % crore(_v(r, "provisions")))
        if _v(r, "gross_npa_pct") is not None:
            bits.append("gross NPA %.2f%%, net NPA %.2f%%" % (_v(r, "gross_npa_pct"), _v(r, "net_npa_pct") or 0))
        if bits:
            lines.append("Bank: " + ", ".join(bits) + ".")
    if r["kind"] == "quarter" and not cur["year_ago"]:
        e = _d(r["end"])
        lines.append("No year-earlier quarter (ending %s) in the store yet, so no year-on-year growth. Add last "
                     "year's filing with `show %s --xbrl <url or file>`; the store fills itself each quarter you "
                     "run `latest --save`." % (e.replace(year=e.year - 1).isoformat(), store["symbol"]))
    if r["kind"] == "quarter" and not cur["prev"]:
        lines.append("No quarter before this one in the store yet, so no quarter-on-quarter change.")
    ex = _v(r, "exceptional")
    if ex:
        lines.append("Exceptional items of %s are inside profit before tax: read the notes before reading the "
                     "profit as growth." % crore(ex))
    assoc, net = _v(r, "associates"), _v(r, "net_profit")
    if assoc and net and abs(assoc) > 0.3 * abs(net):
        lines.append("Share of associates and joint ventures (%s) is a large part of net profit (%s)."
                     % (crore(assoc), crore(net)))
    own = _v(r, "owners_profit")
    if basis == "consolidated" and own is not None and net is not None and (own < 0) != (net < 0):
        lines.append("Net profit is %s but the owners' share is %s: minority shareholders of subsidiaries take "
                     "the difference. EPS follows the owners' share." % (crore(net), crore(own)))
    return lines


# ---------------------------------------------------------------- output

def crore(v):
    if v is None:
        return "n/a"
    return nse.inr(v / 1e7) + " cr"


def _c(v):
    if v is None:
        return "-"
    return ("-" if v < 0 else "") + nse.inr(abs(v) / 1e7)[3:]


def _pp(v):
    return "n/a" if v is None else "%+.1f%%" % v


def render(store, basis=None):
    bases = sorted(set(r["basis"] for r in store.get("periods", [])))
    if not bases:
        return "No results stored for %s yet.\n" % store["symbol"]
    basis = basis or ("consolidated" if "consolidated" in bases else bases[0])
    if basis not in bases:
        raise ResultsError("%s has no %s results in the store (it has %s)" % (store["symbol"], basis,
                                                                             ", ".join(bases)))
    fam = store.get("family") or "ind-as"
    out = ["%s (%s), %s results. Rs crore except EPS. Source: NSE results XBRL (%s)." % (
        store.get("name") or store["symbol"], store["symbol"], basis, fam), ""]
    out.append("%-10s %-23s %11s %11s %11s %11s %11s %8s %8s %8s" % (
        "period", "dates", "revenue", "tot income", "PBT", "net profit", "owners", "EPS dil", "rev QoQ", "rev YoY"))
    for c in compare(store, basis):
        r = c["row"]
        out.append("%-10s %-23s %11s %11s %11s %11s %11s %8s %8s %8s%s" % (
            fiscal_label(r["start"], r["end"]), "%s..%s" % (r["start"], r["end"]), _c(_v(r, "revenue")),
            _c(_v(r, "total_income")), _c(_v(r, "pbt")), _c(_v(r, "net_profit")), _c(_v(r, "owners_profit")),
            "-" if _v(r, "eps_diluted") is None else "%.2f" % _v(r, "eps_diluted"),
            _pp(pct(_v(r, "revenue"), _v(c["prev"], "revenue"))) if c["prev"] else "-",
            _pp(pct(_v(r, "revenue"), _v(c["year_ago"], "revenue"))) if c["year_ago"] else "-",
            "  derived" if r.get("derived") else ""))
    out += ["", "What changed:"]
    for line in headline(store, basis):
        out.append("  - " + line)
    others = [b for b in bases if b != basis]
    if others:
        out.append("")
        out.append("Also stored: %s results (--basis %s)." % (others[0], others[0]))
    srcs = sorted(set(r["source"]["xbrl"] for r in store["periods"] if r["basis"] == basis
                      and r.get("source", {}).get("xbrl") and not r.get("derived")))
    if srcs:
        out.append("")
        out.append("Filings: " + "  ".join(srcs[-4:]))
    if any(r.get("derived") for r in store["periods"] if r["basis"] == basis):
        out.append("Derived quarters are the half-year to date minus the filed quarter; derived EPS is approximate.")
    return "\n".join(out) + "\n"


def metrics_row(store, basis=None):
    comp_basis = basis or ("consolidated" if any(r["basis"] == "consolidated" for r in store["periods"])
                           else "standalone")
    comp = [c for c in compare(store, comp_basis) if not c["row"].get("derived")]
    if not comp:
        return None
    c = next((x for x in comp if x["row"]["kind"] == "quarter"), comp[0])
    r = c["row"]
    rev, net = _v(r, "revenue"), _v(r, "net_profit")
    return {"symbol": store["symbol"], "name": store.get("name"), "basis": comp_basis, "family": store.get("family"),
            "period": fiscal_label(r["start"], r["end"]), "period_start": r["start"], "period_end": r["end"],
            "revenue": rev, "total_income": _v(r, "total_income"), "pbt": _v(r, "pbt"), "net_profit": net,
            "owners_profit": _v(r, "owners_profit"), "eps_basic": _v(r, "eps_basic"),
            "eps_diluted": _v(r, "eps_diluted"),
            "net_margin": round(100.0 * net / rev, 1) if rev and net is not None else None,
            "revenue_qoq": pct(rev, _v(c["prev"], "revenue")) if c["prev"] else None,
            "revenue_yoy": pct(rev, _v(c["year_ago"], "revenue")) if c["year_ago"] else None,
            "source": (r.get("source") or {}).get("xbrl")}


# ---------------------------------------------------------------- fetching

def check_url(url):
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or u.netloc not in ALLOWED_HOSTS:
        raise ResultsError("only https links on %s are fetched, not %s" % (" or ".join(ALLOWED_HOSTS), url))
    return url


class Source(object):
    """Network (plain GET, one request at a time with a pause) or saved files (--offline DIR, repeatable)."""

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
            raise ResultsError("no saved copy of %s in %s" % (name, ", ".join(self.offline)))
        check_url(url)
        if self.calls and self.pause:
            time.sleep(self.pause)
        self.calls += 1
        try:
            return nse.http_get(url, timeout=60)
        except nse.NSEError as e:
            raise ResultsError(str(e))


def read_filing(ref, src):
    """A URL (fetched) or a local file path."""
    if re.match(r"^https?://", ref):
        return parse_xbrl(src.get(ref), ref)
    if not os.path.exists(ref):
        raise ResultsError("no such file %s (give a local XBRL file or an NSE archive link)" % ref)
    return parse_xbrl(nse.read_maybe_gz(ref), os.path.abspath(ref))


def parse_rss(data):
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ResultsError("the RSS feed is not valid XML: %s" % e)
    items = []
    for it in root.iter("item"):
        get = lambda k: (it.findtext(k) or "").strip()  # noqa: E731
        link = fix_link(get("link").split("|")[0].strip())
        items.append({"title": " ".join(get("title").split()), "link": link, "description": get("description"),
                      "published": get("pubDate")})
    return items


def fix_link(link):
    """Integrated_Filing.xml links its XBRL as /corporate/<file>.xml, which answers 404; the file is
    under /corporate/xbrl/ (as Integrated_Filing_Financials.xml links it)."""
    m = re.match(r"^(https://(?:nse)?archives\.nseindia\.com/corporate/)(INTEGRATED_FILING_[^/]+\.xml)$", link)
    return m.group(1) + "xbrl/" + m.group(2) if m else link


def rss_time(text):
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M"):
        try:
            return nse.iso_ist(datetime.datetime.strptime(text.strip(), fmt))
        except ValueError:
            continue
    return text


def _norm(name):
    s = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower().replace("&", " and "))
    words = [w for w in s.split() if w not in ("limited", "ltd", "the", "co", "company")]
    return " ".join(words)


def name_index(equity):
    return dict((_norm(v["name"]), k) for k, v in (equity or {}).items())


def results_items(feeds, names):
    """XBRL results filings and results announcements from the feeds, newest first."""
    out, links = [], set()
    for feed, items in feeds.items():
        for it in items:
            link = it["link"]
            fname = link.rsplit("/", 1)[-1]
            if link in links:
                continue
            links.add(link)
            if feed == "announcements":
                subject = it["description"].rsplit("SUBJECT:", 1)[-1].strip().lower()
                if subject not in ("outcome of board meeting", "financial results updates") \
                        or not re.search(r"financial results?", it["description"], re.I):
                    continue
                kind_ = "announcement"
            else:
                if not link.lower().endswith(".xml") or "GOVERNANCE" in fname:
                    continue
                kind_ = "xbrl"
            sym = names.get(_norm(it["title"]))
            m = re.match(r"^\s*([A-Z0-9&-]+)\s*:", it["description"])
            if not sym and m:
                sym = m.group(1)
            fam = None
            for tag, label in (("BANKING", "bank"), ("NBFC", "nbfc"), ("NONINDAS", "non-ind-as"),
                               ("INSURANCE", "insurer"), ("INDAS", "ind-as")):
                if tag in fname:
                    fam = label
                    break
            out.append({"feed": feed, "kind": kind_, "company": it["title"], "symbol": sym, "link": link,
                        "family": fam, "note": it["description"][:200], "published": rss_time(it["published"])})
    out.sort(key=lambda x: x["published"], reverse=True)
    return out


# ---------------------------------------------------------------- commands

def _watch_symbols(base=None):
    syms = []
    for name in ("watchlist.json", "paper.json", "plan.json"):
        doc = nse.read_json(os.path.join(nse.trader_dir(base), name), None)
        for s in _symbols_in(doc):
            if s not in syms:
                syms.append(s)
    return syms


def _symbols_in(node, depth=0):
    out = []
    if depth > 4:
        return out
    if isinstance(node, dict):
        for k in ("symbol", "ticker"):
            if isinstance(node.get(k), str):
                out.append(node[k])
        for v in node.values():
            if isinstance(v, (list, dict)):
                out += _symbols_in(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            out += [v] if isinstance(v, str) else _symbols_in(v, depth + 1)
    clean = []
    for s in out:
        try:
            c = nse.clean_symbol(s)
        except nse.NSEError:
            continue
        if c not in clean:
            clean.append(c)
    return clean


def one_line(f):
    p = next((x for x in f["periods"] if x["kind"] == "quarter"), f["periods"][0])
    v = p["values"]
    owners = f["basis"] == "consolidated" and v.get("owners_profit") is not None
    prof = v.get("owners_profit") if owners else v.get("net_profit")
    return ("%s %s %s: revenue %s, PBT %s, %s %s, diluted EPS %s" % (
        f["symbol"], fiscal_label(p["start"], p["end"]), f["basis"], crore(v.get("revenue")), crore(v.get("pbt")),
        "owners' profit" if owners else "net profit", crore(prof),
        "-" if v.get("eps_diluted") is None else "Rs %.2f" % v["eps_diluted"]))


def cmd_latest(a):
    base = a.trader_dir
    src = Source(a.offline)
    feeds = {}
    errors = []
    for name in ("integrated", "integrated_all", "legacy", "announcements"):
        if name == "announcements" and a.no_announcements:
            continue
        try:
            feeds[name] = parse_rss(src.get(FEEDS[name]))
        except ResultsError as e:
            errors.append("%s feed: %s" % (name, e))
    try:
        equity = nse.equity_list(base, offline=a.equity_list)
    except nse.NSEError as e:
        equity = {}
        errors.append("NSE equity list (for company names): %s" % e)
    items = results_items(feeds, name_index(equity))
    want = set(nse.clean_symbol(s) for s in (a.symbol or []))
    if a.watchlist:
        want |= set(_watch_symbols(base))
        if not want:
            raise ResultsError("no symbols in trader/watchlist.json or trader/paper.json; use --symbol")
    if want:
        items = [i for i in items if i["symbol"] in want]
    state_path = os.path.join(nse.trader_dir(base), "state", "results.json")
    state = nse.read_json(state_path, {}) or {}
    seen = state.setdefault("seen", {})
    now = nse.parse_now(a.now)
    new = [i for i in items if a.all or (i["link"] + "|" + (i["symbol"] or "")) not in seen]
    fetch = a.fetch or a.save or bool(want)
    shown = 0
    out_rows = []
    for i in new:
        row = dict(i)
        if i["kind"] == "xbrl" and fetch and shown < a.limit:
            try:
                f = parse_xbrl(src.get(i["link"]), i["link"])
                row["summary"] = one_line(f)
                row["symbol"] = f["symbol"] or row["symbol"]
                if a.save and f["symbol"]:
                    st = load_store(f["symbol"], base)
                    merge(st, f, now)
                    save_store(st, base)
                    row["saved"] = store_path(f["symbol"], base)
            except (ResultsError, nse.NSEError) as e:
                row["error"] = str(e)
            shown += 1
        out_rows.append(row)
        seen[i["link"] + "|" + (i["symbol"] or "")] = nse.iso_ist(now)
    if not a.dry_run:
        state["last_check"] = nse.iso_ist(now)
        nse.write_json(state_path, state)
    if a.json:
        print(json.dumps({"new": out_rows, "errors": errors}, indent=2))
        return 0 if out_rows else 1
    if not out_rows:
        print("No new results filings%s since the last check (%d in the feeds)." % (
            " for " + ", ".join(sorted(want)) if want else "", len(items)))
    for r in out_rows:
        tag = "RESULTS" if r["kind"] == "xbrl" else "ANNOUNCED"
        print("%s %s %s%s  %s" % (tag, r["published"][:16].replace("T", " "), r["company"],
                                  " (%s)" % r["symbol"] if r["symbol"] else "", r["link"]))
        if r.get("summary"):
            print("    " + r["summary"])
        if r.get("saved"):
            print("    saved to %s" % r["saved"])
        if r.get("error"):
            print("    could not read it: " + r["error"])
        if r["kind"] == "announcement":
            print("    results announced (board-meeting outcome); the XBRL usually follows in the integrated "
                  "filing feed")
    if out_rows and not fetch:
        print("Add --save to read each XBRL and keep it in trader/results/, or show SYMBOL --xbrl <link>.")
    for e in errors:
        print("error: " + e)
    return 0 if out_rows else 1


def cmd_show(a):
    base = a.trader_dir
    sym = nse.clean_symbol(a.symbol)
    store = load_store(sym, base)
    src = Source(a.offline)
    for ref in a.xbrl or []:
        f = read_filing(ref, src)
        if f["symbol"] and nse.clean_symbol(f["symbol"]) != sym:
            raise ResultsError("%s is a %s filing, not %s" % (ref, f["symbol"], sym))
        n = merge(store, f, nse.parse_now(a.now))
        print("Read %s (%s, %s): %d period(s)." % (ref.rsplit("/", 1)[-1], f["basis"], f["family"], n))
    if a.xbrl and not a.no_save:
        save_store(store, base)
    if not store.get("periods"):
        print("No results stored for %s yet. Run `latest --symbol %s --save` after it files, or "
              "`show %s --xbrl <link or file>` with a results XBRL from NSE." % (sym, sym, sym))
        return 1
    if a.json:
        print(json.dumps(store, indent=2))
    else:
        sys.stdout.write(render(store, a.basis))
    if a.metrics:
        doc = nse.read_json(a.metrics, None) or {"companies": []}
        row = metrics_row(store, a.basis)
        doc["companies"] = [c for c in doc.get("companies", []) if c.get("symbol") != sym] + ([row] if row else [])
        doc["as_of"] = nse.parse_now(a.now).date().isoformat()
        nse.write_json(a.metrics, doc)
        print("wrote %s (%d companies)" % (a.metrics, len(doc["companies"])))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="NSE quarterly results reader (india-analyst).")
    ap.add_argument("--trader-dir", help="the trader/ folder (default $TRADER_DIR or ./trader)")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("latest", help="new results filings in NSE's feeds since the last check")
    p.add_argument("--symbol", action="append", help="only this symbol (repeat)")
    p.add_argument("--watchlist", action="store_true", help="only symbols in trader/watchlist.json and paper.json")
    p.add_argument("--fetch", action="store_true", help="read each new XBRL and print its numbers")
    p.add_argument("--save", action="store_true", help="read each new XBRL and keep it in trader/results/")
    p.add_argument("--limit", type=int, default=10, help="read at most this many XBRL files (default 10)")
    p.add_argument("--all", action="store_true", help="show everything in the feeds, not only new items")
    p.add_argument("--no-announcements", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="do not record what was shown")
    p.add_argument("--offline", action="append", metavar="DIR", help="read saved feeds and XBRL files from DIR")
    p.add_argument("--equity-list", metavar="FILE", help="a saved EQUITY_L.csv (company name -> symbol)")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="one company's stored quarters, optionally adding filings first")
    p.add_argument("symbol")
    p.add_argument("--xbrl", action="append", metavar="URL_OR_FILE", help="a results XBRL to read (repeat)")
    p.add_argument("--basis", choices=["consolidated", "standalone"])
    p.add_argument("--no-save", action="store_true")
    p.add_argument("--metrics", metavar="FILE", help="merge one row for this company into a JSON file")
    p.add_argument("--offline", action="append", metavar="DIR")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"latest": cmd_latest, "show": cmd_show}[a.cmd](a)
    except (ResultsError, nse.NSEError) as e:
        print("results.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
