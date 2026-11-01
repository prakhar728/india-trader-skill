#!/usr/bin/env python3
"""Statistics gates for india-research. Standard library only.

    bootstrap_ci   day-clustered bootstrap of the mean per trade (seeded: same answer every run)
    t_stats        per-trade t and the day-clustered t (daily sums; trades on one day are not independent)
    dsr            Deflated Sharpe Ratio, Bailey & Lopez de Prado (2014): the probability that the
                   true Sharpe ratio is above what the best of N unskilled trials would show by luck,
                   corrected for skew and fat tails
    gates          the eight gates a "candidate" must pass on the validate split (and the holdout)
"""

import math
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "india-common"))
import nse  # noqa: E402,F401  (the pack's shared core; money formatting)

EULER_GAMMA = 0.5772156649015329
N01 = statistics.NormalDist()
MIN_TRADES = {"intraday": 100, "swing": 30}
DSR_MIN = 0.95
T_MIN = 2.0
BREADTH_MIN = 60.0
BOOT_B = 2000
BOOT_SEED = 20261029
ANNUAL = 250
GATE_ORDER = ("min_trades", "bootstrap_ci", "t_stat", "dsr", "cost_stress", "breadth", "benchmark", "no_look_ahead")


def moments(xs):
    """mean, sample sd, skewness, kurtosis (Pearson, NOT excess: a normal has 3)."""
    n = len(xs)
    if n < 2:
        return (xs[0] if xs else None), None, None, None
    m = sum(xs) / n
    sd = statistics.stdev(xs)
    pop = math.sqrt(sum((x - m) ** 2 for x in xs) / n)
    if pop == 0:
        return m, sd, 0.0, 3.0
    skew = sum((x - m) ** 3 for x in xs) / n / pop ** 3
    kurt = sum((x - m) ** 4 for x in xs) / n / pop ** 4
    return m, sd, skew, kurt


def sharpe(xs):
    m, sd, _, _ = moments(xs)
    return None if not sd else m / sd


def t_stat(xs):
    if len(xs) < 2:
        return None
    sd = statistics.stdev(xs)
    return None if not sd else (sum(xs) / len(xs)) / (sd / math.sqrt(len(xs)))


def by_day(trades, key):
    out = {}
    for t in trades:
        out.setdefault(t["date"], []).append(t[key])
    return out


def daily_sums(trades, key, dates=None):
    d = by_day(trades, key)
    days = sorted(set(dates) if dates is not None else d)
    return [sum(d.get(x, [])) for x in days]


def bootstrap_ci(trades, key="R_net", b=BOOT_B, seed=BOOT_SEED, alpha=0.05):
    """95 % CI of the mean per trade, resampling whole DAYS with replacement."""
    groups = list(by_day(trades, key).values())
    if not groups:
        return None, None
    rng = random.Random(seed)
    sums = [(sum(g), len(g)) for g in groups]
    n = len(sums)
    stats = []
    for _ in range(b):
        s = c = 0
        for _ in range(n):
            gs, gc = sums[rng.randrange(n)]
            s += gs
            c += gc
        stats.append(s / c)
    stats.sort()
    lo = stats[int(math.floor(alpha / 2 * b))]
    hi = stats[min(b - 1, int(math.ceil((1 - alpha / 2) * b)) - 1)]
    return lo, hi


def expected_max_sr(n_trials, var_sr):
    """E[max SR] of N unskilled trials (False Strategy Theorem)."""
    if n_trials <= 1:
        return 0.0
    a = N01.inv_cdf(1 - 1.0 / n_trials)
    b = N01.inv_cdf(1 - 1.0 / (n_trials * math.e))
    return math.sqrt(var_sr) * ((1 - EULER_GAMMA) * a + EULER_GAMMA * b)


def psr(sr, t, skew, kurt, sr0=0.0):
    """Probabilistic Sharpe Ratio: P(true SR > sr0), all in per-observation units."""
    den = 1 - skew * sr + (kurt - 1) / 4.0 * sr * sr
    if t < 2 or den <= 0:
        return None
    return N01.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(den))


def dsr(sr, t, skew, kurt, n_trials, var_sr=None):
    """Deflated Sharpe Ratio. var_sr = the variance of the Sharpe ratios across the trials; without
    it, the null's own sampling variance 1/T."""
    v = var_sr if var_sr else 1.0 / t
    sr0 = expected_max_sr(n_trials, v)
    return psr(sr, t, skew, kurt, sr0), sr0


def trial_variance(trial_srs):
    """Variance of the registry's trial Sharpe ratios (None under 10 of them)."""
    return statistics.variance(trial_srs) if trial_srs and len(trial_srs) >= 10 else None


def dsr_at(sr, t, skew, kurt, n_trials, trial_srs=None):
    """The gate's DSR for a per-trade SR over T trades at n_trials: -> (dsr, sr0, variance, from_trials).
    Used by the gate and to re-state an old gate at the registry's CURRENT trial count."""
    var_trials = trial_variance(trial_srs)
    v = max(1.0 / t, var_trials or 0.0)
    p, sr0 = dsr(sr, t, skew, kurt, max(1, n_trials), v)
    return p, sr0, v, bool(var_trials and v == var_trials)


def _res(ok, value, need, text):
    return {"pass": bool(ok), "value": value, "need": need, "text": text}


def gates(trades, dates, stress_trades, bench, n_trials, trial_srs=None, kind="intraday", capital=100000.0,
          look=None):
    """-> ordered list of (name, result). trades: the split's trades; dates: the split's sessions."""
    out = []
    n = len(trades)
    need_n = MIN_TRADES.get(kind, 100)
    out.append(("min_trades", _res(n >= need_n, n, ">= %d" % need_n, "%d trades (need %d for %s)" % (n, need_n, kind))))

    rn = [t["R_net"] for t in trades]
    lo, hi = bootstrap_ci(trades, "R_net")
    lo_i, hi_i = bootstrap_ci(trades, "pnl_net")
    mean_r = sum(rn) / n if n else None
    mean_i = sum(t["pnl_net"] for t in trades) / n if n else None
    ok = lo is not None and lo > 0 and lo_i is not None and lo_i > 0
    out.append(("bootstrap_ci", _res(ok, {"mean_R": _r(mean_r), "ci_R": [_r(lo), _r(hi)], "mean_inr": _r(mean_i, 2),
                                          "ci_inr": [_r(lo_i, 2), _r(hi_i, 2)]},
                                     "lower bound > 0",
                                     "mean net R %s, 95%% CI [%s, %s]; Rs %s a trade, CI [%s, %s] (days resampled, "
                                     "%d draws, seed %d)" % (_f(mean_r), _f(lo), _f(hi), _f(mean_i, 2), _f(lo_i, 2),
                                                             _f(hi_i, 2), BOOT_B, BOOT_SEED))))

    t_trade = t_stat(rn)
    t_day = t_stat(daily_sums(trades, "R_net", dates))
    out.append(("t_stat", _res(t_day is not None and t_day >= T_MIN, {"per_trade": _r(t_trade, 2), "per_day": _r(t_day, 2)},
                               "day-clustered t >= %.1f" % T_MIN,
                               "t %s per trade, %s on daily sums over %d sessions" % (_f(t_trade), _f(t_day), len(dates)))))

    m, sd, sk, ku = moments(rn)
    if n >= 3 and sd:
        sr = m / sd
        p, sr0, v, from_trials = dsr_at(sr, n, sk, ku, n_trials, trial_srs)
        txt = ("SR %.3f a trade (skew %.2f, kurtosis %.2f, T=%d); %d trials so far -> luck alone would show %.3f; "
               "DSR %s (%s)" % (sr, sk, ku, n, n_trials, sr0, _f(p, 3),
                                "trial-SR variance %.4f" % v if from_trials else "null variance 1/T"))
        res = _res(p is not None and p >= DSR_MIN, _r(p, 4), ">= %.2f" % DSR_MIN, txt)
        res["inputs"] = {"sr": sr, "T": n, "skew": sk, "kurt": ku, "n_trials": n_trials}
        out.append(("dsr", res))
    else:
        out.append(("dsr", _res(False, None, ">= %.2f" % DSR_MIN, "not enough trades for a Sharpe ratio")))

    sr_ = [t["R_net"] for t in stress_trades]
    s_mean = sum(sr_) / len(sr_) if sr_ else None
    s_inr = sum(t["pnl_net"] for t in stress_trades)
    out.append(("cost_stress", _res(s_mean is not None and s_mean > 0 and s_inr > 0,
                                    {"mean_R": _r(s_mean), "total_inr": round(s_inr, 2), "trades": len(sr_)},
                                    "net > 0 with charges x1.5 and slippage x2",
                                    "charges x1.5, slippage x2: %d trades, mean net R %s, %s in all"
                                    % (len(sr_), _f(s_mean), nse.inr(s_inr)))))

    sym, mon = {}, {}
    for t in trades:
        sym[t["symbol"]] = sym.get(t["symbol"], 0) + t["pnl_net"]
        mon[t["date"][:7]] = mon.get(t["date"][:7], 0) + t["pnl_net"]
    ps = 100.0 * sum(1 for v in sym.values() if v > 0) / len(sym) if sym else 0.0
    pm = 100.0 * sum(1 for v in mon.values() if v > 0) / len(mon) if mon else 0.0
    out.append(("breadth", _res(ps >= BREADTH_MIN and pm >= BREADTH_MIN,
                                {"symbols_pos_pct": round(ps, 1), "symbols": len(sym), "months_pos_pct": round(pm, 1),
                                 "months": len(mon)}, ">= %d %% of symbols and of months positive" % BREADTH_MIN,
                                "positive on %d of %d symbols (%.0f %%) and %d of %d months (%.0f %%)" % (
                                    sum(1 for v in sym.values() if v > 0), len(sym), ps,
                                    sum(1 for v in mon.values() if v > 0), len(mon), pm))))

    strat = [x / capital for x in daily_sums(trades, "pnl_net", dates)]
    s_sh = sharpe(strat)
    rows, ok = [], s_sh is not None and sum(strat) > 0
    for name in ("universe_ew", "nifty"):
        series = (bench or {}).get(name) or {}
        b = [series[d] for d in sorted(series) if d in set(dates)]
        if len(b) < 2:
            continue
        b_sh = sharpe(b)
        rows.append((name, b_sh, sum(b)))
        if b_sh is not None and (s_sh is None or s_sh <= b_sh):
            ok = False
    if not rows:
        ok = False
    ann = lambda x: None if x is None else x * math.sqrt(ANNUAL)  # noqa: E731
    out.append(("benchmark", _res(ok, {"strategy_sharpe_ann": _r(ann(s_sh), 2), "strategy_return_pct": _r(100 * sum(strat), 2),
                                       "benchmarks": dict((n_, {"sharpe_ann": _r(ann(b), 2), "return_pct": _r(100 * r, 2)})
                                                          for n_, b, r in rows)},
                                  "daily Sharpe above buy-and-hold of the universe and NIFTY 50, and positive",
                                  "strategy Sharpe %s (%.2f %% on capital) vs %s" % (
                                      _f(ann(s_sh), 2), 100 * sum(strat),
                                      "; ".join("%s %s (%.2f %%)" % (n_, _f(ann(b), 2), 100 * r) for n_, b, r in rows)
                                      or "no benchmark data"))))

    look = look or {}
    ok = look.get("decided_after_entry") == 0 and look.get("loader_ok") and not look.get("violations")
    out.append(("no_look_ahead", _res(ok, look, "0 peeks, 0 late decisions, loader cut before %s" % look.get("cutoff"),
                                      "%d trades each decided on bars closed by its entry; %d late; engine violations %d; "
                                      "newest bar in memory %s < cutoff %s" % (
                                          look.get("trades", 0), look.get("decided_after_entry", 0),
                                          look.get("violations", 0), look.get("max_bar_date"), look.get("cutoff")))))
    return out


def _r(x, n=4):
    return None if x is None else round(x, n)


def _f(x, n=3):
    return "-" if x is None else ("%+." + str(n) + "f") % x
