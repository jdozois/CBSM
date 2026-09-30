# CBSM mirror

This is a faster, searchable copy of Minnesota DHS's Community-Based Services Manual (CBSM). A scheduled job re-downloads every page each morning. It publishes the result as a website with full-text search, a table of contents you can collapse, pinned pages, and a "What changed" log.

## What's in here

```
scraper/scrape.py           downloads and cleans every CBSM page into JSON
site/index.html             the website (one file, no build step)
site/data/                  created by the scraper; the site reads from here
.github/workflows/sync.yml  daily sync + deploy to GitHub Pages
tests/test_parse.py         checks the HTML parsing against a sample page
```

## Set it up (about 10 minutes, free)

1. Create a new repository on GitHub. It can be public or private. Pages on private repos requires a paid plan or an organization account.
2. Upload the contents of this folder to it, including the hidden `.github` folder. The easiest way is `git init`, `git add .`, `git commit`, then push. If you upload with drag-and-drop in the browser, make sure the `.github/workflows/sync.yml` file comes along.
3. In the repo, go to **Settings → Pages** and set **Source** to **GitHub Actions**.
4. Go to **Actions → Sync CBSM and deploy → Run workflow**. The first run fetches every page one at a time with a pause between each, so expect roughly 5 to 15 minutes.
5. When it finishes, your site is live at `https://<your-username>.github.io/<repo-name>/`.

After that, it re-syncs every day at about 5 AM Central. You can also press **Run workflow** any time you want it fresh right away.

## Run it on your own computer

```
pip install -r scraper/requirements.txt
python scraper/scrape.py --out site/data
cd site && python -m http.server 8000
```

Then open http://localhost:8000.

## How it behaves

- **Full-text search.** Search covers every page's text, not just titles. Acronyms like CADI, EW, PCA, and EVV are expanded automatically, and matches are highlighted on the page you open.
- **Links.** Links between CBSM pages stay inside the mirror. Links to statutes, bulletins, and forms open the original site.
- **Page info.** Every page shows when DHS last updated it, when it was last synced, and a link to the official page.
- **What changed.** This page lists which pages DHS added, edited, or removed on each day. The log starts after the second sync.
- **Safety checks.** If DHS is down or its page layout changes, the scraper stops without overwriting anything. It refuses to save a run that finds far fewer pages than the last one. The site then keeps showing the previous day's copy, and the failed run appears in the Actions tab with a red ✕.

## Things to know

- **This is unofficial.** DHS's site is the authority on policy. Keep the "Official page" link, and check it before acting on anything time-sensitive. The mirror can be up to a day behind.
- **Be polite to DHS's servers.** The scraper waits one second between requests and runs once a day. Please don't lower the delay or run it every few minutes. It's also worth a quick read of DHS's website terms of use before sharing the site widely.
- **If the scraper breaks.** It depends on DHS's current page structure: the `mainContent` and `divSidebar` areas and the `dDocName` links. If DHS redesigns its site, the scraper will fail safely, and `clean_content()` and `parse_toc()` in `scraper/scrape.py` are the two functions to adjust.
