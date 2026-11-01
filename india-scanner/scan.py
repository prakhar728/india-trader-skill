#!/usr/bin/env python3
"""Chart Scanner for india-scanner: scan NSE charts for setups. Standard library only.

    python3 scan.py universe                          # Nifty 500 (EQ) -> trader/universe.txt
    python3 scan.py universe --universe all           # every EQ stock in NSE's EQUITY_L.csv
    python3 scan.py daily                             # pre-market: daily setups -> trader/watchlist.json
    python3 scan.py daily --symbols RELIANCE,TCS,INFY # or --watchlist FILE, --universe all, --limit 200
    python3 scan.py orb                               # after 09:35 IST: opening-range breakouts so far
    python3 scan.py orb --at 10:15 --or-minutes 15    # cut the clock at 10:15 IST
    python3 scan.py orb --offline nifty5m.json.gz --now "2026-10-29 10:15"

universe  Builds the symbol list. --universe nifty500 (the default, NSE's
          ind_nifty500list.csv) or all (every stock in NSE's EQUITY_L.csv).
          Only series EQ is kept: BE and BZ are trade-to-trade (delivery only,
          no intraday) and SME boards are not in EQUITY_L at all. F&O stocks
          are marked from NSE's fo_mktlots.csv when it can be read.

daily     Pre-market scan of daily CLOSES (Yahoo spark: 20 symbols a request,
          3 months of 1-day bars, closes only, .NS symbols). Filters from the
          spark meta: last price >= --min-price (Rs 50) and last session's
          turnover (price x volume) >= --min-turnover crore (Rs 10 crore).
          Spark has no daily volumes, so the turnover is the LAST session's,
          not an average (during the session it is the day's so far).
          Setups, from closes only:
            breakout_20d     the last close is above every close of the 20 before it
            bull_flag        up more than 10 % over the 10 sessions before the
                             last 5, then the last 5 closes inside a 5 % range
            reclaim_ma20     closed under the 20-day average, then closed above it
            oversold         RSI 14 (Wilder) under 30
            oversold_bounce  RSI 14 was under 30 yesterday and today closed up
          Today's daily bar is dropped until the 15:30 close.

orb       Opening-range breakouts on 5-minute bars, IST. The range is the
          first --or-minutes (15) of the session: the 09:15, 09:20 and 09:25
          bars. A hit is the first completed bar after the range to CLOSE
          above the range high, at that bar's close time.
          Yahoo (default without Dhan keys): stage 1 is spark closes for every
          symbol (20 a request); a chart goes on only if a completed bar after
          the range closed at or above the highest range close (a necessary
          condition). Stage 2 reads each candidate's v8 chart (real highs and
          lows), at most --max-confirm (200), the most traded first.
          Dhan (DHAN_CLIENT_ID + DHAN_ACCESS_TOKEN set, data_source auto or
          dhan, Dhan Data plan): stage 1 is one /marketfeed/ohlc snapshot per
          1000 symbols; a chart goes on only if the day's high is above the
          day's open (a breakout close is above the range high, which is at
          least the open). Stage 2 reads /charts/intraday 5-minute bars, one a
          second, strongest move from the open first.
          A chart is usable when every opening-range bar is there and at most
          --max-missing (2) bars after it are missing: Yahoo drops a bar here
          and there, and F&O stocks have no bars after 15:15 since NSE's
          closing auction session began (Aug 3 2026).

Bars: null closes are dropped, a bar off the 5-minute grid from 09:15 (the live
tick) is dropped, and a bar counts only once it is complete (start + 5 minutes
<= the clock). Times are IST (UTC+05:30, no daylight saving). --now pins the
clock ("2026-10-29 10:15", IST).

--offline PATH reads files instead of the network: a folder of <SYMBOL>.json
v8 charts or spark answers, or one file that is a spark answer, a v8 chart or
a bundle {"RELIANCE": <v8 json>, ...}. .json.gz is fine. --equity-list,
--index-list and --fo-list read NSE's CSVs from disk instead of the cache.

A scan is a list of charts that match a rule, not a list of stocks to buy.
Not financial advice. Nothing is ever placed without your typed yes.
Exit code 0 when the scan found something, 1 when it did not or could not run.
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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "india-common"))
import nse  # noqa: E402

SPARK_MAX = 20       # Yahoo's spark answers 400 above 20 symbols
PAUSE = nse.PAUSE    # seconds between requests: be polite to free endpoints
BACKOFF = nse.BACKOFF
SLEEP = time.sleep   # tests swap this out
GET = nse.http_get   # tests swap this out
BAR_MIN = 5
MAX_CONFIRM = 200
MAX_MISSING = 2
MIN_DAILY_BARS = 30
CRORE = 1e7
FO_LIST_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
NOTE = "A scan is a list of charts that match a rule, not a list of stocks to buy."
ADVICE = "Not financial advice; nothing is placed without your typed yes."

SETUPS = [
    ("breakout_20d", "20-day closing-high breakout"),
    ("bull_flag", "bull flag"),
    ("reclaim_ma20", "reclaimed the 20-day average"),
    ("oversold", "oversold (RSI under 30)"),
    ("oversold_bounce", "oversold bounce"),
]
LABEL = dict(SETUPS)


class ScanError(Exception):
    pass


# ---------------------------------------------------------------- time

def parse_now(text):
    try:
        return nse.parse_now(text).replace(microsecond=0)
    except nse.NSEError as e:
        raise ScanError(str(e).replace("bad time", "--now wants a time like \"2026-10-29 10:15\" (IST), not"))


def parse_hhmm(text, flag="--at"):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ScanError("%s wants an IST time like 10:15" % flag)
    return datetime.time(int(m.group(1)), int(m.group(2)))


def hm(t):
    return t.strftime("%H:%M")


def day_label(d):
    return "%s %s" % (d.strftime("%a"), d.isoformat())


def why_closed(d, cfg=None):
    if d.weekday() >= 5 and not nse.special_session(d, cfg):
        return "weekend"
    name = nse.holiday_name(d, cfg)
    return ("NSE holiday: " + name) if name else "no session"


# ---------------------------------------------------------------- network

def yahoo_get(path):
    """GET a Yahoo Finance path: query1 first, query2 on a 429 or 5xx, and one
    more round after BACKOFF seconds if a 429 was seen. Other 4xx are final."""
    last = "no answer"
    for attempt in (0, 1):
        saw_429 = False
        for host in nse.YAHOO_HOSTS:
            try:
                return GET("https://%s.finance.yahoo.com%s" % (host, path), retries=0)
            except nse.NSEError as e:
                last = str(e)
                m = re.search(r"HTTP (\d{3})", last)
                code = int(m.group(1)) if m else 0
                if code == 429:
                    saw_429 = True
                    continue
                if code >= 500 or code == 0:
                    continue
                raise ScanError("Yahoo answered %s for %s" % (last.split(" from ")[0], path.split("?")[0]))
        if attempt == 0 and saw_429:
            SLEEP(BACKOFF)
            continue
        break
    raise ScanError("Yahoo answered %s for %s" % (last.split(" from ")[0], path.split("?")[0]))


def to_yahoo(sym):
    return nse.yahoo_symbol(sym)


def spark_path(symbols, rng, interval):
    return "/v7/finance/spark?symbols=%s&range=%s&interval=%s" % (
        ",".join(urllib.parse.quote(to_yahoo(s)) for s in symbols), rng, interval)


def chart_path(sym, rng, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (urllib.parse.quote(to_yahoo(sym)), rng, interval)


# ---------------------------------------------------------------- parsing
# A series is {"bars": [{"t": naive IST, "o","h","l": float or None, "c": float, "v": int or None}], "meta": {}}.
# Spark answers carry closes only, so o/h/l/v are None there.

def series_from_result(res):
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def col(k, i):
        v = q.get(k)
        if not isinstance(v, list) or i >= len(v) or v[i] is None:
            return None
        return v[i]
    bars = []
    for i, ts in enumerate(res.get("timestamp") or []):
        c = col("close", i)
        if c is None:
            continue
        o, h, l, v = col("open", i), col("high", i), col("low", i), col("volume", i)
        bars.append({"t": nse.ist_from_epoch(ts), "c": float(c),
                     "o": None if o is None else float(o), "h": None if h is None else float(h),
                     "l": None if l is None else float(l), "v": None if v is None else int(v)})
    return {"bars": bars, "meta": res.get("meta") or {}}


def _ours(ysym):
    try:
        return nse.clean_symbol(ysym)
    except nse.NSEError:
        return ysym.upper()


def parse_spark(text_or_doc, wanted=None):
    """Spark JSON -> {SYMBOL: series}. Symbols Yahoo does not know are simply missing."""
    doc = json.loads(text_or_doc) if isinstance(text_or_doc, (str, bytes)) else text_or_doc
    sp = (doc or {}).get("spark") if isinstance(doc, dict) else None
    if not isinstance(sp, dict):
        raise ScanError("Yahoo's spark answer is not the shape this tool knows")
    err = sp.get("error")
    out = {}
    for item in sp.get("result") or []:
        resp = item.get("response") or []
        if not resp:
            continue
        ysym = (item.get("symbol") or (resp[0].get("meta") or {}).get("symbol") or "").upper()
        out[(wanted or {}).get(ysym, _ours(ysym))] = series_from_result(resp[0])
    if err and not out:
        raise ScanError("Yahoo spark: %s" % (err.get("description") or err.get("code") or err))
    return out


def parse_chart(text_or_doc, sym=""):
    try:
        doc = json.loads(text_or_doc) if isinstance(text_or_doc, (str, bytes)) else text_or_doc
        chart = doc["chart"]
    except (ValueError, KeyError, TypeError):
        raise ScanError("Yahoo returned something unexpected for %s" % sym)
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise ScanError("no data for %s: %s" % (sym, err.get("description", "unknown symbol")))
    return series_from_result(res[0])


def series_from_bars(bars):
    """nse bars (Dhan) -> a series."""
    return {"bars": [dict(b) for b in bars], "meta": {}}


def _stem(name):
    for ext in (".json.gz", ".json"):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def _absorb(doc, hint, data, errors):
    if isinstance(doc, dict) and "spark" in doc:
        data.update(parse_spark(doc))
    elif isinstance(doc, dict) and "chart" in doc:
        sym = hint
        if not sym:
            try:
                sym = (doc["chart"].get("result") or [{}])[0]["meta"]["symbol"]
            except (AttributeError, KeyError, TypeError, IndexError):
                raise ScanError("an offline v8 chart without a symbol: name the file <SYMBOL>.json")
        sym = _ours(sym)
        try:
            data[sym] = parse_chart(doc, sym)
        except ScanError as e:
            errors[sym] = str(e)
    elif isinstance(doc, dict):
        for k, v in doc.items():
            _absorb(v, k, data, errors)
    else:
        raise ScanError("offline file %s is not a spark answer, a v8 chart or a bundle" % hint)


def _load_json_file(path):
    try:
        return json.loads(nse.read_maybe_gz(path).decode("utf-8"))
    except ValueError as e:
        raise ScanError("%s is not valid JSON: %s" % (path, e))


def load_offline(path):
    """A folder of <SYMBOL>.json(.gz) files or one file -> ({SYMBOL: series}, {SYMBOL: error})."""
    data, errors = {}, {}
    if os.path.isdir(path):
        names = sorted(f for f in os.listdir(path) if f.endswith((".json", ".json.gz")))
        if not names:
            raise ScanError("no .json files in %s" % path)
        for f in names:
            doc = _load_json_file(os.path.join(path, f))
            if isinstance(doc, dict) and "chart" in doc:
                _absorb(doc, _stem(f), data, errors)
            else:
                _absorb(doc, "", data, errors)
    elif os.path.isfile(path):
        _absorb(_load_json_file(path), "", data, errors)
    else:
        raise ScanError("no offline file or folder at %s" % path)
    return data, errors


# ---------------------------------------------------------------- universe

def parse_fo_list(text):
    """NSE fo_mktlots.csv -> set of stock symbols with F&O (index rows dropped)."""
    out, stocks = set(), False
    for line in text.splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) < 2:
            continue
        if cells[0].lower().startswith("derivatives on individual securities"):
            stocks = True
            continue
        if stocks and cells[1] and cells[1].upper() != "SYMBOL":
            out.add(cells[1].upper())
    return out


class Lists(object):
    """NSE's lists, read lazily: EQUITY_L (series and names), Nifty 500, F&O."""

    def __init__(self, equity_path=None, index_path=None, fo_path=None):
        self.equity_path, self.index_path, self.fo_path = equity_path, index_path, fo_path
        self._eq = self._n500 = self._fo = None
        self.notes = []

    def equity(self):
        if self._eq is None:
            try:
                self._eq = nse.equity_list(offline=self.equity_path)
            except (nse.NSEError, OSError) as e:
                self.notes.append("NSE's EQUITY_L.csv could not be read (%s): series not checked" % e)
                self._eq = {}
        return self._eq

    def nifty500(self):
        if self._n500 is None:
            path = os.path.join(nse.trader_dir(), "cache", "ind_nifty500list.csv")
            try:
                raw = nse.cached(path, nse.NIFTY500_LIST, offline=self.index_path)
            except (nse.NSEError, OSError) as e:
                raise ScanError("could not read NSE's Nifty 500 list: %s" % e)
            text = raw.decode("utf-8-sig", "replace")
            rows = nse.parse_index_list(text)
            series = dict((r.get("Symbol", "").strip(), (r.get("Series") or "").strip())
                          for r in csv.DictReader(io.StringIO(text)))
            for r in rows:
                r["series"] = series.get(r["symbol"]) or "EQ"
            self._n500 = rows
        return self._n500

    def fno(self):
        if self._fo is None:
            path = os.path.join(nse.trader_dir(), "cache", "fo_mktlots.csv")
            try:
                self._fo = parse_fo_list(nse.cached(path, FO_LIST_URL, offline=self.fo_path)
                                         .decode("utf-8", "replace"))
            except (nse.NSEError, OSError) as e:
                self.notes.append("NSE's F&O list could not be read (%s): F&O stocks not marked" % e)
                self._fo = set()
        return self._fo

    def name(self, sym):
        row = self.equity().get(sym)
        if row:
            return row["name"]
        for r in self._n500 or []:
            if r["symbol"] == sym:
                return r["name"]
        return None


def build_universe(lists, which):
    """-> ([(SYMBOL, name)], counts). Only series EQ."""
    eq = lists.equity()
    counts = {}
    kept = {}
    if which == "nifty500":
        rows = lists.nifty500()
        counts["listed"] = len(rows)
        for r in rows:
            series = (eq.get(r["symbol"]) or {}).get("series") if eq else None
            series = series or r.get("series") or "EQ"
            if series not in nse.INTRADAY_SERIES:
                counts["series " + series] = counts.get("series " + series, 0) + 1
                continue
            kept[r["symbol"]] = (eq.get(r["symbol"]) or {}).get("name") or r["name"]
    elif which == "all":
        if not eq:
            raise ScanError("NSE's EQUITY_L.csv is needed for --universe all")
        counts["listed"] = len(eq)
        for sym, r in eq.items():
            if r["series"] not in nse.INTRADAY_SERIES:
                counts["series " + r["series"]] = counts.get("series " + r["series"], 0) + 1
                continue
            kept[sym] = r["name"]
    else:
        raise ScanError("--universe wants nifty500, all or a file")
    return sorted(kept.items()), counts


def check_series(symbols, lists):
    """Drop symbols that are not series EQ. -> (kept, {sym: why})."""
    eq = lists.equity()
    if not eq:
        return list(symbols), {}
    kept, dropped = [], {}
    for s in symbols:
        row = eq.get(s)
        if row is None:
            dropped[s] = "not in NSE's equity list"
            continue
        ok, why = nse.intraday_allowed(row["series"])
        if ok:
            kept.append(s)
        else:
            dropped[s] = why
    return kept, dropped


# ---------------------------------------------------------------- daily setups

def rsi(closes, n=14):
    """Wilder's RSI on closes."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def daily_closes(series, now, cfg=None):
    """[(date, close)] of COMPLETE daily bars: today's bar is dropped until the session
    closes, anything dated after the clock is dropped. One bar a day (the last wins)."""
    today = now.date()
    s = nse.session(today, cfg)
    today_done = s is None or now >= s[1]
    out = {}
    for b in series["bars"]:
        d = b["t"].date()
        if d > today or (d == today and not today_done):
            continue
        out[d] = b["c"]
    return sorted(out.items())


def find_setups(closes):
    c = closes
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r = rsi(c)
    r_prev = rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": r, "rsi_prev": r_prev, "high20": hi20, "ma20": ma20,
                  "run_pct": run * 100, "range5_pct": rng5 * 100}


def scan_daily(data, symbols, now, min_price, min_turnover, info=None, cfg=None):
    """Pure. data = {SYM: series}; min_turnover in rupees. Returns (hits, counts, last_date)."""
    info = info or {}
    hits, dates = [], {}
    counts = {"requested": len(symbols), "fetched": 0, "illiquid": 0, "short": 0, "scanned": 0}
    for sym in symbols:
        s = data.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = daily_closes(s, now, cfg)
        meta = s.get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price is None and bars:
            price = bars[-1][1]
        vol = meta.get("regularMarketVolume")
        turnover = price * vol if (price is not None and vol is not None) else None
        if price is None or price < min_price or (min_turnover > 0 and (turnover is None or turnover < min_turnover)):
            counts["illiquid"] += 1
            continue
        if len(bars) < MIN_DAILY_BARS:
            counts["short"] += 1
            continue
        counts["scanned"] += 1
        d_last = bars[-1][0]
        dates[d_last] = dates.get(d_last, 0) + 1
        closes = [c for _, c in bars]
        tags, nums = find_setups(closes)
        if tags:
            extra = info.get(sym) or {}
            hit = {"symbol": sym, "exchange": "NSE", "setups": tags, "price": round(closes[-1], 2),
                   "close_date": d_last.isoformat(), "rsi": round(nums["rsi"], 1),
                   "volume": int(vol) if vol is not None else None,
                   "turnover_cr": round(turnover / CRORE, 2) if turnover is not None else None}
            if extra.get("name"):
                hit["name"] = extra["name"]
            if extra.get("fno") is not None:
                hit["fno"] = extra["fno"]
            hits.append(hit)
    hits.sort(key=lambda h: (-(h["turnover_cr"] or 0), h["symbol"]))  # most rupees traded first
    last_date = max(dates, key=lambda d: (dates[d], d)) if dates else None
    counts["hits"] = len(hits)
    return hits, counts, last_date


# ---------------------------------------------------------------- opening-range breakout

def session_open(day, cfg=None):
    s = nse.session(day, cfg)
    return s[0] if s else datetime.datetime.combine(day, nse.OPEN)


def slot(day, k, cfg=None):
    return session_open(day, cfg) + datetime.timedelta(minutes=BAR_MIN * k)


def session_bars(series, day, cutoff, cfg=None):
    """5-minute bars of `day` complete at `cutoff`, on the grid from the open, with a close."""
    s = nse.session(day, cfg)
    open_dt = s[0] if s else datetime.datetime.combine(day, nse.OPEN)
    close_dt = s[1] if s else datetime.datetime.combine(day, nse.CLOSE)
    step = datetime.timedelta(minutes=BAR_MIN)
    out = {}
    for b in series["bars"]:
        t = b["t"]
        if t.date() != day or t.second or t.microsecond:
            continue
        mins = (t - open_dt).total_seconds() / 60.0
        if mins < 0 or mins % BAR_MIN or t >= close_dt:
            continue
        if t + step > cutoff:
            continue
        out[t] = b
    return [out[k] for k in sorted(out)]


def expected_slots(day, cutoff, cfg=None):
    """How many 5-minute bars should be complete by the cutoff (not counting after 15:15
    on closing-auction days: F&O stocks stop trading continuously at 15:15)."""
    s = nse.session(day, cfg)
    end = min(cutoff, s[1]) if s else cutoff
    if s and s[0].time() == nse.OPEN and day >= nse.CAS_START:
        end = min(end, datetime.datetime.combine(day, datetime.time(15, 15)))
    return max(0, int((end - slot(day, 0, cfg)).total_seconds() // (BAR_MIN * 60)))


def usable(bars, day, cutoff, n_range, max_missing=MAX_MISSING, cfg=None):
    """Every opening-range bar is there, and at most max_missing bars after it are missing."""
    want = expected_slots(day, cutoff, cfg)
    if want <= n_range:
        return False
    starts = set(b["t"] for b in bars)
    if any(slot(day, k, cfg) not in starts for k in range(n_range)):
        return False
    have = sum(1 for k in range(n_range, want) if slot(day, k, cfg) in starts)
    return (want - n_range) - have <= max_missing and have > 0


def range_count(bars, day, n_range, cfg=None):
    starts = [slot(day, k, cfg) for k in range(n_range)]
    return [b for b in bars if b["t"] in starts]


def orb_candidate(bars, day, n_range, cfg=None):
    """Stage 1, closes only: a bar after the range closed at or above the highest range close."""
    rng = range_count(bars, day, n_range, cfg)
    top = max(b["c"] for b in rng)
    end = slot(day, n_range, cfg)
    return any(b["c"] >= top for b in bars if b["t"] >= end)


def orb_hit(bars, day, n_range, cfg=None):
    """Stage 2, real highs and lows: the first completed bar to CLOSE above the range high."""
    rng = range_count(bars, day, n_range, cfg)
    if len(rng) < n_range or any(b["h"] is None or b["l"] is None for b in rng):
        return None
    oh = max(b["h"] for b in rng)
    ol = min(b["l"] for b in rng)
    end = slot(day, n_range, cfg)
    after = [b for b in bars if b["t"] >= end]
    for k, b in enumerate(after):
        if b["c"] > oh:
            closes_at = b["t"] + datetime.timedelta(minutes=BAR_MIN)
            return {"price": round(b["c"], 2), "at": hm(closes_at), "bar_start": hm(b["t"]),
                    "bar_index": n_range + int((b["t"] - end).total_seconds() // (BAR_MIN * 60)),
                    "or_high": round(oh, 2), "or_low": round(ol, 2),
                    "pct_over": round((b["c"] / oh - 1) * 100, 2), "_t": closes_at}
    return None


def orb_window(now, at, or_minutes, until, explicit_day=None, cfg=None):
    """-> (day, cutoff naive IST) or raise ScanError with the plain message."""
    day = explicit_day or now.date()
    if or_minutes < BAR_MIN or or_minutes % BAR_MIN or or_minutes > 120:
        raise ScanError("--or-minutes wants a multiple of 5 from 5 to 120 (15 is the classic)")
    s = nse.session(day, cfg)
    if not s:
        raise ScanError("No session on %s (%s): the market is closed. Run orb on a trading day "
                        "after the opening range, or replay a past day with --offline." % (day_label(day),
                                                                                            why_closed(day, cfg)))
    open_dt, close_dt = s
    range_end = slot(day, or_minutes // BAR_MIN, cfg)
    first_close = range_end + datetime.timedelta(minutes=BAR_MIN)
    if at is not None:
        cutoff = datetime.datetime.combine(day, at)
        if explicit_day is None and cutoff > now + datetime.timedelta(seconds=59):
            raise ScanError("--at %s is later than now (%s IST)." % (hm(at), hm(now)))
        if cutoff > close_dt:
            raise ScanError("--at %s is after the %s close on %s." % (hm(at), hm(close_dt), day_label(day)))
    else:
        cutoff = now.replace(second=0, microsecond=0)
        if cutoff >= close_dt:
            raise ScanError("The market closed at %s IST on %s. Replay the session with --at 10:15."
                            % (hm(close_dt), day_label(day)))
    if cutoff < first_close:
        raise ScanError("It is %s IST: the %d-minute opening range runs %s to %s and the first bar after it "
                        "closes at %s. Run orb again after %s."
                        % (hm(cutoff), or_minutes, hm(open_dt), hm(range_end), hm(first_close), hm(first_close)))
    if until is not None and cutoff.time() > until:
        raise ScanError("It is %s IST, after %s, your last time for new entries (no_entries_after): an "
                        "opening-range breakout found now is expired. Run orb tomorrow after %s, or replay "
                        "today with --at 10:15." % (hm(cutoff), hm(until), hm(first_close)))
    return day, cutoff


def _turnover(bars):
    if any(b.get("v") is None for b in bars):
        return None
    return sum(b["c"] * b["v"] for b in bars)


def _orb_hit_row(sym, h, info):
    row = dict({"symbol": sym, "exchange": "NSE", "setups": ["orb"]}, **h)
    extra = (info or {}).get(sym) or {}
    if extra.get("name"):
        row["name"] = extra["name"]
    if extra.get("fno") is not None:
        row["fno"] = extra["fno"]
    return row


def _finish(hits, counts):
    hits.sort(key=lambda x: (x["_t"], x["symbol"]))
    for h in hits:
        del h["_t"]
    counts["hits"] = len(hits)
    return hits


def scan_orb(stage1, symbols, day, cutoff, or_minutes, min_price=0.0, min_turnover=0.0, confirm=None,
             max_confirm=None, max_missing=MAX_MISSING, info=None, cfg=None):
    """Yahoo two-stage scan. stage1 = {SYM: series} (spark or v8). confirm(sym) -> series with
    highs and lows (None = the stage-1 series has them). Returns (hits, counts, failed)."""
    n_range = or_minutes // BAR_MIN
    counts = {"requested": len(symbols), "fetched": 0, "unusable": 0, "illiquid": 0, "scanned": 0,
              "candidates": 0, "confirmed": 0, "unconfirmed": 0}
    hits, failed, cands = [], {}, []
    for sym in symbols:
        s = stage1.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = session_bars(s, day, cutoff, cfg)
        if not usable(bars, day, cutoff, n_range, max_missing, cfg):
            counts["unusable"] += 1
            continue
        turn = _turnover(bars)
        if turn is None:
            meta = s.get("meta") or {}
            turn = (meta.get("regularMarketVolume") or 0) * bars[-1]["c"]
        if bars[-1]["c"] < min_price or turn < min_turnover:
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        if orb_candidate(bars, day, n_range, cfg):
            cands.append((sym, bars, turn))
    counts["candidates"] = len(cands)
    if confirm is not None and max_confirm and len(cands) > max_confirm:
        cands.sort(key=lambda x: -x[2])
        counts["unconfirmed"] = len(cands) - max_confirm
        cands = cands[:max_confirm]
    for sym, bars, _ in cands:
        full = bars
        if confirm is not None:
            try:
                full = session_bars(confirm(sym), day, cutoff, cfg)
            except (ScanError, nse.NSEError) as e:
                failed[sym] = str(e)
                continue
            if not usable(full, day, cutoff, n_range, max_missing, cfg):
                failed[sym] = "the v8 chart is missing bars"
                continue
        counts["confirmed"] += 1
        h = orb_hit(full, day, n_range, cfg)
        if h:
            hits.append(_orb_hit_row(sym, h, info))
    return _finish(hits, counts), counts, failed


def scan_orb_dhan(snapshot, sid_of, symbols, day, cutoff, or_minutes, fetch_bars, min_price=0.0,
                  min_turnover=0.0, max_confirm=None, max_missing=MAX_MISSING, info=None, cfg=None):
    """Dhan two-stage scan. snapshot = Dhan /marketfeed/ohlc data for NSE_EQ ({security id:
    {"last_price", "ohlc": {open, high, low, close}}}). Stage 1: the day's high is above the
    day's open (necessary: a breakout close beats the range high, which is >= the open).
    fetch_bars(sym) -> nse bars from /charts/intraday. Returns (hits, counts, failed)."""
    n_range = or_minutes // BAR_MIN
    counts = {"requested": len(symbols), "fetched": 0, "unusable": 0, "illiquid": 0, "scanned": 0,
              "candidates": 0, "confirmed": 0, "unconfirmed": 0}
    hits, failed, cands = [], {}, []
    for sym in symbols:
        sid = sid_of.get(sym)
        if sid is None:
            failed[sym] = "not in Dhan's NSE equity list"
            continue
        q = snapshot.get(str(sid))
        if not q:
            failed[sym] = "no quote from Dhan"
            continue
        counts["fetched"] += 1
        ohlc = q.get("ohlc") or {}
        lp = float(q.get("last_price") or 0)
        o, h = float(ohlc.get("open") or 0), float(ohlc.get("high") or 0)
        if lp < min_price or lp <= 0:
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        if o > 0 and h > o:
            cands.append((sym, h / o - 1))
    counts["candidates"] = len(cands)
    cands.sort(key=lambda x: (-x[1], x[0]))
    if max_confirm and len(cands) > max_confirm:
        counts["unconfirmed"] = len(cands) - max_confirm
        cands = cands[:max_confirm]
    for sym, _ in cands:
        try:
            bars = session_bars(series_from_bars(fetch_bars(sym)), day, cutoff, cfg)
        except (ScanError, nse.NSEError) as e:
            failed[sym] = str(e)
            continue
        if not usable(bars, day, cutoff, n_range, max_missing, cfg):
            failed[sym] = "Dhan's bars are missing some 5-minute bars"
            continue
        turn = _turnover(bars) or 0
        if turn < min_turnover:
            counts["illiquid"] += 1
            continue
        counts["confirmed"] += 1
        hit = orb_hit(bars, day, n_range, cfg)
        if hit:
            hits.append(_orb_hit_row(sym, hit, info))
    return _finish(hits, counts), counts, failed


# ---------------------------------------------------------------- fetching

def progress(a, msg):
    if not getattr(a, "json", False) and not getattr(a, "quiet", False):
        print(msg, file=sys.stderr)


def fetch_spark(symbols, rng, interval, pause, a=None):
    """Batches of 20. -> ({SYM: series}, {SYM: error})."""
    data, failed = {}, {}
    batches = [symbols[i:i + SPARK_MAX] for i in range(0, len(symbols), SPARK_MAX)]
    for k, batch in enumerate(batches):
        if k and pause:
            SLEEP(pause)
        wanted = dict((to_yahoo(s).upper(), s) for s in batch)
        try:
            got = parse_spark(yahoo_get(spark_path(batch, rng, interval)), wanted)
        except (ScanError, ValueError) as e:
            for s in batch:
                failed[s] = str(e)
            continue
        for s in batch:
            if s in got:
                data[s] = got[s]
            else:
                failed[s] = "no data from Yahoo"
        if a is not None and len(batches) > 25 and (k + 1) % 25 == 0:
            progress(a, "  spark %d of %d requests" % (k + 1, len(batches)))
    return data, failed


def symbols_from_file(path):
    if not os.path.exists(path):
        raise ScanError("no file at %s" % path)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        items = doc.get("hits", []) if isinstance(doc, dict) else doc
        raw = [str(x.get("symbol") if isinstance(x, dict) else x) for x in items if x]
    else:
        raw = []
        for ln in text.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                raw.append(ln.split("|")[0].strip())
    return [_ours(s) for s in raw if s]


def read_symbols(a, lists, offline_syms=None):
    """-> (symbols, universe label, {sym: why dropped})."""
    dropped = {}
    if a.symbols:
        syms = [_ours(s) for s in re.split(r"[,\s]+", a.symbols) if s]
        label = "symbols"
    elif a.watchlist:
        syms, label = symbols_from_file(a.watchlist), "watchlist"
    elif offline_syms is not None and not a.universe:
        syms, label = sorted(offline_syms), "offline"
    else:
        which = a.universe or "nifty500"
        if which in ("nifty500", "all"):
            kept, _ = build_universe(lists, which)
            syms, label = [s for s, _ in kept], which
        else:
            syms, label = symbols_from_file(which), os.path.basename(which)
    out = []
    for s in syms:
        if s not in out:
            out.append(s)
    if label in ("symbols", "watchlist") or label not in ("nifty500", "all", "offline"):
        out, dropped = check_series(out, lists)
    if a.limit:
        out = out[:a.limit]
    if not out:
        raise ScanError("no symbols to scan" + (" (%s)" % "; ".join("%s: %s" % kv for kv in dropped.items())
                                                 if dropped else ""))
    return out, label, dropped


def symbol_info(symbols, lists, mark_fno=True):
    fo = lists.fno() if mark_fno else set()
    info = {}
    for s in symbols:
        info[s] = {"name": lists.name(s), "fno": (s in fo) if fo else None}
    return info


def data_source(a, cfg):
    want = (a.source or cfg.get("data_source") or "auto").lower()
    cred = nse.dhan_credentials()
    if want == "dhan" and not cred:
        raise ScanError("data_source is dhan but DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are not set in your shell")
    if want not in ("auto", "yahoo", "dhan"):
        raise ScanError("data_source wants auto, yahoo or dhan")
    return "dhan" if (want == "dhan" or (want == "auto" and cred)) else "yahoo"


def lists_from(a):
    return Lists(a.equity_list, a.index_list, a.fo_list)


def default_out():
    return os.path.join(nse.trader_dir(), "watchlist.json")


# ---------------------------------------------------------------- commands

def cmd_universe(a):
    lists = lists_from(a)
    kept, counts = build_universe(lists, a.universe or "nifty500")
    fo = lists.fno()
    out = a.out or os.path.join(nse.trader_dir(), "universe.txt")
    if not a.no_write:
        d = os.path.dirname(os.path.abspath(out))
        os.makedirs(d, exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            for sym, name in kept:
                fh.write("%s|%s%s\n" % (sym, name, "|F&O" if sym in fo else ""))
    dropped = dict((k, v) for k, v in counts.items() if k != "listed")
    nfo = sum(1 for s, _ in kept if s in fo)
    if a.json:
        print(json.dumps({"universe": a.universe or "nifty500", "count": len(kept), "listed": counts.get("listed"),
                          "fno": nfo if fo else None, "dropped": dropped, "notes": lists.notes,
                          "out": None if a.no_write else out}, indent=2))
    else:
        print("%s: %s listed, kept %s series-EQ stocks (intraday allowed)%s." % (
            "Nifty 500" if (a.universe or "nifty500") == "nifty500" else "NSE EQUITY_L",
            "{:,}".format(counts.get("listed", 0)), "{:,}".format(len(kept)),
            (", %d of them with F&O" % nfo) if fo else ""))
        if dropped:
            print("Dropped: " + ", ".join("%s %s" % ("{:,}".format(v), k) for k, v in
                                          sorted(dropped.items(), key=lambda kv: -kv[1]))
                  + " (BE/BZ are trade-to-trade: delivery only, no intraday).")
        for n in lists.notes:
            print("note: " + n)
        if not a.no_write:
            print("Wrote %s (one SYMBOL|Name[|F&O] a line)." % out)
    return 0 if kept else 1


def cmd_daily(a):
    t0 = time.time()
    now = parse_now(a.now)
    cfg = nse.load_config(a.config) if a.config else nse.load_config()
    lists = lists_from(a)
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
    symbols, label, dropped = read_symbols(a, lists, offline_syms)
    if not a.offline:
        progress(a, "Fetching daily closes for %s NSE symbols (%d spark requests, %.1f s apart)..."
                 % ("{:,}".format(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX, a.pause))
        data, failed = fetch_spark(symbols, "3mo", "1d", a.pause, a)
    info = symbol_info(symbols, lists, not a.no_fno)
    min_turn = a.min_turnover * CRORE
    hits, counts, last_date = scan_daily(data, symbols, now, a.min_price, min_turn, info, cfg)
    if a.fno_only:
        hits = [h for h in hits if h.get("fno")]
        counts["hits"] = len(hits)
    nfailed = len(symbols) - counts["fetched"]
    doc = {"created_by": "india-scanner", "created_at": nse.iso_ist(now), "mode": "daily",
           "as_of": ("%s close" % last_date.isoformat()) if last_date else None,
           "universe": len(symbols), "universe_name": label, "scanned": counts["scanned"], "failed": nfailed,
           "source": "yahoo", "exchange": "NSE",
           "filters": {"min_price": a.min_price, "min_turnover_cr": a.min_turnover, "series": "EQ",
                       "fno_only": bool(a.fno_only)},
           "note": NOTE, "hits": hits}
    if dropped:
        doc["dropped"] = dropped
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    out = a.out or default_out()
    if not a.no_write:
        nse.write_json(out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, notes=lists.notes, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    print("Daily scan, %s IST, closes through %s (Yahoo spark, daily closes only, NSE)." % (
        now.strftime("%a %Y-%m-%d %H:%M"), last_date.isoformat() if last_date else "nothing"))
    s = nse.session(now.date(), cfg)
    if s and s[0] <= now < s[1]:
        print("The session is open: today's bar is left out and the turnover is today's so far.")
    print("Fetched %s of %s charts%s. %s passed the filters (%s+ and %s crore+ last-session turnover)%s; "
          "%s scanned." % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
                           (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
                           "{:,}".format(counts["fetched"] - counts["illiquid"]), nse.inr(a.min_price, 0),
                           _n(a.min_turnover),
                           (", %d had under %d days" % (counts["short"], MIN_DAILY_BARS)) if counts["short"] else "",
                           "{:,}".format(counts["scanned"])))
    for sym, why in sorted(dropped.items()):
        print("  skipped %s: %s" % (sym, why))
    if hits:
        per = [(k, sum(1 for h in hits if k in h["setups"])) for k, _ in SETUPS]
        print("%d chart%s matched a setup: %s." % (len(hits), "" if len(hits) == 1 else "s",
                                                  ", ".join("%d %s" % (n, LABEL[k]) for k, n in per if n)))
        print("Most traded first (last session's turnover):")
    else:
        print("No setups.")
    for h in hits[:a.show]:
        print("  %-11s %13s  RSI %5.1f  %7s cr%s  %s" % (
            h["symbol"], nse.inr(h["price"]), h["rsi"],
            "-" if h["turnover_cr"] is None else "{:,.0f}".format(h["turnover_cr"]),
            "  F&O" if h.get("fno") else "     ", ", ".join(LABEL[t] for t in h["setups"])))
    if len(hits) > a.show:
        print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    for n in lists.notes:
        print("note: " + n)
    if not a.no_write:
        print("Wrote %s (mode daily, %d hit%s)." % (out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " " + ADVICE + " Next: pick one chart, then /india-news SYMBOL.")
    return 0 if hits else 1


def _n(x):
    x = float(x)
    return "{:,}".format(int(x)) if x == int(x) else "%.2f" % x


def _dhan_fetchers(a, day, cutoff):
    dhan = nse.Dhan.from_env()
    scrip = nse.scrip_master()
    start = datetime.datetime.combine(day, nse.OPEN)

    def fetch_bars(sym):
        return dhan.intraday(nse.security_id(sym, scrip), start, cutoff, interval=BAR_MIN)
    return dhan, scrip, fetch_bars


def cmd_orb(a):
    t0 = time.time()
    now = parse_now(a.now)
    at = parse_hhmm(a.at) if a.at else None
    cfg = nse.load_config(a.config) if a.config else nse.load_config()
    until = parse_hhmm(a.until or cfg.get("no_entries_after") or "14:30", "--until")
    lists = lists_from(a)
    data = failed = None
    explicit_day = None
    offline_syms = None
    source = "yahoo"
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
        if not a.now:
            days = [s["bars"][-1]["t"].date() for s in data.values() if s["bars"]]
            if days:
                explicit_day = max(days)
                if at is None:
                    raise ScanError("offline replay: give --at HH:MM or --now \"YYYY-MM-DD HH:MM\"")
    else:
        source = data_source(a, cfg)
    day, cutoff = orb_window(now, at, a.or_minutes, until, explicit_day, cfg)
    symbols, label, dropped = read_symbols(a, lists, offline_syms)
    info = symbol_info(symbols, lists, not a.no_fno)
    min_turn = a.min_turnover * CRORE
    if a.offline:
        hits, counts, failed2 = scan_orb(data, symbols, day, cutoff, a.or_minutes, a.min_price, min_turn, None,
                                         None, a.max_missing, info, cfg)
    elif source == "dhan":
        dhan, scrip, fetch_bars = _dhan_fetchers(a, day, cutoff)
        sid_of = dict((s, (scrip.get(s) or {}).get("security_id")) for s in symbols)
        sid_of = dict((s, v) for s, v in sid_of.items() if v)
        progress(a, "Stage 1: Dhan OHLC snapshot for %s symbols (%d requests)..."
                 % ("{:,}".format(len(symbols)), (len(sid_of) + 999) // 1000))
        snapshot = dhan.ohlc(list(sid_of.values()))
        failed = {}
        hits, counts, failed2 = scan_orb_dhan(snapshot, sid_of, symbols, day, cutoff, a.or_minutes, fetch_bars,
                                              a.min_price, min_turn, a.max_confirm, a.max_missing, info, cfg)
    else:
        today = nse.now_ist().date()
        if (today - day).days > 6:
            raise ScanError("Yahoo's free 5-minute history is short: replay %s with --offline" % day.isoformat())
        rng = "1d" if day == today else "5d"
        progress(a, "Stage 1: 5-minute closes for %s NSE symbols (%d spark requests)..."
                 % ("{:,}".format(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, rng, "%dm" % BAR_MIN, a.pause, a)
        done = []

        def confirm(sym):
            if a.pause:
                SLEEP(a.pause)
            done.append(sym)
            if len(done) % 50 == 0:
                progress(a, "  stage 2: %d v8 charts so far" % len(done))
            return parse_chart(yahoo_get(chart_path(sym, rng, "%dm" % BAR_MIN)), sym)
        hits, counts, failed2 = scan_orb(data, symbols, day, cutoff, a.or_minutes, a.min_price, min_turn, confirm,
                                         a.max_confirm, a.max_missing, info, cfg)
    if a.fno_only:
        hits = [h for h in hits if h.get("fno")]
        counts["hits"] = len(hits)
    failed = dict(failed or {})
    failed.update(failed2)
    nfailed = len([s for s in symbols if s in failed])
    doc = {"created_by": "india-scanner", "created_at": nse.iso_ist(now), "mode": "orb",
           "as_of": "%s %s IST" % (day.isoformat(), hm(cutoff)), "universe": len(symbols), "universe_name": label,
           "scanned": counts["scanned"], "failed": nfailed, "unconfirmed": counts["unconfirmed"],
           "source": source, "exchange": "NSE", "or_minutes": a.or_minutes, "interval": "%dm" % BAR_MIN,
           "opening_range": "%s-%s" % (hm(slot(day, 0, cfg)), hm(slot(day, a.or_minutes // BAR_MIN, cfg))),
           "filters": {"min_price": a.min_price, "min_turnover_cr": a.min_turnover, "series": "EQ",
                       "fno_only": bool(a.fno_only), "max_missing": a.max_missing},
           "note": NOTE, "hits": hits}
    if dropped:
        doc["dropped"] = dropped
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    out = a.out or default_out()
    if not a.no_write:
        nse.write_json(out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, notes=lists.notes, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    print("Opening-range breakout scan, %s %s IST: %d-minute range (%s to %s), 5-minute bars, %s."
          % (day_label(day), hm(cutoff), a.or_minutes, hm(slot(day, 0, cfg)),
             hm(slot(day, a.or_minutes // BAR_MIN, cfg)), "Dhan" if source == "dhan" else "Yahoo"))
    if not nse.calendar_known(day):
        print("note: NSE's %d holiday list is not in this pack yet; check the day is a trading day." % day.year)
    print("Fetched %s of %s charts%s; %s usable%s; %s candidates, %s confirmed with real highs and lows."
          % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
             (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
             "{:,}".format(counts["scanned"]),
             (" (%s under the price / turnover floor)" % "{:,}".format(counts["illiquid"])) if counts["illiquid"]
             else "", "{:,}".format(counts["candidates"]), "{:,}".format(counts["confirmed"])))
    for sym, why in sorted(dropped.items()):
        print("  skipped %s: %s" % (sym, why))
    if counts["unconfirmed"]:
        print("PARTIAL: %s more candidates were not checked (only the first %s are; --max-confirm 0 checks all)."
              % ("{:,}".format(counts["unconfirmed"]), "{:,}".format(a.max_confirm)))
    if hits:
        print("%d of %s charts closed above their opening-range high by %s IST:"
              % (len(hits), ("the %s checked" % "{:,}".format(counts["confirmed"])) if counts["unconfirmed"]
                 else "{:,}".format(counts["scanned"]), hm(cutoff)))
        for h in hits[:a.show]:
            print("  %5s  %-11s %13s  range %s to %s  %+.2f%% over%s" % (
                h["at"], h["symbol"], nse.inr(h["price"]), _p(h["or_low"]), _p(h["or_high"]), h["pct_over"],
                "  F&O" if h.get("fno") else ""))
        if len(hits) > a.show:
            print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    else:
        print("No chart closed above its opening-range high by %s IST." % hm(cutoff))
    for n in lists.notes:
        print("note: " + n)
    if not a.no_write:
        print("Wrote %s (mode orb, %d hit%s)." % (out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " " + ADVICE + " Next: pick one chart, then /india-news SYMBOL and /india-entry SYMBOL.")
    return 0 if hits else 1


def _p(x):
    return "{:,.2f}".format(x)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Chart Scanner (india-scanner): NSE daily setups and "
                                             "opening-range breakouts.")
    sub = ap.add_subparsers(dest="cmd")

    def lists_args(p):
        p.add_argument("--equity-list", metavar="CSV", help="NSE EQUITY_L.csv on disk instead of the cache")
        p.add_argument("--index-list", metavar="CSV", help="NSE ind_nifty500list.csv on disk instead of the cache")
        p.add_argument("--fo-list", metavar="CSV", help="NSE fo_mktlots.csv on disk instead of the cache")

    p = sub.add_parser("universe", help="build the NSE symbol list -> trader/universe.txt")
    p.add_argument("--universe", choices=["nifty500", "all"], default="nifty500")
    p.add_argument("--out")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--json", action="store_true")
    lists_args(p)

    def common(p, min_turnover):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--symbols", help="comma-separated NSE symbols instead of the universe")
        g.add_argument("--watchlist", metavar="FILE", help="a watchlist.json (hits) or a text file of symbols")
        p.add_argument("--universe", help="nifty500 (default), all, or a file with one SYMBOL|Name a line")
        p.add_argument("--limit", type=int, help="scan only the first N symbols")
        p.add_argument("--min-price", type=float, default=50.0, help="rupees (50)")
        p.add_argument("--min-turnover", type=float, default=min_turnover, help="rupees crore (%s)" % min_turnover)
        p.add_argument("--fno-only", action="store_true", help="keep only stocks with F&O")
        p.add_argument("--no-fno", action="store_true", help="do not read NSE's F&O list")
        p.add_argument("--offline", metavar="PATH", help="fixture folder or file instead of the network")
        p.add_argument("--now", help='pretend it is this time ("2026-10-29 10:15", IST)')
        p.add_argument("--config", help="trader/config.json (default: in TRADER_DIR)")
        p.add_argument("--out", help="default trader/watchlist.json")
        p.add_argument("--no-write", action="store_true")
        p.add_argument("--pause", type=float, default=PAUSE, help="seconds between requests (%.1f)" % PAUSE)
        p.add_argument("--show", type=int, default=50, help="hits to print (all are written)")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")
        lists_args(p)

    p = sub.add_parser("daily", help="pre-market setups from daily closes")
    common(p, 10.0)
    p = sub.add_parser("orb", help="opening-range breakouts so far today")
    common(p, 0.0)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--max-confirm", type=int, default=MAX_CONFIRM,
                   help="stage-2 charts to fetch (%d; 0 = all)" % MAX_CONFIRM)
    p.add_argument("--max-missing", type=int, default=MAX_MISSING,
                   help="5-minute bars a chart may miss after the range and stay usable (%d)" % MAX_MISSING)
    p.add_argument("--at", help="cut the clock at this IST time, HH:MM")
    p.add_argument("--until", help="no new entries after HH:MM (default: config no_entries_after, 14:30)")
    p.add_argument("--source", choices=["auto", "yahoo", "dhan"], help="default: config data_source (auto)")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb}[a.cmd](a)
    except (ScanError, nse.NSEError) as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("scan.py: stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
