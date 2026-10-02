#!/usr/bin/env python3
"""
House IE Tracker

Builds a self-updating "House Races w/ Party Spending (General Election)" table from the
FEC's bulk file of 24/48-hour independent expenditure reports. Standard library only;
no FEC API key.

    python tracker.py                  download the latest FEC file, rebuild docs/ if it changed
    python tracker.py --force          rebuild even if the FEC file hasn't changed
    python tracker.py --input f.csv    build from a local copy of the IE file (no download)
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import re
import sys
import tomllib
import urllib.error
import urllib.request
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

VERSION = "1.0"
ROOT = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
UA = f"house-ie-tracker/{VERSION}"
MONTHS = {m: i for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}

# The FEC's documentation and the actual CSV header use different column names.
HEADER_ALIASES = {
    "can_id": "cand_id", "can_nam": "cand_name", "ele_typ": "ele_type",
    "can_par_aff": "cand_pty_aff", "exp_dat": "exp_date", "amn_ind": "amndt_ind",
    "tra_id": "tran_id", "ima_num": "image_num", "rec_dt": "receipt_dat",
}


# ----------------------------------------------------------------------------- helpers

def parse_date(s: str | None) -> str:
    """FEC dates look like 28-SEP-26 (sometimes MM/DD/YYYY). Returns ISO or ''."""
    s = (s or "").strip()
    if not s:
        return ""
    m = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})-(\d{2,4})", s)
    if m and m[2].upper() in MONTHS:
        year = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
        try:
            return dt.date(year, MONTHS[m[2].upper()], int(m[1])).isoformat()
        except ValueError:
            return ""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y%m%d"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def to_float(s) -> float:
    try:
        return float(str(s).replace(",", "").replace("$", "").strip() or 0)
    except ValueError:
        return 0.0


def num(s: str) -> int:
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def norm_party(s: str | None) -> str:
    s = (s or "").strip().upper()
    if s.startswith("REP"):
        return "REP"
    if s.startswith(("DEM", "DFL")):
        return "DEM"
    return ""


def display_name(n: str) -> str:
    """'MILLER-MEEKS, MARIANNETTE JANE' -> 'MARIANNETTE JANE MILLER-MEEKS'."""
    n = " ".join((n or "").replace(".", ". ").split())
    if "," in n:
        last, first = (x.strip() for x in n.split(",", 1))
        first = re.sub(r"^(MR|MRS|MS|DR|HON)\.?\s+", "", first, flags=re.I)
        first = re.sub(r"\s+(MR|MRS|MS|DR)\.?$", "", first, flags=re.I)
        n = f"{first} {last}".strip()
    return " ".join(n.split()).upper()


def last_name(n: str) -> str:
    n = (n or "").strip()
    if "," in n:
        return n.split(",")[0].strip().upper()
    parts = n.split()
    return parts[-1].upper() if parts else "UNKNOWN"


def side_of(party: str, sup_opp: str) -> str | None:
    if sup_opp not in ("S", "O"):
        return None
    if party == "REP":
        return "R" if sup_opp == "S" else "D"
    if party == "DEM":
        return "D" if sup_opp == "S" else "R"
    return None


def download(url: str, dest: Path, etag: str | None = None) -> dict:
    """Streams url to dest. Sends If-None-Match so unchanged files cost nothing."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            tmp = dest.with_name(dest.name + ".part")
            with open(tmp, "wb") as fh:
                while chunk := r.read(1 << 20):
                    fh.write(chunk)
            tmp.replace(dest)
            return {"changed": True, "etag": r.headers.get("ETag"),
                    "last_modified": r.headers.get("Last-Modified")}
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return {"changed": False, "etag": etag}
        raise


# ----------------------------------------------------------------------------- loading

def load_candidate_master(path: Path) -> dict:
    """FEC cn.txt (pipe-delimited, no header) inside cnYY.zip, or a bare .txt."""
    def parse(lines):
        out = {}
        for line in lines:
            p = line.rstrip("\r\n").split("|")
            if len(p) >= 8 and p[0]:
                out[p[0].upper()] = {"name": p[1], "party": p[2], "state": p[4],
                                     "office": p[5], "district": p[6], "ici": p[7]}
        return out

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".txt"))
            with z.open(name) as fh:
                return parse(io.TextIOWrapper(fh, encoding="latin-1"))
    with open(path, encoding="latin-1") as fh:
        return parse(fh)


def load_pres_margins(path: Path) -> dict:
    """district,margin  -> {district: +R / -D float}. Accepts R+1.8, TRUMP+1.8, D+3, HARRIS+3, -3."""
    out = {}
    if not path.exists():
        return out
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.reader(fh):
            if len(row) < 2 or not row[0].strip() or row[0].strip().lower() == "district":
                continue
            d = re.sub(r"[^A-Z0-9]", "", row[0].upper())
            d = d[:2] + d[2:].zfill(2) if d[2:].isdigit() else d[:2] + "00"
            m = row[1].strip().upper()
            sign = -1 if m.startswith(("D", "HARRIS", "BIDEN")) else 1
            val = re.sub(r"[^0-9.\-]", "", m.split("+")[-1])
            if val:
                out[d] = round(sign * abs(float(val)) if "+" in m else float(val), 1)
    return out


def read_ie(path: Path, cfg: dict) -> tuple[list[dict], dict, Counter]:
    """Returns House general-election rows, the amendment map (all offices), and counts."""
    etypes = tuple(t.upper() for t in cfg.get("filters", {}).get("election_types", ["G"]))
    cycle = str(cfg.get("cycle", 2026))
    parent: dict[str, str] = {}
    rows: list[dict] = []
    stats: Counter = Counter()

    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.DictReader(fh)
        reader.fieldnames = [HEADER_ALIASES.get(h.strip().lower(), h.strip().lower())
                             for h in (reader.fieldnames or [])]
        for r in reader:
            stats["rows_in_file"] += 1
            fn = (r.get("file_num") or "").strip()
            prev = (r.get("prev_file_num") or "").strip()
            if fn and prev:
                parent[fn] = prev  # amendment chains are tracked across every office

            if (r.get("can_office") or "").strip().upper() != "H":
                continue
            if not (r.get("ele_type") or "").strip().upper().startswith(etypes):
                continue
            yr = (r.get("fec_election_yr") or "").strip()
            if yr and yr != cycle:
                continue
            st = (r.get("can_office_state") or "").strip().upper()
            if not re.fullmatch(r"[A-Z]{2}", st):
                stats["skipped_no_state"] += 1
                continue
            dis = (r.get("can_office_dis") or "").strip()
            dis = dis.zfill(2)[-2:] if dis.isdigit() else "00"
            cid = (r.get("cand_id") or "").strip().upper()
            name = (r.get("cand_name") or "").strip()
            district = st + dis
            cand_key = cid if re.fullmatch(r"H[0-9A-Z]{8}", cid) else f"{district}:{last_name(name)}"
            receipt = parse_date(r.get("receipt_dat"))
            rows.append({
                "cand_key": cand_key, "cand_id": cid, "cand_name": name, "district": district,
                "party_raw": norm_party(r.get("cand_pty_aff")),
                "party_text": (r.get("cand_pty_aff") or "").strip().upper(),
                "spe_id": (r.get("spe_id") or "").strip().upper(),
                "spe_nam": " ".join((r.get("spe_nam") or "").split()),
                "amount": to_float(r.get("exp_amo")), "agg": to_float(r.get("agg_amo")),
                "so": (r.get("sup_opp") or "").strip().upper()[:1],
                "purpose": (r.get("pur") or "").strip(), "payee": (r.get("pay") or "").strip(),
                "file_num": fn, "amend": (r.get("amndt_ind") or "").strip(),
                "tran_id": (r.get("tran_id") or "").strip(),
                "image_num": (r.get("image_num") or "").strip(),
                "date": parse_date(r.get("dissem_dt")) or parse_date(r.get("exp_date")) or receipt,
                "filed": receipt,
            })
    stats["rows_house_general"] = len(rows)
    return rows, parent, stats


# ----------------------------------------------------------------------------- cleaning

def superseded_filings(parent: dict[str, str]) -> set[str]:
    """Every filing in an amendment chain except the newest one. Handles chains where
    A2 points at A1 and chains where every amendment points at the original."""
    def root(fn: str) -> str:
        seen = set()
        while fn in parent and fn not in seen:
            seen.add(fn)
            fn = parent[fn]
        return fn

    members = set(parent) | set(parent.values())
    newest: dict[str, str] = {}
    for fn in members:
        r = root(fn)
        if r not in newest or num(fn) > num(newest[r]):
            newest[r] = fn
    return {fn for fn in members if newest[root(fn)] != fn}


def clean_rows(rows: list[dict], superseded: set[str], stats: Counter) -> list[dict]:
    """Drop rows from amended-away filings, then exact re-filings of the same transaction."""
    out, seen = [], set()
    for r in sorted(rows, key=lambda r: num(r["file_num"]), reverse=True):
        if r["file_num"] in superseded:
            stats["dropped_amended"] += 1
            continue
        if r["tran_id"]:
            key = (r["spe_id"] or r["spe_nam"].upper(), r["tran_id"], r["cand_key"],
                   round(r["amount"], 2), r["date"])
            if key in seen:
                stats["dropped_duplicate"] += 1
                continue
            seen.add(key)
        out.append(r)
    stats["rows_counted"] = len(out)
    return out


def resolve_candidates(rows: list[dict], master: dict, cfg: dict) -> tuple[dict, list[dict]]:
    overrides = {k.upper(): norm_party(v) for k, v in cfg.get("party_overrides", {}).items()}
    agg = defaultdict(lambda: {"districts": Counter(), "parties": Counter(),
                               "names": Counter(), "texts": Counter()})
    for r in rows:
        a = agg[r["cand_key"]]
        a["districts"][r["district"]] += abs(r["amount"]) or 1
        a["parties"][r["party_raw"]] += 1
        a["names"][r["cand_name"]] += 1
        a["texts"][r["party_text"]] += 1

    cands, notes = {}, []
    for key, a in agg.items():
        m = master.get(key, {})
        reported = next((p for p, _ in a["parties"].most_common() if p), "")
        mparty = norm_party(m.get("party"))
        if key in overrides:
            party, how = overrides[key], "override"
        elif reported:
            party, how = reported, "filers"
        else:
            party, how = mparty, "fec_master" if mparty else "unknown"
        if how in ("fec_master", "unknown") or (how == "filers" and mparty and mparty != reported):
            notes.append({"key": key, "name": display_name(m.get("name") or a["names"].most_common(1)[0][0]),
                          "district": a["districts"].most_common(1)[0][0],
                          "filers_say": a["texts"].most_common(1)[0][0] or "blank",
                          "fec_master_says": m.get("party", "") or "n/a", "counted_as": party or "unassigned"})
        cands[key] = {
            "key": key,
            "name": display_name(m.get("name") or a["names"].most_common(1)[0][0]),
            "party": party,
            "district": a["districts"].most_common(1)[0][0],
            "incumbent": m.get("ici") == "I",
        }
    return cands, notes


# ----------------------------------------------------------------------------- aggregation

def compile_columns(cfg: dict) -> list[dict]:
    cols = []
    for c in cfg.get("columns", []):
        cols.append({"key": c["key"], "label": c.get("label", c["key"]), "side": c["side"].upper(),
                     "ids": {i.upper() for i in c.get("ids", [])},
                     "pats": [re.compile(p, re.I) for p in c.get("name_patterns", [])]})
    return cols


def classify(spe_id: str, spe_nam: str, side: str, cols: list[dict]) -> str:
    for c in cols:
        if spe_id in c["ids"] or any(p.search(spe_nam) for p in c["pats"]):
            return c["key"] if c["side"] == side else f"OTH_{side}"
    return f"OTH_{side}"


def build(cfg: dict, rows: list[dict], cands: dict, pres: dict) -> dict:
    cols = compile_columns(cfg)
    named = [c["key"] for c in cols]
    as_of = max((r["filed"] for r in rows if r["filed"]), default=dt.date.today().isoformat())
    week_ago = (dt.date.fromisoformat(as_of) - dt.timedelta(days=6)).isoformat()

    races: dict[str, dict] = {}
    unassigned: dict[str, dict] = {}
    pair_check = defaultdict(lambda: {"sum": 0.0, "agg": 0.0, "label": ("", "", "")})

    for r in rows:
        c = cands[r["cand_key"]]
        side = side_of(c["party"], r["so"])
        if side is None:
            u = unassigned.setdefault(c["key"], {"name": c["name"], "district": c["district"], "amount": 0.0})
            u["amount"] += r["amount"]
            continue
        col = classify(r["spe_id"], r["spe_nam"], side, cols)
        d = c["district"]
        race = races.setdefault(d, {"cols": defaultdict(float), "cand_money": defaultdict(float),
                                    "spenders": {}, "new7": defaultdict(float), "last_filed": ""})
        race["cols"][col] += r["amount"]
        race["cand_money"][c["key"]] += abs(r["amount"])
        if r["filed"] >= week_ago:
            race["new7"][side] += r["amount"]
        race["last_filed"] = max(race["last_filed"], r["filed"])

        sk = f"{r['spe_id'] or r['spe_nam']}|{col}"
        sp = race["spenders"].setdefault(sk, {"id": r["spe_id"], "name": r["spe_nam"], "col": col,
                                              "side": side, "amount": 0.0, "targets": Counter(),
                                              "last": "", "n": 0})
        sp["amount"] += r["amount"]
        sp["targets"][f"{'For' if r['so'] == 'S' else 'Against'} {c['name']}"] += r["amount"]
        sp["last"] = max(sp["last"], r["date"] or r["filed"])
        sp["n"] += 1

        pc = pair_check[(r["spe_id"] or r["spe_nam"].upper(), c["key"])]
        pc["sum"] += r["amount"]
        pc["agg"] = max(pc["agg"], r["agg"])
        pc["label"] = (r["spe_nam"], c["name"], d)

    out_races = []
    for d, race in races.items():
        nominees = {}
        for party in ("REP", "DEM"):
            pool = [(amt, k) for k, amt in race["cand_money"].items() if cands[k]["party"] == party]
            if pool:
                k = max(pool)[1]
                nominees[party] = {"name": cands[k]["name"], "incumbent": cands[k]["incumbent"], "id": k}
        cvals = {k: round(v, 2) for k, v in race["cols"].items()}
        r_total = sum(v for k, v in cvals.items() if k in [c["key"] for c in cols if c["side"] == "R"] + ["OTH_R"])
        d_total = sum(v for k, v in cvals.items() if k in [c["key"] for c in cols if c["side"] == "D"] + ["OTH_D"])
        spenders = sorted(
            ({"id": s["id"], "name": s["name"], "col": s["col"], "side": s["side"],
              "amount": round(s["amount"], 2), "last": s["last"], "n": s["n"],
              "targets": [t for t, _ in s["targets"].most_common()]}
             for s in race["spenders"].values()),
            key=lambda s: -s["amount"])
        st, dn = d[:2], d[2:]
        out_races.append({
            "district": d, "pres": pres.get(d),
            "rep": nominees.get("REP"), "dem": nominees.get("DEM"),
            "cols": cvals, "r_total": round(r_total, 2), "d_total": round(d_total, 2),
            "total": round(r_total + d_total, 2), "adv": round(d_total - r_total, 2),
            "new7": round(race["new7"]["R"] + race["new7"]["D"], 2),
            "new7_r": round(race["new7"]["R"], 2), "new7_d": round(race["new7"]["D"], 2),
            "party_race": any(abs(cvals.get(k, 0)) > 0 for k in named),
            "last_filed": race["last_filed"], "spenders": spenders,
            "fec_url": f"https://www.fec.gov/data/elections/house/{st}/{dn}/{cfg.get('cycle', 2026)}/",
        })
    out_races.sort(key=lambda r: -r["total"])

    # Biggest groups that don't have their own column, to help decide what to promote.
    other = defaultdict(lambda: {"amount": 0.0, "races": set(), "id": "", "name": ""})
    for race in out_races:
        for s in race["spenders"]:
            if s["col"].startswith("OTH_"):
                o = other[(s["id"] or s["name"], s["side"])]
                o["amount"] += s["amount"]
                o["races"].add(race["district"])
                o["id"], o["name"] = s["id"], s["name"]
    top_other = {side: [{"id": o["id"], "name": o["name"], "amount": round(o["amount"], 2),
                         "races": len(o["races"])}
                        for (k, sd), o in sorted(other.items(), key=lambda kv: -kv[1]["amount"]) if sd == side][:12]
                 for side in ("R", "D")}

    over = [{"spender": v["label"][0], "candidate": v["label"][1], "district": v["label"][2],
             "counted": round(v["sum"], 2),
             "filer_aggregate": round(v["agg"], 2)}
            for k, v in pair_check.items()
            if v["agg"] > 0 and v["sum"] > v["agg"] * 1.05 and v["sum"] - v["agg"] > 10_000]
    over.sort(key=lambda x: -(x["counted"] - x["filer_aggregate"]))

    columns = ([{"key": c["key"], "label": c["label"], "side": "R"} for c in cols if c["side"] == "R"]
               + [{"key": "OTH_R", "label": cfg.get("display", {}).get("other_r_label", "OTH R"), "side": "R"}]
               + [{"key": c["key"], "label": c["label"], "side": "D"} for c in cols if c["side"] == "D"]
               + [{"key": "OTH_D", "label": cfg.get("display", {}).get("other_d_label", "OTH D"), "side": "D"}])

    return {
        "as_of": as_of, "week_start": week_ago, "columns": columns, "races": out_races,
        "default_view": cfg.get("filters", {}).get("races", "party"),
        "diagnostics": {
            "unassigned": sorted(({"key": k, **v, "amount": round(v["amount"], 2)}
                                  for k, v in unassigned.items()), key=lambda x: -x["amount"]),
            "top_other": top_other,
            "possible_double_counts": over[:15],
        },
    }


# ----------------------------------------------------------------------------- output

def write_outputs(result: dict, rows: list[dict], cands: dict, out: Path, cfg: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "data.json").write_text(json.dumps(result, separators=(",", ":")))

    keys = [c["key"] for c in result["columns"]]
    with open(out / "races.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["district", "pres_2024_margin_r", "rep", "dem"]
                   + [c["label"] for c in result["columns"]]
                   + ["r_total", "d_total", "advantage_d_minus_r", "filed_last_7_days", "total"])
        for r in result["races"]:
            w.writerow([r["district"], r["pres"] if r["pres"] is not None else "",
                        (r["rep"] or {}).get("name", ""), (r["dem"] or {}).get("name", "")]
                       + [r["cols"].get(k, 0) for k in keys]
                       + [r["r_total"], r["d_total"], r["adv"], r["new7"], r["total"]])

    with open(out / "transactions.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["district", "candidate", "candidate_party", "support_oppose", "spender_id", "spender",
                    "amount", "date", "filed", "purpose", "payee", "file_num", "tran_id", "image_num"])
        for r in sorted(rows, key=lambda r: (r["filed"], r["date"]), reverse=True):
            c = cands[r["cand_key"]]
            w.writerow([c["district"], c["name"], c["party"], r["so"], r["spe_id"], r["spe_nam"],
                        r["amount"], r["date"], r["filed"], r["purpose"], r["payee"],
                        r["file_num"], r["tran_id"], r["image_num"]])

    template = (ROOT / "template.html").read_text()
    payload = json.dumps(result, separators=(",", ":")).replace("</", "<\\/")
    page = (template.replace("/*__DATA__*/null", payload)
                    .replace("__TITLE__", cfg.get("display", {}).get("title", "House IE Tracker")))
    (out / "index.html").write_text(page)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--input", help="local IE CSV instead of downloading")
    ap.add_argument("--candidates", help="local cnYY.zip / cn.txt instead of downloading")
    ap.add_argument("--out", default=str(ROOT / "docs"))
    ap.add_argument("--force", action="store_true", help="rebuild even if the FEC file is unchanged")
    ap.add_argument("--note", default="", help="banner text to show on the page (e.g. for previews)")
    args = ap.parse_args()

    cfg_text = Path(args.config).read_text()
    cfg = tomllib.loads(cfg_text)
    out = Path(args.out)
    meta_path = out / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    cycle = int(cfg.get("cycle", 2026))
    fmt = {"cycle": cycle, "yy": str(cycle)[-2:]}
    build_sig = hashlib.sha1((VERSION + cfg_text + (ROOT / "template.html").read_text()).encode()).hexdigest()
    pres_path = ROOT / cfg.get("sources", {}).get("pres_margins_csv", "pres_margins_2024.csv")
    pres_sig = hashlib.sha1(pres_path.read_bytes()).hexdigest() if pres_path.exists() else ""

    if args.input:
        ie_path = Path(args.input)
        source = {"ie_url": str(ie_path), "last_modified": None, "etag": None}
    else:
        url = cfg["sources"]["ie_url"].format(**fmt)
        ie_path = ROOT / "data" / Path(url).name
        unchanged_build = meta.get("build_sig") == build_sig and meta.get("pres_sig") == pres_sig
        etag = meta.get("ie_etag") if (unchanged_build and not args.force) else None
        print(f"Checking {url} ...")
        res = download(url, ie_path, etag)
        if not res["changed"]:
            print("FEC file unchanged since last build. Nothing to do.")
            return 0
        source = {"ie_url": url, "last_modified": res.get("last_modified"), "etag": res.get("etag")}

    master: dict = {}
    try:
        if args.candidates:
            master = load_candidate_master(Path(args.candidates))
        elif not args.input and cfg["sources"].get("candidate_master_url"):
            cn_url = cfg["sources"]["candidate_master_url"].format(**fmt)
            cn_path = ROOT / "data" / Path(cn_url).name
            download(cn_url, cn_path)
            master = load_candidate_master(cn_path)
    except Exception as e:  # names/party fall back to what filers report
        print(f"Warning: candidate master file unavailable ({e}); using filer-reported names.", file=sys.stderr)

    rows, parent, stats = read_ie(ie_path, cfg)
    rows = clean_rows(rows, superseded_filings(parent), stats)
    cands, party_notes = resolve_candidates(rows, master, cfg)
    result = build(cfg, rows, cands, load_pres_margins(pres_path))

    now = dt.datetime.now(ET)
    result.update({
        "title": cfg.get("display", {}).get("title", "House IE Tracker"),
        "generated": now.isoformat(timespec="seconds"),
        "generated_label": now.strftime("%b %-d, %Y at %-I:%M %p ET"),
        "source": source, "note": args.note,
        "stats": dict(stats), "has_pres": bool(load_pres_margins(pres_path)),
    })
    result["diagnostics"]["party_notes"] = party_notes
    write_outputs(result, rows, cands, out, cfg)

    meta.update({"ie_etag": source.get("etag"), "ie_last_modified": source.get("last_modified"),
                 "build_sig": build_sig, "pres_sig": pres_sig, "generated": result["generated"]})
    meta_path.write_text(json.dumps(meta, indent=2))

    party = [r for r in result["races"] if r["party_race"]]
    print(f"Rows in file: {stats['rows_in_file']:,} | House general: {stats['rows_house_general']:,} | "
          f"counted: {stats['rows_counted']:,} (dropped {stats['dropped_amended']:,} amended, "
          f"{stats['dropped_duplicate']:,} duplicate)")
    print(f"Races: {len(party)} with party spending, {len(result['races'])} total. "
          f"Data filed through {result['as_of']}. Wrote {out}/index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
