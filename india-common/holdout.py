#!/usr/bin/env python3
"""Research holdouts, respected OUTSIDE the research lab. Standard library only.

india-research cuts each dataset once into explore / validate / a locked holdout and saves
the windows in trader/research/splits.json. The lab's loader never hands a holdout bar to a
strategy, but any other tool that measures a rule on those dates (an india-backtest run, an
options backtest, a swing backtest) spends the holdout just the same: the result is seen,
and the holdout is no longer unseen. So the backtesters withhold those dates by default:

    import holdout
    wins = holdout.windows(["RELIANCE", "TCS"])            # {SYM: [(from, to, dataset)]}
    kept, info = holdout.withhold(sessions_by_symbol)        # drops sessions inside any window
    print(holdout.note(info), file=sys.stderr)               # one line: what was withheld

`allow=True` (a tool's --allow-holdout) keeps everything and says so (info["holdout_included"]).
Matching is by symbol: a dataset's holdout applies to every symbol in its split record
(an index such as NIFTY included). Rows are anything with a `.date` (or pass date_of=).
Not financial advice.
"""

import os

import nse


def splits_path(base=None):
    return os.path.join(nse.trader_dir(base), "research", "splits.json")


def read_splits(base=None):
    try:
        return nse.read_json(splits_path(base), {}) or {}
    except nse.NSEError:
        return {}


def windows(symbols=None, base=None, splits=None):
    """{SYM: [(from, to, dataset_key)]} for the symbols given (None: every symbol in any split)."""
    doc = read_splits(base) if splits is None else splits
    want = None if symbols is None else set(symbols)
    out = {}
    for key, rec in sorted(doc.items()):
        h = rec.get("holdout")
        if not h or len(h) != 2:
            continue
        for s in rec.get("symbols") or []:
            if want is None or s in want:
                out.setdefault(s, []).append((h[0], h[1], key))
    return out


def inside(date, wins):
    """The dataset whose holdout holds this date ('YYYY-MM-DD'), or None."""
    for a, z, key in wins or ():
        if a <= date <= z:
            return key
    return None


def _date(x):
    d = getattr(x, "date", x)
    return d if isinstance(d, str) else str(d)[:10]


def withhold(rows_by_symbol, base=None, allow=False, date_of=None, splits=None):
    """-> (kept {SYM: [rows]}, info). Drops every row dated inside a holdout window of a split
    that lists the symbol, unless allow. info: holdout_included, withheld_sessions (distinct
    dates), withheld_stock_days, by_dataset {key: {"from", "to", "dates", "symbols"}}."""
    date_of = date_of or _date
    wins = windows(list(rows_by_symbol), base, splits)
    kept, dates, by = {}, set(), {}
    n = 0
    for sym, rows in rows_by_symbol.items():
        w = wins.get(sym)
        if not w or allow:
            kept[sym] = list(rows)
            if allow and w:
                for r in rows:
                    key = inside(date_of(r), w)
                    if key:
                        d = date_of(r)
                        b = by.setdefault(key, {"dates": set(), "symbols": set()})
                        b["dates"].add(d)
                        b["symbols"].add(sym)
            continue
        out = []
        for r in rows:
            d = date_of(r)
            key = inside(d, w)
            if key:
                n += 1
                dates.add(d)
                b = by.setdefault(key, {"dates": set(), "symbols": set()})
                b["dates"].add(d)
                b["symbols"].add(sym)
            else:
                out.append(r)
        if out:
            kept[sym] = out
    info = {"holdout_included": bool(allow), "withheld_sessions": 0 if allow else len(dates),
            "withheld_stock_days": 0 if allow else n,
            "by_dataset": dict((k, {"from": min(v["dates"]), "to": max(v["dates"]), "dates": len(v["dates"]),
                                    "symbols": len(v["symbols"])}) for k, v in sorted(by.items()))}
    return kept, info


def note(info, flag="--allow-holdout"):
    """One line for the console (stderr, so --json output stays clean)."""
    by = info.get("by_dataset") or {}
    where = "; ".join("%s %s..%s, %d symbol(s)" % (k, v["from"], v["to"], v["symbols"]) for k, v in by.items())
    if info.get("holdout_included"):
        if not by:
            return "Holdout: %s given, but no session here falls in a research holdout." % flag
        return ("Holdout INCLUDED (%s): sessions inside research holdouts are in this result (%s). Recorded as "
                "holdout_included in the output; it spends those holdouts' evidential value." % (flag, where))
    if not info.get("withheld_sessions"):
        return None
    return ("Holdout: withheld %d session(s) (%d stock-days) inside locked research holdouts (%s); pass %s "
            "to include them." % (info["withheld_sessions"], info["withheld_stock_days"], where, flag))
