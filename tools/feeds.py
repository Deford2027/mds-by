#!/usr/bin/env python3
"""Snapshot Tilda feed API responses into static JSON next to each page,
and patch tilda-feed JS to read them.

Usage: python3 -I feeds.py <site_dir> <cache_dir>
"""
import glob, json, math, os, re, sys, time, urllib.parse, urllib.request

TOOLS = os.path.dirname(os.path.abspath(__file__))
SITE_DIR, CACHE = os.path.abspath(sys.argv[1]), sys.argv[2]
sys.argv = ["mirror", "/dev/null", SITE_DIR, CACHE]
sys.path.insert(0, TOOLS)
os.chdir(SITE_DIR)
import mirror  # noqa: E402  (reuses fetch/download/asset mapping)

API = "https://feeds.tildaapi.biz/api/getfeed/"
SITE = "https://mds.by"


def api_get(params, referer):
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(API + "?" + q, headers={
        "User-Agent": mirror.UA, "Referer": referer, "Origin": SITE})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            if data[:2] == b"\x1f\x8b":
                import gzip
                data = gzip.decompress(data)
            return json.loads(data)
        except Exception as e:  # noqa: BLE001
            if attempt == 3:
                raise
            time.sleep(2)


def localize(obj, page_file):
    """Walk JSON: download Tilda CDN assets, make site links relative."""
    if isinstance(obj, dict):
        return {k: localize(v, page_file) for k, v in obj.items()}
    if isinstance(obj, list):
        return [localize(v, page_file) for v in obj]
    if isinstance(obj, str):
        s = obj
        for a in sorted(set(mirror.ASSET_RE.findall(s)), key=len, reverse=True):
            lp = mirror.download_asset(a)
            if lp:
                s = s.replace(a, mirror.rel(page_file, lp))
        def site_rel(m):
            path = m.group(1).strip("/")
            target = f"{path}/index.html" if path else "index.html"
            r = mirror.rel(page_file, target)
            return r[: -len("index.html")] or "./"
        s = re.sub(r"https://mds\.by(/[^\s\"'<>#?]*)?", lambda m: site_rel(m) if m.group(1) is not None else mirror.rel(page_file, "index.html")[:-10] or "./", s)
        return s
    return obj


def feeds_on_page(html):
    for m in re.finditer(r"t_feed_init\('(\d+)',options\)", html):
        seg = html[max(0, m.start() - 6000): m.start()]
        o = seg[seg.rfind("var options"):]
        g = lambda k: (re.search(k + r"\s*:\s*'([^']*)'", o) or [None, ""])[1]
        yield m.group(1), g("feeduid"), g("amountOfPosts"), g("reverse") or "desc"


def main():
    total_files = 0
    for page in sorted(glob.glob("**/index.html", recursive=True)):
        html = open(page, encoding="utf-8").read()
        page_dir = os.path.dirname(page)
        url_path = "/" + page_dir if page_dir else "/"
        for recid, feeduid, size, reverse in feeds_on_page(html):
            slice_ = 1
            while True:
                params = {"feeduid": feeduid, "recid": recid, "c": int(time.time() * 1000),
                          "size": size, "slice": slice_, "sort[date]": reverse, "filters[date]": ""}
                if slice_ == 1:
                    params["getparts"] = "true"
                data = api_get(params, SITE + url_path)
                # navigate to the static post page instead of the API-driven popup
                data["disablepopup"] = True
                data["poststemplatepageid"] = data.get("poststemplatepageid") or 1
                data = localize(data, page)
                out = os.path.join(page_dir, "feeds", f"{recid}-{slice_}.json")
                os.makedirs(os.path.dirname(out), exist_ok=True)
                json.dump(data, open(out, "w", encoding="utf-8"), ensure_ascii=False)
                total_files += 1
                n = len(data.get("posts") or [])
                per = int(data.get("size") or 36)
                tot = int(data.get("total") or n)
                print(f"{page} rec{recid} slice{slice_}: {n} posts (total {tot})")
                if slice_ >= math.ceil(tot / per) or n == 0:
                    break
                slice_ += 1

    # patch the feed runtime to read the local JSON snapshots
    js = "assets/tilda/js/tilda-feed-1.1.min.js"
    t = open(js, encoding="utf-8").read()
    old = 'c="https://"+window.t_feeds_endpoint+"/api/getfeed/";if(!o.isPublishedPage){'
    new = 'c="feeds/"+t+"-"+(_.slice||1)+".json";if(false){'
    if old in t:
        t = t.replace(old, new)
        t = t.replace('v.open("GET",c+"?"+m(_),!0)', 'v.open("GET",c,!0)')
        open(js, "w", encoding="utf-8").write(t)
        print("patched", js)
    elif new in t:
        print("already patched", js)
    else:
        sys.exit("feed JS pattern not found — Tilda changed the script")
    print("feed files:", total_files)


if __name__ == "__main__":
    main()
