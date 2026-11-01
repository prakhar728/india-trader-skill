import json
import os
import re
import unittest

from _load import ROOT

SKILLS = ["india-scanner", "india-news", "india-entry", "india-exit", "india-timemachine",
          "india-risk", "india-budget", "india-broker", "india-alerts", "india-analyst",
          "india-insiders", "india-backtest", "india-research"]
ORDER_PATH = ["india-scanner", "india-entry", "india-exit", "india-timemachine", "india-budget",
              "india-insiders", "india-risk", "india-broker", "india-alerts"]
# words that would mean a US leftover slipped into an India skill
US_ONLY = [r"\bNYSE\b", r"\bNew York\b", r"\bSEC EDGAR\b", r"\bAlpaca\b", r"\bET\)", r"\$\d",
           r"\bFINRA\b", r"\b8-K\b", r"\bPelosi\b"]


def frontmatter(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1) if m else ""), text


def skill_files():
    for s in SKILLS + ["india-common"]:
        for base, dirs, files in os.walk(os.path.join(ROOT, s)):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                if f.endswith((".md", ".py", ".json")):
                    yield os.path.join(base, f)


class Repo(unittest.TestCase):
    def test_skill_folders(self):
        found = sorted(d for d in os.listdir(ROOT) if os.path.isfile(os.path.join(ROOT, d, "SKILL.md")))
        self.assertEqual(found, sorted(SKILLS))

    def test_common_core_has_no_skill_md(self):
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "india-common", "nse.py")))
        self.assertFalse(os.path.exists(os.path.join(ROOT, "india-common", "SKILL.md")))

    def test_frontmatter(self):
        for s in SKILLS:
            fm, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("name: %s\n" % s, fm + "\n", s)
            self.assertRegex(fm, r"description: ", s)
            self.assertGreater(len(fm), 200, "%s description is too thin" % s)
            self.assertGreater(len(text.splitlines()), 60, s)

    def test_order_path_safety_rule(self):
        for s in ORDER_PATH:
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            low = " ".join(text.lower().split())
            self.assertIn("not financial advice", low, s)
            self.assertIn("yes", low, s)

    def test_scripts_use_the_shared_core(self):
        for s in SKILLS:
            pys = [f for f in os.listdir(os.path.join(ROOT, s)) if f.endswith(".py")]
            self.assertTrue(pys, s)
            for f in pys:
                with open(os.path.join(ROOT, s, f), encoding="utf-8") as fh:
                    src = fh.read()
                self.assertIn("india-common", src, "%s/%s" % (s, f))
                self.assertIn("import nse", src, "%s/%s" % (s, f))

    def test_no_us_leftovers(self):
        for path in skill_files():
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            for pat in US_ONLY:
                self.assertIsNone(re.search(pat, text), "%s matches %s" % (os.path.relpath(path, ROOT), pat))

    def test_no_cookie_scraping_of_nse(self):
        for path in skill_files():
            if path.endswith(".py"):
                with open(path, encoding="utf-8") as fh:
                    self.assertNotIn("www.nseindia.com/api", fh.read(), path)

    def test_plugin_manifest(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json")) as fh:
            plugin = json.load(fh)
        self.assertEqual((plugin["name"], plugin["skills"]), ("india-trader-skill", "./"))
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")) as fh:
            market = json.load(fh)
        self.assertEqual(market["plugins"][0]["name"], "india-trader-skill")


if __name__ == "__main__":
    unittest.main()
