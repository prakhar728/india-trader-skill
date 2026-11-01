#!/usr/bin/env python3
"""Time Machine for india-timemachine. Standard library only.

Replays a planned NSE day trade before you risk a rupee: 10,000 simulated runs
of the rest of the session from your entry to your flat-by time, plus the same
rule on every past session in the data. Every R is shown gross and after
Dhan's intraday charges.

    python3 timemachine.py run                          # trader/plan.json, 10,000 replays, seed 19
    python3 timemachine.py run --write                  # also save trader/timemachine.json
    python3 timemachine.py run --until 15:30            # replay to the close instead of flat-by
    python3 timemachine.py run --seed 7 --replays 20000 --json
    python3 timemachine.py run --symbol RELIANCE --date 2026-10-29 --entry 1174.30 \\
        --stop 1163.30 --target 1196.30 --entry-time 09:40
    python3 timemachine.py chart --from                 # ASCII fan chart of the saved run

How the replays work (the US pack's sim, adapted to NSE sessions):
  1. Fetch 60 days of 5-minute bars (Yahoo SYMBOL.NS range=60d interval=5m, one
     request; or Dhan /v2/charts/intraday when data_source picks Dhan). Keep the
     last 60 sessions before the plan's date that have at least 60 of the 75
     five-minute bars from 09:15 to 15:25 IST. Yahoo drops a bar here and
     there (72 to 75 a session), and F&O stocks have no 15:20 bar since NSE's
     closing auction (Aug 3 2026): gaps are expected and never fatal.
  2. The pool: for every bar slot 1..74 of the day, each past session's bar as
     ratios to the close of the bar before it (the last one that exists, so a
     dropped bar's move is folded into the next): (open, high, low, close) / prev close.
  3. The replay walks the slots after the entry bar up to --until (default the
     plan's flat_by, 15:05). A slot that fewer than half the pool sessions have
     (15:20 for an F&O stock) is skipped and listed. For each slot i it picks
     j = i + randint(-window, window), kept in 1..74 (j = i when slot j is
     empty), then one past bar from pool[j] (random.choice), and scales it to
     the current price. The stop is checked before the target in every bar,
     so a bar that touches both counts as the stop (the conservative call).
  4. Outcomes: "target" or "stop" (whichever was touched first), or "neither"
     when the trade is still open at --until. "green" = target first, or above
     the entry at --until. R = the target's R at the target, -1 at the stop,
     otherwise (price at --until - entry) / risk. Net R takes off the round
     trip's charges (nse.charges) for the reference quantity:
       ((exit - entry) x qty - charges) / (risk x qty)
     A gap through the stop still counts as a fill at the stop here; a real
     fill can be worse.
  5. The price at --until: median and the 5th to 95th percentile. The fan chart
     uses a second run of 2,000 paths with the same seed. The histogram has 24
     bins between the 1st and 99th percentile.
  6. History: the plan's own rule (an opening-range breakout with the same
     opening range, stop rule and target R, inside the config's entry window,
     09:30 to 14:30 by default) re-run on every one of those past sessions.

The verdict looks at the replays that hit one side and compares the target's
share, target / (target + stop), with the share that breaks even AFTER
charges: |net R at the stop| / (net R at the target + |net R at the stop|).
Less than 5 points away: "Close to a coin flip" (1R) / "Close to break-even";
5 to 15: "Leans toward ..."; 15 or more: "Strongly toward ...".

Same plan, same data and same --seed give the same numbers on any machine.
A simulation built from the stock's own recent bars is not a prediction:
results, news, circuit limits and F&O expiry days can do what the last 60
sessions never did.
"""

import argparse
import datetime
import json
import math
import os
import random
import re
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "india-common"))
import nse  # noqa: E402

INTERVAL = 5          # minutes
OPEN_MIN = 9 * 60 + 15
CLOSE_MIN = 15 * 60 + 30
SLOTS = (CLOSE_MIN - OPEN_MIN) // INTERVAL     # 75: 09:15 ... 15:25
MAX_SLOT = SLOTS - 1
POOL_SESSIONS = 60
MIN_SESSIONS = 20
MIN_SESSION_BARS = 60
LIVE_SHARE = 0.5
SAMPLE_PATHS = 160
BAND_PATHS = 2000
BAND_Q = (0.05, 0.25, 0.5, 0.75, 0.95)
HIST_BINS = 24
COIN_FLIP = 0.05
STRONG = 0.15
MANY_NEITHER = 0.40
DEFAULT_MAX_LOSS = 500.0
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class TMError(nse.NSEError):
    pass


# ---------------------------------------------------------------- time

def parse_now(text):
    if not text:
        return nse.now_ist().replace(microsecond=0)
    try:
        return nse.parse_now(text)
    except nse.NSEError:
        raise TMError('--now wants an IST time like "2026-10-29 09:45"')


def parse_clock(text, what):
    """'09:40', '9:40', '15:05', '3:05 PM' -> minutes after midnight (IST)."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*(?:([AaPp])\.?\s*[Mm]\.?)?\s*$", str(text or ""))
    if not m:
        raise TMError("%s wants an IST time like 09:40 or 15:05, not %r" % (what, text))
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if mi > 59 or h > 23 or (ap and not 1 <= h <= 12):
        raise TMError("%s %r is not a clock time" % (what, text))
    if ap == "p" and h != 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    return h * 60 + mi


def clock(minutes):
    return "%02d:%02d" % divmod(minutes, 60)


def slot_close(i):
    return clock(OPEN_MIN + INTERVAL * (i + 1))


# ---------------------------------------------------------------- data

def _http(url):
    """Swapped out in tests."""
    return nse.http_get(url)


def fetch_yahoo(sym, exchange="NSE"):
    last = None
    for n, host in enumerate(nse.YAHOO_HOSTS):
        if n:
            time.sleep(nse.PAUSE)
        try:
            return _http(nse.yahoo_chart_url(sym, "60d", "5m", host, exchange))
        except nse.NSEError as e:
            last = e
    raise TMError("Yahoo did not answer for %s (%s). Wait a minute and run it again." % (sym, last))


def fetch_dhan(sym, day, dhan=None, scrip=None):
    """Dhan intraday 5-minute bars, 90 days back (one request; needs the Data plan)."""
    dhan = dhan or nse.Dhan.from_env()
    scrip = scrip if scrip is not None else nse.scrip_master()
    sid = nse.security_id(sym, scrip)
    start = datetime.datetime.combine(day - datetime.timedelta(days=89), nse.OPEN)
    end = datetime.datetime.combine(day, nse.CLOSE)
    return {}, dhan.intraday(sid, start, end, interval=5)


def read_offline(path, sym):
    """A saved Yahoo chart file (.json or .json.gz), or a folder with one for the symbol."""
    if os.path.isdir(path):
        for name in ("%s-60d5m.json" % sym, "%s-60d5m.json.gz" % sym, "yahoo-%s.NS-60d5m.json.gz" % sym,
                     "yahoo-%s.NS-60d5m.json" % sym, "%s.json" % sym, "%s.json.gz" % sym):
            if os.path.exists(os.path.join(path, name)):
                path = os.path.join(path, name)
                break
        else:
            raise TMError("no %s-60d5m.json(.gz) in %s" % (sym, path))
    if not os.path.exists(path):
        raise TMError("no offline chart file %s" % path)
    try:
        return nse.read_maybe_gz(path)
    except OSError as e:
        raise TMError("could not read %s: %s" % (path, e))


def chart_from_text(text, sym):
    """Yahoo chart JSON -> (meta, bars, raw timestamp count)."""
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        raise TMError("Yahoo returned something that is not JSON for %s" % sym)
    if not isinstance(doc, dict) or not isinstance(doc.get("chart"), dict):
        raise TMError("Yahoo returned something unexpected for %s" % sym)
    try:
        meta, bars = nse.parse_yahoo_chart(doc)
    except nse.NSEError as e:
        raise TMError("Yahoo has no data for %s: %s" % (sym, str(e).replace("Yahoo: ", "")))
    res = (doc["chart"].get("result") or [{}])[0] or {}
    return meta, bars, len(res.get("timestamp") or [])


def parse_bars(bars, now, data_time=None, raw_count=None):
    """Bars -> (session bars, dropped). Keeps 5-minute bars on the 09:15 grid, inside
    09:15-15:30 IST, complete by the earlier of now and the feed's own last trade."""
    as_of = now
    if data_time and data_time < as_of:
        as_of = data_time
    dropped = {"no_price": max(0, (raw_count or len(bars)) - len(bars)), "off_grid": 0, "not_complete": 0,
               "outside_hours": 0}
    out = {}
    for b in bars:
        t = b["t"]
        mins = t.hour * 60 + t.minute - OPEN_MIN
        if t.second or mins % INTERVAL:
            dropped["off_grid"] += 1
            continue
        if mins < 0 or mins >= SLOTS * INTERVAL:
            dropped["outside_hours"] += 1
            continue
        if t + datetime.timedelta(minutes=INTERVAL) > as_of:
            dropped["not_complete"] += 1
            continue
        out[t] = {"date": t.date().isoformat(), "slot": mins // INTERVAL, "start": t.strftime("%H:%M"),
                  "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b.get("v", 0)}
    return [out[t] for t in sorted(out)], dropped


def split_sessions(bars):
    """{date: {slot: bar}}."""
    days = {}
    for b in bars:
        days.setdefault(b["date"], {})[b["slot"]] = b
    return days


def pool_sessions(days, before, keep=POOL_SESSIONS):
    """The last `keep` sessions strictly before the plan's date with at least
    MIN_SESSION_BARS bars, oldest first, and the short ones left out."""
    good, short = [], []
    for d in sorted(days):
        if d >= before:
            continue
        if len(days[d]) >= MIN_SESSION_BARS:
            good.append((d, days[d]))
        else:
            short.append({"date": d, "bars": len(days[d])})
    return good[-keep:], short


# ---------------------------------------------------------------- the simulation

def build_pool(sessions):
    """pool[i] = every past session's bar i as (o, h, l, c) / the close of the bar before it."""
    pool = [[] for _ in range(SLOTS)]
    for _, D in sessions:
        prev = None
        for i in range(SLOTS):
            b = D.get(i)
            if b is None:
                continue
            if prev is not None:
                pc = prev["c"]
                pool[i].append((b["o"] / pc, b["h"] / pc, b["l"] / pc, b["c"] / pc))
            prev = b
    return pool


def live_slots(pool, k, last, n_sessions):
    """(slots to replay after the entry bar k up to last, slots skipped for want of bars)."""
    steps, skipped = [], []
    for i in range(k + 1, last + 1):
        if len(pool[i]) >= LIVE_SHARE * n_sessions and pool[i]:
            steps.append(i)
        else:
            skipped.append({"slot": i, "start": clock(OPEN_MIN + INTERVAL * i), "sessions": len(pool[i])})
    return steps, skipped


def pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


def net_r(entry, exit_price, risk, qty, exchange="NSE"):
    c = nse.charges(qty * entry, qty * exit_price, exchange)["total"]
    return ((exit_price - entry) * qty - c) / (risk * qty)


def simulate(pool, entry, stop, target, steps, replays=10000, seed=19, window=2, qty=1, exchange="NSE"):
    """Monte Carlo replays of the slots in `steps`. Seeded: same inputs, same numbers."""
    risk = entry - stop
    r_target = round((target - entry) / risk, 4)

    def slot(rng, i):
        if window == 0:
            return i
        j = min(MAX_SLOT, max(1, i + rng.randint(-window, window)))
        return j if pool[j] else i

    rng = random.Random(seed)
    choice = rng.choice
    outcomes, closes, paths = [], [], []
    for n in range(replays):
        p = entry
        outcome = None
        keep = n < SAMPLE_PATHS
        path = [entry]
        for i in steps:
            o, h, l, c = choice(pool[slot(rng, i)])
            hi, lo, cl = p * h, p * l, p * c
            if outcome is None:
                if lo <= stop:
                    outcome = "stop"        # both inside one bar: count the loss (conservative)
                elif hi >= target:
                    outcome = "target"
            p = cl
            if keep:
                path.append(round(p, 2))
        outcomes.append(outcome or "close")
        closes.append(p)
        if keep:
            paths.append(path)
    cnt = {x: outcomes.count(x) for x in ("target", "stop", "close")}
    green = sum(1 for o, p in zip(outcomes, closes) if o == "target" or (o == "close" and p > entry))
    Rs = [r_target if o == "target" else -1.0 if o == "stop" else (p - entry) / risk for o, p in zip(outcomes, closes)]
    r_net_t = net_r(entry, target, risk, qty, exchange)
    r_net_s = net_r(entry, stop, risk, qty, exchange)
    Rn = [r_net_t if o == "target" else r_net_s if o == "stop" else net_r(entry, p, risk, qty, exchange)
          for o, p in zip(outcomes, closes)]
    srt = sorted(closes)
    rng = random.Random(seed)
    choice = rng.choice
    allp = []
    for n in range(BAND_PATHS):
        p = entry
        path = [entry]
        for i in steps:
            o, h, l, c = choice(pool[slot(rng, i)])
            p = p * c
            path.append(p)
        allp.append(path)
    bands = []
    for j in range(len(steps) + 1):
        col = sorted(pp[j] for pp in allp)
        bands.append([round(pct(col, q), 2) for q in BAND_Q])
    lo_, hi_ = pct(srt, 0.01), pct(srt, 0.99)
    W = (hi_ - lo_) / HIST_BINS
    bins = [0] * HIST_BINS
    for c in srt:
        b = int((c - lo_) / W) if W > 0 else 0
        if 0 <= b < HIST_BINS:
            bins[b] += 1
    return {
        "replays": replays, "seed": seed, "window": window, "bars_left": len(steps),
        "p_target": round(cnt["target"] / replays, 4), "p_stop": round(cnt["stop"] / replays, 4),
        "p_neither": round(cnt["close"] / replays, 4), "p_green": round(green / replays, 4),
        "median_close": round(pct(srt, 0.5), 2), "p5_close": round(pct(srt, 0.05), 2),
        "p95_close": round(pct(srt, 0.95), 2), "avg_R": round(statistics.mean(Rs), 3),
        "avg_R_net": round(statistics.mean(Rn), 3), "qty": qty,
        "r_at_target": r_target, "r_net_at_target": round(r_net_t, 3), "r_net_at_stop": round(r_net_s, 3),
        "band_paths": BAND_PATHS, "bands": bands, "sample_paths": paths,
        "hist": {"lo": round(lo_, 2), "hi": round(hi_, 2), "bins": bins},
    }


# ---------------------------------------------------------------- stop rules and the history replay

def opening_range(D, n):
    """(high, low) of the first n bars, or None when one of them is missing."""
    if not all(i in D for i in range(n)):
        return None
    rows = [D[i] for i in range(n)]
    # to the paisa first: Yahoo's 1156.199951 is NSE's 1156.20, and must not tick-round down to 1156.10
    return round(max(b["h"] for b in rows), 2), round(min(b["l"] for b in rows), 2)


def rule_stop(rule, oh, ol, entry, tick):
    r = rule["stop_rule"]
    if r == "or_mid":
        return nse.round_down((oh + ol) / 2.0, tick)
    if r == "or_low":
        return nse.round_down(ol, tick)
    return nse.round_down(entry * (1 - rule["stop_pct"] / 100.0), tick)


def ref_qty(max_loss, risk):
    return max(1, int(math.floor(max_loss / risk + 1e-9)))


def history_replay(sessions, rule, last, window, tick=None, max_loss=DEFAULT_MAX_LOSS, exchange="NSE"):
    """The plan's rule on every past session: entry = the first bar after the opening
    range to close above its high, with its close inside the entry window; then stop
    first, then target, bar by bar (missing bars skipped), else the last close by --until."""
    n = rule["or_minutes"] // INTERVAL
    before, cutoff = window
    trades, no_range = [], 0
    for d, D in sessions:
        orr = opening_range(D, n)
        if orr is None:
            no_range += 1
            continue
        oh, ol = orr
        kk = None
        for i in range(n, last + 1):
            b = D.get(i)
            end = OPEN_MIN + INTERVAL * (i + 1)
            if b is None or end < before or end > cutoff:
                continue
            if round(b["c"], 2) > oh:
                kk = i
                break
        if kk is None:
            continue
        e = round(D[kk]["c"], 2)
        tk = tick or nse.band_tick(e)
        s = rule_stop(rule, oh, ol, e, tk)
        risk = round(e - s, 2)
        if risk <= 0:
            continue
        t = nse.round_down(e + rule["target_r"] * risk, tk)
        oc, exit_px = None, None
        for i in range(kk + 1, last + 1):
            b = D.get(i)
            if b is None:
                continue
            if round(b["l"], 2) <= s:
                oc, exit_px = "stop", s
                break
            if round(b["h"], 2) >= t:
                oc, exit_px = "target", t
                break
        if oc is None:
            closes = [D[i]["c"] for i in range(kk, last + 1) if i in D]
            exit_px = closes[-1]
        R = (exit_px - e) / risk
        if oc == "stop":
            R = -1.0
        q = ref_qty(max_loss, risk)
        trades.append({"date": d, "time": slot_close(kk), "entry": e, "stop": s, "target": t,
                       "outcome": oc or "close", "R": round(R, 2), "R_net": round(net_r(e, exit_px, risk, q, exchange), 2)})
    return {
        "sessions": len(sessions), "triggered": len(trades), "no_opening_range": no_range,
        "target": sum(1 for x in trades if x["outcome"] == "target"),
        "stop": sum(1 for x in trades if x["outcome"] == "stop"),
        "close": sum(1 for x in trades if x["outcome"] == "close"),
        "green": sum(1 for x in trades if x["R"] > 0),
        "avg_R": round(statistics.mean(x["R"] for x in trades), 3) if trades else None,
        "avg_R_net": round(statistics.mean(x["R_net"] for x in trades), 3) if trades else None,
        "rule": rule, "trades": trades,
    }


# ---------------------------------------------------------------- the plan

def _num(v, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise TMError("the plan needs a number for %s, got %r" % (what, v))
    if not x > 0 or math.isinf(x):
        raise TMError("the plan's %s must be a positive price, got %r" % (what, v))
    return x


def load_plan(a, now):
    if a.symbol:
        missing = [f for f in ("entry", "stop", "target", "entry_time") if getattr(a, f) is None]
        if missing:
            raise TMError("with --symbol also give %s" % ", ".join("--" + m.replace("_", "-") for m in missing))
        raw = {"symbol": a.symbol, "side": a.side, "date": a.date or now.date().isoformat(),
               "entry": a.entry, "stop": a.stop, "target": a.target, "entry_time": a.entry_time,
               "setup": a.setup, "opening_range": {"minutes": a.or_minutes}, "stop_rule": a.stop_rule,
               "interval": "5m", "tick": a.tick}
        src = "flags"
    else:
        path = a.plan or os.path.join(nse.trader_dir(), "plan.json")
        if not os.path.exists(path):
            raise TMError("no plan at %s: run /india-exit first, or pass --symbol --date --entry --stop "
                          "--target --entry-time" % path)
        try:
            raw = nse.read_json(path)
        except nse.NSEError as e:
            raise TMError(str(e))
        if not isinstance(raw, dict):
            raise TMError("%s is not a plan" % path)
        src = path
        if not raw.get("entry_time") and raw.get("entry_bar") is None:
            raw = with_entry_time(raw, os.path.join(os.path.dirname(os.path.abspath(path)), "entry.json"))
    return normalize_plan(raw, src)


def with_entry_time(raw, entry_path):
    """The contract's plan.json may carry no entry time: take it from india-entry's
    entry.json beside it when that is the same symbol, day and price."""
    try:
        e = nse.read_json(entry_path) or {}
    except nse.NSEError:
        return raw
    same = (str(e.get("symbol") or "").upper() == str(raw.get("symbol") or "").upper()
            and e.get("date") == raw.get("date") and e.get("status") == "triggered" and e.get("time"))
    if same and e.get("price") is not None and abs(float(e["price"]) - float(raw.get("entry") or 0)) < 0.005:
        return dict(raw, entry_time=e["time"], entry_bar=e.get("bar_index"))
    return raw


def normalize_plan(raw, src="flags"):
    """A plan dict (trader/plan.json shape) -> the checked plan the time machine runs."""
    try:
        sym = nse.clean_symbol(raw.get("symbol") or "")
    except nse.NSEError:
        raise TMError("the plan needs an NSE symbol, got %r" % raw.get("symbol"))
    side = str(raw.get("side") or "long").lower()
    if side not in ("long", "buy"):
        raise TMError("this pack plans longs only (the plan says side %r)" % raw.get("side"))
    interval = str(raw.get("interval") or "5m")
    if interval != "5m":
        raise TMError("5-minute bars only (the plan says %s): Yahoo keeps 60 days of 5-min bars but only about "
                      "7 days of 1-min bars, too few sessions for a fair replay pool" % interval)
    try:
        date = datetime.date.fromisoformat(str(raw.get("date") or "")).isoformat()
    except ValueError:
        raise TMError("the plan needs a date like 2026-10-29, got %r" % raw.get("date"))
    entry, stop, target = (_num(raw.get(k), k) for k in ("entry", "stop", "target"))
    if not stop < entry < target:
        raise TMError("a long needs stop < entry < target (got stop %s, entry %s, target %s)" % (stop, entry, target))
    k = None
    if raw.get("entry_time"):
        close_min = parse_clock(raw["entry_time"], "entry_time")
        if (close_min - OPEN_MIN) % INTERVAL:
            raise TMError("entry_time %s is not on the 5-minute grid from 09:15 (use the bar's close, e.g. 09:40)"
                          % raw["entry_time"])
        k = (close_min - OPEN_MIN) // INTERVAL - 1
    if raw.get("entry_bar") is not None:
        try:
            kb = int(raw["entry_bar"])
        except (TypeError, ValueError):
            raise TMError("entry_bar must be a bar number, got %r" % raw["entry_bar"])
        if k is not None and kb != k:
            raise TMError("the plan disagrees with itself: entry_bar %d but entry_time %s is bar %d"
                          % (kb, raw["entry_time"], k))
        k = kb
    if k is None:
        raise TMError("the plan needs entry_time (the entry bar's close, e.g. 09:40) or entry_bar")
    if not 0 <= k <= SLOTS - 2:
        raise TMError("the entry must close between 09:20 and 15:25 IST to leave bars to replay (got %s)"
                      % slot_close(k))
    orng = raw.get("opening_range") or {}
    or_min = orng.get("minutes", 15) if isinstance(orng, dict) else 15
    try:
        or_min = int(or_min)
    except (TypeError, ValueError):
        raise TMError("opening_range.minutes must be a number")
    if or_min < INTERVAL or or_min % INTERVAL or or_min >= SLOTS * INTERVAL:
        raise TMError("opening_range.minutes must be a multiple of 5 (got %s)" % or_min)
    tick = raw.get("tick")
    try:
        tick = float(tick) if tick else None
    except (TypeError, ValueError):
        tick = None
    risk = entry - stop
    ch = raw.get("charges") if isinstance(raw.get("charges"), dict) else {}
    return {
        "symbol": sym, "exchange": str(raw.get("exchange") or "NSE").upper(), "side": "long", "date": date,
        "setup": str(raw.get("setup") or "orb"), "entry": entry, "entry_time": slot_close(k), "entry_bar": k,
        "stop": stop, "target": target, "risk_per_share": round(risk, 2),
        "target_r": round((target - entry) / risk, 2), "tick": tick or nse.band_tick(entry),
        "opening_range": dict(orng, minutes=or_min) if isinstance(orng, dict) else {"minutes": or_min},
        "stop_rule": str(raw.get("stop_rule") or "auto").replace("-", "_"), "interval": "5m",
        "flat_by": raw.get("flat_by"), "qty": ch.get("qty"), "from": src,
    }


def resolve_rule(plan, entry_day):
    """The history rule: orb, the plan's opening range, its stop rule (or_mid, or_low,
    or 'pct' = the plan's stop distance as a percent of the entry) and its target R.
    'auto' works the stop rule out from the plan day's own opening range."""
    if plan["setup"].lower() != "orb":
        return None
    n = plan["opening_range"]["minutes"] // INTERVAL
    rule = {"setup": "orb", "or_minutes": plan["opening_range"]["minutes"], "stop_rule": plan["stop_rule"],
            "target_r": plan["target_r"], "stop_pct": round(100.0 * plan["risk_per_share"] / plan["entry"], 4)}
    if rule["stop_rule"] == "auto":
        rule["stop_rule"] = "pct"
        orr = opening_range(entry_day, n)
        if orr:
            oh, ol = orr
            if abs(nse.round_down((oh + ol) / 2.0, plan["tick"]) - plan["stop"]) < 0.005:
                rule["stop_rule"] = "or_mid"
            elif abs(nse.round_down(ol, plan["tick"]) - plan["stop"]) < 0.005:
                rule["stop_rule"] = "or_low"
    elif rule["stop_rule"] not in ("or_mid", "or_low"):
        rule["stop_rule"] = "pct"
    if rule["stop_rule"] != "pct":
        del rule["stop_pct"]
    return rule


# ---------------------------------------------------------------- the run

def lean(mc):
    """(gap, share, break_even): the target's share of the replays that hit one side minus
    the share that breaks even after charges. None when nothing hit a side."""
    pt, ps = mc["p_target"], mc["p_stop"]
    if pt + ps <= 0:
        return None
    rt = mc.get("r_net_at_target", mc.get("r_at_target") or 1.0)
    rs = abs(mc.get("r_net_at_stop", -1.0))
    share = pt / (pt + ps)
    even = rs / (rt + rs) if rt > 0 else 1.0
    return share - even, share, even


def verdict(mc):
    pt, ps = mc["p_target"], mc["p_stop"]
    until = mc.get("until", "15:05")
    lv = lean(mc)
    if lv is None:
        return "No replay reached the target or the stop by %s: the price at %s decides every one." % (until, until)
    d, share, even = lv
    r = mc.get("r_at_target") or 1.0
    one_r = abs(r - 1.0) < 0.005
    if abs(d) < COIN_FLIP:
        head = "Close to a coin flip" if one_r else "Close to break-even"
    elif abs(d) < STRONG:
        head = "Leans toward the target" if d > 0 else "Leans toward the stop"
    else:
        head = "Strongly toward the target" if d > 0 else "Strongly toward the stop"
    return ("%s: at %gR (%+.2fR after charges, %+.2fR at the stop) the target has to come first in %.0f %% of the "
            "replays that hit one side; it did in %.0f %% (target first %.1f %% vs stop first %.1f %%)."
            % (head, round(r, 2), mc.get("r_net_at_target", r), mc.get("r_net_at_stop", -1.0), 100 * even,
               100 * share, 100 * pt, 100 * ps))


def verdict_notes(mc):
    notes = []
    until = mc.get("until", "15:05")
    lv = lean(mc)
    if mc.get("p_neither", 0) > MANY_NEITHER and lv is not None:
        notes.append("%.1f %% of the replays hit neither by %s; the price at %s decides those."
                     % (100 * mc["p_neither"], until, until))
    avg = mc.get("avg_R_net")
    if lv and abs(lv[0]) >= COIN_FLIP and avg is not None and avg != 0 and (avg > 0) != (lv[0] > 0):
        notes.append("Average R after charges is %+.3f all the same: the replays that hit neither pull it the "
                     "other way." % avg)
    return notes


def time_machine(meta, bars, plan, now, replays=10000, seed=19, window=2, until=None, source="yahoo",
                 cfg=None, raw_count=None, qty=None):
    """Bars + a normalized plan -> the timemachine.json document."""
    cfg = cfg or {}
    if not 1 <= replays <= 1000000:
        raise TMError("--replays must be between 1 and 1,000,000")
    if not 0 <= window <= 10:
        raise TMError("--window must be between 0 and 10 bars")
    sym = plan["symbol"]
    msym = meta.get("symbol")
    if msym and nse.clean_symbol(msym) != sym:
        raise TMError("the data is for %s but the plan is for %s" % (msym, sym))
    gran = meta.get("dataGranularity")
    if gran and gran != "5m":
        raise TMError("the data has %s bars; the time machine needs 5-minute bars" % gran)
    pday = datetime.date.fromisoformat(plan["date"])
    if nse.special_session(pday, cfg):
        raise TMError("%s is a special session (Muhurat trading): it is too short and unlike any regular day "
                      "to replay from regular sessions" % plan["date"])
    rmt = meta.get("regularMarketTime")
    sbars, dropped = parse_bars(bars, now, nse.ist_from_epoch(rmt) if rmt else None, raw_count)
    if not sbars:
        raise TMError("no complete 5-minute bars for %s in the data" % sym)
    k = plan["entry_bar"]
    fb = plan.get("flat_by") or nse.flat_by(cfg).strftime("%H:%M")
    until_min = parse_clock(until or fb, "--until" if until else "flat_by")
    if (until_min - OPEN_MIN) % INTERVAL or until_min > CLOSE_MIN or until_min <= OPEN_MIN:
        raise TMError("--until must be on the 5-minute grid from 09:15 and no later than 15:30 (got %s)" % clock(until_min))
    last = (until_min - OPEN_MIN) // INTERVAL - 1
    if last <= k:
        raise TMError("--until %s leaves nothing to replay after the %s entry" % (clock(until_min), plan["entry_time"]))
    days = split_sessions(sbars)
    entry_day = days.get(plan["date"], {})
    if k not in entry_day:
        reason = ("no bars for that day" if not entry_day else
                  "the data has no complete %s bar; its last is the %s bar" % (
                      clock(OPEN_MIN + INTERVAL * k), entry_day[max(entry_day)]["start"]))
        raise TMError("the entry is not in the data: no complete %s bar (closing %s) on %s for %s (%s)"
                      % (clock(OPEN_MIN + INTERVAL * k), plan["entry_time"], plan["date"], sym, reason))
    eb = entry_day[k]
    warn = []
    if not eb["l"] - plan["tick"] <= plan["entry"] <= eb["h"] + plan["tick"]:
        warn.append("the plan's entry %.2f is outside that bar's range %.2f to %.2f" % (plan["entry"], eb["l"], eb["h"]))
    sessions, short = pool_sessions(days, plan["date"])
    if len(sessions) < MIN_SESSIONS:
        raise TMError("only %d full session(s) of %s before %s in the data; the time machine needs at least %d"
                      % (len(sessions), sym, plan["date"], MIN_SESSIONS))
    pool = build_pool(sessions)
    steps, skipped = live_slots(pool, k, last, len(sessions))
    if not steps:
        raise TMError("no bars to replay between %s and %s" % (plan["entry_time"], clock(until_min)))
    try:
        max_loss = float(cfg.get("max_loss_per_trade") or DEFAULT_MAX_LOSS)
    except (TypeError, ValueError):
        max_loss = DEFAULT_MAX_LOSS
    q = int(qty or plan.get("qty") or ref_qty(max_loss, plan["risk_per_share"]))
    mc = simulate(pool, plan["entry"], plan["stop"], plan["target"], steps, replays, seed, window, q, plan["exchange"])
    mc = dict(mc, until=clock(until_min), pool_sessions=len(sessions), pool_from=sessions[0][0],
              pool_to=sessions[-1][0], band_times=[plan["entry_time"]] + [slot_close(i) for i in steps],
              skipped_slots=skipped, pool_bars={"min": min(len(pool[i]) for i in steps),
                                               "max": max(len(pool[i]) for i in steps)})
    rule = resolve_rule(plan, entry_day)
    plan_out = {kk: v for kk, v in plan.items() if v is not None}
    plan_out["qty"] = q
    if rule:
        plan_out["stop_rule"] = rule["stop_rule"]
        before = parse_clock(cfg.get("no_entries_before") or "09:30", "no_entries_before")
        cutoff = parse_clock(cfg.get("no_entries_after") or "14:30", "no_entries_after")
        history = history_replay(sessions, rule, last, (before, cutoff), plan.get("tick"), max_loss, plan["exchange"])
        history["entry_window"] = [clock(before), clock(cutoff)]
    else:
        history = {"sessions": len(sessions), "triggered": 0, "rule": None, "trades": [],
                   "note": "skipped: the history replay knows the opening-range breakout (orb) only, "
                           "this plan's setup is %r" % plan["setup"]}
    last_bar = sbars[-1]
    return {
        "created_by": "india-timemachine",
        "made": nse.iso_ist(now),
        "source": source,
        "data": {"symbol": sym, "exchange": plan["exchange"], "interval": "5m", "range": "60d",
                 "last_bar": "%s %s IST" % (last_bar["date"], last_bar["start"]),
                 "dropped": dropped, "short_sessions_left_out": short},
        "plan": plan_out,
        "mc": mc,
        "history": history,
        "verdict": verdict(mc),
        "verdict_notes": verdict_notes(mc),
        "warnings": warn,
        "note": ("A simulation built from %s's own last %d sessions. It is not a prediction: results, news, "
                 "circuit limits and F&O expiry days can do things those sessions never did. Same seed, same numbers."
                 % (sym, len(sessions))),
    }


# ---------------------------------------------------------------- output

def pc(x):
    return "%.1f %%" % (100.0 * x)


def report(doc):
    p, m, h = doc["plan"], doc["mc"], doc["history"]
    until = m["until"]
    wd = WEEKDAYS[datetime.date.fromisoformat(p["date"]).weekday()]
    lines = [
        "%s long, %s %s: entry %.2f at %s IST (bar %d), stop %.2f, target %.2f (%gR, risk %.2f a share)."
        % (p["symbol"], wd, p["date"], p["entry"], p["entry_time"], p["entry_bar"], p["stop"], p["target"],
           p["target_r"], p["risk_per_share"]),
        "%s replays of the rest of the session, %s to %s IST (%d five-minute bars), seed %d."
        % ("{:,}".format(m["replays"]), p["entry_time"], until, m["bars_left"], m["seed"]),
        "Each bar is drawn whole from %s's own 5-min bars at the same time of day (+-%d bars) over its last "
        "%d sessions (%s to %s)." % (p["symbol"], m["window"], m["pool_sessions"], m["pool_from"], m["pool_to"]),
    ]
    if m.get("skipped_slots"):
        lines.append("Skipped %s: fewer than half the sessions have that bar (NSE's closing auction or Yahoo gaps)."
                     % ", ".join("%s (%d of %d)" % (s["start"], s["sessions"], m["pool_sessions"])
                                 for s in m["skipped_slots"]))
    lines += [
        "",
        "  target first   %s" % pc(m["p_target"]),
        "  stop first     %s" % pc(m["p_stop"]),
        "  neither        %-8s still open at %s" % (pc(m["p_neither"]), until),
        "  green          %-8s target first, or above the entry at %s" % (pc(m["p_green"]), until),
        "",
        "  Price at %s: median %.2f, 90 %% of replays between %.2f and %.2f."
        % (until, m["median_close"], m["p5_close"], m["p95_close"]),
        "  Average R: %+.3f gross, %+.3f after charges (R = the %.2f risk; %d shares: +%gR is %+.2fR after "
        "charges at the target, -1R is %+.2fR at the stop)."
        % (m["avg_R"], m["avg_R_net"], p["risk_per_share"], m["qty"], m["r_at_target"], m["r_net_at_target"],
           m["r_net_at_stop"]),
        "",
    ]
    if h.get("rule"):
        ru = h["rule"]
        stop_words = {"or_mid": "opening-range midpoint", "or_low": "opening-range low"}.get(
            ru["stop_rule"], "%.2f %% under the entry" % ru.get("stop_pct", 0))
        if h["triggered"]:
            lines.append("History: the same rule (%d-min opening range, entries %s-%s, stop at the %s, %gR target) "
                         "on each of those %d real sessions fired %d times: %d target, %d stop, %d neither. "
                         "Average R %+.3f gross, %+.3f after charges."
                         % (ru["or_minutes"], h["entry_window"][0], h["entry_window"][1], stop_words, ru["target_r"],
                            h["sessions"], h["triggered"], h["target"], h["stop"], h["close"], h["avg_R"],
                            h["avg_R_net"]))
        else:
            lines.append("History: the same rule never fired in those %d sessions." % h["sessions"])
        if h.get("no_opening_range"):
            lines.append("(%d session(s) had a missing opening-range bar and were not tested.)" % h["no_opening_range"])
    else:
        lines.append("History: " + h.get("note", "skipped."))
    lines += ["", doc["verdict"]] + list(doc.get("verdict_notes") or []) + [""]
    for w in doc.get("warnings") or []:
        lines.append("Note: " + w + ".")
    if doc["data"]["short_sessions_left_out"]:
        lines.append("Left out %d short session(s) (fewer than %d bars): %s."
                     % (len(doc["data"]["short_sessions_left_out"]), MIN_SESSION_BARS,
                        ", ".join(x["date"] for x in doc["data"]["short_sessions_left_out"])))
    lines.append(doc["note"] + " Repeat this run with --seed %d." % m["seed"])
    return lines


def render_chart(doc, height=17):
    """ASCII fan chart of the bands: one column per 5-min bar."""
    p, m = doc["plan"], doc["mc"]
    bands, times = m["bands"], m["band_times"]
    lo = min([b[0] for b in bands] + [p["stop"], p["target"]])
    hi = max([b[4] for b in bands] + [p["stop"], p["target"]])
    pad = (hi - lo) * 0.04 or 0.01
    lo, hi = lo - pad, hi + pad
    step = (hi - lo) / (height - 1)

    def row(price):
        return max(0, min(height - 1, int(round((hi - price) / step))))
    grid = [[" "] * len(bands) for _ in range(height)]
    for j, b in enumerate(bands):
        r5, r25, r50, r75, r95 = [row(x) for x in b]
        for r in range(r95, r5 + 1):
            grid[r][j] = "."
        for r in range(r75, r25 + 1):
            grid[r][j] = ":"
        grid[r50][j] = "*"
    marks = {row(p["target"]): "target", row(p["stop"]): "stop"}
    if row(p["entry"]) not in marks:
        marks[row(p["entry"])] = "entry"
    for r in (row(p["target"]), row(p["stop"])):
        grid[r] = ["-" if ch == " " else ch for ch in grid[r]]
    out = ["%s %s, %s replay paths from the %s IST entry (seed %d)" % (
        p["symbol"], p["date"], "{:,}".format(m.get("band_paths", BAND_PATHS)), p["entry_time"], m["seed"])]
    for r in range(height):
        label = marks.get(r)
        price = {"target": p["target"], "stop": p["stop"], "entry": p["entry"]}.get(label, hi - r * step)
        left = ("%9.2f |" % price) if (label or r in (0, height - 1)) else "          |"
        out.append(left + "".join(grid[r]) + ("  " + label if label else ""))
    axis = [" "] * len(bands)
    for j in sorted({0, len(bands) // 2, len(bands) - 1}):
        lab = times[j]
        s = max(0, min(j, len(bands) - len(lab)))
        axis[s:s + len(lab)] = list(lab)
    out.append("          +" + "-" * len(bands))
    out.append("           " + "".join(axis))
    out.append("* median   : middle half of the paths   . 90 % of the paths   - stop and target")
    return out


# ---------------------------------------------------------------- commands

def pick_source(cfg):
    src = str(cfg.get("data_source") or "auto").lower()
    if src not in ("auto", "yahoo", "dhan"):
        raise TMError("data_source is auto, yahoo or dhan, not %r" % src)
    if src == "auto":
        return "dhan" if nse.dhan_credentials() else "yahoo"
    return src


def compute(a):
    now = parse_now(a.now)
    try:
        cfg = nse.load_config(a.config)
    except nse.NSEError as e:
        raise TMError(str(e))
    plan = load_plan(a, now)
    meta, bars, raw_count, source = None, None, None, None
    if a.offline:
        meta, bars, raw_count = chart_from_text(read_offline(a.offline, plan["symbol"]), plan["symbol"])
        source = "offline"
    elif pick_source(cfg) == "dhan":
        try:
            meta, bars = fetch_dhan(plan["symbol"], datetime.date.fromisoformat(plan["date"]))
            source = "dhan"
        except nse.NSEError as e:
            if str(cfg.get("data_source") or "auto").lower() == "dhan":
                raise TMError("Dhan: %s" % e)
            print("Dhan did not answer (%s); using Yahoo." % e, file=sys.stderr)
    if bars is None:
        meta, bars, raw_count = chart_from_text(fetch_yahoo(plan["symbol"], plan["exchange"]), plan["symbol"])
        source = "yahoo"
    return time_machine(meta, bars, plan, now, a.replays, a.seed, a.window, a.until, source, cfg, raw_count, a.qty)


def cmd_run(a):
    doc = compute(a)
    path = None
    if a.write is not None:
        path = a.write or os.path.join(nse.trader_dir(), "timemachine.json")
        nse.write_json(path, doc)
    if a.json:
        slim = dict(doc, mc={k: v for k, v in doc["mc"].items() if k != "sample_paths"})
        print(json.dumps(slim, indent=2))
    else:
        print("\n".join(report(doc)))
        if path:
            print("Wrote %s." % path)
        print("Next: /india-budget sizes it to your budget and your max loss per trade. Nothing was placed.")
    return 0


def cmd_chart(a):
    if a.from_file is not None:
        path = a.from_file or os.path.join(nse.trader_dir(), "timemachine.json")
        if not os.path.exists(path):
            raise TMError("no %s: run timemachine.py run --write first" % path)
        try:
            doc = nse.read_json(path)
            doc["mc"]["bands"]
        except (nse.NSEError, KeyError, TypeError):
            raise TMError("%s is not a time machine result" % path)
    else:
        doc = compute(a)
    print("\n".join(render_chart(doc)))
    print("\n".join([doc.get("verdict", "")] + list(doc.get("verdict_notes") or [])))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Time Machine: replay a planned NSE day trade (india-timemachine).")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--plan", help="the plan from india-exit (default trader/plan.json)")
        p.add_argument("--symbol", help="or give the plan as flags: NSE symbol")
        p.add_argument("--side", default="long", choices=["long", "buy", "short", "sell"])
        p.add_argument("--date", help="the plan's day, YYYY-MM-DD (default today, IST)")
        p.add_argument("--entry", type=float)
        p.add_argument("--stop", type=float)
        p.add_argument("--target", type=float)
        p.add_argument("--entry-time", help="the entry bar's close, IST, e.g. 09:40")
        p.add_argument("--tick", type=float, help="the stock's tick (default NSE's price band)")
        p.add_argument("--setup", default="orb")
        p.add_argument("--or-minutes", type=int, default=15)
        p.add_argument("--stop-rule", default="auto", choices=["auto", "or_mid", "or_low", "pct"])
        p.add_argument("--qty", type=int, help="shares for the charges (default the plan's, or max_loss / risk)")
        p.add_argument("--replays", type=int, default=10000)
        p.add_argument("--seed", type=int, default=19)
        p.add_argument("--window", type=int, default=2, help="draw from the same time of day +- this many bars")
        p.add_argument("--until", help="end each replay at this IST time (default the plan's flat_by, 15:05)")
        p.add_argument("--config", help="default trader/config.json (TRADER_DIR)")
        p.add_argument("--offline", metavar="FILE", help="a saved Yahoo chart JSON (.json or .json.gz), no network")
        p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-10-29 09:45"')

    p = sub.add_parser("run", help="replay the plan: 10,000 simulated sessions plus the history")
    common(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--write", nargs="?", const="", metavar="PATH", help="save the result (default trader/timemachine.json)")
    p = sub.add_parser("chart", help="ASCII fan chart of the replays")
    common(p)
    p.add_argument("--from", dest="from_file", nargs="?", const="", metavar="PATH",
                   help="draw a saved result instead of running again")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    if a.side in ("short", "sell"):
        print("timemachine.py: this pack plans longs only.", file=sys.stderr)
        return 1
    try:
        return {"run": cmd_run, "chart": cmd_chart}[a.cmd](a)
    except nse.NSEError as e:
        print("timemachine.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
