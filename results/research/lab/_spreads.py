#!/usr/bin/env python3
"""Defined-risk NIFTY weekly option SPREADS for india-research. Research only: never trades.

The lab engine (lab_engine.py) runs intraday equity only, so this module runs option spread
ideas with the same discipline: the same registry (research.append), the same gates
(stats.gates), a locked split made once (lab_engine.save_split, key bhav:nifty-weekly, cut
by EXPIRY: oldest ~60 % of the weekly expiries explore, next ~20 % validate, latest ~20 %
holdout, which loads only with a HoldoutTicket after a permanent holdout claim), every
parameter combination counted as a trial, and run files in trader/research/runs/<id>-<split>.json
with the fields research.py writes.

Pricing is india-backtest/options.py's, imported, never edited: Black-Scholes per 5-minute
bar from the real NIFTY bars, the volatility implied from that contract's real bhavcopy open,
clipped into the contract's real day range (the reality clamp). What is REAL, not modelled:
the settlement of every leg held to expiry (the bhavcopy settle = intrinsic at the final
settlement price), the daily marks of a multi-day position (the bhavcopy settle each day),
lot sizes and strikes. What is modelled: every intraday fill (entries at 09:30 / 10:15, stops,
same-day flat exits). `--shift` reprices every modelled option by (1 + shift) before the
clamp, to show how much a 10 % model error moves the answer.

Charges per leg: nse.option_charges (Rs 20 an executed order, STT 0.1 % of SOLD premium
before 2026-04-01 and 0.15 % from then, NSE txn 0.03553 %, SEBI, stamp 0.003 % of bought
premium, GST 18 %). A leg that expires worthless pays no closing order. A LONG leg that
expires in the money pays STT 0.125 % of its intrinsic value on exercise (the buyer's
charge); a short leg assigned pays nothing more. Slippage: half-spread 0.5 % of the premium,
at least one tick (Rs 0.05), on every leg, every side (x2 in the cost stress; charges x1.5).

Margin, estimated conservatively (no broker calculator here; NSE's framework since 2020):
per lot, exposure margin 2 % of the notional (spot x lot) for EACH short leg, hedged or not,
plus SEBI's extra 2 % ELM per short leg when the position is open on its expiry day, plus
the structure's maximum loss (the SPAN of a hedged spread is about its worst case). Lots =
floor(90 % of capital / margin). On a Rs 1,00,000 account that means: ONE vertical through
expiry (about Rs 65,000-80,000), an iron condor only if it is closed the session before
expiry (about Rs 65,000-76,000), and NO condor held into expiry day (Rs 1.2-1.4 lakh): such
days are skipped and counted. Debit spreads are sized like the losing option buys: 20 % of
capital in net debit, whole lots. Nothing here is naked.

Strategy file (trader/research/lab/<id>.py): META as for the lab engine (family, kind,
rationale, rules, default, grid) and a SPREAD dict; `signals` is a stub for research.py.

    SPREAD = {"structure": "iron_condor" | "put_credit" | "call_credit" | "vertical_0dte" | "debit_orb"}
    params: k_sigma (short strikes k x VIX-implied sigma for the time left), wing (points),
            entry ("09:30" | "10:15" | "open" | "orb": at the first 5-minute close outside the
            opening range; vertical_0dte then sells the put spread on a break up and the call
            spread on a break down, and the put spread at a clock entry), dte (0 = expiry day,
            1, 2, or "any"), exit ("settle" = hold to the real settlement | "prev_close" = close
            at the real bhavcopy close of the session before expiry, never on expiry day |
            "flat" = the same day's 15:05 at modelled prices), stop_mult (close a credit spread
            when its modelled value reaches stop_mult x the credit; 0 = none),
            vix_filter (1 = enter only when India VIX at entry is above the median of the
            previous 20 sessions' closes), or_minutes / stop_pct / target_pct (debit_orb).

    python3 _spreads.py explore|validate|gate|holdout <id> [--trader-dir DIR] [--folds 2]
    python3 _spreads.py split            # make (once) and print the expiry split
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _skill_root():
    env = os.environ.get("INDIA_SKILL_DIR")
    if env and os.path.isfile(os.path.join(env, "india-research", "research.py")):
        return env
    d = HERE
    for _ in range(6):
        for cand in (d, os.path.join(d, "india-trader-skill")):
            if os.path.isfile(os.path.join(cand, "india-research", "research.py")):
                return cand
        d = os.path.dirname(d)
    home = os.path.expanduser("~/.claude/skills")
    if os.path.isfile(os.path.join(home, "india-research", "research.py")):
        return home
    raise SystemExit("cannot find the india-trader-skill folder (set INDIA_SKILL_DIR)")


ROOT = _skill_root()
for sub in ("india-common", "india-backtest", "india-research"):
    sys.path.insert(0, os.path.join(ROOT, sub))
import nse  # noqa: E402
import backtest as bt  # noqa: E402
import options as op  # noqa: E402
import lab_engine as le  # noqa: E402
import stats as st  # noqa: E402
import research as rs  # noqa: E402

DATASET = "bhav:nifty-weekly"
SYM = "NIFTY"
CAPITAL = 100000.0
SLIP_PCT = 0.5            # half-spread each side, every leg (index options)
EXPOSURE_PCT = 2.0        # margin estimate: 2 % of notional per short leg + the max loss
MARGIN_USE = 0.90         # at most 90 % of capital in margin (a 10 % cash buffer, as india-budget keeps)
EXPIRY_ELM_PCT = 2.0      # SEBI (Oct 2024): an extra 2 % ELM on short options open on their expiry day
DEBIT_ALLOC_PCT = 20.0    # debit spreads: 20 % of capital in net debit (as the losing buys)
EXERCISE_STT_PCT = 0.125  # STT on a long option exercised at expiry, on its intrinsic value
STRIKE_STEP = 50.0
ENTRY_SLOT = {"open": 0, "09:30": 2, "10:15": 11}   # bar index whose close we trade (open = 09:15 bar's open)
FLAT_SLOT, EXPIRY_FLAT_SLOT = 69, 68                # 15:05 flat (the 15:00 bar), 15:00 on expiry day
FRACTIONS = le.FRACTIONS


class SpreadError(nse.NSEError):
    pass


# ---------------------------------------------------------------- data

def all_bhav_dates():
    return op.bhav_dates()


def expiry_of_dates():
    """{date: nearest NIFTY expiry} from the cached bhavcopies (an expiry calendar, no prices)."""
    out = {}
    for d in all_bhav_dates():
        ch = op.load_chain(d)
        if ch and SYM in ch.exp:
            e = ch.nearest(SYM)
            if e:
                out[d] = e
    return out


def make_expiry_split(now=None):
    cal = expiry_of_dates()
    exps = sorted(set(cal.values()))
    n = len(exps)
    if n < 10:
        raise SpreadError("only %d expiries cached: too few for a three-way split" % n)
    e = int(n * FRACTIONS[0])
    v = int(round(n * (FRACTIONS[0] + FRACTIONS[1])))
    groups = {"explore": set(exps[:e]), "validate": set(exps[e:v]), "holdout": set(exps[v:])}
    dates = {g: sorted(d for d, x in cal.items() if x in s) for g, s in groups.items()}
    rec = {"explore": [dates["explore"][0], dates["explore"][-1]],
           "validate": [dates["validate"][0], dates["validate"][-1]],
           "holdout": [dates["holdout"][0], dates["holdout"][-1]],
           "sessions": [len(dates["explore"]), len(dates["validate"]), len(dates["holdout"])],
           "expiries": [e, v - e, n - v], "by": "expiry (a session belongs to its nearest NIFTY expiry)",
           "symbols": [SYM], "created": nse.iso_ist(now or nse.now_ist().replace(microsecond=0)),
           "dates_sha": hashlib.sha256(",".join(sorted(cal)).encode()).hexdigest()[:16]}
    return le.save_split(DATASET, rec)


def split_rec():
    doc = le.read_splits()
    return doc.get(DATASET) or make_expiry_split()


class Data(object):
    """Everything a run may see: sessions, VIX, chains and the clock, cut at `cutoff` (no bar,
    chain or VIX value on or after it exists in memory)."""

    def __init__(self, split, ticket=None):
        sp = split_rec()
        self.split_rec = sp
        if split == "explore":
            cutoff = sp["validate"][0]
        elif split == "validate":
            cutoff = sp["holdout"][0]
        elif split == "holdout":
            if not isinstance(ticket, le.HoldoutTicket):
                raise le.HoldoutError("the holdout loads only with a HoldoutTicket (research.py-style claim first)")
            cutoff = le.next_day(sp["holdout"][1])
        else:
            raise SpreadError("split must be explore, validate or holdout")
        self.cutoff = cutoff
        ss, _ = op.load_sessions(SYM)
        vs, _ = op.load_sessions("INDIAVIX")
        ss = [s for s in ss if s.date < cutoff]
        vs = [s for s in vs if s.date < cutoff]
        self.vix_by_date = dict((s.date, s) for s in vs)
        self.vix_closes = [(s.date, [b for b in s.bars if b is not None][-1]["c"]) for s in vs
                           if any(b is not None for b in s.bars)]
        known = [d for d in all_bhav_dates() if d < cutoff]
        self.clock = op.Clock(known)
        days, no_bhav = op.build_days(SYM, ss, vs, date_to=le.next_day(cutoff) and None)
        self.days = [d for d in days if d.date < cutoff and d.expiry]
        self.no_bhav = no_bhav
        self.by_date = dict((d.date, d) for d in self.days)
        self.dates = sorted(self.by_date)
        assert all(d < cutoff for d in self.dates), "loader kept a bar past the cutoff"
        assert all(d.expiry < cutoff for d in self.days), "a day's expiry lies past the cutoff"
        self.max_bar_date = max(self.dates) if self.dates else None

    def dates_in(self, a, b):
        return [d for d in self.dates if a <= d <= b]

    def vix_median20(self, date):
        prev = [c for d, c in self.vix_closes if d < date][-20:]
        return statistics.median(prev) if len(prev) >= 10 else None

    def benchmark(self, dates):
        out = {}
        for d in dates:
            day = self.by_date.get(d)
            if not day or not day.sess.prev or not day.sess.prev.get("close"):
                continue
            last = [b for b in day.sess.bars if b is not None]
            if last:
                out[d] = last[-1]["c"] / day.sess.prev["close"] - 1
        return {"universe_ew": dict(out), "nifty": dict(out)}


# ---------------------------------------------------------------- pricing

class Pricer(object):
    """Modelled option bars per contract-day, shifted by (1 + shift) then clamped to the real range.
    `sigma` overrides the contract's own anchor (used when a leg's bhavcopy-open anchor is unusable
    or would invert a vertical: a farther strike priced above a nearer one)."""

    def __init__(self, data, shift=0.0):
        cfg = {"r": op.R_DEFAULT, "q": op.Q_DEFAULT, "vix_drift": False, "capital": CAPITAL, "alloc_pct": 20,
               "slip_pct": SLIP_PCT, "flat_by": "15:05"}
        self.eng = op.Engine(SYM, data.days, data.clock, cfg)
        self.shift = shift
        self.cache = {}
        self._atm = {}

    def path(self, day, expiry, strike, typ, sigma=None):
        key = (day.date, expiry, float(strike), typ, sigma)
        if key in self.cache:
            return self.cache[key]
        row = day.chain.row(SYM, expiry, strike, typ)
        res = None
        if row:
            ed = op.as_date(expiry)
            if sigma is None:
                raw, sig, src = self.eng.contract(day, row, ed)
            else:
                raw, sig, src = self.eng.model.path(day.sess, row, day.d, ed, sigma, day.vix), sigma, "atm_iv"
            if raw is not None:
                sh = [None if b is None else tuple(max(op.OPT_TICK, x * (1 + self.shift)) for x in b) for b in raw]
                res = (op.clamp_path(sh, row["low"], row["high"]), row, sig, src)
        self.cache[key] = res
        return res

    def atm_iv(self, day, expiry):
        """The nearest-the-open ATM contract's implied vol (calls, then puts) from its bhavcopy open."""
        key = (day.date, expiry)
        if key not in self._atm:
            b0 = day.sess.bars[0]
            iv = None
            if b0 is not None:
                K = day.chain.atm(SYM, expiry, b0["o"])
                for typ in ("CE", "PE"):
                    row = day.chain.row(SYM, expiry, K, typ) if K is not None else None
                    if row:
                        sg, src = self.eng.model.anchor(row, day.sess, day.d, op.as_date(expiry))
                        if sg and src == "bhav_open":
                            iv = sg
                            break
            self._atm[key] = iv
        return self._atm[key]

    def legs_paths(self, day, expiry, legs, k):
        """Paths for a structure's legs with the no-arbitrage check: a leg whose own anchor is missing
        or outside 0.6-1.8 x the ATM vol, or a vertical that comes out inverted at bar k, is repriced
        at the ATM vol. -> (paths, rows, sigmas, sources) or (None, why)."""
        atm = self.atm_iv(day, expiry)
        out = []
        for leg in legs:
            r = self.path(day, expiry, leg["strike"], leg["type"])
            sg = r[2] if r else None
            if r is None or (atm and not (0.6 * atm <= sg <= 1.8 * atm)):
                if not atm:
                    return None, "no usable implied vol for %s %g %s" % (expiry, leg["strike"], leg["type"])
                r = self.path(day, expiry, leg["strike"], leg["type"], atm)
                if r is None:
                    return None, "no bhavcopy row for %s %g %s" % (expiry, leg["strike"], leg["type"])
            out.append(r)
        # verticals: the short leg (nearer the money) must be worth more than its long wing
        for i in range(0, len(legs) - 1, 2):
            a, b = out[i][0][k], out[i + 1][0][k]
            if a is None or b is None:
                return None, "no modelled bar at the entry"
            if a[3] <= b[3]:
                if not atm:
                    return None, "inverted vertical and no ATM vol to reprice it"
                out[i] = self.path(day, expiry, legs[i]["strike"], legs[i]["type"], atm)
                out[i + 1] = self.path(day, expiry, legs[i + 1]["strike"], legs[i + 1]["type"], atm)
                if out[i] is None or out[i + 1] is None or out[i][0][k][3] <= out[i + 1][0][k][3]:
                    return None, "inverted vertical even at the ATM vol"
        return out, None


def slip(px, mult=1.0):
    return max(px * SLIP_PCT * mult / 100.0, op.OPT_TICK)


def round_strike(x):
    return round(x / STRIKE_STEP) * STRIKE_STEP


# ---------------------------------------------------------------- the structures

def legs_for(structure, p, day, data, k, S):
    """-> (legs, why_not). A leg: {"strike","type","side": "short"|"long"}."""
    expiry = day.expiry
    ed = op.as_date(expiry)
    if structure not in ("iron_condor", "put_credit", "call_credit"):
        return None, "unknown credit structure %r" % structure
    vix = day.vix[k]["c"] if day.vix and day.vix[k] is not None else None
    if vix is None:
        return None, "no India VIX bar at the entry"
    T = data.clock.years(day.d, bt.slot_end(k), ed)
    sig_pts = S * vix / 100.0 * math.sqrt(T)
    dist = round_strike(p["k_sigma"] * sig_pts)
    if dist < STRIKE_STEP:
        dist = STRIKE_STEP
    atm = round_strike(S)
    put_k, call_k = atm - dist, atm + dist
    w = float(p["wing"])
    legs = []
    if structure in ("iron_condor", "put_credit"):
        legs += [{"strike": put_k, "type": "PE", "side": "short"}, {"strike": put_k - w, "type": "PE", "side": "long"}]
    if structure in ("iron_condor", "call_credit"):
        legs += [{"strike": call_k, "type": "CE", "side": "short"}, {"strike": call_k + w, "type": "CE", "side": "long"}]
    return legs, None


def intrinsic(S, strike, typ):
    return max(0.0, S - strike) if typ == "CE" else max(0.0, strike - S)


def leg_fill(path, k, side, slip_mult, use_open=False):
    """Fill one leg at bar k: short legs sell at mid - slip, long legs buy at mid + slip."""
    b = path[k]
    if b is None:
        return None, None
    mid = b[0] if use_open else b[3]
    px = mid - slip(mid, slip_mult) if side == "short" else mid + slip(mid, slip_mult)
    return max(op.OPT_TICK, op.round_down_tick(px) if side == "short" else op.round_up_tick(px)), mid


def leg_close(mid, side, slip_mult):
    """Closing a leg: a short leg is bought back at mid + slip, a long leg sold at mid - slip."""
    px = mid + slip(mid, slip_mult) if side == "short" else mid - slip(mid, slip_mult)
    return max(op.OPT_TICK, op.round_up_tick(px) if side == "short" else op.round_down_tick(px))


def spread_value(paths, legs, i, use="c"):
    """Net value of the structure (what it costs to close: short legs minus long legs) at bar i."""
    v = 0.0
    j = {"o": 0, "h": 1, "l": 2, "c": 3}[use]
    for leg, path in zip(legs, paths):
        b = path[i]
        if b is None:
            return None
        v += b[j] if leg["side"] == "short" else -b[j]
    return v


def leg_charges(leg, entry_px, exit_px, qty, date, settled, intr, mult):
    """Charges of one leg, rupees."""
    if leg["side"] == "short":
        if settled:
            ch = nse.option_charges(0.0, entry_px * qty, orders=1, on=date)["total"]
        else:
            ch = nse.option_charges(exit_px * qty, entry_px * qty, orders=2, on=date)["total"]
    else:
        if settled:
            ch = nse.option_charges(entry_px * qty, 0.0, orders=1, on=date)["total"]
            if intr > 0:
                ch += round(intr * qty * EXERCISE_STT_PCT / 100.0, 2)
        else:
            ch = nse.option_charges(entry_px * qty, exit_px * qty, orders=2, on=date)["total"]
    return round(ch * mult, 2)


def margin_estimate(legs, S, lot, max_loss_per_lot, on_expiry_day):
    n_short = sum(1 for l in legs if l["side"] == "short")
    pct = EXPOSURE_PCT + (EXPIRY_ELM_PCT if on_expiry_day else 0.0)
    return round(n_short * pct / 100.0 * S * lot + max_loss_per_lot, 2)


# ---------------------------------------------------------------- one trade

def run_credit(structure, p, day, data, pricer, slip_mult, ch_mult, k_entry, use_open, why_entry=""):
    """A credit structure opened at bar k_entry of `day`, held to the real settlement (exit=settle),
    closed at the real bhavcopy close of the session before expiry (exit=prev_close) or at the
    same day's 15:05 at modelled prices (exit=flat), with an optional stop on the modelled value."""
    if day.sess.bars[k_entry] is None or day.sess.bars[0] is None:
        return None, "no NIFTY bar at the entry"
    S = day.sess.bars[k_entry]["c"] if not use_open else day.sess.bars[0]["o"]
    legs, why = legs_for(structure, p, day, data, k_entry, S)
    if legs is None:
        return None, why
    expiry, ed = day.expiry, op.as_date(day.expiry)
    res, why = pricer.legs_paths(day, expiry, legs, k_entry)
    if res is None:
        return None, why
    paths, rows, srcs = [r[0] for r in res], [r[1] for r in res], [r[3] for r in res]
    lot = int(rows[0]["lot"] or 1)
    fills, mids = [], []
    for leg, path in zip(legs, paths):
        f, m = leg_fill(path, k_entry, leg["side"], slip_mult, use_open)
        if f is None:
            return None, "no modelled bar at the entry"
        fills.append(f)
        mids.append(m)
    credit = sum(f if l["side"] == "short" else -f for l, f in zip(legs, fills))
    credit_mid = sum(m if l["side"] == "short" else -m for l, m in zip(legs, mids))
    if credit <= 0:
        return None, "no net credit after the spread"
    width = float(p["wing"])
    max_loss_lot = (width - credit) * lot
    if max_loss_lot <= 0:
        return None, "credit above the width (model artefact)"
    is_exp = expiry == day.date
    exit_mode = p.get("exit", "settle")
    if exit_mode == "prev_close" and is_exp:
        return None, "exit=prev_close never opens on expiry day"
    held_on_expiry = exit_mode != "prev_close" and not (exit_mode == "flat" and not is_exp)
    margin = margin_estimate(legs, S, lot, max_loss_lot, held_on_expiry)
    lots = int((CAPITAL * MARGIN_USE) // margin)
    if lots < 1:
        return None, "does not fit: one lot needs %s of margin (%s), over %g %% of capital" % (
            nse.inr(margin, 0), "incl. expiry-day ELM" if held_on_expiry else "no expiry-day ELM", MARGIN_USE * 100)
    qty = lots * lot
    stop_mult = float(p.get("stop_mult") or 0)
    stop_level = credit * stop_mult if stop_mult else None
    last_pre = max([d for d in data.dates if d < expiry], default=None)
    # --- walk the position: the entry day's remaining bars, then (multi-day) each later day
    outcome, exit_legs, exit_date, exit_time, flagged = None, None, None, None, False
    days_held = [day]
    if not is_exp:
        for d in data.dates:
            if d > day.date:
                dd = data.by_date.get(d)
                if dd is None:
                    continue
                days_held.append(dd)
                if d >= expiry:
                    break
    last_bar_today = EXPIRY_FLAT_SLOT if is_exp else FLAT_SLOT
    marks = []                      # (date, value of the structure at that day's close, real?)
    for di, dd in enumerate(days_held):
        d_is_exp = dd.date == expiry
        if di == 0:
            ps = paths
            start = k_entry + 1
        else:
            res2, _ = pricer.legs_paths(dd, expiry, legs, 0)
            ps = [r[0] for r in res2] if res2 else None
            start = 0
        last_bar = EXPIRY_FLAT_SLOT if d_is_exp else FLAT_SLOT
        if ps is not None and (stop_level or (exit_mode == "flat" and (di == 0 or d_is_exp))):
            for i in range(start, last_bar + 1):
                v = spread_value(ps, legs, i)
                if v is None:
                    continue
                if stop_level and v >= stop_level:
                    exit_legs = [leg_close(ps[j][i][3], legs[j]["side"], slip_mult) for j in range(len(legs))]
                    outcome, exit_date, exit_time = "stop", dd.date, bt.clock(bt.slot_end(i))
                    flagged = d_is_exp
                    break
            if outcome is None and exit_mode == "flat" and (di == 0 and not d_is_exp or d_is_exp):
                i = max(j for j in range(0, last_bar + 1) if ps[0][j] is not None)
                exit_legs = [leg_close(ps[j][i][3], legs[j]["side"], slip_mult) for j in range(len(legs))]
                outcome, exit_date, exit_time = "flat", dd.date, bt.clock(bt.slot_end(i))
                flagged = d_is_exp
        if outcome is None and exit_mode == "prev_close" and dd.date == last_pre:
            xs = []
            for leg in legs:
                row = dd.chain.row(SYM, expiry, leg["strike"], leg["type"])
                px = row and (row["close"] if row["close"] > 0 else row["settle"])
                if not px or px <= 0:
                    xs = None
                    break
                xs.append(leg_close(px, leg["side"], slip_mult))
            if xs is None:
                return None, "no bhavcopy close to exit at on %s" % dd.date
            exit_legs, outcome, exit_date, exit_time = xs, "prev_close", dd.date, "15:30"
        if outcome:
            break
        # still open at this day's close: mark at the REAL bhavcopy settle (intrinsic on expiry day)
        mk = 0.0
        ok = True
        for leg in legs:
            row = dd.chain.row(SYM, expiry, leg["strike"], leg["type"])
            if not row:
                ok = False
                break
            px = row["settle"] if row["settle"] and row["settle"] > 0 else row["close"]
            if d_is_exp:
                px = intrinsic(row["und"], leg["strike"], leg["type"])
            mk += px if leg["side"] == "short" else -px
        marks.append((dd.date, mk if ok else None))
        if d_is_exp:
            # settlement: every leg at its intrinsic value on the final settlement price
            row0 = dd.chain.row(SYM, expiry, legs[0]["strike"], legs[0]["type"])
            S_set = row0["und"] if row0 else None
            if not S_set:
                return None, "no settlement price on expiry day"
            exit_legs = [intrinsic(S_set, l["strike"], l["type"]) for l in legs]
            outcome, exit_date, exit_time = "settle", dd.date, "15:30"
            break
    if outcome is None:
        return None, "position never closed (data ends before the expiry)"
    settled = outcome == "settle"
    gross = charges = 0.0
    for leg, f, x in zip(legs, fills, exit_legs):
        gross += (f - x) * qty if leg["side"] == "short" else (x - f) * qty
        charges += leg_charges(leg, f, x, qty, exit_date, settled, x if settled else 0.0, ch_mult)
    gross, charges = round(gross, 2), round(charges, 2)
    net = round(gross - charges, 2)
    risk = max_loss_lot * lots
    real_exit = outcome in ("settle", "prev_close")
    # daily P&L for the account curve: real marks at each close, the exit on the exit day
    daily = {}
    prev_v = credit
    for d_, mk in marks:
        if mk is None:
            continue
        daily[d_] = daily.get(d_, 0.0) + (prev_v - mk) * qty
        prev_v = mk
    close_v = sum(x if l["side"] == "short" else -x for l, x in zip(legs, exit_legs))
    daily[exit_date] = daily.get(exit_date, 0.0) + (prev_v - close_v) * qty - charges
    if abs(sum(daily.values()) - net) > 1.0:
        daily = {exit_date: net}
    return {"symbol": SYM, "date": day.date, "time": bt.clock(bt.slot_end(k_entry)) if not use_open else "09:15",
            "seen_until": bt.clock(bt.slot_end(k_entry)) if not use_open else "09:15", "side": "short",
            "structure": structure, "expiry": expiry, "dte": data.clock.dte(day.d, ed), "expiry_day": is_exp,
            "legs": ["%s %g %s" % (l["side"], l["strike"], l["type"]) for l in legs], "underlying_px": S,
            "lot": lot, "lots": lots, "qty": qty, "margin_est": round(margin * lots, 2),
            "credit": round(credit, 2), "credit_mid": round(credit_mid, 2), "width": width,
            "entry": round(credit, 2), "stop": round(stop_level, 2) if stop_level else None, "target": 0.0,
            "exit": round(close_v, 2), "exit_date": exit_date, "exit_time": exit_time, "outcome": outcome,
            "model_exit": not real_exit, "expiry_day_model_exit": flagged, "risk": round(risk, 2),
            "R": round(gross / risk, 4), "R_net": round(net / risk, 4), "pnl": gross, "charges": charges,
            "pnl_net": net, "iv_source": "/".join(sorted(set(srcs))), "repriced_at_atm": sum(1 for x in srcs if x == "atm_iv"),
            "daily": daily, "why": "%s%s, short strikes %g sigma (%.0f pts), VIX %.2f" % (
                why_entry + "; " if why_entry else "", structure, p["k_sigma"], abs(legs[0]["strike"] - round_strike(S)),
                day.vix[k_entry]["c"] if day.vix and day.vix[k_entry] else float("nan"))}, None


def run_debit(spec, p, day, data, pricer, slip_mult, ch_mult):
    """The losing ORB option buy with the cost capped: buy ATM, sell `wing` points further out."""
    win = bt.Window({"flat_by": "15:05"})
    sig = op.sig_orb(day.sess, {"or_minutes": p.get("or_minutes", 15)}, win)
    if sig is None or isinstance(sig, str):
        return None, sig or "no breakout"
    k = sig["k"]
    expiry, ed = day.expiry, op.as_date(day.expiry)
    is_exp = expiry == day.date
    last = EXPIRY_FLAT_SLOT if is_exp else FLAT_SLOT
    if k >= last:
        return None, "signal too late"
    S = day.sess.bars[k]["c"]
    typ = sig["side"]
    atm = day.chain.atm(SYM, expiry, S)
    if atm is None:
        return None, "no strikes"
    w = float(p["wing"])
    legs = [{"strike": atm, "type": typ, "side": "long"},
            {"strike": atm + w if typ == "CE" else atm - w, "type": typ, "side": "short"}]
    res, why = pricer.legs_paths(day, expiry, legs, k)   # (long ATM, short wing): the ATM must be dearer
    if res is None:
        return None, why
    paths = [res[0][0], res[1][0]]
    srcs = [res[0][3], res[1][3]]
    lot = int(day.chain.row(SYM, expiry, atm, typ)["lot"] or 1)
    fills = [leg_fill(path, k, leg["side"], slip_mult)[0] for leg, path in zip(legs, paths)]
    if any(f is None for f in fills):
        return None, "no modelled bar at the entry"
    debit = fills[0] - fills[1]
    if debit <= 0:
        return None, "no net debit"
    lots = int((CAPITAL * DEBIT_ALLOC_PCT / 100.0) // (debit * lot))
    if lots < 1:
        return None, "one lot of the spread costs more than %g %% of capital" % DEBIT_ALLOC_PCT
    qty = lots * lot
    flat_only = p.get("exit", "stop_target") == "flat"
    stop = debit * (1 - p["stop_pct"] / 100.0) if p.get("stop_pct") and not flat_only else None
    target = debit * (1 + p["target_pct"] / 100.0) if p.get("target_pct") and not flat_only else None
    outcome = None
    for i in range(k + 1, last + 1):
        if paths[0][i] is None or paths[1][i] is None:
            continue
        v = paths[0][i][3] - paths[1][i][3]      # value of the long spread at the bar's close
        if stop is not None and v <= stop:
            outcome, xi = "stop", i
            break
        if target is not None and v >= target:
            outcome, xi = "target", i
            break
    if outcome is None:
        xi = max(j for j in range(k, last + 1) if paths[0][j] is not None and paths[1][j] is not None)
        outcome = "flat"
    exit_legs = [leg_close(paths[j][xi][3], legs[j]["side"], slip_mult) for j in range(2)]
    gross = charges = 0.0
    for leg, f, x in zip(legs, fills, exit_legs):
        gross += (x - f) * qty if leg["side"] == "long" else (f - x) * qty
        charges += leg_charges(leg, f, x, qty, day.date, False, 0.0, ch_mult)
    gross, charges = round(gross, 2), round(charges, 2)
    net = round(gross - charges, 2)
    risk = debit * qty
    close_v = exit_legs[0] - exit_legs[1]
    return {"symbol": SYM, "date": day.date, "time": bt.clock(bt.slot_end(k)), "seen_until": bt.clock(bt.slot_end(k)),
            "side": "long", "structure": "debit_orb", "expiry": expiry, "dte": data.clock.dte(day.d, ed),
            "expiry_day": is_exp, "legs": ["%s %g %s" % (l["side"], l["strike"], l["type"]) for l in legs],
            "underlying_px": S, "lot": lot, "lots": lots, "qty": qty, "margin_est": round(debit * qty, 2),
            "debit": round(debit, 2), "width": w, "entry": round(debit, 2), "stop": round(stop, 2) if stop else None,
            "target": round(target, 2) if target else None, "exit": round(close_v, 2), "exit_date": day.date,
            "exit_time": bt.clock(bt.slot_end(xi)), "outcome": outcome, "model_exit": True,
            "expiry_day_model_exit": is_exp, "risk": round(risk, 2), "R": round(gross / risk, 4),
            "R_net": round(net / risk, 4), "pnl": gross, "charges": charges, "pnl_net": net,
            "iv_source": "/".join(sorted(set(srcs))), "repriced_at_atm": sum(1 for x in srcs if x == "atm_iv"),
            "daily": {day.date: net}, "why": sig["why"]}, None


# ---------------------------------------------------------------- a variant over dates

def run_variant(spec, p, data, dates, slip_mult=1.0, ch_mult=1.0, shift=0.0, pricer=None):
    pricer = pricer or Pricer(data, shift)
    trades, skips = [], {}
    keep = set(dates)
    open_until = None       # one position at a time
    for d in data.dates:
        if d not in keep:
            continue
        day = data.by_date[d]
        if open_until and d <= open_until:
            continue
        if spec["structure"] == "debit_orb":
            t, why = run_debit(spec, p, day, data, pricer, slip_mult, ch_mult)
        else:
            dte = p.get("dte", 0)
            want = data.clock.dte(day.d, op.as_date(day.expiry))
            if dte != "any" and want != int(dte):
                continue
            if int(p.get("vix_filter") or 0):
                k0 = ENTRY_SLOT.get(p.get("entry", "09:30"), 2)
                v = day.vix[k0]["c"] if day.vix and day.vix[k0] is not None else None
                med = data.vix_median20(d)
                if v is None or med is None or v <= med:
                    skips["vix below its 20-session median"] = skips.get("vix below its 20-session median", 0) + 1
                    continue
            entry = p.get("entry", "09:30")
            structure, why_entry = spec["structure"], ""
            if entry == "orb":
                sig = op.sig_orb(day.sess, {"or_minutes": p.get("or_minutes", 15)}, bt.Window({"flat_by": "15:05"}))
                if sig is None or isinstance(sig, str):
                    skips[sig or "no breakout"] = skips.get(sig or "no breakout", 0) + 1
                    continue
                k0, use_open, why_entry = sig["k"], False, sig["why"]
                if structure == "vertical_0dte":
                    structure = "put_credit" if sig["side"] == "CE" else "call_credit"
            else:
                use_open = entry == "open"
                k0 = ENTRY_SLOT.get(entry, 2)
                if structure == "vertical_0dte":
                    structure = "put_credit"
            t, why = run_credit(structure, p, day, data, pricer, slip_mult, ch_mult, k0, use_open, why_entry)
        if t is None:
            skips[why] = skips.get(why, 0) + 1
            continue
        trades.append(t)
        open_until = t["exit_date"]
    return trades, {"skipped": skips, "calls": len(trades)}


def run_variants(spec, strat, data, dates, slip_mult=1.0, ch_mult=1.0, shift=0.0):
    pricer = Pricer(data, shift)
    out = []
    for p in strat.variants():
        tr, info = run_variant(spec, p, data, dates, slip_mult, ch_mult, shift, pricer)
        out.append({"params": p, "label": le.label(p), "trades": tr, "info": info})
    return out


def walk_forward(spec, strat, data, train, test, folds):
    rows = run_variants(spec, strat, data, sorted(set(train) | set(test)))
    trades, info, seen = [], [], list(train)
    for f in le.folds_of(test, folds):
        if not f:
            continue
        r, how = le.choose(rows, seen, strat.default())
        fs = set(f)
        ft = [t for t in r["trades"] if t["date"] in fs]
        trades += ft
        info.append({"from": f[0], "to": f[-1], "sessions": len(f), "chosen": r["label"], "params": r["params"],
                     "chosen_by": how, "trades": len(ft)})
        seen += f
    return trades, info, rows


def stress_run(spec, data, fold_info):
    out = []
    pricer = Pricer(data, 0.0)
    for f in fold_info:
        dates = data.dates_in(f["from"], f["to"])
        tr, _ = run_variant(spec, f["params"], data, dates, 2.0, 1.5, 0.0, pricer)
        out += tr
    return out


def shifted_runs(spec, data, fold_info):
    out = {}
    for shift in (-0.10, 0.10):
        trs = []
        pricer = Pricer(data, shift)
        for f in fold_info:
            dates = data.dates_in(f["from"], f["to"])
            tr, _ = run_variant(spec, f["params"], data, dates, 1.0, 1.0, shift, pricer)
            trs += tr
        s = rs.summary(trs)
        out["%+.0f%%" % (100 * shift)] = {"trades": s.get("trades", 0), "inr_per_trade": s.get("inr_per_trade"),
                                         "total_inr": s.get("total_inr"), "avg_R_net": s.get("avg_R_net")}
    return out


def account_of(trades, dates):
    daily = {}
    for t in trades:
        for d, v in t["daily"].items():
            daily[d] = daily.get(d, 0.0) + v
    fake = [{"date": d, "pnl_net": v} for d, v in daily.items()]
    acc = op.account(fake, sorted(set(dates) | set(daily)), CAPITAL)
    by_wk = {}
    for d, v in daily.items():
        wk = datetime.date.fromisoformat(d).isocalendar()[:2]
        by_wk[wk] = by_wk.get(wk, 0.0) + v
    acc["worst_week_inr"] = round(min(by_wk.values()), 2) if by_wk else 0.0
    acc["expiry_day_model_exits"] = sum(1 for t in trades if t.get("expiry_day_model_exit"))
    acc["model_exits"] = sum(1 for t in trades if t.get("model_exit"))
    acc["avg_margin_est"] = round(sum(t["margin_est"] for t in trades) / len(trades), 2) if trades else None
    acc["max_margin_est"] = max((t["margin_est"] for t in trades), default=None)
    acc.pop("equity_curve", None)
    return acc


def audit(trades, data):
    bad = [t for t in trades if t.get("seen_until") != t["time"]]
    return {"trades": len(trades), "decided_after_entry": len(bad), "max_bar_date": data.max_bar_date,
            "cutoff": data.cutoff, "loader_ok": (data.max_bar_date or "") < data.cutoff, "violations": 0,
            "calls": len(trades)}


# ---------------------------------------------------------------- commands (registry-compatible)

def load_strat(exp_id):
    path = rs.lab_path(exp_id)
    strat = le.load_strategy(path, exp_id)
    spec = getattr(strat.module, "SPREAD", None)
    if not spec or "structure" not in spec:
        raise SpreadError("%s has no SPREAD = {'structure': ...}" % path)
    return strat, spec


def cmd_split(a):
    rec = split_rec()
    print(json.dumps(rec, indent=1))
    return 0


def cmd_explore(a):
    recs, e = rs.get_exp(a.id)
    if e["status"] not in ("idea", "tested"):
        raise rs.ResearchError("%s is %s: a new hypothesis needs a new id" % (a.id, e["status"]))
    strat, spec = load_strat(a.id)
    if e["validate"] and e["validate"][-1]["sha"] != strat.sha:
        raise rs.ResearchError("%s changed after validate froze it: log a new id" % a.id)
    data = Data("explore")
    dates = data.dates_in(*data.split_rec["explore"])
    rows = run_variants(spec, strat, data, dates)
    gs = rs.grid_sha(strat)
    counted = not any(r["id"] == a.id and r["sha"] == strat.sha and r["grid_sha"] == gs for r in recs if r["op"] == "explore")
    variants = [dict(rs.summary(r["trades"]), label=r["label"], params=r["params"], skipped=r["info"]["skipped"],
                     account=account_of(r["trades"], dates) if r["trades"] else None) for r in rows]
    ok = [v for v in variants if v["trades"] >= le.MIN_SELECT_TRADES]
    best = max(ok, key=lambda v: (v["avg_R_net"], v["trades"])) if ok else None
    rec = {"op": "explore", "id": a.id, "sha": strat.sha, "grid_sha": gs, "trials": len(rows), "counted": counted,
           "dataset": DATASET, "window": [dates[0], dates[-1]], "sessions": len(dates), "symbols": 1,
           "variants": [dict((k, v) for k, v in x.items() if k not in ("account", "skipped")) for x in variants],
           "best": best["label"] if best else None, "calls": sum(r["info"]["calls"] for r in rows),
           "engine": "lab/_spreads.py (options spreads; prices modelled by india-backtest/options.py)"}
    rs.save_run(a.id, "explore", {"made": nse.iso_ist(a.now_dt), "sha": strat.sha, "dataset": DATASET,
                                  "window": rec["window"], "engine": rec["engine"],
                                  "variants": [{"label": r["label"], "params": r["params"], "trades": r["trades"],
                                                "info": r["info"]} for r in rows]})
    rs.append(rec, a.now_dt)
    if e["status"] == "idea":
        rs.set_status(a.id, "tested", "explored %d variant(s) (options spread engine)" % len(rows), a.now_dt)
    recs, _ = rs.read_registry()
    print("Explore %s on %s: %s to %s (%d sessions). %d variant(s) %s; registry total %d trials." % (
        a.id, DATASET, dates[0], dates[-1], len(dates), len(rows),
        "counted" if counted else "not counted again", rs.total_trials(recs)))
    print("  %-40s %6s %6s %8s %9s %9s %7s %7s %6s" % ("variant", "trades", "win%", "R net", "Rs/trade", "total", "DD%", "ruin%", "worst"))
    for v in variants:
        acc = v.get("account") or {}
        print("  %-40s %6d %6s %8s %9s %9s %7s %7s %6s%s" % (
            v["label"][:40], v["trades"], rs.fr(v.get("win_pct"), "%.1f"), rs.fr(v.get("avg_R_net")),
            rs.fr(v.get("inr_per_trade"), "%+.0f"), rs.fr(v.get("total_inr"), "%+.0f"),
            rs.fr(acc.get("max_dd_pct"), "%.1f"), rs.fr(acc.get("ruin_windows_pct"), "%.1f"),
            rs.fr((acc.get("worst_day") or {}).get("inr"), "%+.0f"), "  <- best" if best and v is best else ""))
        if v.get("skipped"):
            print("      skipped: " + ", ".join("%s x%d" % kv for kv in sorted(v["skipped"].items(), key=lambda kv: -kv[1])[:4]))
    return 0


def cmd_validate(a):
    recs, e = rs.get_exp(a.id)
    if e["claim"]:
        raise rs.ResearchError("%s has used its holdout: validate is closed" % a.id)
    strat, spec = load_strat(a.id)
    if not any(r["sha"] == strat.sha for r in e["explore"]):
        raise rs.ResearchError("%s: explore this exact lab file first" % a.id)
    if e["validate"] and e["validate"][0]["sha"] != strat.sha:
        raise rs.ResearchError("%s changed after validate froze it: log a new id" % a.id)
    data = Data("validate")
    sp = data.split_rec
    train, test = data.dates_in(*sp["explore"]), data.dates_in(*sp["validate"])
    trades, folds, rows = walk_forward(spec, strat, data, train, test, a.folds)
    stress = stress_run(spec, data, folds)
    sens = shifted_runs(spec, data, folds)
    look = audit(trades, data)
    s = rs.summary(trades)
    doc = {"made": nse.iso_ist(a.now_dt), "sha": strat.sha, "dataset": DATASET, "split": "validate", "dates": test,
           "train": [train[0], train[-1]], "folds": folds, "trades": trades, "stress_trades": stress,
           "benchmark": data.benchmark(test), "look_ahead": look, "capital": CAPITAL, "kind": strat.kind,
           "account": account_of(trades, test), "model_sensitivity": sens,
           "engine": "lab/_spreads.py (options spreads; prices modelled by india-backtest/options.py)"}
    rs.save_run(a.id, "validate", doc)
    rs.append({"op": "validate", "id": a.id, "sha": strat.sha, "dataset": DATASET, "window": [test[0], test[-1]],
               "sessions": len(test), "folds": folds, "summary": s, "stress": rs.summary(stress),
               "model_sensitivity": sens, "account": doc["account"]}, a.now_dt)
    print("Validate %s (anchored walk-forward, %d fold(s)) on %s to %s (%d sessions):" % (a.id, len(folds), test[0], test[-1], len(test)))
    for f in folds:
        print("  fold %s..%s: traded %s (%s), %d trades" % (f["from"], f["to"], f["chosen"], f["chosen_by"], f["trades"]))
    print("  %d trades, win %s %%, net R %s a trade, %s a trade, %s in all; stress %s/trade; model %s" % (
        s.get("trades", 0), rs.fr(s.get("win_pct"), "%.1f"), rs.fr(s.get("avg_R_net")), nse.inr(s.get("inr_per_trade") or 0),
        nse.inr(s.get("total_inr") or 0), nse.inr(rs.summary(stress).get("inr_per_trade") or 0),
        ", ".join("%s: %s/trade" % (k, nse.inr(v["inr_per_trade"] or 0)) for k, v in sens.items())))
    acc = doc["account"]
    print("  account: max DD %.1f %%, worst day %s, worst week %s, ruin windows %s %%, margin est max %s" % (
        acc["max_dd_pct"], nse.inr(acc["worst_day"]["inr"]), nse.inr(acc["worst_week_inr"]),
        rs.fr(acc["ruin_windows_pct"], "%.1f"), nse.inr(acc["max_margin_est"] or 0)))
    return 0


def cmd_gate(a):
    recs, e = rs.get_exp(a.id)
    if e["claim"]:
        raise rs.ResearchError("%s has used its holdout: its validate gates are final" % a.id)
    doc = nse.read_json(rs.runs_path(a.id, "validate"))
    if not doc or not e["validate"]:
        raise rs.ResearchError("%s has no validate run" % a.id)
    strat, spec = load_strat(a.id)
    if doc["sha"] != strat.sha or e["validate"][-1]["sha"] != strat.sha:
        raise rs.ResearchError("%s changed after validate: log it as a new id" % a.id)
    gs = rs.run_gates(doc, recs)
    n = rs.total_trials(recs)
    k = rs.print_gates(a.id, "validate", doc, gs, n)
    allp = k == len(gs)
    rs.append({"op": "gate", "id": a.id, "split": "validate", "sha": strat.sha, "trials_at_gate": n,
               "gates": dict(gs), "passed": k, "of": len(gs), "all_pass": allp}, a.now_dt)
    status = "candidate" if allp else "rejected"
    if e["status"] != status:
        rs.set_status(a.id, status, "%d/%d gates on validate (options spread engine)" % (k, len(gs)), a.now_dt)
    print("Result: %d/%d gates passed -> %s." % (k, len(gs), status.upper()))
    return 0


def cmd_holdout(a):
    recs, e = rs.get_exp(a.id)
    if e["claim"] or e["holdout"]:
        raise rs.ResearchError("%s already used its holdout; it is never re-run" % a.id)
    if e["status"] != "candidate":
        raise rs.ResearchError("%s is %s: only a candidate may open the holdout" % (a.id, e["status"]))
    strat, spec = load_strat(a.id)
    if strat.sha != e["validate"][-1]["sha"]:
        raise rs.ResearchError("%s changed since validate" % a.id)
    rs.append({"op": "holdout_claim", "id": a.id, "sha": strat.sha}, a.now_dt)
    ticket = le.HoldoutTicket(a.id, rs.reg_path())
    data = Data("holdout", ticket)
    sp = data.split_rec
    pre = data.dates_in(sp["explore"][0], sp["validate"][1])
    hold = data.dates_in(*sp["holdout"])
    rows = run_variants(spec, strat, data, pre)
    chosen, how = le.choose(rows, pre, strat.default())
    fold = [{"from": hold[0], "to": hold[-1], "params": chosen["params"]}]
    pricer = Pricer(data, 0.0)
    trades, _ = run_variant(spec, chosen["params"], data, hold, 1.0, 1.0, 0.0, pricer)
    stress = stress_run(spec, data, fold)
    sens = shifted_runs(spec, data, fold)
    look = audit(trades, data)
    doc = {"made": nse.iso_ist(a.now_dt), "sha": strat.sha, "dataset": DATASET, "split": "holdout", "dates": hold,
           "chosen": chosen["label"], "chosen_by": how, "trades": trades, "stress_trades": stress,
           "benchmark": data.benchmark(hold), "look_ahead": look, "capital": CAPITAL, "kind": strat.kind,
           "account": account_of(trades, hold), "model_sensitivity": sens}
    rs.save_run(a.id, "holdout", doc)
    recs, _ = rs.read_registry()
    gs = rs.run_gates(doc, recs)
    k = rs.print_gates(a.id, "HOLDOUT", doc, gs, rs.total_trials(recs))
    ok = k == len(gs)
    rs.append({"op": "holdout", "id": a.id, "sha": strat.sha, "chosen": chosen["label"], "summary": rs.summary(trades),
               "gates": dict(gs), "passed": k, "of": len(gs), "all_pass": ok, "model_sensitivity": sens,
               "account": doc["account"]}, a.now_dt)
    rs.set_status(a.id, "holdout-passed" if ok else "holdout-failed", "%d/%d gates on the holdout" % (k, len(gs)), a.now_dt)
    print("Holdout %s: %s (%d/%d gates). Recorded permanently." % (a.id, "PASSED" if ok else "FAILED", k, len(gs)))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--trader-dir")
    ap.add_argument("--now")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("split")
    for name in ("explore", "validate", "gate", "holdout"):
        p = sub.add_parser(name)
        p.add_argument("id")
        if name == "validate":
            p.add_argument("--folds", type=int, default=rs.FOLDS)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    if a.trader_dir:
        os.environ["TRADER_DIR"] = os.path.abspath(a.trader_dir)
    a.now_dt = nse.parse_now(a.now) if a.now else nse.now_ist().replace(microsecond=0)
    try:
        return {"split": cmd_split, "explore": cmd_explore, "validate": cmd_validate, "gate": cmd_gate,
                "holdout": cmd_holdout}[a.cmd](a)
    except (nse.NSEError, ValueError) as e:
        print("_spreads.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
