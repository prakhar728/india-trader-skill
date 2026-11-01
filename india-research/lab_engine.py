#!/usr/bin/env python3
"""Lab engine for india-research. Standard library only. Never places an order.

Runs research strategies (plain Python files in trader/research/lab/<id>.py) on NSE
5-minute bars, long AND short, with india-backtest's rules imported from
india-backtest/backtest.py (sessions, entry window, flat-by, ticks, slippage, sizing,
charges, metrics). It adds three things backtest.py does not have:

1. Short selling for intraday equity (series EQ): sell first, buy to cover by flat-by.
   The stop sits above the entry and rounds UP to the tick (further away, so the risk
   shown is honest), the target sits below and rounds UP too (reachable). The fill is
   the decision bar's close minus slippage. Exits bar by bar after the entry: a bar
   that OPENS at or above the stop covers at its open + slippage (gap), a bar whose
   high touches the stop covers at the stop + slippage, else a bar whose low reaches
   the target covers at the target; a bar touching both is the stop; still open at
   flat-by: the last bar's close + slippage. Charges: nse.charges(buy_value, sell_value)
   with the SELL leg = the entry (STT 0.025 % on it) and the BUY leg = the cover
   (stamp duty on it), whatever the order of the two legs.

2. Strict causality. The engine streams the day: for every bar k inside the entry
   window it calls signals(day, ctx) with ONLY the bars that have closed (09:15 ...
   bar k), and the same cut for sector peers, the index and India VIX. A strategy can
   only open a trade at the close of that last visible bar. Reading a bar that has
   not closed yet raises LookAheadError (an AssertionError) and fails the run even if
   the strategy catches it; so does a trade dated to an earlier bar at which the
   strategy, when asked then, did not want it (a back-dated decision).

3. Locked data splits. A dataset (source + universe) is cut once, the first time it is
   used, into explore (oldest ~60 % of sessions), validate (next ~20 %) and holdout
   (latest ~20 %), saved in trader/research/splits.json and never changed. The loader
   drops every bar on or after the split's cutoff BEFORE sessions are built: an
   explore run holds no validate or holdout bars, a validate run holds no holdout
   bars. The holdout loads only with a HoldoutTicket, which exists only after
   research.py has written a permanent holdout claim for that experiment. A new split
   also records `exposed_before`: earlier trader/backtest/ artefacts and other splits'
   explore/validate windows that already looked at its validate / holdout dates.

4. A portfolio capital cap (default ON). Every session streams bar by bar across all
   its stocks, so trades arrive in decision-time order (same bar: symbol order). A trade
   is taken only if the open positions plus it stay within cfg "capital" (Rs 1,00,000 by
   default, at entry value) and cfg "max_positions" (5); otherwise it is skipped and
   counted (info["capital_skips"]). Per-trade R, rupees, the daily-sum t-stat and the
   benchmark gate are then computed on the trades an account of that size could take.
   cfg "capital_cap": False (research.py --no-capital-cap) gives per-trade stats only.

THE STRATEGY FILE (trader/research/lab/<id>.py)

    META = {
        "family": "short-breakout",        # free text, groups experiments in the report
        "kind": "intraday",                # intraday (swing: not run by this engine)
        "side": "short",                   # long | short | both (documentation)
        "rationale": "why it should work: the mechanism, not 'it backtested well'",
        "rules": "exact rules in words",
        "default": {"or_minutes": 15, "r": 2.0},
        "grid": {"or_minutes": [15, 30], "r": [1.5, 2.0]},   # every combination counts as a trial
        "max_trades_per_day": 1,           # per stock (default 1)
    }

    def signals(day, ctx):
        # return the trades to open AT THE CLOSE of day.bars[-1] (possibly none)
        return [{"side": "short", "entry_time": day.time, "entry": day.bars[-1]["c"],
                 "stop": 1180.0, "target": 1150.0, "reason": "closed under the range low"}]

day (one stock, one session, as of the close of bar day.k):
    day.symbol, day.date ("YYYY-MM-DD"), day.tick, day.sector
    day.bars      bars[0] = the 09:15 bar ... bars[day.k] = the last CLOSED bar. Each is a
                  read-only {"o","h","l","c","v"} or None (Yahoo drops bars). len = k + 1.
                  Negative indexes count back from the last closed bar.
    day.k, day.time   index of the last closed bar, and its END time "HH:MM" (= entry_time)
    day.prev      {"date","high","low","close"} of the previous session, or None
    day.daily     earlier daily bars [{"date","o","h","l","c","v"}], oldest first (up to 60)
    day.avg_value20   average traded value (rupees) of the 20 sessions before today, or None
    day.state     a dict kept between the calls of this stock-day (calls come in time
                  order; bars while a trade is open are skipped)
ctx:
    ctx.params    this variant's parameters (META default updated with the grid point)
    ctx.time      = day.time; ctx.window {"first_entry","last_entry","flat_by"}
    ctx.index     NIFTY 50 bars of the day (same cut) or None; ctx.index_prev_close
    ctx.vix       India VIX bars of the day (same cut) or None; ctx.vix_prev_close
    ctx.peers     {SYMBOL: peer} for the stock's sector peers in the universe (NSE Nifty 500
                  Industry), each with .bars (same cut), .prev, .tick, .avg_value20 (average
                  traded value of the 20 sessions before today, rupees)
    ctx.events    recorded events (india-research events.py) for this stock detected in the
                  last 7 days up to ctx.time
Helpers: vwap(bars), atr(bars, n), end_time(k), slot_of(hhmm).

A trade: side long|short; entry_time = day.time; entry = the close of the last bar;
stop and target on the right sides of the entry. The engine sizes it (india-budget's
rules), rounds stop and target to the tick, adds slippage, simulates it to flat-by and
charges it.
"""

import bisect
import fcntl
import datetime
import glob
import hashlib
import importlib.util
import itertools
import json
import math
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "india-common"))
sys.path.insert(0, os.path.join(HERE, "..", "india-backtest"))
import nse  # noqa: E402
import backtest as bt  # noqa: E402  (india-backtest: imported, never edited)

INTERVAL = bt.INTERVAL
OPEN_MIN = bt.OPEN_MIN
FRACTIONS = (0.6, 0.2, 0.2)
MIN_SPLIT_SESSIONS = 20
MIN_SELECT_TRADES = 10
EVENT_LOOKBACK_DAYS = 7
SIDES = ("long", "short")
MAX_POSITIONS = 5               # portfolio cap: concurrent intraday positions (config max_positions)


class LabError(nse.NSEError):
    pass


class StrategyError(LabError):
    pass


class HoldoutError(LabError):
    pass


class LookAheadError(AssertionError):
    """A strategy read a bar that had not closed, or back-dated a decision."""


# ---------------------------------------------------------------- paths

def research_dir(base=None):
    return os.path.join(nse.trader_dir(base), "research")


def lab_dir(base=None):
    return os.path.join(research_dir(base), "lab")


def splits_path(base=None):
    return os.path.join(research_dir(base), "splits.json")


def events_dir(base=None):
    return os.path.join(research_dir(base), "events")


# ---------------------------------------------------------------- small helpers

def end_time(k):
    return bt.clock(bt.slot_end(k))


def slot_of(hhmm):
    """'09:35' (a bar's END time) -> slot index 3. None when it is not a bar end."""
    try:
        m = bt.hm_min(hhmm)
    except (nse.NSEError, ValueError, AttributeError, TypeError):
        return None
    d = m - OPEN_MIN - INTERVAL
    if d < 0 or d % INTERVAL:
        return None
    return d // INTERVAL


def present(bars):
    return [b for b in bars if b is not None]


def vwap(bars):
    """Session VWAP over the bars given (typical price x volume; plain average if no volume)."""
    pv = vol = tp_sum = 0.0
    n = 0
    for b in bars:
        if b is None:
            continue
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        pv += tp * b["v"]
        vol += b["v"]
        tp_sum += tp
        n += 1
    if not n:
        return None
    return pv / vol if vol > 0 else tp_sum / n


def atr(bars, n=14):
    """Average true range of the last n bars present (fewer if fewer exist)."""
    rows = present(bars)
    trs = []
    for i, b in enumerate(rows):
        if i == 0:
            trs.append(b["h"] - b["l"])
        else:
            pc = rows[i - 1]["c"]
            trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    trs = trs[-n:]
    return sum(trs) / len(trs) if trs else None


def file_sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:16]


# ---------------------------------------------------------------- causal views

class Prefix(object):
    """The bars that have closed: a read-only sequence that refuses to reach past them."""
    __slots__ = ("_b", "_n", "_trip", "_what")

    def __init__(self, bars, n, trip, what):
        self._b, self._n, self._trip, self._what = bars, n, trip, what

    def __len__(self):
        return self._n

    def __iter__(self):
        b = self._b
        for j in range(self._n):
            yield b[j]

    def __getitem__(self, i):
        if isinstance(i, slice):
            a, z, st = i.indices(self._n)
            return [self._b[j] for j in range(a, z, st)]
        if i < 0:
            i += self._n
            if i < 0:
                raise IndexError("bar index out of range")
        if i >= self._n:
            msg = "%s: read bar %d (%s-%s) but only bars up to %s have closed" % (
                self._what, i, bt.clock(OPEN_MIN + INTERVAL * i), end_time(i), end_time(self._n - 1))
            self._trip.append(msg)
            raise LookAheadError(msg)
        return self._b[i]


def freeze(bars):
    return [types.MappingProxyType(dict(b)) if b is not None else None for b in bars]


class DayView(object):
    def __init__(self, sym, date, k, frozen, trip, tick, sector, prev, daily_fn, state, avg_value20=None):
        self.symbol, self.date, self.k, self.tick, self.sector = sym, date, k, tick, sector
        self.avg_value20 = avg_value20
        self.prev = types.MappingProxyType(dict(prev)) if prev else None
        self.bars = Prefix(frozen, k + 1, trip, sym)
        self.time = end_time(k)
        self._daily_fn = daily_fn
        self.state = state

    @property
    def daily(self):
        return self._daily_fn()


class PeerView(object):
    __slots__ = ("symbol", "bars", "prev", "tick", "avg_value20")

    def __init__(self, symbol, bars, prev, tick, avg_value20):
        self.symbol, self.bars, self.prev, self.tick, self.avg_value20 = symbol, bars, prev, tick, avg_value20


class Ctx(object):
    def __init__(self, ds, sym, date, k, params, trip, win):
        self._ds, self._sym, self._date, self._k, self._trip = ds, sym, date, k, trip
        self.params = params
        self.time = end_time(k)
        self.window = {"first_entry": bt.clock(win.before), "last_entry": bt.clock(win.cutoff),
                       "flat_by": bt.clock(win.flat)}
        self.sector = ds.sectors.get(sym)
        self._peers = None
        ix = ds.index.get(date)
        self.index = Prefix(ix["frozen"], min(k + 1, len(ix["frozen"])), trip, "NIFTY") if ix else None
        self.index_prev_close = ix["prev_close"] if ix else None
        vx = ds.vix.get(date)
        self.vix = Prefix(vx["frozen"], min(k + 1, len(vx["frozen"])), trip, "INDIAVIX") if vx else None
        self.vix_prev_close = vx["prev_close"] if vx else None

    @property
    def peers(self):
        if self._peers is None:
            out = {}
            day = self._ds.by_date.get(self._date, {})
            for p in self._ds.peer_map.get(self._sym, ()):
                s = day.get(p)
                if s is None:
                    continue
                out[p] = PeerView(p, Prefix(self._ds.frozen(s), self._k + 1, self._trip, p), s.prev,
                                  s.tick, self._ds.avg_value20.get((p, s.date)))
            self._peers = out
        return self._peers

    @property
    def events(self):
        evs = self._ds.events.get(self._sym) or []
        if not evs:
            return []
        until = "%sT%s" % (self._date, self.time)
        since = (datetime.date.fromisoformat(self._date) - datetime.timedelta(days=EVENT_LOOKBACK_DAYS)).isoformat()
        return [types.MappingProxyType(e) for e in evs if since <= (e.get("detected") or "")[:16] <= until]


# ---------------------------------------------------------------- strategies

class Strategy(object):
    def __init__(self, sid, path, module):
        self.id, self.path, self.module = sid, path, module
        self.meta = dict(getattr(module, "META", {}) or {})
        if not callable(getattr(module, "signals", None)):
            raise StrategyError("%s has no signals(day, ctx) function" % path)
        self.signals = module.signals
        self.sha = file_sha(path)
        self.kind = self.meta.get("kind", "intraday")
        self.max_per_day = int(self.meta.get("max_trades_per_day", 1) or 1)

    def default(self):
        return dict(self.meta.get("default") or {})

    def variants(self):
        return expand_grid(self.default(), self.meta.get("grid") or {})


def expand_grid(default, grid):
    keys = sorted(grid)
    if not keys:
        return [dict(default)]
    out = []
    for combo in itertools.product(*[list(grid[k]) for k in keys]):
        p = dict(default)
        p.update(zip(keys, combo))
        out.append(p)
    return out


def grid_size(grid):
    n = 1
    for v in (grid or {}).values():
        n *= max(1, len(v))
    return n


def label(p):
    return " ".join("%s=%s" % (k, ("%g" % v) if isinstance(v, float) else v) for k, v in sorted(p.items())) or "(no parameters)"


def load_strategy(path, sid=None):
    if not os.path.isfile(path):
        raise StrategyError("no strategy file at %s" % path)
    sid = sid or os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location("lab_" + sid.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # a broken lab file is the researcher's to fix
        raise StrategyError("%s does not load: %s: %s" % (path, type(e).__name__, e))
    return Strategy(sid, path, mod)


# ---------------------------------------------------------------- splits

def read_splits(base=None):
    return nse.read_json(splits_path(base), {}) or {}


def make_split(dates, symbols, now=None):
    dates = sorted(dates)
    n = len(dates)
    if n < MIN_SPLIT_SESSIONS:
        raise LabError("only %d sessions in this dataset: a locked three-way split needs at least %d"
                       % (n, MIN_SPLIT_SESSIONS))
    e = int(n * FRACTIONS[0])
    v = int(round(n * (FRACTIONS[0] + FRACTIONS[1])))
    return {"explore": [dates[0], dates[e - 1]], "validate": [dates[e], dates[v - 1]],
            "holdout": [dates[v], dates[-1]], "sessions": [e, v - e, n - v],
            "symbols": sorted(symbols), "created": nse.iso_ist(now or nse.now_ist().replace(microsecond=0)),
            "dates_sha": hashlib.sha256(",".join(dates).encode()).hexdigest()[:16]}


def save_split(key, rec, base=None):
    """Adds a dataset's split. An existing split is never changed. A NEW split also records
    `exposed_before`: earlier artefacts (trader/backtest/*.json, other splits' explore/validate
    windows) that already measured something on its validate or holdout dates for its symbols.
    Informational only: the windows themselves are not moved."""
    os.makedirs(os.path.dirname(splits_path(base)), exist_ok=True)
    with open(splits_path(base) + ".lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            doc = read_splits(base)
            if key in doc:
                return doc[key]
            rec = dict(rec)
            if "exposed_before" not in rec:
                try:
                    rec["exposed_before"] = exposure(rec, key, base, doc)
                except Exception as e:  # a scan problem must never block or alter the split itself
                    rec["exposed_before"] = [{"error": "exposure scan failed: %s: %s" % (type(e).__name__, e)}]
            doc[key] = rec
            nse.write_json(splits_path(base), doc)
            return rec
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def _overlap(a, z, lo, hi):
    x, y = max(a, lo), min(z, hi)
    return [x, y] if x <= y else None


def artefact_meta(path):
    """(from, to, symbols, made) of a trader/backtest/*.json artefact, or None (no dated meta)."""
    try:
        doc = nse.read_json(path)
    except (nse.NSEError, OSError):
        return None
    m = doc.get("meta") if isinstance(doc, dict) else None
    if not isinstance(m, dict) or not m.get("from") or not m.get("to"):
        return None
    syms = list(m.get("symbols") or []) if isinstance(m.get("symbols"), list) else []
    if m.get("underlying"):
        syms.append(m["underlying"])
    return str(m["from"])[:10], str(m["to"])[:10], syms, doc.get("made")


def exposure(rec, key=None, base=None, splits=None, windows=("validate", "holdout")):
    """Earlier looks at this split's validate / holdout dates for its symbols: each india-backtest
    artefact in trader/backtest/ whose meta date range and symbols (or underlying) overlap, and each
    other split whose explore or validate window does. -> [{artefact, kind, window, overlap,
    symbols, n_symbols, made}]. Date ranges are the artefacts' own from/to (an upper bound)."""
    syms = set(rec.get("symbols") or [])
    wins = [(w, rec[w]) for w in windows if rec.get(w)]
    out = []
    root = nse.trader_dir(base)
    for path in sorted(glob.glob(os.path.join(root, "backtest", "*.json"))):
        m = artefact_meta(path)
        if not m:
            continue
        common = sorted(syms.intersection(m[2]))
        if not common:
            continue
        for w, (a, z) in wins:
            ov = _overlap(a, z, m[0], m[1])
            if ov:
                out.append({"artefact": os.path.relpath(path, root), "kind": "backtest", "window": w, "overlap": ov,
                            "symbols": common[:12], "n_symbols": len(common), "made": m[3]})
    for k, other in sorted((splits if splits is not None else read_splits(base)).items()):
        if k == key:
            continue
        common = sorted(syms.intersection(other.get("symbols") or []))
        if not common:
            continue
        for ow in ("explore", "validate"):
            if not other.get(ow):
                continue
            for w, (a, z) in wins:
                ov = _overlap(a, z, other[ow][0], other[ow][1])
                if ov:
                    out.append({"artefact": "splits.json %s (%s)" % (k, ow), "kind": "split", "window": w,
                                "overlap": ov, "symbols": common[:12], "n_symbols": len(common),
                                "made": other.get("created")})
    return out


def locked_ranges(key, symbols, base=None):
    """Other datasets' holdout ranges for the symbols they share with this one: {sym: [(a, b)]}."""
    out = {}
    syms = set(symbols)
    for k, rec in read_splits(base).items():
        if k == key:
            continue
        for s in syms.intersection(rec.get("symbols") or []):
            out.setdefault(s, []).append(tuple(rec["holdout"]))
    return out


# ---------------------------------------------------------------- tickets

class HoldoutTicket(object):
    """Proof that research.py wrote a permanent holdout claim for this experiment."""

    def __init__(self, exp_id, registry_path):
        self.exp_id, self.registry_path = exp_id, registry_path
        claims = results = 0
        try:
            with open(registry_path, encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    r = json.loads(line)
                    if r.get("id") != exp_id:
                        continue
                    claims += r.get("op") == "holdout_claim"
                    results += r.get("op") == "holdout"
        except OSError:
            pass
        if claims != 1 or results:
            raise HoldoutError("no open holdout claim for %s in the registry: the holdout opens only through "
                               "`research.py holdout %s`, once" % (exp_id, exp_id))


# ---------------------------------------------------------------- datasets

def source_dirs(source, base=None):
    """'yahoo' / 'angel' -> the bar caches; 'dir:/path[,/path]' -> those folders."""
    if source.startswith("dir:"):
        return [d for d in source[4:].split(",") if d]
    return [bt.bars_dir(source) if base is None else os.path.join(nse.trader_dir(base), "cache", "bars", source)]


def index_dirs(source_d, base=None):
    root = os.path.join(nse.trader_dir(base), "cache", "bars")
    return source_d + [os.path.join(root, "angel"), os.path.join(root, "yahoo")]


def _raw(dirs, sym):
    try:
        return bt.load_offline(dirs, sym)[:2]
    except (nse.NSEError, OSError, ValueError):
        return None


class Dataset(object):
    """Sessions of one split, cut so later bars do not exist in memory."""

    def __init__(self, key, split, sessions, cutoff, active, split_rec, notes):
        self.key, self.split, self.sessions, self.cutoff = key, split, sessions, cutoff
        self.active, self.split_rec, self.notes = active, split_rec, notes
        self.by_date = {}
        for sym, ss in sessions.items():
            for s in ss:
                self.by_date.setdefault(s.date, {})[sym] = s
        self.dates = sorted(self.by_date)
        self.sectors, self.peer_map, self.index, self.vix, self.events = {}, {}, {}, {}, {}
        self.avg_value20, self.daily = {}, {}
        self._frozen = {}

    def frozen(self, s):
        key = (s.symbol, s.date)
        f = self._frozen.get(key)
        if f is None:
            f = self._frozen[key] = freeze(s.bars)
        return f

    def dates_in(self, a, b):
        return [d for d in self.dates if a <= d <= b]

    def max_bar_date(self):
        ds = [s.date for ss in self.sessions.values() for s in ss]
        ds += list(self.index) + list(self.vix)
        ds += [d for rows in self.daily.values() for d, _ in rows]
        return max(ds) if ds else None


def _cut(bars, cutoff, locked=()):
    out = []
    for b in bars:
        d = b["t"].date().isoformat()
        if d >= cutoff:
            continue
        if any(a <= d <= z for a, z in locked):
            continue
        out.append(b)
    return out


def _index_sessions(dirs, sym, cutoff, cfg):
    raw = _raw(dirs, sym)
    if not raw:
        return {}
    b5, b1 = _cut(raw[0], cutoff), _cut(raw[1], cutoff)
    ss, _ = bt.build_sessions(sym, b5, b1, None, cfg)
    out, prev_c = {}, None
    for s in ss:
        last = [b for b in s.bars if b is not None]
        out[s.date] = {"frozen": freeze(s.bars), "prev_close": (s.prev or {}).get("close") or prev_c}
        prev_c = last[-1]["c"] if last else prev_c
    return out


def session_dates(dirs, symbols, cfg=None):
    dates, have = set(), []
    for sym in symbols:
        raw = _raw(dirs, sym)
        if not raw:
            continue
        ss, _ = bt.build_sessions(sym, raw[0], raw[1], None, cfg)
        if ss:
            have.append(sym)
        dates.update(s.date for s in ss)
    return sorted(dates), have


def ensure_split(key, dirs, symbols, cfg=None, base=None, now=None):
    rec = read_splits(base).get(key)
    if rec:
        return rec
    dates, have = session_dates(dirs, symbols, cfg)
    if not have:
        raise LabError("no 5-minute bars for any of %s in %s" % (", ".join(symbols[:8]), ", ".join(dirs)))
    return save_split(key, make_split(dates, have, now), base)


def next_day(iso):
    return (datetime.date.fromisoformat(iso) + datetime.timedelta(days=1)).isoformat()


def load_dataset(key, symbols, dirs, split, ticket=None, cfg=None, base=None, sectors=None, events=None,
                 ticks=None, now=None):
    """The one way bars reach a strategy. split: explore | validate | holdout."""
    rec = ensure_split(key, dirs, symbols, cfg, base, now)
    if split == "explore":
        cutoff, active = rec["validate"][0], (rec["explore"][0], rec["explore"][1])
    elif split == "validate":
        cutoff, active = rec["holdout"][0], (rec["explore"][0], rec["validate"][1])
    elif split == "holdout":
        if not isinstance(ticket, HoldoutTicket):
            raise HoldoutError("the holdout of %s is locked: it opens only through `research.py holdout <id>`, "
                               "once per experiment" % key)
        HoldoutTicket(ticket.exp_id, ticket.registry_path)      # still open? (re-checks the registry)
        cutoff, active = next_day(rec["holdout"][1]), (rec["explore"][0], rec["holdout"][1])
    else:
        raise LabError("unknown split %r" % split)
    locks = locked_ranges(key, symbols, base)
    notes, sessions, daily = [], {}, {}
    ticks = ticks or {}
    for sym in symbols:
        raw = _raw(dirs, sym)
        if not raw:
            notes.append("%s: no bars" % sym)
            continue
        lk = locks.get(sym, ())
        b5, b1 = _cut(raw[0], cutoff, lk), _cut(raw[1], cutoff, lk)
        ss, _ = bt.build_sessions(sym, b5, b1, None, cfg, ticks.get(sym))
        ss = [s for s in ss if active[0] <= s.date <= active[1]]
        if ss:
            sessions[sym] = ss
            daily[sym] = [(b["t"].date().isoformat(), b) for b in sorted(b1, key=lambda b: b["t"])]
    if lk_syms(locks, symbols):
        notes.append("dates locked as another dataset's holdout were removed for: %s" % lk_syms(locks, symbols))
    ds = Dataset(key, split, sessions, cutoff, active, rec, notes)
    ds.daily = daily
    idirs = index_dirs(list(dirs), base)
    ds.index = _index_sessions(idirs, "NIFTY", cutoff, cfg)
    ds.vix = _index_sessions(idirs, "INDIAVIX", cutoff, cfg)
    ds.sectors = dict((s, (sectors or {}).get(s)) for s in sessions)
    groups = {}
    for s, ind in ds.sectors.items():
        if ind:
            groups.setdefault(ind, []).append(s)
    ds.peer_map = dict((s, tuple(sorted(x for x in groups.get(ind, []) if x != s))) for s, ind in ds.sectors.items())
    # average traded value of the 20 sessions before each session (from the 5-minute bars)
    for sym, ss in sessions.items():
        vals = []
        for s in ss:
            ds.avg_value20[(sym, s.date)] = (sum(vals[-20:]) / len(vals[-20:])) if vals else None
            vals.append(sum(b["c"] * b["v"] for b in s.bars if b is not None))
    # recorded events, only those detected before the cutoff
    for e in events or []:
        if (e.get("detected") or "")[:10] < cutoff and e.get("symbol"):
            ds.events.setdefault(e["symbol"], []).append(e)
    mx = ds.max_bar_date()
    assert mx is None or mx < cutoff, "data loader leaked a bar from %s (cutoff %s)" % (mx, cutoff)
    return ds


def lk_syms(locks, symbols):
    return ", ".join(sorted(s for s in symbols if locks.get(s)))


# ---------------------------------------------------------------- sizing and simulation

def size_short(entry, stop, cfg):
    """india-budget's four limits for a short: loss at the stop (+ charges, buy = cover at the stop,
    sell = the entry), position cap, max order value, cash after the buffer."""
    c = dict(bt.DEFAULTS)
    c.update(cfg or {})
    capital = float(c["capital"])
    risk = round(stop - entry, 2)
    q = 0
    ml = float(c["max_loss_per_trade"])
    if risk > 0 and ml > 0:
        q = bt.shares_for(ml, risk)
        while q > 0:
            loss = q * risk + (nse.charges(q * stop, q * entry, c["exchange"])["total"]
                               if c.get("include_charges", True) else 0.0)
            if loss <= ml + 1e-9:
                break
            q -= 1
    limits = {"max_loss_per_trade": q}
    if c.get("position_cap_pct"):
        limits["position_cap_pct"] = bt.shares_for(capital * float(c["position_cap_pct"]) / 100.0, entry)
    if c.get("max_order_value"):
        limits["max_order_value"] = bt.shares_for(float(c["max_order_value"]), entry)
    limits["cash"] = bt.shares_for(capital * (1 - float(c.get("cash_buffer_pct") or 0) / 100.0), entry)
    qn = min(limits.values())
    return qn, [k for k, v in limits.items() if v == qn][0]


def simulate(s, k, side, stop_raw, target_raw, win, cfg, slip_ticks=1, charges_mult=1.0, why=""):
    """One trade opened at the close of bar k -> (trade, None) or (None, why not)."""
    price = round(s.bars[k]["c"], 2)
    tick = s.tick or nse.band_tick(price)
    long_ = side == "long"
    if long_:
        stop, target = nse.round_down(stop_raw, tick), nse.round_down(target_raw, tick)
        if stop <= 0 or stop >= price:
            return None, "stop not under the entry"
        if target <= price:
            return None, "target not above the entry"
        gap = price - stop
    else:
        stop, target = nse.round_up(stop_raw, tick), nse.round_up(target_raw, tick)
        if stop <= price:
            return None, "stop not above the entry"
        if target >= price or target <= 0:
            return None, "target not under the entry"
        gap = stop - price
    if gap < max(bt.MIN_TICKS * tick, price * bt.MIN_RISK_PCT / 100.0) - 1e-9:
        return None, "stop too tight (under 3 ticks or 0.05 %)"
    slip = round(slip_ticks * tick, 2)
    fill = round(price + slip, 2) if long_ else round(price - slip, 2)
    risk = round(fill - stop, 2) if long_ else round(stop - fill, 2)
    if risk <= 0:
        return None, "slippage ate the whole risk"
    qty, decided = bt.size(fill, stop, cfg) if long_ else size_short(fill, stop, cfg)
    if qty <= 0:
        return None, "zero shares fit the budget"
    outcome = exit_px = exit_k = None
    for i in range(k + 1, win.last + 1):
        b = s.bars[i]
        if b is None:
            continue
        if long_:
            if b["o"] <= stop:
                outcome, exit_px, exit_k = "stop_gap", round(b["o"] - slip, 2), i
            elif b["l"] <= stop:
                outcome, exit_px, exit_k = "stop", round(stop - slip, 2), i
            elif b["h"] >= target:
                outcome, exit_px, exit_k = "target", target, i
        else:
            if b["o"] >= stop:
                outcome, exit_px, exit_k = "stop_gap", round(b["o"] + slip, 2), i
            elif b["h"] >= stop:
                outcome, exit_px, exit_k = "stop", round(stop + slip, 2), i
            elif b["l"] <= target:
                outcome, exit_px, exit_k = "target", target, i
        if outcome:
            break
    if outcome is None:
        exit_k = max(i for i in range(k, win.last + 1) if s.bars[i] is not None)
        c = s.bars[exit_k]["c"]
        outcome, exit_px = "flat", round(c - slip, 2) if long_ else round(c + slip, 2)
    exit_px = max(exit_px, 0.01)
    ex = (cfg or {}).get("exchange") or "NSE"
    if long_:
        ch = nse.charges(qty * fill, qty * exit_px, ex)
        gross = round((exit_px - fill) * qty, 2)
        move = exit_px - fill
    else:
        ch = nse.charges(qty * exit_px, qty * fill, ex)      # buy = the cover, sell = the entry
        gross = round((fill - exit_px) * qty, 2)
        move = fill - exit_px
    total_ch = round(ch["total"] * charges_mult, 2)
    net = round(gross - total_ch, 2)
    return {"symbol": s.symbol, "date": s.date, "time": end_time(k), "side": side, "signal": price, "entry": fill,
            "stop": stop, "target": target, "tick": tick, "risk": risk, "qty": qty, "sized_by": decided,
            "exit": exit_px, "exit_time": end_time(exit_k) if outcome != "flat" else bt.clock(win.flat),
            "exit_k": exit_k, "outcome": outcome, "R": round(move / risk, 4), "R_net": round(net / (risk * qty), 4),
            "pnl": gross, "charges": total_ch, "stt": ch["stt"], "stamp": ch["stamp"], "pnl_net": net,
            "why": why}, None


# ---------------------------------------------------------------- the streaming run

def _check_trade(t, sym, date):
    if not isinstance(t, dict):
        raise StrategyError("%s %s: signals() must return a list of dicts, got %r" % (sym, date, t))
    miss = [f for f in ("side", "entry_time", "entry", "stop", "target") if t.get(f) is None]
    if miss:
        raise StrategyError("%s %s: trade without %s" % (sym, date, ", ".join(miss)))
    if t["side"] not in SIDES:
        raise StrategyError("%s %s: side must be long or short, not %r" % (sym, date, t["side"]))
    k = slot_of(t["entry_time"])
    if k is None:
        raise StrategyError("%s %s: entry_time %r is not the end of a 5-minute bar" % (sym, date, t["entry_time"]))
    return k


def cap_settings(cfg):
    """The portfolio cap: {"on", "capital", "max_positions"}. ON unless cfg says capital_cap False."""
    c = cfg or {}
    return {"on": bool(c.get("capital_cap", True)),
            "capital": float(c.get("capital") or bt.DEFAULTS["capital"]),
            "max_positions": int(c.get("max_positions") or MAX_POSITIONS)}


def run_strategy(ds, strat, params, cfg=None, dates=None, symbols=None, slip_ticks=1, charges_mult=1.0):
    """Stream every stock-day of `dates`; -> (trades, info). Raises LookAheadError on any peek.

    Each session is streamed bar by bar across ALL its stocks (bar k of every stock, stocks in
    symbol order, then bar k+1), so decisions arrive in decision-time order. With the portfolio
    cap on (the default; cfg capital_cap False turns it off) a trade is taken only if, at the close
    of its decision bar, the open positions (entered earlier, not yet out) plus this one stay within
    cfg capital (default Rs 1,00,000, entry value) and cfg max_positions (default 5); otherwise it
    is skipped and counted (info capital_skips) and the strategy is asked again on later bars, as a
    trader who could not take it would be. Trades come back ordered by date, symbol, time."""
    win = bt.Window(cfg)
    dates = sorted(dates if dates is not None else ds.dates)
    want = set(symbols) if symbols else None
    cap = cap_settings(cfg)
    trades, skips = [], {}
    info = {"calls": 0, "decisions": 0, "violations": 0, "stock_days": 0, "capital_cap": cap, "capital_skips": 0,
            "peak_positions": 0, "peak_deployed": 0.0}
    for date in dates:
        day_sessions = ds.by_date.get(date, {})
        live = []
        for sym in sorted(day_sessions):
            if want is not None and sym not in want:
                continue
            s = day_sessions[sym]
            info["stock_days"] += 1
            rows = ds.daily.get(sym, [])
            cut = bisect.bisect_left([d for d, _ in rows], date) if rows else 0

            def daily_fn(rows=rows, cut=cut):
                return tuple(types.MappingProxyType({"date": d, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"],
                                                     "v": b.get("v", 0)}) for d, b in rows[max(0, cut - 60):cut])
            live.append({"sym": sym, "s": s, "frozen": ds.frozen(s), "daily_fn": daily_fn, "state": {},
                         "called": {}, "taken": 0, "busy": -1, "done": False})
        day_trades, open_pos = [], []
        for k in range(0, win.last + 1):
            if bt.slot_end(k) > win.cutoff:
                break
            for L in live:
                if L["done"]:
                    continue
                sym, s = L["sym"], L["s"]
                if s.bars[k] is None:
                    continue
                if not win.ok(k) or k <= L["busy"]:
                    continue
                if L["taken"] >= strat.max_per_day:
                    L["done"] = True
                    continue
                trip = []
                day = DayView(sym, date, k, L["frozen"], trip, s.tick or nse.band_tick(s.bars[k]["c"]),
                              ds.sectors.get(sym), s.prev, L["daily_fn"], L["state"], ds.avg_value20.get((sym, date)))
                ctx = Ctx(ds, sym, date, k, params, trip, win)
                info["calls"] += 1
                try:
                    out = strat.signals(day, ctx) or []
                except LookAheadError:
                    info["violations"] += 1
                    raise
                if trip:
                    info["violations"] += 1
                    raise LookAheadError("%s %s at %s: %s (the strategy caught the error, the engine did not)"
                                         % (strat.id, date, day.time, trip[0]))
                called = L["called"]
                offered, now_trades = set(), []
                for t in out:
                    kt = _check_trade(t, sym, date)
                    key = (t["side"], kt, round(float(t["stop"]), 2), round(float(t["target"]), 2))
                    if kt > k:
                        info["violations"] += 1
                        raise LookAheadError("%s %s %s: a trade at %s returned at %s: entries happen at the close of "
                                             "the last closed bar" % (strat.id, sym, date, t["entry_time"], day.time))
                    if kt < k:
                        if kt in called and key not in called[kt]:
                            info["violations"] += 1
                            raise LookAheadError(
                                "%s %s %s: back-dated decision: at %s the strategy wanted no such trade; at %s, "
                                "having seen later bars, it returned one dated %s" % (
                                    strat.id, sym, date, t["entry_time"], day.time, t["entry_time"]))
                        continue
                    if abs(float(t["entry"]) - round(s.bars[k]["c"], 2)) > 0.005:
                        raise StrategyError("%s %s %s: entry %.2f is not the close %.2f of the %s bar"
                                            % (strat.id, sym, date, float(t["entry"]), s.bars[k]["c"], day.time))
                    offered.add(key)
                    now_trades.append(t)
                called[k] = offered
                if not now_trades:
                    continue
                t = now_trades[0]
                info["decisions"] += 1
                tr, why = simulate(s, k, t["side"], float(t["stop"]), float(t["target"]), win, cfg, slip_ticks,
                                   charges_mult, t.get("reason") or "")
                if tr is None:
                    skips[why] = skips.get(why, 0) + 1
                    continue
                open_pos = [p for p in open_pos if p[0] > k]              # out during or before this bar
                value = tr["entry"] * tr["qty"]
                deployed = sum(p[1] for p in open_pos)
                if cap["on"]:
                    reason = None
                    if len(open_pos) >= cap["max_positions"]:
                        reason = "capital cap: %d positions open" % cap["max_positions"]
                    elif deployed + value > cap["capital"] + 1e-6:
                        reason = "capital cap: Rs %.0f deployed" % cap["capital"]
                    if reason:
                        skips[reason] = skips.get(reason, 0) + 1
                        info["capital_skips"] += 1
                        continue
                open_pos.append((tr["exit_k"], value))                    # peaks are tracked either way
                info["peak_positions"] = max(info["peak_positions"], len(open_pos))
                info["peak_deployed"] = round(max(info["peak_deployed"], deployed + value), 2)
                tr["seen_until"] = day.time
                tr["params"] = label(params)
                day_trades.append(tr)
                L["taken"] += 1
                L["busy"] = tr["exit_k"]
        day_trades.sort(key=lambda x: x["symbol"])                  # stable: time order within a stock
        trades += day_trades
    info["skips"] = skips
    return trades, info


def unsizable(ds, cfg=None):
    """Stocks no session of which could buy even one share under the size limits that do not depend
    on the stop (position cap, max order value, cash after the buffer): e.g. BOSCHLTD above a
    Rs 25,000 position cap. They never trade, silently, unless reported. -> [{symbol, min_price,
    limit, by}]."""
    c = dict(bt.DEFAULTS)
    c.update(cfg or {})
    capital = float(c["capital"])
    lim = {"cash": capital * (1 - float(c.get("cash_buffer_pct") or 0) / 100.0)}
    if c.get("position_cap_pct"):
        lim["position_cap_pct"] = capital * float(c["position_cap_pct"]) / 100.0
    if c.get("max_order_value"):
        lim["max_order_value"] = float(c["max_order_value"])
    by = min(lim, key=lambda k: lim[k])
    out = []
    for sym, ss in sorted(ds.sessions.items()):
        lows = [b["l"] for s in ss for b in s.bars if b is not None]
        if lows and min(lows) > lim[by]:
            out.append({"symbol": sym, "min_price": round(min(lows), 2), "limit": round(lim[by], 2), "by": by})
    return out


def run_variants(ds, strat, cfg=None, dates=None, slip_ticks=1, charges_mult=1.0, variants=None):
    out = []
    for p in variants or strat.variants():
        tr, info = run_strategy(ds, strat, p, cfg, dates, None, slip_ticks, charges_mult)
        out.append({"params": p, "label": label(p), "trades": tr, "info": info})
    return out


def choose(rows, dates, default, min_trades=MIN_SELECT_TRADES):
    """Best variant on `dates` by mean net R (at least min_trades), else the default."""
    keep = set(dates)
    best = None
    for r in rows:
        tr = [t for t in r["trades"] if t["date"] in keep]
        if len(tr) < min_trades:
            continue
        m = sum(t["R_net"] for t in tr) / len(tr)
        if best is None or (m, len(tr)) > best[0]:
            best = ((m, len(tr)), r)
    if best:
        return best[1], "best mean net R on %d training sessions (>= %d trades)" % (len(keep), min_trades)
    for r in rows:
        if r["params"] == default:
            return r, "default (no variant had %d training trades)" % min_trades
    return rows[0], "first variant (no default in the grid)"


def folds_of(dates, n):
    dates = sorted(dates)
    n = max(1, min(n, len(dates)))
    size = len(dates) / float(n)
    return [dates[int(round(i * size)):int(round((i + 1) * size))] for i in range(n)]


def walk_forward(ds, strat, cfg, train_dates, test_dates, folds=2, slip_ticks=1):
    """Anchored walk-forward: each fold trades the variant chosen on everything before it."""
    rows = run_variants(ds, strat, cfg, sorted(set(train_dates) | set(test_dates)), slip_ticks)
    trades, fold_info, chosen = [], [], []
    seen = list(train_dates)
    for f in folds_of(test_dates, folds):
        if not f:
            continue
        r, how = choose(rows, seen, strat.default())
        fs = set(f)
        ft = [t for t in r["trades"] if t["date"] in fs]
        trades += ft
        chosen.append(r["params"])
        fold_info.append({"from": f[0], "to": f[-1], "sessions": len(f), "chosen": r["label"], "params": r["params"],
                          "chosen_by": how, "trades": len(ft)})
        seen += f
    calls = sum(r["info"]["calls"] for r in rows)
    return trades, fold_info, rows, calls


def stress(ds, strat, cfg, fold_info, slip_ticks=2, charges_mult=1.5):
    """Each fold's chosen variant again with worse costs."""
    out = []
    for f in fold_info:
        dates = [d for d in ds.dates if f["from"] <= d <= f["to"]]
        tr, _ = run_strategy(ds, strat, f["params"], cfg, dates, None, slip_ticks, charges_mult)
        out += tr
    return out


# ---------------------------------------------------------------- benchmark

def benchmark(ds, dates):
    """Daily returns of buy-and-hold: the universe equal-weighted, and NIFTY 50 when its bars exist."""
    ew, nifty = {}, {}
    for d in dates:
        rets = []
        for sym, s in ds.by_date.get(d, {}).items():
            last = [b for b in s.bars if b is not None]
            pc = (s.prev or {}).get("close")
            if last and pc:
                rets.append(last[-1]["c"] / pc - 1)
        if rets:
            ew[d] = sum(rets) / len(rets)
        ix = ds.index.get(d)
        if ix and ix["prev_close"]:
            last = [b for b in ix["frozen"] if b is not None]
            if last:
                nifty[d] = last[-1]["c"] / ix["prev_close"] - 1
    return {"universe_ew": ew, "nifty": nifty}


def audit(trades, ds):
    """No look-ahead, re-checked on the result: every trade decided on bars closed by its entry."""
    bad = [t for t in trades if t.get("seen_until") != t["time"]]
    return {"trades": len(trades), "decided_after_entry": len(bad), "max_bar_date": ds.max_bar_date(),
            "cutoff": ds.cutoff, "loader_ok": (ds.max_bar_date() or "") < ds.cutoff}
