# Screener.in FII/DII & Block Deal Scraper (Streamlit)

Two modes:

1. **🔎 Single Company** — shareholding split (Promoters / FIIs / DIIs /
   Government / Public) with QoQ trend, plus that company's recent Block &
   Bulk deals.
2. **📡 Bulk Scan – Recent Deals** — scan many companies at once (paste a
   list, upload a watchlist CSV, or run the full bundled NSE/BSE universe)
   and pull only the Block/Bulk deals from the **last N days** (default: 3),
   with a live-updating results table and CSV export.

## Login required for Trades data

Screener.in gates the "Trades" modal (Block Deals / Bulk Deals / Insider
Trades) behind a logged-in account. This app authenticates every request
using a cookie jar exported from a logged-in browser session.

**`cookies.pkl`** goes in the repo root, next to `app.py`. It should unpickle
to a simple dict:

```python
{"csrftoken": "...", "sessionid": "..."}
```

(Selenium-style `[{"name": ..., "value": ...}, ...]` lists and pickled
`requests`/`http.cookiejar` jars are also auto-detected.)

To generate it: log into screener.in in your browser, grab the `csrftoken`
and `sessionid` cookies (DevTools → Application → Cookies), then:

```python
import pickle
cookies = {"csrftoken": "...", "sessionid": "..."}
pickle.dump(cookies, open("cookies.pkl", "wb"))
```

If `cookies.pkl` isn't found, the app still runs (shareholding data doesn't
need login) but warns you that Trades data will likely come back empty. You
can also drop in a cookie file mid-session via the sidebar uploader.

Session cookies expire — if the app starts reporting `login_required` on
most companies during a scan, re-export a fresh `cookies.pkl`.

## How company resolution works

Given a company, single-company mode tries, in order, and uses the first
one whose shareholding table actually has rows (some companies' consolidated
page exists but is blank):

1. `https://www.screener.in/company/{NSE_TICKER}/consolidated/`
2. `https://www.screener.in/company/{BSE_CODE}/consolidated/`
3. `https://www.screener.in/company/{NSE_TICKER}/` (standalone)
4. `https://www.screener.in/company/{BSE_CODE}/` (standalone)

Bulk-scan mode skips the shareholding check (it only needs the Trades link)
and just uses the first page that loads, to save a request per company.

## Bulk scan notes

- Each company costs **2 page fetches** (its screener page, to find the
  internal Trades URL, then the Trades page itself) — there's no way around
  this since the Trades URL embeds an internal Screener ID not present in
  the NSE/BSE lists.
- Scanning the **full universe** (~3,000 NSE / ~4,900 BSE names) means
  6,000–10,000 requests. Use the "Max companies to scan" cap, a modest
  worker count (default 6), and the polite per-request delay (default
  150 ms) — hammering screener.in risks rate-limiting or getting your
  session logged out mid-scan.
- The results table streams in live as companies finish, and only shows
  companies that actually had a Block/Bulk deal in your chosen window.
- A per-company status log (`ok_deals_found`, `ok_no_recent_deals`,
  `no_trades_link`, `login_required`, `fetch_failed`, ...) is available in
  an expander after the scan, so you can see what got skipped and why.

## Setup

```bash
pip install -r requirements.txt
streamlit run app.py
```

`data/NSE_Tickers_List.csv` and `data/BSE_Codes_List.csv` ship with the app
and power both the single-company search and the "Full NSE/BSE universe"
bulk-scan option. Replace them with a newer export any time — same two
columns (`NSE Ticker,Name` and `BSE Code,Name`).

## Notes / limitations

- **FII/DII tagging is a heuristic** based on keyword matching against the
  counterparty name (foreign custodian/bank names, "Mauritius", "Singapore",
  known Indian mutual fund / insurer names, etc.). Screener doesn't label
  these itself — always eyeball the raw entity name before relying on the
  tag.
- Research convenience only, not investment advice — cross-check against
  NSE/BSE exchange filings before acting on anything shown here.
