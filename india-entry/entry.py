#!/usr/bin/env python3
"""Entry Timer for india-entry: watches an NSE setup bar by bar and says the
moment to get in. Standard library only. Long side only. It never places an order.

    python3 entry.py check RELIANCE                         # 15-minute opening range, 5-minute bars
    python3 entry.py check RELIANCE --now "2026-10-29 09:40" --offline RELIANCE.json
    python3 entry.py check RELIANCE --setup level --level 1182.10
    python3 entry.py check RELIANCE --interval 1m --min-volume-x 1.5 --write
    python3 entry.py watch RELIANCE --every 30 --until 14:30
    python3 entry.py watchlist                              # every hit in trader/watchlist.json

Setups (longs only):
  orb    opening-range breakout. The range is the high and low of the first
         --or-minutes (default 15: the 09:15, 09:20 and 09:25 five-minute
         bars, IST). It triggers on the first COMPLETED bar after the range
         that CLOSES above the range high. A wick above the high is not a trigger.
  level  the first completed bar that closes above --level (for example a
         20-day high from india-scanner).
  --min-volume-x X  the trigger bar also needs at least X times the average
         volume of the opening-range bars that report volume (Yahoo's 09:15
         five-minute NSE bar often shows 0; it is left out of the average).

A trigger's time is the trigger bar's CLOSE time (the 09:35 bar closes at
09:40 IST) and its price is that close. Only bars on the interval grid from
09:15 count (Yahoo's live tick, e.g. 15:29:03, is dropped) and a bar counts
only once it has closed: now >= its start + the interval.

Statuses: triggered; waiting (pre-open, range still forming, or no close above
the level yet); expired (no trigger by no_entries_after, default 14:30 IST);
no-data (the market is shut today, or no bars came).

trader/config.json (optional): "no_entries_before" (09:30), "no_entries_after"
(14:30), "flat_by" (15:05), "data_source" (auto | yahoo | dhan), "exchange"
(NSE), "extra_holidays", "special_sessions" (Muhurat trading: on a special
session the range starts at its open and entries stop 30 minutes before the
flat-by time, which is 5 minutes before its close).

Data: data_source auto uses Dhan's /v2/charts/intraday when DHAN_CLIENT_ID and
DHAN_ACCESS_TOKEN are set (needs Dhan's Data plan), else Yahoo's free chart
JSON for SYMBOL.NS (unofficial, can lag). --offline PATH reads a saved Yahoo
chart JSON (.json or .json.gz) or PATH/<SYMBOL>.json from a folder; without
--now an offline file is read as of its own time (meta.regularMarketTime).

Exit codes: check 0 = triggered, 1 = anything else or an error. watch 0 on a
trigger, 1 otherwise. watchlist 0 when at least one symbol triggered.
"""

import argparse
import datetime
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "india-common"))
import nse  # noqa: E402

GRACE = 2        # seconds after a bar closes before watch asks for it
SETTLE = 60      # seconds a closed bar may wait for Yahoo's data to catch up
INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"no_entries_before": "09:30", "no_entries_after": "14:30"}
SPECIAL_CUTOFF = 30   # minutes before flat-by: the last new entry on a special session


class EntryError(nse.NSEError):
    pass


# ---------------------------------------------------------------- time

def hm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def parse_now(text):
    """--now -> naive IST. '2026-10-29 09:40', '...T09:40', or just '09:40' for today."""
    if not text:
        return nse.now_ist().replace(microsecond=0)
    t = text.strip()
    if t.upper().endswith(" IST"):
        t = t[:-4].strip()
    if len(t) <= 5 and ":" in t:
        t = "%s %s" % (nse.now_ist().date().isoformat(), t.zfill(5))
    try:
        return nse.parse_now(t)
    except nse.NSEError:
        raise EntryError('--now wants an IST time like "2026-10-29 09:40"')


def parse_hhmm(text, what):
    try:
        return nse.hhmm(text)
    except nse.NSEError:
        raise EntryError("%s wants an IST time like 14:30, not %r" % (what, text))
    except ValueError:
        raise EntryError("%s wants an IST time like 14:30, not %r" % (what, text))


def day_status(d, cfg):
    """(open_dt, close_dt, why_closed)."""
    s = nse.session(d, cfg)
    if s:
        return s[0], s[1], ""
    if d.weekday() >= 5 and d.isoformat() not in nse.MUHURAT:
        return None, None, "weekend"
    name = nse.holiday_name(d, cfg)
    if d.isoformat() in nse.MUHURAT:
        return None, None, ("NSE holiday: %s. Muhurat trading is set for this evening but its hour is not "
                            "announced in this pack: add it to trader/config.json special_sessions" % (name or "Diwali"))
    return None, None, "NSE holiday: %s" % name


def session_flat_by(cfg, day):
    """nse.flat_by, except on an evening special session (Muhurat) where the default
    15:05 would fall before the session opens: then 5 minutes before its close."""
    fb = nse.flat_by(cfg, day)
    sp = nse.special_session(day, cfg)
    if sp and not (sp[0] < fb <= sp[1]):
        fb = (datetime.datetime.combine(day, sp[1]) - datetime.timedelta(minutes=5)).time()
    return fb


def next_open(now, cfg):
    s = nse.next_session(now, cfg)
    return s[0].strftime("%Y-%m-%d %H:%M") if s else None


# ---------------------------------------------------------------- data

def parse_chart(text, sym):
    """Yahoo chart JSON -> {symbol, meta, bars, prev_close, data_time, granularity}."""
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        raise EntryError("Yahoo returned something unexpected for %s" % sym)
    try:
        meta, bars = nse.parse_yahoo_chart(doc)
    except nse.NSEError as e:
        raise EntryError("Yahoo has no data for %s: %s" % (sym, str(e).replace("Yahoo: ", "")))
    except (AttributeError, TypeError, IndexError):
        raise EntryError("Yahoo returned something unexpected for %s" % sym)
    rmt = meta.get("regularMarketTime")
    return {"symbol": nse.clean_symbol(meta.get("symbol") or sym), "meta": meta, "bars": bars,
            "prev_close": meta.get("previousClose") or meta.get("chartPreviousClose"),
            "data_time": nse.ist_from_epoch(rmt) if rmt else None,
            "granularity": meta.get("dataGranularity"), "source": "yahoo"}


def chart_range(day, today, interval):
    """The smallest Yahoo range that still holds the session of `day`."""
    back = (today - day).days
    if back <= 0:
        return "1d"
    if back <= 6:
        return "5d"
    if interval == "1m":
        raise EntryError("Yahoo keeps 1-minute bars for about a week: use --interval 5m for %s" % day)
    if back <= 28:
        return "1mo"
    if back <= 58:
        return "60d"
    raise EntryError("Yahoo keeps 5-minute bars for about 60 days: %s is too far back" % day)


def _http(url):
    """Swapped out in tests."""
    return nse.http_get(url)


def _sleep(seconds):
    time.sleep(seconds)


def yahoo_chart(sym, interval, day, today, exchange="NSE"):
    rng = chart_range(day, today, interval)
    last = None
    for host in nse.YAHOO_HOSTS:
        try:
            ch = parse_chart(_http(nse.yahoo_chart_url(sym, rng, interval, host, exchange)), sym)
            return ch
        except nse.NSEError as e:
            last = e
            if "no data" in str(e).lower():
                raise
            _sleep(nse.PAUSE)
    raise EntryError("Yahoo did not answer (%s): wait a minute and try again" % last)


def dhan_chart(sym, interval, day, cfg, dhan=None, scrip=None):
    """Dhan /v2/charts/intraday for one session (needs Dhan's Data plan)."""
    dhan = dhan or nse.Dhan.from_env()
    scrip = scrip if scrip is not None else nse.scrip_master()
    sid = nse.security_id(sym, scrip)
    o, c, _ = day_status(day, cfg)
    if not o:
        o = datetime.datetime.combine(day, nse.OPEN)
        c = datetime.datetime.combine(day, nse.CLOSE)
    bars = dhan.intraday(sid, o, c, interval=INTERVALS[interval] // 60)
    return {"symbol": nse.clean_symbol(sym), "meta": {}, "bars": bars, "prev_close": None,
            "data_time": None, "granularity": interval, "source": "dhan"}


def offline_file(path, sym):
    if os.path.isdir(path):
        for name in (sym + ".json", sym + ".json.gz", sym + ".NS.json", sym + ".NS.json.gz"):
            p = os.path.join(path, name)
            if os.path.exists(p):
                return p
        raise EntryError("no offline file for %s in %s (want %s.json)" % (sym, path, sym))
    if not os.path.exists(path):
        raise EntryError("no offline file %s" % path)
    return path


def pick_source(cfg, env=None):
    src = str((cfg or {}).get("data_source") or "auto").lower()
    if src not in ("auto", "yahoo", "dhan"):
        raise EntryError("data_source is auto, yahoo or dhan, not %r" % src)
    if src == "auto":
        return "dhan" if nse.dhan_credentials(env) else "yahoo"
    return src


def get_chart(sym, interval, day=None, offline=None, today=None, cfg=None):
    """Bars for one symbol: Dhan or Yahoo (per data_source) or a saved Yahoo file."""
    sym = nse.clean_symbol(sym)
    cfg = cfg or {}
    exchange = str(cfg.get("exchange") or "NSE").upper()
    if offline:
        f = offline_file(offline, sym)
        try:
            text = nse.read_maybe_gz(f).decode("utf-8", "replace")
        except OSError as e:
            raise EntryError("could not read %s: %s" % (f, e))
        ch = parse_chart(text, sym)
        ch["source"] = "offline"
    else:
        today = today or nse.now_ist().date()
        day = day or today
        src = pick_source(cfg)
        ch = None
        if src == "dhan":
            try:
                ch = dhan_chart(sym, interval, day, cfg)
            except nse.NSEError as e:
                if str(cfg.get("data_source") or "auto").lower() == "dhan":
                    raise EntryError("Dhan: %s" % e)
                ch = None
                note = "Dhan did not answer (%s); used Yahoo instead" % e
        if ch is None:
            ch = yahoo_chart(sym, interval, day, today, exchange)
            if src == "dhan":
                ch["note"] = note
    if ch["symbol"] != sym:
        raise EntryError("the chart file holds %s, not %s" % (ch["symbol"], sym))
    gran = ch.get("granularity")
    if gran and gran != interval:
        raise EntryError("%s has %s bars, not %s: use --interval %s" % (sym, gran, interval, gran))
    return ch


# ---------------------------------------------------------------- the rule

def read_config(path=None):
    try:
        cfg = nse.load_config(path)
    except nse.NSEError as e:
        raise EntryError(str(e))
    out = dict(cfg)
    for k, v in DEFAULTS.items():
        if not out.get(k):
            out[k] = v
        parse_hhmm(out[k], k)
    return out


def r2(x):
    return None if x is None else round(float(x), 2) + 0.0


def entry_window(day, open_dt, close_dt, cfg, or_minutes):
    """(no entries before, no entries after) as datetimes."""
    if nse.special_session(day, cfg):
        before = open_dt + datetime.timedelta(minutes=or_minutes)
        fb = datetime.datetime.combine(day, session_flat_by(cfg, day))
        cutoff = max(before, fb - datetime.timedelta(minutes=SPECIAL_CUTOFF))
        return before, cutoff
    before = datetime.datetime.combine(day, parse_hhmm(cfg["no_entries_before"], "no_entries_before"))
    cutoff = datetime.datetime.combine(day, parse_hhmm(cfg["no_entries_after"], "no_entries_after"))
    fb = datetime.datetime.combine(day, nse.flat_by(cfg))
    return before, min(cutoff, fb)


def evaluate(chart, now, setup="orb", or_minutes=15, interval="5m", level=None, min_volume_x=None, cfg=None):
    """Pure function: the setup's status at `now` (naive IST) from one chart's bars."""
    cfg = cfg or dict(DEFAULTS)
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    if interval not in INTERVALS:
        raise EntryError("--interval is 5m or 1m")
    step = INTERVALS[interval]
    if or_minutes <= 0 or (or_minutes * 60) % step:
        raise EntryError("--or-minutes must be a whole number of %s bars" % interval)
    if setup not in ("orb", "level"):
        raise EntryError("--setup is orb or level")
    if setup == "level" and level is None:
        raise EntryError("--setup level needs --level PRICE (for example a 20-day high)")
    n_or = or_minutes * 60 // step
    day = now.date()
    res = {"symbol": chart["symbol"], "exchange": str(cfg.get("exchange") or "NSE").upper(), "setup": setup,
           "side": "long", "date": day.isoformat(), "interval": interval, "or_minutes": or_minutes,
           "now": now.strftime("%Y-%m-%d %H:%M:%S"), "status": "waiting", "phase": "", "reason": "",
           "opening_range": None, "level": None, "trigger": None, "last": None, "forming": None,
           "min_volume_x": min_volume_x, "prev_close": chart.get("prev_close"),
           "calendar_known": nse.calendar_known(day), "source": chart.get("source", "yahoo"), "_bars": []}
    if chart.get("note"):
        res["source_note"] = chart["note"]
    dt = chart.get("data_time")
    res["data_time"] = dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None
    open_dt, close_dt, why = day_status(day, cfg)
    if not open_dt:
        res.update(status="no-data", phase="closed", reason="the NSE is closed today (%s)" % why,
                   next_open=next_open(now, cfg))
        return res
    before, cutoff = entry_window(day, open_dt, close_dt, cfg, or_minutes)
    or_end = open_dt + datetime.timedelta(minutes=or_minutes)
    special = nse.special_session(day, cfg) is not None
    res.update(opens=hm(open_dt), closes=hm(close_dt), special_session=special,
               no_entries_before=hm(before), no_entries_after=hm(cutoff), flat_by=hm(session_flat_by(cfg, day)))

    by_slot = {}
    sstep = datetime.timedelta(seconds=step)
    for b in chart["bars"]:
        t = b["t"]
        if t.date() != day or not (open_dt <= t < close_dt) or t.second:
            continue
        off = int((t - open_dt).total_seconds())
        if off % step:
            continue                       # Yahoo's live tick (15:29:03) is not a bar
        slot = off // step
        by_slot[slot] = dict(b, end=t + sstep, slot=slot)
    bars, pending = [], None
    top = max(by_slot) if by_slot else -1
    for slot in sorted(by_slot):
        b = by_slot[slot]
        if b["end"] <= now:
            # Closed by the clock. On live data it counts once the feed has caught up: its last
            # trade is at or after the close, a later bar exists, or a minute has passed.
            if dt is None or dt >= b["end"] or slot < top or now >= b["end"] + datetime.timedelta(seconds=SETTLE):
                bars.append(b)
            else:
                pending = b
                res["forming"] = {"bar_start": hm(b["t"]), "closes": hm(b["end"]), "price": r2(b["c"])}
                res["lag_note"] = ("the feed's last trade is %s, before the %s bar's close: it counts once the "
                                   "feed catches up (a minute at most)" % (dt.strftime("%H:%M:%S"), hm(b["t"])))
        elif b["t"] <= now:
            res["forming"] = {"bar_start": hm(b["t"]), "closes": hm(b["end"]), "price": r2(b["c"])}
    res["_bars"] = bars
    res["bars_closed"] = len(bars)

    if now < open_dt:
        res.update(phase="pre-open", reason="the NSE opens at %s%s" % (hm(open_dt), (
            " and the %d-minute range is set at %s" % (or_minutes, hm(or_end))) if setup == "orb" else ""))
        return res

    or_bars = [b for b in bars if b["slot"] < n_or]
    vol_bars = [b for b in or_bars if b["v"] > 0]
    avg_vol = (sum(b["v"] for b in vol_bars) / float(len(vol_bars))) if vol_bars else None
    if or_bars:
        res["opening_range"] = {"high": r2(max(b["h"] for b in or_bars)), "low": r2(min(b["l"] for b in or_bars)),
                                "minutes": or_minutes, "bars": len(or_bars), "of": n_or,
                                "complete": now >= or_end, "avg_volume": int(round(avg_vol)) if avg_vol else None}
    if setup == "orb":
        if now < or_end:
            res.update(phase="forming", reason="%d of %d range bars closed; the range is set at %s" % (
                len(or_bars), n_or, hm(or_end)))
            return res
        if not or_bars:
            res.update(status="no-data", phase="open", reason="no bars came for the opening range")
            return res
        lvl = r2(max(b["h"] for b in or_bars))     # to the paisa: Yahoo's floats carry noise
    else:
        lvl = r2(level)
    res["level"] = r2(lvl)

    first_slot = n_or if setup == "orb" else 0
    trig, thin = None, []
    for b in bars:
        if b["slot"] < first_slot or b["end"] < before or b["end"] > cutoff:
            continue
        if r2(b["c"]) > lvl:
            vx = (b["v"] / avg_vol) if avg_vol else None
            if min_volume_x and (vx is None or vx < min_volume_x):
                thin.append(b)
                continue
            trig = b
            break
    last = bars[-1] if bars else None
    if last:
        res["last"] = {"bar_start": hm(last["t"]), "close": r2(last["c"]),
                       "vs_level_pct": r2(100.0 * (last["c"] / lvl - 1))}

    if trig:
        vx = (trig["v"] / avg_vol) if avg_vol else None
        res.update(status="triggered", phase="open" if now < close_dt else "after-close",
                   reason="the %s bar closed above %s" % (hm(trig["t"]), "the range high" if setup == "orb" else "the level"))
        res["trigger"] = {"time": hm(trig["end"]), "bar_start": hm(trig["t"]), "bar_index": trig["slot"],
                          "price": r2(trig["c"]), "close": trig["c"], "volume": trig["v"], "volume_x": r2(vx),
                          "pct_over": r2(100.0 * (trig["c"] / lvl - 1))}
        res["minutes_since"] = int((now - trig["end"]).total_seconds() // 60)
        if dt and dt < trig["end"]:
            res["data_note"] = ("the feed's last trade is %s, before this bar closed: its close can still move a tick"
                                % dt.strftime("%H:%M:%S"))
        return res
    if not bars:
        res.update(status="no-data", phase="open", reason="no bars came for %s yet" % day.isoformat())
        return res
    what = "close above the range high %s" % nse.inr(lvl) if setup == "orb" else "close above %s" % nse.inr(lvl)
    if now >= cutoff and not (pending and pending["end"] <= cutoff):
        res.update(status="expired", phase="after-close" if now >= close_dt else "after-cutoff",
                   reason="no %s by %s IST, the last time for a new entry%s" % (
                       what, hm(cutoff), " (special session)" if special else ""))
        return res
    res["phase"] = "open"
    if r2(last["c"]) > lvl and last in thin:
        res["reason"] = "the %s bar closed above at %s but on %.2fx the range's volume, under your %.2fx" % (
            hm(last["t"]), nse.inr(last["c"]), last["v"] / avg_vol if avg_vol else 0, min_volume_x)
    elif r2(last["c"]) > lvl and last["end"] < before:
        res["reason"] = "the %s bar closed above at %s, but no entries before %s (config)" % (
            hm(last["t"]), nse.inr(last["c"]), hm(before))
    else:
        gap = lvl - last["c"]
        res["reason"] = "last close %s (%s bar), %s %s %s (%+.2f%%)" % (
            nse.inr(last["c"]), hm(last["t"]), nse.inr(abs(gap)), "under" if gap >= 0 else "over",
            "the range high %s" % nse.inr(lvl) if setup == "orb" else "the level %s" % nse.inr(lvl),
            100.0 * (last["c"] / lvl - 1))
    return res


def public(res):
    """The result without the internal bar list, ready for JSON."""
    return {k: v for k, v in res.items() if not k.startswith("_")}


def entry_doc(res, now):
    """trader/entry.json, read by india-exit."""
    t = res.get("trigger") or {}
    orr = res.get("opening_range")
    return {"created_by": "india-entry", "symbol": res["symbol"], "exchange": res.get("exchange", "NSE"),
            "setup": res["setup"], "side": "long", "date": res["date"], "status": res["status"],
            "time": t.get("time"), "bar_start": t.get("bar_start"), "bar_index": t.get("bar_index"),
            "price": t.get("price"), "level": res.get("level"), "interval": res["interval"],
            "opening_range": {"high": orr["high"], "low": orr["low"], "minutes": orr["minutes"]} if orr else None,
            "source": res.get("source", "yahoo"), "checked_at": nse.iso_ist(now), "reason": res.get("reason")}


# ---------------------------------------------------------------- words

LABEL = {"triggered": "TRIGGERED", "waiting": "WAITING", "expired": "EXPIRED", "no-data": "NO DATA"}
PHASE_LABEL = {"pre-open": "NOT OPEN YET", "forming": "RANGE FORMING", "closed": "MARKET CLOSED"}
SHORT = {"pre-open": "not open", "forming": "forming", "closed": "closed"}
SOURCE = {"yahoo": "Yahoo, free, can lag", "offline": "saved Yahoo file", "dhan": "Dhan Data API"}


def setup_name(res):
    if res["setup"] == "orb":
        return "opening-range breakout (%d-minute range)" % res["or_minutes"]
    return "close above %s" % nse.inr(res["level"]) if res["level"] is not None else "close above a level"


def describe(res):
    """Plain lines for a person."""
    when = datetime.datetime.strptime(res["now"], "%Y-%m-%d %H:%M:%S")
    bars = "5-minute" if res["interval"] == "5m" else "1-minute"
    head = "%s  %s %s IST  %s on %s bars (%s)" % (
        res["symbol"], when.strftime("%a %Y-%m-%d"), when.strftime("%H:%M"), setup_name(res), bars,
        SOURCE.get(res.get("source"), res.get("source")))
    if res.get("data_time") and res["data_time"] <= res["now"] and res["data_time"][:10] == res["date"]:
        head += ", data to %s" % res["data_time"][11:]
    out = [head]
    if res.get("source_note"):
        out.append(res["source_note"] + ".")
    orr = res.get("opening_range")
    if orr:
        start = datetime.datetime.strptime(res["opens"], "%H:%M")
        end = start + datetime.timedelta(minutes=orr["minutes"])
        out.append("Opening range %s-%s: high %s, low %s (%d of %d bars%s)." % (
            hm(start), hm(end), nse.inr(orr["high"]), nse.inr(orr["low"]), orr["bars"], orr["of"],
            "" if orr["complete"] else ", still forming"))
    label = PHASE_LABEL.get(res["phase"]) or LABEL.get(res["status"], res["status"].upper())
    t = res.get("trigger")
    if t:
        out.append("TRIGGERED %s IST at %s: the %s bar (bar %d) closed above %s %s (%+.2f%%%s)." % (
            t["time"], nse.inr(t["price"]), t["bar_start"], t["bar_index"],
            "the range high" if res["setup"] == "orb" else "the level", nse.inr(res["level"]), t["pct_over"],
            (", %.2fx the range's volume" % t["volume_x"]) if res.get("min_volume_x") and t.get("volume_x") else ""))
        step = INTERVALS[res["interval"]] // 60
        late = res.get("minutes_since", 0) > step
        if late and res.get("last"):
            out.append("That was %d minutes ago; the last close is %s (%s bar). A late entry is a different "
                       "trade: the price, the stop distance and the odds have moved." % (
                           res["minutes_since"], nse.inr(res["last"]["close"]), res["last"]["bar_start"]))
        if res.get("data_note"):
            out.append(res["data_note"] + ".")
        if late:
            out.append("Nothing was placed.")
        else:
            out.append("This is the moment to get in, if you take the trade. Nothing was placed.")
        out.append("Next: set the stop and target BEFORE you buy: /india-exit (exit.py plan, reads trader/entry.json).")
    else:
        out.append("%s  %s." % (label, res["reason"]))
        f = res.get("forming")
        if res.get("lag_note") and res["status"] == "waiting":
            out.append(res["lag_note"][0].upper() + res["lag_note"][1:] + ".")
        elif f and res["status"] == "waiting" and res["phase"] in ("open", "forming"):
            out.append("The %s bar is still forming (%s so far, closes %s); it does not count until it closes." % (
                f["bar_start"], nse.inr(f["price"]), f["closes"]))
        if res["status"] == "expired":
            out.append("No entry on this setup today. Nothing was placed.")
        if res.get("next_open"):
            out.append("Next open: %s IST." % res["next_open"])
    if res.get("special_session"):
        out.append("Special session today (%s-%s IST): no new entries after %s, flat by %s." % (
            res["opens"], res["closes"], res["no_entries_after"], res["flat_by"]))
    if not res.get("calendar_known"):
        out.append("NSE's %s holiday list is not in this pack yet: check it, and add closures to trader/config.json "
                   "extra_holidays." % res["date"][:4])
    return out


def bar_line(b, res):
    """One watch line for a bar that just closed."""
    orr = res.get("opening_range") or {}
    lvl = res.get("level")
    t = res.get("trigger")
    n_or = orr.get("of") or 0
    if res["setup"] == "orb" and b["slot"] < n_or:
        seen = [x for x in res["_bars"] if x["slot"] <= b["slot"]]
        note = "range so far %s to %s" % (nse.inr(min(x["l"] for x in seen)), nse.inr(max(x["h"] for x in seen)))
        if b["slot"] == n_or - 1:
            note = "opening range set: high %s, low %s" % (nse.inr(orr["high"]), nse.inr(orr["low"]))
    elif t and t["bar_index"] == b["slot"]:
        note = "TRIGGERED: closed above %s (%+.2f%%). Get in now if you take it: %s IST, %s" % (
            nse.inr(lvl), t["pct_over"], t["time"], nse.inr(t["price"]))
    elif lvl is None:
        note = ""
    elif b["c"] > lvl:
        note = "above %s" % nse.inr(lvl)
        avg = orr.get("avg_volume")
        if res.get("min_volume_x") and avg:
            note += ", but volume %.2fx is under %.2fx" % (b["v"] / float(avg), res["min_volume_x"])
        elif b["end"] < datetime.datetime.combine(b["t"].date(), parse_hhmm(res["no_entries_before"], "x")):
            note += ", but no entries before %s" % res["no_entries_before"]
    else:
        note = "%s under %s" % (nse.inr(lvl - b["c"]), nse.inr(lvl))
    return "%s bar  closed %s  %s" % (hm(b["t"]), nse.inr(b["c"]), note)


# ---------------------------------------------------------------- watch

class SimClock(object):
    """A clock that moves only when it sleeps: replays a day without waiting."""

    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += datetime.timedelta(seconds=max(1, int(round(seconds))))


def watch_loop(provider, clock, sleep, emit, opts, every=30, until=None, max_errors=5, on_trigger=None):
    """Poll bar by bar until a trigger (0), expiry, the close or --until (1).
    provider(now) -> chart, clock() -> naive IST, sleep(seconds), emit(line).
    It sleeps in steps of at most `every` seconds and re-reads the clock each time,
    so a computer that dozed off catches up as soon as it wakes."""
    step = INTERVALS[opts.get("interval", "5m")]
    cfg = opts.get("cfg") or {}
    printed, errors, said_preopen, first = -1, 0, False, True
    next_fetch = None
    while True:
        now = clock()
        if next_fetch is not None and now < next_fetch:
            sleep(max(1, min(every, (next_fetch - now).total_seconds())))
            continue
        day = now.date()
        open_dt, close_dt, why = day_status(day, cfg)
        if not open_dt:
            emit("%s: the NSE is closed today (%s). Nothing to watch." % (day.isoformat(), why))
            return 1, None
        if now < open_dt + datetime.timedelta(seconds=step):
            if not said_preopen:
                emit("%s IST  %s: watching from the first bar's close at %s." % (
                    now.strftime("%H:%M"), "not open yet" if now < open_dt else "open, no bar closed yet",
                    hm(open_dt + datetime.timedelta(seconds=step))))
                said_preopen = True
            next_fetch = open_dt + datetime.timedelta(seconds=step + GRACE)
            continue
        try:
            chart = provider(now)
            errors = 0
        except nse.NSEError as e:
            errors += 1
            emit("%s IST  could not read bars: %s" % (now.strftime("%H:%M:%S"), e))
            if errors >= max_errors:
                emit("Stopping after %d failed reads in a row." % errors)
                return 1, None
            next_fetch = now + datetime.timedelta(seconds=every)
            continue
        res = evaluate(chart, now, **opts)
        bars = res["_bars"]
        lines = describe(res)
        fresh = first
        if first:
            emit(lines[0])
            if len(lines) > 1 and lines[1].startswith("Opening range"):
                emit(lines[1])
            if bars and not res.get("trigger"):
                emit(bar_line(bars[-1], res))
            printed = bars[-1]["slot"] if bars else -1
            first = False
        else:
            for b in bars:
                if b["slot"] > printed:
                    emit(bar_line(b, res))
                    printed = b["slot"]
        if res["status"] == "triggered":
            for line in lines[1:]:
                if line.startswith("Opening range") or (line.startswith("TRIGGERED") and not fresh):
                    continue
                emit(line)
            if on_trigger:
                on_trigger(res, now)
            return 0, res
        if res["status"] == "expired" or now >= close_dt:
            emit("EXPIRED  %s." % (res["reason"] or "the market closed without a trigger"))
            return 1, res
        if until and now >= until:
            emit("Stopped at --until %s without a trigger. %s." % (hm(until), res["reason"]))
            return 1, res
        cur_slot = int((now - open_dt).total_seconds()) // step
        behind = (not bars) or bars[-1]["slot"] < cur_slot - 1
        if behind:
            wait = every
        else:
            nxt = open_dt + datetime.timedelta(seconds=(cur_slot + 1) * step + GRACE)
            wait = (nxt - now).total_seconds()
        cutoff = datetime.datetime.combine(day, parse_hhmm(res.get("no_entries_after") or "14:30", "cutoff"))
        for stop_at in (until, cutoff, close_dt):
            if stop_at and stop_at > now:
                wait = min(wait, (stop_at - now).total_seconds())
        next_fetch = now + datetime.timedelta(seconds=max(1, wait))


# ---------------------------------------------------------------- commands

def _opts(a, cfg):
    if a.side in ("sell", "short"):
        raise EntryError("this pack plans longs only: no short entries (and intraday shorts need allow_short).")
    if a.setup == "orb" and a.level is not None:
        raise EntryError("--level is for --setup level (an orb's level is its range high)")
    if a.min_volume_x is not None and a.min_volume_x <= 0:
        raise EntryError("--min-volume-x must be above 0")
    if a.level is not None and a.level <= 0:
        raise EntryError("--level must be a price above 0")
    return {"setup": a.setup, "or_minutes": a.or_minutes, "interval": a.interval, "level": a.level,
            "min_volume_x": a.min_volume_x, "cfg": cfg}


def _load(a, sym, now, cfg):
    """(chart, now). An offline file without --now is read as of its own time."""
    real_today = nse.now_ist().date()
    if a.offline and now is None:
        ch = get_chart(sym, a.interval, offline=a.offline, cfg=cfg)
        return ch, (ch["data_time"] if ch.get("data_time") else parse_now(None))
    now = now or parse_now(None)
    return get_chart(sym, a.interval, now.date(), a.offline, real_today, cfg), now


def _out_path(p, name):
    return p or os.path.join(nse.trader_dir(), name)


def cmd_check(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    sym = nse.clean_symbol(a.symbol)
    chart, now = _load(a, sym, parse_now(a.now) if a.now else None, cfg)
    res = evaluate(chart, now, **opts)
    path = None
    if a.write is not None:
        path = _out_path(a.write or None, "entry.json")
        nse.write_json(path, entry_doc(res, now))
    if a.json:
        print(json.dumps(public(res), indent=2, default=str))
    else:
        for line in describe(res):
            print(line)
        if path:
            print("Wrote %s (status %s)." % (path, res["status"]))
    return 0 if res["status"] == "triggered" else 1


def cmd_watch(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    sym = nse.clean_symbol(a.symbol)
    if a.every <= 0:
        raise EntryError("--every must be above 0 seconds")
    start = parse_now(a.now) if a.now else None
    if a.offline or start is not None:
        if start is None:
            start = _load(a, sym, None, cfg)[1]
        clock = SimClock(start)
        now_fn, sleep_fn = clock.now, clock.sleep
    else:
        now_fn, sleep_fn = (lambda: parse_now(None)), _sleep
    real_today = nse.now_ist().date()

    def provider(now):
        return get_chart(sym, a.interval, now.date(), a.offline, real_today, cfg)

    until = None
    if a.until:
        until = datetime.datetime.combine(now_fn().date(), parse_hhmm(a.until, "--until"))
    out = _out_path(a.out, "entry.json")

    def on_trigger(res, now):
        nse.write_json(out, entry_doc(res, now))
        print("Wrote %s." % out, flush=True)

    def emit(line):
        print(line, flush=True)

    code, _ = watch_loop(provider, now_fn, sleep_fn, emit, opts, every=a.every, until=until, on_trigger=on_trigger)
    return code


def watchlist_symbols(doc):
    """india-scanner's watchlist: {"hits": [{"symbol": ..}, ..]} (or "symbols": [..])."""
    rows = doc.get("hits") or doc.get("symbols") or doc.get("watchlist") or []
    out = []
    for h in rows:
        if isinstance(h, str):
            out.append({"symbol": h})
        elif isinstance(h, dict) and h.get("symbol"):
            out.append(h)
    return out


def cmd_watchlist(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    path = _out_path(a.watchlist, "watchlist.json")
    if not os.path.exists(path):
        raise EntryError("no watchlist at %s: run /india-scanner first (it writes the hits there)" % path)
    try:
        doc = nse.read_json(path)
    except nse.NSEError as e:
        raise EntryError(str(e))
    hits = watchlist_symbols(doc or {})
    if not hits:
        raise EntryError("no hits in %s: the scanner found nothing to watch" % path)
    if a.offline and not os.path.isdir(a.offline):
        raise EntryError("watchlist --offline wants a folder of <SYMBOL>.json files")
    now = parse_now(a.now) if a.now else None
    rows = []
    for i, h in enumerate(hits):
        o = dict(opts)
        try:
            sym = nse.clean_symbol(h["symbol"])
            if a.setup == "level" and o["level"] is None:
                o["level"] = h.get("level")
            if a.setup == "level" and o["level"] is None:
                raise EntryError("needs a level: --level PRICE or a \"level\" in the watchlist")
            if i and not a.offline:
                _sleep(nse.PAUSE)
            chart, when = _load(a, sym, now, cfg)
            rows.append((sym, evaluate(chart, when, **o), None))
        except nse.NSEError as e:
            rows.append((str(h["symbol"]).upper(), None, str(e)))
    if a.json:
        print(json.dumps([{"symbol": s, "result": public(r) if r else None, "error": e} for s, r, e in rows],
                         indent=2, default=str))
    else:
        print("%-10s  %-9s  %-5s  %-12s  %-12s  %-12s  %s" % ("SYMBOL", "STATUS", "TIME", "PRICE", "LEVEL", "LAST",
                                                            "VS LEVEL"))
        for s, r, e in rows:
            if e:
                print("%-10s  error: %s" % (s, e))
                continue
            t = r.get("trigger") or {}
            last = r.get("last") or {}
            status = SHORT.get(r["phase"], r["status"]) if r["status"] != "triggered" else "triggered"
            print("%-10s  %-9s  %-5s  %-12s  %-12s  %-12s  %s" % (
                s, status[:9], t.get("time") or "-", nse.inr(t["price"]) if t else "-",
                nse.inr(r["level"]) if r.get("level") is not None else "-",
                nse.inr(last["close"]) if last else "-",
                ("%+.2f%%" % last["vs_level_pct"]) if last.get("vs_level_pct") is not None else "-"))
        n = sum(1 for _, r, _ in rows if r and r["status"] == "triggered")
        print("%d of %d triggered. Pick one, then: entry.py check <SYMBOL> --write, and /india-exit." % (n, len(rows)))
        print("Bars from Yahoo (free, unofficial, can lag) or Dhan. Nothing was placed.")
    return 0 if any(r and r["status"] == "triggered" for _, r, _ in rows) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Entry Timer (india-entry): watches an NSE setup bar by bar.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--setup", choices=["orb", "level"], default="orb")
        p.add_argument("--or-minutes", type=int, default=15, help="opening range length (default 15)")
        p.add_argument("--interval", choices=["5m", "1m"], default="5m")
        p.add_argument("--level", type=float, help="the price to close above (--setup level)")
        p.add_argument("--min-volume-x", type=float, help="trigger bar volume vs the range bars' average")
        p.add_argument("--side", default="long", choices=["buy", "long", "sell", "short"], help="longs only")
        p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-10-29 09:40"')
        p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON (.json/.json.gz) or a folder")
        p.add_argument("--config", help="default trader/config.json (TRADER_DIR)")

    p = sub.add_parser("check", help="the setup's status right now")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--write", nargs="?", const="", metavar="PATH", help="save the result (default trader/entry.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("watch", help="poll bar by bar until it triggers or expires")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--every", type=float, default=30, help="seconds between polls while the feed is behind")
    p.add_argument("--until", help="stop at this IST time (HH:MM)")
    p.add_argument("--out", help="where a trigger is written (default trader/entry.json)")
    p = sub.add_parser("watchlist", help="check every hit in trader/watchlist.json")
    common(p)
    p.add_argument("--watchlist", help="default trader/watchlist.json")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"check": cmd_check, "watch": cmd_watch, "watchlist": cmd_watchlist}[a.cmd](a)
    except nse.NSEError as e:
        print("entry.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("entry.py: stopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
