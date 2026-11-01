#!/usr/bin/env python3
"""Strategy research for NSE (india-research). Standard library only. NEVER trades.

An honest research loop: every hypothesis is logged before it is tested, every parameter
combination is counted as a trial, data is split once into explore / validate / a locked
holdout, and a strategy is a "candidate" only after eight statistics gates on validate,
and worth paper trading only after one, single holdout run.

    python3 research.py init                          # folders + the three example lab strategies
    python3 research.py log --id short_orb_v1 --from-example short_orb_breakdown
    python3 research.py log --id my_idea --family mean-reversion --rationale "..." --rules "..." \\
                            --grid '{"k": [1.5, 2]}' --universe nifty50 --source yahoo
    python3 research.py list | show <id> | verify
    python3 research.py explore <id>                  # grid on the explore split (trials counted)
    python3 research.py validate <id>                 # anchored walk-forward on the validate split
    python3 research.py gate <id>                     # PASS / FAIL per gate -> candidate | rejected
    python3 research.py pipeline <id>                 # explore + validate + gate
    python3 research.py holdout <id>                  # ONCE, candidates only, recorded forever
    python3 research.py promote <id>                  # holdout-passed -> paper (never live)
    python3 research.py reject <id> --why "..."
    python3 research.py splits                        # the locked windows per dataset (+ what saw them before)
    python3 research.py note --looks 12 --dataset angel:nifty100:5m --why "..."   # side looks count as trials
    python3 research.py exposure-note --dataset KEY --why "..." [--artefact FILE ...]   # once per source
    python3 research.py peers TATAPOWER               # sector group + correlations
    python3 research.py events record|show|stats      # the event recorder (safe every 5 minutes)
    python3 research.py report                        # trader/research/report.md + research-dash.json
    --no-capital-cap (any run command): per-trade stats only, no portfolio cap (recorded in the run)

Files (trader/research/): experiments.jsonl (append-only, hash-chained), splits.json,
lab/<id>.py, runs/<id>-<split>.json, events/YYYY-MM.jsonl, report.md, research-dash.json.
Not financial advice. A backtest is not a forecast.
"""

import argparse
import contextlib
import fcntl
import datetime
import hashlib
import json
import os
import re
import shutil
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "india-common"))
sys.path.insert(0, os.path.join(HERE, "..", "india-backtest"))
sys.path.insert(0, HERE)
import nse  # noqa: E402
import backtest as bt  # noqa: E402
import lab_engine as le  # noqa: E402
import stats as st  # noqa: E402
import peers as pr  # noqa: E402

AUTHOR = "strategy-researcher"
STATUSES = ("idea", "tested", "rejected", "candidate", "holdout-passed", "holdout-failed", "paper")
EXAMPLES = ("short_orb_breakdown", "vwap_reversion", "sector_lead_lag")
ID_RX = re.compile(r"^[a-z0-9][a-z0-9_\-]{1,60}$")
MIN_RATIONALE = 40
FOLDS = 2
CAVEATS = [
    "Survivorship: universes are today's NSE index lists applied to the past.",
    "Yahoo keeps 60 days of 5-minute bars: 60 sessions split 36 / 12 / 12 is a short history; the gates are "
    "deliberately strict because of it.",
    "Fills: decision bar's close +/- slippage (1 tick; 2 in the cost stress), stops and flat-by exits worse by "
    "slippage, gaps through the stop at the open, targets exact. Real fills in fast markets can be worse.",
    "Charges: Dhan's intraday equity schedule (nse.charges); shorts pay STT on the entry (the sell) and stamp "
    "duty on the cover (the buy). The cost stress multiplies charges by 1.5.",
    "Every parameter combination tried counts as a trial, and so does every side look logged with `research.py note "
    "--looks N`; the Deflated Sharpe Ratio uses the running total, and the report re-states every gate's DSR at "
    "today's total.",
    "Portfolio cap (runs from 2026-10-29 on): at most the capital (Rs 1,00,000) deployed and 5 intraday positions at "
    "once, signals beyond it skipped in decision-time order; earlier run files were made without it and their "
    "account-level totals (total Rs, daily t, benchmark) assume unlimited capital.",
    "No result here is a promise of returns. Research never places an order. Not financial advice.",
]


class ResearchError(nse.NSEError):
    pass


CAPITAL_CAP = True              # main() sets it from --no-capital-cap
MIN_WHY = 20
DSR_RX = re.compile(r"SR ([-\d.]+) a trade \(skew ([-\d.]+), kurtosis ([-\d.]+), T=(\d+)\)")


# ---------------------------------------------------------------- paths

def rdir():
    return le.research_dir()


def reg_path():
    return os.path.join(rdir(), "experiments.jsonl")


def runs_path(exp_id, split):
    return os.path.join(rdir(), "runs", "%s-%s.json" % (exp_id, split))


def lab_path(exp_id):
    return os.path.join(le.lab_dir(), exp_id + ".py")


def example_path(name):
    return os.path.join(HERE, "lab", name + ".py")


# ---------------------------------------------------------------- the registry (append-only, hash-chained)

def _h(line):
    return hashlib.sha256(line.encode("utf-8")).hexdigest()[:16]


def read_registry(strict=True):
    """-> (records, problem). Each line carries the hash of the line before it."""
    recs, prev, problem = [], "genesis", None
    try:
        with open(reg_path(), encoding="utf-8") as fh:
            lines = fh.read().split("\n")
    except FileNotFoundError:
        return [], None
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            problem = "line %d is not JSON" % (i + 1)
            break
        if r.get("prev") != prev:
            problem = "line %d does not follow line %d (an entry was edited, removed or reordered)" % (i + 1, i)
            break
        recs.append(r)
        prev = _h(line)
    if problem and strict:
        raise ResearchError("experiments.jsonl is append-only and its hash chain is broken: %s. Nothing was "
                            "written. Restore the file from a backup; never rewrite history." % problem)
    return recs, problem


@contextlib.contextmanager
def registry_lock():
    """One writer at a time: several researchers may run in parallel, and the hash chain
    needs read-last-line + append to happen as one step."""
    os.makedirs(rdir(), exist_ok=True)
    with open(os.path.join(rdir(), ".registry.lock"), "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def append(rec, now):
    with registry_lock():
        return _append(rec, now)


def _append(rec, now):
    recs, _ = read_registry()
    prev = "genesis"
    if recs:
        with open(reg_path(), encoding="utf-8") as fh:
            last = [x for x in fh.read().split("\n") if x.strip()][-1]
        prev = _h(last)
    rec = dict(rec, at=nse.iso_ist(now), prev=prev)
    os.makedirs(rdir(), exist_ok=True)
    with open(reg_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    return rec


def experiments(recs):
    out = {}
    for r in recs:
        e = out.get(r.get("id"))
        if r["op"] == "log":
            out[r["id"]] = {"log": r, "explore": [], "validate": [], "gate": [], "claim": None, "holdout": None,
                            "status": "idea", "history": [("idea", r["at"], "logged")]}
            continue
        if e is None:
            continue
        if r["op"] in ("explore", "validate", "gate"):
            e[r["op"]].append(r)
        elif r["op"] == "holdout_claim":
            e["claim"] = r
        elif r["op"] == "holdout":
            e["holdout"] = r
        elif r["op"] == "status":
            e["status"] = r["status"]
            e["history"].append((r["status"], r["at"], r.get("why", "")))
    return out


def trial_runs(recs):
    """Explore runs that count: one per (id, lab file, grid)."""
    seen, out = set(), []
    for r in recs:
        if r["op"] != "explore":
            continue
        key = (r["id"], r["sha"], r["grid_sha"])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def explore_trials(recs):
    return sum(r["trials"] for r in trial_runs(recs))


def side_looks(recs):
    """Looks logged with `research.py note --looks N`: measurements made outside explore."""
    return sum(int(r.get("looks") or 0) for r in recs if r.get("op") == "note")


def total_trials(recs):
    return explore_trials(recs) + side_looks(recs)


def dsr_inputs(g):
    """(sr, T, skew, kurt) a gate record's DSR was computed from: the stored inputs, or (older
    records) parsed from the gate's text (SR to 3 decimals, skew/kurtosis to 2)."""
    d = (g.get("gates") or {}).get("dsr") or {}
    i = d.get("inputs")
    if i:
        return float(i["sr"]), int(i["T"]), float(i["skew"]), float(i["kurt"])
    m = DSR_RX.search(d.get("text") or "")
    if not m:
        return None
    return float(m.group(1)), int(m.group(4)), float(m.group(2)), float(m.group(3))


def dsr_now(g, recs):
    """A gate's DSR re-stated at the registry's CURRENT trial count (and trial-SR variance)."""
    x = dsr_inputs(g) if g else None
    if not x:
        return None
    p, _, _, _ = st.dsr_at(x[0], x[1], x[2], x[3], max(1, total_trials(recs)), trial_srs(recs))
    return None if p is None else round(p, 4)


def trial_srs(recs):
    return [v["sr"] for r in trial_runs(recs) for v in r["variants"] if v.get("sr") is not None]


def get_exp(exp_id):
    recs, _ = read_registry()
    exps = experiments(recs)
    if exp_id not in exps:
        raise ResearchError("no experiment %r in the registry (research.py list)" % exp_id)
    return recs, exps[exp_id]


def set_status(exp_id, status, why, now):
    assert status in STATUSES
    append({"op": "status", "id": exp_id, "status": status, "why": why}, now)


# ---------------------------------------------------------------- data

def config():
    cfg = dict(bt.DEFAULTS)
    try:
        cfg.update(nse.load_config())
    except nse.NSEError as e:
        raise ResearchError(str(e))
    cfg.setdefault("max_positions", le.MAX_POSITIONS)
    cfg.setdefault("capital_cap", True)
    if not CAPITAL_CAP:
        cfg["capital_cap"] = False
    return cfg


def sizing_notes(ds, cfg, quiet=False):
    """Stocks that can never be sized (price above the position cap etc.): a run note, not silence."""
    rows = le.unsizable(ds, cfg)
    if rows and not quiet:
        print("  note: never sized (one share is above the %s limit of %s): %s" % (
            rows[0]["by"], nse.inr(rows[0]["limit"], 0),
            ", ".join("%s (low %s)" % (r["symbol"], nse.inr(r["min_price"], 0)) for r in rows)))
    return rows


def source_dirs(source):
    return le.source_dirs(source)


def dataset_key(source, universe_name):
    label = source
    if source.startswith("dir:"):
        first = source[4:].split(",")[0].rstrip("/")
        label = "dir-" + (os.path.basename(first) or "bars")
    return "%s:%s:5m" % (label, universe_name)


def resolve(log, a):
    """-> (key, symbols, dirs) for an experiment's dataset."""
    source = log["source"]
    uname, syms, _ = bt.resolve_universe(log["universe"], getattr(a, "list_file", None) or log.get("list_file"))
    return dataset_key(source, uname), syms, source_dirs(source)


def sectors(a):
    try:
        s2i, _ = pr.load_sectors(getattr(a, "sectors", None))
    except nse.NSEError:
        s2i = {}
    return s2i


def lab_events():
    try:
        import events as ev  # noqa: E402
        return ev.for_lab()
    except Exception:  # an unreadable events folder must not stop research
        return []


def load(log, a, split, ticket=None):
    key, syms, dirs = resolve(log, a)
    ds = le.load_dataset(key, syms, dirs, split, ticket=ticket, cfg=config(), sectors=sectors(a),
                         events=lab_events(), ticks=bt.cached_tick_table()[0], now=a.now_dt)
    if not ds.sessions:
        raise ResearchError("no sessions for %s in %s (%s)" % (key, split, ", ".join(dirs)))
    return ds


def strategy_for(exp_id):
    p = lab_path(exp_id)
    if not os.path.isfile(p):
        raise ResearchError("no lab file %s: write it (or `log --from-example`) before testing" % p)
    return le.load_strategy(p, exp_id)


# ---------------------------------------------------------------- summaries

def ordered(gates):
    return [(k, gates[k]) for k in st.GATE_ORDER if k in gates] + [(k, v) for k, v in gates.items() if k not in st.GATE_ORDER]


def summary(trades):
    n = len(trades)
    if not n:
        return {"trades": 0}
    rn = [t["R_net"] for t in trades]
    m, sd, _, _ = st.moments(rn)
    return {"trades": n, "win_pct": round(100.0 * sum(1 for t in trades if t["pnl_net"] > 0) / n, 1),
            "avg_R": round(sum(t["R"] for t in trades) / n, 4), "avg_R_net": round(m, 4),
            "inr_per_trade": round(sum(t["pnl_net"] for t in trades) / n, 2),
            "total_inr": round(sum(t["pnl_net"] for t in trades), 2),
            "charges_inr": round(sum(t["charges"] for t in trades), 2),
            "sr": round(m / sd, 4) if sd else None, "t": _r(st.t_stat(rn), 2),
            "long": sum(1 for t in trades if t["side"] == "long"), "short": sum(1 for t in trades if t["side"] == "short"),
            "first": min(t["date"] for t in trades), "last": max(t["date"] for t in trades)}


def _r(x, n=3):
    return None if x is None else round(x, n)


def fr(x, fmt="%+.3f"):
    return "-" if x is None else fmt % x


def grid_sha(strat):
    return hashlib.sha256(json.dumps([strat.default(), strat.meta.get("grid") or {}], sort_keys=True).encode()).hexdigest()[:12]


def cap_text(cap, skips):
    if not cap.get("on"):
        return "OFF (--no-capital-cap: per-trade stats only; totals assume unlimited capital)"
    return "at most %s deployed and %d positions at once; %d signal(s) skipped by it" % (
        nse.inr(cap["capital"], 0), cap["max_positions"], skips)


def save_run(exp_id, split, doc):
    nse.write_json(runs_path(exp_id, split), doc)


# ---------------------------------------------------------------- commands

def cmd_init(a):
    os.makedirs(le.lab_dir(), exist_ok=True)
    os.makedirs(os.path.join(rdir(), "runs"), exist_ok=True)
    os.makedirs(le.events_dir(), exist_ok=True)
    for n in EXAMPLES:
        dst = lab_path(n)
        if os.path.exists(dst):
            print("kept     %s (exists)" % dst)
        else:
            shutil.copyfile(example_path(n), dst)
            print("copied   %s" % dst)
    print("Registry: %s (%d records). Next: research.py log --id <id> --from-example <name>" % (
        reg_path(), len(read_registry()[0])))
    return 0


def cmd_log(a):
    if not ID_RX.match(a.id or ""):
        raise ResearchError("--id must be lower case letters, digits, _ or - (2-61 characters)")
    recs, _ = read_registry()
    if a.id in experiments(recs):
        raise ResearchError("%s is already in the registry: a new or changed hypothesis needs a new id" % a.id)
    path = lab_path(a.id)
    if a.from_example:
        if a.from_example not in EXAMPLES:
            raise ResearchError("--from-example: one of %s" % ", ".join(EXAMPLES))
        if not os.path.exists(path):
            os.makedirs(le.lab_dir(), exist_ok=True)
            shutil.copyfile(example_path(a.from_example), path)
    meta = {}
    if os.path.isfile(path):
        meta = le.load_strategy(path, a.id).meta
    grid = json.loads(a.grid) if a.grid else meta.get("grid") or {}
    rationale = a.rationale or meta.get("rationale") or ""
    rules = a.rules or meta.get("rules") or ""
    if len(rationale.strip()) < MIN_RATIONALE:
        raise ResearchError("--rationale must state WHY it should work (a mechanism, %d+ characters); "
                            "'it backtested well' is not a reason" % MIN_RATIONALE)
    if len(rules.strip()) < 20:
        raise ResearchError("--rules must state the exact rules")
    source = ("dir:" + a.bars) if a.bars else a.source
    list_file = os.path.abspath(a.list_file) if a.list_file else None   # ids must re-run from any folder
    uname, syms, notes = bt.resolve_universe(a.universe, list_file)
    rec = {"op": "log", "id": a.id, "author": a.author, "date": a.now_dt.date().isoformat(),
           "family": a.family or meta.get("family") or "unclassified", "kind": meta.get("kind", a.kind),
           "rationale": rationale.strip(), "rules": rules.strip(), "grid": grid, "grid_size": le.grid_size(grid),
           "source": source, "universe": a.universe, "list_file": list_file, "dataset": dataset_key(source, uname),
           "symbols": len(syms), "lab": os.path.relpath(path, rdir()), "lab_exists": os.path.isfile(path),
           "notes": a.notes or ""}
    append(rec, a.now_dt)
    print("Logged %s (%s, %s): %d parameter combination(s) on %s, %d symbols. Status: idea." % (
        a.id, rec["family"], rec["kind"], rec["grid_size"], rec["dataset"], len(syms)))
    if not rec["lab_exists"]:
        print("Write the strategy at %s (see lab_engine.py for signals(day, ctx))." % path)
    return 0


def _explore(a, exp_id, quiet=False):
    recs, e = get_exp(exp_id)
    if e["status"] not in ("idea", "tested"):
        raise ResearchError("%s is %s: exploring it again would be a new hypothesis, so log a new id" % (exp_id, e["status"]))
    strat = strategy_for(exp_id)
    if e["validate"] and e["validate"][-1]["sha"] != strat.sha:
        raise ResearchError("%s's lab file changed after validate froze it (sha %s, now %s): log the change as a new id"
                            % (exp_id, e["validate"][-1]["sha"], strat.sha))
    ds = load(e["log"], a, "explore")
    rec_split = ds.split_rec
    dates = ds.dates_in(*rec_split["explore"])
    cfg = config()
    rows = le.run_variants(ds, strat, cfg, dates)
    cap = le.cap_settings(cfg)
    never = sizing_notes(ds, cfg, quiet=True)
    gs = grid_sha(strat)
    counted = not any(r["id"] == exp_id and r["sha"] == strat.sha and r["grid_sha"] == gs for r in recs
                      if r["op"] == "explore")
    variants = []
    for r in rows:
        s = summary(r["trades"])
        variants.append(dict(s, label=r["label"], params=r["params"], capital_skips=r["info"].get("capital_skips", 0)))
    ok = [v for v in variants if v["trades"] >= le.MIN_SELECT_TRADES]
    best = max(ok, key=lambda v: (v["avg_R_net"], v["trades"])) if ok else None
    rec = {"op": "explore", "id": exp_id, "sha": strat.sha, "grid_sha": gs, "trials": len(rows), "counted": counted,
           "dataset": ds.key, "window": [dates[0], dates[-1]], "sessions": len(dates),
           "symbols": len(ds.sessions), "variants": variants, "best": best["label"] if best else None,
           "calls": sum(r["info"]["calls"] for r in rows), "capital_cap": cap,
           "never_sized": [r["symbol"] for r in never]}
    save_run(exp_id, "explore", {"made": nse.iso_ist(a.now_dt), "sha": strat.sha, "dataset": ds.key,
                                 "capital_cap": cap, "never_sized": never,
                                 "window": rec["window"], "variants": [{"label": r["label"], "params": r["params"],
                                                                        "trades": r["trades"], "info": r["info"]}
                                                                       for r in rows]})
    append(rec, a.now_dt)
    if e["status"] == "idea":
        set_status(exp_id, "tested", "explored %d variant(s)" % len(rows), a.now_dt)
    recs, _ = read_registry()
    if not quiet:
        print("Explore %s on %s: %s to %s (%d sessions, %d stocks). %d variant(s) %s; registry total %d trials."
              % (exp_id, ds.key, dates[0], dates[-1], len(dates), len(ds.sessions), len(rows),
                 "counted" if counted else "not counted again (same code and grid as an earlier run)",
                 total_trials(recs)))
        print("  Portfolio cap: %s." % cap_text(cap, sum(v["capital_skips"] for v in variants)))
        sizing_notes(ds, cfg)
        print("  %-36s %6s %6s %8s %8s %9s %6s" % ("variant", "trades", "win%", "R gross", "R net", "Rs/trade", "t"))
        for v in variants:
            print("  %-36s %6d %6s %8s %8s %9s %6s%s" % (
                v["label"][:36], v["trades"], fr(v.get("win_pct"), "%.1f"), fr(v.get("avg_R")), fr(v.get("avg_R_net")),
                fr(v.get("inr_per_trade"), "%+.0f"), fr(v.get("t"), "%.1f"), "  <- best" if best and v is best else ""))
    return rec


def cmd_explore(a):
    _explore(a, a.id)
    return 0


def _validate(a, exp_id, quiet=False):
    recs, e = get_exp(exp_id)
    if e["claim"]:
        raise ResearchError("%s has used its holdout: validate is closed" % exp_id)
    strat = strategy_for(exp_id)
    if not any(r["sha"] == strat.sha for r in e["explore"]):
        raise ResearchError("%s: explore this exact lab file first (sha %s has no explore run)" % (exp_id, strat.sha))
    if e["validate"] and e["validate"][0]["sha"] != strat.sha:
        raise ResearchError("%s's lab file changed after validate froze it: log the change as a new id" % exp_id)
    ds = load(e["log"], a, "validate")
    sp = ds.split_rec
    train = ds.dates_in(*sp["explore"])
    test = ds.dates_in(*sp["validate"])
    cfg = config()
    trades, folds, rows, calls = le.walk_forward(ds, strat, cfg, train, test, a.folds)
    stress = le.stress(ds, strat, cfg, folds)
    bench = le.benchmark(ds, test)
    look = le.audit(trades, ds)
    look["violations"] = 0
    look["calls"] = calls
    s = summary(trades)
    cap = le.cap_settings(cfg)
    skips = dict((r["label"], r["info"].get("capital_skips", 0)) for r in rows)
    never = sizing_notes(ds, cfg, quiet=True)
    doc = {"made": nse.iso_ist(a.now_dt), "sha": strat.sha, "dataset": ds.key, "split": "validate",
           "dates": test, "train": [train[0], train[-1]], "folds": folds, "trades": trades, "stress_trades": stress,
           "benchmark": bench, "look_ahead": look, "capital": cfg["capital"], "kind": strat.kind,
           "capital_cap": cap, "capital_skips_by_variant": skips, "never_sized": never}
    save_run(exp_id, "validate", doc)
    rec = {"op": "validate", "id": exp_id, "sha": strat.sha, "dataset": ds.key, "window": [test[0], test[-1]],
           "sessions": len(test), "folds": folds, "summary": s, "stress": summary(stress), "capital_cap": cap,
           "never_sized": [r["symbol"] for r in never]}
    append(rec, a.now_dt)
    if not quiet:
        print("Validate %s (anchored walk-forward, %d fold(s)) on %s to %s (%d sessions):" % (
            exp_id, len(folds), test[0], test[-1], len(test)))
        for f in folds:
            print("  fold %s..%s: traded %s (%s), %d trades" % (f["from"], f["to"], f["chosen"], f["chosen_by"], f["trades"]))
        print("  %d trades, win %s %%, net R %s a trade, %s a trade, %s in all." % (
            s["trades"], fr(s.get("win_pct"), "%.1f"), fr(s.get("avg_R_net")), nse.inr(s.get("inr_per_trade") or 0),
            nse.inr(s.get("total_inr") or 0)))
        print("  Portfolio cap: %s (train + validate, all variants)." % cap_text(cap, sum(skips.values())))
        sizing_notes(ds, cfg)
    return rec


def cmd_validate(a):
    _validate(a, a.id)
    return 0


def run_gates(doc, recs):
    return st.gates(doc["trades"], doc["dates"], doc["stress_trades"], doc["benchmark"], max(1, total_trials(recs)),
                    trial_srs(recs), doc.get("kind", "intraday"), float(doc.get("capital") or 100000), doc["look_ahead"])


def print_gates(exp_id, split, doc, gs, n_trials):
    print("Gates for %s on %s (%s to %s, %d sessions; %d trials in the registry):" % (
        exp_id, split, doc["dates"][0], doc["dates"][-1], len(doc["dates"]), n_trials))
    for name, g in gs:
        print("  %s %-14s %s  [need %s]" % ("PASS" if g["pass"] else "FAIL", name, g["text"], g["need"]))
    k = sum(1 for _, g in gs if g["pass"])
    return k


def _gate(a, exp_id, quiet=False):
    recs, e = get_exp(exp_id)
    if e["claim"]:
        raise ResearchError("%s has used its holdout: its validate gates are final" % exp_id)
    doc = nse.read_json(runs_path(exp_id, "validate"))
    if not doc or not e["validate"]:
        raise ResearchError("%s has no validate run: research.py validate %s" % (exp_id, exp_id))
    strat = strategy_for(exp_id)
    if doc["sha"] != strat.sha or e["validate"][-1]["sha"] != strat.sha:
        raise ResearchError("%s's lab file changed after validate: log it as a new id" % exp_id)
    gs = run_gates(doc, recs)
    n = total_trials(recs)
    k = print_gates(exp_id, "validate", doc, gs, n) if not quiet else sum(1 for _, g in gs if g["pass"])
    if e["gate"] and not quiet:
        g0 = e["gate"][-1]
        print("  DSR as gated on %s at N=%d: %s; at today's N=%d: %s." % (
            g0["at"][:16], g0["trials_at_gate"], fr(g0["gates"].get("dsr", {}).get("value"), "%.3f"), n,
            fr(dict(gs)["dsr"]["value"], "%.3f")))
    allp = k == len(gs)
    append({"op": "gate", "id": exp_id, "split": "validate", "sha": strat.sha, "trials_at_gate": n,
            "gates": dict(gs), "passed": k, "of": len(gs), "all_pass": allp}, a.now_dt)
    status = "candidate" if allp else "rejected"
    if e["status"] != status:
        set_status(exp_id, status, "%d/%d gates on validate" % (k, len(gs)), a.now_dt)
    print("Result: %d/%d gates passed -> %s.%s" % (k, len(gs), status.upper(),
          " Next: research.py holdout %s (one run, ever)." % exp_id if allp else
          " A negative result honestly reported is a result."))
    return allp


def cmd_gate(a):
    _gate(a, a.id)
    return 0


def cmd_pipeline(a):
    _explore(a, a.id)
    print("")
    _validate(a, a.id)
    print("")
    _gate(a, a.id)
    return 0


def cmd_holdout(a):
    recs, e = get_exp(a.id)
    resume = bool(e["claim"]) and not e["holdout"] and not os.path.exists(runs_path(a.id, "holdout"))
    if (e["claim"] or e["holdout"]) and not resume:
        raise ResearchError("%s already used its holdout on %s. It is recorded permanently and never re-run; a changed "
                            "strategy is a new id (and its trials add to the count)." % (a.id, (e["claim"] or e["holdout"])["at"]))
    if e["status"] != "candidate":
        raise ResearchError("%s is %s: only a candidate (all gates PASS on validate) may open the holdout" % (a.id, e["status"]))
    strat = strategy_for(a.id)
    frozen = e["validate"][-1]["sha"]
    if strat.sha != frozen:
        raise ResearchError("%s's lab file changed since validate (sha %s, now %s): log it as a new id" % (a.id, frozen, strat.sha))
    if resume:
        print("Resuming %s's interrupted holdout run (claimed %s, nothing was computed)." % (a.id, e["claim"]["at"][:16]))
    else:
        append({"op": "holdout_claim", "id": a.id, "sha": strat.sha}, a.now_dt)  # BEFORE any holdout bar is read
    ticket = le.HoldoutTicket(a.id, reg_path())
    ds = load(e["log"], a, "holdout", ticket)
    sp = ds.split_rec
    pre = ds.dates_in(sp["explore"][0], sp["validate"][1])
    hold = ds.dates_in(*sp["holdout"])
    cfg = config()
    rows = le.run_variants(ds, strat, cfg, pre)
    chosen, how = le.choose(rows, pre, strat.default())
    trades, info = le.run_strategy(ds, strat, chosen["params"], cfg, hold)
    stress, _ = le.run_strategy(ds, strat, chosen["params"], cfg, hold, None, 2, 1.5)
    look = le.audit(trades, ds)
    look["violations"] = info["violations"]
    doc = {"made": nse.iso_ist(a.now_dt), "sha": strat.sha, "dataset": ds.key, "split": "holdout", "dates": hold,
           "chosen": chosen["label"], "chosen_by": how, "trades": trades, "stress_trades": stress,
           "benchmark": le.benchmark(ds, hold), "look_ahead": look, "capital": cfg["capital"], "kind": strat.kind,
           "capital_cap": le.cap_settings(cfg), "capital_skips": info.get("capital_skips", 0)}
    save_run(a.id, "holdout", doc)
    recs, _ = read_registry()
    gs = run_gates(doc, recs)
    k = print_gates(a.id, "HOLDOUT", doc, gs, total_trials(recs))
    ok = k == len(gs)
    append({"op": "holdout", "id": a.id, "sha": strat.sha, "chosen": chosen["label"], "summary": summary(trades),
            "gates": dict(gs), "passed": k, "of": len(gs), "all_pass": ok}, a.now_dt)
    set_status(a.id, "holdout-passed" if ok else "holdout-failed", "%d/%d gates on the holdout" % (k, len(gs)), a.now_dt)
    print("Holdout %s: %s (%d/%d gates). Recorded permanently; this id can never run the holdout again." % (
        a.id, "PASSED" if ok else "FAILED", k, len(gs)))
    if ok:
        print("Next: research.py promote %s -> paper trading only (india-broker paper mode), never live." % a.id)
    return 0


def cmd_promote(a):
    _, e = get_exp(a.id)
    if e["status"] != "holdout-passed":
        raise ResearchError("%s is %s: only holdout-passed experiments go to paper trading" % (a.id, e["status"]))
    set_status(a.id, "paper", "promoted to PAPER trading (local ledger); never live without the user's own decision",
               a.now_dt)
    print("%s -> paper. Paper means india-broker's local ledger (mode paper); research never sends orders." % a.id)
    return 0


def cmd_reject(a):
    _, e = get_exp(a.id)
    if e["status"] in ("holdout-passed", "holdout-failed", "paper"):
        raise ResearchError("%s is %s: its record is final" % (a.id, e["status"]))
    set_status(a.id, "rejected", a.why or "rejected by the researcher", a.now_dt)
    print("%s -> rejected." % a.id)
    return 0


def row_for(exp_id, e, recs=None):
    v = e["validate"][-1]["summary"] if e["validate"] else {}
    g = e["gate"][-1] if e["gate"] else None
    ci = (g or {}).get("gates", {}).get("bootstrap_ci", {}).get("value") or {}
    dsr = (g or {}).get("gates", {}).get("dsr", {}).get("value")
    h = e["holdout"]
    trials = sum(r["trials"] for r in e["explore"])
    return {"id": exp_id, "family": e["log"]["family"], "status": e["status"], "kind": e["log"]["kind"],
            "dataset": e["log"]["dataset"], "logged": e["log"]["date"], "trials": trials,
            "trades": v.get("trades"), "net_R": v.get("avg_R_net"), "net_inr": v.get("inr_per_trade"),
            "ci_low": (ci.get("ci_R") or [None, None])[0], "ci_high": (ci.get("ci_R") or [None, None])[1],
            "dsr": dsr, "trials_at_gate": g["trials_at_gate"] if g else None,
            "dsr_now": dsr_now(g, recs) if (g and recs is not None) else None,
            "gates_passed": ("%d/%d" % (g["passed"], g["of"])) if g else None,
            "holdout": ("PASS" if h["all_pass"] else "FAIL") + " %d/%d" % (h["passed"], h["of"]) if h else None,
            "best_explore": (e["explore"][-1]["best"] if e["explore"] else None)}


def cmd_list(a):
    recs, problem = read_registry(strict=False)
    exps = experiments(recs)
    rows = [row_for(i, e, recs) for i, e in exps.items()]
    if a.json:
        print(json.dumps({"total_trials": total_trials(recs), "chain_ok": problem is None, "experiments": rows}, indent=1))
        return 0
    print("%d experiment(s), %d trials in all (%d explore combinations + %d logged side looks).%s" % (
        len(rows), total_trials(recs), explore_trials(recs), side_looks(recs),
        "" if problem is None else "  WARNING: registry chain broken: " + problem))
    if rows:
        print("%-24s %-15s %-14s %6s %6s %8s %9s %17s %6s %7s %6s %s" % (
            "id", "family", "status", "trials", "trades", "net R", "Rs/trade", "95% CI (R)", "DSR", "DSR now", "gates",
            "holdout"))
        for r in rows:
            print("%-24s %-15s %-14s %6d %6s %8s %9s %17s %6s %7s %6s %s" % (
                r["id"][:24], r["family"][:15], r["status"], r["trials"], r["trades"] if r["trades"] is not None else "-",
                fr(r["net_R"]), fr(r["net_inr"], "%+.0f"),
                "[%s, %s]" % (fr(r["ci_low"]), fr(r["ci_high"])) if r["ci_low"] is not None else "-",
                fr(r["dsr"], "%.2f"), fr(r["dsr_now"], "%.2f"), r["gates_passed"] or "-", r["holdout"] or "-"))
        print("(DSR: as gated, at the trial count then; DSR now: the same gate re-stated at today's %d.)"
              % total_trials(recs))
    return 0


def cmd_show(a):
    recs, e = get_exp(a.id)
    if a.json:
        print(json.dumps([r for r in recs if r.get("id") == a.id], indent=1, ensure_ascii=False))
        return 0
    lg = e["log"]
    print("%s  [%s, %s]  status %s" % (a.id, lg["family"], lg["kind"], e["status"].upper()))
    print("Logged %s by %s on %s, %d combination(s), %s." % (lg["date"], lg["author"], lg["dataset"], lg["grid_size"], lg["lab"]))
    print("Why it should work: " + lg["rationale"])
    print("Rules: " + lg["rules"])
    for r in e["explore"]:
        print("Explore %s (%s..%s, sha %s): %d variants%s; best %s" % (r["at"][:16], r["window"][0], r["window"][1], r["sha"],
                                                                     r["trials"], "" if r["counted"] else " (not recounted)",
                                                                     r["best"]))
        for v in r["variants"]:
            print("    %-36s n=%-4d net R %s  Rs %s" % (v["label"][:36], v["trades"], fr(v.get("avg_R_net")),
                                                      fr(v.get("inr_per_trade"), "%+.0f")))
    for r in e["validate"]:
        s = r["summary"]
        print("Validate %s (%s..%s): %d trades, net R %s, Rs %s a trade; stress net R %s" % (
            r["at"][:16], r["window"][0], r["window"][1], s.get("trades", 0), fr(s.get("avg_R_net")),
            fr(s.get("inr_per_trade"), "%+.0f"), fr(r["stress"].get("avg_R_net"))))
    for g in e["gate"]:
        print("Gates %s: %d/%d (%d trials then; DSR %s then, %s at today's %d trials)" % (
            g["at"][:16], g["passed"], g["of"], g["trials_at_gate"], fr(g["gates"].get("dsr", {}).get("value"), "%.3f"),
            fr(dsr_now(g, recs), "%.3f"), total_trials(recs)))
        for name, x in ordered(g["gates"]):
            print("    %s %-14s %s" % ("PASS" if x["pass"] else "FAIL", name, x["text"]))
    if e["holdout"]:
        h = e["holdout"]
        print("HOLDOUT %s: %s %d/%d, variant %s" % (h["at"][:16], "PASS" if h["all_pass"] else "FAIL", h["passed"], h["of"], h["chosen"]))
    print("History: " + "; ".join("%s %s %s" % (s, t[:16], w) for s, t, w in e["history"]))
    return 0


def cmd_verify(a):
    recs, problem = read_registry(strict=False)
    if problem:
        print("BROKEN: %s" % problem)
        return 1
    print("OK: %d records, hash chain intact, %d trials (%d explore + %d side looks)." % (
        len(recs), total_trials(recs), explore_trials(recs), side_looks(recs)))
    return 0


def cmd_splits(a):
    doc = le.read_splits()
    if not doc:
        print("No dataset has been split yet (the first explore run on a dataset splits it).")
        return 0
    recs, _ = read_registry(strict=False)
    notes = exposure_notes(recs)
    for k, r in sorted(doc.items()):
        print("%s  (%d symbols, made %s)" % (k, len(r["symbols"]), r["created"][:16]))
        for name, n in zip(("explore", "validate", "holdout"), r["sessions"]):
            print("  %-9s %s to %s  %d sessions%s" % (name, r[name][0], r[name][1], n,
                                                     "  LOCKED: research.py holdout only" if name == "holdout" else ""))
        for line in exposure_lines(k, r, notes.get(k, [])):
            print("  " + line)
    return 0


def exposure_notes(recs):
    out = {}
    for r in recs:
        if r.get("op") == "exposure_note":
            out.setdefault(r["dataset"], []).append(r)
    return out


def _exp_item(x):
    if x.get("error"):
        return x["error"]
    return "%s on %s %s..%s (%d symbol(s)%s)" % (
        x["artefact"], x["window"], x["overlap"][0], x["overlap"][1], x["n_symbols"],
        ": " + ", ".join(x["symbols"][:6]) + (" ..." if x["n_symbols"] > 6 else "") if x.get("symbols") else "")


def exposure_lines(key, rec, notes):
    """What already looked at this split's validate / holdout dates before it was locked."""
    L = []
    eb = rec.get("exposed_before")
    if eb is None and not notes:
        return ["exposure: not recorded (split made before exposure scans; see the registry's exposure notes)"]
    if eb is not None:
        if eb:
            L.append("exposed before (scanned when the split was made, informational):")
            L += ["  - " + _exp_item(x) for x in eb]
        else:
            L.append("exposed before: nothing found when the split was made")
    for n in notes:
        L.append("exposure note %s (%s): %s" % (n["at"][:16], n.get("source") or "-", n["why"]))
        L += ["  - " + _exp_item(x) for x in n.get("artefacts") or []]
        if n.get("also_found_by_scan"):
            L.append("  also overlapping (scan): " + ", ".join(n["also_found_by_scan"]))
    return L


def cmd_note(a):
    """Side looks (a table read, a quick script, an earlier backtest) count as trials."""
    if a.looks is None or a.looks < 1:
        raise ResearchError("--looks must be a whole number of looks, at least 1")
    if not (a.dataset or "").strip():
        raise ResearchError("--dataset: the data the looks were taken on (a split key such as angel:nifty100:5m)")
    if len((a.why or "").strip()) < MIN_WHY:
        raise ResearchError("--why must say what was looked at and how it was counted (%d+ characters)" % MIN_WHY)
    rec = append({"op": "note", "looks": int(a.looks), "dataset": a.dataset.strip(), "family": a.family or "",
                  "why": a.why.strip(), "author": a.author}, a.now_dt)
    recs, _ = read_registry()
    print("Noted %d look(s) on %s%s. Registry total now %d trials (%d explore + %d side looks); every later DSR "
          "uses it." % (rec["looks"], rec["dataset"], (" (%s)" % rec["family"]) if rec["family"] else "",
                        total_trials(recs), explore_trials(recs), side_looks(recs)))
    return 0


def cmd_exposure_note(a):
    """Record, append-only, that earlier artefacts already measured on a split's validate / holdout."""
    sp = le.read_splits().get(a.dataset)
    if not sp:
        raise ResearchError("no split %r in splits.json (research.py splits)" % a.dataset)
    if len((a.why or "").strip()) < MIN_WHY:
        raise ResearchError("--why must state the finding (%d+ characters)" % MIN_WHY)
    recs, _ = read_registry()
    source = (a.source or "").strip() or "researcher"
    for r in recs:
        if r.get("op") == "exposure_note" and r.get("dataset") == a.dataset and r.get("source") == source:
            raise ResearchError("an exposure note for %s from %r is already in the registry (%s): it is recorded "
                                "once" % (a.dataset, source, r["at"][:16]))
    scan = le.exposure(sp, a.dataset)
    picked = scan
    if a.artefact:
        want = list(a.artefact)
        picked = [x for x in scan if any(w == x["artefact"] or w == os.path.basename(x["artefact"])
                                         or x["artefact"].startswith(w) for w in want)]
        miss = [w for w in want if not any(w == x["artefact"] or w == os.path.basename(x["artefact"])
                                           or x["artefact"].startswith(w) for x in scan)]
        if miss:
            raise ResearchError("no overlap with %s's validate/holdout found for: %s" % (a.dataset, ", ".join(miss)))
    others = sorted(set(x["artefact"] for x in scan) - set(x["artefact"] for x in picked))
    rec = append({"op": "exposure_note", "dataset": a.dataset, "source": source, "why": a.why.strip(),
                  "windows": {"validate": sp["validate"], "holdout": sp["holdout"]}, "artefacts": picked,
                  "also_found_by_scan": others, "author": a.author}, a.now_dt)
    print("Exposure note for %s: %d overlap(s) recorded%s. Informational: the split is unchanged." % (
        a.dataset, len(rec["artefacts"]), (", %d more artefact(s) overlap per the scan" % len(others)) if others else ""))
    for line in exposure_lines(a.dataset, {"exposed_before": None}, [rec]):
        print("  " + line)
    return 0


def cmd_peers(a):
    s2i, groups = pr.load_sectors(a.sectors)
    dirs = [d for d in (a.bars.split(",") if a.bars else [bt.bars_dir("yahoo"), bt.bars_dir("angel")]) if d]
    excl = pr.holdout_ranges()  # locked holdout dates never feed peer statistics
    for line in pr.report(a.symbol, s2i, groups, dirs, excl, a.top):
        print(line)
    return 0


def cmd_events(a):
    import events as ev  # noqa: E402
    if a.action == "record":
        offline = [d for d in (a.offline or "").split(",") if d]
        getter = ev.Getter(offline or None)
        errs = []
        if offline:
            p = [x for x in (os.path.join(d, "EQUITY_L.csv") for d in offline) if os.path.exists(x)]
            equity = nse.parse_equity_list(nse.read_maybe_gz(p[0]).decode("utf-8", "replace")) if p else {}
        else:
            try:
                equity = nse.equity_list()
            except nse.NSEError as e:
                errs.append("EQUITY_L.csv: %s" % e)
                equity = {}
        resolver = ev.news.Resolver(equity)
        universe = None
        if a.universe != "all":
            s2i, _ = pr.load_sectors(a.sectors, allow_download=not offline)
            universe = set(s2i) or None
        prices = ev.OfflinePrices(offline) if offline else None
        out = ev.record(a.now_dt, getter, resolver, universe, None, prices, config(), a.max_symbols, errors=errs)
        print("events %s: %d new, %d in the feeds, %d snapshot(s) filled, %d missed, %d waiting (not due or no price "
              "yet), %d symbol(s) priced."
              % (nse.iso_ist(a.now_dt)[:16], out["new"], out["seen_in_feeds"], out["filled"], out["missed"],
                 out["pending"], out["symbols_priced"]))
        for e in out["errors"]:
            print("  note: " + e)
        return 0
    known, _ = ev.read_all()
    if a.action == "stats":
        s = ev.stats(known)
        print("Events recorded: %d. A category is testable at %d complete events over %d+ sessions." % (
            len(known), ev.MIN_EVENTS_TO_TEST, ev.MIN_EVENT_SESSIONS))
        for k, v in sorted(s.items()):
            print("  %-13s %5d events, %5d complete, %4d symbols, %3d sessions  %s" % (
                k, v["events"], v["complete"], v["symbols"], v["sessions"], "TESTABLE" if v["testable"] else "not yet"))
        return 0
    rows = ev.as_list(known)
    if a.symbol:
        rows = [r for r in rows if r["symbol"] == nse.clean_symbol(a.symbol)]
    for r in rows[-a.limit:]:
        sn = " ".join("%s=%s" % (k, (r["snaps"][k].get("price") if r["snaps"][k].get("price") is not None
                                     else r["snaps"][k].get("status"))) for k in ev.SNAPS if k in r["snaps"])
        print("%s %-12s %-12s %s  %s" % (r["detected"][:16], r["symbol"], r["category"], r["title"][:70], sn))
    return 0


def cmd_report(a):
    recs, problem = read_registry(strict=False)
    exps = experiments(recs)
    rows = [row_for(i, e, recs) for i, e in exps.items()]
    splits = le.read_splits()
    n = total_trials(recs)
    notes = [r for r in recs if r.get("op") == "note" and r.get("looks")]
    xnotes = exposure_notes(recs)
    made = nse.iso_ist(a.now_dt)
    windows = "; ".join("%s explore %s..%s, validate %s..%s, holdout %s..%s" % (
        k, r["explore"][0], r["explore"][1], r["validate"][0], r["validate"][1], r["holdout"][0], r["holdout"][1])
        for k, r in sorted(splits.items()))
    L = ["# Strategy research report", "",
         "Made %s. %d experiment(s), **%d trials** in all (%d parameter combinations explored + %d side looks logged "
         "with `research.py note`). Registry chain: %s." % (
             made, len(rows), n, explore_trials(recs), side_looks(recs),
             "intact" if problem is None else "BROKEN (" + problem + ")"), "",
         "A candidate needs every gate on the validate split; paper trading needs the one-time holdout too. "
         "Nothing here places an order.", "",
         "DSR = as gated, at the trial count then (N in brackets); DSR now = the same validate result re-stated at "
         "today's N = %d, which is the bar any of them would face today." % n, "",
         "| id | family | status | trials | trades | net R/trade | Rs/trade | 95% CI (R) | DSR (N then) | DSR now | gates "
         "| holdout |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append("| %s | %s | %s | %d | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r["id"], r["family"], r["status"], r["trials"], r["trades"] if r["trades"] is not None else "-",
            fr(r["net_R"]), fr(r["net_inr"], "%+.0f"),
            "[%s, %s]" % (fr(r["ci_low"]), fr(r["ci_high"])) if r["ci_low"] is not None else "-",
            ("%s (%d)" % (fr(r["dsr"], "%.2f"), r["trials_at_gate"])) if r["trials_at_gate"] is not None else "-",
            fr(r["dsr_now"], "%.2f"), r["gates_passed"] or "-", r["holdout"] or "-"))
    L += ["", "## Side looks (counted as trials)", ""]
    L += ["- %s: **%d** on %s%s: %s" % (x["at"][:10], x["looks"], x["dataset"],
                                        (" (%s)" % x["family"]) if x.get("family") else "", x["why"])
          for x in notes] or ["- none logged"]
    L += ["", "## Hypotheses", ""]
    for i, e in exps.items():
        L.append("- **%s** (%s): %s" % (i, e["log"]["family"], e["log"]["rationale"]))
        if e["gate"]:
            g = e["gate"][-1]
            L.append("  - gates: " + ", ".join("%s %s" % (k, "PASS" if v["pass"] else "FAIL") for k, v in ordered(g["gates"])))
    L += ["", "## Data windows (locked)", ""] + (["- " + w for w in windows.split("; ")] if windows else ["- none yet"])
    L += ["", "## Holdout exposure (what looked at validate / holdout dates outside the pipeline)", ""]
    for k, r in sorted(splits.items()):
        L.append("- **%s**" % k)
        L += [("    - " + x[4:]) if x.startswith("  - ") else ("  - " + x.strip())
              for x in exposure_lines(k, r, xnotes.get(k, []))]
    L += ["", "## Caveats", ""] + ["- " + c for c in CAVEATS]
    os.makedirs(rdir(), exist_ok=True)
    with open(os.path.join(rdir(), "report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    dash = {"made": made, "rs_experiments": rows,
            "rs_meta": [{"made": made, "total_trials": n, "explore_trials": explore_trials(recs),
                         "side_looks": side_looks(recs), "experiments": len(rows),
                         "side_looks_detail": "; ".join("%d %s: %s" % (x["looks"], x.get("family") or x["dataset"],
                                                                         x["why"]) for x in notes),
                         "holdout_exposure": "; ".join(
                             "%s: %s" % (k, ", ".join(sorted(set(x["artefact"] for nn in v for x in nn.get("artefacts") or []))))
                             for k, v in sorted(xnotes.items())),
                         "candidates": sum(1 for r in rows if r["status"] == "candidate"),
                         "paper": sum(1 for r in rows if r["status"] == "paper"),
                         "data_windows": windows, "registry_ok": problem is None, "caveats": " ".join(CAVEATS)}]}
    nse.write_json(os.path.join(rdir(), "research-dash.json"), dash)
    print("Wrote %s and %s (%d experiments, %d trials)." % (os.path.join(rdir(), "report.md"),
                                                             os.path.join(rdir(), "research-dash.json"), len(rows), n))
    return 0


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="Strategy research for NSE: log, explore, validate, gate, holdout once. "
                                             "Never places an order.")
    ap.add_argument("--trader-dir", help="the trader/ folder (default ./trader or TRADER_DIR)")
    ap.add_argument("--now", help='pretend it is this IST time, e.g. "2026-10-29 18:00"')
    ap.add_argument("--list-file", help="an NSE index list CSV for the universe instead of the cached one")
    ap.add_argument("--sectors", help="NSE's Nifty 500 list CSV for sector groups (default: the cached one)")
    ap.add_argument("--no-capital-cap", action="store_true",
                    help="no portfolio cap (capital / max_positions): per-trade stats only; recorded in the run")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("init")
    p = sub.add_parser("log")
    p.add_argument("--id", required=True)
    p.add_argument("--from-example", choices=EXAMPLES)
    p.add_argument("--family")
    p.add_argument("--rationale")
    p.add_argument("--rules")
    p.add_argument("--grid", help="JSON {param: [values]} (default: the lab file's META grid)")
    p.add_argument("--universe", default="nifty50")
    p.add_argument("--source", default="yahoo", choices=["yahoo", "angel"])
    p.add_argument("--bars", help="read bars from this folder instead of the cache (DIR[,DIR])")
    p.add_argument("--author", default=AUTHOR)
    p.add_argument("--kind", default="intraday", choices=["intraday", "swing"])
    p.add_argument("--notes")
    for name in ("explore", "validate", "gate", "pipeline", "holdout", "promote", "show"):
        p = sub.add_parser(name)
        p.add_argument("id")
        if name in ("validate", "pipeline"):
            p.add_argument("--folds", type=int, default=FOLDS)
        if name == "show":
            p.add_argument("--json", action="store_true")
    p = sub.add_parser("reject")
    p.add_argument("id")
    p.add_argument("--why")
    p = sub.add_parser("list")
    p.add_argument("--json", action="store_true")
    sub.add_parser("verify")
    sub.add_parser("splits")
    p = sub.add_parser("peers")
    p.add_argument("symbol")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--bars", help="folders with daily bars (default the yahoo and angel caches)")
    p = sub.add_parser("events")
    p.add_argument("action", choices=["record", "show", "stats"])
    p.add_argument("--offline", help="read feeds from these folders (tests)")
    p.add_argument("--universe", default="nifty500", choices=["nifty500", "all"])
    p.add_argument("--max-symbols", type=int, default=40, help="Yahoo price lookups per run (default 40)")
    p.add_argument("--symbol")
    p.add_argument("--limit", type=int, default=40)
    sub.add_parser("report")
    p = sub.add_parser("note", help="log side looks (measurements outside explore): they count as trials")
    p.add_argument("--looks", type=int, required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--why", required=True)
    p.add_argument("--family")
    p.add_argument("--author", default=AUTHOR)
    p = sub.add_parser("exposure-note", help="record that earlier artefacts already looked at a split's "
                                             "validate / holdout dates (append-only, once per source)")
    p.add_argument("--dataset", required=True)
    p.add_argument("--why", required=True)
    p.add_argument("--artefact", action="append", help="limit to these overlapping artefacts (file name or "
                                                       "'splits.json KEY'); default: everything the scan finds")
    p.add_argument("--source", help="who found it, e.g. 'audit 2026-10-29' (one note per dataset and source)")
    p.add_argument("--author", default=AUTHOR)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    if a.trader_dir:
        os.environ["TRADER_DIR"] = os.path.abspath(a.trader_dir)
    global CAPITAL_CAP
    CAPITAL_CAP = not a.no_capital_cap
    try:
        a.now_dt = nse.parse_now(a.now) if a.now else nse.now_ist().replace(microsecond=0)
        fn = {"init": cmd_init, "log": cmd_log, "explore": cmd_explore, "validate": cmd_validate, "gate": cmd_gate,
              "pipeline": cmd_pipeline, "holdout": cmd_holdout, "promote": cmd_promote, "reject": cmd_reject,
              "list": cmd_list, "show": cmd_show, "verify": cmd_verify, "splits": cmd_splits, "peers": cmd_peers,
              "events": cmd_events, "report": cmd_report, "note": cmd_note,
              "exposure-note": cmd_exposure_note}[a.cmd]
        return fn(a)
    except (nse.NSEError, ValueError) as e:
        print("research.py: %s" % e, file=sys.stderr)
        return 1
    except le.LookAheadError as e:
        print("research.py: LOOK-AHEAD: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
