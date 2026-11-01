import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX, COMMON

import nse

res = load("india-analyst", "results")

ADIR = os.path.join(FIX, "india-analyst")
BASE = "https://nsearchives.nseindia.com/corporate/xbrl/"
HERO_CONS = "INTEGRATED_FILING_INDAS_1748300_28102026090830_WEB.xml"
HERO_STD = "INTEGRATED_FILING_INDAS_1748299_28102026090747_WEB.xml"
GMR = "INTEGRATED_FILING_INDAS_1748397_28102026112927_WEB.xml"
BANK = "INTEGRATED_FILING_BANKING_1748586_29102026012518_WEB.xml"
NBFC = "INTEGRATED_FILING_NBFC_INDAS_1748551_29102026124836_WEB.xml"
NONINDAS = "INTEGRATED_FILING_NONINDAS_1748452_29102026111652_WEB.xml"
EQ = os.path.join(COMMON, "EQUITY_L.csv")


def filing(name, folder=ADIR):
    p = os.path.join(folder, name)
    if not os.path.exists(p):
        p += ".gz"
    return res.parse_xbrl(nse.read_maybe_gz(p), BASE + name)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = res.main(argv)
    return code, out.getvalue(), err.getvalue()


class Parse(unittest.TestCase):
    def test_consolidated_ind_as(self):
        f = filing(HERO_CONS)
        self.assertEqual((f["symbol"], f["name"], f["basis"], f["family"]),
                         ("HEROMOTOCO", "HERO MOTOCORP LIMITED", "consolidated", "ind-as"))
        self.assertEqual(f["rounding"], "Crores")
        q, ytd = f["periods"][0], f["periods"][1]
        self.assertEqual((q["start"], q["end"], q["kind"]), ("2026-07-01", "2026-09-30", "quarter"))
        self.assertEqual((ytd["start"], ytd["kind"]), ("2026-04-01", "half year"))
        v = q["values"]
        self.assertEqual(v["revenue"], 149705000000.0)            # Rs 14,970.50 crore, filed in rupees
        self.assertEqual(v["total_income"], 151720000000.0)
        self.assertEqual(v["expenses"], 132279900000.0)
        self.assertEqual(v["pbt"], 19440100000.0)
        self.assertEqual(v["net_profit"], 18605500000.0)
        self.assertEqual(v["owners_profit"], 18613300000.0)
        self.assertEqual((v["eps_basic"], v["eps_diluted"]), (92.99, 92.91))
        self.assertEqual(res.fiscal_label(q["start"], q["end"]), "Q2 FY27")
        self.assertEqual(res.fiscal_label(ytd["start"], ytd["end"]), "H1 FY27")

    def test_standalone_has_no_owners_line(self):
        f = filing(HERO_STD)
        self.assertEqual(f["basis"], "standalone")
        self.assertNotIn("owners_profit", f["periods"][0]["values"])
        self.assertEqual(f["periods"][0]["values"]["eps_diluted"], 72.69)

    def test_bank(self):
        f = filing(BANK)
        self.assertEqual((f["symbol"], f["name"], f["family"], f["basis"]),
                         ("ESAFSFB", "ESAF SMALL FINANCE BANK LIMITED", "bank", "standalone"))
        v = f["periods"][0]["values"]
        self.assertEqual(v["revenue"], 11747124000.0)               # interest earned
        self.assertEqual(v["net_interest_income"], 11747124000.0 - 5314628000.0)
        self.assertEqual((v["pbt"], v["net_profit"]), (2145016000.0, 1601748000.0))
        self.assertEqual((v["gross_npa_pct"], v["net_npa_pct"]), (2.84, 0.34))
        self.assertEqual(v["eps_diluted"], 3.1)
        self.assertEqual(v["provisions"], 1478913000.0)

    def test_nbfc_non_ind_as_and_the_older_taxonomy(self):
        f = filing(NBFC)
        self.assertEqual((f["symbol"], f["family"], f["basis"]), ("EMKAY", "nbfc", "consolidated"))
        self.assertEqual(f["periods"][0]["values"]["revenue"], 1513490000.0)
        f = filing(NONINDAS)
        self.assertEqual((f["symbol"], f["family"], f["period_type"]), ("MONOLITH", "non-ind-as", "Half Yearly"))
        self.assertEqual(len(f["periods"]), 1)
        self.assertEqual((f["periods"][0]["kind"], f["periods"][0]["values"]["net_profit"]), ("half year", 145180000.0))
        f = filing("res.xml", COMMON)
        self.assertEqual((f["symbol"], f["family"], f["basis"]),
                         ("AHLWEST", "ind-as (older in-bse-fin taxonomy)", "consolidated"))
        # the FourD context's own dates say Q4, its DateOfStartOfReportingPeriod says the year
        self.assertEqual([(p["start"], p["kind"]) for p in f["periods"]],
                         [("2021-01-01", "quarter"), ("2020-04-01", "year")])
        self.assertEqual(f["periods"][0]["values"]["eps_diluted"], -33.65)
        self.assertEqual(res.fiscal_label("2021-01-01", "2021-03-31"), "Q4 FY21")

    def test_owners_share_left_at_zero_is_not_read_as_zero(self):
        f = filing("INTEGRATED_FILING_INDAS_1748771_29102026035145_WEB.xml")    # Ashima, Q2 FY27, real filing
        v = f["periods"][0]["values"]
        self.assertEqual((f["symbol"], f["basis"], v["net_profit"]), ("ASHIMASYN", "consolidated", -201907000.0))
        self.assertNotIn("owners_profit", v)
        self.assertTrue(f["owners_not_filed"])
        self.assertIn("net profit -Rs 20.19 cr", res.one_line(f))

    def test_formats_it_cannot_read_fail_clearly(self):
        with open(os.path.join(FIX, "india-insiders", "IT_180_WebXMLFile_20261029_120656926.xml"), "rb") as fh:
            with self.assertRaises(res.ResultsError) as e:
                res.parse_xbrl(fh.read(), "insider.xml")
        self.assertIn("no profit-and-loss lines", str(e.exception))
        insurer = (b'<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:x="urn:x">'
                   b'<x:NetPremiumEarned contextRef="OneD">1</x:NetPremiumEarned></xbrli:xbrl>')
        with self.assertRaises(res.ResultsError) as e:
            res.parse_xbrl(insurer)
        self.assertIn("insurer", str(e.exception))
        with self.assertRaises(res.ResultsError):
            res.parse_xbrl(b"<html>not xbrl</html>")


class Store(unittest.TestCase):
    def test_derived_quarter_and_qoq(self):
        st = {"symbol": "HEROMOTOCO", "periods": []}
        self.assertEqual(res.merge(st, filing(HERO_CONS)), 3)
        q1 = [r for r in st["periods"] if r["derived"]][0]
        self.assertEqual((q1["start"], q1["end"]), ("2026-04-01", "2026-06-30"))
        self.assertEqual(q1["values"]["revenue"], 280968500000.0 - 149705000000.0)
        comp = res.compare(st, "consolidated")
        top = comp[0]
        self.assertEqual(top["row"]["end"], "2026-09-30")
        self.assertIs(top["prev"], q1)
        self.assertIsNone(top["year_ago"])
        self.assertEqual(res.pct(top["row"]["values"]["revenue"], q1["values"]["revenue"]), 14.0)
        lines = res.headline(st, "consolidated")
        self.assertIn("Rs 14,970.50 cr (+14.0% vs the quarter before, derived)", lines[0])
        self.assertIn("profit attributable to owners Rs 1,861.33 cr", lines[0])
        self.assertTrue(any("No year-earlier quarter (ending 2025-09-30)" in l for l in lines))

    def test_filed_beats_derived_and_year_on_year_when_stored(self):
        st = {"symbol": "HEROMOTOCO", "periods": []}
        res.merge(st, filing(HERO_CONS))
        # A filed Q1 (built here from the same filing's numbers) replaces the derived one
        f = filing(HERO_CONS)
        q1 = json.loads(json.dumps(f["periods"][0]))
        q1.update(start="2026-04-01", end="2026-06-30")
        res.merge(st, dict(f, periods=[q1]))
        rows = [r for r in st["periods"] if r["end"] == "2026-06-30"]
        self.assertEqual((len(rows), rows[0]["derived"]), (1, False))
        res.merge(st, filing(HERO_CONS))                       # re-reading Q2 keeps the filed Q1
        self.assertFalse([r for r in st["periods"] if r["end"] == "2026-06-30"][0]["derived"])
        # last year's Q2 (the same numbers shifted a year, to exercise the match) gives YoY
        ly = json.loads(json.dumps(f["periods"][0]))
        ly.update(start="2025-07-01", end="2025-09-30")
        for k in ly["values"]:
            ly["values"][k] = ly["values"][k] / 2
        res.merge(st, dict(f, periods=[ly]))
        top = res.compare(st, "consolidated")[0]
        self.assertEqual(top["year_ago"]["end"], "2025-09-30")
        self.assertIn("(+100.0% vs a year earlier)", res.headline(st, "consolidated")[0])

    def test_gmr_flags(self):
        st = {"symbol": "GMRAIRPORT", "periods": []}
        res.merge(st, filing(GMR))
        lines = " ".join(res.headline(st, "consolidated"))
        self.assertIn("profit attributable to owners -Rs 14.92 cr", lines)
        self.assertIn("Share of associates and joint ventures (Rs 54.84 cr)", lines)
        self.assertIn("minority shareholders", lines)
        text = res.render(st)
        self.assertIn("-14.92", text)

    def test_bank_headline(self):
        st = {"symbol": "ESAFSFB", "periods": [], "family": "bank"}
        res.merge(st, filing(BANK))
        lines = res.headline(st, "standalone")
        self.assertIn("interest earned Rs 1,174.71 cr", lines[0])
        self.assertIn("Bank: net interest income Rs 643.25 cr", " ".join(lines))
        self.assertIn("gross NPA 2.84%, net NPA 0.34%", " ".join(lines))


class Feeds(unittest.TestCase):
    def feeds(self):
        out = {}
        for name, path in (("integrated", os.path.join(ADIR, "Integrated_Filing_Financials.xml")),
                           ("integrated_all", os.path.join(ADIR, "Integrated_Filing.xml")),
                           ("legacy", os.path.join(COMMON, "Financial_Results.xml")),
                           ("announcements", os.path.join(COMMON, "Online_announcements.xml"))):
            with open(path, "rb") as fh:
                out[name] = res.parse_rss(fh.read())
        return out

    def test_items_symbols_and_links(self):
        with open(EQ) as fh:
            names = res.name_index(nse.parse_equity_list(fh.read()))
        items = res.results_items(self.feeds(), names)
        x = [i for i in items if i["kind"] == "xbrl"]
        links = [i["link"] for i in x]
        self.assertEqual(len(links), len(set(links)))                      # deduped
        hero = [i for i in x if i["symbol"] == "HEROMOTOCO"]
        self.assertEqual(sorted(i["link"] for i in hero), [BASE + HERO_STD, BASE + HERO_CONS])   # /xbrl/ fixed
        bank = [i for i in x if i["symbol"] == "ESAFSFB"][0]
        self.assertEqual((bank["family"], bank["published"]), ("bank", "2026-10-29T13:24:27+05:30"))
        self.assertFalse([i for i in x if "GOVERNANCE" in i["link"]])
        ann = [i for i in items if i["kind"] == "announcement"]
        self.assertTrue(all("financial result" in i["note"].lower() for i in ann))
        self.assertFalse([i for i in ann if "PRIOR_INTIMATION" in i["link"]])
        self.assertIn("LAURUSLABS", [i["symbol"] for i in ann])

    def test_only_nse_archive_hosts(self):
        self.assertTrue(res.check_url(BASE + GMR))
        self.assertTrue(res.check_url("https://archives.nseindia.com/corporate/xbrl/INDAS_1.xml"))
        for bad in ("https://www.nseindia.com/api/corporates-financial-results", "http://nsearchives.nseindia.com/x.xml",
                    "https://example.com/x.xml"):
            with self.assertRaises(res.ResultsError):
                res.check_url(bad)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.base = ["--trader-dir", self.d]
        self.off = ["--offline", ADIR, "--offline", COMMON]

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_show_saves_and_compares_both_bases(self):
        code, text, _ = run(self.base + ["show", "HEROMOTOCO", "--xbrl", BASE + HERO_CONS, "--xbrl", BASE + HERO_STD,
                                         "--now", "2026-10-29 16:00"] + self.off)
        self.assertEqual(code, 0)
        self.assertIn("HERO MOTOCORP LIMITED (HEROMOTOCO), consolidated results", text)
        self.assertIn("Q2 FY27    2026-07-01..2026-09-30    14,970.50", text)
        self.assertIn("Also stored: standalone results", text)
        with open(os.path.join(self.d, "results", "HEROMOTOCO.json")) as fh:
            store = json.load(fh)
        self.assertEqual(len(store["periods"]), 6)
        self.assertEqual(store["updated"], "2026-10-29T16:00:00+05:30")
        code, text, _ = run(self.base + ["show", "heromotoco.ns", "--basis", "standalone"])
        self.assertIn("14,687.43", text)
        self.assertIn("+13.0%", text)

    def test_show_refuses_another_companys_filing_and_says_when_empty(self):
        code, _, err = run(self.base + ["show", "HEROMOTOCO", "--xbrl", BASE + GMR] + self.off)
        self.assertEqual(code, 1)
        self.assertIn("GMRAIRPORT filing, not HEROMOTOCO", err)
        code, text, _ = run(self.base + ["show", "INFY"])
        self.assertEqual(code, 1)
        self.assertIn("No results stored for INFY yet", text)

    def test_latest_new_once_and_save(self):
        args = self.base + ["latest", "--equity-list", EQ, "--now", "2026-10-29 16:00"] + self.off
        code, text, _ = run(args + ["--symbol", "ESAFSFB", "--save"])
        self.assertEqual(code, 0)
        self.assertIn("RESULTS 2026-10-29 13:24 ESAF Small Finance Bank Limited (ESAFSFB)", text)
        self.assertIn("ESAFSFB Q2 FY27 standalone: revenue Rs 1,174.71 cr", text)
        self.assertIn("ANNOUNCED", text)                                   # the outcome PDF too
        self.assertTrue(os.path.exists(os.path.join(self.d, "results", "ESAFSFB.json")))
        code, text, _ = run(args + ["--symbol", "ESAFSFB"])
        self.assertEqual(code, 1)
        self.assertIn("No new results filings for ESAFSFB", text)
        code, text, _ = run(args + ["--json", "--no-announcements"])
        rows = json.loads(text)["new"]
        self.assertEqual(sum(1 for r in rows if r["symbol"] == "ESAFSFB"), 0)
        self.assertIn("GMRAIRPORT", [r["symbol"] for r in rows])
        self.assertTrue(all("summary" not in r for r in rows))             # listed, not fetched

    def test_latest_watchlist(self):
        with open(os.path.join(self.d, "watchlist.json"), "w") as fh:
            json.dump({"symbols": ["GMRAIRPORT"]}, fh)
        code, text, _ = run(self.base + ["latest", "--watchlist", "--no-announcements", "--equity-list", EQ] + self.off)
        self.assertEqual(code, 0)
        self.assertIn("GMRAIRPORT Q2 FY27 consolidated", text)
        self.assertNotIn("HEROMOTOCO", text)

    def test_metrics_example_is_a_real_run(self):
        with open(os.path.join(ROOT, "india-analyst", "metrics.example.json")) as fh:
            doc = json.load(fh)
        rows = dict((c["symbol"], c) for c in doc["companies"])
        self.assertEqual(sorted(rows), ["EMKAY", "ESAFSFB", "GMRAIRPORT", "HEROMOTOCO", "MONOLITH"])
        st = {"symbol": "HEROMOTOCO", "periods": []}
        res.merge(st, filing(HERO_CONS))
        res.merge(st, filing(HERO_STD))
        self.assertEqual(res.metrics_row(st), rows["HEROMOTOCO"])


if __name__ == "__main__":
    unittest.main()
