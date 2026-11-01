#!/usr/bin/env python3
"""Sector peers for india-research. Standard library only.

Two ways to say who moves with whom:
  * sector groups: NSE's own Nifty 500 list (ind_nifty500list.csv, the Industry column,
    e.g. "Power", "Financial Services", "Information Technology");
  * correlation: Pearson correlation of daily close-to-close returns from the cached
    daily bars (trader/cache/bars/<source>/<SYM>-1d.json.gz, or Yahoo / bar-store files
    in a folder), on the days both stocks traded. Dates inside any dataset's locked
    holdout are left out by default, so peer choices never lean on holdout data.
"""

import csv
import io
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "india-common"))
sys.path.insert(0, os.path.join(HERE, "..", "india-backtest"))
import nse  # noqa: E402
import backtest as bt  # noqa: E402

MIN_OVERLAP = 40


def parse_sectors(text):
    """Nifty 500 list CSV -> ({symbol: industry}, {industry: [symbols]}), series EQ only."""
    sym2ind, groups = {}, {}
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        s = (r.get("Symbol") or "").strip()
        ind = (r.get("Industry") or "").strip()
        if not s or (r.get("Series") or "EQ").strip() != "EQ":
            continue
        sym2ind[s] = ind
        groups.setdefault(ind, []).append(s)
    for v in groups.values():
        v.sort()
    return sym2ind, groups


def load_sectors(list_file=None, base=None, allow_download=True):
    """From a file, else trader/cache/ind_nifty500list.csv (downloaded from NSE's archive if missing)."""
    if list_file:
        return parse_sectors(nse.read_maybe_gz(list_file).decode("utf-8-sig", "replace"))
    path = os.path.join(nse.trader_dir(base), "cache", "ind_nifty500list.csv")
    if os.path.isfile(path):
        with open(path, encoding="utf-8-sig") as fh:
            return parse_sectors(fh.read())
    if not allow_download:
        return {}, {}
    return parse_sectors(bt.index_list("nifty500"))


def daily_closes(dirs, sym, exclude=None):
    """{date: close} from the daily bars in these folders, minus excluded date ranges."""
    p = bt.find_offline(dirs, sym, "1d")
    if not p:
        return {}
    out = {}
    for b in bt._read_any(p):
        d = b["t"].date().isoformat()
        if exclude and any(a <= d <= z for a, z in exclude):
            continue
        out[d] = b["c"]
    return out


def returns(closes):
    ds = sorted(closes)
    return dict((ds[i], closes[ds[i]] / closes[ds[i - 1]] - 1) for i in range(1, len(ds)) if closes[ds[i - 1]])


def corr(a, b, min_overlap=MIN_OVERLAP):
    common = sorted(set(a) & set(b))
    if len(common) < min_overlap:
        return None, len(common)
    x = [a[d] for d in common]
    y = [b[d] for d in common]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sxy = sum((p - mx) * (q - my) for p, q in zip(x, y))
    sxx = sum((p - mx) ** 2 for p in x)
    syy = sum((q - my) ** 2 for q in y)
    if sxx <= 0 or syy <= 0:
        return None, len(common)
    return sxy / math.sqrt(sxx * syy), len(common)


def holdout_ranges(base=None):
    """Every locked holdout range in splits.json (peer choices must not lean on them)."""
    try:
        doc = nse.read_json(os.path.join(nse.trader_dir(base), "research", "splits.json"), {}) or {}
    except nse.NSEError:
        doc = {}
    return [tuple(r["holdout"]) for r in doc.values() if r.get("holdout")]


def available(dirs):
    out = set()
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            for suf in ("-1d.json.gz", "-1d.json", ".NS-1y1d.json.gz", ".NS-1y1d.json"):
                if n.endswith(suf):
                    s = n[: -len(suf)]
                    out.add(s[len("yahoo-"):] if s.startswith("yahoo-") else s)
    return sorted(out)


def correlated(sym, dirs, candidates=None, exclude=None, top=10, min_overlap=MIN_OVERLAP):
    """[(symbol, correlation, overlapping days)] most correlated first."""
    me = returns(daily_closes(dirs, sym, exclude))
    if not me:
        return []
    out = []
    for c in candidates if candidates is not None else available(dirs):
        if c == sym:
            continue
        r, n = corr(me, returns(daily_closes(dirs, c, exclude)), min_overlap)
        if r is not None:
            out.append((c, round(r, 3), n))
    out.sort(key=lambda x: (-x[1], x[0]))
    return out[:top] if top else out


def report(sym, sectors, groups, dirs, exclude=None, top=10):
    """Lines for `research.py peers SYM`."""
    sym = nse.clean_symbol(sym)
    ind = sectors.get(sym)
    L = []
    if ind:
        members = [s for s in groups.get(ind, []) if s != sym]
        L.append("%s: sector '%s' in NSE's Nifty 500 list, %d other member(s)." % (sym, ind, len(members)))
        rows = dict((c, (r, n)) for c, r, n in correlated(sym, dirs, members, exclude, top=0))
        for m in members:
            r = rows.get(m)
            L.append("  %-14s %s" % (m, ("corr %+.2f over %d days" % r) if r else "no cached daily bars to correlate"))
    else:
        L.append("%s is not in NSE's Nifty 500 list: no sector group." % sym)
    best = correlated(sym, dirs, None, exclude, top)
    if best:
        L.append("Most correlated among the %d stocks with cached daily bars (daily returns%s):" % (
            len(available(dirs)), ", holdout dates left out" if exclude else ""))
        for c, r, n in best:
            L.append("  %-14s corr %+.2f over %d days%s" % (c, r, n, ("  [%s]" % sectors.get(c)) if sectors.get(c) else ""))
    else:
        L.append("No cached daily bars for %s in %s: run india-backtest once to fill the cache." % (sym, ", ".join(dirs)))
    return L
