import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import COMMON, FIX, load

import nse

news = load("india-news", "news")
NDIR = os.path.join(FIX, "india-news")
OFFLINE = COMMON + "," + NDIR
NOW = "2026-10-29 15:40"          # after the close, Thu Oct 29 2026 (IST)
DT = datetime.datetime


def raw(name, folder=COMMON):
    return nse.read_maybe_gz(os.path.join(folder, name))


def equity():
    return nse.parse_equity_list(raw("EQUITY_L.csv").decode("utf-8"))


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


class Times(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(news.parse_time("29-Oct-2026 15:29:10"), DT(2026, 10, 29, 15, 29, 10))   # NSE: IST
        self.assertEqual(news.parse_time("Thu, 29 Oct 2026 09:54:00 +0000"), DT(2026, 10, 29, 15, 24))
        self.assertEqual(news.parse_time("Thu, 29 Oct 2026 15:29:48 +0530"), DT(2026, 10, 29, 15, 29, 48))
        self.assertEqual(news.parse_time("2026-10-29T04:00:00Z"), DT(2026, 10, 29, 9, 30))
        self.assertIsNone(news.parse_time("soon"))
        self.assertEqual(news.parse_date("03-Nov-2026"), datetime.date(2026, 11, 3))


class Symbols(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = news.Resolver(equity())

    def test_file_prefix(self):
        link = "https://nsearchives.nseindia.com/corporate/GKENERGY_29102026152857_Incorporation_of_WOS_sd.pdf"
        self.assertEqual(self.r.symbol("", link), "GKENERGY")
        self.assertEqual(self.r.symbol("GK Energy Limited", link), "GKENERGY")

    def test_name_when_the_file_name_is_not_a_symbol(self):
        # PNGJEWELLERS is not a symbol (PNGJL is); Reliance's filing is filed by its secretary's name
        self.assertEqual(self.r.symbol("P N Gadgil Jewellers Limited",
                                       ".../corporate/PNGJEWELLERS_29102026152731_Outcomeofboardmeeting.pdf"),
                         "PNGJL")
        self.assertEqual(self.r.symbol("Reliance Industries Limited",
                                       ".../corporate/kavinavora_29102026084603_SE_29102026.pdf"), "RELIANCE")
        self.assertEqual(self.r.symbol("Kamat Hotels (I) Limited",
                                       ".../xbrl/PRIOR_INTIMATION_878_WebXMLFile_20261029_150419243.xml"),
                         "KAMATHOTEL")

    def test_name_wins_a_disagreement(self):
        # a real item: JSW Steel's newspaper ad, filed under SUPRIYA_..., which is another listed symbol
        self.assertIn("SUPRIYA", self.r.equity)
        self.assertEqual(self.r.symbol("JSW Steel Limited",
                                       ".../corporate/Supriya_29102026152053_SE_intimation.pdf"), "JSWSTEEL")

    def test_unknown(self):
        self.assertIsNone(self.r.symbol("Aditya Birla Sun Life Nifty 50 ETF", ""))
        self.assertEqual(news.norm_name("The Ramco Cements Ltd."), "ramco cements")
        self.assertEqual(news.norm_name("Mahindra & Mahindra Limited"), "mahindra and mahindra")


class Feeds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = news.Resolver(equity())

    def test_announcements(self):
        watch = {"INFY": None, "RELIANCE": None, "JSWSTEEL": None, "BHARTIARTL": None}
        items = news.parse_nse_feed(raw("Online_announcements.xml"), "announcement", self.r, watch)
        by = {}
        for it in items:
            by.setdefault(it["ticker"], []).append(it)
        self.assertEqual(dict((k, len(v)) for k, v in by.items()),
                         {"INFY": 2, "RELIANCE": 1, "JSWSTEEL": 1, "BHARTIARTL": 3})
        infy = by["INFY"][1]
        self.assertEqual(infy["time"], DT(2026, 10, 29, 15, 5, 32))
        self.assertTrue(infy["title"].startswith("[Updates] Infosys Limited has informed the Exchange regarding"))
        self.assertTrue(infy["id"].startswith("nse:announcement:"))
        everything = news.parse_nse_feed(raw("Online_announcements.xml"), "announcement", self.r)
        self.assertGreater(len(everything), 400)            # most items map to a symbol by name or file

    def test_board_meetings(self):
        items = news.parse_nse_feed(raw("Board_Meetings.xml"), "board", self.r, {"KAMATHOTEL": None})
        self.assertEqual([i["meeting_date"] for i in items], ["03-Nov-2026", "03-Nov-2026"])
        self.assertEqual(items[0]["title"], "Board meeting 03-Nov-2026: Financial Results")
        ev = news.upcoming(items, {"KAMATHOTEL": None}, datetime.date(2026, 10, 29))
        self.assertEqual(ev, [{"ticker": "KAMATHOTEL", "what": "results", "date": "2026-11-03", "days": 5,
                               "purpose": "Financial Results", "link": items[0]["link"]}])
        self.assertEqual(news.upcoming(items, {"KAMATHOTEL": None}, datetime.date(2026, 10, 20)), [])  # 14 days

    def test_corporate_actions(self):
        items = news.tag(news.parse_nse_feed(raw("Corporate_action.xml"), "action", self.r,
                                             {"SUPREMEIND": None, "KAJARIACER": None}), {"SUPREMEIND": None})
        sup = [i for i in items if i["ticker"] == "SUPREMEIND"][0]
        self.assertEqual(sup["title"], "Ex-date 30-Oct-2026: INTERIM DIVIDEND - RS 12 PER SHARE "
                                       "(record date 30-Oct-2026)")
        self.assertEqual(sup["tags"], ["dividend"])
        ev = news.upcoming(items, {"SUPREMEIND": None, "KAJARIACER": None}, datetime.date(2026, 10, 29))
        self.assertEqual([(e["ticker"], e["what"], e["days"]) for e in ev],
                         [("KAJARIACER", "ex-date", 0), ("SUPREMEIND", "ex-date", 1)])

    def test_financial_results(self):
        items = news.tag(news.parse_nse_feed(raw("Financial_Results.xml"), "results", self.r), {})
        self.assertEqual(len(items), 4)
        self.assertEqual(items[0]["ticker"], "AHLWEST")
        self.assertEqual(items[0]["title"], "Results filed: Annual, Audited, Consolidated, period ended 31-Mar-2021")
        self.assertIn("results", items[0]["tags"])
        self.assertTrue(items[0]["important"])

    def test_yahoo_rss(self):
        items = news.parse_yahoo_rss(raw("yahoo-INFY.xml", NDIR), "INFY")
        self.assertEqual(len(items), 14)
        self.assertEqual(items[0]["title"], "Winners of Qorus-Infosys Finacle Banking Innovation Awards 2026 Announced")
        self.assertEqual(items[0]["time"], DT(2026, 10, 29, 15, 24))       # 09:54 UTC
        self.assertEqual(news.parse_yahoo_rss(raw("yahoo-RELIANCE.xml", NDIR), "RELIANCE"), [])  # real empty feed

    def test_bad_xml(self):
        with self.assertRaises(news.NewsError):
            news.parse_yahoo_rss(b"<html>busy", "TCS")


class Tagging(unittest.TestCase):
    def item(self, title, kind="announcement", ticker="X"):
        return {"id": title, "source": "s", "kind": kind, "ticker": ticker, "title": title, "summary": "",
                "link": "", "time": None}

    def test_india_tags(self):
        cases = {
            "Company bags order worth Rs 450 crore from NHAI": "order win",
            "Receipt of Letter of Award from NTPC": "order win",
            "[Bagging/Receiving of orders/contracts] ACME Solar Holdings Limited has informed the Exchange about "
            "execution of Power Purchase Agreement": "order win",                # NSE's subject line, Oct 29
            "Acquisition of 51% stake in XYZ Private Limited": "acquisition",
            "Creation of pledge on promoter shares": "pledge",
            "SEBI show cause notice received": "regulatory",
            "Resignation of Chief Financial Officer (CFO)": "management change",
            "CRISIL reaffirms credit rating at AA+": "rating",
            "Block deal: promoter sells 2% stake": "block/bulk deal",
            "Board approves QIP of Rs 2,000 crore": "fund raising",
            "Record date for 1:1 bonus issue": "bonus",
            "Stock split: sub-division of face value": "split",
            "Buyback of equity shares": "buyback",
            "Outcome of Board Meeting: financial results for Q2 FY27": "results",
            "Disclosure under SAST regulations": "insider",
        }
        for title, want in cases.items():
            self.assertIn(want, news.tag([self.item(title)], {"X": None})[0]["tags"], title)

    def test_real_items(self):
        r = news.Resolver(equity())
        items = news.tag(news.parse_nse_feed(raw("Online_announcements.xml"), "announcement", r,
                                             {"BHARTIARTL": None, "INFY": None}), {"BHARTIARTL": None, "INFY": None})
        orders = [i for i in items if "orders passed" in i["title"]]
        self.assertTrue(all("regulatory" in i["tags"] and i["important"] for i in orders))
        meet = [i for i in items if "Analysts or Institutional" in i["title"]][0]
        self.assertEqual(meet["tags"], ["analyst"])
        # "a wholly-owned subsidiary of Infosys" in a press release is not an acquisition
        y = news.tag(news.parse_yahoo_rss(raw("yahoo-INFY.xml", NDIR), "INFY"), {"INFY": "Infosys Limited"})
        self.assertNotIn("acquisition", y[0]["tags"])
        self.assertFalse(y[0]["important"])                  # headlines are leads, never "!"

    def test_mentions(self):
        y = news.parse_yahoo_rss(raw("yahoo-TCS.xml", NDIR), "TCS")
        items = news.tag(y, {"TCS": "Tata Consultancy Services Limited", "INFY": "Infosys Limited"})
        both = [i for i in items if i["touches"] == ["INFY", "TCS"]]
        self.assertTrue(all(i["ticker"] == "TCS" for i in items))
        for i in both:
            self.assertRegex(i["title"] + i["summary"], "(?i)infosys")

    def test_dedupe(self):
        a = news.parse_yahoo_rss(raw("yahoo-INFY.xml", NDIR), "INFY")
        b = news.parse_yahoo_rss(raw("yahoo-INFY.xml", NDIR), "TCS")
        items = news.dedupe(news.tag(a + b, {"INFY": None, "TCS": None}))
        self.assertEqual(len(items), 14)
        self.assertEqual(items[0]["touches"], ["INFY", "TCS"])


class Check(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.state = os.path.join(self.d, "state", "news.json")
        none = os.path.join(self.d, "none.json")
        self.base = ["--offline", OFFLINE, "--state", self.state, "--now", NOW,
                     "--watchlist", none, "--plan", none, "--paper", none]

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_new_once_newest_first(self):
        code, out, err = run(["--tickers", "INFY,RELIANCE,BHARTIARTL", "--hours", "24", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        times = [i["time"] for i in data["items"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertTrue(all(t.endswith("+05:30") for t in times))
        self.assertEqual(data["checked"], "2026-10-29T15:40:00+05:30")
        kinds = sorted(set((i["ticker"], i["source"]) for i in data["items"]))
        self.assertEqual(kinds, [("BHARTIARTL", "NSE filing"), ("INFY", "NSE filing"), ("INFY", "Yahoo Finance"),
                                 ("RELIANCE", "NSE filing")])
        self.assertEqual(len(data["items"]), 8)    # 3 + 2 + 1 NSE filings, 2 INFY headlines in 24 hours
        moves = dict((m["ticker"], (m["price"], m["prev_close"], m["pct"], m["time"])) for m in data["moves"])
        self.assertEqual(moves["RELIANCE"], (1167.4, 1158.0, 0.81, "2026-10-29T15:29:03+05:30"))
        self.assertEqual(moves["INFY"][2], 2.08)
        code, out, _ = run(["--tickers", "INFY,RELIANCE,BHARTIARTL", "--hours", "24"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", out)
        code, out, _ = run(["--tickers", "INFY,RELIANCE,BHARTIARTL", "--hours", "24", "--all"] + self.base)
        self.assertEqual(code, 1)                    # shown again, but nothing NEW
        self.assertIn("Infosys Limited has informed the Exchange", out)

    def test_text_output(self):
        code, out, err = run(["--tickers", "RELIANCE", "KAMATHOTEL", "SUPREMEIND", "INFY", "--hours", "24",
                              "--move-pct", "2", "--dry-run"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("RELIANCE    Rs 1,167.40 +0.81% vs previous close Rs 1,158.00 at 15:29 IST", out)
        self.assertIn("INFY        Rs 1,146.50 +2.08% vs previous close Rs 1,123.15 at 15:29 IST  MOVING", out)
        self.assertIn("KAMATHOTEL  RESULTS THIS WEEK: board meeting Tue 2026-11-03 (in 5 days): Financial Results", out)
        self.assertIn("SUPREMEIND  ex-date Fri 2026-10-30 (tomorrow): INTERIM DIVIDEND - RS 12 PER SHARE", out)
        self.assertIn("! 2026-10-29 15:11 IST  KAMATHOTEL  NSE board mtg   Board meeting 03-Nov-2026: Financial Results",
                      out)
        self.assertIn("2026-10-29 08:55 IST  RELIANCE    NSE filing", out)
        self.assertIn("Not financial advice", out)
        for bad in (" ET", "SEC", "$", "New York", "NYSE"):
            self.assertNotIn(bad, out)
        self.assertFalse(os.path.exists(self.state))

    def test_pinned_clock_hides_later_items(self):
        # a pinned clock keeps items up to 10 minutes after it (feeds stamp a little late), nothing later
        code, out, _ = run(["--tickers", "INFY", "--hours", "6", "--no-moves", "--dry-run",
                            "--now", "2026-10-29 15:00"] + self.base[:4])
        self.assertIn("15:05 IST  INFY", out)
        self.assertNotIn("15:15 IST", out)
        self.assertNotIn("15:24 IST", out)

    def test_tickers_from_trader_files(self):
        wl = os.path.join(self.d, "watchlist.json")
        plan = os.path.join(self.d, "plan.json")
        paper = os.path.join(self.d, "paper.json")
        with open(wl, "w") as fh:
            json.dump({"created_by": "india-scanner", "hits": [{"symbol": "INFY", "exchange": "NSE"}]}, fh)
        with open(plan, "w") as fh:
            json.dump({"symbol": "RELIANCE", "exchange": "NSE"}, fh)
        with open(paper, "w") as fh:
            json.dump({"positions": [{"symbol": "TCS", "qty": 5}, {"symbol": "SBIN", "qty": 0}]}, fh)
        self.assertEqual(sorted(news.watch_from_files(wl, plan, paper)), ["INFY", "RELIANCE", "TCS"])
        args = list(self.base)
        args[args.index("--watchlist") + 1] = wl
        code, out, err = run(["--json", "--dry-run", "--hours", "24", "--no-moves"] + args)
        self.assertEqual(json.loads(out)["symbols"], ["INFY"])

    def test_no_symbols(self):
        code, _, err = run(self.base)
        self.assertEqual(code, 2)
        self.assertIn("no symbols", err)
        code, _, err = run(["--tickers", "not a symbol!"] + self.base)
        self.assertEqual(code, 2)

    def test_offline_missing_feed_is_a_note(self):
        code, out, _ = run(["--tickers", "INFY", "--offline", NDIR, "--state", self.state, "--now", NOW,
                            "--dry-run", "--hours", "24"])
        self.assertIn("note: NSE Online_announcements.xml: offline: no file", out)
        self.assertIn("note: NSE's EQUITY_L.csv could not be read", out)

    def test_state_forgets_after_30_days(self):
        state = {"seen": {"old": "2026-09-01T00:00:00+05:30", "new": "2026-10-20T00:00:00+05:30"}}
        news.remember([], state, DT(2026, 10, 29, 15, 40))
        self.assertEqual(sorted(state["seen"]), ["new"])
        self.assertEqual(state["last_check"], "2026-10-29T15:40:00+05:30")

    def test_moves(self):
        m = news.parse_chart(raw("chart-TCS.json", NDIR), "TCS")
        self.assertEqual((m["price"], m["prev_close"], m["pct"]), (2478.0, 2460.0, 0.73))
        self.assertNotIn("MOVING", news.render_moves([m])[0])
        with self.assertRaises(news.NewsError):
            news.parse_chart(b'{"chart": {"result": null}}', "ZZZ")


class Docs(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(NDIR)), "..", "india-news", "SKILL.md"),
                  encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: india-news\n"))
        self.assertGreater(len(text.split("---", 2)[2].splitlines()), 60)
        for bad in ("NYSE", "8-K", "EDGAR", "New York", "Alpaca"):
            self.assertNotIn(bad, text)


if __name__ == "__main__":
    unittest.main()
