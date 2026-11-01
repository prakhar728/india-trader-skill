#!/usr/bin/env python3
"""Event recorder for india-research. Standard library only. Read-only: plain GETs.

Every run (safe every 5 minutes; idempotent):
  1. reads NSE's public feeds on nsearchives.nseindia.com: corporate announcements,
     board meetings, corporate actions, financial results (india-news's parser), insider
     trading disclosures (india-insiders's RSS parser) and, at most once an hour, the
     day's bulk and block deals (india-insiders's CSV parser);
  2. stores each item it has never seen, for stocks in the universe (Nifty 500 by
     default), with the time WE detected it (that, not the filing's own time stamp, is
     when a strategy could have acted);
  3. fills price snapshots that have come due, for events of this run and earlier ones:
     at detection, +5m, +15m, +60m, the reaction session's close and the next session's
     close. The clock starts at detection during market hours, else at the next session's
     09:15 open. Intraday snapshots come from Yahoo 1-minute bars (kept 5 sessions; after
     that a missing one is marked missed), closes from Yahoo daily bars; an Angel One
     5-minute bar cache is used when Yahoo has nothing.

Storage: trader/research/events/YYYY-MM.jsonl (month of detection), append-only lines:
  {"type": "event", "id", "symbol", "category", "title", "link", "published", "detected", "base"}
  {"type": "snap", "id", "key", "price", "bar", "src", "at"}   (or "status": "missed" / "n/a")
A lock file stops two runs from writing at once.
"""

import datetime
import glob
import hashlib
import importlib.util
import json
import os
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "india-common"))
sys.path.insert(0, os.path.join(HERE, "..", "india-backtest"))
import nse  # noqa: E402
import backtest as bt  # noqa: E402


def _sibling(folder, name):
    path = os.path.join(HERE, "..", folder, name + ".py")
    spec = importlib.util.spec_from_file_location("research_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


news = _sibling("india-news", "news")
insiders = _sibling("india-insiders", "insiders")

ALLOWED_HOSTS = ("nsearchives.nseindia.com", "archives.nseindia.com", "query1.finance.yahoo.com",
                 "query2.finance.yahoo.com")
SNAPS = ("detect", "5m", "15m", "60m", "close", "next_close")
OFFSETS = {"5m": 5, "15m": 15, "60m": 60}
DEALS_EVERY_MIN = 60
INTRADAY_KEEP_DAYS = 5          # Yahoo serves 1-minute bars for about this many sessions
DUE_LAG = datetime.timedelta(minutes=2)
STALE_LOCK_SEC = 900
MAX_SYMBOLS = 40
# Event studies need numbers: the intraday gates want 100 trades on the validate split, which is
# about 20 % of the dates, so a category needs roughly 500 complete events over 60+ sessions.
MIN_EVENTS_TO_TEST = 500
MIN_EVENT_SESSIONS = 60


class EventError(nse.NSEError):
    pass


def folder(base=None):
    return os.path.join(nse.trader_dir(base), "research", "events")


# ---------------------------------------------------------------- IO

class Getter(object):
    """Plain GETs to NSE's archive host (or saved files with offline=[dirs]), a pause between calls."""

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
            raise EventError("offline: no file %s" % name)
        u = urllib.parse.urlsplit(url)
        if u.scheme != "https" or u.netloc not in ALLOWED_HOSTS:
            raise EventError("refusing %s: only plain GETs to NSE's archive host and Yahoo" % url)
        if self.calls and self.pause:
            time.sleep(self.pause)
        self.calls += 1
        return nse.http_get(url, timeout=60)


def read_all(base=None):
    """-> ({id: event with 'snaps': {key: snap}}, number of lines)."""
    evs, n = {}, 0
    for path in sorted(glob.glob(os.path.join(folder(base), "*.jsonl"))):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                n += 1
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("type") == "event" and r["id"] not in evs:
                    evs[r["id"]] = dict(r, snaps={})
                elif r.get("type") == "snap" and r.get("id") in evs:
                    evs[r["id"]]["snaps"].setdefault(r["key"], r)
    return evs, n


def append(base, month, rows):
    if not rows:
        return
    d = folder(base)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "%s.jsonl" % month), "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


class Lock(object):
    def __init__(self, base=None):
        self.path = os.path.join(folder(base), ".lock")

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            if time.time() - os.path.getmtime(self.path) > STALE_LOCK_SEC:
                os.remove(self.path)
        except OSError:
            pass
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise EventError("another events run holds %s (stale after %d min)" % (self.path, STALE_LOCK_SEC // 60))
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return self

    def __exit__(self, *a):
        try:
            os.remove(self.path)
        except OSError:
            pass


def read_state(base=None):
    try:
        return nse.read_json(os.path.join(folder(base), "state.json"), {}) or {}
    except nse.NSEError:
        return {}


def write_state(base, st):
    nse.write_json(os.path.join(folder(base), "state.json"), st)


# ---------------------------------------------------------------- collecting

def _sha(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]


def collect(getter, resolver, universe=None, deals=True, errors=None):
    """All feed items as events (no prices yet), for symbols in `universe` (None = every symbol)."""
    errors = [] if errors is None else errors
    out = []
    for kind, fname, _ in news.NSE_FEEDS:
        try:
            items = news.parse_nse_feed(getter.get(news.RSS_BASE + fname), kind, resolver, None)
        except (nse.NSEError, news.NewsError) as e:
            errors.append("%s: %s" % (fname, e))
            continue
        news.tag(items, {})
        for it in items:
            out.append({"id": it["id"], "symbol": it["ticker"], "category": kind, "tags": it.get("tags") or [],
                        "title": it["title"][:300], "link": it.get("link") or "",
                        "published": nse.iso_ist(it["time"]) if it.get("time") else None})
    try:
        for it in insiders.parse_rss(getter.get(insiders.FEED)):
            if not it.get("symbol"):
                continue
            out.append({"id": "insider:%s" % it["link"].rsplit("/", 1)[-1], "symbol": it["symbol"],
                        "category": "insider", "tags": ["revision"] if it.get("revision") else [],
                        "title": "PIT disclosure: %s" % it["company"], "link": it["link"],
                        "published": it.get("published")})
    except (nse.NSEError, insiders.InsiderError) as e:
        errors.append("InsiderTrading.xml: %s" % e)
    if deals:
        for kind, url in (("bulk", insiders.BULK), ("block", insiders.BLOCK)):
            try:
                rows = insiders.parse_deals(getter.get(url).decode("utf-8", "replace"), kind)
            except (nse.NSEError, insiders.InsiderError) as e:
                errors.append("%s deals: %s" % (kind, e))
                continue
            for d in rows:
                out.append({"id": "deal:%s:%s:%s:%s" % (kind, d["date"], d["symbol"],
                                                        _sha(d["client"], d["side"], d["qty"], d["price"])),
                            "symbol": d["symbol"], "category": kind, "tags": [d["side"].lower()],
                            "title": "%s %s %s @ %.2f by %s" % (kind.upper(), d["side"], "{:,.0f}".format(d["qty"]),
                                                                d["price"], d["client"]),
                            "link": url, "published": d["date"], "value": d["value"]})
    if universe is not None:
        out = [e for e in out if e["symbol"] in universe]
    return out


# ---------------------------------------------------------------- the clock of an event

def lag_minutes(published, det):
    """Minutes from the filing's own time stamp to our detection (None for date-only stamps). A big lag means
    the item was already public when we saw it (e.g. the first run's backfill): event strategies should
    filter on it."""
    if not published or len(published) < 16:
        return None
    try:
        p = datetime.datetime.fromisoformat(published[:19])
    except ValueError:
        return None
    return int((det - p).total_seconds() // 60)


def in_session(t, cfg=None):
    """Regular hours only (a Muhurat special session is not a normal reaction window)."""
    return (nse.is_trading_day(t.date(), cfg) and not nse.special_session(t.date(), cfg)
            and nse.OPEN <= t.time() < nse.CLOSE)


def next_trading_day(d, cfg=None, limit=30):
    for _ in range(limit):
        d += datetime.timedelta(days=1)
        if nse.is_trading_day(d, cfg) and not nse.special_session(d, cfg):
            return d
    raise EventError("no trading day within %d days of %s" % (limit, d))


def next_open(t, cfg=None):
    d = t.date()
    if nse.is_trading_day(d, cfg) and not nse.special_session(d, cfg) and t.time() < nse.OPEN:
        return datetime.datetime.combine(d, nse.OPEN)
    return datetime.datetime.combine(next_trading_day(d, cfg), nse.OPEN)


def base_time(detected, cfg=None):
    return detected if in_session(detected, cfg) else next_open(detected, cfg)


def targets(ev, cfg=None):
    """{key: (kind, due datetime, target)}; kind 'bar' = price at a time, 'day' = a session's close."""
    det = datetime.datetime.fromisoformat(ev["detected"][:19])
    base = base_time(det, cfg)
    close_at = datetime.datetime.combine(base.date(), nse.CLOSE)
    out = {"detect": ("bar", det, det)}
    for k, m in OFFSETS.items():
        t = base + datetime.timedelta(minutes=m)
        out[k] = ("bar", t, t) if t <= close_at else ("na", t, None)
    nxt = next_trading_day(base.date(), cfg)
    out["close"] = ("day", close_at + datetime.timedelta(minutes=15), base.date())
    out["next_close"] = ("day", datetime.datetime.combine(nxt, nse.CLOSE) + datetime.timedelta(minutes=15), nxt)
    return out


def price_at(bars, t, minutes=1, stale_min=15):
    """The close of the last bar that had closed by t (and closed no more than stale_min before it)."""
    step = datetime.timedelta(minutes=minutes)
    best = None
    for b in bars:
        if b["t"] + step <= t:
            if best is None or b["t"] > best["t"]:
                best = b
    if best is None:
        return None, None
    if t - (best["t"] + step) > datetime.timedelta(minutes=stale_min) and in_session(t):
        return None, None
    return round(best["c"], 2), best["t"].isoformat()


def detect_price(bars, det):
    """At detection: the last 1-minute close by then (the previous close when the market was shut)."""
    if in_session(det):
        return price_at(bars, det)
    return price_at(bars, det, stale_min=10 ** 6)


def close_on(daily, d):
    for b in daily:
        if b["t"].date() == d:
            return round(b["c"], 2), d.isoformat()
    return None, None


# ---------------------------------------------------------------- prices

class YahooPrices(object):
    def __init__(self, getter=None, pause=nse.PAUSE):
        self.getter = getter or Getter(pause=pause)
        self.memo = {}

    def __call__(self, sym, interval):
        key = (sym, interval)
        if key not in self.memo:
            rng = "5d" if interval == "1m" else "1mo"
            try:
                self.memo[key] = nse.parse_yahoo_chart(self.getter.get(nse.yahoo_chart_url(sym, rng, interval)))[1]
            except (nse.NSEError, ValueError):
                self.memo[key] = []
        return self.memo[key]


class OfflinePrices(object):
    """Saved Yahoo charts: yahoo-SYM.NS-1d1m.json(.gz) for 1-minute bars, yahoo-SYM.NS-1y1d.json(.gz) for daily."""

    def __init__(self, dirs):
        self.dirs = dirs

    def __call__(self, sym, interval):
        stem = "yahoo-%s.NS-%s.json" % (sym, "1d1m" if interval == "1m" else "1y1d")
        for d in self.dirs:
            for p in (os.path.join(d, stem), os.path.join(d, stem + ".gz")):
                if os.path.exists(p):
                    return nse.parse_yahoo_chart(nse.read_maybe_gz(p))[1]
        return []


def angel_cache_bars(sym, base=None):
    path = os.path.join(nse.trader_dir(base), "cache", "bars", "angel", "%s-5m.json.gz" % sym)
    doc = bt.read_store(path)
    return (doc or {}).get("bars") or []


# ---------------------------------------------------------------- one run

def record(now, getter, resolver, universe=None, base=None, prices=None, cfg=None, max_symbols=MAX_SYMBOLS,
           deals=None, errors=None):
    """Collect, store new events, fill due snapshots. -> summary dict."""
    errors = [] if errors is None else errors
    prices = prices or YahooPrices()
    with Lock(base):
        st = read_state(base)
        if deals is None:
            last = st.get("deals_checked")
            deals = bool(getter.offline) or not last or (
                now - datetime.datetime.fromisoformat(last[:19])) >= datetime.timedelta(minutes=DEALS_EVERY_MIN)
        known, _ = read_all(base)
        found = collect(getter, resolver, universe, deals, errors)
        new_rows, seen = {}, set()
        for e in found:
            if e["id"] in known or e["id"] in seen:
                continue
            seen.add(e["id"])
            det = now.replace(microsecond=0)
            row = dict(e, type="event", detected=nse.iso_ist(det), base=nse.iso_ist(base_time(det, cfg)),
                       lag_min=lag_minutes(e.get("published"), det))
            new_rows.setdefault(det.strftime("%Y-%m"), []).append(row)
            known[e["id"]] = dict(row, snaps={})
        for m, rows in new_rows.items():
            append(base, m, rows)
        filled, missed, pending, fetched = fill(known, now, prices, base, cfg, max_symbols)
        if deals:
            st["deals_checked"] = nse.iso_ist(now)
        st["last_run"] = nse.iso_ist(now)
        write_state(base, st)
    return {"new": sum(len(v) for v in new_rows.values()), "seen_in_feeds": len(found), "filled": filled,
            "missed": missed, "pending": pending, "symbols_priced": fetched, "errors": errors}


def fill(known, now, prices, base=None, cfg=None, max_symbols=MAX_SYMBOLS):
    due = {}
    pending = 0
    for ev in known.values():
        for key, (kind, when, tgt) in targets(ev, cfg).items():
            if key in ev["snaps"]:
                continue
            if kind == "na":
                due.setdefault(ev["symbol"], []).append((ev, key, kind, tgt))
                continue
            if now < when + (DUE_LAG if kind == "bar" else datetime.timedelta(0)):
                pending += 1
                continue
            due.setdefault(ev["symbol"], []).append((ev, key, kind, tgt))
    rows, filled, missed, fetched, waiting = {}, 0, 0, 0, 0
    # oldest due first, so nothing starves behind the per-run cap
    order = sorted(due, key=lambda sym: (min(ev["detected"] for ev, _, _, _ in due[sym]), sym))
    deferred = order[max_symbols:] if max_symbols else []
    waiting += sum(len(due[sym]) for sym in deferred)
    for sym in order[:max_symbols] if max_symbols else order:
        jobs = due[sym]
        if any(kind != "na" for _, _, kind, _ in jobs):
            fetched += 1
        m1 = d1 = None
        for ev, key, kind, tgt in jobs:
            rec = {"type": "snap", "id": ev["id"], "key": key, "at": nse.iso_ist(now)}
            if kind == "na":
                rec.update(price=None, status="n/a", note="after the close")
            elif kind == "bar":
                if m1 is None:
                    m1 = prices(sym, "1m")
                px, bar = detect_price(m1, tgt) if key == "detect" else price_at(m1, tgt)
                src = "yahoo 1m"
                if px is None:
                    a5 = angel_cache_bars(sym, base)
                    if key == "detect" and not in_session(tgt):
                        px, bar = price_at(a5, tgt, 5, stale_min=10 ** 6)
                    else:
                        px, bar = price_at(a5, tgt, 5)
                    src = "angel 5m cache"
                if px is None:
                    if (now - tgt).days >= INTRADAY_KEEP_DAYS:
                        rec.update(price=None, status="missed", note="no 1-minute bar left for %s" % tgt.isoformat())
                    else:
                        waiting += 1
                        continue
                else:
                    rec.update(price=px, bar=bar, src=src)
            else:
                if d1 is None:
                    d1 = prices(sym, "1d")
                px, day = close_on(d1, tgt)
                if px is None:
                    if (now.date() - tgt).days >= 20:
                        rec.update(price=None, status="missed", note="no daily bar for %s" % tgt)
                    else:
                        waiting += 1
                        continue
                else:
                    rec.update(price=px, bar=day, src="yahoo 1d")
            rows.setdefault(ev["detected"][:7], []).append(rec)
            ev["snaps"][key] = rec
            if rec.get("price") is not None:
                filled += 1
            elif rec.get("status") == "missed":
                missed += 1
    for m, rr in rows.items():
        append(base, m, rr)
    return filled, missed, pending + waiting, fetched


# ---------------------------------------------------------------- reading back

def as_list(known):
    return sorted(known.values(), key=lambda e: (e["detected"], e["id"]))


def stats(known):
    """Per category: events, complete ones (every snapshot priced or n/a), symbols, sessions."""
    out = {}
    for e in known.values():
        c = out.setdefault(e["category"], {"events": 0, "complete": 0, "symbols": set(), "sessions": set()})
        c["events"] += 1
        c["symbols"].add(e["symbol"])
        c["sessions"].add((e.get("base") or e["detected"])[:10])
        if all(k in e["snaps"] and e["snaps"][k].get("status") != "missed" for k in SNAPS):
            c["complete"] += 1
    return dict((k, {"events": v["events"], "complete": v["complete"], "symbols": len(v["symbols"]),
                     "sessions": len(v["sessions"]),
                     "testable": v["complete"] >= MIN_EVENTS_TO_TEST and len(v["sessions"]) >= MIN_EVENT_SESSIONS})
                for k, v in out.items())


def for_lab(base=None):
    """Events as the lab engine's ctx.events wants them (flat dicts, snaps included)."""
    known, _ = read_all(base)
    return [dict(e, snaps=dict((k, v.get("price")) for k, v in e["snaps"].items())) for e in as_list(known)]
