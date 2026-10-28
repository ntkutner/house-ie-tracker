#!/usr/bin/env python3
"""
House 48-hour contribution notices (FEC Form 6)

Inside the last 20 days before an election, a House campaign that receives a contribution of
$1,000 or more has to report it to the FEC within 48 hours, on a Form 6 "48-hour notice". This
script finds every Form 6 a House campaign files during the general-election window, reads each
one straight from the filing, and builds docs/late.html: late money by race and by candidate,
with every notice and every contribution on it.

    python late.py                      list new notices, read them, rebuild docs/late.html
    python late.py --force              rebuild the page even if nothing new was filed
    python late.py --fixtures DIR       build from local test filings (no network)

Needs the fecfile package (pip install fecfile) and, for listing filings, the same FEC_API_KEY
secret the tracker uses. Parsed notices are cached in cache/f6/, so each run only reads new ones.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import tracker as T

VERSION = "1.0"
ROOT = Path(__file__).resolve().parent
ET = T.ET
UA = f"house-ie-tracker-late/{VERSION}"

# Who gave, from each contribution's entity type. Order is the page's display order.
TYPES = [
    ("IND", "Individuals"),
    ("PAC", "PACs & orgs"),      # PACs, other committees and organizations
    ("PTY", "Party"),
    ("CAN", "Self-funding"),     # the candidate's own money, including loans
]
TYPE_OF = {"IND": "IND", "PAC": "PAC", "COM": "PAC", "CCM": "PAC", "ORG": "PAC", "PTY": "PTY", "CAN": "CAN"}


def election_day(year: int) -> dt.date:
    """Tuesday after the first Monday in November."""
    d = dt.date(year, 11, 1)
    d += dt.timedelta(days=(0 - d.weekday()) % 7)   # first Monday
    return d + dt.timedelta(days=1)


def settings(cfg: dict) -> dict:
    s = cfg.get("late_contributions", {})
    cycle = int(cfg.get("cycle", 2026))
    eday = dt.date.fromisoformat(s["election_date"]) if s.get("election_date") else election_day(cycle)
    # Notices cover money received after the 20th day before the election (the pre-general
    # report's close of books) and more than 48 hours before it.
    start = eday - dt.timedelta(days=19)
    end = eday - dt.timedelta(days=3)
    return {"cycle": cycle, "election_date": eday, "window_start": start, "window_end": end,
            "max_new": int(s.get("max_new_filings_per_run", 3000)),
            "workers": int(s.get("download_threads", 6))}


# ----------------------------------------------------------------------------- listing filings

def api_get(path: str, params: list[tuple[str, str]], api_key: str) -> dict:
    url = f"{T.FEC_API}{path}?" + urllib.parse.urlencode(params + [("api_key", api_key)])
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(10 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < 3:
                time.sleep(10 * (attempt + 1))
                continue
            raise
    return {}


def filing_meta(f: dict, source: str) -> dict | None:
    form = str(f.get("form_type") or "").upper()
    fn = f.get("file_number") or f.get("fec_file_id")
    if not fn or not form.startswith("F6"):
        return None
    amends = f.get("previous_file_number") or f.get("amends_file") or f.get("amendment_of") or ""
    return {
        "file_number": int(fn), "form_type": form, "source": source,
        # The processed list has dates only; the raw e-file list has the time of day too.
        "receipt": re.sub(r"T00:00:00$", "", str(f.get("receipt_date") or "")[:10 if source == "filings" else 19]),
        "committee_id": str(f.get("committee_id") or "").upper(),
        "committee_name": f.get("committee_name") or f.get("filer_name") or "",
        "cand_id": str(f.get("candidate_id") or "").upper(),
        "office": str(f.get("office") or f.get("office_sought") or "").upper()[:1],
        "amendment": form.endswith("A") or str(f.get("amendment_indicator") or "").upper() == "A",
        "amends": str(amends) if amends and str(amends) != str(fn) else "",
    }


def list_processed(api_key: str, since: str, max_pages: int = 200) -> list[dict]:
    """Form 6 filings from OpenFEC's filings list (covers the whole window)."""
    base = [("form_type", "F6"), ("min_receipt_date", since), ("per_page", "100"), ("sort", "-receipt_date")]
    extra = [("committee_type", "H")]
    out = []
    page = 1
    while page <= max_pages:
        try:
            res = api_get("/filings/", base + extra + [("page", str(page))], api_key)
        except urllib.error.HTTPError as e:
            if extra and e.code in (400, 422):
                extra = []          # committee_type filter not accepted; filter House filings ourselves
                continue
            raise
        results = res.get("results", [])
        out += [m for m in (filing_meta(f, "filings") for f in results) if m]
        pages = (res.get("pagination") or {}).get("pages") or 0
        if not results or page >= pages:
            break
        page += 1
    return out


def list_efiled(api_key: str, since: str, max_pages: int = 150) -> list[dict]:
    """Form 6 filings from the raw e-filing list, which shows new filings within minutes."""
    out = []
    extra = [("form_type", "F6")]
    page = 1
    while page <= max_pages:
        params = [("min_receipt_date", since), ("per_page", "100"), ("sort", "-receipt_date"), ("page", str(page))]
        try:
            res = api_get("/efile/filings/", params + extra, api_key)
        except urllib.error.HTTPError as e:
            if extra and e.code in (400, 422):
                extra = []
                continue
            raise
        results = res.get("results", [])
        if not results:
            break
        out += [m for m in (filing_meta(f, "efile") for f in results) if m and m["receipt"][:10] >= since]
        oldest = min((str(f.get("receipt_date") or "")[:10] for f in results), default="")
        if oldest and oldest < since:
            break
        page += 1
    return out


def update_index(index: dict, found: list[dict]) -> int:
    """Merges newly listed filings into the saved index. Returns how many are new."""
    new = 0
    for m in found:
        k = str(m["file_number"])
        old = index["filings"].get(k)
        if old is None:
            index["filings"][k] = m
            new += 1
            continue
        for f in ("cand_id", "office", "amends", "committee_name"):
            if m.get(f) and not old.get(f):
                old[f] = m[f]
        # The raw e-file list carries the time of day; keep it.
        if len(m.get("receipt", "")) > len(old.get("receipt", "")):
            old["receipt"] = m["receipt"]
        old["amendment"] = old.get("amendment") or m.get("amendment")
    return new


# ----------------------------------------------------------------------------- reading filings

def fetch_lines(file_number: int) -> list[bytes]:
    for kind in ("dcdev", "paper"):
        url = f"https://docquery.fec.gov/{kind}/posted/{file_number}.fec"
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0 (house-ie-tracker)"}), timeout=60) as r:
                return r.read().splitlines()
        except urllib.error.HTTPError as e:
            if e.code == 404 and kind == "dcdev":
                continue
            raise
    raise FileNotFoundError(file_number)


def parse_notice(lines, meta: dict) -> dict:
    """A Form 6 as a compact record: who it's for and each contribution on it (no street addresses)."""
    from fecfile import fecparser
    rec = {"file_number": meta["file_number"], "form_type": meta.get("form_type", ""), "items": []}
    for it in fecparser.iter_lines(lines, options={"as_strings": True}):
        d = it.data if isinstance(it.data, dict) else {}
        g = lambda k: str(d.get(k) or "").strip()
        if it.data_type == "summary":
            rec.update({
                "form_type": g("form_type").upper() or rec["form_type"],
                "committee_id": g("filer_committee_id_number").upper(),
                "committee_name": g("committee_name"),
                "cand_id": g("candidate_id_number").upper(),
                "cand_name": ", ".join(x for x in (g("candidate_last_name"), g("candidate_first_name")) if x)
                             or g("candidate_name"),
                "office": g("candidate_office").upper()[:1], "state": g("candidate_state").upper(),
                "district": g("candidate_district"),
                "original_date": T.parse_date(g("original_amendment_date")),
                "signed": T.parse_date(g("date_signed")),
            })
            continue
        if it.data_type != "itemization" or not g("form_type").upper().startswith("F65"):
            continue
        org = g("contributor_organization_name")
        person = " ".join(x for x in (g("contributor_first_name"), g("contributor_middle_name"),
                                       g("contributor_last_name"), g("contributor_suffix")) if x)
        rec["items"].append({
            "tid": g("transaction_id") or g("transaction_id_number"),
            "ent": g("entity_type").upper(),
            "name": org or person or g("contributor_name"),
            "emp": g("contributor_employer"), "occ": g("contributor_occupation"),
            "city": g("contributor_city"), "st": g("contributor_state").upper(),
            "date": T.parse_date(g("contribution_date")),
            "amt": round(T.to_float(d.get("contribution_amount")), 2),
            "fecid": g("contributor_fec_id").upper(),
        })
    return rec


def read_new_notices(index: dict, cache_dir: Path, want: list[str], limit: int, workers: int,
                     fixtures: Path | None) -> tuple[int, list[str]]:
    """Reads filings not yet in the cache, newest first, up to `limit` per run."""
    todo = [k for k in want if not (cache_dir / f"{k}.json").exists()]
    todo.sort(key=lambda k: int(k), reverse=True)
    todo = todo[:limit]
    errors = []

    def one(k: str):
        meta = index["filings"][k]
        if fixtures:
            p = fixtures / f"{k}.fec"
            lines = p.read_bytes().splitlines() if p.exists() else None
            if lines is None:
                raise FileNotFoundError(f"{k}.fec")
        else:
            lines = fetch_lines(int(k))
        return k, parse_notice(lines, meta)

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = [pool.submit(one, k) for k in todo]
        for fut in as_completed(futs):
            try:
                k, rec = fut.result()
            except Exception as e:  # not posted yet, or a network blip: try again next run
                errors.append(str(e)[:160])
                continue
            (cache_dir / f"{k}.json").write_text(json.dumps(rec, separators=(",", ":")))
            done += 1
    return done, errors


# ----------------------------------------------------------------------------- building

def ie_context(path: Path) -> tuple[dict, dict]:
    """From the IE tracker's data.json: each race's nominees and outside-spending totals, and
    which party the tracker counts each nominee with."""
    if not path.exists():
        return {}, {}
    try:
        d = json.loads(path.read_text())
    except Exception:
        return {}, {}
    races, roles = {}, {}
    for r in d.get("races", []):
        rep, dem = r.get("rep") or {}, r.get("dem") or {}
        races[r["district"]] = {"bf": bool(r.get("party_race")), "ie_r": r.get("r_total", 0),
                                "ie_d": r.get("d_total", 0), "rep": rep.get("name", ""), "dem": dem.get("name", ""),
                                "same": r.get("same_party") or ""}
        if rep.get("id"):
            roles[rep["id"].upper()] = "REP"
        if dem.get("id"):
            roles[dem["id"].upper()] = "DEM"
    return races, roles


def is_excluded(cfg: dict, cand_id: str, district: str, name: str) -> bool:
    ex = cfg.get("exclusions", {})
    if district in {r.upper() for r in ex.get("races", [])}:
        return True
    toks = set(T.name_tokens(name)[0])
    for e in (x.strip().upper() for x in ex.get("candidates", [])):
        if e == cand_id:
            return True
        if ":" in e:
            ed, en = e.split(":", 1)
            if ed == district and set(T.name_tokens(en)[0]) <= toks:
                return True
        elif not re.fullmatch(r"[HSP][0-9A-Z]{8}", e):
            want = set(T.name_tokens(e)[0])
            if want and want <= toks:
                return True
    return False


def build(cfg: dict, s: dict, index: dict, cache_dir: Path, master: dict, ie_races: dict, roles: dict,
          now: dt.datetime) -> tuple[dict, list[dict], Counter]:
    stats = Counter()
    pcc_to_cand = {m["pcc"]: cid for cid, m in master.items() if m.get("office") == "H" and m.get("pcc")}
    overrides = {k.upper(): T.norm_party(v) for k, v in cfg.get("party_overrides", {}).items()}
    same_party = {k.upper(): T.norm_party(v.get("party")) for k, v in cfg.get("same_party_races", {}).items()}
    wstart, eday = s["window_start"].isoformat(), s["election_date"].isoformat()

    # 1) Load every House notice we've read.
    recs = {}
    for k, meta in index["filings"].items():
        p = cache_dir / f"{k}.json"
        if not p.exists():
            continue
        rec = json.loads(p.read_text())
        if not str(rec.get("form_type", "")).startswith("F6"):
            stats["not_form_6"] += 1
            continue
        cid = rec.get("cand_id") or meta.get("cand_id") or pcc_to_cand.get(rec.get("committee_id", ""), "")
        office = rec.get("office") or (cid[:1] if cid else "") or meta.get("office", "")
        if office != "H":
            stats["not_house"] += 1
            continue
        if meta.get("receipt") and meta["receipt"][:10] < s["window_start"].isoformat():
            stats["filed_before_window"] += 1
            continue
        rec["cand_id"] = cid.upper()
        rec["receipt"] = meta.get("receipt", "")
        rec["amends"] = meta.get("amends", "")
        rec["amendment"] = bool(meta.get("amendment") or rec.get("original_date") or
                                str(rec.get("form_type", "")).endswith("A"))
        recs[k] = rec
    stats["notices_read"] = len(recs)

    # 2) Amendments: a notice that amends another replaces it. When the filing list doesn't say
    #    which one, it's the filer's earlier notice that shares a transaction ID.
    superseded = {r["amends"] for r in recs.values() if r["amends"]}
    by_tid = defaultdict(set)
    for k, r in recs.items():
        for it in r["items"]:
            if it["tid"]:
                by_tid[(r["committee_id"], it["tid"])].add(int(k))
    for k, r in recs.items():
        if r["amendment"] and not r["amends"]:
            earlier = {x for it in r["items"] if it["tid"] for x in by_tid[(r["committee_id"], it["tid"])]
                       if x < int(k)}
            superseded |= {str(x) for x in earlier}
    superseded &= set(recs)
    stats["notices_amended_away"] = len(superseded)

    # 3) Contributions, newest notice first so a re-reported contribution counts once.
    seen, lines = set(), []
    wlabel = s["window_start"].strftime("%b %-d")
    for k in sorted(recs, key=int, reverse=True):
        r = recs[k]
        if k in superseded:   # still shown on the page, struck through
            lines += [{**it, "file": k, "note": "the notice was replaced by an amendment"} for it in r["items"]]
            continue
        for it in r["items"]:
            key = ((r["committee_id"], it["tid"]) if it["tid"] else
                   (r["committee_id"], it["name"].upper(), it["date"], it["amt"]))
            if key in seen:
                stats["duplicate_lines"] += 1
                continue
            seen.add(key)
            note = ""
            if not it["amt"]:
                note = "zero amount"
            elif it["date"] and it["date"] < wstart:
                note = f"dated before {wlabel}, so it belongs on the pre-general report"
            elif it["date"] and it["date"] > eday:
                note = "dated after election day"
            lines.append({**it, "file": k, "note": note})
    stats["contribution_lines"] = sum(1 for ln in lines if ln["file"] not in superseded)

    # 4) Candidates and races.
    cands: dict[str, dict] = {}
    by_race = defaultdict(list)   # FEC candidate file entries by race, for name matching
    for mid, mm in master.items():
        if mm.get("office") == "H":
            st = mm.get("state", "").upper()
            by_race[st + T.norm_district(st, mm.get("district", ""))].append(mid)

    def name_party(name: str, district: str) -> str:
        """Party for a candidate whose ID didn't resolve: the IE tracker's nominee of that name in
        the race, else a same-named candidate in the race in the FEC candidate file."""
        ie = ie_races.get(district, {})
        for side, party in (("rep", "REP"), ("dem", "DEM")):
            if ie.get(side) and T.same_person(name, ie[side]):
                return party
        hits = {T.norm_party(master[mid].get("party")) for mid in by_race.get(district, [])
                if T.same_person(name, master[mid].get("name", ""))} - {""}
        return hits.pop() if len(hits) == 1 else ""

    for k, r in recs.items():
        state = (r.get("state") or "").upper()
        m = master.get(r["cand_id"], {})
        # Some campaigns put an old or wrong candidate ID on their notices; trust the committee.
        if (not m or m.get("office") != "H" or (state and m.get("state", "").upper() != state)) \
                and pcc_to_cand.get(r.get("committee_id", "")):
            r["cand_id"] = pcc_to_cand[r["committee_id"]]
            m = master[r["cand_id"]]
        cid = r["cand_id"] or f"C:{r['committee_id']}"
        state = state or m.get("state", "").upper()
        district = state + T.norm_district(state, r.get("district", ""), m.get("district", ""))
        if cid not in cands:
            name = T.display_name(m.get("name") or r.get("cand_name") or r.get("committee_name", ""))
            party = (T.find_override(overrides, r["cand_id"], district, [name, r.get("cand_name", "")])
                     or same_party.get(district) or roles.get(r["cand_id"]) or T.norm_party(m.get("party"))
                     or name_party(name, district))
            cands[cid] = {"id": r["cand_id"], "name": name, "party": party, "district": district,
                          "inc": m.get("ici") == "I", "cmte": r["committee_id"],
                          "cmte_name": r.get("committee_name", ""), "files": [],
                          "excluded": is_excluded(cfg, r["cand_id"], district, name)}
        cands[cid]["files"].append(k)
        r["cand_key"] = cid
    for c in cands.values():
        if c["excluded"]:
            stats["excluded_candidates"] += 1

    # Compact page data.
    type_idx = {t: i for i, (t, _) in enumerate(TYPES)}
    cutoff = now - dt.timedelta(hours=48)

    def recent(receipt: str) -> bool:
        if len(receipt) > 10:
            try:
                t = dt.datetime.fromisoformat(receipt)
                t = t.replace(tzinfo=ET) if t.tzinfo is None else t
                return t >= cutoff
            except ValueError:
                pass
        return bool(receipt) and receipt[:10] >= (now.date() - dt.timedelta(days=1)).isoformat()

    keep = sorted((c for c in cands.values() if not c["excluded"]),
                  key=lambda c: (c["district"], c["party"] != "DEM", c["name"]))
    cand_idx = {id(c): i for i, c in enumerate(keep)}
    key_idx = {ck: cand_idx[id(c)] for ck, c in cands.items() if id(c) in cand_idx}
    notices, notice_idx = [], {}
    for k in sorted(recs, key=lambda k: (recs[k]["receipt"], int(k)), reverse=True):
        r = recs[k]
        if r["cand_key"] not in key_idx:
            continue
        notice_idx[k] = len(notices)
        notices.append({"fn": int(k), "c": key_idx[r["cand_key"]], "at": r["receipt"],
                        "amd": 1 if r["amendment"] else 0, "sup": 1 if k in superseded else 0,
                        "n": 0, "amt": 0.0, "cmte": r["committee_id"]})
    items = []
    agg = defaultdict(lambda: {"tot": 0.0, "n": 0, "by": [0.0] * len(TYPES), "recent": 0.0, "files": set(),
                               "last": "", "big": 0.0})
    excluded_lines = []
    for ln in lines:
        if ln["file"] not in notice_idx:
            continue
        ni = notice_idx[ln["file"]]
        nt = notices[ni]
        t = type_idx[TYPE_OF.get(ln["ent"], "IND" if ln["ent"] in ("", "IND") else "PAC")]
        items.append([ni, t, ln["name"], ln["emp"], ln["occ"], ln["city"], ln["st"], ln["date"], ln["amt"],
                      ln["note"]])
        if ln["note"]:
            excluded_lines.append(ln)
            continue
        nt["n"] += 1
        nt["amt"] = round(nt["amt"] + ln["amt"], 2)
        a = agg[nt["c"]]
        a["tot"] += ln["amt"]
        a["n"] += 1
        a["by"][t] += ln["amt"]
        a["big"] = max(a["big"], ln["amt"])
        a["files"].add(ln["file"])
        if recent(nt["at"]):
            a["recent"] += ln["amt"]
    for nt in notices:
        a = agg[nt["c"]]
        if not nt["sup"]:
            a["files"].add(str(nt["fn"]))
        a["last"] = max(a["last"], nt["at"])

    out_cands = []
    for i, c in enumerate(keep):
        a = agg[i]
        out_cands.append({"id": c["id"], "name": c["name"], "p": c["party"] or "OTH", "d": c["district"],
                          "inc": c["inc"], "cmte": c["cmte"], "cmte_name": c["cmte_name"],
                          "tot": round(a["tot"], 2), "n": a["n"], "by": [round(x, 2) for x in a["by"]],
                          "recent": round(a["recent"], 2), "notices": len(a["files"]), "last": a["last"],
                          "big": a["big"]})

    races = defaultdict(lambda: {"cands": [], "D": 0.0, "R": 0.0, "O": 0.0, "recent_D": 0.0, "recent_R": 0.0,
                                 "notices": 0, "last": ""})
    for i, c in enumerate(out_cands):
        r = races[c["d"]]
        r["cands"].append(i)
        side = {"DEM": "D", "REP": "R"}.get(c["p"], "O")
        r[side] += c["tot"]
        if side in "DR":
            r[f"recent_{side}"] += c["recent"]
        r["notices"] += c["notices"]
        r["last"] = max(r["last"], c["last"])
    out_races = []
    for d, r in sorted(races.items()):
        ie = ie_races.get(d, {})
        out_races.append({"d": d, "cands": r["cands"], "D": round(r["D"], 2), "R": round(r["R"], 2),
                          "O": round(r["O"], 2), "rD": round(r["recent_D"], 2), "rR": round(r["recent_R"], 2),
                          "notices": r["notices"], "last": r["last"], "bf": ie.get("bf", False),
                          "same": same_party.get(d, ""), "ie_d": ie.get("ie_d", 0), "ie_r": ie.get("ie_r", 0),
                          "rep": ie.get("rep", ""), "dem": ie.get("dem", "")})

    stats["candidates"] = len(out_cands)
    stats["races"] = len(out_races)
    stats["lines_not_counted"] = sum(1 for ln in excluded_lines if ln["file"] not in superseded)
    data = {
        "election_date": eday, "window_start": wstart, "window_end": s["window_end"].isoformat(),
        "types": [lbl for _, lbl in TYPES], "cands": out_cands, "races": out_races,
        "notices": [[n["fn"], n["c"], n["at"], n["amd"], n["sup"], n["n"], n["amt"], n["cmte"], 1 if recent(n["at"]) else 0]
                    for n in notices],
        "items": items,
    }
    rows = [{**ln, **{"cand": keep[notices[notice_idx[ln["file"]]]["c"]]}} for ln in lines if ln["file"] in notice_idx]
    return data, rows, stats


def write_csvs(out: Path, data: dict, rows: list[dict], recs_meta: dict) -> None:
    labels = dict(TYPES)
    with open(out / "late_contributions.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["district", "candidate", "party", "candidate_id", "committee_id", "file_number", "filed",
                    "contributor", "contributor_type", "employer", "occupation", "city", "state",
                    "contribution_date", "amount", "counted", "note"])
        for r in sorted(rows, key=lambda r: (r["cand"]["district"], r["cand"]["name"], -int(r["file"]))):
            c = r["cand"]
            w.writerow([c["district"], c["name"], c["party"], c["id"], c["cmte"], r["file"],
                        recs_meta.get(r["file"], {}).get("receipt", ""), r["name"],
                        labels[TYPE_OF.get(r["ent"]) or ("IND" if not r["ent"] else "PAC")],
                        r["emp"], r["occ"], r["city"], r["st"], r["date"], r["amt"],
                        "no" if r["note"] else "yes", r["note"]])
    with open(out / "late_notices.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["file_number", "filed", "district", "candidate", "party", "committee_id", "amendment",
                    "replaced_by_amendment", "contributions_counted", "amount_counted", "fec_link"])
        for fn, ci, at, amd, sup, n, amt, cmte, _recent in data["notices"]:
            c = data["cands"][ci]
            w.writerow([fn, at, c["d"], c["name"], c["p"], cmte, "yes" if amd else "", "yes" if sup else "",
                        n, amt, f"https://docquery.fec.gov/cgi-bin/forms/{cmte}/{fn}/"])


# ----------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--out", default=str(ROOT / "docs"))
    ap.add_argument("--cache", default=str(ROOT / "cache" / "f6"))
    ap.add_argument("--candidates", help="local cnYY.zip / cn.txt instead of downloading")
    ap.add_argument("--ie-data", help="the IE tracker's data.json (default: <out>/data.json)")
    ap.add_argument("--fixtures", help="folder with filings.json and <file_number>.fec files (testing)")
    ap.add_argument("--now", help="pretend it's this time, ISO format (testing)")
    ap.add_argument("--force", action="store_true", help="rebuild the page even if nothing changed")
    args = ap.parse_args()

    cfg_text = Path(args.config).read_text()
    cfg = tomllib.loads(cfg_text)
    s = settings(cfg)
    out, cache_dir = Path(args.out), Path(args.cache)
    out.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.fromisoformat(args.now).replace(tzinfo=ET) if args.now else dt.datetime.now(ET)
    fixtures = Path(args.fixtures) if args.fixtures else None
    api_key = os.environ.get("FEC_API_KEY") or "DEMO_KEY"
    errors: list[str] = []

    index_path = cache_dir / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {"filings": {}}
    index.setdefault("filings", {})

    # Candidate file: party, clean names, and which committee belongs to which candidate.
    master: dict = {}
    try:
        if args.candidates:
            master = T.load_candidate_master(Path(args.candidates))
        elif not fixtures and cfg.get("sources", {}).get("candidate_master_url"):
            fmt = {"cycle": s["cycle"], "yy": str(s["cycle"])[-2:]}
            url = cfg["sources"]["candidate_master_url"].format(**fmt)
            path = ROOT / "data" / Path(url).name
            if not path.exists() or time.time() - path.stat().st_mtime > 6 * 3600:
                T.download(url, path)
            master = T.load_candidate_master(path)
    except Exception as e:
        errors.append(f"FEC candidate file unavailable ({e}); using names as filed")
    house_pccs = {m["pcc"] for m in master.values() if m.get("office") == "H" and m.get("pcc")}
    other_pccs = {m["pcc"] for m in master.values() if m.get("office") in ("S", "P") and m.get("pcc")}

    # 1) List Form 6 filings since the window opened (or since the last run, minus a margin).
    new_listed = 0
    if fixtures:
        new_listed = update_index(index, [m for m in (filing_meta(f, "fixture") for f in
                                                      json.loads((fixtures / "filings.json").read_text())) if m])
    elif now.date() >= s["window_start"]:
        since = max(s["window_start"], dt.date.fromisoformat(index.get("listed_through", s["window_start"].isoformat()))
                    - dt.timedelta(days=2)).isoformat()
        # Three lists, in case one lags or leaves something out (OpenFEC's stalled for two days
        # after the FEC's late-October outage); filings are matched by number.
        sources = [("FEC filings list", lambda: list_processed(api_key, since)),
                   ("FEC e-filing list", lambda: list_efiled(api_key, since)),
                   ("FEC filing feed", lambda: [m for m in (filing_meta(f, "rss") for f in T.list_rss_filings())
                                                if m and m["receipt"][:10] >= since])]
        worked = 0
        for label, fn in sources:
            try:
                new_listed += update_index(index, fn())
                worked += 1
            except Exception as e:
                print(f"Warning: couldn't read the {label} ({e})", file=sys.stderr)
        if not worked:
            errors.append("couldn't reach the FEC to list new notices; will retry next hour")
        else:
            index["listed_through"] = now.date().isoformat()
    print(f"Form 6 filings listed: {len(index['filings']):,} ({new_listed:,} new)")

    # 2) Read the House ones we haven't read yet.
    def maybe_house(m: dict) -> bool:
        if m.get("office") in ("S", "P") or m.get("cand_id", "")[:1] in ("S", "P"):
            return False
        if m.get("committee_id") in other_pccs and m.get("committee_id") not in house_pccs:
            return False
        return True
    want = [k for k, m in index["filings"].items() if maybe_house(m)]
    read, read_errors = read_new_notices(index, cache_dir, want, s["max_new"], s["workers"], fixtures)
    pending = sum(1 for k in want if not (cache_dir / f"{k}.json").exists())
    if read_errors:
        errors.append(f"{len(read_errors)} notice(s) couldn't be read yet, will retry: " + "; ".join(read_errors[:3]))
    print(f"Notices read this run: {read:,}; still to read: {pending:,}")
    index_path.write_text(json.dumps(index, separators=(",", ":"), sort_keys=True))

    # 3) Build.
    ie_races, roles = ie_context(Path(args.ie_data) if args.ie_data else out / "data.json")
    data, rows, stats = build(cfg, s, index, cache_dir, master, ie_races, roles, now)
    template = (ROOT / "late_template.html").read_text()
    sig = hashlib.sha1((VERSION + json.dumps(data, sort_keys=True, default=str) + template
                        + Path(__file__).read_text()).encode()).hexdigest()
    status = {"last_checked": now.isoformat(timespec="seconds"),
              "last_checked_label": now.strftime("%b %-d at %-I:%M %p ET"),
              "pending": pending, "errors": errors}
    (out / "late_status.json").write_text(json.dumps(status))
    if sig == index.get("page_sig") and not args.force and (out / "late.html").exists():
        print("No new 48-hour notices. Page unchanged.")
        return 0

    data.update({
        "generated": now.isoformat(timespec="seconds"),
        "generated_label": now.strftime("%b %-d, %Y at %-I:%M %p ET"),
        "stats": dict(stats), "pending": pending, "errors": errors,
        "listed": len(index["filings"]),
    })
    data["election_date"] = s["election_date"].isoformat()
    payload = json.dumps(data, separators=(",", ":"), default=str).replace("</", "<\\/")
    title = cfg.get("late_contributions", {}).get("title", f"{s['cycle']} House 48-Hour Contribution Notices")
    (out / "late.html").write_text(template.replace("/*__DATA__*/null", payload).replace("__TITLE__", title))
    write_csvs(out, data, rows, index["filings"])
    index["page_sig"] = sig
    index_path.write_text(json.dumps(index, separators=(",", ":"), sort_keys=True))
    tot = sum(c["tot"] for c in data["cands"])
    print(f"{stats['candidates']} candidates in {stats['races']} races; {len(data['notices'])} notices; "
          f"${tot:,.0f} counted. Wrote {out}/late.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
