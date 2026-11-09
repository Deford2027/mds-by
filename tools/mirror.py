#!/usr/bin/env python3
"""Mirror a Tilda site into a self-contained static site.

Usage: python3 -I mirror.py <urls.txt> <out_dir> <cache_dir>
"""
import hashlib
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SITE = "https://mds.by"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36"

ASSET_RE = re.compile(
    r"""(?:https?:)?//(?:static|thb|neo|optim|ws)\.tildacdn\.(?:com|biz|info|one|pro)/[^"'\s)<>\\]+"""
    r"""|https://fonts\.googleapis\.com/css2?\?[^"'\s<>]+"""
    r"""|https://fonts\.gstatic\.com/[^"'\s)<>]+"""
)
CSS_URL_RE = re.compile(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""")

urls_txt, OUT, CACHE = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(CACHE, exist_ok=True)


def fetch(url, binary=True):
    key = hashlib.sha1(url.encode()).hexdigest()
    path = os.path.join(CACHE, key)
    if not os.path.exists(path):
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = r.read()
                with open(path, "wb") as f:
                    f.write(data)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 3:
                    print("FAIL", url, e, file=sys.stderr)
                    return None
                time.sleep(2)
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] == b"\x1f\x8b":  # CDN sometimes sends gzip regardless of Accept-Encoding
        import gzip
        data = gzip.decompress(data)
    return data


def asset_local_path(url):
    """Map a remote asset URL to a path under assets/."""
    u = urllib.parse.urlsplit(url if not url.startswith("//") else "https:" + url)
    host = u.netloc.split(".")[0]  # static / thb / fonts ...
    p = urllib.parse.unquote(u.path).lstrip("/")
    if u.netloc.startswith("fonts.googleapis"):
        h = hashlib.sha1(url.encode()).hexdigest()[:10]
        return f"assets/fonts/googlefonts-{h}.css"
    if u.netloc.startswith("fonts.gstatic"):
        return "assets/fonts/" + p
    if p.startswith("ws/project"):  # per-page block bundles
        name = p.split("/", 2)[2]
        return "assets/tilda/" + ("css/" if name.endswith(".css") else "js/") + name
    if p.startswith(("js/", "css/", "lib/")):
        return "assets/tilda/" + p
    # images: tildXXXX-.../-/resize/20x/file.png -> keep unique dir
    p = p.replace("/-/", "/_/")
    return f"assets/img/{host}/{p}"


downloaded = {}  # remote url -> local path


def clean_url(raw):
    u = raw.replace("&amp;", "&")
    if u.startswith("//"):
        u = "https:" + u
    # Tilda adds cache-busting ?t=..; keep for fetching but not for path
    return u


def download_asset(raw):
    url = clean_url(raw)
    if url in downloaded:
        return downloaded[url]
    local = asset_local_path(url.split("?")[0] if "fonts.googleapis" not in url else url)
    data = fetch(url)
    if data is None:
        downloaded[url] = None
        return None
    dest = os.path.join(OUT, local)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if local.endswith(".css"):
        text = data.decode("utf-8", "replace")
        text = rewrite_css(text, url, local)
        data = text.encode()
    elif local.endswith(".js"):
        data = rewrite_js(data.decode("utf-8", "replace")).encode()
    with open(dest, "wb") as f:
        f.write(data)
    downloaded[url] = local
    return local


# Tilda scripts build CDN URLs at runtime: "https://static.tildacdn."+zone()+"/js/x.js".
# Point them at the local assets/tilda/ folder (window.t_localAssetBase, set in every page).
DYN_CDN_RE = re.compile(r"""https://static\.tildacdn\.(["'])\+[\w.]+\(\)\+(["'])""")
DYNAMIC_EXTRAS = [  # files those scripts may request
    "js/tilda-undercut-1.0.min.js", "js/tilda-forms-custommask-1.0.min.js",
    "js/tilda-forms-payments-1.0.min.js", "js/tilda-forms-dict-1.0.min.js",
    "js/tilda-errors-1.0.min.js", "js/tilda-performance-1.0.min.js",
    "js/highlight.min.js", "js/ya-share.js", "css/highlight.min.css",
    "lib/flags/flags7.png", "ffb6456b-781b-40e8-9517-ffb5225e8bcd/imgfish.jpg",
]
CSS_VAR_URL_RE = re.compile(r"""(--[\w-]+\s*:\s*url\(\s*['"]?)([^'")]+)""")
CSS_DIR_FILE = "assets/tilda/css/_.css"  # every stylesheet lives in this folder


DYN_CDN_BASE_RE = re.compile(r'"https://static\.tildacdn\."\+[\w.]+\(\)(?=[;,)])')


def rewrite_js(text):
    text = DYN_CDN_RE.sub(lambda m: m.group(1) + "+window.t_localAssetBase+" + m.group(2), text)
    # bare base URL, e.g. zero-forms: d="https://static.tildacdn."+zone();
    return DYN_CDN_BASE_RE.sub("window.t_localAssetBase", text)


def download_dynamic_extras():
    for p in DYNAMIC_EXTRAS:
        data = fetch("https://static.tildacdn.biz/" + p)
        if data is None:
            continue
        dest = os.path.join(OUT, "assets/tilda", p)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if p.endswith(".js"):
            data = rewrite_js(data.decode("utf-8", "replace")).encode()
        open(dest, "wb").write(data)


def fix_css_var_urls(html, out_file):
    """url() inside a CSS custom property resolves against the stylesheet that
    uses var(), not the page — so make it relative to assets/tilda/css/."""
    def repl(m):
        ref = m.group(2)
        if ref.startswith(("http", "data:", "//", "#")):
            return m.group(0)
        target = os.path.normpath(os.path.join(os.path.dirname(out_file), ref))
        return m.group(1) + rel(CSS_DIR_FILE, target)
    return CSS_VAR_URL_RE.sub(repl, html)


def rel(from_file, to_path):
    return os.path.relpath(to_path, os.path.dirname(from_file) or ".").replace(os.sep, "/")


def rewrite_css(text, base_url, local):
    def repl(m):
        ref = m.group(1).strip()
        if ref.startswith("data:") or ref.startswith("#"):
            return m.group(0)
        absu = urllib.parse.urljoin(base_url, ref)
        if not ASSET_RE.match(absu):
            return m.group(0)
        lp = download_asset(absu)
        return f"url('{rel(local, lp)}')" if lp else m.group(0)

    text = CSS_URL_RE.sub(repl, text)
    # @import of google fonts
    for m in set(ASSET_RE.findall(text)):
        lp = download_asset(m)
        if lp:
            text = text.replace(m, rel(local, lp))
    return text


def page_out_path(url):
    p = urllib.parse.urlsplit(url).path.strip("/")
    return f"{p}/index.html" if p else "index.html"


# --- page-level rewrites --------------------------------------------------

TRACKING_PATTERNS = [
    # Tilda's own stats / fallback CDN loader / dns-prefetch to Tilda
    re.compile(r'<script[^>]*tilda-fallback[^>]*>\s*</script>', re.I),
    re.compile(r'<script[^>]*tilda-stat[^>]*>\s*</script>', re.I),
    re.compile(r'<link rel="dns-prefetch" href="https://(?:ws|static)\.tildacdn\.[a-z]+"\s*/?>', re.I),
]


def rewrite_internal_links(html, out_file, page_paths):
    def to_rel(path):
        path = path.strip("/")
        target = f"{path}/index.html" if path else "index.html"
        r = rel(out_file, target)
        # nicer: point to directory
        return r[: -len("index.html")] or "./"

    def repl(m):
        attr, q, href = m.group(1), m.group(2), m.group(3)
        if href.startswith(SITE):
            href = href[len(SITE):] or "/"
        if not href.startswith("/") or href.startswith("//"):
            return m.group(0)
        path, _, frag = href.partition("#")
        path, _, query = path.partition("?")
        if path.strip("/") in page_paths or path == "/":
            new = to_rel(path)
            if frag:
                new += "#" + frag
            return f"{attr}={q}{new}{q}"
        return m.group(0)

    # only rewrite <a href>; keep canonical / og:url / hreflang absolute for SEO
    html = re.sub(r'(<a\b[^>]*?\shref)=(["\'])([^"\']*)\2', repl, html)
    # form redirect targets ("thanks" pages)
    return re.sub(r'(data-success-url)=(["\'])([^"\']*)\2', repl, html)


def process_page(url, page_paths, out_file=None):
    raw = fetch(url)
    html = raw.decode("utf-8", "replace")
    out_file = out_file or page_out_path(url)

    for p in TRACKING_PATTERNS:
        html = p.sub("", html)

    found = sorted(set(ASSET_RE.findall(html)), key=len, reverse=True)
    with ThreadPoolExecutor(16) as ex:
        list(ex.map(lambda a: fetch(clean_url(a)), found))
    for a in found:
        lp = download_asset(a)
        if lp:
            html = html.replace(a, rel(out_file, lp))

    # local /custom.css
    html = re.sub(r'href="/custom\.css\?t=\d+"', f'href="{rel(out_file, "assets/tilda/css/custom.css")}"', html)
    html = rewrite_internal_links(html, out_file, page_paths)
    html = fix_css_var_urls(html, out_file)
    base = rel(out_file, "assets/tilda")
    html = html.replace("<head>", '<head><script>window.t_localAssetBase=new URL("%s",location.href).href.replace(/\\/$/,"");</script>' % base, 1)

    dest = os.path.join(OUT, out_file)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        f.write(html)
    return out_file, len(found)


def main():
    urls = [u.strip() for u in open(urls_txt) if u.strip()]
    page_paths = {urllib.parse.urlsplit(u).path.strip("/") for u in urls}
    os.makedirs(os.path.join(OUT, "assets"), exist_ok=True)
    css = fetch(SITE + "/custom.css")
    if css:
        text = rewrite_css(css.decode("utf-8", "replace"), SITE + "/custom.css", "assets/tilda/css/custom.css")
        os.makedirs(os.path.join(OUT, "assets/tilda/css"), exist_ok=True)
        open(os.path.join(OUT, "assets/tilda/css/custom.css"), "w").write(text)
    download_dynamic_extras()
    for u in urls:
        out, n = process_page(u, page_paths)
        print(f"{out}: {n} assets")
        if u.rstrip("/").endswith("/error_page"):  # static hosts serve /404.html
            process_page(u, page_paths, out_file="404.html")
            print("404.html: from error_page")
    print("total assets:", sum(1 for v in downloaded.values() if v))


if __name__ == "__main__":
    main()
