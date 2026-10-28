import re, urllib.request, json, os, collections
UA = {"User-Agent": "house-ie-tracker-diag"}
def get(u):
    with urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=90) as r:
        return r.read().decode("utf-8", "replace")
base = "https://efilingapps.fec.gov/rss/generate?preDefinedFilingType="
for t in ["ALL", "F24", "F6", "F5", "F3X"]:
    try:
        txt = get(base + t)
    except Exception as e:
        print("==", t, "ERR", e); continue
    items = re.findall(r"<item>(.*?)</item>", txt, re.S)
    print(f"\n== {t}: {len(items)} items, {len(txt)} bytes")
    if t == "ALL":
        print(txt[:600]); print("---- first items raw:")
        for it in items[:3]: print(it[:1200]); print("----")
    dates = [re.search(r"<pubDate>(.*?)</pubDate>", it).group(1) for it in items if "<pubDate>" in it]
    if dates: print("newest", dates[0], "| oldest", dates[-1])
    fns = [int(x) for it in items for x in re.findall(r"/(\d{7})/", it)[:1]]
    if fns: print("file numbers", min(fns), "-", max(fns))
    forms = collections.Counter(re.findall(r"(F\d+[A-Z]?[NAT]?)\b", " ".join(re.findall(r"<description>(.*?)</description>", txt, re.S))))
    print("forms in descriptions:", forms.most_common(12))
# docquery for a few RSS file numbers
txt = get(base + "ALL")
fns = sorted({int(x) for x in re.findall(r"/(\d{7})/", txt)}, reverse=True)[:5]
for fn in fns:
    for kind in ("dcdev/posted", "cgi-bin/forms"):
        try:
            with urllib.request.urlopen(urllib.request.Request(f"https://docquery.fec.gov/{kind}/{fn}.fec" if kind.startswith("dcdev") else f"https://docquery.fec.gov/cgi-bin/forms/x/{fn}/", headers=UA), timeout=60) as r:
                b = r.read(300).decode("latin-1", "replace").replace("\x1c", "|")
                print(fn, kind, r.status, b.splitlines()[1][:80] if kind.startswith("dcdev") and len(b.splitlines()) > 1 else "")
        except Exception as e:
            print(fn, kind, "ERR", str(e)[:80])
