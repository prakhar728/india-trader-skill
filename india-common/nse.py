#!/usr/bin/env python3
"""India core shared by every india-* skill: the NSE clock, holidays, tick
sizes, intraday charges, symbols, the trader/ folder, and the two price
sources (Yahoo's free chart JSON and the Dhan Data API). Standard library only.

Every india-* script imports it like this, so the skill folders and this one
must sit side by side (as they do in ~/.claude/skills/ or a repo's
.claude/skills/):

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "india-common"))
    import nse

Time. India has no daylight saving: IST is always UTC+05:30. Every time this
module hands out is naive IST (datetime without tzinfo) unless the name says utc.

The cash market (NSE and BSE, equity):
    09:00-09:08  pre-open order entry (call auction), 09:08-09:15 matching
    09:15-15:30  normal market
    15:30-15:40  closing price calculation, 15:40-16:00 post-close session
  From August 3 2026 NSE runs a closing auction session (CAS) for F&O stocks:
  continuous trading in those ends 15:15 (order entry 15:20 to the random
  close). Yahoo's 5-minute NSE bars for an F&O stock therefore stop at 15:15
  (no 15:20 bar, and a 15:25 bar only on some days), and it skips a bar here
  and there: expect 72 to 75 bars a session, never assume 75. Brokers square off intraday earlier: Dhan's equity intraday auto
  square-off is 15:10, so the pack's default flat-by is 15:05.

Holidays: NSE's own 2026 cash-market list (api/holiday-master, segment CM).
2027 is not published yet (NSE publishes it in December); until then a 2027
date is a trading day on weekdays and the scripts warn. Add closures or
special sessions in trader/config.json (past full-day special sessions at normal hours, such as
the Sunday Union Budget session of 2026-02-01, are built in: KNOWN_SPECIAL_SESSIONS):
    "extra_holidays": {"2027-01-26": "Republic Day"},
    "special_sessions": {"2026-11-08": ["18:00", "19:00"]}   # Muhurat trading

Tick sizes (NSE, revised April 15 2025, set each month from the last
month-end close): below 250 -> 0.01, 250-1,000 -> 0.05, 1,000-5,000 -> 0.10,
5,000-10,000 -> 0.50, 10,000-20,000 -> 1.00, above 20,000 -> 5.00. Dhan's
instrument file carries the exchange's tick per stock (SEM_TICK_SIZE, in
paise) and that wins when it is known.

Charges (Dhan, intraday equity, per its pricing page): brokerage the lower of
Rs 20 or 0.03 % per executed order; STT 0.025 % on the sell side; exchange
transaction charges NSE 0.0030699 %, BSE 0.00375 %; SEBI fee 0.0001 %; stamp
duty 0.003 % on the buy side; IPFT 0.0000001 %; GST 18 % on brokerage +
exchange + SEBI + IPFT. STT and stamp duty round to the rupee.
"""

import csv
import datetime
import gzip
import io
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; india-trader-skill/1.0)"
IST_OFFSET = datetime.timedelta(hours=5, minutes=30)
EPOCH = datetime.datetime(1970, 1, 1)

PRE_OPEN = datetime.time(9, 0)
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
CAS_START = datetime.date(2026, 8, 3)      # NSE closing auction session for F&O stocks
DHAN_SQUARE_OFF = datetime.time(15, 10)    # Dhan equity intraday auto square-off
DEFAULT_FLAT_BY = datetime.time(15, 5)

# NSE cash market (CM) 2026, from NSE's holiday master. Weekend entries are
# kept for the record; they close nothing extra.
NSE_HOLIDAYS = {
    "2026-01-15": "Municipal Corporation Election - Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-02-15": "Mahashivratri",
    "2026-03-03": "Holi",
    "2026-03-21": "Id-Ul-Fitr (Ramadan Eid)",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-08-15": "Independence Day",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-08": "Diwali Laxmi Pujan (Muhurat trading in the evening)",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
CALENDAR_YEARS = (2026,)
# Muhurat trading: NSE confirmed Sunday November 8 2026; the hour is announced
# separately, so it is not hard-coded. Set it in config "special_sessions".
MUHURAT = {"2026-11-08": None}
# Past special sessions held at NORMAL hours on a day that is otherwise shut (a weekend). They are
# trading days for calendar code (is_trading_day, session, previous_trading_day), but NOT
# special_session()s: that function means "unusual hours" (Muhurat) and backtests leave those out,
# while these sessions ran 09:15-15:30 and the bar data treats them as normal sessions.
KNOWN_SPECIAL_SESSIONS = {
    "2026-02-01": "Union Budget 2026 (Sunday): NSE special live trading session, normal hours",
}

TICK_BANDS = ((250, 0.01), (1000, 0.05), (5000, 0.10), (10000, 0.50), (20000, 1.00), (None, 5.00))

CHARGES = {
    "brokerage_pct": 0.03, "brokerage_cap": 20.0,
    "stt_sell_pct": 0.025,
    "exchange_pct": {"NSE": 0.0030699, "BSE": 0.00375},
    "sebi_pct": 0.0001, "stamp_buy_pct": 0.003, "ipft_pct": 0.0000001,
    "gst_pct": 18.0,
}

YAHOO_HOSTS = ("query1", "query2")
PAUSE = 0.5
BACKOFF = 5.0
DHAN_BASE = "https://api.dhan.co/v2"
DHAN_SCRIP_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
NSE_EQUITY_LIST = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
NIFTY500_LIST = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
# EQ trades rolling (intraday allowed); BE and BZ are trade-to-trade (every
# trade must be delivered, no intraday); SM/ST are SME boards.
INTRADAY_SERIES = ("EQ",)
T2T_SERIES = ("BE", "BZ")


class NSEError(Exception):
    pass


# ---------------------------------------------------------------- time

def ist_from_utc(utc):
    return utc.replace(tzinfo=None) + IST_OFFSET


def utc_from_ist(ist):
    return ist.replace(tzinfo=None) - IST_OFFSET


def ist_from_epoch(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts)) + IST_OFFSET


def epoch_from_ist(ist):
    return int((utc_from_ist(ist) - EPOCH).total_seconds())


def now_ist():
    return ist_from_utc(datetime.datetime.utcnow())


def parse_now(text):
    """'2026-10-29 10:15' or '2026-10-29T10:15:00' (IST), or None for the real clock."""
    if not text:
        return now_ist()
    t = text.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(t[:19], fmt)
        except ValueError:
            continue
    raise NSEError("bad time %r: use 'YYYY-MM-DD HH:MM' (IST)" % text)


def hhmm(text):
    if isinstance(text, datetime.time):
        return text
    try:
        h, m = str(text).strip().split(":")[:2]
        return datetime.time(int(h), int(m))
    except (ValueError, AttributeError):
        raise NSEError("bad time of day %r: use HH:MM (IST, 24-hour)" % (text,))


def iso_ist(dt):
    return dt.replace(microsecond=0).isoformat() + "+05:30"


# ---------------------------------------------------------------- calendar

def _cfg_holidays(cfg):
    return dict((cfg or {}).get("extra_holidays") or {})


def holiday_name(d, cfg=None):
    key = d.isoformat()
    extra = _cfg_holidays(cfg)
    return extra.get(key) or NSE_HOLIDAYS.get(key)


def special_session(d, cfg=None):
    """(open, close) for a special session such as Muhurat trading, or None."""
    spec = ((cfg or {}).get("special_sessions") or {}).get(d.isoformat())
    if not spec:
        return None
    return hhmm(spec[0]), hhmm(spec[1])


def calendar_known(d):
    return d.year in CALENDAR_YEARS


def is_trading_day(d, cfg=None):
    if special_session(d, cfg):
        return True
    if d.isoformat() in KNOWN_SPECIAL_SESSIONS and d.isoformat() not in _cfg_holidays(cfg):
        return True
    if d.weekday() >= 5:
        return False
    return holiday_name(d, cfg) is None


def session(d, cfg=None):
    """(open, close) as naive IST datetimes, or None when the market is shut."""
    sp = special_session(d, cfg)
    if sp:
        return datetime.datetime.combine(d, sp[0]), datetime.datetime.combine(d, sp[1])
    if not is_trading_day(d, cfg):
        return None
    return datetime.datetime.combine(d, OPEN), datetime.datetime.combine(d, CLOSE)


def next_session(after, cfg=None, limit=30):
    d = after.date()
    s = session(d, cfg)
    if s and after < s[1]:
        return s
    for _ in range(limit):
        d += datetime.timedelta(days=1)
        s = session(d, cfg)
        if s:
            return s
    return None


def previous_trading_day(d, cfg=None, limit=30):
    for _ in range(limit):
        d -= datetime.timedelta(days=1)
        if is_trading_day(d, cfg):
            return d
    return None


def market_status(now, cfg=None):
    """A dict: state (pre-open / open / closed), today's session, the next one."""
    d = now.date()
    s = session(d, cfg)
    out = {"now": iso_ist(now), "date": d.isoformat(), "holiday": holiday_name(d, cfg),
           "calendar_known": calendar_known(d)}
    if s and s[0] <= now < s[1]:
        out.update(state="open", opens=iso_ist(s[0]), closes=iso_ist(s[1]),
                   minutes_to_close=int((s[1] - now).total_seconds() // 60))
    elif s and s[0].time() == OPEN and datetime.datetime.combine(d, PRE_OPEN) <= now < s[0]:
        out.update(state="pre-open", opens=iso_ist(s[0]), closes=iso_ist(s[1]))
    else:
        out["state"] = "closed"
    nxt = next_session(now if out["state"] != "open" else s[1], cfg)
    if nxt and out["state"] != "open":
        out["next_open"] = iso_ist(nxt[0])
    if d.weekday() >= 5 and not s:
        out["why"] = "weekend"
    elif out["holiday"] and not s:
        out["why"] = out["holiday"]
    if not calendar_known(d):
        out["warning"] = ("NSE's %d holiday list is not in this pack yet; weekdays are treated as "
                          "trading days. Add closures to trader/config.json extra_holidays." % d.year)
    return out


def flat_by(cfg=None, d=None):
    """The time to be flat: config flat_by, never later than Dhan's square-off."""
    t = hhmm((cfg or {}).get("flat_by") or DEFAULT_FLAT_BY)
    if d is not None:
        sp = special_session(d, cfg)
        if sp:
            close = (datetime.datetime.combine(d, sp[1]) - datetime.timedelta(minutes=5)).time()
            return t if sp[0] < t <= close else close
    return min(t, DHAN_SQUARE_OFF)


# ---------------------------------------------------------------- ticks

def band_tick(price):
    for upper, tick in TICK_BANDS:
        if upper is None or price < upper:
            return tick
    return TICK_BANDS[-1][1]


def tick_for(price, symbol=None, scrip=None):
    """The exchange tick from Dhan's instrument file when known, else the band."""
    if symbol and scrip:
        row = scrip.get(symbol.upper())
        if row and row.get("tick"):
            return row["tick"]
    return band_tick(price)


def _ticks(price, tick):
    # snap to the paisa first: Yahoo sends 1156.199951 for 1156.20
    return round(price, 2) / tick


def round_down(price, tick):
    n = math.floor(_ticks(price, tick) + 1e-9)
    return round(n * tick, 2)


def round_up(price, tick):
    n = math.ceil(_ticks(price, tick) - 1e-9)
    return round(n * tick, 2)


def on_tick(price, tick):
    return abs(round(_ticks(price, tick)) - _ticks(price, tick)) < 1e-6


# ---------------------------------------------------------------- charges

def charges(buy_value, sell_value, exchange="NSE", orders=2, rates=None):
    """Intraday equity charges in rupees for one round trip (buy then sell).
    orders = executed orders (each pays brokerage)."""
    r = dict(CHARGES)
    r.update(rates or {})
    turnover = buy_value + sell_value
    per_order = [buy_value, sell_value] if orders == 2 else [turnover / max(orders, 1)] * orders
    brokerage = sum(min(r["brokerage_cap"], v * r["brokerage_pct"] / 100.0) for v in per_order)
    stt = round(sell_value * r["stt_sell_pct"] / 100.0)
    exch = turnover * r["exchange_pct"][exchange.upper()] / 100.0
    sebi = turnover * r["sebi_pct"] / 100.0
    stamp = round(buy_value * r["stamp_buy_pct"] / 100.0)
    ipft = turnover * r["ipft_pct"] / 100.0
    gst = (brokerage + exch + sebi + ipft) * r["gst_pct"] / 100.0
    parts = {"brokerage": brokerage, "stt": float(stt), "exchange": exch, "sebi": sebi,
             "stamp": float(stamp), "ipft": ipft, "gst": gst}
    parts = dict((k, round(v, 2)) for k, v in parts.items())
    parts["total"] = round(sum(parts.values()), 2)
    return parts


def round_trip_cost(qty, entry, exit_price, exchange="NSE", rates=None):
    return charges(qty * entry, qty * exit_price, exchange, rates=rates)["total"]


# Equity / index OPTIONS (NSE F&O), intraday or overnight: the same schedule.
# Sources, checked 2026-10-29 by the options builder (online): angelone.in/exchange-transaction-charges
# (brokerage Rs 20 an executed order, STT 0.15 % sell, NSE options txn 0.0355299 %, SEBI Rs 10/crore,
# stamp 0.003 % buy, IPFT 0.0000001 %, GST 18 %), zerodha.com/charges (NSE options 0.03553 % on
# premium, STT 0.15 % on the sell premium), dhan.co/pricing (Rs 20 per executed F&O order).
# STT on the option premium sold went from 0.1 % to 0.15 % on 2026-04-01 (Union Budget 2026,
# exchange circulars of 2026-03-31); option_charges(on=date) uses 0.1 % before that day.
# Audit note (2026-10-29, offline, NOT re-verified): NSE's own circular rate for options
# transaction charges from 2024-10-01 is 0.03503 % of premium; 0.03553 % above is the brokers'
# published figure as read by the builder. The two STT/txn figures could not be checked offline;
# both err on the conservative side by a rupee or less a trade, so they are kept as cited.
# Re-check against NSE's latest circular (nseindia.com, "transaction charges") before relying
# on per-trade rupee results.
# All charges are on the PREMIUM value (price x quantity), never the notional.
OPTION_CHARGES = {
    "brokerage_per_order": 20.0,
    "stt_sell_pct": 0.15, "stt_sell_pct_before": 0.1, "stt_change": "2026-04-01",
    "exchange_pct": {"NSE": 0.03553, "BSE": 0.0325},
    "sebi_pct": 0.0001, "stamp_buy_pct": 0.003, "ipft_pct": 0.0000001,
    "gst_pct": 18.0,
    "checked": "2026-10-29 (broker pricing pages); NSE circular 0.03503 % not re-verified offline",
}


def option_charges(buy_premium_value, sell_premium_value, exchange="NSE", orders=2, rates=None, on=None):
    """Charges in rupees for one bought-then-sold option round trip, on premium values.
    on = the trade date (date or 'YYYY-MM-DD'); before 2026-04-01 STT is 0.1 %, from then 0.15 %.
    orders = executed orders (each pays the flat brokerage). A zero sell value (expired
    worthless) still pays the buy side's charges."""
    r = dict(OPTION_CHARGES)
    r.update(rates or {})
    stt_pct = r["stt_sell_pct"]
    if on is not None and str(on)[:10] < r["stt_change"]:
        stt_pct = r["stt_sell_pct_before"]
    turnover = buy_premium_value + sell_premium_value
    brokerage = r["brokerage_per_order"] * orders
    stt = round(sell_premium_value * stt_pct / 100.0)
    exch = turnover * r["exchange_pct"][exchange.upper()] / 100.0
    sebi = turnover * r["sebi_pct"] / 100.0
    stamp = round(buy_premium_value * r["stamp_buy_pct"] / 100.0)
    ipft = turnover * r["ipft_pct"] / 100.0
    gst = (brokerage + exch + sebi + ipft) * r["gst_pct"] / 100.0
    parts = {"brokerage": brokerage, "stt": float(stt), "exchange": exch, "sebi": sebi,
             "stamp": float(stamp), "ipft": ipft, "gst": gst}
    parts = dict((k, round(v, 2)) for k, v in parts.items())
    parts["total"] = round(sum(parts.values()), 2)
    return parts


# Equity DELIVERY (CNC: bought one day, sold on a later day), per broker. Checked 2026-10-29:
#   dhan.co/pricing (Equity Delivery): brokerage Rs 0; STT 0.1 % on buy and sell; exchange
#     transaction NSE 0.0030699 %, BSE 0.00375 %; SEBI turnover fee 0.0001 % (Rs 10/crore); stamp
#     duty 0.015 % on buy orders; IPFT 0.0000001 %; GST 18 % on brokerage + transaction + SEBI +
#     IPFT; DP charges "Rs 12.50 / instruction / ISIN + GST" (a debit from the demat account when
#     shares are sold). "Stamp Duty and STT are rounded off to the nearest rupee."
#   angelone.in/exchange-transaction-charges (stock investments): brokerage Rs 0 for the first 30
#     days (up to Rs 500), then "lower of Rs 20 or 0.1 % per executed order, minimum Rs 5"; STT 0.1 %
#     buy and sell; NSE txn 0.0030699 %; SEBI Rs 10/crore; stamp 0.015 % buy; GST 18 %; DP charges
#     Rs 20 + GST per ISIN debit (sell side only; Rs 3.5 CDSL + Rs 16.5 Angel One).
# The DP charge is per scrip (ISIN) per day it is sold from the demat, not per order.
DELIVERY_CHARGES = {
    "stt_pct": 0.1, "exchange_pct": {"NSE": 0.0030699, "BSE": 0.00375},
    "sebi_pct": 0.0001, "stamp_buy_pct": 0.015, "ipft_pct": 0.0000001, "gst_pct": 18.0,
    "brokers": {
        "dhan": {"brokerage_pct": 0.0, "brokerage_cap": 0.0, "brokerage_min": 0.0, "dp_per_scrip": 12.5},
        "angel": {"brokerage_pct": 0.1, "brokerage_cap": 20.0, "brokerage_min": 5.0, "dp_per_scrip": 20.0},
    },
    "checked": "2026-10-29",
}


def delivery_charges(buy_value, sell_value, exchange="NSE", broker="dhan", dp_debits=None, rates=None):
    """Equity delivery (CNC) charges in rupees, per the broker pricing pages checked 2026-10-29:
    Dhan (dhan.co/pricing): brokerage Rs 0, DP Rs 12.50 + GST per scrip per sell day.
    Angel One (angelone.in/exchange-transaction-charges): brokerage the lower of Rs 20 or 0.1 % per
    executed order (minimum Rs 5) after the first 30 days, DP Rs 20 + GST per scrip per sell day.
    Both: STT 0.1 % on the buy AND the sell value, NSE transaction 0.0030699 % (BSE 0.00375 %),
    SEBI Rs 10/crore (0.0001 %), stamp duty 0.015 % on the buy, IPFT 0.0000001 %, GST 18 % on
    brokerage + transaction + SEBI + IPFT (and on the DP charge). STT and stamp duty round to the
    rupee on each side (a delivery buy and its sell sit on different contract notes).
    buy_value / sell_value may be 0 to price one leg. dp_debits = (scrip, sell day) debits
    charged; default 1 when there is a sell, else 0. The caller makes sure a scrip sold twice on
    one day pays it once."""
    r = dict(DELIVERY_CHARGES)
    r.update(rates or {})
    b = dict(r["brokers"][broker.lower()])
    b.update((rates or {}).get("broker_rates") or {})
    if dp_debits is None:
        dp_debits = 1 if sell_value > 0 else 0

    def brok(v):
        if v <= 0 or b["brokerage_pct"] <= 0:
            return 0.0
        x = v * b["brokerage_pct"] / 100.0
        if b["brokerage_cap"]:
            x = min(b["brokerage_cap"], x)
        return max(b["brokerage_min"], x)

    turnover = buy_value + sell_value
    brokerage = brok(buy_value) + brok(sell_value)
    stt = round(buy_value * r["stt_pct"] / 100.0) + round(sell_value * r["stt_pct"] / 100.0)
    exch = turnover * r["exchange_pct"][exchange.upper()] / 100.0
    sebi = turnover * r["sebi_pct"] / 100.0
    stamp = round(buy_value * r["stamp_buy_pct"] / 100.0)
    ipft = turnover * r["ipft_pct"] / 100.0
    dp = dp_debits * b["dp_per_scrip"]
    gst = (brokerage + exch + sebi + ipft + dp) * r["gst_pct"] / 100.0
    parts = {"brokerage": brokerage, "stt": float(stt), "exchange": exch, "sebi": sebi,
             "stamp": float(stamp), "ipft": ipft, "dp": dp, "gst": gst}
    parts = dict((k, round(v, 2)) for k, v in parts.items())
    parts["total"] = round(sum(parts.values()), 2)
    return parts


# ---------------------------------------------------------------- money

def inr(x, decimals=2):
    """Indian digit grouping: 1234567.5 -> 'Rs 12,34,567.50'."""
    neg = x < 0
    s = ("%." + str(decimals) + "f") % abs(x)
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    out = "Rs " + whole + ("." + frac if frac else "")
    return "-" + out if neg else out


# ---------------------------------------------------------------- symbols

def clean_symbol(text):
    """'reliance', 'RELIANCE.NS', 'NSE:RELIANCE' -> 'RELIANCE'. Keeps & and -."""
    s = (text or "").strip().upper()
    for pre in ("NSE:", "BSE:", "NSE_EQ:", "BSE_EQ:"):
        if s.startswith(pre):
            s = s[len(pre):]
    for suf in (".NS", ".BO", "-EQ"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    if not s or not all(c.isalnum() or c in "&-_" for c in s) or len(s) > 20:
        raise NSEError("not an NSE symbol: %r" % text)
    return s


def yahoo_symbol(symbol, exchange="NSE"):
    return clean_symbol(symbol) + (".BO" if exchange.upper() == "BSE" else ".NS")


def parse_equity_list(text):
    """NSE's EQUITY_L.csv -> {symbol: {name, series, isin, listed, lot}}."""
    out = {}
    reader = csv.reader(io.StringIO(text))
    header = [h.strip().upper() for h in next(reader)]
    idx = dict((h, i) for i, h in enumerate(header))
    for row in reader:
        if len(row) < len(header):
            continue
        sym = row[idx["SYMBOL"]].strip()
        out[sym] = {"name": row[idx["NAME OF COMPANY"]].strip(), "series": row[idx["SERIES"]].strip(),
                    "isin": row[idx["ISIN NUMBER"]].strip(), "listed": row[idx["DATE OF LISTING"]].strip(),
                    "lot": int(float(row[idx["MARKET LOT"]] or 1))}
    return out


def parse_index_list(text):
    """NSE index constituents CSV (e.g. ind_nifty500list.csv) -> [{symbol, name, industry, isin}]."""
    rows = list(csv.DictReader(io.StringIO(text)))
    return [{"symbol": r["Symbol"].strip(), "name": r["Company Name"].strip(),
             "industry": r.get("Industry", "").strip(), "isin": r.get("ISIN Code", "").strip()} for r in rows]


def parse_dhan_scrip(text, exchange="NSE"):
    """Dhan's api-scrip-master.csv -> {symbol: {security_id, series, tick, lot, name, type}}
    for one exchange's cash-equity rows. Tick is in rupees (the file has paise)."""
    out = {}
    for r in csv.DictReader(io.StringIO(text)):
        if r.get("SEM_EXM_EXCH_ID") != exchange or r.get("SEM_SEGMENT") != "E":
            continue
        if r.get("SEM_INSTRUMENT_NAME") != "EQUITY":
            continue
        try:
            tick = round(float(r.get("SEM_TICK_SIZE") or 0) / 100.0, 4)
        except ValueError:
            tick = None
        out[r["SEM_TRADING_SYMBOL"]] = {
            "security_id": r["SEM_SMST_SECURITY_ID"], "series": r.get("SEM_SERIES", ""),
            "tick": tick or None, "lot": int(float(r.get("SEM_LOT_UNITS") or 1)),
            "name": r.get("SM_SYMBOL_NAME", ""), "type": r.get("SEM_EXCH_INSTRUMENT_TYPE", "")}
    return out


def intraday_allowed(series):
    """(ok, why) for an NSE series."""
    if series in INTRADAY_SERIES:
        return True, ""
    if series in T2T_SERIES:
        return False, "series %s is trade-to-trade: every trade must be delivered, no intraday" % series
    return False, "series %s is not a regular rolling equity series (EQ)" % (series or "?")


# ---------------------------------------------------------------- trader/ files

def trader_dir(base=None):
    return os.path.abspath(base or os.environ.get("TRADER_DIR") or "trader")


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except ValueError as e:
        raise NSEError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def load_config(path=None):
    return read_json(path or os.path.join(trader_dir(), "config.json"), {}) or {}


# ---------------------------------------------------------------- http

def http_get(url, headers=None, timeout=25, data=None, method=None, retries=1):
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip"}
    h.update(headers or {})
    body = None if data is None else (data if isinstance(data, bytes) else json.dumps(data).encode())
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=body, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                attempt += 1
                time.sleep(BACKOFF)
                continue
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise NSEError(("HTTP %d from %s %s" % (e.code, urllib.parse.urlsplit(url).netloc, detail)).strip())
        except (urllib.error.URLError, OSError) as e:
            raise NSEError("could not reach %s: %s" % (urllib.parse.urlsplit(url).netloc, e))


def read_maybe_gz(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw


# ---------------------------------------------------------------- bars
# A bar is a dict: {"t": naive IST datetime of the bar's START, "o","h","l","c": float, "v": int}.

def yahoo_chart_url(symbol, rng="1d", interval="5m", host=YAHOO_HOSTS[0], exchange="NSE"):
    return "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=%s" % (
        host, urllib.parse.quote(yahoo_symbol(symbol, exchange)), rng, interval)


def parse_yahoo_chart(data):
    """Yahoo chart JSON -> (meta, bars). Drops bars with no prices."""
    doc = json.loads(data) if isinstance(data, (bytes, str)) else data
    chart = doc.get("chart") or {}
    if chart.get("error"):
        raise NSEError("Yahoo: %s" % (chart["error"].get("description") or chart["error"]))
    res = (chart.get("result") or [None])[0]
    if not res:
        raise NSEError("Yahoo sent no chart")
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    bars = []
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c = (q.get(k, [None])[i] if i < len(q.get(k, [])) else None for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c):
            continue
        v = (q.get("volume") or [0] * (i + 1))[i] or 0
        bars.append({"t": ist_from_epoch(ts), "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": int(v)})
    return res.get("meta") or {}, bars


def yahoo_bars(symbol, rng="1d", interval="5m", exchange="NSE"):
    last = None
    for host in YAHOO_HOSTS:
        try:
            return parse_yahoo_chart(http_get(yahoo_chart_url(symbol, rng, interval, host, exchange)))
        except NSEError as e:
            last = e
            time.sleep(PAUSE)
    raise last


def on_grid(bars, minutes):
    """Keep only bars whose start sits on the interval grid from 09:15 (drops Yahoo's live tick)."""
    out = []
    for b in bars:
        mins = b["t"].hour * 60 + b["t"].minute - (OPEN.hour * 60 + OPEN.minute)
        if b["t"].second == 0 and mins % minutes == 0:
            out.append(b)
    return out


def closed_bars(bars, minutes, now):
    step = datetime.timedelta(minutes=minutes)
    return [b for b in bars if b["t"] + step <= now]


def by_day(bars):
    days = {}
    for b in bars:
        days.setdefault(b["t"].date(), []).append(b)
    return days


def resample(bars, minutes):
    """Merge 1-minute (or finer) bars into N-minute bars anchored at 09:15."""
    out, cur = [], None
    for b in bars:
        mins = b["t"].hour * 60 + b["t"].minute - (OPEN.hour * 60 + OPEN.minute)
        start = datetime.datetime.combine(b["t"].date(), OPEN) + datetime.timedelta(minutes=(mins // minutes) * minutes)
        if cur and cur["t"] == start:
            cur["h"] = max(cur["h"], b["h"])
            cur["l"] = min(cur["l"], b["l"])
            cur["c"] = b["c"]
            cur["v"] += b["v"]
        else:
            cur = {"t": start, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]}
            out.append(cur)
    return out


def bars_to_json(bars):
    return [dict(b, t=b["t"].isoformat()) for b in bars]


def bars_from_json(rows):
    return [dict(r, t=datetime.datetime.fromisoformat(r["t"])) for r in rows]


# ---------------------------------------------------------------- .env

def env_paths():
    """Where a .env is looked for: the current folder, then the folder holding trader/."""
    out = [os.path.abspath(".env"), os.path.join(os.path.dirname(trader_dir()), ".env")]
    return list(dict.fromkeys(out))


def parse_env(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        out[key.strip()] = val
    return out


def load_env(paths=None, env=None):
    """Read KEY=value lines from .env into the environment. A variable already set
    in the shell wins. Keys never get printed."""
    env = os.environ if env is None else env
    for path in paths or env_paths():
        try:
            with open(path, encoding="utf-8") as fh:
                pairs = parse_env(fh.read())
        except OSError:
            continue
        for k, v in pairs.items():
            if v and not env.get(k):
                env[k] = v


# ---------------------------------------------------------------- Dhan

def dhan_credentials(env=None):
    if env is None:
        load_env()
    env = os.environ if env is None else env
    cid, tok = env.get("DHAN_CLIENT_ID"), env.get("DHAN_ACCESS_TOKEN")
    return (cid, tok) if cid and tok else None


class Dhan(object):
    """The Dhan v2 REST API. Keys come from the environment (DHAN_CLIENT_ID,
    DHAN_ACCESS_TOKEN), never from chat or files. Data endpoints need Dhan's
    paid Data API plan; order endpoints need a whitelisted static IP."""

    def __init__(self, client_id, token, base=None, transport=None):
        self.client_id, self.token = str(client_id), token
        self.base = (base or os.environ.get("DHAN_BASE_URL") or DHAN_BASE).rstrip("/")
        self.transport = transport or http_get
        self.last_call = 0.0

    @classmethod
    def from_env(cls, **kw):
        cred = dhan_credentials()
        if not cred:
            raise NSEError("set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in your shell (never paste the token in chat)")
        return cls(cred[0], cred[1], **kw)

    def headers(self):
        return {"access-token": self.token, "client-id": self.client_id,
                "Content-Type": "application/json", "Accept": "application/json"}

    def call(self, method, path, body=None):
        wait = 1.0 - (time.time() - self.last_call)     # quote APIs allow one request a second
        if wait > 0 and self.transport is http_get:
            time.sleep(wait)
        self.last_call = time.time()
        raw = self.transport(self.base + path, headers=self.headers(), data=body, method=method)
        if not raw:
            return {}
        doc = json.loads(raw)
        if isinstance(doc, dict) and doc.get("errorCode"):
            raise NSEError("Dhan %s: %s" % (doc.get("errorCode"), doc.get("errorMessage") or doc.get("errorType")))
        return doc

    # data
    def intraday(self, security_id, start, end, interval=5, segment="NSE_EQ"):
        doc = self.call("POST", "/charts/intraday", {
            "securityId": str(security_id), "exchangeSegment": segment, "instrument": "EQUITY",
            "interval": str(interval), "oi": False,
            "fromDate": start.strftime("%Y-%m-%d %H:%M:%S"), "toDate": end.strftime("%Y-%m-%d %H:%M:%S")})
        return dhan_bars(doc)

    def daily(self, security_id, start, end, segment="NSE_EQ"):
        doc = self.call("POST", "/charts/historical", {
            "securityId": str(security_id), "exchangeSegment": segment, "instrument": "EQUITY",
            "oi": False, "fromDate": start.isoformat(), "toDate": end.isoformat()})
        return dhan_bars(doc)

    def ltp(self, ids, segment="NSE_EQ"):
        return self._quote("/marketfeed/ltp", ids, segment)

    def ohlc(self, ids, segment="NSE_EQ"):
        return self._quote("/marketfeed/ohlc", ids, segment)

    def quote(self, ids, segment="NSE_EQ"):
        return self._quote("/marketfeed/quote", ids, segment)

    def _quote(self, path, ids, segment):
        out = {}
        ids = [int(i) for i in ids]
        for i in range(0, len(ids), 1000):
            doc = self.call("POST", path, {segment: ids[i:i + 1000]})
            out.update(((doc.get("data") or {}).get(segment) or {}))
        return out

    def profile(self):
        return self.call("GET", "/profile")


def dhan_bars(doc):
    """Dhan's parallel arrays -> bars. Dhan's docs do not name the epoch's zone:
    standard UTC epoch is assumed, and if the bars land outside market hours
    but sit inside them read as IST wall-clock epoch, that reading is used."""
    ts = doc.get("timestamp") or []
    shift = datetime.timedelta(0)
    intraday = len(ts) > 1 and (ts[1] - ts[0]) < 86400
    if intraday:
        def inside(dt):
            return PRE_OPEN <= dt.time() <= datetime.time(16, 0)
        sample = [ist_from_epoch(t) for t in ts]
        if not all(inside(d) for d in sample) and all(inside(d - IST_OFFSET) for d in sample):
            shift = -IST_OFFSET
    bars = []
    for i, t in enumerate(ts):
        bars.append({"t": ist_from_epoch(t) + shift, "o": float(doc["open"][i]), "h": float(doc["high"][i]),
                     "l": float(doc["low"][i]), "c": float(doc["close"][i]), "v": int(doc["volume"][i] or 0)})
    return bars


# ---------------------------------------------------------------- caches

def cached(path, url, max_age_hours=20, offline=None):
    """Bytes of url, cached at path for max_age_hours. offline=PATH reads a file instead."""
    if offline:
        return read_maybe_gz(offline)
    try:
        if time.time() - os.path.getmtime(path) < max_age_hours * 3600:
            with open(path, "rb") as fh:
                return fh.read()
    except OSError:
        pass
    raw = http_get(url, timeout=120)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(raw)
    return raw


def equity_list(base=None, offline=None):
    path = os.path.join(trader_dir(base), "cache", "EQUITY_L.csv")
    return parse_equity_list(cached(path, NSE_EQUITY_LIST, offline=offline).decode("utf-8", "replace"))


def scrip_master(base=None, offline=None, exchange="NSE"):
    """Dhan's instrument file, slimmed to cash equities and cached as JSON."""
    slim = os.path.join(trader_dir(base), "cache", "dhan-%s-equity.json" % exchange.lower())
    if not offline:
        try:
            if time.time() - os.path.getmtime(slim) < 20 * 3600:
                return read_json(slim)
        except OSError:
            pass
    raw = offline and read_maybe_gz(offline) or http_get(DHAN_SCRIP_URL, timeout=180)
    table = parse_dhan_scrip(raw.decode("utf-8", "replace"), exchange)
    if not offline:
        write_json(slim, table)
    return table


def security_id(symbol, scrip):
    row = scrip.get(clean_symbol(symbol))
    if not row:
        raise NSEError("%s is not in Dhan's NSE equity list" % symbol)
    return row["security_id"]


# ---------------------------------------------------------------- CM bhavcopy (every stock, every day)
# NSE's capital-market end-of-day file, a plain archive download (no cookies). Two formats:
#   legacy  .../content/historical/EQUITIES/2019/JUN/cm03JUN2019bhav.csv.zip   (last day 2024-07-05)
#   UDiFF   .../content/cm/BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv.zip     (official from 2024-07-08;
#           NSE also back-filled 2024-01-01 .. 2024-07-05; earlier dates 404)
# A non-trading day is a 404 in both. Prices are as traded (NOT adjusted for splits or bonuses);
# PREVCLOSE on an ex-date is NSE's ADJUSTED previous close, which is what reveals the adjustment.

CM_BHAV_UDIFF_FROM = datetime.date(2024, 7, 8)
CM_BHAV_LEGACY_URL = "https://nsearchives.nseindia.com/content/historical/EQUITIES/%Y/{MON}/cm%d{MON}%Ybhav.csv.zip"
CM_BHAV_UDIFF_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_%Y%m%d_F_0000.csv.zip"
CM_BHAV_FIELDS = ("symbol", "series", "open", "high", "low", "close", "prev_close", "volume", "value", "isin")


def cm_bhav_url(d, fmt=None):
    """The bhavcopy URL for date d; fmt 'legacy' | 'udiff' (default: by date)."""
    fmt = fmt or ("udiff" if d >= CM_BHAV_UDIFF_FROM else "legacy")
    if fmt == "udiff":
        return d.strftime(CM_BHAV_UDIFF_URL)
    mon = d.strftime("%b").upper()
    return d.strftime(CM_BHAV_LEGACY_URL.replace("{MON}", mon))


def unzip_first(raw):
    """The first file inside a zip archive's bytes (a plain CSV passes through)."""
    import zipfile
    if raw[:2] != b"PK":
        return raw
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        return z.read(z.namelist()[0])


def _num(x):
    x = (x or "").strip()
    if not x or x == "-":
        return None
    try:
        return float(x)
    except ValueError:
        return None


def parse_cm_bhav(data, series=None):
    """Either bhavcopy format (zip bytes, CSV bytes or text) -> (date ISO, [row dicts with CM_BHAV_FIELDS]).
    series: an iterable of series to keep (default every one)."""
    if isinstance(data, bytes):
        data = unzip_first(data).decode("utf-8", "replace")
    rd = csv.DictReader(io.StringIO(data.lstrip("﻿")))
    keep = set(series) if series else None
    rows, day = [], None
    for r in rd:
        r = dict(((k or "").strip(), (v or "").strip()) for k, v in r.items() if k)
        if "TckrSymb" in r:                                   # UDiFF
            if r.get("Sgmt", "CM") != "CM":
                continue
            row = {"symbol": r["TckrSymb"], "series": r.get("SctySrs", ""), "open": _num(r.get("OpnPric")),
                   "high": _num(r.get("HghPric")), "low": _num(r.get("LwPric")), "close": _num(r.get("ClsPric")),
                   "prev_close": _num(r.get("PrvsClsgPric")), "volume": _num(r.get("TtlTradgVol")),
                   "value": _num(r.get("TtlTrfVal")), "isin": r.get("ISIN", "")}
            day = day or r.get("TradDt")
        elif "SYMBOL" in r:                                   # legacy
            row = {"symbol": r["SYMBOL"], "series": r.get("SERIES", ""), "open": _num(r.get("OPEN")),
                   "high": _num(r.get("HIGH")), "low": _num(r.get("LOW")), "close": _num(r.get("CLOSE")),
                   "prev_close": _num(r.get("PREVCLOSE")), "volume": _num(r.get("TOTTRDQTY")),
                   "value": _num(r.get("TOTTRDVAL")), "isin": r.get("ISIN", "")}
            if not day and r.get("TIMESTAMP"):
                ts = r["TIMESTAMP"].title()                   # 02-JAN-2015; one day in 2020 says 13-Jul-20
                day = datetime.datetime.strptime(ts, "%d-%b-%Y" if len(ts) > 9 else "%d-%b-%y").date().isoformat()
        else:
            raise NSEError("not an NSE CM bhavcopy (columns %s)" % ", ".join(list(r)[:6]))
        if keep is not None and row["series"] not in keep:
            continue
        if row["close"] is None:
            continue
        rows.append(row)
    return day, rows
