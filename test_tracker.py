"""Run: python -m unittest discover tests"""
import json, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TrackerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = Path(tempfile.mkdtemp())
        cfg = (ROOT / "config.toml").read_text().replace('races = "party"', 'races = "all"')
        cfg += '\n"H2CA03157" = "REP"\n'  # under [party_overrides], the last table in the file
        (cls.out / "config.toml").write_text(cfg)
        subprocess.run([sys.executable, str(ROOT / "tracker.py"), "--config", str(cls.out / "config.toml"),
                        "--input", str(ROOT / "tests/fixture_ie.csv"), "--out", str(cls.out)], check=True)
        cls.data = json.loads((cls.out / "data.json").read_text())
        cls.races = {r["district"]: r for r in cls.data["races"]}

    def test_amendment_chains_keep_only_newest_filing(self):
        # AB PAC vs Mitchell: two N filings amended by A1s. Filer's own aggregate is $892,779.75.
        self.assertAlmostEqual(self.races["IA02"]["d_total"], 892779.75, places=2)
        self.assertEqual(self.data["stats"]["dropped_amended"], 4)

    def test_duplicate_refilings_counted_once(self):
        # 907 Initiative filed two reports twice. Filer's aggregate is $173,867.52.
        self.assertAlmostEqual(self.races["AK00"]["d_total"], 173867.52, places=2)
        self.assertEqual(self.data["stats"]["dropped_duplicate"], 2)

    def test_primaries_and_senate_excluded(self):
        self.assertNotIn("CA22", self.races)
        self.assertEqual(self.data["stats"]["rows_house_general"], 57)

    def test_support_and_oppose_land_on_same_side(self):
        co = self.races["CO08"]  # 314 Action: for Rutinel (D) + against Evans (R)
        self.assertAlmostEqual(co["d_total"], 253610.45, places=2)
        self.assertEqual(co["rep"]["name"], "TIMOTHY EVANS")
        self.assertEqual(co["dem"]["name"], "MANNY RUTINEL")

    def test_party_override(self):
        ca = self.races["CA06"]  # Kiley reported as OTHER; overridden to REP
        self.assertAlmostEqual(ca["r_total"], 291758, places=2)
        self.assertAlmostEqual(ca["d_total"], 925452, places=2)
        self.assertEqual(self.data["diagnostics"]["unassigned"], [])

    def test_no_double_count_flags(self):
        self.assertEqual(self.data["diagnostics"]["possible_double_counts"], [])


if __name__ == "__main__":
    unittest.main()
