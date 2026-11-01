#!/usr/bin/env python3
"""Exit Planner for india-exit: sets the stop and the target BEFORE you
enter, so you know when to get out. Standard library only. Long side only.
It never places an order.

    python3 exit.py plan                                    # from trader/entry.json: or-mid stop, 2R target
    python3 exit.py plan --write                            # also save trader/plan.json
    python3 exit.py plan --symbol RELIANCE --entry 1174.30 --or-high 1170.50 --or-low 1156.20
    python3 exit.py plan --stop atr:1.5 --offline RELIANCE.json   # ATR needs the session's bars
    python3 exit.py plan --symbol IDEA --entry 10.42 --setup level --stop pct:1
    python3 exit.py show                                    # the current trader/plan.json

Stops (a long's stop rounds DOWN to the stock's NSE tick, away from the entry):
  or-mid     the opening-range midpoint (the default for an orb entry)
  or-low     the opening-range low
  price:X    your price
  pct:X      X percent under the entry
  atr:K      K times the 14-bar ATR under the entry, on the session's bars up
             to the entry bar (--offline FILE or Yahoo). With fewer than 14
             bars by then it averages all of them and says so.
  A level setup has no opening-range default: give a stop.

Targets (round DOWN to the tick too, so the price is one the market can print
and the R shown is never flattered):
  2R (default), 1R, 1.5R ...   entry + R x risk, where risk = entry - stop
  price:X                      your price

The tick: --tick, else Dhan's instrument file (trader/cache/dhan-nse-equity.json
when india-common has cached it, or --scrip FILE, the api-scrip-master.csv),
else NSE's price band (below 250: 0.01, to 1,000: 0.05, to 5,000: 0.10 ...).

Refused, with the reason: a stop at or above the entry, a target at or below
it, a risk under the larger of 3 ticks and 0.05% of the price (too tight to
survive the spread), an entry.json that has not triggered (unless you give
--entry), a short (this pack plans longs only).

Charges: Dhan intraday charges for one round trip (nse.charges), for the
reference quantity floor(max_loss_per_trade / risk) (or --qty), shown per
share at the stop and at the target, and the R after costs:
  net R = ((exit - entry) x qty - charges) / (risk x qty).

flat_by: nse.flat_by(config) -- 15:05 IST by default, never after Dhan's 15:10
square-off. An entry at or after it is refused. "min_reward_risk" in the
config (default 1.5) only warns here; india-risk enforces it.

A stop is a plan, not a guarantee: a gap or a fast market can fill past it.
Exit codes: plan 0 = a plan, 1 = refused or an error. show 0 = a plan exists.
"""

import argparse
import datetime
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "india-common"))
import nse  # noqa: E402

INTERVALS = {"1m": 60, "5m": 300}
ATR_BARS = 14
MIN_RISK_PCT = 0.05      # risk under 0.05% of the price is inside the spread's noise
MIN_TICKS = 3            # and never fewer than 3 ticks
DEFAULT_MIN_RR = 1.5
DEFAULT_MAX_LOSS = 500.0
NOTE = "A stop is a plan, not a guarantee: a gap or a fast market can fill past it."
WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


class PlanError(nse.NSEError):
    pass


# ---------------------------------------------------------------- small helpers

def num(x):
    """1, 1.5, 2.25: no trailing zeros."""
    s = ("%.4f" % float(x)).rstrip("0").rstrip(".")
    return s or "0"


def ratio(x):
    s = "%.2f" % float(x)
    return s[:-1] if s.endswith("0") else s


def p2(x):
    return round(float(x) + 1e-9, 2) + 0.0


def rupees(x):
    return nse.inr(x)


def parse_hhmm(text, what):
    try:
        return nse.hhmm(text)
    except (nse.NSEError, ValueError):
        raise PlanError("%s wants an IST time like 15:05, not %r" % (what, text))


def parse_stop(text):
    t = str(text).strip().lower()
    if t in ("or-mid", "or_mid", "mid", "or-midpoint"):
        return "or_mid", None
    if t in ("or-low", "or_low", "low"):
        return "or_low", None
    m = re.match(r"^(price|pct|atr):\s*(?:rs\.?|₹)?\s*([0-9][0-9,]*\.?[0-9]*)$", t)
    if not m:
        raise PlanError("--stop is or-mid, or-low, price:X, pct:X or atr:K (got %r)" % text)
    kind, val = m.group(1), float(m.group(2).replace(",", ""))
    if val <= 0:
        raise PlanError("--stop %s needs a number above 0" % kind)
    if kind == "pct" and val >= 100:
        raise PlanError("--stop pct:X needs X under 100")
    return kind, val


def parse_target(text):
    t = str(text).strip()
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*[Rr]$", t)
    if m:
        r = float(m.group(1))
        if r <= 0:
            raise PlanError("--target needs an R multiple above 0, like 1R or 2R")
        return "r", r
    m = re.match(r"^price:\s*(?:rs\.?|₹)?\s*([0-9][0-9,]*\.?[0-9]*)$", t.lower())
    if m:
        return "price", float(m.group(1).replace(",", ""))
    raise PlanError("--target is 2R, 1R, 1.5R ... or price:X (got %r)" % text)


def r_label(r):
    n = int(r) if float(r).is_integer() else None
    return "%sR (%s times the risk)" % (num(r), WORDS.get(n, num(r)))


def rule_name(stop_rule):
    return stop_rule.replace("_", "-")


# ---------------------------------------------------------------- the math

def compute(entry, stop_rule, stop_param, target_rule, target_param, or_high=None, or_low=None,
            atr=None, atr_bars=None, tick=None):
    """The stop, the target and the risk on the NSE tick. Raises PlanError with the reason."""
    e = p2(entry)
    if e <= 0:
        raise PlanError("the entry price must be above Rs 0")
    tick = tick or nse.band_tick(e)
    detail = ""
    if stop_rule in ("or_mid", "or_low"):
        if or_high is None or or_low is None:
            raise PlanError("an %s stop needs the opening range (--or-high and --or-low)" % rule_name(stop_rule))
        oh, ol = p2(or_high), p2(or_low)
        if oh <= ol:
            raise PlanError("the opening-range high %s must be above its low %s" % (rupees(oh), rupees(ol)))
        if stop_rule == "or_mid":
            raw = (oh + ol) / 2.0
            label = "opening-range midpoint"
            detail = "(%s + %s) / 2 = %s" % (rupees(oh), rupees(ol), num(raw))
        else:
            raw, label = ol, "opening-range low"
    elif stop_rule == "price":
        raw, label = float(stop_param), "your price"
    elif stop_rule == "pct":
        raw = e * (1 - stop_param / 100.0)
        label = "%s%% under the entry" % num(stop_param)
        detail = "%s x (1 - %s%%) = %s" % (rupees(e), num(stop_param), num(raw))
    elif stop_rule == "atr":
        if atr is None:
            raise PlanError("an atr stop needs the session's bars")
        raw = e - stop_param * atr
        label = "%s x ATR(%d) under the entry" % (num(stop_param), atr_bars)
        detail = "ATR %s over the %d bars to the entry%s" % (
            "%.4f" % atr, atr_bars, " (fewer than 14 so far)" if atr_bars < ATR_BARS else "")
    else:
        raise PlanError("unknown stop rule %r" % stop_rule)
    stop = nse.round_down(raw, tick)
    if abs(stop - raw) > 1e-6:
        detail = (detail + ", rounded down to the %s tick" % num(tick)) if detail else \
            "%s rounded down to the %s tick" % (num(raw), num(tick))
    if stop >= e:
        raise PlanError("the stop %s (%s) is at or above the entry %s: a long's stop goes under the entry"
                        % (rupees(stop), label, rupees(e)))
    if stop <= 0:
        raise PlanError("the stop works out to %s: pick a stop above Rs 0" % rupees(stop))
    risk = p2(e - stop)
    floor_risk = max(MIN_TICKS * tick, e * MIN_RISK_PCT / 100.0)
    if risk < floor_risk - 1e-9:
        raise PlanError("the stop is %s under the entry, less than %s (the larger of %d ticks of %s and 0.05%% of "
                        "%s): too tight to survive the spread, normal wiggle would stop you out. Widen it."
                        % (rupees(risk), rupees(floor_risk), MIN_TICKS, num(tick), rupees(e)))
    if target_rule == "r":
        raw_t = e + target_param * risk
        target = nse.round_down(raw_t, tick)
        t_label = r_label(target_param)
    else:
        raw_t = float(target_param)
        target = nse.round_down(raw_t, tick)
        t_label = None
    if target <= e:
        raise PlanError("the target %s is at or below the entry %s: a long's target goes above it"
                        % (rupees(target), rupees(e)))
    rr = round((target - e) / risk + 1e-9, 2)
    if t_label is None:
        t_label = "your price (%sR)" % ratio(rr)
    elif abs(target - raw_t) > 1e-6:
        t_label += ", rounded down to the tick"
    return {"entry": e, "stop": stop, "stop_label": label, "stop_detail": detail, "target": target,
            "target_r": target_param if target_rule == "r" else rr, "target_label": t_label,
            "risk": risk, "reward_risk": rr, "tick": tick}


def charges_view(entry, stop, target, qty, exchange="NSE"):
    """Round-trip charges (Dhan intraday) at the stop and at the target for qty shares."""
    qty = max(1, int(qty))
    risk = entry - stop
    c_stop = nse.charges(qty * entry, qty * stop, exchange)["total"]
    c_tgt = nse.charges(qty * entry, qty * target, exchange)["total"]
    net_stop = (-(risk * qty) - c_stop) / (risk * qty)
    net_tgt = ((target - entry) * qty - c_tgt) / (risk * qty)
    return {"qty": qty, "at_stop": c_stop, "at_target": c_tgt,
            "per_share_at_stop": round(c_stop / qty, 2), "per_share_at_target": round(c_tgt / qty, 2),
            "r_net_at_stop": round(net_stop, 2), "r_net_at_target": round(net_tgt, 2),
            "reward_risk_net": round(net_tgt / -net_stop, 2) if net_stop < 0 else None}


def reference_qty(cfg, risk, given=None):
    if given:
        return int(given), "your --qty"
    try:
        ml = float(cfg.get("max_loss_per_trade") or DEFAULT_MAX_LOSS)
    except (TypeError, ValueError):
        ml = DEFAULT_MAX_LOSS
    q = max(1, int(math.floor(ml / risk + 1e-9)))
    return q, "max_loss_per_trade %s / risk %s" % (rupees(ml), num(risk))


# ---------------------------------------------------------------- ticks

def find_tick(sym, price, a, base=None):
    """(tick, where): --tick, Dhan's instrument file, else NSE's band."""
    if a.tick:
        return float(a.tick), "your --tick"
    scrip = None
    if a.scrip:
        try:
            scrip = nse.parse_dhan_scrip(nse.read_maybe_gz(a.scrip).decode("utf-8", "replace"))
        except OSError as e:
            raise PlanError("could not read %s: %s" % (a.scrip, e))
    else:
        cache = os.path.join(nse.trader_dir(base), "cache", "dhan-nse-equity.json")
        if os.path.exists(cache):
            try:
                scrip = nse.read_json(cache)
            except nse.NSEError:
                scrip = None
    if scrip:
        row = scrip.get(sym)
        if row and row.get("tick"):
            return float(row["tick"]), "Dhan instrument file"
    return nse.band_tick(price), "NSE price band"


# ---------------------------------------------------------------- bars for ATR

def load_bars(sym, interval, day, offline=None, exchange="NSE"):
    if offline:
        path = offline
        if os.path.isdir(offline):
            for name in (sym + ".json", sym + ".json.gz", sym + ".NS.json", sym + ".NS.json.gz"):
                if os.path.exists(os.path.join(offline, name)):
                    path = os.path.join(offline, name)
                    break
        if not os.path.exists(path) or os.path.isdir(path):
            raise PlanError("no offline file %s" % path)
        raw = nse.read_maybe_gz(path)
    else:
        back = (nse.now_ist().date() - day).days
        rng = "1d" if back <= 0 else "5d" if back <= 6 else "1mo" if back <= 28 else "60d"
        if interval == "1m" and back > 6:
            raise PlanError("Yahoo keeps 1-minute bars for about a week")
        raw = nse.http_get(nse.yahoo_chart_url(sym, rng, interval, exchange=exchange))
    try:
        meta, bars = nse.parse_yahoo_chart(raw)
    except ValueError:
        raise PlanError("Yahoo returned something unexpected for %s" % sym)
    if nse.clean_symbol(meta.get("symbol") or sym) != sym:
        raise PlanError("the chart file holds %s, not %s" % (meta.get("symbol"), sym))
    rmt = meta.get("regularMarketTime")
    return {"bars": bars, "data_time": nse.ist_from_epoch(rmt) if rmt else None}


def session_bars(chart, day, interval, upto_slot=None, now=None, cfg=None):
    """Closed, on-grid bars of one NSE session, oldest first (the live tick is dropped)."""
    step = INTERVALS[interval]
    s = nse.session(day, cfg)
    if not s:
        return []
    open_dt, close_dt = s
    if now is None:
        now = chart.get("data_time")
    out = {}
    for b in chart["bars"]:
        t = b["t"]
        if t.date() != day or not (open_dt <= t < close_dt) or t.second:
            continue
        off = int((t - open_dt).total_seconds())
        if off % step:
            continue
        if now and t + datetime.timedelta(seconds=step) > now:
            continue
        slot = off // step
        if upto_slot is not None and slot > upto_slot:
            continue
        out[slot] = b
    return [out[k] for k in sorted(out)]


def atr_of(bars, n=ATR_BARS):
    """Average true range of the last n bars (all of them if fewer). The first
    bar of the session uses its own high - low (no overnight gap)."""
    trs, prev = [], None
    for b in bars:
        tr = b["h"] - b["l"]
        if prev is not None:
            tr = max(tr, abs(b["h"] - prev), abs(b["l"] - prev))
        trs.append(tr)
        prev = b["c"]
    use = trs[-n:]
    return sum(use) / len(use), len(use)


# ---------------------------------------------------------------- plan

def read_config(path):
    try:
        return nse.load_config(path)
    except nse.NSEError as e:
        raise PlanError(str(e))


def session_flat_by(cfg, day):
    """nse.flat_by, except on an evening special session (Muhurat) where the default
    15:05 would fall before the session opens: then 5 minutes before its close."""
    fb = nse.flat_by(cfg, day)
    sp = nse.special_session(day, cfg)
    if sp and not (sp[0] < fb <= sp[1]):
        fb = (datetime.datetime.combine(day, sp[1]) - datetime.timedelta(minutes=5)).time()
    return fb


def resolve_flat_by(day, explicit, cfg, entry_time):
    """(HH:MM, note). nse.flat_by caps it at Dhan's 15:10 square-off."""
    c = dict(cfg)
    note = ""
    if explicit:
        t = parse_hhmm(explicit, "--flat-by")
        c["flat_by"] = "%02d:%02d" % (t.hour, t.minute)
    elif cfg.get("flat_by"):
        parse_hhmm(cfg["flat_by"], "flat_by")
    want = parse_hhmm(c.get("flat_by") or nse.DEFAULT_FLAT_BY, "flat_by")
    fb = session_flat_by(c, day)
    if fb < want and not nse.special_session(day, cfg):
        note = "moved up to %s: Dhan squares off equity intraday positions at 15:10" % fb.strftime("%H:%M")
    elif nse.special_session(day, cfg):
        note = "%s on the special session (5 minutes before its close)" % fb.strftime("%H:%M")
    if entry_time and entry_time >= fb:
        raise PlanError("the entry at %s is at or after your flat-by %s: there is no time left to hold it"
                        % (entry_time.strftime("%H:%M"), fb.strftime("%H:%M")))
    return fb.strftime("%H:%M"), note


def gather(a):
    """The entry facts, from trader/entry.json or the flags."""
    if a.symbol and a.from_path:
        raise PlanError("use --from or --symbol, not both")
    if a.side in ("sell", "short"):
        raise PlanError("this pack plans longs only: no stops above a short entry. Plan a buy instead.")
    if a.symbol:
        if a.entry is None:
            raise PlanError("--symbol needs --entry PRICE")
        if (a.or_high is None) != (a.or_low is None):
            raise PlanError("give both --or-high and --or-low, or neither")
        info = {"symbol": nse.clean_symbol(a.symbol), "exchange": "NSE", "entry": a.entry,
                "setup": a.setup or ("orb" if a.or_high is not None else "level"),
                "date": a.date, "entry_time": a.time, "entry_bar": a.entry_bar, "interval": a.interval or "5m",
                "or_high": a.or_high, "or_low": a.or_low, "or_minutes": a.or_minutes, "source": "flags"}
    else:
        path = a.from_path or os.path.join(nse.trader_dir(), "entry.json")
        if not os.path.exists(path):
            raise PlanError("no entry at %s (run /india-entry first, or give --symbol and --entry)" % path)
        try:
            doc = nse.read_json(path) or {}
        except nse.NSEError as e:
            raise PlanError(str(e))
        if str(doc.get("side") or "long").lower() not in ("buy", "long"):
            raise PlanError("%s is a %s: this pack plans longs only" % (path, doc.get("side")))
        if doc.get("status") != "triggered" and a.entry is None:
            raise PlanError("%s says %s is %s, not triggered: plan once it triggers, or give --entry PRICE to plan "
                            "ahead" % (path, doc.get("symbol"), doc.get("status") or "unknown"))
        orr = doc.get("opening_range") or {}
        info = {"symbol": nse.clean_symbol(doc.get("symbol") or ""), "exchange": doc.get("exchange") or "NSE",
                "entry": a.entry if a.entry is not None else doc.get("price"),
                "setup": a.setup or doc.get("setup") or "orb", "date": a.date or doc.get("date"),
                "entry_time": a.time or (doc.get("time") if a.entry is None else None),
                "entry_bar": a.entry_bar if a.entry_bar is not None else (doc.get("bar_index") if a.entry is None else None),
                "interval": a.interval or doc.get("interval") or "5m",
                "or_high": a.or_high if a.or_high is not None else orr.get("high"),
                "or_low": a.or_low if a.or_low is not None else orr.get("low"),
                "or_minutes": orr.get("minutes") or a.or_minutes, "source": path}
        if info["entry"] is None:
            raise PlanError("%s has no price" % path)
    if info["interval"] not in INTERVALS:
        raise PlanError("interval is 5m or 1m")
    return info


def build_plan(a, now):
    info = gather(a)
    cfg = read_config(a.config)
    stop_text = a.stop
    if not stop_text:
        if info["setup"] != "orb":
            raise PlanError("a %s setup has no opening-range stop to default to: give --stop price:X, pct:X or atr:K"
                            % info["setup"])
        stop_text = "or-mid"
    stop_rule, stop_param = parse_stop(stop_text)
    target_text = a.target or "2R"
    target_rule, target_param = parse_target(target_text)
    if info["date"]:
        try:
            day = datetime.date.fromisoformat(str(info["date"]))
        except ValueError:
            raise PlanError("date wants YYYY-MM-DD, not %r" % info["date"])
    else:
        day = now.date()
    entry_time = parse_hhmm(info["entry_time"], "the entry time") if info["entry_time"] else None
    atr = atr_n = None
    if stop_rule == "atr":
        chart = load_bars(info["symbol"], info["interval"], day, a.offline, info["exchange"])
        bars = session_bars(chart, day, info["interval"], info["entry_bar"], parse_now(a.now) if a.now else None, cfg)
        if len(bars) < 3:
            raise PlanError("an atr stop needs at least 3 closed %s bars of %s on %s by the entry; found %d"
                            % (info["interval"], info["symbol"], day, len(bars)))
        atr, atr_n = atr_of(bars)
    tick, tick_from = find_tick(info["symbol"], float(info["entry"]), a)
    c = compute(info["entry"], stop_rule, stop_param, target_rule, target_param, info["or_high"], info["or_low"],
                atr, atr_n, tick)
    flat_by, flat_note = resolve_flat_by(day, a.flat_by, cfg, entry_time)
    qty, qty_from = reference_qty(cfg, c["risk"], a.qty)
    ch = charges_view(c["entry"], c["stop"], c["target"], qty, info["exchange"])
    ch["qty_from"] = qty_from
    orr = None
    if info["or_high"] is not None and info["or_low"] is not None:
        orr = {"high": p2(info["or_high"]), "low": p2(info["or_low"]), "minutes": int(info["or_minutes"] or 15)}
    tr = ("%sR" % num(target_param)) if target_rule == "r" else "price"
    plan = {"created_by": "india-exit", "symbol": info["symbol"], "exchange": info["exchange"], "side": "long",
            "date": day.isoformat(), "entry": c["entry"], "stop": c["stop"], "target": c["target"], "tick": tick,
            "risk_per_share": c["risk"], "r_multiple": float(c["target_r"]), "stop_rule": rule_name(stop_rule),
            "target_rule": tr, "flat_by": flat_by, "opening_range": orr, "made": nse.iso_ist(now),
            "setup": info["setup"], "interval": info["interval"],
            "entry_time": entry_time.strftime("%H:%M") if entry_time else None, "entry_bar": info["entry_bar"],
            "reward_risk": c["reward_risk"], "stop_label": c["stop_label"], "stop_detail": c["stop_detail"],
            "target_label": c["target_label"], "tick_from": tick_from, "charges": ch, "note": NOTE}
    if atr is not None:
        plan["atr"] = round(atr, 4)
        plan["atr_bars"] = atr_n
    warnings = []
    if flat_note:
        warnings.append("Flat-by %s: %s." % (flat_by, flat_note))
    try:
        mrr = float(cfg.get("min_reward_risk", DEFAULT_MIN_RR))
    except (TypeError, ValueError):
        mrr = DEFAULT_MIN_RR
    if c["reward_risk"] < mrr:
        warnings.append("Reward:risk %s is under your min_reward_risk %s: india-risk checks that."
                        % (ratio(c["reward_risk"]), num(mrr)))
    elif ch["reward_risk_net"] is not None and ch["reward_risk_net"] < mrr:
        warnings.append("After charges the reward:risk is %s, under your min_reward_risk %s."
                        % (ratio(ch["reward_risk_net"]), num(mrr)))
    if tick_from == "NSE price band":
        warnings.append("Tick %s from NSE's price band (Dhan's instrument file was not cached): NSE sets each "
                        "stock's tick from last month's closing price, so check it near a band edge." % num(tick))
    if day != now.date():
        warnings.append("This plan is for %s, not today (%s)." % (day.isoformat(), now.date().isoformat()))
    if not nse.is_trading_day(day, cfg):
        warnings.append("%s is not an NSE trading day in this pack's calendar." % day.isoformat())
    return plan, warnings


def describe(plan):
    day = datetime.date.fromisoformat(plan["date"])
    setup = {"orb": "opening-range breakout", "level": "close above a level"}.get(plan.get("setup"), plan.get("setup"))
    e, risk = plan["entry"], plan["risk_per_share"]
    pct = round(100.0 * risk / e, 2)
    when = ""
    if plan.get("entry_time"):
        when = " at %s IST" % plan["entry_time"]
        if plan.get("entry_bar") is not None:
            when += " (bar %d)" % plan["entry_bar"]
    out = ["Exit plan: %s long (%s), %s, %s on %s bars" % (
        plan["symbol"], plan.get("exchange", "NSE"), day.strftime("%a %Y-%m-%d"), setup,
        "5-minute" if plan.get("interval", "5m") == "5m" else "1-minute")]
    out.append("  Entry         %s%s" % (rupees(e), when))
    out.append("  Stop          %s  %s%s" % (rupees(plan["stop"]), plan.get("stop_label", ""),
                                             (": " + plan["stop_detail"]) if plan.get("stop_detail") else ""))
    out.append("  Target        %s  %s" % (rupees(plan["target"]), plan.get("target_label", "")))
    out.append("  Risk a share  %s (%s%% of the entry), tick %s" % (rupees(risk), "%.2f" % pct, num(plan["tick"])))
    out.append("  Reward:risk   %s" % ratio(plan.get("reward_risk", plan["r_multiple"])))
    ch = plan.get("charges")
    if ch:
        out.append("  Charges       round trip for %d shares (%s): %s at the stop (%s a share), %s at the target "
                   "(%s a share)" % (ch["qty"], ch.get("qty_from", ""), rupees(ch["at_stop"]),
                                     rupees(ch["per_share_at_stop"]), rupees(ch["at_target"]),
                                     rupees(ch["per_share_at_target"])))
        out.append("  After costs   target %+.2fR, stop %+.2fR%s" % (
            ch["r_net_at_target"], ch["r_net_at_stop"],
            (", reward:risk %s" % ratio(ch["reward_risk_net"])) if ch.get("reward_risk_net") else ""))
    out.append("  Flat by       %s IST, win or lose: no overnight hold (Dhan squares off at 15:10)" % plan["flat_by"])
    out.append("Set before the entry. If price hits the stop, you are out; never move it further away.")
    out.append(NOTE)
    return out


def parse_now(text):
    if not text:
        return nse.now_ist().replace(microsecond=0)
    try:
        return nse.parse_now(text)
    except nse.NSEError:
        raise PlanError('--now wants an IST time like "2026-10-29 09:40"')


def cmd_plan(a):
    now = parse_now(a.now)
    plan, warnings = build_plan(a, now)
    path = None
    if a.write is not None:
        path = a.write or os.path.join(nse.trader_dir(), "plan.json")
        nse.write_json(path, plan)
    if a.json:
        print(json.dumps(dict(plan, warnings=warnings), indent=2))
        return 0
    for line in describe(plan):
        print(line)
    for w in warnings:
        print("Note: " + w)
    if path:
        print("Wrote %s." % path)
    print("Next: /india-timemachine replays it, then /india-budget sizes it to your max loss. Nothing was placed.")
    return 0


def cmd_show(a):
    path = a.plan or os.path.join(nse.trader_dir(), "plan.json")
    if not os.path.exists(path):
        print("No plan at %s yet. Make one: exit.py plan --write" % path)
        return 1
    try:
        plan = nse.read_json(path)
    except nse.NSEError as e:
        raise PlanError(str(e))
    for k in ("symbol", "date", "entry", "stop", "target", "risk_per_share", "flat_by", "tick"):
        if plan.get(k) is None:
            raise PlanError("%s is missing %r: make a new plan with exit.py plan --write" % (path, k))
    if a.json:
        print(json.dumps(plan, indent=2))
        return 0
    for line in describe(plan):
        print(line)
    today = parse_now(a.now).date().isoformat()
    if plan["date"] != today:
        print("Note: this plan is for %s, not today (%s)." % (plan["date"], today))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Exit Planner (india-exit): the stop and target before the entry.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("plan", help="set the stop and target")
    p.add_argument("--from", dest="from_path", metavar="PATH", help="entry.json to plan from (default trader/entry.json)")
    p.add_argument("--symbol")
    p.add_argument("--entry", type=float, help="entry price (or a plan-ahead price with --from)")
    p.add_argument("--or-high", type=float)
    p.add_argument("--or-low", type=float)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--setup", choices=["orb", "level"])
    p.add_argument("--date", help="session date YYYY-MM-DD (default: the entry's, or today)")
    p.add_argument("--time", help="entry time, IST, e.g. 09:40")
    p.add_argument("--entry-bar", type=int)
    p.add_argument("--interval", choices=["5m", "1m"])
    p.add_argument("--side", default="long", choices=["buy", "long", "sell", "short"], help="longs only")
    p.add_argument("--stop", help="or-mid (orb default), or-low, price:X, pct:X, atr:K")
    p.add_argument("--target", help="2R (default), 1R, 1.5R, price:X")
    p.add_argument("--tick", type=float, help="the stock's tick in rupees (default: Dhan's file, else NSE's band)")
    p.add_argument("--scrip", metavar="FILE", help="Dhan api-scrip-master.csv to read the tick from")
    p.add_argument("--qty", type=int, help="shares for the charges line (default max_loss_per_trade / risk)")
    p.add_argument("--flat-by", help="HH:MM IST (default config flat_by, 15:05; never after 15:10)")
    p.add_argument("--config", help="default trader/config.json (TRADER_DIR)")
    p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON for an atr stop")
    p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-10-29 09:40"')
    p.add_argument("--write", nargs="?", const="", metavar="PATH", help="save the plan (default trader/plan.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="print the current plan")
    p.add_argument("--plan", help="default trader/plan.json")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"plan": cmd_plan, "show": cmd_show}[a.cmd](a)
    except nse.NSEError as e:
        print("exit.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
