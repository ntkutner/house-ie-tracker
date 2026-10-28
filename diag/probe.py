import json, os, urllib.request, urllib.error, time
K = os.environ.get("FEC_API_KEY") or "DEMO_KEY"
UA = {"User-Agent": "house-ie-tracker-diag"}
def get(url, raw=False):
    t = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
            body = r.read()
            return r.status, dict(r.headers), body if raw else json.loads(body), round(time.time() - t, 1)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()[:300], round(time.time() - t, 1)
    except Exception as e:
        return "ERR", {}, repr(e)[:300], round(time.time() - t, 1)
A = "https://api.open.fec.gov/v1"
def show(label, url, n=8):
    st, h, b, s = get(url + f"&api_key={K}")
    print(f"\n== {label}  [{st}, {s}s]")
    if isinstance(b, dict):
        print("pagination:", b.get("pagination"))
        for f in b.get("results", [])[:n]:
            print(" ", f.get("file_number") or f.get("fec_file_id"), f.get("form_type"), f.get("receipt_date"), f.get("committee_id"), (f.get("committee_name") or "")[:40])
    else:
        print(str(b)[:300])
show("efile latest (any form)", f"{A}/efile/filings/?sort=-receipt_date&per_page=20", 20)
show("efile since Oct 27", f"{A}/efile/filings/?sort=-receipt_date&per_page=20&min_receipt_date=2026-10-27")
show("efile F24 since Oct 26", f"{A}/efile/filings/?sort=-receipt_date&per_page=20&min_receipt_date=2026-10-26&form_type=F24")
show("efile F6 since Oct 26", f"{A}/efile/filings/?sort=-receipt_date&per_page=20&min_receipt_date=2026-10-26&form_type=F6")
show("filings latest", f"{A}/filings/?sort=-receipt_date&per_page=10")
show("filings F24 latest", f"{A}/filings/?sort=-receipt_date&per_page=10&form_type=F24")
show("filings F6 latest", f"{A}/filings/?sort=-receipt_date&per_page=10&form_type=F6")
show("efile sort by file number", f"{A}/efile/filings/?sort=-file_number&per_page=10")
show("sched E efile latest", f"{A}/schedules/schedule_e/efile/?sort=-expenditure_date&per_page=5")
# Which file numbers exist on docquery after the last one the tracker saw (2042325)?
print("\n== docquery probe")
last_ok = None
for fn in list(range(2042326, 2042340)) + list(range(2042400, 2046000, 200)):
    st, h, b, s = get(f"https://docquery.fec.gov/dcdev/posted/{fn}.fec", raw=True)
    head = (b[:160].decode("latin-1", "replace").replace("\x1c", "|").splitlines() if isinstance(b, bytes) else [str(b)[:100]])
    form = head[1][:60] if len(head) > 1 else ""
    print(" ", fn, st, s, form)
    if st == 200:
        last_ok = fn
print("last docquery file found:", last_ok)
for u in ["https://efilingapps.fec.gov/rss/generate?preDefinedFilingType=ALL",
          "https://www.fec.gov/files/bulk-downloads/2026/independent_expenditure_2026.csv"]:
    req = urllib.request.Request(u, headers=UA, method="HEAD" if u.endswith(".csv") else "GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            print("\n==", u, r.status, r.headers.get("Last-Modified"), r.headers.get("Content-Length"))
            if not u.endswith(".csv"):
                txt = r.read().decode("utf-8", "replace")
                import re
                items = re.findall(r"<title>(.*?)</title>.*?<pubDate>(.*?)</pubDate>", txt, re.S)[:12]
                for t, d in items: print("  ", d, "|", t[:90])
    except Exception as e:
        print("\n==", u, "ERR", repr(e)[:200])
