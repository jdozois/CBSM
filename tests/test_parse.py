import sys; sys.path.insert(0,"scraper")
from bs4 import BeautifulSoup
import scrape

HTML = """<html><head><meta name="VirtualPath" content="/Manuals/CBSM/Waiver_Programs/ID_1"><meta name="dc.title" content="CBSM - CADI"></head>
<body><div id="divSidebar"><ul>
<li><a href="/main/idcplg?IdcService=GET_DYNAMIC_CONVERSION&dDocName=id_000402">Home page</a>
 <ul><li><a href="/main/idcplg?IdcService=X&dDocName=id_000690">A to Z</a></li></ul></li>
<li><a href="/main/idcplg?IdcService=X&dDocName=id_000852">Waiver programs</a>
 <ul><li><a href="/main/idcplg?IdcService=X&dDocName=id_000855">CADI</a>
   <ul><li><a href="/main/idcplg?IdcService=X&dDocName=dhs-1">Deep</a></li></ul></li></ul></li>
</ul></div>
<div id="mainContent"><h1>CADI</h1><p onclick="x()">See <a href="javascript:link('ID_001787','sec2')">customized living</a>
and <a href="/main/idcplg?IdcService=X&dDocName=DHS-999">forms</a> or <a href="https://revisor.mn.gov/x">statute</a>.</p>
<script>bad()</script><table><tr><td colspan="2" style="x">Rate</td></tr></table>
<strong><a href="javascript:showRate()">Report this page</a></strong>
<table><tr><td><select><option>Bad/Broken link</option></select></td></tr></table>
<p>Updated: 3/22/18 1:42 PM</p></div></body></html>"""

soup = BeautifulSoup(HTML, "html.parser")
toc = scrape.parse_toc(soup)
assert [n["id"] for n in toc] == ["ID_000402","ID_000852"], toc
assert toc[1]["children"][0]["children"][0]["id"] == "DHS-1"
html, title, links = scrape.clean_content(soup, {"ID_001787"})
assert title == "CADI"
assert "#/page/ID_001787/sec2" in html and "#/page/DHS-999" in html
assert "https://revisor.mn.gov/x" in html and 'target="_blank"' in html
assert "script" not in html and "onclick" not in html and "style" not in html
assert "Report this page" not in html and "Bad/Broken" not in html
assert 'colspan="2"' in html
assert links == ["ID_001787","DHS-999"]
assert scrape.is_cbsm(soup)
assert scrape.updated_date(soup).startswith("3/22/18")
print("all parser tests pass")
