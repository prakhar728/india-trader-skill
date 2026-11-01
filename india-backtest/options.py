#!/usr/bin/env python3
"""Options backtest for india-backtest (run as `backtest.py options ...`). Standard library only.

Tests, honestly, "buy ATM Nifty weekly (or stock monthly) options on a range breakout with
~20 % of a Rs 1,00,000 account, strict premium stop and target", plus variants, over about a
year of real expiries. Expired option contracts have no intraday history anywhere free, so
the option's 5-minute price is MODELLED, but anchored to real prices every day:

  1. Signals come from the underlying's real 5-minute bars (Angel One SmartAPI, IST).
  2. The contract is the ATM strike at the signal (nearest strike to the underlying) of the
     nearest expiry (expiry day included; --roll-on-expiry takes the next one on expiry day).
  3. The option's price at each bar is Black-Scholes (r, q configurable; trading-time
     years to 15:30 on expiry day) with the volatility IMPLIED from that exact contract's
     real NSE bhavcopy open price against the underlying's 09:15 open (fallback: the
     bhavcopy close vs the underlying close; fallback: India VIX x this underlying's median
     IV/VIX ratio). --vix-drift moves the vol intraday with India VIX.
  4. Reality clamp: every modelled price is clipped into that contract's real bhavcopy day
     range [low, high]: a target above the real day high is never hit, a stop below the
     real day low never fills there. Every trade where the clamp changed the result is
     flagged and counted.
  5. `validate` compares the model against Angel's real 5-minute bars of contracts that are
     still live (the only ones Angel serves): median absolute % error, by moneyness and
     days to expiry, and whether stop / target outcomes agree.
  6. Charges: nse.option_charges (brokerage Rs 20 an order, STT on the sell premium, NSE
     txn, SEBI, stamp, GST) on premium values; slippage half-spread each side.
  7. Size: alloc-pct (20 %) of capital in premium, whole lots (lot size from the bhavcopy
     that day), at least one lot or the trade is skipped (and counted). Per-lot results too.

    python3 backtest.py options fetch --underlying NIFTY --days 260
    python3 backtest.py options run --underlying NIFTY --strategy all --variants
    python3 backtest.py options validate
    python3 backtest.py options compare

Research holdouts: sessions inside a locked india-research holdout for the underlying
(trader/research/splits.json; bhav:nifty-weekly covers NIFTY) are withheld by default, with a
one-line note; --allow-holdout includes them and the output records "holdout_included": true.

Read-only: it downloads NSE archive files and calls Angel's login, candle and instrument
routes only, never an order route. A backtest never places an order. Not financial advice.
"""

import argparse
import bisect
import csv
import datetime
import gzip
import io
import json
import math
import os
import statistics
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "india-common"))
sys.path.insert(0, HERE)
import nse  # noqa: E402
import backtest as bt  # noqa: E402
import holdout  # noqa: E402  (india-common/holdout.py: research holdouts withheld by default)

BHAV_URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_%s_F_0000.csv.zip"
BHAV_GAP = 1.2                  # seconds between bhavcopy downloads
ANGEL_GAP = 3.0                 # seconds between Angel calls (it 403s on bursts)
ANGEL_BACKOFF = (60, 60, 60)    # on any Angel error (403 rate limit): wait 60 s, at most 3 retries
EACH_SIDE = 15                  # strikes kept each side of the underlying
FIELDS = ["sym", "expiry", "strike", "type", "open", "high", "low", "close", "last", "settle", "und", "oi",
          "vol", "lot"]
DEFAULT_SYMS = ["NIFTY", "SBIN", "RELIANCE", "HDFCBANK"]
INDEX_TOKENS = {"NIFTY": "99926000", "INDIAVIX": "99926017"}
INDEXES = ("NIFTY",)
R_DEFAULT, Q_DEFAULT = 0.055, 0.012
DAY_MIN = 375
YEAR_MIN = 252 * DAY_MIN
OPT_TICK = 0.05
SLIP_PCT = {"index": 0.5, "stock": 1.5}
MIN_IS_TRADES = 10
RUIN_WINDOW, RUIN_FALL = 20, 30.0
VALID_ENTRIES = (3, 20, 44)     # bars ending 09:35, 11:00, 13:00
VALID_STOP, VALID_TARGET = 25.0, 50.0
SPLIT = 2.0 / 3.0


class OptError(nse.NSEError):
    pass


def is_index(sym):
    return sym.upper() in INDEXES


def hm(text):
    t = nse.hhmm(text)
    return t.hour * 60 + t.minute


def _r(x, n=2):
    return None if x is None else round(x, n)


# ================================================================ bhavcopy

def bhav_url(d):
    return BHAV_URL % d.strftime("%Y%m%d")


def unzip_bhav(raw):
    """The bhavcopy zip's bytes -> the CSV text."""
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name = [n for n in z.namelist() if n.lower().endswith(".csv")][0]
        return z.read(name).decode("utf-8", "replace")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def parse_bhav(text, syms=None):
    """UDiFF F&O bhavcopy CSV -> option rows (IDO index options, STO stock options) as dicts."""
    want = set(s.upper() for s in syms) if syms else None
    out = []
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        if r.get("FinInstrmTp") not in ("IDO", "STO"):
            continue
        sym = (r.get("TckrSymb") or "").strip().upper()
        if want is not None and sym not in want:
            continue
        out.append({"sym": sym, "kind": r["FinInstrmTp"], "date": r.get("TradDt"),
                    "expiry": (r.get("FininstrmActlXpryDt") or r.get("XpryDt") or "").strip(),
                    "strike": _f(r.get("StrkPric")), "type": (r.get("OptnTp") or "").strip(),
                    "open": _f(r.get("OpnPric")), "high": _f(r.get("HghPric")), "low": _f(r.get("LwPric")),
                    "close": _f(r.get("ClsPric")), "last": _f(r.get("LastPric")), "settle": _f(r.get("SttlmPric")),
                    "und": _f(r.get("UndrlygPric")), "oi": int(_f(r.get("OpnIntrst"))),
                    "vol": int(_f(r.get("TtlTradgVol"))), "lot": int(_f(r.get("NewBrdLotQty")) or 1)})
    return out


def keep_expiries(expiries, index):
    """Which expiries to keep: the nearest two; for an index also the monthly (last expiry of
    the month) of the nearest expiry's month and of the month after (for validation)."""
    ex = sorted(set(expiries))
    keep = set(ex[:2])
    if index and ex:
        months = {}
        for e in ex:
            months.setdefault(e[:7], []).append(e)
        m0 = ex[0][:7]
        y, m = int(m0[:4]), int(m0[5:7])
        m1 = "%04d-%02d" % (y + (m == 12), 1 if m == 12 else m + 1)
        for mm in (m0, m1):
            if mm in months:
                keep.add(max(months[mm]))
    return keep


def slim_bhav(rows, date, syms, each_side=EACH_SIDE):
    """Option rows of one day -> the slim cached doc: per symbol the kept expiries, and in each
    the strikes within each_side strikes of the underlying."""
    out_rows = []
    by_sym = {}
    for r in rows:
        by_sym.setdefault(r["sym"], []).append(r)
    for sym in sorted(by_sym):
        rs = by_sym[sym]
        keep = keep_expiries([r["expiry"] for r in rs], is_index(sym) or rs[0]["kind"] == "IDO")
        for e in sorted(keep):
            er = [r for r in rs if r["expiry"] == e]
            und = statistics.median([r["und"] for r in er if r["und"] > 0] or [0])
            strikes = sorted(set(r["strike"] for r in er))
            if not strikes:
                continue
            i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - und))
            ks = set(strikes[max(0, i - each_side): i + each_side + 1])
            for r in sorted(er, key=lambda r: (r["strike"], r["type"])):
                if r["strike"] in ks:
                    out_rows.append([r[f] for f in FIELDS])
    return {"date": date, "symbols": sorted(set(s.upper() for s in syms)), "fields": FIELDS, "rows": out_rows}


def bhav_dir():
    return os.path.join(nse.trader_dir(), "cache", "fo_bhav")


def bhav_path(d, base=None):
    return os.path.join(base or bhav_dir(), "%s.json.gz" % (d if isinstance(d, str) else d.isoformat()))


def write_gz_json(path, doc):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(gzip.compress(json.dumps(doc, separators=(",", ":")).encode("utf-8")))
    os.replace(tmp, path)


def read_gz_json(path):
    try:
        return json.loads(nse.read_maybe_gz(path))
    except (OSError, ValueError):
        return None


class Chain(object):
    """One day's slim bhavcopy: rows by (sym, expiry, strike, type)."""

    def __init__(self, doc):
        self.date = doc["date"]
        self.symbols = doc.get("symbols") or []
        idx = dict((f, i) for i, f in enumerate(doc["fields"]))
        self.rows = {}
        self.exp = {}
        self.strikes = {}
        for a in doc["rows"]:
            r = dict((f, a[i]) for f, i in idx.items())
            key = (r["sym"], r["expiry"], float(r["strike"]), r["type"])
            self.rows[key] = r
            self.exp.setdefault(r["sym"], set()).add(r["expiry"])
            self.strikes.setdefault((r["sym"], r["expiry"]), set()).add(float(r["strike"]))
        self.exp = dict((k, sorted(v)) for k, v in self.exp.items())
        self.strikes = dict((k, sorted(v)) for k, v in self.strikes.items())

    def expiries(self, sym):
        return [e for e in self.exp.get(sym, []) if e >= self.date]

    def nearest(self, sym, roll=False):
        ex = self.expiries(sym)
        if roll and ex and ex[0] == self.date:
            ex = ex[1:]
        return ex[0] if ex else None

    def atm(self, sym, expiry, price):
        ks = self.strikes.get((sym, expiry)) or []
        if not ks:
            return None
        return min(ks, key=lambda k: (abs(k - price), k))

    def row(self, sym, expiry, strike, typ):
        return self.rows.get((sym, expiry, float(strike), typ))


def load_chain(d, base=None, _memo={}):
    key = (base or bhav_dir(), str(d))
    if key not in _memo:
        doc = read_gz_json(bhav_path(d, base))
        _memo[key] = Chain(doc) if doc else None
    return _memo[key]


def bhav_dates(base=None):
    try:
        names = os.listdir(base or bhav_dir())
    except OSError:
        return []
    return sorted(n[:10] for n in names if n.endswith(".json.gz") and n[4] == "-")


def fetch_bhav(dates, syms, now, http=None, sleep=time.sleep, log=None):
    """Download, slim and cache each date's bhavcopy (weekdays only). Resumable: a cached day
    that already covers syms is skipped, a 404 (holiday) more than 2 days old is remembered in
    _missing.json. Returns counts."""
    http = http or (lambda u: bt.safe_get(u, headers={"User-Agent": nse.UA}, timeout=60, retries=1))
    log = log or (lambda m: print(m, file=sys.stderr))
    miss_path = os.path.join(bhav_dir(), "_missing.json")
    missing = nse.read_json(miss_path, {}) or {}
    want = set(s.upper() for s in syms)
    got = skipped = holes = errors = 0
    last = 0.0
    for d in dates:
        if d.weekday() >= 5:
            continue
        k = d.isoformat()
        if k in missing:
            holes += 1
            continue
        doc = read_gz_json(bhav_path(k))
        if doc and want <= set(doc.get("symbols") or []):
            skipped += 1
            continue
        wait = BHAV_GAP - (time.time() - last)
        if wait > 0:
            sleep(wait)
        last = time.time()
        try:
            raw = http(bhav_url(d))
        except nse.NSEError as e:
            if "HTTP 404" in str(e):
                if (now.date() - d).days >= 2:
                    missing[k] = "404 (holiday or not published)"
                    nse.write_json(miss_path, missing)
                holes += 1
                continue
            errors += 1
            log("  bhavcopy %s: %s" % (k, str(e)[:100]))
            continue
        try:
            rows = parse_bhav(unzip_bhav(raw), want | set((doc or {}).get("symbols") or []))
        except (zipfile.BadZipFile, KeyError, IndexError) as e:
            errors += 1
            log("  bhavcopy %s: not a readable zip (%s)" % (k, e))
            continue
        write_gz_json(bhav_path(k), slim_bhav(rows, k, want | set((doc or {}).get("symbols") or [])))
        got += 1
        if got % 20 == 0:
            log("  bhavcopy: %d days fetched (at %s)" % (got, k))
    return {"fetched": got, "cached": skipped, "missing": holes, "errors": errors}


# ================================================================ Black-Scholes

def ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S, K, T, sigma, r=R_DEFAULT, q=Q_DEFAULT, typ="CE"):
    """Black-Scholes-Merton price of a European call (CE) or put (PE). T in years."""
    if T <= 0 or sigma <= 0:
        fwd_intr = S - K if typ == "CE" else K - S
        return max(0.0, fwd_intr)
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / sq
    d2 = d1 - sq
    if typ == "CE":
        return S * math.exp(-q * T) * ncdf(d1) - K * math.exp(-r * T) * ncdf(d2)
    return K * math.exp(-r * T) * ncdf(-d2) - S * math.exp(-q * T) * ncdf(-d1)


def implied_vol(price, S, K, T, r=R_DEFAULT, q=Q_DEFAULT, typ="CE", lo=0.005, hi=5.0):
    """Volatility that reprices `price`, by bisection; None when the price is outside the
    no-arbitrage bounds (under intrinsic, or above the underlying)."""
    if price is None or price <= 0 or T <= 0 or S <= 0 or K <= 0:
        return None
    if price <= bs_price(S, K, T, lo, r, q, typ) + 1e-9 or price >= bs_price(S, K, T, hi, r, q, typ):
        return None
    a, b = lo, hi
    for _ in range(100):
        m = 0.5 * (a + b)
        if bs_price(S, K, T, m, r, q, typ) < price:
            a = m
        else:
            b = m
        if b - a < 1e-7:
            break
    return 0.5 * (a + b)


# ================================================================ trading-time clock

class Clock(object):
    """Trading minutes to 15:30 on expiry day. Trading days = the bhavcopy dates where known
    (that covers 2025's holidays, which nse.py does not list), else nse.is_trading_day."""

    def __init__(self, known_dates=()):
        self.known = sorted(set(str(d) for d in known_dates))
        self._memo = {}

    def is_trading(self, d):
        k = d.isoformat()
        if self.known and self.known[0] <= k <= self.known[-1]:
            i = bisect.bisect_left(self.known, k)
            return i < len(self.known) and self.known[i] == k
        return nse.is_trading_day(d)

    def days_between(self, d, e):
        """Trading days strictly after d and strictly before e."""
        key = (d, e)
        if key not in self._memo:
            n, x = 0, d + datetime.timedelta(days=1)
            while x < e:
                n += self.is_trading(x)
                x += datetime.timedelta(days=1)
            self._memo[key] = n
        return self._memo[key]

    def minutes(self, d, minute_of_day, expiry):
        """d: date, minute_of_day: minutes after midnight IST, expiry: date."""
        close = 15 * 60 + 30
        today = max(0, close - minute_of_day) if self.is_trading(d) else 0
        if expiry == d:
            return today
        if expiry < d:
            return 0
        return today + DAY_MIN * self.days_between(d, expiry) + DAY_MIN

    def years(self, d, minute_of_day, expiry):
        return max(self.minutes(d, minute_of_day, expiry), 1) / float(YEAR_MIN)

    def dte(self, d, expiry):
        """Trading days to expiry (0 on expiry day)."""
        return 0 if expiry <= d else self.days_between(d, expiry) + 1


def as_date(x):
    return x if isinstance(x, datetime.date) else datetime.date.fromisoformat(str(x)[:10])


# ================================================================ option pricing on a day

def round_up_tick(p, tick=OPT_TICK):
    return round(math.ceil(round(p / tick, 6) - 1e-9) * tick, 2)


def round_down_tick(p, tick=OPT_TICK):
    return round(math.floor(round(p / tick, 6) + 1e-9) * tick, 2)


class Model(object):
    """Prices one contract on one day from the underlying's bars."""

    def __init__(self, clock, r=R_DEFAULT, q=Q_DEFAULT, vix_drift=False):
        self.clock, self.r, self.q, self.vix_drift = clock, r, q, vix_drift

    def anchor(self, row, sess, d, expiry, vix_open=None, ratio=None):
        """(sigma, source) for the contract-day."""
        K, typ = float(row["strike"]), row["type"]
        b0 = sess.bars[0]
        if row["open"] > 0 and b0 is not None:
            iv = implied_vol(row["open"], b0["o"], K, self.clock.years(d, 9 * 60 + 15, expiry), self.r, self.q, typ)
            if iv:
                return iv, "bhav_open"
        if row["close"] > 0 and row["und"] > 0 and expiry > d:
            iv = implied_vol(row["close"], row["und"], K, self.clock.years(d, 15 * 60 + 29, expiry), self.r, self.q, typ)
            if iv:
                return iv, "bhav_close"
        if vix_open and ratio:
            return vix_open / 100.0 * ratio, "vix"
        return None, None

    def path(self, sess, row, d, expiry, sigma, vix=None):
        """Modelled option bars [(o, h, l, c) or None per slot], unclamped."""
        K, typ = float(row["strike"]), row["type"]
        v0 = None
        if self.vix_drift and vix is not None and vix[0] is not None:
            v0 = vix[0]["o"]
        out = []
        for k, b in enumerate(sess.bars):
            if b is None:
                out.append(None)
                continue
            start = bt.OPEN_MIN + bt.INTERVAL * k
            end = start + bt.INTERVAL
            sg, sg0 = sigma, sigma
            if v0 and vix[k] is not None:
                sg = sigma * vix[k]["c"] / v0
                sg0 = sigma * vix[k]["o"] / v0
            T0 = self.clock.years(d, start, expiry)
            T1 = self.clock.years(d, end, expiry)

            def P(S, T, s):
                return max(0.05, bs_price(S, K, T, s, self.r, self.q, typ))
            hi_S, lo_S = (b["h"], b["l"]) if typ == "CE" else (b["l"], b["h"])
            o, h, l, c = P(b["o"], T0, sg0), P(hi_S, T1, sg), P(lo_S, T1, sg), P(b["c"], T1, sg)
            out.append((o, max(o, h, l, c), min(o, h, l, c), c))
        return out


def clamp_path(path, lo, hi):
    if not (lo and hi and hi >= lo > 0):
        return list(path)

    def c(x):
        return min(max(x, lo), hi)
    return [None if b is None else (c(b[0]), c(b[1]), c(b[2]), c(b[3])) for b in path]


# ================================================================ signals

def sig_orb(s, p, win):
    n = p.get("or_minutes", 15) // bt.INTERVAL
    orr = bt.opening_range(s, n)
    if orr is None:
        return "no opening range (a bar is missing)"
    oh, ol = orr
    for k in range(n, win.last + 1):
        b = s.bars[k]
        if b is None:
            continue
        if bt.slot_end(k) > win.cutoff:
            break
        if not win.ok(k):
            continue
        if b["c"] > oh:
            return {"k": k, "side": "CE", "level": (oh + ol) / 2.0,
                    "why": "close %.2f above the %d-min range high %.2f" % (b["c"], p.get("or_minutes", 15), oh)}
        if b["c"] < ol:
            return {"k": k, "side": "PE", "level": (oh + ol) / 2.0,
                    "why": "close %.2f below the %d-min range low %.2f" % (b["c"], p.get("or_minutes", 15), ol)}
    return None


def sig_pdh_pdl(s, p, win):
    if not s.prev:
        return "no prior day"
    pdh, pdl = s.prev["high"], s.prev["low"]
    prev_c = None
    for k in range(0, win.last + 1):
        b = s.bars[k]
        if b is None:
            continue
        if bt.slot_end(k) > win.cutoff:
            break
        up = prev_c is not None and prev_c <= pdh < b["c"]
        dn = prev_c is not None and prev_c >= pdl > b["c"]
        prev_c = b["c"]
        if win.ok(k) and (up or dn):
            lvl = pdh if up else pdl
            return {"k": k, "side": "CE" if up else "PE", "level": lvl,
                    "why": "close %.2f crossed %s the prior day's %s %.2f" % (
                        b["c"], "above" if up else "below", "high" if up else "low", lvl)}
    return None


def opt_grid(ustop=True):
    out = []
    for st in (25, 35, 50):
        for tg in (30, 50, 100):
            out.append({"stop_pct": st, "target_pct": tg})
    if ustop:
        for tg in (30, 50, 100):
            out.append({"stop_pct": None, "ustop": True, "target_pct": tg})
    out.append({"stop_pct": 25, "target_pct": None, "trail_after": 30, "trail_pct": 20})
    out.append({"stop_pct": 35, "target_pct": None, "trail_after": 50, "trail_pct": 25})
    out.append({"stop_pct": 25, "target_pct": 50, "expiry_flat": "14:45"})
    return out


def _with(base, rows):
    return [dict(base, **r) for r in rows]


STRATS = {
    "opt_orb": {
        "fn": sig_orb, "skip_expiry": False, "title": "15-min range breakout, buy the ATM option (the thread's setup)",
        "rules": ("Opening range = the underlying's 09:15-09:30 high and low. The first 5-minute bar (closing 09:35 "
                  "to 14:30) that closes above the high buys the ATM call; the first that closes below the low buys "
                  "the ATM put. One trade a day. ATM = nearest strike to the underlying at the signal, nearest expiry "
                  "(expiry day included). Premium stop -25 %, target +50 % (variants: stop -25/-35/-50 % x target "
                  "+30/+50/+100 %; underlying back to the range midpoint as the stop; trailing stops; flat by 14:45 "
                  "on expiry day). Flat by 15:05 (15:00 on expiry day)."),
        "default": {"or_minutes": 15, "stop_pct": 25, "target_pct": 50},
        "variants": _with({"or_minutes": 15}, opt_grid(True))},
    "opt_orb30": {
        "fn": sig_orb, "skip_expiry": False, "title": "30-min range breakout, ATM option",
        "rules": ("Same as opt_orb with the 09:15-09:45 range (signals close 09:50 to 14:30)."),
        "default": {"or_minutes": 30, "stop_pct": 25, "target_pct": 50},
        "variants": _with({"or_minutes": 30}, opt_grid(True))},
    "opt_pdh_pdl": {
        "fn": sig_pdh_pdl, "skip_expiry": False, "title": "previous day's high / low break, ATM option",
        "rules": ("The first bar (closing 09:30 to 14:30) whose close crosses above the previous session's high "
                  "(the bar before closed at or under it) buys the ATM call; crossing below the previous low buys "
                  "the ATM put. Premium stop -25 %, target +50 % (premium variants as opt_orb, no midpoint stop)."),
        "default": {"stop_pct": 25, "target_pct": 50},
        "variants": opt_grid(False)},
    "opt_no_expiry": {
        "fn": sig_orb, "skip_expiry": True, "title": "opt_orb, but never on expiry day",
        "rules": "opt_orb's rules, skipping every expiry day of the contract it would trade (for contrast).",
        "default": {"or_minutes": 15, "stop_pct": 25, "target_pct": 50},
        "variants": [v for v in _with({"or_minutes": 15}, opt_grid(True)) if "expiry_flat" not in v]},
}
ORDER = ["opt_orb", "opt_orb30", "opt_pdh_pdl", "opt_no_expiry"]


def variant_label(name, p):
    parts = []
    if p.get("ustop"):
        parts.append("stop=underlying mid")
    elif p.get("stop_pct"):
        parts.append("stop=-%g%%" % p["stop_pct"])
    if p.get("trail_after"):
        parts.append("trail %g%% after +%g%%" % (p["trail_pct"], p["trail_after"]))
    parts.append("target=+%g%%" % p["target_pct"] if p.get("target_pct") else "no target")
    if p.get("expiry_flat"):
        parts.append("expiry flat %s" % p["expiry_flat"])
    return " ".join(parts) + (" (default)" if p == STRATS[name]["default"] else "")


def parse_strats(text):
    if not text or text == "all":
        return list(ORDER)
    out = []
    for s in text.split(","):
        s = s.strip().lower().replace("-", "_")
        if not s.startswith("opt_"):
            s = "opt_" + s
        if s not in STRATS:
            raise OptError("unknown options strategy %r: pick from %s (or all)" % (s, ", ".join(ORDER)))
        if s not in out:
            out.append(s)
    return out


# ================================================================ the engine

class Day(object):
    __slots__ = ("sym", "date", "d", "sess", "chain", "vix", "expiry")

    def __init__(self, sym, sess, chain, vix=None):
        self.sym, self.date, self.sess, self.chain, self.vix = sym, sess.date, sess, chain, vix
        self.d = as_date(sess.date)
        self.expiry = chain.nearest(sym) if chain else None


class Engine(object):
    def __init__(self, sym, days, clock, cfg):
        self.sym, self.days, self.clock, self.cfg = sym, days, clock, cfg
        self.model = Model(clock, cfg["r"], cfg["q"], cfg["vix_drift"])
        self.win = bt.Window({"flat_by": cfg.get("flat_by", "15:05")})
        self._anchor, self._path = {}, {}
        self.ratio = self.iv_vix_ratio()

    def iv_vix_ratio(self):
        """Median (IV at the open of the nearest-expiry ATM call) / (India VIX open / 100)."""
        rs = []
        for day in self.days:
            if not day.vix or day.vix[0] is None or not day.expiry or day.sess.bars[0] is None:
                continue
            S = day.sess.bars[0]["o"]
            K = day.chain.atm(self.sym, day.expiry, S)
            row = day.chain.row(self.sym, day.expiry, K, "CE")
            if not row or row["open"] <= 0:
                continue
            iv = implied_vol(row["open"], S, K, self.clock.years(day.d, 555, as_date(day.expiry)),
                             self.cfg["r"], self.cfg["q"], "CE")
            if iv:
                rs.append(iv / (day.vix[0]["o"] / 100.0))
        return statistics.median(rs) if rs else None

    def contract(self, day, row, expiry):
        key = (day.date, row["expiry"], row["strike"], row["type"])
        if key not in self._anchor:
            vo = day.vix[0]["o"] if day.vix and day.vix[0] is not None else None
            self._anchor[key] = self.model.anchor(row, day.sess, day.d, expiry, vo, self.ratio)
        sigma, src = self._anchor[key]
        if sigma is None:
            return None, None, None
        if key not in self._path:
            self._path[key] = self.model.path(day.sess, row, day.d, expiry, sigma, day.vix)
        return self._path[key], sigma, src

    def slip(self, p):
        return max(p * self.cfg["slip_pct"] / 100.0, OPT_TICK)

    def trade(self, day, sig, p):
        """A signal -> (trade dict, None) or (None, why)."""
        roll = self.cfg.get("roll")
        expiry = day.chain.nearest(self.sym, roll)
        if not expiry:
            return None, "no live expiry in the bhavcopy"
        ed = as_date(expiry)
        k = sig["k"]
        S = day.sess.bars[k]["c"]
        K = day.chain.atm(self.sym, expiry, S)
        row = day.chain.row(self.sym, expiry, K, sig["side"]) if K is not None else None
        if not row:
            return None, "no bhavcopy row for the ATM contract"
        path, sigma, src = self.contract(day, row, ed)
        if path is None:
            return None, "no implied volatility (bhavcopy prices outside bounds, no VIX)"
        is_exp = expiry == day.date
        last = self.win.last
        if is_exp:
            last = min(last, (hm(p.get("expiry_flat") or self.cfg.get("expiry_flat", "15:00")) - bt.OPEN_MIN)
                       // bt.INTERVAL - 1)
        if k >= last:
            return None, "signal too late for the flat-by"
        lo, hi = row["low"], row["high"]
        cl = clamp_path(path, lo, hi)
        lot = int(row["lot"] or 1)
        raw_res = self.exits(path, k, last, p, day, sig, row, ed, sigma, clamped=False)
        res = self.exits(cl, k, last, p, day, sig, row, ed, sigma, clamped=True)
        fill = res["entry"]
        budget = self.cfg["capital"] * self.cfg["alloc_pct"] / 100.0
        lots = int(budget // (fill * lot)) if fill > 0 else 0
        name = "%s %s %g %s" % (self.sym, expiry, K, sig["side"])
        per_lot_ch = nse.option_charges(fill * lot, res["exit"] * lot, on=day.date)["total"]
        per_lot_net = round((res["exit"] - fill) * lot - per_lot_ch, 2)
        changed = (raw_res["outcome"] != res["outcome"] or
                   abs((raw_res["exit"] - raw_res["entry"]) - (res["exit"] - res["entry"])) > 0.05 * fill)
        t = {"underlying": self.sym, "date": day.date, "time": bt.clock(bt.slot_end(k)), "side": sig["side"],
             "contract": name, "expiry": expiry, "strike": K, "expiry_day": is_exp,
             "dte": self.clock.dte(day.d, ed), "underlying_px": S, "iv": round(sigma, 4), "iv_source": src,
             "model_premium": round(path[k][3], 2), "entry": fill, "stop": res["stop"], "target": res["target"],
             "lot": lot, "lots": lots, "qty": lots * lot, "exit": res["exit"], "exit_time": res["exit_time"],
             "outcome": res["outcome"], "real_low": lo, "real_high": hi,
             "unclamped_outcome": raw_res["outcome"], "unclamped_exit": raw_res["exit"],
             "unclamped_entry": raw_res["entry"], "clamp_changed": changed,
             "ret_pct_mid": round(100.0 * (res["exit_mid"] / res["entry_mid"] - 1), 2),
             "pnl_per_lot_net": per_lot_net, "charges_per_lot": per_lot_ch, "why": sig["why"]}
        if lots < 1:
            t.update(pnl=0.0, charges=0.0, pnl_net=0.0, ret_pct=0.0, ret_pct_net=0.0, skipped="one lot costs "
                     "%s, more than %g %% of capital" % (nse.inr(fill * lot, 0), self.cfg["alloc_pct"]))
            return t, None
        qty = lots * lot
        ch = nse.option_charges(fill * qty, res["exit"] * qty, on=day.date)["total"]
        gross = round((res["exit"] - fill) * qty, 2)
        net = round(gross - ch, 2)
        t.update(pnl=gross, charges=ch, pnl_net=net, premium=round(fill * qty, 2),
                 ret_pct=round(100.0 * gross / (fill * qty), 2), ret_pct_net=round(100.0 * net / (fill * qty), 2))
        return t, None

    def exits(self, path, k, last, p, day, sig, row, ed, sigma, clamped):
        prem = path[k][3]
        fill = round_up_tick(prem + self.slip(prem))
        stop = round_down_tick(fill * (1 - p["stop_pct"] / 100.0)) if p.get("stop_pct") else None
        target = round_down_tick(fill * (1 + p["target_pct"] / 100.0)) if p.get("target_pct") else None
        peak = fill
        lvl, side = sig["level"], sig["side"]
        bars = day.sess.bars
        lo, hi = (row["low"], row["high"]) if clamped else (None, None)

        def clip(x):
            return min(max(x, lo), hi) if (lo and hi and hi >= lo > 0) else x

        def out(px, i, why, mid=None):
            return {"entry": fill, "stop": stop, "target": target, "exit": max(OPT_TICK, round_down_tick(px)),
                    "exit_time": bt.clock(bt.slot_end(i)), "outcome": why, "entry_mid": prem,
                    "exit_mid": px if mid is None else mid}
        for i in range(k + 1, last + 1):
            b = path[i]
            if b is None:
                continue
            o, h, l, c = b
            cur = stop
            if p.get("trail_after") and peak >= fill * (1 + p["trail_after"] / 100.0):
                tr = round_down_tick(peak * (1 - p["trail_pct"] / 100.0))
                cur = max(cur or 0, tr)
            if cur is not None and o <= cur:
                return out(o - self.slip(o), i, "stop_gap" if cur == stop else "trail_stop", o)
            if cur is not None and l <= cur:
                return out(cur - self.slip(cur), i, "stop" if cur == stop else "trail_stop", cur)
            if p.get("ustop"):
                u = bars[i]
                gap = u["o"] <= lvl if side == "CE" else u["o"] >= lvl
                touch = u["l"] <= lvl if side == "CE" else u["h"] >= lvl
                if gap or touch:
                    S = u["o"] if gap else lvl
                    m = 0 if gap else 1
                    T = self.clock.years(day.d, bt.OPEN_MIN + bt.INTERVAL * (i + m), ed)
                    px = clip(max(0.05, bs_price(S, float(row["strike"]), T, sigma, self.cfg["r"], self.cfg["q"],
                                                 row["type"])))
                    return out(px - self.slip(px), i, "underlying_stop", px)
            if target is not None and h >= target:
                return out(target, i, "target")
            peak = max(peak, h)
        i = max(j for j in range(k, last + 1) if path[j] is not None)
        c = path[i][3]
        r = out(c - self.slip(c), i, "flat", c)
        return r


# ================================================================ metrics

def small(trades):
    n = len(trades)
    if not n:
        return {"trades": 0, "win_pct": None, "avg_inr_net": None, "avg_ret_pct_net": None, "total_inr_net": 0.0}
    return {"trades": n, "win_pct": round(100.0 * sum(1 for t in trades if t["pnl_net"] > 0) / n, 1),
            "avg_inr_net": round(sum(t["pnl_net"] for t in trades) / n, 2),
            "avg_ret_pct_net": round(sum(t["ret_pct_net"] for t in trades) / n, 2),
            "total_inr_net": round(sum(t["pnl_net"] for t in trades), 2)}


def account(trades, session_dates, capital):
    """Daily equity of a capital-rupee account, its drawdown, worst day and ruin windows."""
    by = {}
    for t in trades:
        by[t["date"]] = by.get(t["date"], 0.0) + t["pnl_net"]
    eq, cum, curve = [], 0.0, []
    for d in session_dates:
        cum += by.get(d, 0.0)
        eq.append(capital + cum)
        if d in by:
            curve.append({"date": d, "cum_inr_net": round(cum, 2)})
    peak, dd = capital, 0.0
    for v in eq:
        peak = max(peak, v)
        dd = max(dd, peak - v)
    # ruin windows: a fresh capital-rupee account started on each session; did it fall more than
    # RUIN_FALL % below its start at any close in the next RUIN_WINDOW sessions?
    windows = fell = 0
    worst_window = 0.0
    daily = [by.get(d, 0.0) for d in session_dates]
    for i in range(0, max(0, len(daily) - RUIN_WINDOW + 1)):
        c = low = 0.0
        for x in daily[i:i + RUIN_WINDOW]:
            c += x
            low = min(low, c)
        loss = min(100.0, -100.0 * low / capital)
        worst_window = max(worst_window, loss)
        windows += 1
        fell += loss > RUIN_FALL
    worst = min(by.items(), key=lambda kv: kv[1]) if by else (None, 0.0)
    blown = next((d for d, v in zip(session_dates, eq) if v <= 0), None)
    return {"max_dd_inr": round(dd, 2), "max_dd_pct": round(100.0 * dd / capital, 2),
            "worst_day": {"date": worst[0], "inr": round(worst[1], 2)},
            "ruin_windows": windows, "ruin_windows_fell": fell,
            "ruin_windows_pct": round(100.0 * fell / windows, 2) if windows else None,
            "worst_20d_loss_pct": round(worst_window, 2), "end_equity": round(capital + cum, 2), "blown_on": blown,
            "equity_curve": curve}


def opt_metrics(trades, session_dates, capital, detail=True):
    trades = sorted(trades, key=lambda t: (t["date"], t["time"]))
    m = small(trades)
    n = len(trades)
    if not n:
        m.update(account(trades, session_dates, capital) if detail else {})
        return m
    pnl = [t["pnl_net"] for t in trades]
    wins = sum(x for x in pnl if x > 0)
    losses = -sum(x for x in pnl if x < 0)
    sd = statistics.stdev(pnl) if n > 1 else None
    best = cur = 0
    for x in pnl:
        cur = cur + 1 if x <= 0 else 0
        best = max(best, cur)
    m.update({
        "avg_inr_gross": round(sum(t["pnl"] for t in trades) / n, 2),
        "avg_ret_pct_gross": round(sum(t["ret_pct"] for t in trades) / n, 2),
        "avg_ret_pct_before_costs": round(sum(t["ret_pct_mid"] for t in trades) / n, 2),
        "win_pct_gross": round(100.0 * sum(1 for t in trades if t["pnl"] > 0) / n, 1),
        "profit_factor": round(wins / losses, 2) if losses > 0 else None,
        "t_stat": round((sum(pnl) / n) / (sd / math.sqrt(n)), 2) if sd else None,
        "total_charges_inr": round(sum(t["charges"] for t in trades), 2),
        "avg_premium_inr": round(sum(t["premium"] for t in trades) / n, 2),
        "avg_lots": round(sum(t["lots"] for t in trades) / n, 2),
        "outcomes": dict((o, sum(1 for t in trades if t["outcome"] == o))
                         for o in ("target", "stop", "stop_gap", "trail_stop", "underlying_stop", "flat")),
        "longest_losing_streak": best,
        "clamp_changed": sum(1 for t in trades if t["clamp_changed"]),
        "clamp_changed_pct": round(100.0 * sum(1 for t in trades if t["clamp_changed"]) / n, 1),
        "iv_sources": dict((s, sum(1 for t in trades if t["iv_source"] == s)) for s in ("bhav_open", "bhav_close", "vix")),
        "first": trades[0]["date"], "last": trades[-1]["date"],
    })
    if detail:
        m.update(account(trades, session_dates, capital))
        m["by_expiry_day"] = [dict(small([t for t in trades if t["expiry_day"] == e]), bucket=b)
                              for e, b in ((True, "expiry_day"), (False, "other_day"))]
        m["by_side"] = [dict(small([t for t in trades if t["side"] == s]), bucket=s) for s in ("CE", "PE")]
        months = sorted(set(t["date"][:7] for t in trades))
        m["by_month"] = [dict(small([t for t in trades if t["date"][:7] == mo]), bucket=mo) for mo in months]
    return m


def per_lot(all_trades):
    n = len(all_trades)
    if not n:
        return {"signals": 0}
    x = [t["pnl_per_lot_net"] for t in all_trades]
    return {"signals": n, "avg_inr_per_lot_net": round(sum(x) / n, 2), "total_inr_per_lot_net": round(sum(x), 2),
            "win_pct": round(100.0 * sum(1 for v in x if v > 0) / n, 1)}


def opt_verdict(full, oos, capital):
    n = full.get("trades") or 0
    if n == 0:
        return "never fired: nothing to judge"
    if n < 30:
        return "too few trades to judge (%d)" % n
    g, net = full["avg_inr_gross"], full["avg_inr_net"]
    o = (oos or {}).get("avg_inr_net")
    on = (oos or {}).get("trades") or 0
    ruin = full.get("ruin_windows_pct") or 0
    tail = ("; the %s account fell over 30 %% in %.0f %% of 20-session windows" % (
        nse.inr(capital, 0), ruin)) if ruin else ""
    mid = full.get("avg_ret_pct_before_costs")
    if full.get("blown_on"):
        tail += "; a %s account would have been wiped out by %s" % (nse.inr(capital, 0), full["blown_on"])
    if net <= 0:
        if g > 0:
            return "no edge after charges: %s a trade before charges becomes %s net%s" % (
                nse.inr(g, 0), nse.inr(net, 0), tail)
        if mid is not None and mid > 0:
            return ("no edge after costs: %+.1f %% on premium at model mid prices, but the spread and charges make it "
                    "%s a trade%s" % (mid, nse.inr(net, 0), tail))
        return "no edge, even before costs (%+.1f %% on premium at model mid prices; %s a trade net)%s" % (
            mid if mid is not None else 0.0, nse.inr(net, 0), tail)
    if o is None or on < 10:
        return "small positive edge (%s net a trade), too few out-of-sample trades to confirm%s" % (nse.inr(net, 0), tail)
    if o <= 0:
        return "positive over the year (%s net a trade) but it did not hold out of sample (%s)%s" % (
            nse.inr(net, 0), nse.inr(o, 0), tail)
    t = full.get("t_stat") or 0
    if t >= 2 and n >= 200:
        return "positive edge after charges (%s net a trade, t=%.1f, held out of sample at %s)%s" % (
            nse.inr(net, 0), t, nse.inr(o, 0), tail)
    return "small positive edge, thin sample (%s net a trade, t=%.1f; out of sample %s)%s" % (
        nse.inr(net, 0), t, nse.inr(o, 0), tail)


def split_by_expiry(days):
    """First ~2/3 of the expiry cycles in sample, the rest out of sample -> (is_dates set, info)."""
    exps = sorted(set(d.expiry for d in days if d.expiry))
    if len(exps) < 3:
        return set(d.date for d in days), {"expiries": len(exps)}
    cut = exps[int(round(len(exps) * SPLIT)) - 1]
    is_dates = set(d.date for d in days if d.expiry and d.expiry <= cut)
    oos = sorted(d.date for d in days if d.date not in is_dates)
    ins = sorted(is_dates)
    return is_dates, {"expiries": len(exps), "in_sample_expiries": sum(1 for e in exps if e <= cut),
                      "out_of_sample_expiries": sum(1 for e in exps if e > cut), "cut_expiry": cut,
                      "in_sample": [ins[0], ins[-1], len(ins)] if ins else None,
                      "out_of_sample": [oos[0], oos[-1], len(oos)] if oos else None}


def run_strategy(eng, name, params):
    spec = STRATS[name]
    taken, sized_out, skips = [], [], {}
    for day in eng.days:
        if not day.chain or not day.expiry:
            skips["no bhavcopy for the day"] = skips.get("no bhavcopy for the day", 0) + 1
            continue
        exp = day.chain.nearest(eng.sym, eng.cfg.get("roll"))
        if spec["skip_expiry"] and (day.expiry == day.date or exp == day.date):
            continue
        sig = spec["fn"](day.sess, params, eng.win)
        if sig is None:
            continue
        if isinstance(sig, str):
            skips[sig] = skips.get(sig, 0) + 1
            continue
        t, why = eng.trade(day, sig, params)
        if t is None:
            skips[why] = skips.get(why, 0) + 1
        elif t.get("skipped"):
            sized_out.append(t)
            skips["one lot over the allocation"] = skips.get("one lot over the allocation", 0) + 1
        else:
            taken.append(t)
    return taken, sized_out, skips


def backtest_underlying(eng, names, with_trades=True):
    dates = [d.date for d in eng.days]
    is_dates, split = split_by_expiry(eng.days)
    oos_dates = [d for d in dates if d not in is_dates]
    cap = eng.cfg["capital"]
    out = {}
    for name in names:
        spec = STRATS[name]
        rows, chosen_pool = [], []
        default = None
        for p in spec["variants"]:
            tr, so, sk = run_strategy(eng, name, p)
            ins = [t for t in tr if t["date"] in is_dates]
            oos = [t for t in tr if t["date"] not in is_dates]
            row = {"label": variant_label(name, p), "params": p, "default": p == spec["default"],
                   "full": opt_metrics(tr, dates, cap, detail=False), "in_sample": small(ins),
                   "out_of_sample": small(oos), "per_lot": per_lot(tr + so), "skipped_size": len(so)}
            row["full"].update(account(tr, dates, cap))
            del row["full"]["equity_curve"]
            rows.append(row)
            chosen_pool.append((row, oos))
            if p == spec["default"]:
                default = (tr, so, sk)
        ok = [(r, o) for r, o in chosen_pool if r["in_sample"]["trades"] >= MIN_IS_TRADES]
        chosen, chosen_oos = max(ok, key=lambda ro: (ro[0]["in_sample"]["avg_inr_net"], ro[0]["in_sample"]["trades"])) \
            if ok else [ro for ro in chosen_pool if ro[0]["default"]][0]
        tr, so, sk = default
        full = opt_metrics(tr, dates, cap)
        oos_m = opt_metrics(chosen_oos, oos_dates, cap, detail=False)
        drow = [r for r in rows if r["default"]][0]
        res = {"title": spec["title"], "rules": spec["rules"], "default_variant": variant_label(name, spec["default"]),
               "full": full, "per_lot": per_lot(tr + so), "skipped": sk, "skipped_size": len(so),
               "variants": rows,
               "walk_forward": {"split": split, "chosen": chosen["label"], "chosen_params": chosen["params"],
                                "chosen_by": ("best in-sample average net rupees a trade with at least %d trades"
                                              % MIN_IS_TRADES) if ok else "default (too few in-sample trades)",
                                "chosen_in_sample": chosen["in_sample"], "chosen_out_of_sample": oos_m,
                                "default_out_of_sample": drow["out_of_sample"]},
               "verdict": opt_verdict(full, oos_m, cap)}
        if with_trades:
            res["trades"] = tr
            res["skipped_size_trades"] = so
        out[name] = res
    return out, split


# ================================================================ loading data

def angel_store(sym):
    return bt.read_store(bt.store_path("angel", sym, "5m"))


def load_sessions(sym, now=None):
    doc = angel_store(sym)
    if not doc:
        raise OptError("no Angel 5-minute bars for %s cached: run `backtest.py options fetch --underlying %s`"
                       % (sym, sym))
    ss, notes = bt.build_sessions(sym, doc["bars"], None, now)
    return ss, notes


def build_days(sym, sessions, vix_sessions, base=None, days=None, date_from=None, date_to=None):
    vix = dict((s.date, s.bars) for s in vix_sessions or [])
    have = set(bhav_dates(base))
    out, no_bhav = [], []
    for s in sessions:
        if date_from and s.date < date_from or date_to and s.date > date_to:
            continue
        if s.date not in have:
            no_bhav.append(s.date)
            continue
        ch = load_chain(s.date, base)
        if not ch or sym not in ch.exp:
            no_bhav.append(s.date)
            continue
        out.append(Day(sym, s, ch, vix.get(s.date)))
    if days:
        out = out[-days:]
    return out, no_bhav


def make_cfg(a, sym):
    return {"capital": float(getattr(a, "capital", None) or 100000), "alloc_pct": float(getattr(a, "alloc_pct", None) or 20),
            "slip_pct": float(a.slip_pct) if getattr(a, "slip_pct", None) is not None else
            SLIP_PCT["index" if is_index(sym) else "stock"],
            "r": float(getattr(a, "r", None) or R_DEFAULT), "q": float(getattr(a, "q", None) if getattr(a, "q", None) is not None else Q_DEFAULT),
            "vix_drift": bool(getattr(a, "vix_drift", False)), "roll": bool(getattr(a, "roll_on_expiry", False)),
            "expiry_flat": getattr(a, "expiry_flat", None) or "15:00", "flat_by": "15:05"}


def run_underlying(sym, a, names, now):
    sessions, notes = load_sessions(sym, now)
    vix_ss = []
    vdoc = angel_store("INDIAVIX")
    if vdoc:
        vix_ss = bt.build_sessions("INDIAVIX", vdoc["bars"], None, now)[0]
    # sessions inside a locked india-research holdout for this underlying (e.g. bhav:nifty-weekly for
    # NIFTY) are withheld before --days / --from / --to, unless --allow-holdout
    kept, held = holdout.withhold({sym: sessions}, allow=bool(getattr(a, "allow_holdout", False)))
    sessions = kept.get(sym, [])
    line = holdout.note(held)
    if line:
        print("%s: %s" % (sym, line), file=sys.stderr)
    days, no_bhav = build_days(sym, sessions, vix_ss, None, getattr(a, "days", None), getattr(a, "date_from", None),
                               getattr(a, "date_to", None))
    if len(days) < 3:
        raise OptError("%s: only %d session(s) have both Angel bars and a cached bhavcopy: run options fetch%s"
                       % (sym, len(days), (" (%d session(s) withheld as research holdout; --allow-holdout includes "
                                           "them)" % held["withheld_sessions"]) if held["withheld_sessions"] else ""))
    clock = Clock(bhav_dates())
    cfg = make_cfg(a, sym)
    eng = Engine(sym, days, clock, cfg)
    results, split = backtest_underlying(eng, names)
    close_chk = bhav_close_check(eng, results)
    meta = {"underlying": sym, "kind": "index weekly options" if is_index(sym) else "stock monthly options",
            "from": days[0].date, "to": days[-1].date, "sessions": len(days),
            "expiries": split.get("expiries"), "split": split,
            "sessions_without_bhavcopy": len(no_bhav), "short_sessions": len(notes["short"]),
            "capital": cfg["capital"], "alloc_pct": cfg["alloc_pct"], "slip_pct": cfg["slip_pct"],
            "r": cfg["r"], "q": cfg["q"], "vix_drift": cfg["vix_drift"], "roll_on_expiry": cfg["roll"],
            "expiry_flat": cfg["expiry_flat"], "flat_by": "15:05", "entry_window": ["09:30", "14:30"],
            "iv_vix_ratio": _r(eng.ratio, 3), "vix_sessions": sum(1 for d in days if d.vix),
            "lot_sizes": sorted(set(t["lot"] for r in results.values() for t in r.get("trades", []))),
            "model_vs_bhav_close": close_chk,
            "charges_model": charges_text(), "now": nse.iso_ist(now),
            "holdout_included": held["holdout_included"], "holdout_withheld": held}
    meta["caveats"] = caveats(meta)
    return {"created_by": "india-backtest options", "made": nse.iso_ist(now), "meta": meta, "strategies": results,
            "ranking": ranking(results)}


def bhav_close_check(eng, results):
    """For the contracts the default variants traded: the model's 15:25-bar close against the
    bhavcopy close and last price (a whole-year, expiry-day-included sanity check)."""
    seen = {}
    for r in results.values():
        for t in r.get("trades", []) + r.get("skipped_size_trades", []):
            seen[(t["date"], t["expiry"], t["strike"], t["side"])] = t["expiry_day"]
    errs = {"expiry_day": [], "other_day": []}
    errs_last = {"expiry_day": [], "other_day": []}
    signed = {"expiry_day": [], "other_day": []}
    for (d, e, k, side), is_exp in seen.items():
        day = next((x for x in eng.days if x.date == d), None)
        row = day.chain.row(eng.sym, e, k, side) if day else None
        if not row:
            continue
        path = eng._path.get((d, row["expiry"], row["strike"], row["type"]))
        if not path:
            continue
        lastbar = [b for b in path if b is not None][-1][3]
        key = "expiry_day" if is_exp else "other_day"
        if row["close"] >= 1:
            errs[key].append(100.0 * abs(lastbar - row["close"]) / row["close"])
        if row["last"] >= 1:
            errs_last[key].append(100.0 * abs(lastbar - row["last"]) / row["last"])
            signed[key].append(100.0 * (lastbar - row["last"]) / row["last"])
    return dict((k, {"contracts": len(errs[k]),
                     "median_abs_err_pct_vs_close": _r(statistics.median(errs[k]), 1) if errs[k] else None,
                     "median_abs_err_pct_vs_last": _r(statistics.median(errs_last[k]), 1) if errs_last[k] else None,
                     "median_signed_err_pct_vs_last": _r(statistics.median(signed[k]), 1) if signed[k] else None})
                for k in errs)


def charges_text():
    c = nse.OPTION_CHARGES
    return ("nse.option_charges: brokerage Rs %g an executed order; STT %g %% of the sell premium (%g %% before %s); "
            "NSE txn %g %% of premium; SEBI Rs 10/crore; stamp %g %% of the buy premium; GST 18 %% on brokerage + "
            "txn + SEBI (Angel One / Zerodha / Dhan pricing pages, checked 2026-10-29)"
            % (c["brokerage_per_order"], c["stt_sell_pct"], c["stt_sell_pct_before"], c["stt_change"],
               c["exchange_pct"]["NSE"], c["stamp_buy_pct"]))


def caveats(meta):
    return [
        "Option prices are MODELLED (Black-Scholes, r=%g, q=%g, trading-time years), anchored each day to the "
        "contract's real bhavcopy open (implied vol) and clipped into its real day range. Intraday smile and "
        "skew moves, vol crush and expiry-day gamma are only approximated; see `options validate`." % (meta["r"], meta["q"]),
        "The reality clamp uses the day's real high and low, which are not time-stamped: a target below the real day "
        "high may have traded before the entry. The clamp can only make a modelled result less optimistic about "
        "prices that never traded; it cannot make the path right.",
        "The volatility anchor is the contract's first trade (bhavcopy open) against the underlying's 09:15 open; "
        "for thinly traded stock options the first trade can come much later.",
        "Fills: entry at the modelled premium + %g %% half-spread (at least a tick), stop and flat exits the same "
        "worse, targets as limits. Real spreads widen in fast markets and on expiry afternoons." % meta["slip_pct"],
        "Sizing: %g %% of a %s account in premium, whole lots (bhavcopy lot size that day), at least one lot or "
        "skipped; the account does not compound." % (meta["alloc_pct"], nse.inr(meta["capital"], 0)),
        "Sample: %d sessions, %s expiries (%s to %s); the out-of-sample part is the last third of the expiries."
        % (meta["sessions"], meta.get("expiries"), meta["from"], meta["to"]),
        "Charges: %s. Not modelled: income tax, DP charges, margin rules, freeze limits, circuit filters." % meta["charges_model"],
        "Model vs the real last traded price at the close, on the contracts traded: other days median abs %s %% "
        "(signed %s %%: negative = the model is cheaper), expiry days median abs %s %% (signed %s %%). Expiry-day "
        "afternoons are where the model is least reliable." % tuple(
            (meta.get("model_vs_bhav_close") or {}).get(k, {}).get(f) for k in ("other_day", "expiry_day")
            for f in ("median_abs_err_pct_vs_last", "median_signed_err_pct_vs_last")),
        "A backtest is not a forecast and never places an order. F&O stays blocked in india-risk by default "
        "(allow_fno false). Not financial advice.",
    ]


def ranking(results):
    def key(n):
        o = results[n]["walk_forward"]["chosen_out_of_sample"]
        v = o.get("avg_inr_net")
        return ((o.get("trades") or 0) < MIN_IS_TRADES, -(v if v is not None else -1e9), n)
    return sorted(results, key=key)


# ================================================================ reports

def fr(x, fmt="%+.0f"):
    return "-" if x is None else fmt % x


def table(doc):
    m = doc["meta"]
    head = "%-14s %6s %6s %9s %8s %10s %5s %7s %7s %8s %8s %8s %7s  %s" % (
        "strategy", "trades", "win%", "Rs/trade", "ret%", "total Rs", "PF", "DD%1L", "ruin%", "expiry", "other",
        "OOS Rs", "clamp%", "verdict")
    L = ["%s (%s), %s to %s, %d sessions, %s expiries; %s account, %g %% a trade in premium, slippage %g %%."
         % (m["underlying"], m["kind"], m["from"], m["to"], m["sessions"], m["expiries"],
            nse.inr(m["capital"], 0), m["alloc_pct"], m["slip_pct"]), head, "-" * len(head)]
    for n in doc["ranking"]:
        r = doc["strategies"][n]
        f = r["full"]
        o = r["walk_forward"]["chosen_out_of_sample"]
        ed = dict((b["bucket"], b) for b in f.get("by_expiry_day", []))
        L.append("%-14s %6d %6s %9s %8s %10s %5s %7s %7s %8s %8s %8s %7s  %s" % (
            n, f["trades"], fr(f.get("win_pct"), "%.1f"), fr(f.get("avg_inr_net")), fr(f.get("avg_ret_pct_net"), "%+.1f"),
            fr(f.get("total_inr_net")), fr(f.get("profit_factor"), "%.2f"), fr(f.get("max_dd_pct"), "%.1f"),
            fr(f.get("ruin_windows_pct"), "%.1f"), fr((ed.get("expiry_day") or {}).get("avg_inr_net")),
            fr((ed.get("other_day") or {}).get("avg_inr_net")), fr(o.get("avg_inr_net")),
            fr(f.get("clamp_changed_pct"), "%.1f"), r["verdict"]))
    L.append("(default variant over the whole period; Rs/trade, total and ret% (return on premium) are net of charges "
             "and slippage; DD%1L = max drawdown as % of the account; ruin% = share of 20-session windows where the "
             "account fell over 30 %; OOS = the variant the first 2/3 of expiries picked, on the last 1/3; clamp% = "
             "trades whose result the real day range changed.)")
    return L


def strategy_lines(name, r, show_variants):
    f = r["full"]
    wf = r["walk_forward"]
    L = ["", "%s: %s  [%s]" % (name, r["title"], r["default_variant"]), "  Rules: " + r["rules"]]
    if f["trades"]:
        L += ["  %d trades, win %.1f %%, %s a trade net (%s gross), %+.1f %% on premium net; total %s; charges %s; "
              "PF %s." % (f["trades"], f["win_pct"], nse.inr(f["avg_inr_net"], 0), nse.inr(f["avg_inr_gross"], 0),
                          f["avg_ret_pct_net"], nse.inr(f["total_inr_net"], 0), nse.inr(f["total_charges_inr"], 0),
                          fr(f.get("profit_factor"), "%.2f")),
              "  Outcomes: " + ", ".join("%s %d" % kv for kv in f["outcomes"].items() if kv[1]),
              "  Max drawdown %s (%.1f %% of the account); worst day %s %s; longest losing streak %d; 20-session "
              "windows down >30 %%: %s of %d; worst 20-session fall %.1f %%." % (
                  nse.inr(f["max_dd_inr"], 0), f["max_dd_pct"], f["worst_day"]["date"], nse.inr(f["worst_day"]["inr"], 0),
                  f["longest_losing_streak"], f["ruin_windows_fell"], f["ruin_windows"], f["worst_20d_loss_pct"]),
              "  Expiry day vs other: " + "; ".join("%s n=%d %s" % (b["bucket"], b["trades"], fr(b["avg_inr_net"]))
                                                   for b in f["by_expiry_day"]),
              "  Calls vs puts: " + "; ".join("%s n=%d %s" % (b["bucket"], b["trades"], fr(b["avg_inr_net"]))
                                             for b in f["by_side"]),
              "  By month: " + "; ".join("%s %s" % (b["bucket"][2:], fr(b["total_inr_net"])) for b in f["by_month"]),
              "  Per lot (every signal, incl. %d too dear for the allocation): %s a lot net on average." % (
                  r["skipped_size"], nse.inr(r["per_lot"].get("avg_inr_per_lot_net") or 0, 0)),
              "  Reality clamp changed %d trades (%.1f %%). IV from: %s." % (
                  f["clamp_changed"], f["clamp_changed_pct"], ", ".join("%s %d" % kv for kv in f["iv_sources"].items()))]
    else:
        L.append("  Never fired.")
    sp = wf["split"]
    o = wf["chosen_out_of_sample"]
    if sp.get("out_of_sample"):
        L.append("  Walk-forward: %d expiries in sample picked [%s]; on the last %d expiries (%s to %s) it made %s "
                 "a trade over %d trades (default: %s)." % (
                     sp["in_sample_expiries"], wf["chosen"], sp["out_of_sample_expiries"], sp["out_of_sample"][0],
                     sp["out_of_sample"][1], fr(o.get("avg_inr_net")), o["trades"],
                     fr(wf["default_out_of_sample"].get("avg_inr_net"))))
    if show_variants:
        L.append("  %-44s %6s %6s %9s %8s %7s %8s %8s" % ("variant", "trades", "win%", "Rs/trade", "ret%", "DD%",
                                                         "IS Rs", "OOS Rs"))
        for v in r["variants"]:
            vf = v["full"]
            L.append("  %-44s %6d %6s %9s %8s %7s %8s %8s" % (
                v["label"], vf["trades"], fr(vf.get("win_pct"), "%.1f"), fr(vf.get("avg_inr_net")),
                fr(vf.get("avg_ret_pct_net"), "%+.1f"), fr(vf.get("max_dd_pct"), "%.1f"),
                fr(v["in_sample"]["avg_inr_net"]), fr(v["out_of_sample"]["avg_inr_net"])))
    if r["skipped"]:
        L.append("  Not taken: " + "; ".join("%s x%d" % kv for kv in sorted(r["skipped"].items(), key=lambda kv: -kv[1])))
    L.append("  Verdict: " + r["verdict"])
    return L


def markdown(doc):
    m = doc["meta"]
    L = ["# Options backtest: %s" % m["underlying"], "",
         "%s, %s to %s (%d sessions, %s expiries). Made %s. **Modelled option prices, anchored to real bhavcopy "
         "prices; see the validation file. Never places an order. Not financial advice.**" % (
             m["kind"], m["from"], m["to"], m["sessions"], m["expiries"], doc["made"]), "",
         "| strategy | trades | win % | Rs/trade net | ret % on premium | total Rs | PF | max DD % of Rs 1L | ruin windows % | expiry-day Rs/trade | other-day Rs/trade | OOS Rs/trade | clamp changed % | verdict |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for n in doc["ranking"]:
        r = doc["strategies"][n]
        f = r["full"]
        o = r["walk_forward"]["chosen_out_of_sample"]
        ed = dict((b["bucket"], b) for b in f.get("by_expiry_day", []))
        L.append("| %s | %d | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            n, f["trades"], fr(f.get("win_pct"), "%.1f"), fr(f.get("avg_inr_net")), fr(f.get("avg_ret_pct_net"), "%+.1f"),
            fr(f.get("total_inr_net")), fr(f.get("profit_factor"), "%.2f"), fr(f.get("max_dd_pct"), "%.1f"),
            fr(f.get("ruin_windows_pct"), "%.1f"), fr((ed.get("expiry_day") or {}).get("avg_inr_net")),
            fr((ed.get("other_day") or {}).get("avg_inr_net")), fr(o.get("avg_inr_net")),
            fr(f.get("clamp_changed_pct"), "%.1f"), r["verdict"]))
    L += ["", "## Rules", ""] + ["- **%s**: %s" % (n, doc["strategies"][n]["rules"]) for n in doc["ranking"]]
    L += ["", "## Model vs the bhavcopy close (traded contracts)", "",
          "```", json.dumps(m["model_vs_bhav_close"], indent=1), "```"]
    L += ["", "## Caveats", ""] + ["- " + c for c in m["caveats"]]
    return "\n".join(L) + "\n"


def out_dir():
    return os.path.join(nse.trader_dir(), "backtest")


def write_doc(doc, stem):
    path = os.path.join(out_dir(), stem + ".json")
    nse.write_json(path, doc)
    with open(os.path.join(out_dir(), stem + ".md"), "w", encoding="utf-8") as fh:
        fh.write(markdown(doc))
    return path


# ================================================================ dashboard JSON

def dash(docs, validation, made):
    strat_rows, eq_rows, bucket_rows = [], [], []
    for doc in docs:
        sym = doc["meta"]["underlying"]
        for n in doc["ranking"]:
            r = doc["strategies"][n]
            f = r["full"]
            ed = dict((b["bucket"], b) for b in f.get("by_expiry_day", []))
            strat_rows.append({
                "id": "%s-%s" % (sym, n), "underlying": sym, "strategy": n, "title": r["title"], "rules": r["rules"],
                "variant": r["default_variant"], "trades": f["trades"], "win_pct": f.get("win_pct"),
                "avg_ret_pct_net": f.get("avg_ret_pct_net"), "avg_inr_net": f.get("avg_inr_net"),
                "total_inr_net": f.get("total_inr_net"), "profit_factor": f.get("profit_factor"),
                "max_dd_inr": f.get("max_dd_inr"), "max_dd_pct": f.get("max_dd_pct"),
                "ruin_windows_pct": f.get("ruin_windows_pct"),
                "expiry_day_avg_inr": (ed.get("expiry_day") or {}).get("avg_inr_net"),
                "other_day_avg_inr": (ed.get("other_day") or {}).get("avg_inr_net"),
                "oos_avg_inr_net": r["walk_forward"]["chosen_out_of_sample"].get("avg_inr_net"),
                "oos_trades": r["walk_forward"]["chosen_out_of_sample"].get("trades"),
                "oos_variant": r["walk_forward"]["chosen"],
                "clamp_changed_pct": f.get("clamp_changed_pct"), "verdict": r["verdict"]})
            for c in f.get("equity_curve", []):
                eq_rows.append({"underlying": sym, "strategy": n, "date": c["date"], "cum_inr_net": c["cum_inr_net"]})
            for bt_type, key in (("expiry_day", "by_expiry_day"), ("side", "by_side"), ("month", "by_month")):
                for b in f.get(key, []):
                    bucket_rows.append({"underlying": sym, "strategy": n, "bucket_type": bt_type, "bucket": b["bucket"],
                                        "trades": b["trades"], "win_pct": b["win_pct"], "avg_inr_net": b["avg_inr_net"]})
    val_rows = []
    for c in (validation or {}).get("contracts", []):
        val_rows.append({"contract": c["contract"], "sessions": c["sessions"], "bars": c["bars"],
                         "median_abs_err_pct": c["median_abs_err_pct"], "hit_agreement_pct": c["hit_agreement_pct"],
                         "notes": c.get("notes", "")})
    if validation and validation.get("summary"):
        s = validation["summary"]
        val_rows.append({"contract": "ALL", "sessions": s.get("contract_days"), "bars": s.get("bars"),
                         "median_abs_err_pct": s.get("median_abs_err_pct"),
                         "hit_agreement_pct": s.get("hit_agreement_pct"), "notes": "all validated contracts pooled"})
    periods = ["%s %s to %s" % (d["meta"]["underlying"], d["meta"]["from"], d["meta"]["to"]) for d in docs]
    meta = {"made": made, "period": "; ".join(periods),
            "expiries": "; ".join("%s %s" % (d["meta"]["underlying"], d["meta"]["expiries"]) for d in docs),
            "data_sources": ("NSE F&O bhavcopy (UDiFF, nsearchives.nseindia.com, daily OHLC + lot size of each "
                             "contract); Angel One SmartAPI 5-minute bars of the underlying and India VIX; Angel's "
                             "real 5-minute bars of live contracts for validation"),
            "assumptions": ("Black-Scholes, r=%g, q=%g, trading-time years (252 x 375 min) to 15:30 on expiry day; "
                            "IV implied daily from the contract's bhavcopy open vs the underlying's 09:15 open; prices "
                            "clipped into the contract's real day range; Rs 1,00,000 account, 20 %% a trade in premium, "
                            "whole lots; half-spread 0.5 %% (Nifty) / 1.5 %% (stocks) a side"
                            % (R_DEFAULT, Q_DEFAULT)),
            "charges_model": charges_text(),
            "caveats": (docs[0]["meta"]["caveats"] if docs else []) + [
                "Expired contracts have no intraday history on Angel: validation covers only contracts live on %s, "
                "none on its own expiry day." % made[:10]]}
    return {"made": made, "opt_strategies": strat_rows, "opt_equity": eq_rows, "opt_by_bucket": bucket_rows,
            "opt_validation": val_rows, "opt_meta": [meta]}


# ================================================================ validation

def moneyness_bucket(S, K, typ):
    m = (S / K - 1.0) * 100.0 * (1 if typ == "CE" else -1)
    if m > 2:
        return "ITM >2%"
    if m > 0.5:
        return "ITM 0.5-2%"
    if m >= -0.5:
        return "ATM +-0.5%"
    if m >= -2:
        return "OTM 0.5-2%"
    return "OTM >2%"


def dte_bucket(n):
    if n <= 0:
        return "0 (expiry day)"
    if n <= 2:
        return "1-2"
    if n <= 5:
        return "3-5"
    if n <= 10:
        return "6-10"
    return "11+"


TIME_BUCKETS = (("09:15-10:00", 0, 9), ("10:00-12:00", 9, 33), ("12:00-14:00", 33, 57), ("14:00-15:30", 57, 75))


def time_bucket(k):
    for label, a, b in TIME_BUCKETS:
        if a <= k < b:
            return label
    return TIME_BUCKETS[-1][0]


def outcome_on(bars, k0, last, stop_pct=VALID_STOP, target_pct=VALID_TARGET):
    """Entry at bars[k0] close; premium stop / target, no costs -> (outcome, return %) with outcome
    'stop' | 'target' | 'flat'. bars: [(o,h,l,c)|None]."""
    if bars[k0] is None:
        return None, None
    e = bars[k0][3]
    st, tg = e * (1 - stop_pct / 100.0), e * (1 + target_pct / 100.0)
    for i in range(k0 + 1, last + 1):
        b = bars[i]
        if b is None:
            continue
        if b[0] <= st:
            return "stop", 100.0 * (b[0] / e - 1)
        if b[2] <= st:
            return "stop", -stop_pct
        if b[1] >= tg:
            return "target", target_pct
    c = [b for b in bars[k0:last + 1] if b is not None][-1][3]
    return "flat", 100.0 * (c / e - 1)


def validate_contract(info, real_bars, sessions_by_date, vix_by_date, clock, r=R_DEFAULT, q=Q_DEFAULT, base=None):
    """One live contract: model vs Angel's real 5-minute bars on every session both have."""
    sym, expiry, K, typ = info["name"], info["expiry"], float(info["strike"]), info["type"]
    ed = as_date(expiry)
    days = bt.build_sessions(info["symbol"], real_bars, None, None)[0] if real_bars else []
    res = {"contract": "%s %s %g %s" % (sym, expiry, K, typ), "symbol": info["symbol"], "token": info["token"],
           "sessions": 0, "bars": 0, "errs": [], "errs_clamped": [], "errs_vix": [], "trials": 0, "agree": 0,
           "agree_vix": 0, "by_m": {}, "by_dte": {}, "by_time": {}, "skipped": {}, "signed": [],
           "ret_real": [], "ret_model": []}
    # short real sessions (illiquid contracts) are kept: build them by hand
    real = {}
    for b in real_bars:
        mins = b["t"].hour * 60 + b["t"].minute - bt.OPEN_MIN
        if mins < 0 or mins % 5 or mins >= bt.SLOTS * 5:
            continue
        real.setdefault(b["t"].date().isoformat(), {})[mins // 5] = b
    del days
    for d in sorted(real):
        sess = sessions_by_date.get(d)
        ch = load_chain(d, base)
        row = ch.row(sym, expiry, K, typ) if ch else None
        if sess is None or row is None:
            res["skipped"]["no underlying bars or bhavcopy row"] = res["skipped"].get(
                "no underlying bars or bhavcopy row", 0) + 1
            continue
        dd = as_date(d)
        vix = vix_by_date.get(d)
        m0 = Model(clock, r, q, False)
        sig, src = m0.anchor(row, sess, dd, ed, vix[0]["o"] if vix and vix[0] else None, None)
        if sig is None:
            res["skipped"]["no implied vol"] = res["skipped"].get("no implied vol", 0) + 1
            continue
        path = m0.path(sess, row, dd, ed, sig, None)
        pathv = Model(clock, r, q, True).path(sess, row, dd, ed, sig, vix) if vix else path
        cl = clamp_path(path, row["low"], row["high"])
        res["sessions"] += 1
        n = clock.dte(dd, ed)
        rb = real[d]
        for k, b in rb.items():
            if path[k] is None or b["c"] <= 0 or not b.get("v"):
                continue
            mdl, real_c = path[k][3], b["c"]
            e = 100.0 * abs(mdl - real_c) / real_c
            res["errs"].append(e)
            res["signed"].append(100.0 * (mdl - real_c) / real_c)
            res["errs_clamped"].append(100.0 * abs(cl[k][3] - real_c) / real_c)
            res["errs_vix"].append(100.0 * abs(pathv[k][3] - real_c) / real_c)
            res["by_m"].setdefault(moneyness_bucket(sess.bars[k]["c"], K, typ), []).append(e)
            res["by_dte"].setdefault(dte_bucket(n), []).append(e)
            res["by_time"].setdefault(time_bucket(k), []).append(e)
            res["bars"] += 1
        rpath = [((rb[k]["o"], rb[k]["h"], rb[k]["l"], rb[k]["c"]) if k in rb else None) for k in range(bt.SLOTS)]
        last = 69 if expiry != d else 68
        for k0 in VALID_ENTRIES:
            if rpath[k0] is None or cl[k0] is None:
                continue
            ro, rr = outcome_on(rpath, k0, last)
            mo, mr = outcome_on(cl, k0, last)
            mv, _ = outcome_on(clamp_path(pathv, row["low"], row["high"]), k0, last)
            res["trials"] += 1
            res["agree"] += ro == mo
            res["agree_vix"] += ro == mv
            res["ret_real"].append(rr)
            res["ret_model"].append(mr)
    return res


def summarize_contract(r):
    med = lambda xs: _r(statistics.median(xs), 1) if xs else None  # noqa: E731
    return {"contract": r["contract"], "symbol": r["symbol"], "sessions": r["sessions"], "bars": r["bars"],
            "median_abs_err_pct": med(r["errs"]), "median_abs_err_pct_clamped": med(r["errs_clamped"]),
            "median_abs_err_pct_vix_drift": med(r["errs_vix"]),
            "hit_trials": r["trials"],
            "hit_agreement_pct": _r(100.0 * r["agree"] / r["trials"], 1) if r["trials"] else None,
            "hit_agreement_pct_vix_drift": _r(100.0 * r["agree_vix"] / r["trials"], 1) if r["trials"] else None,
            "notes": "; ".join("%s x%d" % kv for kv in r["skipped"].items())}


def summarize_validation(raw):
    med = lambda xs: _r(statistics.median(xs), 1) if xs else None  # noqa: E731
    allv = lambda key: [e for r in raw for e in r[key]]  # noqa: E731
    by_m, by_d, by_t = {}, {}, {}
    for r in raw:
        for k, v in r["by_time"].items():
            by_t.setdefault(k, []).extend(v)
        for k, v in r["by_m"].items():
            by_m.setdefault(k, []).extend(v)
        for k, v in r["by_dte"].items():
            by_d.setdefault(k, []).extend(v)
    trials = sum(r["trials"] for r in raw)
    order_m = ["OTM >2%", "OTM 0.5-2%", "ATM +-0.5%", "ITM 0.5-2%", "ITM >2%"]
    order_d = ["0 (expiry day)", "1-2", "3-5", "6-10", "11+"]
    out = {"contracts": len(raw), "contract_days": sum(r["sessions"] for r in raw), "bars": sum(r["bars"] for r in raw),
           "median_abs_err_pct": med(allv("errs")), "median_abs_err_pct_clamped": med(allv("errs_clamped")),
           "median_abs_err_pct_vix_drift": med(allv("errs_vix")),
           "median_signed_err_pct": med(allv("signed")),
           "mean_trial_ret_pct_real": _r(sum(allv("ret_real")) / len(allv("ret_real")), 2) if allv("ret_real") else None,
           "mean_trial_ret_pct_model": _r(sum(allv("ret_model")) / len(allv("ret_model")), 2) if allv("ret_model") else None,
           "p90_abs_err_pct": _r(sorted(allv("errs"))[int(0.9 * (len(allv("errs")) - 1))], 1) if allv("errs") else None,
           "hit_trials": trials,
           "hit_agreement_pct": _r(100.0 * sum(r["agree"] for r in raw) / trials, 1) if trials else None,
           "hit_agreement_pct_vix_drift": _r(100.0 * sum(r["agree_vix"] for r in raw) / trials, 1) if trials else None,
           "by_moneyness": [{"bucket": b, "bars": len(by_m[b]), "median_abs_err_pct": med(by_m[b])}
                            for b in order_m if b in by_m],
           "by_dte": [{"bucket": b, "bars": len(by_d[b]), "median_abs_err_pct": med(by_d[b])}
                      for b in order_d if b in by_d],
           "by_time_of_day": [{"bucket": b, "bars": len(by_t[b]), "median_abs_err_pct": med(by_t[b])}
                              for b, _, _ in TIME_BUCKETS if b in by_t]}
    return out


def pick_validation_contracts(opts, sym, spot, today):
    rows = [o for o in opts if o["name"] == sym and o["expiry"] >= today]
    exps = sorted(set(o["expiry"] for o in rows))
    if not exps:
        return []
    if is_index(sym):
        months = {}
        for e in exps:
            months.setdefault(e[:7], []).append(e)
        chosen = exps[:2] + [max(months[exps[0][:7]])]
        width = 2
    else:
        chosen = exps[:1]
        width = 1
    out = []
    for e in sorted(set(chosen)):
        ks = sorted(set(o["strike"] for o in rows if o["expiry"] == e))
        i = min(range(len(ks)), key=lambda j: abs(ks[j] - spot))
        keep = set(ks[max(0, i - width): i + width + 1])
        out += [o for o in rows if o["expiry"] == e and o["strike"] in keep]
    return out


# ================================================================ Angel (read-only)

def angel_client():
    import angel
    bt.REQUEST_GAP = max(bt.REQUEST_GAP, ANGEL_GAP)
    bt.BACKOFF = ANGEL_BACKOFF
    client = angel.Angel.from_env(transport=bt.safe_get, public=angel.public_ip() or "", local=angel._local_ip())
    bt.angel_call(client.login, "login")
    return angel, client


def fetch_underlying(sym, now, days, client, angel):
    if sym in INDEX_TOKENS:
        scrip = {sym: {"token": INDEX_TOKENS[sym]}}
    else:
        scrip = angel.scrip_master()
    bars, daily, info = bt.load_angel(sym, now, days, client, scrip)
    return len(bars), info


def option_store(symbol):
    return os.path.join(nse.trader_dir(), "cache", "bars", "angel-nfo", "%s-5m.json.gz" % symbol)


def fetch_option_bars(o, now, client):
    path = option_store(o["symbol"])
    doc = bt.read_store(path)
    if doc and bt.fresh(doc, now):
        return doc["bars"], False
    last = bt.last_complete_session(now)
    start = datetime.datetime.combine(last - datetime.timedelta(days=85), nse.OPEN)
    end = datetime.datetime.combine(last, nse.CLOSE)
    bars = bt.angel_call(lambda: client.candles(o["token"], start, end, minutes=5, exchange="NFO"),
                         o["symbol"])
    bars = bt.complete_only(nse.on_grid(bars, 5), now, "5m")
    bars = bt.merge(doc["bars"] if doc else [], bars)
    bt.write_store(path, {"symbol": o["symbol"], "token": o["token"], "source": "angel", "exchange": "NFO",
                          "interval": "5m", "fetched": nse.iso_ist(now)}, bars)
    return bars, True


# ================================================================ commands

def parse_syms(text):
    if not text or text.lower() == "all":
        return list(DEFAULT_SYMS)
    return [nse.clean_symbol(s) for s in text.split(",") if s.strip()]


def get_now(a):
    return nse.parse_now(a.now) if getattr(a, "now", None) else nse.now_ist().replace(microsecond=0)


def cmd_fetch(a):
    now = get_now(a)
    syms = parse_syms(a.underlying)
    keep = sorted(set(syms) | set(DEFAULT_SYMS))
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731
    if not a.no_bhav:
        cal = int(math.ceil(a.days * 1.45)) + 10
        last = bt.last_complete_session(now)
        dates = [last - datetime.timedelta(days=i) for i in range(cal)][::-1]
        log("bhavcopy: %s to %s, keeping %s (slim rows only)" % (dates[0], dates[-1], ", ".join(keep)))
        res = fetch_bhav(dates, keep, now, log=log)
        print("bhavcopy: %(fetched)d fetched, %(cached)d already cached, %(missing)d holidays/missing, "
              "%(errors)d errors" % res)
    if not a.no_angel:
        angel, client = angel_client()
        try:
            for sym in syms + (["INDIAVIX"] if not a.no_vix else []):
                try:
                    n, info = fetch_underlying(sym, now, a.days, client, angel)
                    print("Angel %s: %d five-minute bars cached (%d requests, covers %s)" % (
                        sym, n, info["requests"], info["covered"]))
                except nse.NSEError as e:
                    print("Angel %s: %s" % (sym, angel.scrub(str(e))))
        finally:
            try:
                client.logout()
            except nse.NSEError:
                pass
    return 0


def cmd_run(a, names=None, quiet=False):
    now = get_now(a)
    names = names or parse_strats(a.strategy)
    docs = []
    for sym in parse_syms(a.underlying):
        doc = run_underlying(sym, a, names, now)
        docs.append(doc)
        if not a.no_write:
            path = write_doc(doc, "%s-options-%s" % (doc["made"][:10], sym))
            doc["_path"] = path
        if a.json:
            print(json.dumps(doc, indent=1, default=str))
            continue
        if quiet:
            continue
        L = table(doc)
        for n in names:
            L += strategy_lines(n, doc["strategies"][n], a.variants)
        L += ["", "Caveats:"] + ["  - " + c for c in doc["meta"]["caveats"]]
        if doc.get("_path"):
            L.append("Wrote %s (and the .md beside it)." % doc["_path"])
        print("\n".join(L))
    return docs


def cmd_validate(a):
    now = get_now(a)
    import angel as angel_mod
    syms = parse_syms(a.underlying)
    opts = angel_mod.nfo_options(offline=a.scrip_file, names=syms)
    clock = Clock(bhav_dates())
    vdoc = angel_store("INDIAVIX")
    vix = dict((s.date, s.bars) for s in bt.build_sessions("INDIAVIX", vdoc["bars"], None, now)[0]) if vdoc else {}
    client = angel = None
    raw = []
    today = now.date().isoformat()
    for sym in syms:
        ss, _ = load_sessions(sym, now)
        by_date = dict((s.date, s) for s in ss)
        spot = [b for b in ss[-1].bars if b][-1]["c"]
        picks = pick_validation_contracts(opts, sym, spot, today)
        print("validate %s: %d live contracts around %.2f" % (sym, len(picks), spot), file=sys.stderr)
        for o in picks:
            path = option_store(o["symbol"])
            doc = bt.read_store(path)
            if not (doc and bt.fresh(doc, now)):
                if a.offline:
                    if not doc:
                        continue
                else:
                    if client is None:
                        angel, client = angel_client()
                    try:
                        fetch_option_bars(o, now, client)
                    except nse.NSEError as e:
                        print("  %s: %s" % (o["symbol"], angel.scrub(str(e))), file=sys.stderr)
                        continue
                doc = bt.read_store(path)
            r = validate_contract(o, doc["bars"], by_date, vix, clock, a.r or R_DEFAULT,
                                  a.q if a.q is not None else Q_DEFAULT)
            raw.append(r)
    if client is not None:
        try:
            client.logout()
        except nse.NSEError:
            pass
    out = {"created_by": "india-backtest options validate", "made": nse.iso_ist(now),
           "method": ("For each live contract: every session where Angel has its real 5-minute bars and the bhavcopy "
                      "has its row, the model (IV from that day's bhavcopy open vs the underlying's 09:15 open, "
                      "Black-Scholes on the underlying's bar closes) is compared with the real bar close (bars with "
                      "volume only). Hit agreement: entries at the bars closing 09:35, 11:00 and 13:00, stop -25 %, "
                      "target +50 % on premium, flat 15:05; the modelled (clamped) path's outcome vs the real one."),
           "contracts": [summarize_contract(r) for r in raw], "summary": summarize_validation(raw)}
    for sym in syms:
        sub = [r for r in raw if r["contract"].startswith(sym + " ")]
        if sub:
            out.setdefault("by_underlying", {})[sym] = summarize_validation(sub)
    path = os.path.join(out_dir(), "%s-options-validation.json" % today)
    nse.write_json(path, out)
    L = validation_lines(out)
    with open(path[:-5] + ".md", "w", encoding="utf-8") as fh:
        fh.write("# Options model validation\n\n" + "\n".join("    " + x for x in L) + "\n")
    print("\n".join(L))
    print("Wrote %s" % path)
    return out


def validation_lines(v):
    s = v["summary"]
    L = ["Model vs Angel's real 5-minute option bars: %d contracts, %d contract-days, %d bars." % (
        s["contracts"], s["contract_days"], s["bars"]),
        "  median abs error %s %% (clamped %s %%, with VIX drift %s %%); 90th percentile %s %%." % (
            s["median_abs_err_pct"], s["median_abs_err_pct_clamped"], s["median_abs_err_pct_vix_drift"],
            s["p90_abs_err_pct"]),
        "  median signed error %s %% (model minus real: negative = the model is cheaper than the market)." % (
            s["median_signed_err_pct"]),
        "  stop/target outcome agreement %s %% of %d trials (VIX drift %s %%); average trial return before costs: "
        "real %s %%, model %s %% (-25 %% stop, +50 %% target, entries 09:35 / 11:00 / 13:00)." % (
            s["hit_agreement_pct"], s["hit_trials"], s["hit_agreement_pct_vix_drift"],
            s["mean_trial_ret_pct_real"], s["mean_trial_ret_pct_model"]),
        "  by moneyness: " + "; ".join("%s %s %% (%d)" % (b["bucket"], b["median_abs_err_pct"], b["bars"])
                                       for b in s["by_moneyness"]),
        "  by trading days to expiry: " + "; ".join("%s %s %% (%d)" % (b["bucket"], b["median_abs_err_pct"], b["bars"])
                                                    for b in s["by_dte"]),
        "  by time of day (the vol is anchored at the open): " + "; ".join(
            "%s %s %%" % (b["bucket"], b["median_abs_err_pct"]) for b in s["by_time_of_day"])]
    for sym, u in sorted((v.get("by_underlying") or {}).items()):
        L.append("  %s: median %s %% (signed %s %%), agreement %s %%, trial return real %s %% vs model %s %% (%d bars)"
                 % (sym, u["median_abs_err_pct"], u["median_signed_err_pct"], u["hit_agreement_pct"],
                    u["mean_trial_ret_pct_real"], u["mean_trial_ret_pct_model"], u["bars"]))
    L.append("  %-30s %4s %5s %7s %7s %7s" % ("contract", "days", "bars", "err%", "clamp%", "agree%"))
    for c in v["contracts"]:
        L.append("  %-30s %4d %5d %7s %7s %7s" % (c["contract"], c["sessions"], c["bars"], c["median_abs_err_pct"],
                                                  c["median_abs_err_pct_clamped"], c["hit_agreement_pct"]))
    return L


def cmd_compare(a):
    a.strategy = a.strategy or "all"
    docs = cmd_run(a, parse_strats(a.strategy), quiet=True)
    for doc in docs:
        print("\n".join(table(doc)))
        print("")
    made = docs[0]["made"] if docs else nse.iso_ist(get_now(a))
    vpath = os.path.join(out_dir(), "%s-options-validation.json" % made[:10])
    validation = nse.read_json(vpath)
    if validation is None:
        cands = sorted(f for f in os.listdir(out_dir()) if f.endswith("-options-validation.json")) \
            if os.path.isdir(out_dir()) else []
        validation = nse.read_json(os.path.join(out_dir(), cands[-1])) if cands else None
    d = dash([dict((k, v) for k, v in doc.items() if k != "_path") for doc in docs], validation, made)
    if not a.no_write:
        path = os.path.join(out_dir(), "%s-options-dash.json" % made[:10])
        nse.write_json(path, d)
        print("Wrote %s" % path)
    return 0


def cmd_list(a):
    print("Options strategies (buy the ATM option on a signal from the underlying's 5-minute bars):")
    for n in ORDER:
        s = STRATS[n]
        print("\n%s: %s\n  %s\n  %d variants: %s" % (n, s["title"], s["rules"], len(s["variants"]),
                                                    "; ".join(variant_label(n, v) for v in s["variants"])))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="backtest.py options",
                                 description="Options backtest (modelled, real-price-anchored). Never places an order.")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list")
    p = sub.add_parser("fetch", help="NSE F&O bhavcopy (slimmed) + Angel 5-minute underlying and India VIX")
    p.add_argument("--underlying", default="NIFTY")
    p.add_argument("--days", type=int, default=260)
    p.add_argument("--no-bhav", action="store_true")
    p.add_argument("--no-angel", action="store_true")
    p.add_argument("--no-vix", action="store_true")
    p.add_argument("--now")

    def common(p, default_syms):
        p.add_argument("--underlying", default=default_syms, help="NIFTY | SBIN | RELIANCE | HDFCBANK | A,B | all")
        p.add_argument("--strategy", default=None, help="opt_orb[,opt_orb30,...] or all")
        p.add_argument("--variants", action="store_true")
        p.add_argument("--days", type=int, help="the last N sessions")
        p.add_argument("--from", dest="date_from")
        p.add_argument("--to", dest="date_to")
        p.add_argument("--capital", type=float, default=100000)
        p.add_argument("--alloc-pct", type=float, default=20)
        p.add_argument("--slip-pct", type=float, help="half-spread a side, %% of premium (0.5 Nifty, 1.5 stocks)")
        p.add_argument("--r", type=float, default=R_DEFAULT)
        p.add_argument("--q", type=float, default=Q_DEFAULT)
        p.add_argument("--vix-drift", action="store_true", help="move the implied vol intraday with India VIX")
        p.add_argument("--roll-on-expiry", action="store_true", help="on expiry day trade the next expiry")
        p.add_argument("--expiry-flat", default="15:00", help="flat-by on expiry day (default 15:00)")
        p.add_argument("--json", action="store_true")
        p.add_argument("--no-write", action="store_true")
        p.add_argument("--now")
        p.add_argument("--allow-holdout", action="store_true",
                       help="include sessions inside india-research's locked holdouts (withheld by default; "
                            "recorded as holdout_included)")
    p = sub.add_parser("run")
    common(p, "NIFTY")
    p = sub.add_parser("compare")
    common(p, "all")
    p = sub.add_parser("validate", help="model vs Angel's real 5-minute bars of live contracts")
    p.add_argument("--underlying", default="all")
    p.add_argument("--scrip-file", help="a saved OpenAPIScripMaster.json (else downloaded, 35 MB)")
    p.add_argument("--offline", action="store_true", help="only contracts already cached")
    p.add_argument("--r", type=float, default=R_DEFAULT)
    p.add_argument("--q", type=float, default=Q_DEFAULT)
    p.add_argument("--now")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        if a.cmd == "run":
            a.strategy = a.strategy or "opt_orb"
            cmd_run(a)
            return 0
        return {"list": cmd_list, "fetch": cmd_fetch, "validate": lambda x: (cmd_validate(x), 0)[1],
                "compare": cmd_compare}[a.cmd](a)
    except nse.NSEError as e:
        print("backtest.py options: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
