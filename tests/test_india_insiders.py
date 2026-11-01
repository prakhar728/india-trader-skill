import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, COMMON

ins = load("india-insiders", "insiders")

IDIR = os.path.join(FIX, "india-insiders")
OFF = ["--offline", IDIR, "--offline", COMMON]


def pit(name):
    with open(os.path.join(IDIR, name), "rb") as fh:
        return ins.parse_pit(fh.read(), "https://nsearchives.nseindia.com/corporate/xbrl/" + name)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ins.main(argv)
    return code, out.getvalue(), err.getvalue()


class Pit(unittest.TestCase):
    def test_promoter_open_market_buy_new_schema(self):
        f = pit("IT_1597_WebXMLFile_20261029_093618789.xml")
        self.assertEqual((f["symbol"], f["filed"], f["schema"]), ("SKMEGGPROD", "2026-10-29", "PIT V2.0 (in-bse-co)"))
        r = f["rows"][0]
        self.assertEqual((r["person"], r["category"], r["type"], r["mode"]),
                         ("SKM SHREE SHIVKUMAR", "Promoter and Director", "Buy", "Market Purchase"))
        self.assertEqual((r["qty"], r["value"], r["before_qty"], r["after_qty"]), (10000, 1905094, 23992278, 24002278))
        self.assertEqual((r["before_pct"], r["after_pct"]), (45.56, 45.58))       # filed as fractions
        self.assertEqual((r["from"], r["to"], r["intimated"], r["exchange"]), ("2026-10-27", "2026-10-27", "2026-10-28", "NSE"))
        self.assertEqual(r["tag"], "PROMOTER OPEN-MARKET BUY")
        self.assertTrue(r["promoter"] and r["open_market"])
        self.assertEqual(ins.lag_days(r), 2)
        self.assertIn("BUY 10,000 equity for Rs 19,05,094 (Rs 190.51 each), market purchase on NSE", ins.describe(r))
        self.assertIn("holding 45.56% -> 45.58%; disclosed 2 days after the trade", ins.describe(r))

    def test_noise_pledge_adr_debenture(self):
        f = pit("IT_11353_WebXMLFile_20261029_122855945.xml")
        self.assertEqual(len(f["rows"]), 2)
        self.assertEqual([r["type"] for r in f["rows"]], ["Pledge", "Pledge"])
        self.assertTrue(all(r["tag"] == "noise: pledge" for r in f["rows"]))
        self.assertEqual(f["rows"][0]["before_pct"], 10.3)
        r = pit("IT_180_WebXMLFile_20261029_120656926.xml")["rows"][0]
        self.assertEqual((r["category"], r["type"], r["instrument"]), ("Designated Person", "Sell",
                                                                       "Any other instrument (ADRs)"))
        self.assertTrue(r["tag"].startswith("not shares"))
        r = pit("IT_1257_WebXMLFile_20261029_101919601.xml")["rows"][0]
        self.assertEqual((r["category"], r["instrument"], r["value"]), ("Promoter Group", "Debenture", 3000000000))
        self.assertEqual(r["tag"], "not shares: Debenture")

    def test_employee_trust_market_sale_is_noise(self):
        rows = pit("IT_2266_WebXMLFile_20261029_154056749.xml")["rows"]       # JSW Steel, 2026-10-29
        self.assertEqual(len(rows), 5)
        sale = [r for r in rows if r["mode"] == "Market Sale"]
        self.assertEqual(len(sale), 2)
        self.assertEqual((sale[0]["category"], sale[0]["tag"]), ("Other", "noise: employee trust"))
        self.assertFalse(sale[0]["promoter"])

    def test_older_schema_keeps_percent(self):
        f = pit("IT_1197866_1662688_30042026102612_WEB.xml")
        self.assertEqual((f["symbol"], f["schema"]), ("BAJAJFINSV", "PIT 2018 (in-capmkt)"))
        buy, sell = f["rows"]
        self.assertEqual((buy["person"], buy["category"], buy["before_pct"], buy["after_pct"]),
                         ("Bajaj Holdings & Investment Limited", "Promoters", 38.28, 38.41))
        self.assertEqual(buy["tag"], "PROMOTER OPEN-MARKET BUY")
        self.assertEqual(sell["tag"], "PROMOTER OPEN-MARKET SELL")
        rows = pit("IT_1197862_1662394_30042026063149_WEB.xml")["rows"]
        self.assertEqual(len(rows), 4)
        self.assertEqual([r["tag"] for r in rows], ["noise: employee trust, esop"] + ["noise: esop"] * 3)
        self.assertEqual(rows[0]["category"], "")                               # HCL left it blank
        rows = pit("IT_1197851_1661892_30042026020546_WEB.xml")["rows"]
        self.assertTrue(all(r["tag"] == "noise: employee trust, off market" for r in rows))   # eClerx welfare trust
        with open(os.path.join(COMMON, "it.xml"), "rb") as fh:
            r = ins.parse_pit(fh.read())["rows"][0]
        self.assertEqual((r["symbol"], r["person"], r["mode"], r["tag"]), ("RELTD", "Shantanu Lath", "ESOP", "noise: esop"))
        self.assertEqual(r["after_pct"], 0.07)

    def test_rss(self):
        with open(os.path.join(IDIR, "InsiderTrading.xml"), "rb") as fh:
            items = ins.parse_rss(fh.read())
        self.assertEqual([i["symbol"] for i in items], ["CAMLINFINE", "INFY", "BAJFINANCE", "SKMEGGPROD"])
        self.assertEqual(items[1]["published"], "2026-10-29T12:06:57+05:30")
        with open(os.path.join(IDIR, "Insider_Trading.xml"), "rb") as fh:
            old = ins.parse_rss(fh.read())
        self.assertIsNone(old[0]["symbol"])                                      # the old feed has no symbol
        self.assertEqual(len(old), len(set(i["link"] for i in old)))           # duplicates dropped
        with self.assertRaises(ins.InsiderError):
            ins.parse_pit(b"<rss/>")


class Deals(unittest.TestCase):
    def test_parse_and_follow(self):
        with open(os.path.join(COMMON, "block.csv")) as fh:
            block = ins.parse_deals(fh.read(), "block")
        self.assertEqual(len(block), 2)
        d = block[1]
        self.assertEqual((d["date"], d["symbol"], d["side"], d["qty"], d["price"], d["value"]),
                         ("2026-10-28", "STLTECH", "SELL", 601900, 887.0, 533885300.0))
        self.assertEqual(ins.follow_match(d["client"], ["serum institute", "jhunjhunwala"]), ["serum institute"])
        with open(os.path.join(COMMON, "bulk.csv")) as fh:
            bulk = ins.parse_deals(fh.read(), "bulk")
        self.assertEqual(len(bulk), 57)
        self.assertEqual(bulk[0]["client"], "VAIBHAV RAJENDRA DOSHI")
        self.assertEqual(bulk[5]["client"], "MANSI CHANDAK")                      # double space squeezed
        self.assertIn("Rs 53,38,85,300", ins.deal_line(d, ["serum institute"]))


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.base = ["--trader-dir", self.d]

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_insider_alerts_once_and_filters(self):
        args = self.base + ["insider", "--now", "2026-10-29 16:00"] + OFF
        code, text, _ = run(args)
        self.assertEqual(code, 0)
        self.assertEqual(text.count("NEW INSIDER"), 5)
        self.assertIn("[PROMOTER OPEN-MARKET BUY]", text)
        self.assertIn("filed days after the trade", text)
        code, text, _ = run(args)
        self.assertEqual(code, 1)
        self.assertIn("No new insider disclosures", text)
        code, text, _ = run(args + ["--all", "--promoters-only", "--buys", "--min-value", "1000000", "--json"])
        rows = json.loads(text)["rows"]
        self.assertEqual([(r["symbol"], r["instrument"]) for r in rows],
                         [("BAJFINANCE", "Debenture"), ("SKMEGGPROD", "Equity")])
        code, text, _ = run(args + ["--all", "--open-market", "--promoters-only", "--json"])
        self.assertEqual([r["symbol"] for r in json.loads(text)["rows"]], ["BAJFINANCE", "SKMEGGPROD"])
        code, text, _ = run(args + ["--all", "--symbol", "infy", "--json"])
        self.assertEqual([r["person"] for r in json.loads(text)["rows"]], ["Mayank Ranjan"])
        with open(os.path.join(self.d, "state", "insiders.json")) as fh:
            st = json.load(fh)
        self.assertEqual(len(st["files"]), 4)                                    # each XBRL read once

    def test_legacy_feed_on_request(self):
        code, text, _ = run(self.base + ["insider", "--legacy", "--promoters-only", "--json"] + OFF)
        doc = json.loads(text)
        self.assertIn("BAJAJFINSV", [r["symbol"] for r in doc["rows"]])
        self.assertIn("PARIN", [r["symbol"] for r in doc["rows"]])
        self.assertTrue(doc["errors"])                                           # files not recorded say so

    def test_deals_follow_saved_and_rings_once(self):
        args = self.base + ["deals", "--now", "2026-10-29 16:00"] + OFF
        code, text, _ = run(args + ["--follow", "Serum Institute"])
        self.assertEqual(code, 0)
        self.assertIn("Following Serum Institute", text)
        self.assertIn("These are not today's (2026-10-29)", text)
        self.assertIn("FOLLOWED NAME BLOCK 2026-10-28 STLTECH", text)
        with open(os.path.join(self.d, "config.json")) as fh:
            self.assertEqual(json.load(fh)["follow_names"], ["Serum Institute"])
        code, text, _ = run(args + ["--following"])
        self.assertNotIn("FOLLOWED NAME", text)                                  # rang already
        self.assertIn("<- FOLLOW: Serum Institute", text)
        code, text, _ = run(args + ["--min-value", "500000000", "--buys", "--json"])
        doc = json.loads(text)
        self.assertEqual(sorted(set(d["symbol"] for d in doc["deals"])), ["KAYNES", "OLAELEC", "PGEL", "STLTECH"])
        code, text, _ = run(args + ["--unfollow", "serum institute", "--following"])
        self.assertEqual(code, 1)

    def test_watch_uses_watchlist_and_paper(self):
        with open(os.path.join(self.d, "watchlist.json"), "w") as fh:
            json.dump({"symbols": ["STLTECH"]}, fh)
        with open(os.path.join(self.d, "paper.json"), "w") as fh:
            json.dump({"positions": [{"symbol": "SKMEGGPROD", "qty": 50}]}, fh)
        code, text, _ = run(self.base + ["watch", "--now", "2026-10-29 16:00"] + OFF)
        self.assertEqual(code, 0)
        self.assertIn("Watching SKMEGGPROD, STLTECH.", text)
        self.assertIn("NEW INSIDER SKMEGGPROD", text)
        self.assertEqual(text.count("DEAL BLOCK"), 2)
        code, text, _ = run(self.base + ["watch"] + OFF)
        self.assertIn("INSIDER SKMEGGPROD", text)
        self.assertNotIn("NEW INSIDER", text)
        code, _, err = run(["--trader-dir", os.path.join(self.d, "empty"), "watch"] + OFF)
        self.assertEqual(code, 1)
        self.assertIn("no symbols", err)

    def test_only_archive_hosts(self):
        src = ins.Source()
        with self.assertRaises(ins.InsiderError):
            src.get("https://www.nseindia.com/api/corporates-pit")


if __name__ == "__main__":
    unittest.main()
