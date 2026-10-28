import sys, json
from collections import Counter, defaultdict
sys.path.insert(0, ".")
import tracker as T
orig = T.build
def wrapped(cfg, rows, cands, pres, excluded):
    same_party = {d.strip().upper(): T.norm_party(v.get("party", "")) for d, v in cfg.get("same_party_races", {}).items()}
    bad = defaultdict(lambda: {"amt": 0.0, "n": 0, "names": Counter(), "party_raw": Counter(), "party_text": Counter(), "so": Counter(), "spe": Counter(), "ids": Counter(), "kind": Counter()})
    for r in rows:
        c = cands[r["cand_key"]]
        sp = same_party.get(c["district"])
        side = (("D" if sp == "DEM" else "R") if sp and r["so"] in ("S", "O") else T.side_of(c["party"], r["so"]))
        if side is None:
            b = bad[(r["cand_key"], c["party"], c["district"])]
            b["amt"] += r["amount"]; b["n"] += 1
            b["names"][r["cand_name"]] += 1; b["party_raw"][r.get("party_raw")] += 1; b["party_text"][r.get("party_text")] += 1
            b["so"][r["so"]] += 1; b["spe"][r["spe_nam"]] += r["amount"]; b["kind"][r["kind"]] += 1
    for k, b in sorted(bad.items(), key=lambda kv: -kv[1]["amt"]):
        print(k, round(b["amt"]), b["n"], dict(b["names"].most_common(3)), dict(b["party_raw"]), dict(b["party_text"]), dict(b["so"]), dict(b["kind"]), [s for s, _ in b["spe"].most_common(3)])
    # what candidates exist in those races
    print("\n-- other candidate keys in those races")
    for (k, p, d) in list(bad)[:40]:
        print(d, k, "->", [(ck, c["name"], c["party"]) for ck, c in cands.items() if c["district"] == d and ck != k][:6])
    return orig(cfg, rows, cands, pres, excluded)
T.build = wrapped
sys.argv = ["tracker.py", "--force", "--out", "/tmp/out"]
T.main()
