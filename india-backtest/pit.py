#!/usr/bin/env python3
"""Point-in-time (PIT) universe and corporate-action-adjusted daily prices for india-backtest swing,
built only from NSE's own capital-market bhavcopy (run as `backtest.py swing pit-fetch | pit-build`).
Standard library only. Removes the survivorship bias of applying today's Nifty 100 / 500 list to
2015-2026.

    python3 backtest.py swing pit-fetch [--from 2013-11-01] [--to 2026-10-29]   (resumable, ~1 s a day)
    python3 backtest.py swing pit-build                                       (universe + adjusted series)
    python3 backtest.py swing compare --universe pit100                       (then pit500)

Source. One file per trading day from nsearchives.nseindia.com (a plain GET with nse.UA, never the
cookie API): the legacy `cmDDMONYYYYbhav.csv.zip` up to 2024-07-05, UDiFF
`BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip` from 2024-07-08 (nse.cm_bhav_url / nse.parse_cm_bhav).
A 404 is a holiday (remembered in _missing.json, except today's not-yet-published file). Only the
slim rows of series EQ, BE and BZ are kept, gzipped JSON per day in trader/cache/cm_bhav/:
[symbol, series, open, high, low, close, prev_close, volume, value (Rs), isin]. Never the zips.

PIT universe (pit_universe.json). At every month's last session, every stock (an INE... ISIN: no ETFs,
no mutual-fund units, no partly-paid IN9... lines) whose series that day is EQ, with at least 200
sessions of bhavcopy history and a close of at least Rs 20, is ranked by the MEDIAN daily traded value
of its last 126 sessions (about 6 months; days it did not trade count as 0). pit100 = the top 100,
pit500 = the top 500. A stock is buyable on the signals of the following month only (the list is known
at that month-end close). Stocks that later delisted, merged, were suspended or collapsed stay in for
the months they qualified.

Symbol changes. NSE renames keep the ISIN (ZOMATO -> ETERNAL, MOTHERSUMI -> MOTHERSON ...); a split
changes the ISIN but keeps the symbol. A series is a chain of symbol-days: when a symbol stops and, on
its next session, a new symbol appears with the same ISIN whose PREVCLOSE equals the old last close (or
the new one carries the same ISIN within 5 sessions), the two are joined under the NEWEST symbol.

Corporate actions (bonus, split, consolidation, rights, demerger). The bhavcopy is unadjusted and, contrary
to what one might expect, so is its PREVCLOSE on ex-dates (checked on INFY's bonus 2014-12-02, RELIANCE's
bonuses 2017-09-07 and 2024-10-28 in legacy, Pd and UDiFF files, HDFCBANK's split 2019-09-19, IRCTC's split
2021-10-28 and the Tata Motors demerger 2025-10-14): prev close = the raw last close. So the adjustments
come from NSE's OWN corporate-action list, the Bc file in the daily PR zip (archives/equities/bhavcopy/
pr/PRddmmyy.zip, a plain GET; every 3rd session, `pit-fetch --ca`): symbol, ex-date and purpose. Bonus a:b,
split X->Y and consolidation use the purpose's exact ratio, accepted only when the ex-day open / last close
agrees with it within -30 % / +45 % (a listed action that did not happen that day is skipped and noted);
rights and demergers / schemes of arrangement use the ex-day open / last close (the price NSE's special
pre-open session discovers) when it fell at least 3 %. Every earlier OHLC is multiplied by the factor and
volume divided, as Yahoo does for splits. Dividends are NOT adjusted, so returns stay PRICE returns, like
the Yahoo path. An open 35 %+ from the last close with no listed action is reported, never adjusted (a
relisting after suspension is a real loss). `pit-check` compares every series Yahoo also has with
Yahoo's split-adjusted closes. A day missing from the archive cache would leave a hole: the build refuses
to run with weekday gaps, and sessions where many stocks' prev close disagrees with their last close (an
unlisted weekend session before them) are reported; `pit-fetch --probe-gaps` fetches those weekends.

Delistings. A series simply ends. A position still held when its stock has no more bars is closed at
its LAST available close (see swing.simulate: `delisted` exits). This is optimistic for companies that
failed (the real exit could have been far lower, or impossible: a stock frozen by suspension), and fair
for mergers / buy-outs (holders got about the last price in shares or cash). The count of such exits is
reported.

A backtest is not a forecast and never places an order. Not financial advice.
"""

import argparse
import bisect
import datetime
import gzip
import json
import math
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "india-common"))
import nse  # noqa: E402

KEEP_SERIES = ("EQ", "BE", "BZ")
FETCH_FROM = datetime.date(2013, 11, 1)
GAP = 1.05                      # seconds between requests (>= 1 s)
BACKOFF = (5, 15, 45, 120)
# weekend sessions NSE held (Budget days, Muhurat, special live sessions); a 404 there is harmless
WEEKEND_SESSIONS = ("2015-02-28", "2016-10-30", "2019-10-27", "2020-02-01", "2020-11-14", "2023-11-12",
                    "2024-01-20", "2024-03-02", "2024-05-18", "2025-02-01", "2026-02-01")
ADJ_TOL = 0.005                 # |prev_close / last close - 1| above this: that stock's previous session is missing
LOOKBACK = 126                  # sessions for the median traded value (~6 months)
MIN_HISTORY = 200               # sessions of history to qualify
MIN_PRICE = 20.0
BIG_GAP = 0.35                  # an open this far from the last close with no listed action: reported, not adjusted
SIZES = {"pit100": 100, "pit500": 500}


class PITError(nse.NSEError):
    pass


def cache_dir():
    return os.path.join(nse.trader_dir(), "cache", "cm_bhav")


def day_path(d):
    return os.path.join(cache_dir(), "%s.json.gz" % d)


def universe_path():
    return os.path.join(nse.trader_dir(), "cache", "pit_universe.json")


def series_dir():
    return os.path.join(nse.trader_dir(), "cache", "pit_daily")


def write_gz(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(gzip.compress(json.dumps(doc, separators=(",", ":")).encode("utf-8")))
    os.replace(tmp, path)


def read_gz(path):
    try:
        return json.loads(nse.read_maybe_gz(path))
    except (OSError, ValueError):
        return None


def slim(day, rows, fmt):
    out = [[r["symbol"], r["series"], r["open"], r["high"], r["low"], r["close"], r["prev_close"],
            int(r["volume"] or 0), int(round(r["value"] or 0.0)), r["isin"]] for r in rows]
    return {"date": day, "format": fmt, "fields": list(nse.CM_BHAV_FIELDS), "rows": out}


# ================================================================ fetch

def _http(url):
    """Swapped out in tests."""
    return nse.http_get(url, timeout=60, retries=0)


def candidate_days(d_from, d_to):
    out, d = [], d_from
    extra = set(WEEKEND_SESSIONS)
    while d <= d_to:
        if d.weekday() < 5 or d.isoformat() in extra:
            out.append(d)
        d += datetime.timedelta(days=1)
    return out


def fetch_day(d, log=None, sleep=time.sleep):
    """-> ('ok', doc) | ('holiday', None). Tries the date's format, then the other one, on a 404."""
    first = "udiff" if d >= nse.CM_BHAV_UDIFF_FROM else "legacy"
    fmts = (first, "legacy" if first == "udiff" else "udiff")
    if d < datetime.date(2024, 1, 1):
        fmts = ("legacy",)
    for fmt in fmts:
        url = nse.cm_bhav_url(d, fmt)
        last = None
        for attempt in range(len(BACKOFF) + 1):
            try:
                raw = _http(url)
                sleep(GAP)
                day, rows = nse.parse_cm_bhav(raw, KEEP_SERIES)
                if day and day != d.isoformat():
                    raise PITError("%s holds %s, not %s" % (url, day, d))
                return "ok", slim(d.isoformat(), rows, fmt)
            except nse.NSEError as e:
                sleep(GAP)
                last = e
                if "HTTP 404" in str(e):
                    break
                if isinstance(e, PITError):
                    raise
                if attempt < len(BACKOFF):
                    if log:
                        log("  %s: %s; waiting %ds" % (d, str(e)[:80], BACKOFF[attempt]))
                    sleep(BACKOFF[attempt])
            except Exception as e:                             # a bad zip
                last = e
                sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
        else:
            raise PITError("%s: no answer (%s)" % (d, last))
    return "holiday", None


def missing_path():
    return os.path.join(cache_dir(), "_missing.json")


def cmd_fetch(a):
    d_from = datetime.date.fromisoformat(a.date_from) if a.date_from else FETCH_FROM
    now = nse.now_ist()
    d_to = datetime.date.fromisoformat(a.date_to) if a.date_to else now.date()
    log = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
    if a.ca:
        return cmd_fetch_ca(a, log)
    missing = nse.read_json(missing_path(), {}) or {}
    if a.probe_gaps:
        stats = {}
        sessions = cached_sessions()
        build_chains(iter_days(sessions), keep=set(), stats=stats)
        todo = []
        for d in suspect_sessions(stats):
            prev = sessions[sessions.index(d) - 1]
            x = datetime.date.fromisoformat(prev) + datetime.timedelta(days=1)
            while x.isoformat() < d:
                if not os.path.isfile(day_path(x.isoformat())) and missing.get(x.isoformat(), "").find("probed") < 0:
                    todo.append(x)
                x += datetime.timedelta(days=1)
        log("pit-fetch --probe-gaps: %d suspect sessions, probing %d calendar days before them: %s" % (
            len(suspect_sessions(stats)), len(todo), ", ".join(x.isoformat() for x in todo)))
        missing = dict((k, v) for k, v in missing.items() if k not in set(x.isoformat() for x in todo))
    else:
        todo = [d for d in candidate_days(d_from, d_to)
                if not os.path.isfile(day_path(d.isoformat())) and d.isoformat() not in missing]
    if a.limit:
        todo = todo[:a.limit]
    log("pit-fetch: %d days to fetch (%s to %s), about %.0f minutes" % (len(todo), d_from, d_to, len(todo) * 1.5 / 60))
    got = {"ok": 0, "holiday": 0, "failed": []}
    for k, d in enumerate(todo):
        try:
            how, doc = fetch_day(d, log)
        except nse.NSEError as e:
            got["failed"].append(d.isoformat())
            log("  %s FAILED %s" % (d, str(e)[:120]))
            continue
        got[how] += 1
        if how == "ok":
            write_gz(day_path(d.isoformat()), doc)
        elif d < now.date() or (d == now.date() and now.time() > datetime.time(23, 0)):
            missing[d.isoformat()] = "404 (holiday or no session)%s" % (", probed" if a.probe_gaps else "")
            nse.write_json(missing_path(), missing)
        if (k + 1) % 50 == 0 or k + 1 == len(todo):
            log("  [%d/%d] %s  ok %d, holidays %d, failed %d" % (k + 1, len(todo), d, got["ok"], got["holiday"],
                                                                 len(got["failed"])))
    print("pit-fetch: %d days fetched, %d holidays, %d failed%s" % (
        got["ok"], got["holiday"], len(got["failed"]), (": " + ", ".join(got["failed"][:20])) if got["failed"] else ""))
    return 0 if not got["failed"] else 1


# ================================================================ build: chains, adjustment, ranking

def cached_sessions(d_from=None, d_to=None):
    return [n[:10] for n in sorted(os.listdir(cache_dir())) if n.endswith(".json.gz")
            and not (d_from and n[:10] < d_from) and not (d_to and n[:10] > d_to)]


def iter_days(sessions):
    """(date, slim rows) for each cached session, read one at a time."""
    for d in sessions:
        doc = read_gz(day_path(d))
        if doc is None:
            raise PITError("cannot read %s" % day_path(d))
        yield d, doc["rows"]


def calendar_gaps(dates, missing):
    """Weekdays between the first and last cached session that are neither cached nor a known 404."""
    have = set(dates)
    if not dates:
        return []
    gaps = []
    for d in candidate_days(datetime.date.fromisoformat(dates[0]), datetime.date.fromisoformat(dates[-1])):
        k = d.isoformat()
        if d.weekday() < 5 and k not in have and k not in missing:
            gaps.append(k)
    return gaps


def build_chains(days, keep=None, stats=None):
    """Join symbol-days into one chain per company. days: iterable of (date, slim rows) in order.
    -> {chain_id: chain}, chain = {"symbols": [old .. newest], "isins": [...], "k": session numbers,
    "val": traded value, "cl": close, "eq": 1 if series EQ} (compact, for ranking) plus "rows"
    [[d, o, h, l, c, pc, v, value, series]] when keep is None or the chain id is in keep.
    Renames: a NEW symbol whose ISIN belongs to a chain that has not traded today, whose own symbol is
    absent today and that traded within the last 5 sessions continues that chain (deterministic, so a
    second pass gives the same ids). stats (a dict) gets {date: [stocks compared, stocks whose prev_close
    differs from their last close by > ADJ_TOL]}: a session where many differ means the session before
    it is missing from the cache (an unlisted weekend session), not a wave of corporate actions."""
    from array import array
    chains, by_sym, by_isin = {}, {}, {}
    sess = []
    for d, rows in days:
        sess.append(d)
        n = len(sess) - 1
        seen = {}
        for r in rows:
            sym, ser = r[0], r[1]
            if sym in seen and (seen[sym][1] == "EQ" or ser != "EQ"):
                continue
            seen[sym] = r
        today = set()
        for sym in sorted(seen):
            r = seen[sym]
            isin = r[9] or ""
            cid = by_sym.get(sym)
            if cid is None and isin and isin in by_isin:
                cand = by_isin[isin]
                ch = chains[cand]
                recent = len(sess) < 6 or ch["k"][-1] >= n - 5
                old = ch["symbols"][-1]
                if cand not in today and recent and old not in seen:
                    cid = cand
                    ch["symbols"].append(sym)
                    ch["segs"].append((sym, d))
                    by_sym.pop(old, None)
                    by_sym[sym] = cid
            if cid is None:
                cid = "%s@%s" % (sym, d)
                chains[cid] = {"symbols": [sym], "segs": [(sym, d)], "isins": [], "k": array("i"), "val": array("d"),
                               "cl": array("d"), "eq": bytearray()}
                if keep is None or cid in keep:
                    chains[cid]["rows"] = []
                by_sym[sym] = cid
            ch = chains[cid]
            if stats is not None and len(ch["cl"]) and r[6] and ch["k"][-1] >= n - 1:
                st = stats.setdefault(d, [0, 0])
                st[0] += 1
                if abs(r[6] / ch["cl"][-1] - 1) > ADJ_TOL:
                    st[1] += 1
            if isin and isin not in ch["isins"]:
                ch["isins"].append(isin)
            if isin:
                by_isin[isin] = cid
            ch["k"].append(n)
            ch["val"].append(float(r[8] or 0))
            ch["cl"].append(float(r[5]))
            ch["eq"].append(1 if r[1] == "EQ" else 0)
            if "rows" in ch:
                ch["rows"].append([d, r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[1]])
            today.add(cid)
    return chains


MARKET_WIDE = 0.15             # a session where more than 15 % of stocks "adjust" = a missing session before it


def suspect_sessions(stats, share=MARKET_WIDE, min_n=50):
    return sorted(d for d, (n, m) in stats.items() if n >= min_n and m > share * n)


def month_end_sessions(sessions, cfg=None):
    """Sessions that end their month. The last one counts too when NSE's calendar says the next session
    is in a new month (the month-end list is known at that close, before the next bhavcopy exists)."""
    out = [sessions[k] for k in range(len(sessions) - 1) if sessions[k + 1][:7] != sessions[k][:7]]
    if sessions:
        last = sessions[-1]
        nxt = nse.next_session(datetime.datetime.combine(datetime.date.fromisoformat(last), datetime.time(23, 59)), cfg)
        if nxt and nxt[0].date().isoformat()[:7] != last[:7]:
            out.append(last)
    return out


def rank_month(cands, sizes=None):
    """cands [(symbol, median value)] -> {name: [symbols]} by value desc, ties by symbol."""
    sizes = sizes or SIZES
    order = [s for s, _ in sorted(cands, key=lambda x: (-x[1], x[0]))]
    return dict((name, order[:n]) for name, n in sizes.items())


def pit_lists(chains, sessions, lookback=LOOKBACK, min_hist=MIN_HISTORY, min_price=MIN_PRICE, sizes=None):
    """chains {name: chain (compact arrays)} -> {month_end: {"pit100": [...], "pit500": [...], "eligible": n}},
    each list using only data up to that month-end's close. Eligible: an INE ISIN, series EQ that day, at
    least min_hist sessions in its chain so far, close >= min_price; ranked by the median traded value of
    the last `lookback` sessions (0 on sessions it did not trade)."""
    pos = dict((d, k) for k, d in enumerate(sessions))
    out = {}
    live = [(nm, ch) for nm, ch in chains.items() if any(i.startswith("INE") for i in ch["isins"])]
    for me in month_end_sessions(sessions):
        m = pos[me]
        if m + 1 < min_hist:
            continue
        cands = []
        for nm, ch in live:
            ks = ch["k"]
            if not ks or ks[0] > m or ks[-1] < m:
                continue
            j = bisect.bisect_right(ks, m) - 1
            if ks[j] != m or not ch["eq"][j] or ch["cl"][j] < min_price or j + 1 < min_hist:
                continue
            lo = bisect.bisect_left(ks, m - lookback + 1)
            vals = list(ch["val"][lo:j + 1]) + [0.0] * (lookback - (j + 1 - lo))
            cands.append((nm, statistics.median(vals)))
        lists = rank_month(cands, sizes)
        lists["eligible"] = len(cands)
        out[me] = lists
    return out


def unique_names(chains):
    """chain id -> output symbol: the newest symbol; a clash (a symbol reused later) gets @first-date."""
    by = {}
    for cid, ch in chains.items():
        by.setdefault(ch["symbols"][-1], []).append(cid)
    out = {}
    for sym, cids in by.items():
        cids.sort(key=lambda c: chains[c]["k"][-1])
        for k, cid in enumerate(cids):
            out[cid] = sym if k == len(cids) - 1 else "%s@%s" % (sym, cid.split("@")[1])
    return out


def cmd_build(a):
    log = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
    sessions = cached_sessions(a.date_from, a.date_to)
    if not sessions:
        raise PITError("no bhavcopies cached: run `backtest.py swing pit-fetch` first")
    gaps = calendar_gaps(sessions, nse.read_json(missing_path(), {}) or {})
    if gaps and not a.allow_gaps:
        raise PITError("%d weekdays neither cached nor a known holiday (%s ...): rerun pit-fetch"
                       % (len(gaps), ", ".join(gaps[:5])))
    log("pit-build: %d sessions %s to %s (pass 1: chains and ranking)" % (len(sessions), sessions[0], sessions[-1]))
    stats = {}
    chains = build_chains(iter_days(sessions), keep=set(), stats=stats)
    suspects = suspect_sessions(stats)
    if suspects:
        log("  WARNING: %d sessions where > %d %% of stocks' prev close differs from their last close (a missing "
            "session before them: run pit-fetch --probe-gaps): %s; not treated as corporate actions"
            % (len(suspects), 100 * MARKET_WIDE, ", ".join(suspects[:10])))
    names = unique_names(chains)
    log("  %d company chains (%d renamed)" % (len(chains), sum(1 for c in chains.values() if len(c["symbols"]) > 1)))
    lists = pit_lists(dict((names[c], ch) for c, ch in chains.items()), sessions)
    ever = {"pit100": set(), "pit500": set()}
    for me, L in lists.items():
        for u in ever:
            ever[u].update(L[u])
    log("  %d month-ends ranked; ever in pit100 %d, pit500 %d (pass 2: full rows)" % (
        len(lists), len(ever["pit100"]), len(ever["pit500"])))
    want = set(c for c, nm in names.items() if nm in ever["pit500"])
    actions = load_actions()
    if not actions:
        log("  WARNING: no corporate-action files cached (run pit-fetch --ca): prices stay UNADJUSTED")
    chains = build_chains(iter_days(sessions), keep=want)
    for cid in chains:
        chains[cid]["name"] = names[cid]
    os.makedirs(series_dir(), exist_ok=True)
    index = {}
    last_session = sessions[-1]
    for cid, ch in chains.items():
        nm = ch["name"]
        if cid not in want:
            continue
        acts = chain_actions(ch["segs"], actions)
        acts += yahoo_actions(nm, acts, ch["rows"][0][0])
        evs = ca_factors(ch["rows"], sorted(acts))
        bars = apply_factors(ch["rows"], evs)
        done = set(ev["k"] for ev in evs if ev.get("factor"))
        unexplained = []
        for k in range(1, len(ch["rows"])):
            r0, r1 = ch["rows"][k - 1], ch["rows"][k]
            g = r1[1] / r0[4] if r0[4] else 1.0
            if abs(g - 1) >= BIG_GAP and k not in done:
                unexplained.append([r1[0], round(g, 4)])
        events = [[ev["date"], ev["factor"], ev["kind"], ev["how"], ev["purpose"], ev["gap"]] for ev in evs]
        ser = {}
        for r in ch["rows"]:
            ser[r[8]] = ser.get(r[8], 0) + 1
        ended = ch["rows"][-1][0] < last_session
        doc = {"symbol": nm, "symbols": ch["symbols"], "isins": ch["isins"],
               "source": "NSE CM bhavcopy, prices back-adjusted from NSE's corporate-action (Bc) files (not dividends)",
               "first": bars[0][0], "last": bars[-1][0], "ended": ended, "series_days": ser,
               "events": events, "unexplained_gaps": unexplained, "bars": bars}
        write_gz(os.path.join(series_dir(), nm + ".json.gz"), doc)
        index[nm] = {"first": doc["first"], "last": doc["last"], "ended": ended, "symbols": ch["symbols"],
                     "events": events, "unexplained_gaps": unexplained, "series_days": ser,
                     "months_pit100": sum(1 for L in lists.values() if nm in L["pit100"]),
                     "months_pit500": sum(1 for L in lists.values() if nm in L["pit500"])}
    uni = {"made": nse.iso_ist(nse.now_ist().replace(microsecond=0)),
           "method": ("At each month's last session: series-EQ stocks with an INE ISIN, >= %d sessions of history, "
                      "close >= Rs %g, ranked by the median daily traded value of the last %d sessions (0 on days "
                      "not traded); pit100 = top 100, pit500 = top 500. Applies to signals from that close until "
                      "the next month-end." % (MIN_HISTORY, MIN_PRICE, LOOKBACK)),
           "source": "NSE CM bhavcopy (legacy cm*bhav.csv.zip to 2024-07-05, UDiFF BhavCopy_NSE_CM_* after)",
           "sessions": [sessions[0], sessions[-1], len(sessions)], "gaps": gaps, "suspect_sessions": suspects,
           "pit100": dict((me, L["pit100"]) for me, L in sorted(lists.items())),
           "pit500": dict((me, L["pit500"]) for me, L in sorted(lists.items())),
           "eligible": dict((me, L["eligible"]) for me, L in sorted(lists.items()))}
    nse.write_json(universe_path(), uni)
    nse.write_json(os.path.join(series_dir(), "_index.json"), index)
    ended = dict((u, sorted(s for s in ever[u] if index[s]["ended"])) for u in ever)
    n_adj = sum(1 for v in index.values() for e in v["events"] if e[1])
    n_skip = sum(1 for v in index.values() for e in v["events"] if not e[1])
    n_unx = sum(len(v["unexplained_gaps"]) for v in index.values())
    print("pit-build: %d sessions; %d month-end lists; ever in pit100 %d (%d no longer trading), pit500 %d (%d no "
          "longer trading); %d corporate-action adjustments (%d listed actions with no price effect); %d unexplained "
          "open gaps >= %d %% left as is; wrote %s and %s/" % (
              len(sessions), len(lists), len(ever["pit100"]), len(ended["pit100"]), len(ever["pit500"]),
              len(ended["pit500"]), n_adj, n_skip, n_unx, 100 * BIG_GAP, universe_path(), series_dir()))
    return 0


# ================================================================ corporate actions (NSE's Bc files)
# The bhavcopy's PREVCLOSE is NOT adjusted on ex-dates (checked: INFY bonus 2014-12-02, RELIANCE bonus
# 2017-09-07 and 2024-10-28 (legacy, Pd and UDiFF alike), HDFCBANK split 2019-09-19, IRCTC split
# 2021-10-28, TATAMOTORS demerger 2025-10-14: prev close = the unadjusted last close every time). So
# the adjustments come from NSE's own corporate-action list: the Bc file inside the daily PR zip
# (archives/equities/bhavcopy/pr/PRddmmyy.zip, a plain GET), one row per action: series, symbol,
# ex-date, purpose ("BONUS 1:1", "FV SPLT FRM RS 10 TO RS 2", "RIGHTS 1:5 @ PREMIUM RS 85",
# "SCHEME OF ARRANGEMENT" ...). Each file lists ex-dates from about a week back to three weeks ahead
# and listed companies must announce a record date at least 7 working days before it, so every 3rd
# session's file catches every action.

import re  # noqa: E402

PR_URL = "https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR%d%m%y.zip"
CA_EVERY = 3
SPLIT_RE = re.compile(r"(?:SPLIT|SPLT|FV ?SPL\b|SUB[- ]?DIVISION)\D*?(\d+(?:\.\d+)?)\D+?(?:TO|-)\s*(?:RS|RE|INR)?\.?\s*(\d+(?:\.\d+)?)")
CONSOL_RE = re.compile(r"CONSOLIDAT\D*?(\d+(?:\.\d+)?)\D+?TO\s*(?:RS|RE|INR)?\.?\s*(\d+(?:\.\d+)?)")
BONUS_RE = re.compile(r"\bBON(?:US)?\s*(?:ISSUE)?\s*(\d+)\s*:\s*(\d+)")
YAHOO_RE = re.compile(r"^YAHOO SPLIT (\d+(?:\.\d+)?):(\d+(?:\.\d+)?)$")
MARKET_KINDS = (("rights", re.compile(r"RIGHT")),
                ("demerger", re.compile(r"DEMERGER|DE-MERGER|SPIN|ARRANGEMENT|ARNGMNT|REDUCTION|REDCTN|RESTRUCTUR")))
SPLIT_WORD = re.compile(r"SPLIT|SPLT|FV ?SPL\b|SUB[- ]?DIV")


def snap_ratio(g, tol=0.12):
    """The ex-day gap g of a split / bonus whose ratio was cut off in the 25-character purpose field ->
    the nearest clean ratio (1/n, or b/(a+b) for a bonus a:b with a, b <= 10) within tol, else None."""
    best = None
    for a in range(1, 11):
        for b in range(1, 11):
            for f in (b / float(a + b), 1.0 / (a + 1) if b == 1 else None):
                if f and (best is None or abs(g / f - 1) < abs(g / best - 1)):
                    best = f
    for n in (12, 15, 20, 25, 50, 100):
        if abs(g * n - 1) < abs(g / best - 1):
            best = 1.0 / n
    return best if best and abs(g / best - 1) <= tol else None


def ca_path(d):
    return os.path.join(nse.trader_dir(), "cache", "cm_ca", "%s.json.gz" % d)


def parse_date_any(text):
    t = (text or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y"):
        try:
            return datetime.datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def parse_bc(data):
    """A Bc csv (text or the PR zip's bytes) -> [[symbol, series, ex_date, purpose]] for EQ/BE/BZ rows."""
    import csv
    import io
    import zipfile
    if isinstance(data, bytes):
        if data[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = [n for n in z.namelist() if os.path.basename(n).lower().startswith("bc") and n.lower().endswith(".csv")]
                if not names:
                    raise PITError("no Bc file in the PR zip")
                data = z.read(names[0])
        data = data.decode("latin-1")
    out = []
    for r in csv.DictReader(io.StringIO(data.lstrip("﻿"))):
        r = dict(((k or "").strip(), (v or "").strip()) for k, v in r.items() if k)
        if r.get("SERIES") not in KEEP_SERIES:
            continue
        ex = parse_date_any(r.get("EX_DT"))
        if not ex:
            continue
        out.append([r.get("SYMBOL", ""), r["SERIES"], ex, " ".join(r.get("PURPOSE", "").upper().split())])
    return out


def ca_kinds(purpose):
    """Purpose text -> [(kind, factor or None)]. factor multiplies the prices BEFORE the ex-date:
    bonus a:b -> b / (a + b); split from X to Y -> Y / X; consolidation X to Y -> Y / X (> 1); rights and
    demergers / arrangements -> None (taken from the market: the ex-date open / the last close)."""
    p = " ".join((purpose or "").upper().split())
    out = []
    m = YAHOO_RE.match(p)
    if m:                                        # route (b): Yahoo's split event "new:old" (bonuses included)
        new, old = float(m.group(1)), float(m.group(2))
        return [("split/bonus %g:%g (Yahoo)" % (new, old), old / new)] if new > 0 and old > 0 else []
    for m in BONUS_RE.finditer(p):
        a, b = int(m.group(1)), int(m.group(2))
        if a > 0 and b > 0:
            out.append(("bonus %d:%d" % (a, b), b / float(a + b)))
    m = SPLIT_RE.search(p)
    if m and "CONSOLIDAT" not in p:
        x, y = float(m.group(1)), float(m.group(2))
        if x > 0 and 0 < y < x:
            out.append(("split %g->%g" % (x, y), y / x))
    m = CONSOL_RE.search(p)
    if m:
        x, y = float(m.group(1)), float(m.group(2))
        if x > 0 and y > x:
            out.append(("consolidation %g->%g" % (x, y), y / x))
    kinds = [k for k, _ in out]
    if SPLIT_WORD.search(p) and not any(k.startswith(("split", "consolidation")) for k in kinds):
        out.append(("split (ratio cut off)", None))
    if re.search(r"\bBON(US)?\b", p) and not any(k.startswith("bonus") for k in kinds):
        out.append(("bonus (ratio cut off)", None))
    for kind, rx in MARKET_KINDS:
        if rx.search(p) and not out:
            out.append((kind, None))
    return out


def fetch_ca_day(d, sleep=time.sleep):
    """-> ('ok', rows) | ('holiday', None) for the PR zip of date d."""
    url = d.strftime(PR_URL)
    last = None
    for attempt in range(len(BACKOFF) + 1):
        try:
            raw = _http(url)
            sleep(GAP)
            return "ok", parse_bc(raw)
        except nse.NSEError as e:
            sleep(GAP)
            last = e
            if "HTTP 404" in str(e):
                return "holiday", None
            if attempt < len(BACKOFF):
                sleep(BACKOFF[attempt])
        except Exception as e:                                 # a bad zip
            last = e
            sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
    raise PITError("%s: no answer (%s)" % (url, last))


def cmd_fetch_ca(a, log):
    sessions = cached_sessions(a.date_from, a.date_to)
    want = sessions[::CA_EVERY] + sessions[-1:]
    todo = [d for d in want if not os.path.isfile(ca_path(d))]
    log("pit-fetch --ca: %d of %d PR zips to fetch (every %d sessions), about %.0f minutes" % (
        len(todo), len(want), CA_EVERY, len(todo) * 1.6 / 60))
    got = {"ok": 0, "holiday": 0, "failed": []}
    for k, d in enumerate(todo):
        try:
            how, rows = fetch_ca_day(datetime.date.fromisoformat(d))
        except nse.NSEError as e:
            got["failed"].append(d)
            log("  %s FAILED %s" % (d, str(e)[:100]))
            continue
        got[how] += 1
        if how == "ok":
            write_gz(ca_path(d), {"date": d, "fields": ["symbol", "series", "ex_date", "purpose"], "rows": rows})
        if (k + 1) % 50 == 0 or k + 1 == len(todo):
            log("  [%d/%d] %s ok %d, missing %d, failed %d" % (k + 1, len(todo), d, got["ok"], got["holiday"],
                                                               len(got["failed"])))
    print("pit-fetch --ca: %d PR zips, %d missing, %d failed" % (got["ok"], got["holiday"], len(got["failed"])))
    return 0 if not got["failed"] else 1


def load_actions():
    """Every cached Bc row, deduplicated -> {symbol: {ex_date: [purpose, ...]}} (dividends, AGMs, interest
    and redemption rows dropped: they do not change the share count)."""
    d = os.path.join(nse.trader_dir(), "cache", "cm_ca")
    out = {}
    if not os.path.isdir(d):
        return out
    for n in sorted(os.listdir(d)):
        if not n.endswith(".json.gz"):
            continue
        doc = read_gz(os.path.join(d, n)) or {}
        for sym, ser, ex, purpose in doc.get("rows", []):
            if not ca_kinds(purpose):
                continue
            L = out.setdefault(sym, {}).setdefault(ex, [])
            if purpose not in L:
                L.append(purpose)
    return out


def yahoo_actions(sym, acts, d0, ydir=None):
    """Route (b): Yahoo's split events (bonuses come as splits) for a symbol Yahoo knows under the same
    name (trader/cache/daily/SYM.json.gz), for dates where NSE's Bc files listed no bonus / split within
    5 days (a Bc file the every-3rd-session sampling missed). Validated by the ex-day gap like the rest."""
    ydir = ydir or os.path.join(nse.trader_dir(), "cache", "daily")
    doc = read_gz(os.path.join(ydir, sym + ".json.gz"))
    out = []
    for d, ratio in (doc or {}).get("splits") or []:
        if d < d0 or not ratio or ":" not in ratio:
            continue
        near = [a for a in acts if abs((datetime.date.fromisoformat(a[0]) - datetime.date.fromisoformat(d)).days) <= 5
                and any(kd[1] is not None or "cut off" in kd[0] for p in a[2] for kd in ca_kinds(p))]
        if not near:
            out.append((d, sym, ["YAHOO SPLIT %s" % ratio]))
    return out


def chain_actions(segs, actions):
    """segs [(symbol, first date)] of one chain -> [(ex_date, symbol, [purposes])] for the dates that
    symbol was the chain's name."""
    out = []
    for k, (sym, d0) in enumerate(segs):
        d1 = segs[k + 1][1] if k + 1 < len(segs) else "9999"
        for ex, ps in (actions.get(sym) or {}).items():
            if d0 <= ex < d1 or (k == 0 and ex < d0):
                out.append((ex, sym, ps))
    return sorted(out)


def ca_factors(rows, acts, gap_tol=(0.7, 1.45), market_min=0.03):
    """rows: a chain's [[d, o, h, l, c, pc, v, ...]], acts: chain_actions(...). -> [event dicts] with the
    factor to apply to every bar BEFORE row k. Bonus / split / consolidation: the purpose's exact ratio,
    accepted when the ex-day open / last close is within gap_tol of it (else, if the stock barely moved,
    the action did not take effect that day: skipped, and noted). Rights / demergers: the ex-day open /
    last close when it fell at least market_min (3 %), else none."""
    dates = [r[0] for r in rows]
    events = []
    for ex, sym, ps in acts:
        k = bisect.bisect_left(dates, ex)
        if k <= 0 or k >= len(rows):
            continue
        if (datetime.date.fromisoformat(dates[k]) - datetime.date.fromisoformat(ex)).days > 10:
            continue
        gap = rows[k][1] / rows[k - 1][4] if rows[k - 1][4] else 1.0
        kinds = []
        for p in ps:
            for kd in ca_kinds(p):
                if kd[0] not in [x[0] for x in kinds]:
                    kinds.append(kd)
        exact = [kd for kd in kinds if kd[1] is not None]
        cut = [kd for kd in kinds if kd[1] is None and "cut off" in kd[0]]
        ev = {"date": dates[k], "ex_date": ex, "symbol": sym, "purpose": " | ".join(ps), "gap": round(gap, 4)}
        if cut:
            ev["kind"] = ", ".join(kd[0] for kd in kinds)
            f = snap_ratio(gap) if gap < 0.9 else None
            ev["factor"], ev["how"] = (round(f, 6), "ex-day gap snapped to a clean ratio") if f else (
                None, "skipped: ratio cut off and gap %.3f is not a clean ratio" % gap)
        elif exact:
            f = 1.0
            for _, x in exact:
                f *= x
            ev["kind"] = ", ".join(kd[0] for kd in exact)
            # the listed ex-date can be a session off the real one (T+1 record dates): look 2 sessions
            # either side for the open gap that matches the ratio best
            best = None
            for kk in range(max(1, k - 2), min(len(rows), k + 3)):
                g = rows[kk][1] / rows[kk - 1][4] if rows[kk - 1][4] else 1.0
                if gap_tol[0] <= g / f <= gap_tol[1] and (best is None or abs(math.log(g / f)) < abs(math.log(best[1] / f))):
                    best = (kk, g)
            if best:
                k, gap = best
                ev["date"], ev["gap"] = dates[k], round(gap, 4)
                ev["factor"], ev["how"] = round(f, 6), "purpose ratio" if "Yahoo" not in ev["kind"] else "Yahoo split event"
            else:
                f2 = snap_ratio(gap) if gap < 0.75 else None       # e.g. a bonus AND a split, one of them unlisted
                if f2:
                    ev["factor"], ev["how"] = round(f2, 6), "ex-day gap snapped to a clean ratio (listed %.3f did not match)" % f
                else:
                    ev["factor"], ev["how"] = None, "skipped: ex-day gap %.3f does not match %.3f" % (gap, f)
        else:
            ev["kind"] = ", ".join(kd[0] for kd in kinds)
            gc = rows[k][4] / rows[k - 1][4] if rows[k - 1][4] else 1.0
            how = "ex-day open / last close"
            if abs(gc / gap - 1) > 0.15:                # the open is not representative: use the close
                gap, how = gc, "ex-day close / last close (the open was not representative)"
                ev["gap"] = round(gap, 4)
            if gap <= 1 - market_min:
                ev["factor"], ev["how"] = round(gap, 6), how
            else:
                ev["factor"], ev["how"] = None, "no price effect (gap %.3f)" % gap
        ev["k"] = k
        if ev.get("factor") and any(e["k"] == k and e.get("factor") for e in events):
            ev["factor"], ev["how"] = None, "duplicate of an action already applied on %s" % dates[k]
        events.append(ev)
    return events


def apply_factors(rows, events):
    """Back-adjust: bars before each event's row k are multiplied by its factor (volumes divided)."""
    facs = {}
    for ev in events:
        if ev.get("factor"):
            facs[ev["k"]] = facs.get(ev["k"], 1.0) * ev["factor"]
    out = [None] * len(rows)
    cum = 1.0
    for k in range(len(rows) - 1, -1, -1):
        r = rows[k]
        out[k] = [r[0], round(r[1] * cum, 4), round(r[2] * cum, 4), round(r[3] * cum, 4), round(r[4] * cum, 4),
                  int(round((r[6] or 0) / cum))]
        if k in facs:
            cum *= facs[k]
    return out


# ================================================================ validation against Yahoo

def ratio_steps(pit_bars, y_rows, tol=0.03, d_from="2014-01-01", w=5):
    """Compare a PIT series with Yahoo's split-adjusted closes: r(d) = pit close / Yahoo close on common
    dates (Yahoo's zero-volume filler bars skipped). A corporate action handled the same way on both
    sides leaves r flat. A STEP is a lasting level shift: the median r of the w dates after differs from
    the median of the w dates before by more than tol (a one-day glitch on either side is not a step).
    -> (common dates, [[date, step ratio]], max |r / r_last - 1|)."""
    y = dict((r[0], r[4]) for r in y_rows if r[4] and not (r[5] == 0 and r[1] == r[2] == r[3] == r[4]))
    pts = [(b[0], b[4] / y[b[0]]) for b in pit_bars if b[0] in y and b[0] >= d_from and y[b[0]] > 0]
    if not pts:
        return 0, [], None
    rs = [r for _, r in pts]
    steps, k = [], w
    while k <= len(pts) - w:
        j = statistics.median(rs[k:k + w]) / statistics.median(rs[k - w:k])
        if abs(j - 1) > tol and abs(rs[k] / rs[k - 1] - 1) > tol:
            steps.append([pts[k][0], round(j, 4)])
            k += w
            continue
        k += 1
    last = statistics.median(rs[-w:])
    worst = max(abs(r / last - 1) for r in rs)
    return len(pts), steps, round(worst, 4)


def cmd_check(a):
    """pit-check: every PIT series that Yahoo also has (trader/cache/daily) -> how many line up exactly."""
    idx = nse.read_json(os.path.join(series_dir(), "_index.json")) or {}
    ydir = os.path.join(nse.trader_dir(), "cache", "daily")
    out = {"checked": 0, "clean": 0, "steps": {}, "worst": []}
    for sym in sorted(idx):
        yd = read_gz(os.path.join(ydir, sym + ".json.gz"))
        if not yd:
            continue
        doc = read_gz(os.path.join(series_dir(), sym + ".json.gz"))
        n, steps, worst = ratio_steps(doc["bars"], yd["bars"])
        if not n:
            continue
        out["checked"] += 1
        if not steps:
            out["clean"] += 1
        else:
            out["steps"][sym] = steps
        out["worst"].append([worst, sym])
    out["worst"] = sorted(out["worst"], reverse=True)[:25]
    nse.write_json(os.path.join(series_dir(), "_yahoo_check.json"), out)
    print("pit-check: %d PIT series also on Yahoo; %d line up with no step > 3 %% (%.1f %%); %d have steps: %s" % (
        out["checked"], out["clean"], 100.0 * out["clean"] / max(1, out["checked"]), len(out["steps"]),
        "; ".join("%s %s" % (k, v[:3]) for k, v in sorted(out["steps"].items())[:40])))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="backtest.py swing pit-*")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("pit-fetch", help="download NSE CM bhavcopies (slim, resumable)")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    p.add_argument("--limit", type=int)
    p.add_argument("--ca", action="store_true", help="fetch NSE's corporate-action (Bc) files instead")
    p.add_argument("--probe-gaps", action="store_true",
                   help="fetch the calendar days (weekends) before sessions whose prev closes say one is missing")
    p = sub.add_parser("pit-build", help="PIT universe + adjusted series from the cached bhavcopies")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    p.add_argument("--allow-gaps", action="store_true")
    sub.add_parser("pit-check", help="compare the adjusted series with Yahoo's split-adjusted closes")
    a = ap.parse_args(argv)
    if a.cmd == "pit-check":
        return cmd_check(a)
    if a.cmd == "pit-fetch":
        return cmd_fetch(a)
    if a.cmd == "pit-build":
        return cmd_build(a)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
