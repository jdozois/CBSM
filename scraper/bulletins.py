"""
Collect Ramsey County "ADS Case Management Bulletin" issues from GovDelivery.

Finds bulletins three ways, so one broken source doesn't stop it:
  1. the GovDelivery topic feed (RSS)
  2. Ramsey County's public bulletin list on GovDelivery
  3. links you paste into scraper/bulletins.txt (always used)

Bulletins never change after they're sent, so each one is downloaded once.
Output: site/data/bulletins.json (index) and site/data/bulletins/<id>.json.
"""
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse

from bs4 import BeautifulSoup, Comment

ACCOUNT = "MNRAMSEY"
TOPIC = "MNRAMSEY_540"  # ADS bulletin topic, from the bulletin's "Subscribe here" link
TITLE_MATCH = re.compile(r"ADS\s+Case\s+Management\s+Bulletin", re.I)
FEED_URL = f"https://public.govdelivery.com/topics/{TOPIC}/feed.rss"
LIST_URL = f"https://content.govdelivery.com/accounts/{ACCOUNT}/bulletins"
BULLETIN_URL = re.compile(rf"https?://content\.govdelivery\.com/accounts/{ACCOUNT}/bulletins/([0-9a-zA-Z]+)")
SEED_FILE = Path(__file__).with_name("bulletins.txt")

ALLOWED = {"p", "br", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "a", "strong", "b", "em",
           "i", "u", "table", "thead", "tbody", "tr", "th", "td", "blockquote", "hr", "sup", "sub",
           "span", "div"}
FOOTER_MARKERS = [r"Did someone forward you this email", r"SUBSCRIBER SERVICES", r"Powered by",
                  r"Update your subscriptions, modify your password"]


def strip_tracking(url):
    try:
        p = urlparse(url)
    except ValueError:
        return url
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    return urlunparse(p._replace(query=urlencode(q)))


def discover(get_text, log):
    """Return {bulletin_id: {"url", "title", "date"}} for ADS bulletins found anywhere."""
    found = {}

    def add(bid, url=None, title="", date=""):
        e = found.setdefault(bid, {"url": url or f"{LIST_URL}/{bid}", "title": "", "date": ""})
        e["title"] = e["title"] or title
        e["date"] = e["date"] or date

    # 1. RSS feed
    xml = get_text(FEED_URL)
    if xml:
        try:
            root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
            n = 0
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                m = BULLETIN_URL.search(link)
                if m and TITLE_MATCH.search(title):
                    add(m.group(1), link, title, (item.findtext("pubDate") or "").strip())
                    n += 1
            log(f"  feed: {n} ADS bulletins")
        except ET.ParseError as e:
            log(f"  feed: couldn't read ({e})")
    else:
        log("  feed: not reachable")

    # 2. Public bulletin list (first page is enough for a daily check)
    html = get_text(LIST_URL)
    if html:
        n = 0
        for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
            m = BULLETIN_URL.search(a["href"]) or re.search(rf"/accounts/{ACCOUNT}/bulletins/([0-9a-zA-Z]+)", a["href"])
            text = a.get_text(" ", strip=True)
            if m and TITLE_MATCH.search(text):
                add(m.group(1), title=text)
                n += 1
        log(f"  bulletin list: {n} ADS bulletins")
    else:
        log("  bulletin list: not reachable")

    # 3. Links pasted into bulletins.txt
    if SEED_FILE.exists():
        n = 0
        for line in SEED_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            m = BULLETIN_URL.search(line)
            if m and not line.startswith("#"):
                add(m.group(1), m.group(0))
                n += 1
        log(f"  bulletins.txt: {n} links")
    return found


def unwrap_layout_tables(root):
    """E-mail layouts nest single-column tables; turn those into plain blocks, keep real data tables."""
    for _ in range(200):
        target = None
        for t in root.find_all("table"):
            nested = t.find("table") is not None
            widest = max((len(tr.find_all(["td", "th"], recursive=False)) for tr in t.find_all("tr")), default=0)
            if nested or widest < 2:
                target = t
                break
        if target is None:
            return
        for cell in target.find_all(["td", "th"]):
            if cell.find_parent("table") is target:
                cell.name = "div"
                cell.attrs = {}
        for tag in target.find_all(["tr", "tbody", "thead", "tfoot"]):
            if tag.find_parent("table") is target:
                tag.unwrap()
        target.name = "div"
        target.attrs = {}


def clean_bulletin(html):
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    if soup.find("h1"):
        title = soup.find("h1").get_text(" ", strip=True)
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)

    sent = ""
    m = re.search(r"sent this bulletin at\s+(\d{1,2}/\d{1,2}/\d{4})", soup.get_text(" "))
    if m:
        sent = datetime.strptime(m.group(1), "%m/%d/%Y").date().isoformat()

    body = (soup.find(id=re.compile("bulletin_body", re.I))
            or soup.find(class_=re.compile("bulletin_body", re.I))
            or soup.body or soup)
    body = BeautifulSoup(str(body), "html.parser")

    for bad in body.find_all(["script", "style", "form", "noscript", "iframe", "img", "input",
                              "button", "select", "textarea", "head", "title", "meta", "link"]):
        bad.decompose()
    for c in body.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()

    # Cut the subscription footer and everything after it
    for marker in FOOTER_MARKERS:
        node = body.find(string=re.compile(marker, re.I))
        if not node:
            continue
        el = node.parent
        # climb only while the wrapper holds little besides the footer text
        while el.parent is not None and len(el.parent.get_text(" ", strip=True)) < 300:
            el = el.parent
        for nxt in list(el.find_all_next()):
            try:
                nxt.decompose()
            except Exception:
                pass
        try:
            el.decompose()
        except Exception:
            pass
    # Drop the header bits already shown by the site
    for s in body.find_all(string=re.compile(r"View as a webpage|sent this bulletin at", re.I)):
        el = s.parent
        while el is not None and el.parent is not None and len(el.parent.get_text(" ", strip=True)) < 120:
            el = el.parent
        if el is not None and len(el.get_text(" ", strip=True)) < 120:
            el.decompose()
        else:
            s.replace_with("")
    h1 = body.find("h1")
    if h1:
        h1.decompose()

    unwrap_layout_tables(body)

    for tag in body.find_all(True):
        if tag.name not in ALLOWED:
            tag.unwrap()
            continue
        keep = {"href"} if tag.name == "a" else ({"colspan", "rowspan"} if tag.name in ("td", "th") else set())
        for attr in list(tag.attrs):
            if attr not in keep:
                del tag[attr]
        if tag.name == "a" and tag.get("href"):
            href = tag["href"].strip()
            if href.startswith("#") or href.lower().startswith("javascript:"):
                tag.unwrap()
            else:
                tag["href"] = strip_tracking(href)
                tag["target"] = "_blank"
                tag["rel"] = "noopener"
    # Remove empty wrappers
    for _ in range(3):
        for tag in body.find_all(["div", "span", "p"]):
            if not tag.get_text(strip=True) and not tag.find(["a", "table", "hr", "br"]):
                tag.decompose()
    out = str(body)
    out = re.sub(r"(\s*<br/?>\s*){3,}", "<br/><br/>", out)
    return title, sent, out.strip()


def text_of(html):
    return re.sub(r"\s+", " ", BeautifulSoup(html, "html.parser").get_text(" ")).strip()


def sync(out, session_get, now, delay=1.0, log=print):
    """
    Update bulletin files. Returns (new_ids, search_entries, index).
    Never raises: a bulletin problem must not break the CBSM sync.
    """
    out = Path(out)
    bdir = out / "bulletins"
    bdir.mkdir(parents=True, exist_ok=True)
    index_path = out / "bulletins.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else []
    have = {b["id"] for b in index}

    def get_text(url):
        try:
            time.sleep(delay)
            r = session_get(url, timeout=45)
            if r.status_code == 200:
                r.encoding = r.apparent_encoding or "utf-8"
                return r.text
            log(f"  {url} -> HTTP {r.status_code}")
        except Exception as e:  # network trouble shouldn't stop anything
            log(f"  {url} -> {e}")
        return None

    new_ids = []
    try:
        log("Checking Ramsey County ADS bulletins…")
        found = discover(get_text, log)
        for bid, info in found.items():
            if bid in have:
                continue
            html = get_text(info["url"])
            if not html:
                continue
            title, sent, body = clean_bulletin(html)
            title = title or info["title"] or f"ADS Case Management Bulletin {bid}"
            if not TITLE_MATCH.search(title):
                log(f"  skipped {bid}: not an ADS bulletin ({title[:60]})")
                continue
            rec = {"id": bid, "title": title, "sent": sent, "url": strip_tracking(info["url"]),
                   "synced": now, "html": body}
            (bdir / f"{bid}.json").write_text(json.dumps(rec, ensure_ascii=False))
            index.append({k: rec[k] for k in ("id", "title", "sent", "url")})
            have.add(bid)
            new_ids.append(bid)
            log(f"  + {sent or '?'}  {title}")
    except Exception as e:
        log(f"  bulletin check stopped early: {e}")

    index.sort(key=lambda b: (b.get("sent") or "", b["id"]), reverse=True)
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=1))

    search = []
    for b in index:
        p = bdir / f"{b['id']}.json"
        if p.exists():
            rec = json.loads(p.read_text())
            search.append({"id": "B-" + b["id"], "t": b["title"],
                           "p": "Ramsey County ADS bulletin" + (f", sent {b['sent']}" if b.get("sent") else ""),
                           "x": text_of(rec["html"])[:30000]})
    return new_ids, search, index
