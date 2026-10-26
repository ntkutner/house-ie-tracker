"""Tests for the 48-hour notice page. Run: python -m unittest test_late"""
import json, subprocess, sys, tempfile, unittest, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FIX = ROOT / "late_fixtures"


class LateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        out = cls.tmp / "docs"
        out.mkdir()
        if (ROOT / "docs" / "data.json").exists():
            shutil.copy(ROOT / "docs" / "data.json", out / "data.json")
        subprocess.run([sys.executable, str(ROOT / "late.py"), "--fixtures", str(FIX),
                        "--candidates", str(FIX / "cn.txt"), "--cache", str(cls.tmp / "cache"),
                        "--out", str(out), "--now", "2026-10-26T16:00:00"], check=True)
        html = (out / "late.html").read_text()
        cls.data = json.loads(html.split("const DATA = ", 1)[1].split(";\nconst $", 1)[0])
        cls.cands = {c["name"]: c for c in cls.data["cands"]}
        cls.races = {r["d"]: r for r in cls.data["races"]}

    def test_totals_by_candidate(self):
        self.assertAlmostEqual(self.cands["DON DAVIS"]["tot"], 60300)
        self.assertEqual(self.cands["DON DAVIS"]["by"], [5300, 5000, 0, 50000])  # indiv, PAC, party, self

    def test_amendment_replaces_notice_and_repeats_count_once(self):
        # 1004 amends 1003 (B2 corrected to $3,000); 1005 re-reports B1 and adds $5,000 from the NRCC.
        b = self.cands["LAURIE BUCKHOUT"]
        self.assertAlmostEqual(b["tot"], 11500)
        self.assertEqual(b["notices"], 2)
        self.assertEqual(self.data["stats"]["duplicate_lines"], 1)

    def test_amendment_found_by_transaction_id(self):
        # 1009 is an F6A with no "amends" link in the listing; it shares K1 with 1006.
        k = self.cands["KEVIN KILEY"]
        self.assertAlmostEqual(k["tot"], 1500)
        self.assertEqual(k["p"], "REP")  # party override from config.toml

    def test_race_totals_and_recent(self):
        nc = self.races["NC01"]
        self.assertAlmostEqual(nc["D"], 60300)
        self.assertAlmostEqual(nc["R"], 11500)
        self.assertAlmostEqual(nc["rD"], 52000)   # notice filed Oct 26 10:15 AM
        self.assertAlmostEqual(nc["rR"], 8500)    # notice filed Oct 25

    def test_senate_and_excluded_candidates_left_out(self):
        self.assertNotIn("SAM SENATE", self.cands)
        self.assertNotIn("CHRISTOPHER GETTY", self.cands)  # in [exclusions]
        self.assertEqual(set(self.races), {"NC01", "CA06"})

    def test_early_lines_shown_but_not_counted(self):
        notes = [it for it in self.data["items"] if "pre-general" in it[9]]
        self.assertEqual(len(notes), 1)  # Davis's Oct 12 line; Kiley's early line was amended away
        replaced = [it for it in self.data["items"] if "amendment" in it[9]]
        self.assertEqual(len(replaced), 4)  # 1003's two lines and 1006's two, shown struck through
        self.assertEqual(notes[0][2], "Eve Early")
        self.assertAlmostEqual(self.cands["DON DAVIS"]["tot"], 60300)


class ListingTest(unittest.TestCase):
    """The FEC filing lists, with the API faked: paging, the House filter falling back when the
    API rejects it, and non-Form 6 filings in the raw e-file list."""
    def test_lists(self):
        import urllib.error
        sys.path.insert(0, str(ROOT))
        import late
        calls = []

        def fake(path, params, key):
            p = dict(params)
            calls.append((path, p))
            if path == "/filings/":
                if "committee_type" in p:
                    raise urllib.error.HTTPError("u", 422, "bad", {}, None)
                page = int(p["page"])
                res = [{"file_number": 10 * page + i, "form_type": "F6", "committee_id": "C1",
                        "receipt_date": "2026-10-2%dT00:00:00" % page, "amendment_indicator": "N"} for i in range(3)]
                return {"results": res, "pagination": {"pages": 2}}
            if "form_type" in p:
                raise urllib.error.HTTPError("u", 400, "bad", {}, None)
            if p["page"] == "1":
                return {"results": [{"file_number": 99, "form_type": "F6", "committee_id": "C2",
                                     "receipt_date": "2026-10-26T09:41:07"},
                                    {"file_number": 98, "form_type": "F3X", "committee_id": "C3",
                                     "receipt_date": "2026-10-26T09:40:00"},
                                    {"file_number": 97, "form_type": "F6", "committee_id": "C4",
                                     "receipt_date": "2026-10-14T09:40:00"}]}
            return {"results": []}

        late.api_get = fake
        got = late.list_processed("k", "2026-10-15")
        self.assertEqual(sorted(m["file_number"] for m in got), [10, 11, 12, 20, 21, 22])
        self.assertEqual(got[0]["receipt"], "2026-10-21")
        eff = late.list_efiled("k", "2026-10-15")
        self.assertEqual([m["file_number"] for m in eff], [99])  # 98 isn't a Form 6; 97 is from before the window
        self.assertEqual(eff[0]["receipt"], "2026-10-26T09:41:07")
        idx = {"filings": {}}
        self.assertEqual(late.update_index(idx, got + eff), 7)
        self.assertEqual(late.update_index(idx, [{**got[0], "receipt": "2026-10-21T15:02:00"}]), 0)
        self.assertEqual(idx["filings"]["10"]["receipt"], "2026-10-21T15:02:00")


if __name__ == "__main__":
    unittest.main()
