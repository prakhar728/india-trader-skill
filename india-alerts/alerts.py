#!/usr/bin/env python3
"""Price alerts and the NSE clock for india-alerts. Standard library only.

    python3 alerts.py clock                          # pre-open / open / closed, next open (IST)
    python3 alerts.py quote RELIANCE --write         # quotes -> trader/prices.json
    python3 alerts.py quote --holdings --write       # plan, paper ledger and watchlist symbols
    python3 alerts.py add --from-plan                # stop, target and flat-by from trader/plan.json
    python3 alerts.py add --symbol RELIANCE --stop 1163.3
    python3 alerts.py add --symbol RELIANCE --target 1185.8
    python3 alerts.py add --at 15:05 --label "flat by" --before-close --symbol RELIANCE
    python3 alerts.py add --symbol INFY --pct 3
    python3 alerts.py add --opening-bell             # 09:15, once per trading day
    python3 alerts.py add --pre-open                 # 09:00 pre-open call auction, once per day
    python3 alerts.py check                          # rings what hit, once; exit 1 if nothing rang
    python3 alerts.py check --now "2026-10-29 15:05" --offline tests/fixtures/india-alerts
    python3 alerts.py list
    python3 alerts.py reset reliance-plan-stop

Rule types:
  above / target  the price is at or above "price"
  below / stop    the price is at or below "price"
  pct_move        the price moved "pct" percent or more from the previous close
                  (or from "ref"); "direction": "up" or "down"
  opening_bell    the NSE normal market opened (09:15 IST, or a special
                  session's start such as Muhurat trading); once per trading day
  pre_open        the 09:00-09:15 pre-open call auction started; once per day
  time            the IST clock passed "at" (HH:MM) on a trading day; once per
                  trading day. "before_close": true keeps it at least 5 minutes
                  before a special session's close (Muhurat trading is one hour)

A rule with "date" is watched on that IST date only (rules from a plan are for
the plan's day). An above / below alert rings ONCE and then stays quiet until
you reset it, unless it has "rearm": true (re-arms when the price crosses back).

Quotes (for alerts and sizing checks, never for timing an order):
  yahoo   Yahoo's free chart JSON for SYMBOL.NS, one request per symbol with a
          pause between (query1, then query2 on an error). Delayed, unofficial.
  dhan    Dhan's POST /v2/marketfeed/ltp, ONE call for every symbol, when
          DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are set (needs Dhan's Data plan).
          LTP carries no previous close: a pct_move rule then needs "ref".
  auto    (default) dhan when the keys are set, else yahoo; config data_source.
  --offline DIR reads DIR/yahoo-<SYMBOL>.json(.gz) or DIR/dhan-ltp.json.
"""

import argparse
import datetime
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "india-common"))
import nse  # noqa: E402

HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
PRICE_TYPES = {"above", "below", "stop", "target", "pct_move", "opening_bell", "pre_open", "time"}
NO_QUOTE = {"opening_bell", "pre_open", "time"}

_http = nse.http_get     # tests swap these two
_sleep = time.sleep


class AlertError(Exception):
    pass


def _paths(base=None):
    d = nse.trader_dir(base)
    return {"rules": os.path.join(d, "alerts.json"), "state": os.path.join(d, "state", "alerts.json"),
            "prices": os.path.join(d, "prices.json"), "plan": os.path.join(d, "plan.json"),
            "config": os.path.join(d, "config.json"), "paper": os.path.join(d, "paper.json"),
            "watchlist": os.path.join(d, "watchlist.json"), "broker": os.path.join(d, "broker.json")}


def _read(path, default):
    try:
        return nse.read_json(path, default) if path else default
    except nse.NSEError as e:
        raise AlertError(str(e))


def _hhmm(text):
    m = HHMM.match(str(text or "").strip())
    if not m:
        return None
    return datetime.time(int(m.group(1)), int(m.group(2)))


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x).rstrip("0").rstrip(".")


# ---------------------------------------------------------------- clock

def status(now, cfg=None):
    """nse.market_status plus what this skill needs: today's session times and a Muhurat note."""
    st = nse.market_status(now, cfg)
    d = now.date()
    s = nse.session(d, cfg)
    st["weekday"] = now.strftime("%A")
    st["open"] = st["state"] == "open"
    if s:
        st["session_open"], st["session_close"] = s[0].strftime("%H:%M"), s[1].strftime("%H:%M")
        st["special"] = nse.special_session(d, cfg) is not None
    if d.isoformat() in nse.MUHURAT and not nse.special_session(d, cfg):
        st["muhurat"] = ("Muhurat trading is on %s; NSE announces the hour separately. Add it to "
                         "trader/config.json: \"special_sessions\": {\"%s\": [\"HH:MM\", \"HH:MM\"]}."
                         % (d.isoformat(), d.isoformat()))
    if st["state"] == "open":
        st["reason"] = "special session" if st.get("special") else "normal market"
    elif st["state"] == "pre-open":
        st["reason"] = "pre-open call auction (orders 09:00-09:08, matching to 09:15)"
    elif s and now < s[0]:
        st["reason"] = "before the open"
    elif s:
        st["reason"] = "after the close"
    else:
        st["reason"] = st.get("why") or "closed"
    return st


def upcoming_muhurat(now, cfg=None, days=15):
    """A note when a Muhurat session without a set hour is coming up."""
    for k in sorted(nse.MUHURAT):
        d = datetime.date.fromisoformat(k)
        if 0 <= (d - now.date()).days <= days and not nse.special_session(d, cfg):
            return ("Muhurat trading is on %s (%s); NSE has not published the hour in this pack. When it does, "
                    "add \"special_sessions\": {\"%s\": [\"HH:MM\", \"HH:MM\"]} to trader/config.json."
                    % (k, d.strftime("%A"), k))
    return None


def _clock_time(iso):
    return iso[:16].replace("T", " ") if iso else ""


# ---------------------------------------------------------------- quotes

def parse_yahoo(data, sym):
    try:
        meta, bars = nse.parse_yahoo_chart(data)
    except (nse.NSEError, ValueError) as e:
        raise AlertError("Yahoo has no data for %s: %s" % (sym, str(e).replace("Yahoo: ", "", 1)))
    price = meta.get("regularMarketPrice")
    if price is None:
        raise AlertError("Yahoo has no price for %s" % sym)
    t = meta.get("regularMarketTime")
    return {"symbol": sym, "price": float(price), "open": bars[0]["o"] if bars else None,
            "high": meta.get("regularMarketDayHigh"), "low": meta.get("regularMarketDayLow"),
            "prev_close": meta.get("previousClose") or meta.get("chartPreviousClose"),
            "time": nse.iso_ist(nse.ist_from_epoch(t)) if t else "", "currency": meta.get("currency"),
            "source": "yahoo"}


def fetch_yahoo(sym):
    """query1 then query2. A 404 is Yahoo's own answer (unknown symbol): no second host."""
    last = None
    for i, host in enumerate(nse.YAHOO_HOSTS):
        if i:
            _sleep(nse.PAUSE)
        try:
            return _http(nse.yahoo_chart_url(sym, "1d", "1d", host))
        except nse.NSEError as e:
            last = e
            if "HTTP 404" in str(e):
                body = str(e)
                if "No data found" in body:
                    raise AlertError("Yahoo has no data for %s.NS: No data found, symbol may be delisted "
                                     "or renamed" % sym)
                break
    raise AlertError("Yahoo: %s" % last)


def _offline_bytes(folder, name):
    p = os.path.join(folder, name)
    for path in (p, p + ".gz"):
        if os.path.exists(path):
            return nse.read_maybe_gz(path)
    raise AlertError("no offline quote file %s" % p)


def yahoo_quotes(symbols, offline=None):
    quotes, errors = {}, []
    for i, sym in enumerate(symbols):
        try:
            if offline:
                data = _offline_bytes(offline, "yahoo-%s.json" % sym)
            else:
                if i:
                    _sleep(nse.PAUSE)
                data = fetch_yahoo(sym)
            quotes[sym] = parse_yahoo(data, sym)
        except AlertError as e:
            errors.append(str(e))
    return quotes, errors


def dhan_quotes(symbols, now, offline=None, scrip=None, client=None):
    """One POST /marketfeed/ltp for all symbols. The response shape, from Dhan's v2 docs
    (it cannot be recorded without keys): {"data": {"NSE_EQ": {"2885": {"last_price": 1167.4}}},
    "status": "success"}."""
    quotes, errors, ids = {}, [], {}
    for sym in symbols:
        try:
            ids[sym] = nse.security_id(sym, scrip)
        except nse.NSEError as e:
            errors.append(str(e))
    if not ids:
        return quotes, errors
    try:
        if offline:
            doc = json.loads(_offline_bytes(offline, "dhan-ltp.json"))
            data = ((doc.get("data") or {}).get("NSE_EQ") or {})
        else:
            data = (client or nse.Dhan.from_env()).ltp(list(ids.values()))
    except (nse.NSEError, ValueError) as e:
        return quotes, errors + ["Dhan: %s" % e]
    for sym, sid in ids.items():
        row = data.get(str(sid)) or data.get(sid)
        if not row or row.get("last_price") is None:
            errors.append("Dhan sent no price for %s (security id %s)" % (sym, sid))
            continue
        quotes[sym] = {"symbol": sym, "price": float(row["last_price"]), "prev_close": None,
                       "time": nse.iso_ist(now), "source": "dhan", "security_id": str(sid)}
    return quotes, errors


def pick_source(source, cfg=None, env=None):
    want = source or (cfg or {}).get("data_source") or "auto"
    if want == "auto":
        return "dhan" if nse.dhan_credentials(env) else "yahoo"
    if want == "dhan" and not nse.dhan_credentials(env):
        raise AlertError("data source dhan needs DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in your shell "
                         "(never paste the token in chat)")
    return want


def get_quotes(symbols, source="yahoo", offline=None, now=None, scrip_file=None):
    symbols = [nse.clean_symbol(s) for s in symbols]
    if source == "dhan":
        try:
            scrip = nse.scrip_master(offline=scrip_file)
        except nse.NSEError as e:
            return {}, ["Dhan's instrument file: %s" % e]
        return dhan_quotes(symbols, now or nse.now_ist(), offline, scrip)
    return yahoo_quotes(symbols, offline)


def write_prices(path, quotes, now, source):
    old = _read(path, {}) or {}
    prices = dict(old.get("prices", {}))
    detail = dict(old.get("detail", {}))
    stamp = nse.iso_ist(now)
    for s, q in quotes.items():
        prices[s] = q["price"]
        detail[s] = {"prev_close": q.get("prev_close"), "time": q.get("time"), "source": q.get("source"),
                     "fetched": stamp}
    doc = {"as_of": stamp, "source": source,
           "note": ("Delayed quotes for alerts and sizing checks, in rupees. Your broker's price is the one that "
                    "fills. detail.fetched is when each price was read (IST)."),
           "prices": prices, "detail": detail}
    nse.write_json(path, doc)
    return doc


# ---------------------------------------------------------------- rules

def validate(rule):
    t = rule.get("type")
    if t not in PRICE_TYPES:
        raise AlertError("rule %s: unknown type %r" % (rule.get("id"), t))
    if t not in NO_QUOTE and not rule.get("symbol"):
        raise AlertError("rule %s needs a symbol" % rule.get("id"))
    if t in ("above", "below", "stop", "target") and not isinstance(rule.get("price"), (int, float)):
        raise AlertError("rule %s needs a numeric price" % rule.get("id"))
    if t == "pct_move" and not isinstance(rule.get("pct"), (int, float)):
        raise AlertError("rule %s needs a numeric pct" % rule.get("id"))
    if t == "time" and _hhmm(rule.get("at")) is None:
        raise AlertError("rule %s needs \"at\" as an IST time like \"15:05\"" % rule.get("id"))
    if rule.get("date") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(rule["date"])):
        raise AlertError("rule %s: date must be YYYY-MM-DD" % rule.get("id"))


def active(rule, today):
    return not rule.get("date") or str(rule["date"]) == today


def _rang_today(stamp, today):
    return bool(stamp) and str(stamp)[:10] == today


def ring_time(rule, now, cfg=None):
    """When a time rule rings today. "before_close" keeps it 5 minutes before a special session's close."""
    at = _hhmm(rule.get("at"))
    if rule.get("before_close"):
        sp = nse.special_session(now.date(), cfg)
        if sp:
            latest = (datetime.datetime.combine(now.date(), sp[1]) - datetime.timedelta(minutes=5)).time()
            if at > latest or at < sp[0]:
                at = latest
    return at


def evaluate(rules, quotes, st, state, now, cfg=None):
    """Pure function. Returns (rings, new_state). Each ring: {id, symbol, message}."""
    state = json.loads(json.dumps(state or {}))
    rung = state.setdefault("rung", {})
    today = st["date"]
    stamp = nse.iso_ist(now)
    rings = []
    for r in rules:
        validate(r)
        rid = r.get("id") or "%s-%s" % (r.get("symbol", "market"), r["type"])
        if not active(r, today):
            continue
        typ = {"stop": "below", "target": "above"}.get(r["type"], r["type"])
        if typ == "opening_bell":
            if st["state"] == "open" and not _rang_today(rung.get(rid), today):
                what = "special session" if st.get("special") else "normal market"
                rings.append({"id": rid, "symbol": "", "message": "Opening bell: NSE is open (%s IST, %s %s to %s)."
                              % (now.strftime("%H:%M"), what, st.get("session_open"), st.get("session_close"))})
                rung[rid] = stamp
            continue
        if typ == "pre_open":
            if st["state"] == "pre-open" and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": "", "message": "Pre-open: NSE's call auction is running "
                              "(%s IST; orders 09:00-09:08, the normal market opens at 09:15)." % now.strftime("%H:%M")})
                rung[rid] = stamp
            continue
        if typ == "time":
            at = ring_time(r, now, cfg)
            if st.get("session_open") and now.time() >= at and not _rang_today(rung.get(rid), today):
                label = r.get("label") or "time alert"
                msg = "%s%s: it is %s IST (set for %s)" % (label[:1].upper(), label[1:], now.strftime("%H:%M"),
                                                           at.strftime("%H:%M"))
                msg += (", NSE closes at %s." % st["session_close"]) if st["open"] else ", the market has closed."
                sym = str(r.get("symbol") or "").upper()
                if sym:
                    msg += (" If %s is still open, close it: india-broker flatten --symbol %s builds the sell "
                            "ticket, then india-risk and your yes. Dhan squares off intraday positions itself at "
                            "15:10." % (sym, sym))
                rings.append({"id": rid, "symbol": sym, "message": msg})
                rung[rid] = stamp
            continue
        sym = r["symbol"].upper()
        q = quotes.get(sym)
        if not q:
            continue
        p = q["price"]
        if typ in ("above", "below"):
            hit = p >= r["price"] if typ == "above" else p <= r["price"]
            if hit and rid not in rung:
                word = "at or above" if typ == "above" else "at or below"
                label = {"stop": "stop", "target": "target"}.get(r["type"], "alert level")
                rings.append({"id": rid, "symbol": sym, "message": "%s %s %s: %s your %s %s%s."
                              % (sym, "is" if st["open"] else "last", nse.inr(p), word, label, nse.inr(r["price"]),
                                 (" (" + r["note"] + ")") if r.get("note") else "")})
                rung[rid] = stamp
            elif not hit and rid in rung and r.get("rearm"):
                del rung[rid]
        elif typ == "pct_move":
            ref = r.get("ref") or q.get("prev_close")
            if not ref:
                state.setdefault("notes", {})[rid] = ("%s: no previous close in the %s quote; give the rule a "
                                                      "\"ref\" price" % (sym, q.get("source")))
                continue
            move = 100.0 * (p / float(ref) - 1)
            want = r.get("direction")
            hit = abs(move) >= r["pct"] and (not want or (want == "up") == (move > 0))
            if hit and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": sym, "message": "%s moved %+.2f%% to %s (from %s)."
                              % (sym, move, nse.inr(p), nse.inr(float(ref)))})
                rung[rid] = stamp
    state["last_check"] = stamp
    return rings, state


def rules_from_plan(plan, cfg=None):
    """trader/plan.json -> a stop alert, a target alert and a flat-by time alert, for the plan's day."""
    if not isinstance(plan, dict) or not plan.get("symbol"):
        raise AlertError("the plan has no symbol (run india-exit first)")
    sym = nse.clean_symbol(plan["symbol"])
    out = []
    for key, word in (("stop", "plan stop"), ("target", "plan target")):
        v = plan.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            raise AlertError("the plan has no %s (run india-exit)" % key)
        rule_txt = plan.get("%s_rule" % key)
        out.append({"id": "%s-plan-%s" % (sym.lower(), key), "symbol": sym, "type": key, "price": float(v),
                    "note": word + (": " + rule_txt if rule_txt else "")})
    c = dict(cfg or {})
    if plan.get("flat_by"):
        c["flat_by"] = plan["flat_by"]
    try:
        flat = nse.flat_by(c).strftime("%H:%M")
    except nse.NSEError:
        raise AlertError("the plan's flat_by %r is not a time like 15:05" % plan.get("flat_by"))
    out.append({"id": "%s-flat-by" % sym.lower(), "symbol": sym, "type": "time", "at": flat,
                "label": "flat by", "before_close": True})
    if plan.get("date"):
        for r in out:
            r["date"] = str(plan["date"])
    return out


# ---------------------------------------------------------------- holdings

def symbols_in(doc):
    """Symbols from a plan, paper ledger, broker log or watchlist, whatever its exact shape."""
    out = []

    def add(x):
        try:
            s = nse.clean_symbol(x)
        except nse.NSEError:
            return
        if s not in out:
            out.append(s)

    def walk(node, depth=0):
        if depth > 4:
            return
        if isinstance(node, dict):
            for k in ("symbol", "ticker"):
                if isinstance(node.get(k), str):
                    add(node[k])
            for k in ("positions", "holdings", "open", "symbols", "items", "watchlist", "setups", "rows"):
                v = node.get(k)
                if isinstance(v, dict):
                    for kk, vv in v.items():
                        if isinstance(vv, dict) and not (vv.get("symbol") or vv.get("ticker")):
                            if (vv.get("qty") or vv.get("quantity") or 0) != 0 or k != "positions":
                                add(kk)
                        else:
                            walk(vv, depth + 1)
                elif isinstance(v, list):
                    walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                if isinstance(v, str):
                    add(v)
                else:
                    walk(v, depth + 1)
    walk(doc)
    return out


def holdings(paths):
    syms = []
    for key in ("plan", "paper", "watchlist"):
        for s in symbols_in(_read(paths[key], None)):
            if s not in syms:
                syms.append(s)
    return syms


def _dedupe(items):
    out = []
    for s in items:
        if s and s not in out:
            out.append(s)
    return out


# ---------------------------------------------------------------- commands

def cmd_clock(a, paths, cfg):
    now = nse.parse_now(a.now)
    st = status(now, cfg)
    note = upcoming_muhurat(now, cfg)
    if a.json:
        if note:
            st["muhurat_note"] = note
        print(json.dumps(st, indent=2))
        return 0 if st["open"] else 1
    print("IST %s (%s): NSE %s, %s." % (now.strftime("%Y-%m-%d %H:%M"), st["weekday"],
                                        st["state"].upper(), st["reason"]))
    if st.get("session_open"):
        print("Today's session: %s to %s IST%s." % (st["session_open"], st["session_close"],
                                                    " (special session)" if st.get("special") else
                                                    " (pre-open from 09:00; F&O stocks' continuous trading ends 15:15 "
                                                    "for the closing auction)"))
    if st["state"] == "open":
        print("%d minutes to the close. Flat by %s (Dhan squares off intraday at 15:10)."
              % (st["minutes_to_close"], nse.flat_by(cfg, now.date()).strftime("%H:%M")))
    elif st.get("next_open"):
        nxt = datetime.datetime.fromisoformat(st["next_open"][:19])
        print("Next open: %s IST (%s)." % (_clock_time(st["next_open"]), nxt.strftime("%A")))
    for k in ("muhurat", "warning"):
        if st.get(k):
            print(st[k])
    if note and not st.get("muhurat"):
        print(note)
    return 0 if st["open"] else 1


def cmd_quote(a, paths, cfg):
    syms = _dedupe([nse.clean_symbol(s) for s in a.symbols] + (holdings(paths) if a.holdings else []))
    if not syms:
        raise AlertError("which symbols? alerts.py quote RELIANCE INFY, or --holdings")
    now = nse.parse_now(a.now)
    src = pick_source(a.source, cfg)
    quotes, errors = get_quotes(syms, src, a.offline, now, a.scrip)
    out = a.write if a.write != "-" else None
    if out == "default":
        out = paths["prices"]
    if out and quotes:
        write_prices(out, quotes, now, src)
    if a.json:
        print(json.dumps({"quotes": quotes, "errors": errors}, indent=2))
    else:
        for s in syms:
            q = quotes.get(s)
            if q:
                chg = ""
                if q.get("prev_close"):
                    chg = " (%+.2f%% vs prev close %s)" % (100.0 * (q["price"] / q["prev_close"] - 1),
                                                          nse.inr(q["prev_close"]))
                print("%-10s %s%s  %s  [%s]" % (s, nse.inr(q["price"]), chg, _clock_time(q.get("time")), q["source"]))
        for e in errors:
            print("error: " + e)
        if out and quotes:
            print("Wrote %d price(s) to %s." % (len(quotes), out))
        print("Quotes are delayed (Yahoo) or a snapshot (Dhan LTP): for alerts and sizing only. "
              "Your broker's price is the one that fills.")
    return 0 if quotes else 1


def cmd_check(a, paths, cfg):
    doc = _read(a.rules or paths["rules"], None)
    if not doc or not doc.get("rules"):
        raise AlertError("no rules in %s: add some with alerts.py add --from-plan" % (a.rules or paths["rules"]))
    rules = doc["rules"]
    now = nse.parse_now(a.now)
    st = status(now, cfg)
    watched = [r for r in rules if active(r, st["date"])]
    syms = _dedupe([str(r.get("symbol", "")).upper() for r in watched if r.get("type") not in NO_QUOTE])
    src = pick_source(a.source, cfg)
    quotes, errors = (get_quotes(syms, src, a.offline, now, a.scrip) if syms else ({}, []))
    state_path = a.state or paths["state"]
    state = _read(state_path, {}) or {}
    rings, state = evaluate(rules, quotes, st, state, now, cfg)
    if not a.no_write:
        nse.write_json(state_path, state)
        if quotes:
            write_prices(a.prices_out or paths["prices"], quotes, now, src)
    if a.json:
        print(json.dumps({"market": st, "rings": rings, "errors": errors,
                          "quotes": dict((s, q["price"]) for s, q in quotes.items())}, indent=2))
    else:
        print("NSE %s (%s IST, %s)." % (st["state"].upper(), now.strftime("%Y-%m-%d %H:%M"), st["reason"]))
        for r in rings:
            print("ALERT " + r["message"])
        if not rings:
            print("Nothing hit. %d rule(s) watched today%s." % (
                len(watched), (", " + ", ".join("%s %s" % (s, nse.inr(quotes[s]["price"])) for s in syms if s in quotes))
                if quotes else ""))
        for n in (state.get("notes") or {}).values():
            print("note: " + n)
        for e in errors:
            print("error: " + e)
        if rings:
            print("An alert is information, not an order. A Dhan super order's stop-loss and target legs already "
                  "sit at the broker: check the position and record any fill (india-broker fill). To close it "
                  "yourself: india-broker flatten, then india-risk and your yes.")
    return 0 if rings else 1


def cmd_add(a, paths, cfg):
    rules_path = a.rules or paths["rules"]
    state_path = a.state or paths["state"]
    doc = _read(rules_path, {"rules": []}) or {"rules": []}
    doc.setdefault("rules", [])
    if a.from_plan:
        plan_path = a.plan or paths["plan"]
        plan = _read(plan_path, None)
        if plan is None:
            raise AlertError("no plan at %s (run india-exit first)" % plan_path)
        new = rules_from_plan(plan, cfg)
        ids = set(r["id"] for r in new)
        doc["rules"] = [r for r in doc["rules"] if r.get("id") not in ids] + new
        state = _read(state_path, {}) or {}
        if state.get("rung"):
            for i in ids:
                state["rung"].pop(i, None)
            nse.write_json(state_path, state)
        nse.write_json(rules_path, doc)
        for r in new:
            print("Added %s: %s" % (r["id"], json.dumps(r)))
        print("Run alerts.py check every few minutes while the trade is open (a scheduled task or /loop). "
              "Quotes are delayed; the broker's stop-loss leg is what actually stops you out.")
        return 0
    if a.opening_bell:
        rule = {"id": a.id or "opening-bell", "type": "opening_bell"}
    elif a.pre_open:
        rule = {"id": a.id or "pre-open", "type": "pre_open"}
    elif a.at:
        if _hhmm(a.at) is None:
            raise AlertError("--at wants an IST time like 15:05")
        rule = {"id": a.id or ("time-%s" % a.at.replace(":", "")), "type": "time", "at": a.at,
                "label": a.label or "time alert"}
        if a.symbol:
            rule["symbol"] = nse.clean_symbol(a.symbol)
        if a.before_close:
            rule["before_close"] = True
    else:
        if not a.symbol:
            raise AlertError("--symbol is required (or --from-plan, --at, --opening-bell, --pre-open)")
        sym = nse.clean_symbol(a.symbol)
        if a.above is not None:
            rule = {"type": "above", "price": a.above}
        elif a.below is not None:
            rule = {"type": "below", "price": a.below}
        elif a.stop is not None:
            rule = {"type": "stop", "price": a.stop}
        elif a.target is not None:
            rule = {"type": "target", "price": a.target}
        elif a.pct is not None:
            rule = {"type": "pct_move", "pct": a.pct}
            if a.direction:
                rule["direction"] = a.direction
            if a.ref:
                rule["ref"] = a.ref
        else:
            raise AlertError("give one of --above, --below, --stop, --target or --pct")
        val = rule.get("price", rule.get("pct"))
        r2 = {"id": a.id or ("%s-%s-%s" % (sym.lower(), rule["type"], _n(val))), "symbol": sym}
        r2.update(rule)
        rule = r2
        if a.rearm:
            rule["rearm"] = True
        if a.note:
            rule["note"] = a.note
    if a.date:
        rule["date"] = a.date
    validate(rule)
    if any(r.get("id") == rule["id"] for r in doc["rules"]):
        raise AlertError("a rule with id %s already exists" % rule["id"])
    doc["rules"].append(rule)
    nse.write_json(rules_path, doc)
    print("Added %s: %s" % (rule["id"], json.dumps(rule)))
    return 0


def cmd_list(a, paths, cfg):
    doc = _read(a.rules or paths["rules"], {"rules": []}) or {"rules": []}
    state = _read(a.state or paths["state"], {}) or {}
    if not doc.get("rules"):
        print("No rules yet.")
        return 1
    for r in doc["rules"]:
        rid = r.get("id")
        rung = state.get("rung", {}).get(rid)
        print("%-26s %s%s" % (rid, json.dumps(dict((k, v) for k, v in r.items() if k != "id")),
                              ("  (rang %s)" % rung) if rung else ""))
    return 0


def cmd_reset(a, paths, cfg):
    state_path = a.state or paths["state"]
    state = _read(state_path, {}) or {}
    if a.id not in state.get("rung", {}):
        print("%s has not rung (nothing to reset)." % a.id)
        return 1
    del state["rung"][a.id]
    nse.write_json(state_path, state)
    print("Re-armed %s." % a.id)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Price alerts and the NSE clock (india-alerts).")
    ap.add_argument("--trader-dir", help="the trader/ folder (default $TRADER_DIR or ./trader)")
    sub = ap.add_subparsers(dest="cmd")

    def net(p):
        p.add_argument("--source", choices=["auto", "yahoo", "dhan"], help="default: config data_source, else auto")
        p.add_argument("--offline", metavar="DIR", help="read yahoo-<SYMBOL>.json / dhan-ltp.json, no network")
        p.add_argument("--scrip", metavar="FILE", help="Dhan instrument CSV to read instead of downloading it")
        p.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-29 15:05\"")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("clock", help="is NSE open?")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("quote", help="latest quotes")
    net(p)
    p.add_argument("symbols", nargs="*")
    p.add_argument("--holdings", action="store_true", help="add symbols from the plan, paper ledger and watchlist")
    p.add_argument("--write", nargs="?", const="default", metavar="PATH",
                   help="save to prices.json (default trader/prices.json)")
    p = sub.add_parser("check", help="check every rule, ring what hit")
    net(p)
    p.add_argument("--rules")
    p.add_argument("--state")
    p.add_argument("--prices-out")
    p.add_argument("--no-write", action="store_true")
    p = sub.add_parser("add", help="add a rule")
    p.add_argument("--rules")
    p.add_argument("--state")
    p.add_argument("--symbol")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--above", type=float)
    g.add_argument("--below", type=float)
    g.add_argument("--stop", type=float)
    g.add_argument("--target", type=float)
    g.add_argument("--pct", type=float)
    g.add_argument("--opening-bell", action="store_true", help="09:15 IST, once per trading day")
    g.add_argument("--pre-open", action="store_true", help="09:00 IST pre-open, once per trading day")
    g.add_argument("--at", metavar="HH:MM", help="a time alert, IST")
    g.add_argument("--from-plan", action="store_true", help="stop, target and flat-by from the plan")
    p.add_argument("--plan")
    p.add_argument("--label", help="what a time alert says, e.g. \"flat by\"")
    p.add_argument("--before-close", action="store_true",
                   help="a time alert kept 5 minutes before a special session's close")
    p.add_argument("--date", help="watch this rule on this IST date only (YYYY-MM-DD)")
    p.add_argument("--direction", choices=["up", "down"])
    p.add_argument("--ref", type=float, help="pct_move from this price instead of the previous close")
    p.add_argument("--rearm", action="store_true")
    p.add_argument("--note")
    p.add_argument("--id")
    p = sub.add_parser("list", help="list the rules")
    p.add_argument("--rules")
    p.add_argument("--state")
    p = sub.add_parser("reset", help="re-arm a rule that rang")
    p.add_argument("id")
    p.add_argument("--state")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    paths = _paths(a.trader_dir)
    try:
        cfg = _read(paths["config"], {}) or {}
        return {"clock": cmd_clock, "quote": cmd_quote, "check": cmd_check, "add": cmd_add,
                "list": cmd_list, "reset": cmd_reset}[a.cmd](a, paths, cfg)
    except (AlertError, nse.NSEError) as e:
        print("alerts.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
