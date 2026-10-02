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

VERSION = "1.2"
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
                out[p[0].upper()] = {"name": p[1], "party": p[2], "year": p[3], "state": p[4],
                                     "office": p[5], "district": p[6], "ici": p[7],
                                     "pcc": p[9].strip().upper() if len(p) > 9 else ""}
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


def named_key(spe_id: str, spe_nam: str, cols: list[dict]) -> str | None:
    """Which named column (if any) a spender belongs to, regardless of side."""
    for c in cols:
        if spe_id in c["ids"] or any(p.search(spe_nam) for p in c["pats"]):
            return c["key"]
    return None


def read_ie(path: Path, cfg: dict, cols: list[dict], master: dict) -> tuple[list[dict], dict, Counter, list[dict]]:
    """Returns House general-election rows, the amendment map (all offices), counts, and
    excluded rows from named-column groups (so gaps in e.g. CLF are visible)."""
    etypes = tuple(t.upper() for t in cfg.get("filters", {}).get("election_types", ["G"]))
    cycle = str(cfg.get("cycle", 2026))
    parent: dict[str, str] = {}
    rows: list[dict] = []
    excluded: list[dict] = []
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

            cid = (r.get("cand_id") or "").strip().upper()
            m = master.get(cid, {})
            # Filers sometimes leave office/state/district blank; fill from the candidate ID / FEC master.
            office = (r.get("can_office") or "").strip().upper()
            if not office and re.fullmatch(r"[HSP][0-9A-Z]{8}", cid):
                office = cid[0]
                stats["recovered_office"] += 1
            if office != "H":
                continue
            st = (r.get("can_office_state") or "").strip().upper()
            if not re.fullmatch(r"[A-Z]{2}", st):
                st = (m.get("state") or (cid[2:4] if cid.startswith("H") else "")).upper()
                if st:
                    stats["recovered_state"] += 1
            dis = (r.get("can_office_dis") or "").strip()
            if not dis.isdigit() and (m.get("district") or "").strip().isdigit():
                dis = m["district"].strip()
                stats["recovered_district"] += 1
            dis = dis.zfill(2)[-2:] if dis.isdigit() else "00"

            name = (r.get("cand_name") or "").strip()
            spe_id = (r.get("spe_id") or "").strip().upper()
            spe_nam = " ".join((r.get("spe_nam") or "").split())
            ele = (r.get("ele_type") or "").strip().upper()
            receipt = parse_date(r.get("receipt_dat"))
            date = parse_date(r.get("dissem_dt")) or parse_date(r.get("exp_date")) or receipt
            amount = to_float(r.get("exp_amo"))
            nk = named_key(spe_id, spe_nam, cols)

            def exclude(reason: str):
                if nk:
                    excluded.append({"column": nk, "reason": reason, "spender": spe_nam, "candidate": name,
                                     "race": f"{st}{dis}" if st else "", "election_type": ele or "blank",
                                     "amount": amount, "date": date, "file_num": fn,
                                     "tran_id": (r.get("tran_id") or "").strip()})

            if not ele.startswith(etypes):
                exclude("Coded as primary or other election")
                continue
            yr = (r.get("fec_election_yr") or "").strip()
            if yr and yr != cycle:
                exclude("Different election cycle")
                continue
            if not re.fullmatch(r"[A-Z]{2}", st):
                stats["skipped_no_state"] += 1
                exclude("No state on filing")
                continue
            district = st + dis
            cand_key = cid if re.fullmatch(r"H[0-9A-Z]{8}", cid) else f"{district}:{last_name(name)}"
            rows.append({
                "kind": "IE", "named": nk,
                "cand_key": cand_key, "cand_id": cid, "cand_name": name, "district": district,
                "party_raw": norm_party(r.get("cand_pty_aff")),
                "party_text": (r.get("cand_pty_aff") or "").strip().upper(),
                "spe_id": spe_id, "spe_nam": spe_nam,
                "amount": amount, "agg": to_float(r.get("agg_amo")),
                "so": (r.get("sup_opp") or "").strip().upper()[:1],
                "purpose": (r.get("pur") or "").strip(), "payee": (r.get("pay") or "").strip(),
                "file_num": fn, "amend": (r.get("amndt_ind") or "").strip(),
                "tran_id": (r.get("tran_id") or "").strip(),
                "image_num": (r.get("image_num") or "").strip(),
                "date": date, "filed": receipt,
            })
    stats["rows_house_general"] = len(rows)
    return rows, parent, stats, excluded


def load_coordinated(path: Path, cfg: dict, cols: list[dict], master: dict, stats: Counter,
                     excluded: list[dict]) -> list[dict]:
    """Party coordinated expenditures (transaction type 24C) from the FEC pas2 file, for
    columns marked coordinated = true. Pipe-delimited, 22 columns, no header."""
    coord_cols = [c for c in cols if c["coordinated"]]
    include_memos = cfg.get("filters", {}).get("coordinated_include_memos", True)
    by_pcc = {m["pcc"]: cid for cid, m in master.items() if m.get("pcc")}
    best: dict[tuple, dict] = {}

    def lines():
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                name = next(n for n in z.namelist() if n.lower().endswith(".txt"))
                with z.open(name) as fh:
                    yield from io.TextIOWrapper(fh, encoding="latin-1")
        else:
            with open(path, encoding="latin-1") as fh:
                yield from fh

    def drop(col, reason, amount, date, p, cid=""):
        excluded.append({"column": col["key"], "reason": reason, "spender": col["label"] + " (coordinated)",
                         "candidate": cid or p[7].strip(), "race": "", "election_type": p[3].strip() or "blank",
                         "amount": amount, "date": date, "file_num": p[18].strip(), "tran_id": p[17].strip()})

    for line in lines():
        p = line.rstrip("\r\n").split("|")
        if len(p) < 22 or p[5].strip().upper() != "24C":
            continue
        cmte = p[0].strip().upper()
        col = next((c for c in coord_cols if cmte in c["ids"]), None)
        if not col:
            continue
        amount = to_float(p[14])
        dt_raw = p[13].strip()
        date = parse_date(f"{dt_raw[:2]}/{dt_raw[2:4]}/{dt_raw[4:]}") if len(dt_raw) == 8 else ""
        cid = p[16].strip().upper()
        if not cid.startswith(("H", "S", "P")):
            cid = by_pcc.get(p[15].strip().upper(), "")  # OTHER_ID can be the candidate's committee
            if cid:
                stats["coordinated_id_from_committee"] += 1
        if not cid:
            drop(col, "Coordinated: no candidate identified", amount, date, p)
            continue
        if not cid.startswith("H"):
            continue  # Senate/presidential
        pgi = p[3].strip().upper()
        if pgi and not pgi.startswith("G"):
            drop(col, "Coordinated: special or other election", amount, date, p, cid)
            continue
        memo = p[19].strip().upper() == "X"
        if memo and not include_memos:
            drop(col, "Coordinated: memo entry", amount, date, p, cid)
            continue
        m = master.get(cid)
        if not m or not m.get("state"):
            drop(col, "Coordinated: candidate not in FEC candidate file", amount, date, p, cid)
            continue
        dis = m.get("district", "").strip()
        dis = dis.zfill(2)[-2:] if dis.isdigit() else "00"
        row = {
            "kind": "COORD", "named": col["key"], "memo": memo,
            "cand_key": cid, "cand_id": cid, "cand_name": m.get("name", ""), "district": m["state"].upper() + dis,
            "party_raw": norm_party(m.get("party")), "party_text": (m.get("party") or "").upper(),
            "spe_id": cmte, "spe_nam": f"{col['label']} (coordinated)",
            "amount": amount, "agg": 0.0, "so": "S",
            "purpose": "Coordinated party expenditure" + (" (memo)" if memo else ""), "payee": p[7].strip(),
            "file_num": p[18].strip(), "amend": p[1].strip(), "tran_id": p[17].strip(),
            "image_num": p[4].strip(), "date": date, "filed": date,
        }
        key = (cmte, row["tran_id"]) if row["tran_id"] else (cmte, p[21].strip())
        if key not in best or num(row["file_num"]) >= num(best[key]["file_num"]):
            best[key] = row

    # A memo entry (disseminated, not yet paid) is later re-reported as a regular entry once paid.
    # Count a memo only if no regular entry for the same committee, candidate and amount exists.
    rows = list(best.values())
    paid = Counter((r["spe_id"], r["cand_key"], round(r["amount"], 2)) for r in rows if not r["memo"])
    out = []
    for r in rows:
        k = (r["spe_id"], r["cand_key"], round(r["amount"], 2))
        if r["memo"] and paid[k] > 0:
            paid[k] -= 1
            stats["coordinated_memo_matched"] += 1
            continue
        out.append(r)
    stats["rows_coordinated"] = len(out)
    return out


SUFFIXES = {"JR", "SR", "II", "III", "IV", "V", "MR", "MRS", "MS", "DR", "HON", "REP", "SEN"}


def name_tokens(n: str) -> tuple[list[str], list[str] | None]:
    """(all tokens, last-name tokens if the name is 'LAST, FIRST' else None), suffixes removed."""
    n = re.sub(r"[^A-Z,\- ]", " ", (n or "").upper())
    clean = lambda part: [t for t in part.replace(",", " ").split() if t not in SUFFIXES]
    if "," in n:
        last, rest = n.split(",", 1)
        return clean(rest) + clean(last), clean(last)
    return clean(n), None


def same_person(row_name: str, master_name: str) -> bool:
    toks, last = name_tokens(row_name)
    mtoks, mlast = name_tokens(master_name)
    mlast = mlast or mtoks[-1:]
    if not toks or not mlast:
        return False
    if last is not None:
        return last == mlast
    return toks[-len(mlast):] == mlast


def resolve_missing_ids(rows: list[dict], master: dict, cfg: dict, stats: Counter) -> None:
    """Many filers (CLF among them) leave the candidate ID and party blank. Match those rows
    to a candidate by last name: first among IDs other filers used in the same race, then in
    the FEC candidate file for the race, then statewide."""
    cycle = str(cfg.get("cycle", 2026))
    in_race = defaultdict(dict)       # district -> {cand_id: name}
    for r in rows:
        if r["cand_id"] and not r["cand_key"].count(":"):
            in_race[r["district"]].setdefault(r["cand_id"], r["cand_name"])
    by_race, by_state = defaultdict(dict), defaultdict(dict)
    for cid, m in master.items():
        if not cid.startswith("H") or m.get("year") != cycle:
            continue
        dis = m.get("district", "").strip()
        dis = dis.zfill(2)[-2:] if dis.isdigit() else "00"
        by_race[m["state"].upper() + dis][cid] = m["name"]
        by_state[m["state"].upper()][cid] = m["name"]

    cache = {}
    for r in rows:
        if ":" not in r["cand_key"]:
            continue
        k = (r["district"], r["cand_name"].upper())
        if k not in cache:
            found = None
            for pool in (in_race.get(r["district"], {}), by_race.get(r["district"], {}),
                         by_state.get(r["district"][:2], {})):
                hits = {cid for cid, nm in pool.items() if same_person(r["cand_name"], nm)}
                if len(hits) == 1:
                    found = hits.pop()
                    break
                if len(hits) > 1:
                    break  # ambiguous; leave it for a party override
            cache[k] = found
        if cache[k]:
            r["cand_key"] = r["cand_id"] = cache[k]
            stats["recovered_candidate_id"] += 1


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


def clean_rows(rows: list[dict], superseded: set[str], stats: Counter, excluded: list[dict]) -> list[dict]:
    """Drop rows from amended-away filings, then exact re-filings of the same transaction."""
    out, seen, dropped = [], set(), []
    for r in sorted(rows, key=lambda r: num(r["file_num"]), reverse=True):
        if r["file_num"] in superseded:
            stats["dropped_amended"] += 1
            dropped.append(r)
            continue
        if r["tran_id"]:
            key = (r["spe_id"] or r["spe_nam"].upper(), r["tran_id"], r["cand_key"],
                   round(r["amount"], 2), r["date"])
            if key in seen:
                stats["dropped_duplicate"] += 1
                continue
            seen.add(key)
        out.append(r)
    # A superseded row whose transaction never reappears in a newer filing is worth surfacing.
    kept = {(r["spe_id"], r["tran_id"], r["cand_key"]) for r in out}
    for r in dropped:
        if r["named"] and (r["spe_id"], r["tran_id"], r["cand_key"]) not in kept:
            excluded.append({"column": r["named"], "reason": "Removed by a later amendment",
                             "spender": r["spe_nam"], "candidate": r["cand_name"], "race": r["district"],
                             "election_type": "G", "amount": r["amount"], "date": r["date"],
                             "file_num": r["file_num"], "tran_id": r["tran_id"]})
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
                     "pats": [re.compile(p, re.I) for p in c.get("name_patterns", [])],
                     "coordinated": bool(c.get("coordinated", False))})
    return cols


def classify(spe_id: str, spe_nam: str, side: str, cols: list[dict]) -> str:
    for c in cols:
        if spe_id in c["ids"] or any(p.search(spe_nam) for p in c["pats"]):
            return c["key"] if c["side"] == side else f"OTH_{side}"
    return f"OTH_{side}"


def build(cfg: dict, rows: list[dict], cands: dict, pres: dict, excluded: list[dict]) -> dict:
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
            if r["named"]:
                excluded.append({"column": r["named"],
                                 "reason": "No usable party or support/oppose",
                                 "spender": r["spe_nam"], "candidate": c["name"], "race": c["district"],
                                 "election_type": "G", "amount": r["amount"], "date": r["date"],
                                 "file_num": r["file_num"], "tran_id": r["tran_id"]})
            continue
        col = classify(r["spe_id"], r["spe_nam"], side, cols)
        d = c["district"]
        race = races.setdefault(d, {"cols": defaultdict(float), "cand_money": defaultdict(float),
                                    "spenders": {}, "new7": defaultdict(float), "last_filed": "",
                                    "coord": defaultdict(float)})
        race["cols"][col] += r["amount"]
        race["cand_money"][c["key"]] += abs(r["amount"])
        if r["filed"] >= week_ago:
            race["new7"][side] += r["amount"]
        race["last_filed"] = max(race["last_filed"], r["filed"])

        sk = f"{r['spe_id'] or r['spe_nam']}|{col}|{r['kind']}"
        sp = race["spenders"].setdefault(sk, {"id": r["spe_id"], "name": r["spe_nam"], "col": col,
                                              "side": side, "amount": 0.0, "targets": Counter(),
                                              "last": "", "n": 0, "kind": r["kind"]})
        if r["kind"] == "COORD":
            race["coord"][col] += r["amount"]
        sp["amount"] += r["amount"]
        sp["targets"][f"{'For' if r['so'] == 'S' else 'Against'} {c['name']}"] += r["amount"]
        sp["last"] = max(sp["last"], r["date"] or r["filed"])
        sp["n"] += 1

        if r["kind"] != "IE":
            continue
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
            ({"id": s["id"], "name": s["name"], "col": s["col"], "side": s["side"], "kind": s["kind"],
              "amount": round(s["amount"], 2), "last": s["last"], "n": s["n"],
              "targets": [t for t, _ in s["targets"].most_common()]}
             for s in race["spenders"].values()),
            key=lambda s: -s["amount"])
        st, dn = d[:2], d[2:]
        out_races.append({
            "district": d, "pres": pres.get(d),
            "rep": nominees.get("REP"), "dem": nominees.get("DEM"),
            "cols": cvals, "coord": {k: round(v, 2) for k, v in race["coord"].items()},
            "r_total": round(r_total, 2), "d_total": round(d_total, 2),
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

    recon = []
    for c in cols:
        counted_ie = sum(r["cols"].get(c["key"], 0) - r["coord"].get(c["key"], 0) for r in out_races)
        counted_coord = sum(r["coord"].get(c["key"], 0) for r in out_races)
        reasons = Counter()
        for e in excluded:
            if e["column"] == c["key"]:
                reasons[e["reason"]] += e["amount"]
        recon.append({"key": c["key"], "label": c["label"], "counted_ie": round(counted_ie, 2),
                      "counted_coordinated": round(counted_coord, 2), "coordinated": c["coordinated"],
                      "not_counted": {k: round(v, 2) for k, v in reasons.most_common()}})

    columns = ([{"key": c["key"], "label": c["label"], "side": "R", "coordinated": c["coordinated"]} for c in cols if c["side"] == "R"]
               + [{"key": "OTH_R", "label": cfg.get("display", {}).get("other_r_label", "OTH R"), "side": "R"}]
               + [{"key": c["key"], "label": c["label"], "side": "D", "coordinated": c["coordinated"]} for c in cols if c["side"] == "D"]
               + [{"key": "OTH_D", "label": cfg.get("display", {}).get("other_d_label", "OTH D"), "side": "D"}])

    return {
        "as_of": as_of, "week_start": week_ago, "columns": columns, "races": out_races,
        "default_view": cfg.get("filters", {}).get("races", "party"),
        "diagnostics": {
            "unassigned": sorted(({"key": k, **v, "amount": round(v["amount"], 2)}
                                  for k, v in unassigned.items()), key=lambda x: -x["amount"]),
            "top_other": top_other,
            "possible_double_counts": over[:15],
            "reconciliation": recon,
        },
    }


# ----------------------------------------------------------------------------- output

def write_outputs(result: dict, rows: list[dict], cands: dict, out: Path, cfg: dict,
                  excluded: list[dict]) -> None:
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
        w.writerow(["type", "district", "candidate", "candidate_party", "support_oppose", "spender_id", "spender",
                    "amount", "date", "filed", "purpose", "payee", "file_num", "tran_id", "image_num"])
        for r in sorted(rows, key=lambda r: (r["filed"], r["date"]), reverse=True):
            c = cands[r["cand_key"]]
            w.writerow([r["kind"], c["district"], c["name"], c["party"], r["so"], r["spe_id"], r["spe_nam"],
                        r["amount"], r["date"], r["filed"], r["purpose"], r["payee"],
                        r["file_num"], r["tran_id"], r["image_num"]])

    with open(out / "not_counted.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["column", "reason", "spender", "candidate", "race", "election_type", "amount", "date",
                    "file_num", "tran_id"])
        for e in sorted(excluded, key=lambda e: (e["column"], e["reason"], e["date"]), reverse=True):
            w.writerow([e[k] for k in ("column", "reason", "spender", "candidate", "race", "election_type",
                                       "amount", "date", "file_num", "tran_id")])

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
    ap.add_argument("--coordinated", help="local pas2YY.zip / itpas2.txt instead of downloading")
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

    cols = compile_columns(cfg)
    rows, parent, stats, excluded = read_ie(ie_path, cfg, cols, master)
    resolve_missing_ids(rows, master, cfg, stats)
    rows = clean_rows(rows, superseded_filings(parent), stats, excluded)

    if any(c["coordinated"] for c in cols):
        coord_path = None
        source_coord = {"url": args.coordinated or "", "last_modified": None}
        if args.coordinated:
            coord_path = Path(args.coordinated)
        elif not args.input and cfg["sources"].get("coordinated_url"):
            co_url = cfg["sources"]["coordinated_url"].format(**fmt)
            coord_path = ROOT / "data" / Path(co_url).name
            try:
                print(f"Downloading {co_url} ...")
                co_res = download(co_url, coord_path)
                source_coord = {"url": co_url, "last_modified": co_res.get("last_modified")}
            except Exception as e:
                print(f"Warning: coordinated file unavailable ({e}); party columns show IEs only.", file=sys.stderr)
                coord_path = None
        if coord_path and coord_path.exists():
            coord_rows = load_coordinated(coord_path, cfg, cols, master, stats, excluded)
            rows += coord_rows
            source_coord["latest_transaction"] = max((r["date"] for r in coord_rows if r["date"]), default="")

    cands, party_notes = resolve_candidates(rows, master, cfg)
    result = build(cfg, rows, cands, load_pres_margins(pres_path), excluded)

    now = dt.datetime.now(ET)
    result.update({
        "title": cfg.get("display", {}).get("title", "House IE Tracker"),
        "generated": now.isoformat(timespec="seconds"),
        "generated_label": now.strftime("%b %-d, %Y at %-I:%M %p ET"),
        "source": source, "note": args.note,
        "source_coordinated": locals().get("source_coord"),
        "stats": dict(stats), "has_pres": bool(load_pres_margins(pres_path)),
    })
    result["diagnostics"]["party_notes"] = party_notes
    write_outputs(result, rows, cands, out, cfg, excluded)

    meta.update({"ie_etag": source.get("etag"), "ie_last_modified": source.get("last_modified"),
                 "build_sig": build_sig, "pres_sig": pres_sig, "generated": result["generated"]})
    meta_path.write_text(json.dumps(meta, indent=2))

    party = [r for r in result["races"] if r["party_race"]]
    print(f"Rows in file: {stats['rows_in_file']:,} | House general: {stats['rows_house_general']:,} | "
          f"counted: {stats['rows_counted']:,} (dropped {stats['dropped_amended']:,} amended, "
          f"{stats['dropped_duplicate']:,} duplicate) | coordinated rows: {stats['rows_coordinated']:,}")
    print(f"Races: {len(party)} with party spending, {len(result['races'])} total. "
          f"Data filed through {result['as_of']}. Wrote {out}/index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
