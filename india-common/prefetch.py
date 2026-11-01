#!/usr/bin/env python3
"""Frozen research dataset prefetch from Angel One SmartAPI. Standard library only. READ-ONLY.

Only the historical-candle route is ever called (plus login/logout); no order, modify, cancel or GTT
route is reachable from this file.

Writes the cache that india-backtest's `--source angel` reads, in exactly its format and naming:
    trader/cache/bars/angel/SYMBOL-5m.json.gz   {symbol, source, interval, token, covered, fetched, bars}
    trader/cache/bars/angel/SYMBOL-1d.json.gz   (daily; bars at 09:15 of the date; `covered` added)
with bars = [[iso naive IST start, o, h, l, c, v], ...]. Then trader/cache/research-dataset.json (manifest).

Run from the folder that holds .env and trader/ :
    python3 india-trader-skill/india-common/prefetch.py                 # fetch everything, resumable
    python3 india-trader-skill/india-common/prefetch.py --manifest-only
    python3 india-trader-skill/india-common/prefetch.py --only NIFTY,RELIANCE --skip-daily

Rate limits: one login, >= 3 s between calls, HTTP 403 / "exceeding access rate" -> sleep 60 s and retry
(max 4), a dead session re-logs in at most once every 5 minutes. Every chunk is written atomically and
chunks already covered are skipped, so a killed run resumes where it stopped.
"""

import argparse
import collections
import datetime
import gzip
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse  # noqa: E402

CHUNK_5M = 90            # calendar days per FIVE_MINUTE request (Angel documents 100)
CHUNK_1D = 1900          # calendar days per ONE_DAY request (Angel documents 2000)
GAP = 3.0
RATE_SLEEP = 60
RATE_RETRIES = 4
RELOGIN_EVERY = 300
START = datetime.date(2025, 4, 1)
END = datetime.date(2026, 10, 29)
DAILY_START = datetime.date(2015, 1, 1)
EXTRAS = ("TATAPOWER", "NTPC", "POWERGRID", "ADANIPOWER", "TATASTEEL")
N100_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv"
# cache symbol -> Angel scrip-master `name` (instrumenttype AMXIDX, exchange NSE)
INDICES = [("NIFTY", "Nifty 50", "99926000"), ("INDIAVIX", "India VIX", "99926017"),
           ("NIFTYBANK", "Nifty Bank", "99926009"), ("NIFTYIT", "Nifty IT", "99926008"),
           ("NIFTYAUTO", "Nifty Auto", "99926029"), ("NIFTYFMCG", "Nifty FMCG", "99926021"),
           ("NIFTYMETAL", "Nifty Metal", "99926030"), ("NIFTYPHARMA", "Nifty Pharma", "99926023"),
           ("NIFTYENERGY", "Nifty Energy", "99926020"), ("NIFTYREALTY", "Nifty Realty", "99926018"),
           ("NIFTYPSUBANK", "Nifty PSU Bank", "99926025"),
           ("NIFTYFINSERVICE", "Nifty Fin Service", "99926037")]
INTERVAL_NAME = {"5m": "FIVE_MINUTE", "1d": "ONE_DAY"}
SESSION_MARKERS = ("invalid token", "ag8001", "ab8050", "ab8051", "session", "unauthor", "401")
RATE_MARKERS = ("403", "exceeding access rate", "access denied", "429", "too many")


class Fatal(Exception):
    pass


class RateLimited(Exception):
    pass


# ---------------------------------------------------------------- pure helpers (tested offline)

def windows(a, b, n):
    """[a..b] split into calendar windows of at most n days, oldest first."""
    out = []
    while a <= b:
        e = min(b, a + datetime.timedelta(days=n - 1))
        out.append((a, e))
        a = e + datetime.timedelta(days=1)
    return out


def plan(cov, start, end, n):
    """Windows still to fetch given covered=[first, last] (ISO dates) or None.
    Forward part first (oldest -> newest), then backward part newest first."""
    if not cov:
        return list(reversed(windows(start, end, n)))
    c0, c1 = datetime.date.fromisoformat(cov[0]), datetime.date.fromisoformat(cov[1])
    fwd = windows(c1 + datetime.timedelta(days=1), end, n) if c1 < end else []
    back = list(reversed(windows(start, c0 - datetime.timedelta(days=1), n))) if c0 > start else []
    return fwd + back


def merge(old, new):
    d = dict((b["t"], b) for b in old or [])
    for b in new:
        d[b["t"]] = b
    return [d[t] for t in sorted(d)]


def read_store(path):
    try:
        with open(path, "rb") as fh:
            doc = json.loads(gzip.decompress(fh.read()))
    except (OSError, ValueError):
        return None
    doc["bars"] = [{"t": datetime.datetime.fromisoformat(r[0]), "o": r[1], "h": r[2], "l": r[3], "c": r[4],
                    "v": r[5]} for r in doc.get("bars") or []]
    return doc


def write_store(path, doc, bars):
    """Atomic: temp file then rename. Same bytes layout as backtest.write_store."""
    rows = [[b["t"].isoformat(), round(b["o"], 2), round(b["h"], 2), round(b["l"], 2), round(b["c"], 2),
             int(b.get("v") or 0)] for b in sorted(bars, key=lambda b: b["t"])]
    out = dict(doc, bars=rows)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(gzip.compress(json.dumps(out, separators=(",", ":")).encode("utf-8")))
    os.replace(tmp, path)


def bars_dir(base=None):
    return os.path.join(nse.trader_dir(base), "cache", "bars", "angel")


def store_path(sym, interval, base=None):
    return os.path.join(bars_dir(base), "%s-%s.json.gz" % (sym, interval))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def classify(err):
    m = str(err).lower()
    if any(k in m for k in RATE_MARKERS):
        return "rate"
    if any(k in m for k in SESSION_MARKERS):
        return "session"
    return "other"


# ---------------------------------------------------------------- requester

class Requester(object):
    """One Angel session, polite and read-only: only the candle route."""

    def __init__(self, client, gap=GAP, sleep=time.sleep, clock=time.time, log=print):
        self.client, self.gap, self._sleep, self._clock, self.log = client, gap, sleep, clock, log
        self.requests = 0
        self.rate_hits = 0
        self.logins = 0
        self._last = 0.0
        self._last_login = None

    def login(self):
        self.client.login()
        self.logins += 1
        self._last_login = self._clock()

    def candles(self, token, a, b, interval, exchange="NSE"):
        """Bars for dates a..b (inclusive). Raises RateLimited / Fatal."""
        body = {"exchange": exchange, "symboltoken": str(token), "interval": INTERVAL_NAME[interval],
                "fromdate": datetime.datetime.combine(a, nse.OPEN).strftime("%Y-%m-%d %H:%M"),
                "todate": datetime.datetime.combine(b, nse.CLOSE).strftime("%Y-%m-%d %H:%M")}
        rate_tries = 0
        while True:
            wait = self.gap - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = self._clock()
            self.requests += 1
            try:
                if self._last_login is None:
                    self.login()
                data = self.client.post("candles", body)
                return self._bars(data or [], interval)
            except Exception as e:  # AngelError / NSEError
                kind = classify(e)
                msg = self._scrub(str(e))[:160]
                if kind == "rate":
                    self.rate_hits += 1
                    rate_tries += 1
                    if rate_tries > RATE_RETRIES:
                        raise RateLimited(msg)
                    self.log("  rate limit (%s); sleeping %ds (%d/%d)" % (msg, RATE_SLEEP, rate_tries, RATE_RETRIES))
                    self._sleep(RATE_SLEEP)
                    self._last = self._clock()
                    continue
                if kind == "session":
                    since = self._clock() - (self._last_login or 0)
                    if since < RELOGIN_EVERY:
                        self._sleep(RELOGIN_EVERY - since)
                    self.log("  session error (%s); re-login" % msg)
                    try:
                        self.login()
                    except Exception as e2:
                        raise Fatal("re-login failed: %s" % self._scrub(str(e2))[:120])
                    rate_tries += 1
                    if rate_tries > RATE_RETRIES:
                        raise Fatal(msg)
                    continue
                raise Fatal(msg)

    def _scrub(self, s):
        try:
            import angel
            return angel.scrub(s)
        except Exception:
            return s

    @staticmethod
    def _bars(rows, interval):
        out = []
        for r in rows:
            t = datetime.datetime.fromisoformat(r[0])
            if t.tzinfo is not None:
                t = nse.ist_from_utc(t.astimezone(datetime.timezone.utc))
            if interval == "1d":
                t = datetime.datetime.combine(t.date(), nse.OPEN)
            out.append({"t": t, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
                        "v": int(r[5] or 0)})
        return out


# ---------------------------------------------------------------- fetch one symbol/interval

def fetch_symbol(req, sym, token, interval, start, end, state, base=None, now=None, learn_floor=False,
                 chunk=None, log=print):
    """Fill trader/cache/bars/angel/SYM-<interval>.json.gz over start..end, skipping covered chunks.
    state['floor'][interval] = first date Angel serves (learned from the index NIFTY): older chunks are skipped.
    Returns the number of requests made for this symbol."""
    n = chunk or (CHUNK_5M if interval == "5m" else CHUNK_1D)
    now = now or nse.now_ist()
    path = store_path(sym, interval, base)
    doc = read_store(path) or {"symbol": sym, "source": "angel", "interval": interval, "token": str(token),
                               "covered": None, "bars": []}
    bars, cov = doc["bars"], doc.get("covered")
    floor = (state.get("floor") or {}).get(interval)
    eff_start = max(start, datetime.date.fromisoformat(floor)) if floor else start
    made = 0
    for a, b in plan(cov, eff_start, end, n):
        forward = bool(cov) and a > datetime.date.fromisoformat(cov[1])
        got = req.candles(token, a, b, interval)
        made += 1
        if interval == "5m":
            got = nse.on_grid(got, 5)
        if now.time() < nse.CLOSE:
            got = [x for x in got if x["t"].date() < now.date()]
        bars = merge(bars, got)
        lo = min(datetime.date.fromisoformat(cov[0]), a) if cov else a
        hi = max(datetime.date.fromisoformat(cov[1]), b) if cov else b
        cov = [lo.isoformat(), hi.isoformat()]
        # Angel serves nothing older than its floor: an empty or late-starting chunk ends the walk back
        at_floor = False
        if not forward:
            first = min((x["t"].date() for x in got), default=None)
            if first is None or first - a > datetime.timedelta(days=min(14, n // 2)):
                at_floor = True
                if learn_floor:
                    seen = first or min((x["t"].date() for x in bars), default=None)
                    if seen:
                        state.setdefault("floor", {})[interval] = seen.isoformat()
                cov[0] = start.isoformat()
        write_store(path, dict(doc, bars=None, covered=cov, fetched=nse.iso_ist(now), token=str(token)), bars)
        log("  %s %s %s..%s -> %d bars (total %d)" % (sym, interval, a, b, len(got), len(bars)))
        if at_floor:
            break
    return made


# ---------------------------------------------------------------- universe

def parse_scrip_indices(rows):
    """Scrip-master rows -> {display name: token} for NSE AMXIDX rows (the display name, "Nifty Bank",
    is the row's `symbol`; its `name` is the short form, "BANKNIFTY")."""
    return dict((r["symbol"], str(r["token"])) for r in rows
                if r.get("exch_seg") == "NSE" and r.get("instrumenttype") == "AMXIDX")


def build_universe(n100_text, scrip_eq, scrip_idx):
    """-> ordered [{symbol, token, group}] (indices, extras, Nifty 100) and a list of problems."""
    uni, problems, seen = [], [], set()
    for sym, name, fallback in INDICES:
        tok = scrip_idx.get(name) or fallback
        if name not in scrip_idx:
            problems.append("index %s not found in scrip master by name; used token %s" % (name, fallback))
        uni.append({"symbol": sym, "token": tok, "group": "index", "angel_name": name})
        seen.add(sym)
    n100 = [r["symbol"] for r in nse.parse_index_list(n100_text)] if n100_text else []
    for grp, syms in (("extra", EXTRAS), ("nifty100", n100)):
        for s in syms:
            if s in seen:
                continue
            row = scrip_eq.get(s)
            if not row or row.get("series") != "EQ":
                problems.append("%s: no NSE -EQ row in Angel scrip master" % s)
                continue
            seen.add(s)
            uni.append({"symbol": s, "token": row["token"], "group": grp})
    for s in EXTRAS:
        if s in n100:
            for u in uni:
                if u["symbol"] == s:
                    u["group"] = "nifty100+extra"
    return uni, problems


# ---------------------------------------------------------------- quality + manifest

def session_stats(bars, interval):
    by = collections.OrderedDict()
    for b in bars:
        by.setdefault(b["t"].date(), []).append(b)
    return by


def bar_issues(bars, interval, is_index):
    iss = collections.OrderedDict()

    def note(k, ex):
        c = iss.setdefault(k, [0, ex])
        c[0] += 1

    for b in bars:
        d = b["t"].date().isoformat()
        o, h, l, c = b["o"], b["h"], b["l"], b["c"]
        if min(o, h, l, c) <= 0:
            note("non_positive_price", d)
        if h < l:
            note("high_below_low", d)
        elif not (l - 1e-9 <= o <= h + 1e-9 and l - 1e-9 <= c <= h + 1e-9):
            note("open_or_close_outside_range", d)
        if b["v"] < 0:
            note("negative_volume", d)
        if interval == "5m":
            tm = b["t"].time()
            if tm < nse.OPEN or tm >= nse.CLOSE:
                note("bar_outside_09:15-15:30", d)
            if b["v"] == 0 and not is_index:
                note("zero_volume_bar", d)
    return ["%s x%d (e.g. %s)" % (k, v[0], v[1]) for k, v in iss.items()]


def analyse(sym, interval, expected, is_index, base=None):
    path = store_path(sym, interval, base)
    doc = read_store(path)
    if not doc:
        return None
    bars = doc["bars"]
    by = session_stats(bars, interval)
    days = sorted(by)
    out = {"file": os.path.relpath(path, nse.trader_dir(base)).replace(os.sep, "/"),
           "sha256": sha256_file(path), "token": doc.get("token"), "covered": doc.get("covered"),
           "bars": len(bars), "sessions": len(days), "first": days[0].isoformat() if days else None,
           "last": days[-1].isoformat() if days else None}
    iss = bar_issues(bars, interval, is_index)
    if days:
        exp = [d for d in expected if days[0] <= d <= days[-1]]
        miss = [d.isoformat() for d in exp if d not in by]
        out["missing_sessions"] = miss
        if interval == "5m":
            dist = collections.Counter(len(v) for v in by.values())
            out["bars_per_session"] = dict((str(k), dist[k]) for k in sorted(dist))
            over = [d.isoformat() for d in days if len(by[d]) > 75]
            if over:
                iss.append("sessions with more than 75 bars x%d (e.g. %s)" % (len(over), over[0]))
            short = [d.isoformat() for d in days if len(by[d]) < 70 and d in set(exp)]
            if short:
                out["short_sessions"] = short
                iss.append("sessions with fewer than 70 bars x%d (e.g. %s)" % (len(short), short[0]))
        extra = [d.isoformat() for d in days if expected and d not in set(expected)
                 and expected[0] <= d <= expected[-1]]
        if extra:
            iss.append("bars on days Nifty has no session x%d (e.g. %s)" % (len(extra), extra[0]))
    out["issues"] = iss
    return out


def expected_sessions(base=None):
    """Sessions = days with Nifty 5m bars; 2026 cross-checked against nse.NSE_HOLIDAYS."""
    doc = read_store(store_path("NIFTY", "5m", base))
    if not doc or not doc["bars"]:
        return [], []
    days = sorted(set(b["t"].date() for b in doc["bars"]))
    notes = []
    first, last = days[0], days[-1]
    have = set(days)
    d = first
    while d <= last:
        if d.year in nse.CALENDAR_YEARS:
            cal, got = nse.is_trading_day(d), d in have
            if cal and not got:
                notes.append("%s: NSE calendar says session, Nifty has no bars" % d)
            elif got and not cal:
                notes.append("%s: Nifty traded but nse.NSE_HOLIDAYS/weekend says closed" % d)
        d += datetime.timedelta(days=1)
    return days, notes


def build_manifest(universe, failures, stats, base=None, now=None, problems=None):
    now = now or nse.now_ist()
    exp, notes = expected_sessions(base)
    syms = collections.OrderedDict()
    for u in universe:
        s = u["symbol"]
        is_idx = u["group"] == "index"
        m5 = analyse(s, "5m", exp, is_idx, base)
        m1 = analyse(s, "1d", [], is_idx, base)
        ent = {"token": u["token"], "group": u["group"]}
        if m5:
            ent.update(m5)
        else:
            ent.update({"sessions": 0, "bars": 0, "first": None, "last": None, "missing_sessions": [],
                        "issues": ["no 5-minute file"]})
        if m1:
            ent["daily"] = m1
        if s in failures:
            ent["issues"] = list(ent.get("issues") or []) + ["fetch failed: %s" % failures[s]]
        syms[s] = ent
    firsts = [e["first"] for e in syms.values() if e.get("first")]
    lasts = [e["last"] for e in syms.values() if e.get("last")]
    dfirst = [e["daily"]["first"] for e in syms.values() if e.get("daily") and e["daily"].get("first")]
    return {
        "frozen": True, "created": nse.iso_ist(now), "source": "Angel One SmartAPI getCandleData (read-only)",
        "interval": "5m (+ daily 1d)", "target_range": {"start": START.isoformat(), "end": END.isoformat(),
                                                         "daily_start": DAILY_START.isoformat()},
        "range_5m": {"first": min(firsts) if firsts else None, "last": max(lasts) if lasts else None},
        "range_daily_first": min(dfirst) if dfirst else None,
        "cache_dir": "cache/bars/angel", "format": "gzip JSON {symbol, source, interval, token, covered, fetched, bars:[[t,o,h,l,c,v]]}; "
                                                    "t = naive IST bar start",
        "expected_sessions": {"count": len(exp), "first": exp[0].isoformat() if exp else None,
                              "last": exp[-1].isoformat() if exp else None, "basis": "days with Nifty 50 5m bars",
                              "calendar_notes": notes},
        "indices": [u["symbol"] for u in universe if u["group"] == "index"],
        "n_symbols": len(syms), "failed": failures, "universe_problems": problems or [],
        "run_stats": stats, "symbols": syms}


# ---------------------------------------------------------------- main

def log_flush(*a):
    print(*a)
    sys.stdout.flush()


def load_universe(args, base=None):
    cache = os.path.join(nse.trader_dir(base), "cache")
    try:
        n100 = nse.cached(os.path.join(cache, "ind_nifty100list.csv"), N100_URL, max_age_hours=24 * 30).decode(
            "utf-8-sig", "replace")
    except nse.NSEError as e:
        raise Fatal("could not fetch the Nifty 100 list: %s" % e)
    import angel
    raw = nse.http_get(angel.SCRIP_URL, timeout=180)
    rows = json.loads(raw)
    eq = angel.parse_scrip(rows)
    return build_universe(n100, eq, parse_scrip_indices(rows))


def make_client():
    import angel
    angel.INTERVALS.setdefault(1440, "ONE_DAY")
    vals, missing = angel.credentials()
    if missing:
        raise Fatal("missing in .env: %s" % ", ".join(missing))

    def transport(url, headers=None, data=None, method=None):
        return nse.http_get(url, headers=headers, timeout=60, data=data, method=method, retries=0)

    return angel.Angel(vals["ANGEL_API_KEY"], vals["ANGEL_CLIENT_CODE"], vals["ANGEL_PIN"],
                       vals["ANGEL_TOTP_SECRET"], transport=transport, public=angel.public_ip(),
                       local=angel._local_ip())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--start", default=START.isoformat())
    ap.add_argument("--end", default=END.isoformat())
    ap.add_argument("--daily-start", default=DAILY_START.isoformat())
    ap.add_argument("--gap", type=float, default=GAP)
    ap.add_argument("--chunk", type=int, default=CHUNK_5M)
    ap.add_argument("--only", help="comma list of cache symbols")
    ap.add_argument("--skip-daily", action="store_true")
    ap.add_argument("--skip-5m", action="store_true")
    ap.add_argument("--manifest-only", action="store_true")
    a = ap.parse_args(argv)
    nse.load_env()
    start, end = datetime.date.fromisoformat(a.start), datetime.date.fromisoformat(a.end)
    dstart = datetime.date.fromisoformat(a.daily_start)
    now = nse.now_ist()
    last = now.date() if (nse.is_trading_day(now.date()) and now.time() >= nse.CLOSE) else nse.previous_trading_day(now.date())
    end = min(end, last)
    universe, problems = load_universe(a)
    for p in problems:
        log_flush("note:", p)
    if a.only:
        want = set(a.only.upper().split(","))
        universe = [u for u in universe if u["symbol"] in want]
    log_flush("universe: %d symbols (%d indices); %s..%s, daily from %s" % (
        len(universe), sum(1 for u in universe if u["group"] == "index"), start, end, dstart))
    failures, stats = {}, {"requests": 0, "rate_limit_hits": 0, "logins": 0}
    state_path = os.path.join(nse.trader_dir(), "cache", "prefetch-state.json")
    state = nse.read_json(state_path, {}) or {}
    if not a.manifest_only:
        req = Requester(make_client(), gap=a.gap, log=log_flush)
        consecutive = 0
        for phase, interval, st in (("5-minute", "5m", start), ("daily", "1d", dstart)):
            if (interval == "5m" and a.skip_5m) or (interval == "1d" and a.skip_daily):
                continue
            log_flush("== %s bars ==" % phase)
            for i, u in enumerate(universe):
                s = u["symbol"]
                try:
                    fetch_symbol(req, s, u["token"], interval, st, end, state, now=now,
                                 learn_floor=(s == "NIFTY"), chunk=a.chunk if interval == "5m" else None,
                                 log=log_flush)
                    consecutive = 0
                except (Fatal, RateLimited) as e:
                    failures["%s" % s] = "%s: %s" % (interval, e)
                    log_flush("FAILED %s %s: %s" % (s, interval, e))
                    consecutive += 1
                    if consecutive >= 4:
                        log_flush("4 symbols failed in a row; stopping (rerun to resume)")
                        break
                nse.write_json(state_path, state)
                if (i + 1) % 10 == 0:
                    log_flush("-- %s %d/%d done, %d requests" % (interval, i + 1, len(universe), req.requests))
            if consecutive >= 4:
                break
        stats = {"requests": req.requests, "rate_limit_hits": req.rate_hits, "logins": req.logins,
                 "floor": state.get("floor")}
        try:
            req.client.logout()
        except Exception:
            pass
    else:
        stats = {"note": "manifest rebuilt from cache", "floor": state.get("floor")}
    m = build_manifest(universe if not a.only else load_universe(a)[0], failures, stats, now=nse.now_ist(),
                       problems=problems)
    out = os.path.join(nse.trader_dir(), "cache", "research-dataset.json")
    nse.write_json(out, m)
    log_flush("manifest: %s (%d symbols, %d failed, %s requests)" % (out, m["n_symbols"], len(failures),
                                                                     stats.get("requests")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
