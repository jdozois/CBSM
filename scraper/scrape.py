#!/usr/bin/env python3
"""
Mirror Minnesota DHS's Community-Based Services Manual (CBSM) into static JSON.

Output (in --out, default site/data):
  toc.json        nested table of contents
  pages/<ID>.json one file per page: title, path, cleaned HTML, DHS dates
  search.json     compact full-text index input for the site
  meta.json       sync time, page count, content hashes
  changes.json    running log of pages added / changed / removed

Polite by design: one request at a time, a pause between requests,
and a descriptive User-Agent. Run once a day at most.
"""
import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import requests

import bulletins
import forms
from bs4 import BeautifulSoup, Comment

DHS = "https://www.dhs.state.mn.us"
BASE = DHS + "/main/idcplg?IdcService=GET_DYNAMIC_CONVERSION&RevisionSelectionMethod=LatestReleased&dDocName="
START = "id_000402"  # CBSM home page
UA = "Mozilla/5.0 (compatible; CBSM-mirror/1.1; unofficial once-a-day sync of the public CBSM)"

ALLOWED_TAGS = {
    "p", "br", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "a", "strong", "b",
    "em", "i", "u", "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption",
    "blockquote", "hr", "sup", "sub", "dl", "dt", "dd", "span", "div", "pre", "code",
}
KEEP_ATTRS = {"a": {"href", "id", "name"}, "td": {"colspan", "rowspan"}, "th": {"colspan", "rowspan", "scope"}}
JS_LINK = re.compile(r"""javascript:\s*link\(\s*['"]([^'"]+)['"]\s*(?:,\s*['"]([^'"]*)['"])?""", re.I)


def norm_id(doc_id):
    return doc_id.strip().upper()


def raw_doc_id(href):
    """Return the dDocName a DHS link points at, exactly as DHS wrote it, or None."""
    if not href:
        return None
    m = JS_LINK.match(href)
    if m:
        return m.group(1).strip()
    try:
        q = parse_qs(urlparse(href).query)
    except ValueError:
        return None
    for k, v in q.items():
        if k.lower() == "ddocname" and v:
            return v[0].strip()
    return None


# normalized ID -> ID as DHS spells it (used when requesting the page)
SOURCE_ID = {}


def doc_id_from_href(href):
    """Normalized (upper-case) ID for file names and links; remembers DHS's own spelling."""
    raw = raw_doc_id(href)
    if not raw:
        return None
    nid = norm_id(raw)
    SOURCE_ID.setdefault(nid, raw)
    return nid


# ---------------------------------------------------------------- fetching
class Fetcher:
    def __init__(self, delay):
        self.delay = delay
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.8",
        })
        self.last = 0.0
        self.why = {}

    def get(self, doc_id):
        doc_id = SOURCE_ID.get(norm_id(doc_id), doc_id)
        for attempt in range(4):
            wait = self.delay - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            try:
                r = self.s.get(BASE + doc_id, timeout=45)
                if r.status_code == 200:
                    r.encoding = r.apparent_encoding or "utf-8"
                    return r.text
                self.why[norm_id(doc_id)] = f"HTTP {r.status_code} for {doc_id}"
                if r.status_code == 404:
                    return None
            except requests.RequestException as e:
                self.why[norm_id(doc_id)] = f"network error: {e}"[:200]
                print(f"  ! {doc_id}: {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
        return None


# ---------------------------------------------------------------- parsing
def meta(soup, name):
    tag = soup.find("meta", attrs={"name": re.compile("^" + re.escape(name) + "$", re.I)})
    return (tag.get("content") or "").strip() if tag else ""


def is_cbsm(soup):
    vp = meta(soup, "VirtualPath")
    return "/manuals/cbsm/" in vp.lower() if vp else True


def parse_toc(soup):
    """Walk the sidebar's nested lists into [{title, id, children}]."""
    side = soup.find(id=re.compile("divSidebar", re.I)) or soup
    lists = side.find_all("ul")
    if not lists:
        return []
    root = max(lists, key=lambda u: len(u.find_all("a")))
    while root.find_parent("ul") and root.find_parent("ul") in lists:
        root = root.find_parent("ul")

    def walk(ul):
        out = []
        for li in ul.find_all("li", recursive=False):
            a = li.find("a")
            did = doc_id_from_href(a.get("href")) if a else None
            if not did:
                continue
            node = {"title": a.get_text(" ", strip=True), "id": did, "children": []}
            sub = li.find("ul")
            if sub:
                node["children"] = walk(sub)
            out.append(node)
        return out

    return walk(root)


def clean_content(soup, known_ids):
    main = (soup.find(id=re.compile("^mainContent$", re.I))
            or soup.find(attrs={"role": "main"})
            or soup.find("main"))
    if main is None:
        return None, "", []
    main = BeautifulSoup(str(main), "html.parser")

    for bad in main.find_all(["script", "style", "form", "noscript", "iframe", "img", "input", "select", "textarea", "button"]):
        bad.decompose()
    for c in main.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()
    # DHS's "Report this page" widget
    for a in main.find_all("a", href=re.compile("showRate", re.I)):
        (a.find_parent(["p", "div", "strong"]) or a).decompose()
    for t in main.find_all("table"):
        if t.find(string=re.compile(r"Bad/Broken link", re.I)):
            t.decompose()

    title = ""
    h1 = main.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)
        h1.decompose()

    found = []
    for tag in main.find_all(True):
        if tag.name not in ALLOWED_TAGS:
            tag.unwrap()
            continue
        allowed = KEEP_ATTRS.get(tag.name, set())
        for attr in list(tag.attrs):
            if attr not in allowed:
                del tag[attr]
        if tag.name == "a" and tag.get("href"):
            href = tag["href"].strip()
            did = doc_id_from_href(href)
            m = JS_LINK.match(href)
            anchor = m.group(2) if m and m.group(2) else ""
            if did:
                found.append(did)
                tag["href"] = f"#/page/{did}" + (f"/{anchor}" if anchor else "")
                if did not in known_ids:
                    tag["data-pending"] = ""  # may resolve once crawled
            elif href.lower().startswith("javascript:"):
                tag.unwrap()
            elif href.startswith("#"):
                pass
            else:
                tag["href"] = urljoin(DHS + "/", href)
                tag["target"] = "_blank"
                tag["rel"] = "noopener"

    root = main.find(id=re.compile("^mainContent$", re.I)) or main
    html = "".join(str(c) for c in root.contents).strip()
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html, title, found


def text_of(html):
    return re.sub(r"\s+", " ", BeautifulSoup(html, "html.parser").get_text(" ")).strip()


def updated_date(soup):
    m = soup.find(string=re.compile(r"Updated:\s*\d"))
    if m:
        return re.sub(r"^.*?Updated:\s*", "", m.strip())
    return meta(soup, "dc.date.modified")


# ---------------------------------------------------------------- main
def flatten(toc, path=()):
    for n in toc:
        yield n, path
        yield from flatten(n["children"], path + (n["title"],))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/data")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--max-pages", type=int, default=1500)
    ap.add_argument("--follow-links", action="store_true", default=True,
                    help="also mirror CBSM pages linked from content but missing from the sidebar")
    args = ap.parse_args()

    out = Path(args.out)
    (out / "pages").mkdir(parents=True, exist_ok=True)
    old_meta = json.loads((out / "meta.json").read_text()) if (out / "meta.json").exists() else {}
    old_hashes = old_meta.get("hashes", {})

    f = Fetcher(args.delay)
    print("Fetching table of contents…")
    home = f.get(START)
    if not home:
        sys.exit("Could not reach the CBSM home page; leaving existing data untouched.")
    toc = parse_toc(BeautifulSoup(home, "html.parser"))
    if len(list(flatten(toc))) < 20:
        sys.exit("Table of contents looks wrong (too few entries); refusing to overwrite data.")

    paths = {n["id"]: list(p) for n, p in flatten(toc)}
    queue = list(paths)
    known = set(queue)
    pages, hashes, failed, reasons = {}, {}, [], {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    while queue and len(pages) < args.max_pages:
        did = queue.pop(0)
        html_raw = home if did == norm_id(START) else f.get(did)
        if not html_raw:
            failed.append(did)
            reasons[did] = f.why.get(did, "no response")
            continue
        soup = BeautifulSoup(html_raw, "html.parser")
        if not is_cbsm(soup):
            continue
        content, title, links = clean_content(soup, known)
        if content is None:
            failed.append(did)
            t = soup.title.get_text(" ", strip=True) if soup.title else "no <title>"
            reasons[did] = f"no main text found (page title: {t[:80]}; {len(html_raw)} bytes)"
            continue
        title = title or re.sub(r"^CBSM\s*[-–]\s*", "", meta(soup, "dc.title")) or did
        page = {
            "id": did, "title": title, "path": paths.get(did, []),
            "dhsUpdated": updated_date(soup), "synced": now,
            "official": BASE + did, "html": content,
        }
        pages[did] = page
        hashes[did] = hashlib.sha256(content.encode()).hexdigest()[:16]
        (out / "pages" / f"{did}.json").write_text(json.dumps(page, ensure_ascii=False))
        print(f"  {len(pages):4d}  {did}  {title[:60]}")
        if args.follow_links:
            for l in links:
                if l not in known:
                    known.add(l)
                    queue.append(l)

    need = max(int(0.5 * len(paths)), int(0.8 * len(old_hashes)), 20)
    if len(pages) < need:
        print("First failures:", file=sys.stderr)
        for k, v in list(reasons.items())[:15]:
            print(f"  {k}: {v}", file=sys.stderr)
        sys.exit(f"Only {len(pages)} pages fetched (need at least {need}); "
                 "refusing to publish a mostly-empty mirror. See the failures listed above.")

    # Remove files for pages that disappeared
    for p in (out / "pages").glob("*.json"):
        if p.stem not in pages:
            p.unlink()

    added = sorted(set(hashes) - set(old_hashes))
    removed = sorted(set(old_hashes) - set(hashes))
    changed = sorted(k for k in hashes if k in old_hashes and hashes[k] != old_hashes[k])
    # Ramsey County ADS bulletins (never allowed to break the CBSM sync)
    new_b, b_search, b_index = bulletins.sync(out, f.s.get, now, delay=args.delay)
    first_bulletin_run = "bulletins" not in old_meta
    b_titles = {b["id"]: b["title"] for b in b_index}

    # DHS forms from eDocs (links found in pages and bulletins, plus the curated catalog)
    docs = [(k, v["title"], v["html"]) for k, v in pages.items()]
    for b in b_index:
        bp = out / "bulletins" / f"{b['id']}.json"
        if bp.exists():
            docs.append(("B-" + b["id"], b["title"], json.loads(bp.read_text())["html"]))
    revised_forms, f_search, f_list = forms.sync(out, f.s, now, docs, delay=args.delay)
    f_titles = {x["num"]: x["title"] for x in f_list}

    log = json.loads((out / "changes.json").read_text()) if (out / "changes.json").exists() else []
    page_changes = bool(old_hashes) and bool(added or removed or changed)
    new_bulletins = [] if first_bulletin_run else new_b
    if page_changes or new_bulletins or revised_forms:
        title_of = lambda k: pages[k]["title"] if k in pages else k
        old_titles = old_meta.get("titles", {})
        log.insert(0, {
            "date": now,
            "added": ([{"id": k, "title": title_of(k)} for k in added] if page_changes else []) +
                     [{"id": "B-" + b, "title": b_titles.get(b, b)} for b in new_bulletins],
            "changed": ([{"id": k, "title": title_of(k)} for k in changed] if page_changes else []) +
                       [{"id": "F-" + n, "title": f"{n} {f_titles.get(n, '')} (form revised)".replace("  ", " ")}
                        for n in revised_forms],
            "removed": [{"id": k, "title": old_titles.get(k, k)} for k in removed] if page_changes else [],
        })
        log = log[:200]

    search = [{"id": k, "t": v["title"], "p": " › ".join(v["path"]), "x": text_of(v["html"])[:30000]}
              for k, v in pages.items()] + b_search + f_search

    (out / "toc.json").write_text(json.dumps(toc, ensure_ascii=False))
    (out / "search.json").write_text(json.dumps(search, ensure_ascii=False))
    (out / "changes.json").write_text(json.dumps(log, ensure_ascii=False, indent=1))
    (out / "meta.json").write_text(json.dumps({
        "synced": now, "pages": len(pages), "failed": failed, "failed_detail": reasons,
        "hashes": hashes, "titles": {k: v["title"] for k, v in pages.items()},
        "bulletins": len(b_index), "forms": len(f_list),
    }, ensure_ascii=False, indent=1))
    print(f"Done: {len(pages)} pages, {len(added)} added, {len(changed)} changed, "
          f"{len(removed)} removed, {len(failed)} failed; {len(new_b)} new bulletins ({len(b_index)} total).")


if __name__ == "__main__":
    main()
