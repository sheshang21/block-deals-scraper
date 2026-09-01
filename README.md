# Screener.in FII/DII & Block Deal Scraper (Streamlit)

Looks up any NSE/BSE-listed company on [screener.in](https://www.screener.in),
prefers the **consolidated** view and automatically falls back to
**standalone** when consolidated is blank, then shows:

- Current shareholding split (Promoters / FIIs / DIIs / Government / Public)
  with quarter-on-quarter trend.
- Recent **Block Deals**, **Bulk Deals** and **Insider Trades** (scraped from
  Screener's "Trades" modal), each counterparty heuristically tagged as
  FII / DII / Other, plus a net buy/sell summary for the selected window.

## How it resolves a company

Given a company you pick from the search box, it tries, in order:

1. `https://www.screener.in/company/{NSE_TICKER}/consolidated/`
2. `https://www.screener.in/company/{BSE_CODE}/consolidated/`
3. `https://www.screener.in/company/{NSE_TICKER}/` (standalone)
4. `https://www.screener.in/company/{BSE_CODE}/` (standalone)

...and uses the first one whose shareholding-pattern table actually has
rows (some companies' consolidated page is present but empty).

## Setup

```bash
pip install -r requirements.txt
streamlit run app.py
```

`data/NSE_Tickers_List.csv` and `data/BSE_Codes_List.csv` ship with the app
and power the search box (name / ticker / code). Replace them with a newer
export any time — same two columns (`NSE Ticker,Name` and `BSE Code,Name`).

## Notes / limitations

- **FII/DII tagging is a heuristic** based on keyword matching against the
  counterparty name (foreign custodian/bank names, "Mauritius", "Singapore",
  known Indian mutual fund / insurer names, etc.). Screener doesn't label
  these itself — always eyeball the raw entity name in the table before
  relying on the tag.
- Network access to screener.in is required at runtime; if your environment
  blocks outbound requests you'll see a "Couldn't reach screener.in" error.
- This is for research convenience only, not investment advice — cross-check
  against NSE/BSE exchange filings.
