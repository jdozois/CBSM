"""
DHS forms (eDocs) for the CBSM site.

Builds site/data/forms.json from:
  1. forms_catalog.json  - forms case managers complete, gather or give out, with descriptions
  2. every eDocs link found in mirrored CBSM pages and Ramsey bulletins (title + where it's mentioned)
  3. forms.txt            - extra form numbers you add, one per line

Then checks each form's eDocs link for a new version (file date/size/ETag only; PDFs
aren't downloaded) and reports revisions. Nothing here may break the CBSM sync.
"""
import json
import re
import time
from pathlib import Path
from urllib import robotparser

from bs4 import BeautifulSoup

HERE = Path(__file__).parent
CATALOG = HERE / "forms_catalog.json"
EXTRA = HERE / "forms.txt"
DHS_LIST = HERE / "forms_dhs_list.txt"
EDOCS = "https://edocs.dhs.state.mn.us/lfserver/{area}/{num}-ENG"
EDOCS_LINK = re.compile(r"edocs\.dhs\.state\.mn\.us/lfserver/(?:(Public|Legacy|Secure|public|legacy|secure)/)?(DHS[-_ ]?\d{3,5}[A-Z]{0,2})-ENG", re.I)
FORM_NUM = re.compile(r"\bDHS[-_ ]?(\d{3,5}[A-Z]{0,2})\b", re.I)
UA = "Mozilla/5.0 (compatible; CBSM-mirror/1.2; unofficial daily check of form versions)"


def norm_num(s):
    m = FORM_NUM.search(s.upper().replace("DHS ", "DHS-"))
    return f"DHS-{m.group(1).upper()}" if m else None


def clean_title(text, num):
    t = re.sub(r"\s+", " ", text or "").strip()
    t = re.sub(r"\(PDF\)", "", t, flags=re.I)
    t = re.sub(rf",?\s*{re.escape(num)}\b", "", t, flags=re.I)
    t = re.sub(rf"\b{re.escape(num.replace('DHS-', 'DHS '))}\b", "", t, flags=re.I)
    t = t.strip(" ,-–:")
    if not t or t.lower() in ("pdf", "form", "instructions", "here", "link"):
        return ""
    return t


def collect_links(docs):
    """docs: iterable of (doc_id, doc_title, html). Returns {num: {"title","area","mentions":[...]}}."""
    found = {}
    for did, dtitle, html in docs:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            m = EDOCS_LINK.search(a["href"])
            if not m:
                continue
            num = norm_num(m.group(2))
            if not num:
                continue
            area = (m.group(1) or "Public").capitalize()
            e = found.setdefault(num, {"title": "", "area": area, "mentions": []})
            t = clean_title(a.get_text(" "), num)
            if t and (not e["title"] or len(t) > len(e["title"])):
                e["title"] = t
            if area != "Public" and e["area"] == "Public":
                e["area"] = area
            if not any(x["id"] == did for x in e["mentions"]):
                e["mentions"].append({"id": did, "title": dtitle})
    return found


def extra_numbers():
    if not EXTRA.exists():
        return []
    out = []
    for line in EXTRA.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        num = norm_num(line)
        if num:
            rest = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else ""
            out.append((num, rest.strip(" -–:,")))
    return out


def dhs_list():
    """DHS's frequently-used forms list: [(num, title, url)]."""
    if not DHS_LIST.exists():
        return []
    out = []
    for line in DHS_LIST.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        num = norm_num(parts[0])
        if num:
            out.append((num, parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else ""))
    return out


def signature(headers):
    etag = headers.get("ETag") or headers.get("Etag") or ""
    lm = headers.get("Last-Modified") or ""
    ln = headers.get("Content-Length") or ""
    sig = etag or (lm + "|" + ln if (lm or ln) else "")
    return sig.strip(), lm


def check_versions(session, forms, old_sigs, delay, log):
    """Returns {num: (sig, last_modified)} for forms that could be checked."""
    rp = robotparser.RobotFileParser()
    try:
        r = session.get("https://edocs.dhs.state.mn.us/robots.txt", timeout=30)
        rp.parse(r.text.splitlines() if r.status_code == 200 else [])
    except Exception:
        rp.parse([])
    results = {}
    checked = blocked = 0
    for f in forms:
        if f["area"] == "Secure" or f.get("custom_url"):
            continue  # needs a login, or isn't an eDocs file; link only
        url = f["url"]
        if not rp.can_fetch(UA, url):
            log("  eDocs robots.txt asks crawlers not to check forms; showing links only.")
            return {}
        time.sleep(delay)
        try:
            resp = session.head(url, timeout=30, allow_redirects=True)
            if resp.status_code in (403, 405, 501) or not signature(resp.headers)[0]:
                resp = session.get(url, timeout=30, stream=True, allow_redirects=True)
                resp.close()
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if resp.status_code == 200 and "html" in ctype:
                # an online form or a bot-check page; neither has a stable file version to compare
                page = session.get(url, timeout=30, allow_redirects=True)
                if re.search(r"captcha|radware", page.text[:5000], re.I):
                    blocked += 1
                    if blocked >= 3 and not results:
                        log("  eDocs is showing a bot check to automated requests; showing links only.")
                        return {}
                continue
            if resp.status_code == 200:
                sig, lm = signature(resp.headers)
                if sig:
                    results[f["num"]] = (sig, lm)
            checked += 1
        except Exception as e:
            log(f"  {f['num']}: {e}"[:160])
    log(f"  checked {checked} forms, {len(results)} with version info")
    return results


def sync(out, session, now, page_docs, delay=1.0, log=print, check=True):
    """
    page_docs: list of (id, title, html) from mirrored pages and bulletins.
    Returns (revised_ids, search_entries, forms_list). Never raises.
    """
    out = Path(out)
    path = out / "forms.json"
    old = {f["num"]: f for f in (json.loads(path.read_text()) if path.exists() else [])}
    try:
        log("Building the forms list…")
        catalog = json.loads(CATALOG.read_text(encoding="utf-8")) if CATALOG.exists() else {"forms": []}
        cat_order = {c: i for i, c in enumerate(catalog.get("categories", []))}
        links = collect_links(page_docs)
        forms = {}

        for i, c in enumerate(catalog["forms"]):
            num = norm_num(c["num"])
            if not num:
                continue
            forms[num] = {
                "num": num, "title": c.get("title", ""), "category": c.get("category", "Other"),
                "who": c.get("who", ""), "programs": c.get("programs", ""), "desc": c.get("desc", ""),
                "more": c.get("more", ""), "related": [norm_num(r) for r in c.get("related", []) if norm_num(r)],
                "cm": c.get("cm", True), "area": "Public", "mentions": [], "order": i,
                "custom_url": c.get("url", ""),
            }
        for num, title, url in dhs_list():
            f = forms.setdefault(num, {"num": num, "title": title, "category": "", "who": "", "programs": "",
                                       "desc": "", "more": "", "related": [], "cm": False, "area": "Public",
                                       "mentions": []})
            f["title"] = f["title"] or title
            if url and not f.get("custom_url"):
                m = EDOCS_LINK.search(url)
                if m and m.group(1):
                    f["area"] = m.group(1).capitalize()
                else:
                    f["custom_url"] = url
        for num, e in links.items():
            f = forms.setdefault(num, {"num": num, "title": e["title"], "category": "", "who": "", "programs": "",
                                       "desc": "", "more": "", "related": [], "cm": False, "area": e["area"],
                                       "mentions": []})
            f["title"] = f["title"] or e["title"]
            f["area"] = e["area"] if f["area"] == "Public" else f["area"]
            f["mentions"] = e["mentions"][:12]
        for num, title in extra_numbers():
            f = forms.setdefault(num, {"num": num, "title": title, "category": "", "who": "", "programs": "",
                                       "desc": "", "more": "", "related": [], "cm": False, "area": "Public",
                                       "mentions": []})
            f["title"] = f["title"] or title
            f["extra"] = True

        for f in forms.values():
            f["url"] = f.get("custom_url") or EDOCS.format(area=f["area"], num=f["num"])
            f["title"] = f["title"] or f["num"]
            prev = old.get(f["num"], {})
            f["sig"], f["revised"], f["lastModified"] = prev.get("sig", ""), prev.get("revised", ""), prev.get("lastModified", "")

        ordered = sorted(forms.values(), key=lambda f: (not f["cm"], cat_order.get(f["category"], 99), f.get("order", 9999), f["num"]))

        revised = []
        if check:
            sigs = check_versions(session, ordered, {k: v.get("sig", "") for k, v in old.items()}, delay, log)
            for f in ordered:
                if f["num"] not in sigs:
                    continue
                sig, lm = sigs[f["num"]]
                if f["sig"] and sig != f["sig"]:
                    f["revised"] = now
                    revised.append(f["num"])
                f["sig"], f["lastModified"] = sig, lm
        path.write_text(json.dumps(ordered, ensure_ascii=False, indent=1))
        log(f"  {len(ordered)} forms ({sum(f['cm'] for f in ordered)} for case managers), {len(revised)} revised")
    except Exception as e:
        log(f"  forms step stopped early: {e}")
        ordered, revised = list(old.values()), []

    search = [{"id": "F-" + f["num"], "t": f"{f['num']} {f['title']}",
               "p": "DHS form" + (f", {f['who'].lower()}" if f.get("who") else ""),
               "x": " ".join([f.get("desc", ""), f.get("programs", ""), f.get("category", "")])}
              for f in ordered]
    return revised, search, ordered
