"""
Screener.in Shareholding & Deals Scraper
=========================================
A Streamlit app that looks up a company (by name / NSE ticker / BSE code),
pulls its Screener.in page (consolidated -> falls back to standalone if the
consolidated view is blank), and shows:

  1. Current shareholding pattern (Promoters / FII / DII / Govt / Public) +
     quarter-on-quarter trend.
  2. Recent Block Deals, Bulk Deals and Insider Trades (from Screener's
     "Trades" modal), with a heuristic FII/DII/Other tag on each
     counterparty and a net-buy/sell summary for the selected window.

Run with:  streamlit run app.py
"""

import os
import re
import time
from datetime import datetime, timedelta

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

BASE = "https://www.screener.in"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
NSE_CSV = os.path.join(DATA_DIR, "NSE_Tickers_List.csv")
BSE_CSV = os.path.join(DATA_DIR, "BSE_Codes_List.csv")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Very rough heuristics for tagging a counterparty as FII / DII / Other.
# Screener doesn't label these directly, so this is a best-effort guess
# based on common name patterns -- always eyeball the raw name too.
FII_HINTS = [
    "mauritius", "singapore", "luxembourg", "ireland", "cayman", "fpi",
    "foreign", "overseas", "ishares", "vanguard", "blackrock", "jpmorgan",
    "jp morgan", "goldman sachs", "morgan stanley", "citigroup", "hsbc",
    "ubs", "credit suisse", "deutsche", "barclays", "state street",
    "fidelity", "capital group", "aia ", "aia-", "abu dhabi", " gic",
    "temasek", "norges", "government pension", "pension fund", "sovereign",
    "bnp paribas", "societe generale", "merrill lynch", "bofa",
    "bank of america", "t rowe price", "t. rowe price", "wellington",
    "nomura", "macquarie", "copthall", "kadensa", "bluepearl",
]
DII_HINTS = [
    "mutual fund", "life insurance", " lic ", "lic ", "sbi mutual",
    "hdfc mutual", "icici prudential", "kotak mutual", "axis mutual",
    "nippon india", "aditya birla sun life", "uti mutual",
    "franklin templeton india", "sundaram mutual", "dsp mutual",
    "tata mutual", "mirae asset", "quant mutual", "edelweiss mutual",
    "invesco india", "motilal oswal mutual", "ppfas", "parag parikh",
    "general insurance", "new india assurance", "national insurance",
    "united india insurance", "oriental insurance", "bajaj allianz",
    "icici lombard", "hdfc life", "sbi life", "max life", "pnb metlife",
    "nps trust",
]

DEAL_TABS = {
    "block": "trades-block-deals",
    "bulk": "trades-bulk-deals",
    "insider": "trades-insider-trades",
}

# --------------------------------------------------------------------------- #
# Ticker / company lookup
# --------------------------------------------------------------------------- #


@st.cache_data(show_spinner=False)
def load_ticker_lists():
    nse = pd.read_csv(NSE_CSV, encoding="utf-8-sig")
    bse = pd.read_csv(BSE_CSV, encoding="utf-8-sig")
    nse.columns = [c.strip() for c in nse.columns]
    bse.columns = [c.strip() for c in bse.columns]
    nse["Name"] = nse["Name"].astype(str).str.strip()
    bse["Name"] = bse["Name"].astype(str).str.strip()
    nse["NSE Ticker"] = nse["NSE Ticker"].astype(str).str.strip()
    bse["BSE Code"] = bse["BSE Code"].astype(str).str.strip()
    return nse, bse


def _norm(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(name).upper())


@st.cache_data(show_spinner=False)
def build_search_index():
    """Outer-merge NSE and BSE lists on a normalised company name so a
    single search box can surface both identifiers when available."""
    nse, bse = load_ticker_lists()
    nse = nse.copy()
    bse = bse.copy()
    nse["_key"] = nse["Name"].map(_norm)
    bse["_key"] = bse["Name"].map(_norm)

    merged = pd.merge(
        nse[["_key", "Name", "NSE Ticker"]],
        bse[["_key", "Name", "BSE Code"]],
        on="_key",
        how="outer",
        suffixes=("_nse", "_bse"),
    )
    merged["Display Name"] = merged["Name_nse"].fillna(merged["Name_bse"])
    merged = merged.drop(columns=["Name_nse", "Name_bse"])
    merged = merged.dropna(subset=["Display Name"]).drop_duplicates()
    return merged.reset_index(drop=True)


def search_companies(query: str, limit: int = 25) -> pd.DataFrame:
    idx = build_search_index()
    if not query:
        return idx.iloc[0:0]
    q = query.strip().upper()
    qn = _norm(query)
    mask = (
        idx["Display Name"].str.upper().str.contains(re.escape(q), na=False)
        | idx["NSE Ticker"].astype(str).str.upper().str.contains(re.escape(q), na=False)
        | idx["BSE Code"].astype(str).str.upper().str.contains(re.escape(q), na=False)
        | idx["_key"].str.contains(re.escape(qn), na=False)
    )
    hits = idx[mask].copy()
    # rank exact / startswith matches first
    hits["_rank"] = hits["Display Name"].str.upper().apply(
        lambda n: 0 if n == q else (1 if n.startswith(q) else 2)
    )
    hits = hits.sort_values(["_rank", "Display Name"]).head(limit)
    return hits.drop(columns=["_rank"])


# --------------------------------------------------------------------------- #
# Screener page fetching
# --------------------------------------------------------------------------- #


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_url(url: str):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        return r.text
    except requests.RequestException:
        return None


def _has_shareholding_data(html: str) -> bool:
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("#quarterly-shp table")
    if not table:
        return False
    rows = table.select("tbody tr")
    return len(rows) > 0


def resolve_company(nse_ticker: str | None, bse_code: str | None):
    """Try consolidated first, then standalone; try NSE id before BSE id.
    Returns dict with html, url, id_used, view_used -- or None if nothing
    worked."""
    candidates = []
    for ident in [nse_ticker, bse_code]:
        if not ident or str(ident).lower() == "nan":
            continue
        candidates.append(ident)

    for view in ["consolidated", ""]:
        for ident in candidates:
            url = f"{BASE}/company/{ident}/" + (f"{view}/" if view else "")
            html = fetch_url(url)
            if html and _has_shareholding_data(html):
                return {"html": html, "url": url, "id_used": ident, "view": view or "standalone"}
    # last resort: return whatever loaded even if shareholding table is empty
    for view in ["consolidated", ""]:
        for ident in candidates:
            url = f"{BASE}/company/{ident}/" + (f"{view}/" if view else "")
            html = fetch_url(url)
            if html:
                return {"html": html, "url": url, "id_used": ident, "view": (view or "standalone") + " (no shareholding data)"}
    return None


def parse_company_name(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.select_one("h1")
    if h1:
        return h1.get_text(strip=True)
    if soup.title:
        return soup.title.get_text(strip=True)
    return "Unknown company"


def parse_trades_url(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    btn = soup.find("button", attrs={"data-url": re.compile(r"/trades/company-\d+/")})
    if btn:
        return btn["data-url"]
    return None


# --------------------------------------------------------------------------- #
# Shareholding pattern parsing
# --------------------------------------------------------------------------- #

ROW_LABELS = {
    "Promoters": "Promoters",
    "FIIs": "FIIs",
    "DIIs": "DIIs",
    "Government": "Government",
    "Public": "Public",
}


def parse_shareholding(html: str) -> pd.DataFrame:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("#quarterly-shp table")
    if not table:
        return pd.DataFrame()

    headers = [th.get_text(strip=True) for th in table.select("thead th")][1:]

    records = {}
    shareholder_counts = None
    for tr in table.select("tbody tr"):
        label_cell = tr.select_one("td.text")
        if label_cell is None:
            continue
        label_text = label_cell.get_text(" ", strip=True)
        cells = tr.find_all("td")[1:]
        values = [c.get_text(strip=True) for c in cells]

        matched = None
        for key in ROW_LABELS:
            if label_text.startswith(key):
                matched = key
                break
        if matched:
            records[matched] = values
        elif "No. of Shareholders" in label_text:
            shareholder_counts = values

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records, index=headers[: len(next(iter(records.values())))]).T
    df.columns = headers[: df.shape[1]]
    if shareholder_counts:
        df.loc["No. of Shareholders"] = shareholder_counts[: df.shape[1]]
    return df


def pct_to_float(x):
    try:
        return float(str(x).replace("%", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Trades (block / bulk / insider) parsing
# --------------------------------------------------------------------------- #


def _clean_number(x: str):
    x = x.replace(",", "").strip()
    try:
        if "." in x:
            return float(x)
        return int(x)
    except ValueError:
        return None


def _parse_date(x: str):
    try:
        return datetime.strptime(x.strip(), "%d %b %Y")
    except (ValueError, AttributeError):
        return None


def parse_block_or_bulk(html: str, tab_id: str) -> pd.DataFrame:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one(f"#{tab_id}")
    if not container:
        return pd.DataFrame()
    table = container.select_one("table")
    if not table:
        return pd.DataFrame()

    rows = []
    current_date = None
    for tr in table.select("tbody tr"):
        strong = tr.select_one("td.text.strong.sub")
        if strong is not None:
            current_date = _parse_date(strong.get_text(strip=True))
            continue
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        person = tds[0].get_text(" ", strip=True)
        action = tds[1].get_text(strip=True)
        qty = _clean_number(tds[2].get_text(strip=True))
        price = _clean_number(tds[3].get_text(strip=True))
        rows.append(
            {
                "Date": current_date,
                "Person / Entity": person,
                "Action": action,
                "Quantity": qty,
                "Price": price,
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.dropna(subset=["Date"])
    return df


def parse_insider(html: str) -> pd.DataFrame:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one(f"#{DEAL_TABS['insider']}")
    if not container:
        return pd.DataFrame()
    table = container.select_one("table")
    if not table:
        return pd.DataFrame()

    rows = []
    current_date = None
    for tr in table.select("tbody tr"):
        strong = tr.select_one("td.text.strong.sub")
        if strong is not None:
            # Insider table dates are "Sep 2025" style (month/year only)
            try:
                current_date = datetime.strptime(strong.get_text(strip=True), "%b %Y")
            except ValueError:
                current_date = None
            continue
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        person_cell = tds[0]
        person = person_cell.get_text(" ", strip=True)
        role_span = person_cell.select_one("span")
        role = role_span.get_text(strip=True) if role_span else ""
        # remaining cells: [maybe qty, avg price, value] -- qty cell may be absent
        rest = [t.get_text(strip=True) for t in tds[1:]]
        qty = _clean_number(rest[0]) if len(rest) >= 3 else (
            _clean_number(rest[0]) if len(rest) >= 1 else None
        )
        avg_price = _clean_number(rest[-2]) if len(rest) >= 2 else None
        value = _clean_number(rest[-1]) if len(rest) >= 1 else None
        direction = "Buy" if "up" in (tds[1].get("class") or []) else (
            "Sell" if "down" in (tds[1].get("class") or []) else ""
        )
        rows.append(
            {
                "Month": current_date,
                "Person": person.replace(role, "").strip(),
                "Role": role,
                "Direction": direction,
                "Quantity": qty,
                "Avg Price": avg_price,
                "Value (Rs. Lacs)": value,
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.dropna(subset=["Month"])
    return df


def classify_investor(name: str) -> str:
    n = (name or "").lower()
    for kw in DII_HINTS:
        if kw in n:
            return "DII"
    for kw in FII_HINTS:
        if kw in n:
            return "FII"
    return "Other / Individual"


def normalise_action(a: str) -> str:
    a = (a or "").strip().upper()
    if a in ("B", "BUY"):
        return "Buy"
    if a in ("SALE", "SELL"):
        return "Sell"
    return a.title() if a else "-"


# --------------------------------------------------------------------------- #
# Streamlit UI
# --------------------------------------------------------------------------- #


def main():
    st.set_page_config(page_title="Screener FII/DII & Block Deal Scraper", layout="wide")
    st.title("📊 Screener.in Shareholding & Deals Scraper")
    st.caption(
        "Looks up a company on screener.in, prefers the consolidated view and falls "
        "back to standalone when consolidated is blank. Pulls the current FII/DII/"
        "Promoter/Public shareholding split plus recent Block Deals, Bulk Deals and "
        "Insider Trades."
    )

    with st.sidebar:
        st.header("Find a company")
        query = st.text_input("Search by name, NSE ticker or BSE code", value="RELIANCE")
        results = search_companies(query) if query else pd.DataFrame()

        selected_row = None
        if not results.empty:
            options = []
            for _, r in results.iterrows():
                nse = r.get("NSE Ticker")
                bse = r.get("BSE Code")
                tag_bits = []
                if pd.notna(nse):
                    tag_bits.append(f"NSE:{nse}")
                if pd.notna(bse):
                    tag_bits.append(f"BSE:{bse}")
                options.append(f"{r['Display Name']}  ({', '.join(tag_bits)})")
            choice = st.selectbox("Matches", options, index=0)
            selected_row = results.iloc[options.index(choice)]
        else:
            st.info("Type a company name, NSE ticker (e.g. RELIANCE) or BSE code (e.g. 500325).")

        st.divider()
        window_days = st.slider("Deals window (days)", 7, 730, 180, step=7)
        st.caption("Applies to Block Deals / Bulk Deals / Insider Trades below.")

    if selected_row is None:
        st.stop()

    nse_ticker = selected_row.get("NSE Ticker")
    bse_code = selected_row.get("BSE Code")
    nse_ticker = None if pd.isna(nse_ticker) else str(nse_ticker)
    bse_code = None if pd.isna(bse_code) else str(int(float(bse_code))) if str(bse_code).replace(".", "").isdigit() else str(bse_code)

    with st.spinner("Fetching from screener.in ..."):
        resolved = resolve_company(nse_ticker, bse_code)

    if resolved is None:
        st.error(
            "Couldn't reach screener.in for this company. It may be blocked from this "
            "network, or the ticker/code doesn't exist on Screener."
        )
        st.stop()

    company_name = parse_company_name(resolved["html"])
    st.subheader(company_name)
    st.caption(
        f"Source: [{resolved['url']}]({resolved['url']})  •  view: **{resolved['view']}**  "
        f"•  identifier used: `{resolved['id_used']}`"
    )

    # --------------------------------------------------------------------------- #
    # Shareholding pattern
    # --------------------------------------------------------------------------- #

    sh_df = parse_shareholding(resolved["html"])

    st.markdown("### Shareholding Pattern")
    if sh_df.empty:
        st.warning("No shareholding pattern table found on this page.")
    else:
        quarters = list(sh_df.columns)
        latest_q = quarters[-1]
        prev_q = quarters[-2] if len(quarters) > 1 else None

        cat_order = ["Promoters", "FIIs", "DIIs", "Government", "Public"]
        cat_order = [c for c in cat_order if c in sh_df.index]

        cols = st.columns(len(cat_order) + 1)
        for i, cat in enumerate(cat_order):
            latest_val = pct_to_float(sh_df.loc[cat, latest_q])
            prev_val = pct_to_float(sh_df.loc[cat, prev_q]) if prev_q else None
            delta = None if (latest_val is None or prev_val is None) else round(latest_val - prev_val, 2)
            cols[i].metric(
                cat,
                f"{latest_val:.2f}%" if latest_val is not None else "-",
                f"{delta:+.2f} pp" if delta is not None else None,
            )
        if "No. of Shareholders" in sh_df.index:
            cols[-1].metric("No. of Shareholders", sh_df.loc["No. of Shareholders", latest_q])

        left, right = st.columns([1, 2])
        with left:
            pie_vals = {c: pct_to_float(sh_df.loc[c, latest_q]) for c in cat_order}
            pie_df = pd.DataFrame({"Category": list(pie_vals.keys()), "Percent": list(pie_vals.values())})
            st.caption(f"Breakup as of {latest_q}")
            st.dataframe(pie_df.set_index("Category"), use_container_width=True)
        with right:
            trend = sh_df.loc[[c for c in cat_order if c in sh_df.index]].T
            trend = trend.apply(lambda col: col.map(pct_to_float))
            st.caption("Quarter-on-quarter trend (%)")
            st.line_chart(trend)

        with st.expander("Full shareholding table"):
            st.dataframe(sh_df, use_container_width=True)

    # --------------------------------------------------------------------------- #
    # Trades (block / bulk / insider)
    # --------------------------------------------------------------------------- #

    st.markdown("### Recent Trades")

    trades_url_path = parse_trades_url(resolved["html"])
    if not trades_url_path:
        st.warning("No Trades link found for this company on screener.in.")
    else:
        full_trades_url = BASE + trades_url_path
        with st.spinner("Fetching trades ..."):
            trades_html = fetch_url(full_trades_url)

        if not trades_html:
            st.error("Could not fetch the Trades page.")
        else:
            cutoff = datetime.now() - timedelta(days=window_days)

            block_df = parse_block_or_bulk(trades_html, DEAL_TABS["block"])
            bulk_df = parse_block_or_bulk(trades_html, DEAL_TABS["bulk"])
            insider_df = parse_insider(trades_html)

            def prep(df):
                if df.empty:
                    return df
                df = df.copy()
                df = df[df["Date"] >= cutoff]
                df["Action"] = df["Action"].map(normalise_action)
                df["Tag"] = df["Person / Entity"].map(classify_investor)
                df = df.sort_values("Date", ascending=False)
                return df

            block_recent = prep(block_df)
            bulk_recent = prep(bulk_df)

            tab1, tab2, tab3, tab4 = st.tabs(
                ["📌 Block Deals", "📦 Bulk Deals", "👤 Insider Trades", "🧮 FII/DII Net Flow"]
            )

            with tab1:
                if block_recent.empty:
                    st.info(f"No block deals in the last {window_days} days.")
                else:
                    show = block_recent.copy()
                    show["Date"] = show["Date"].dt.strftime("%d %b %Y")
                    st.dataframe(
                        show[["Date", "Person / Entity", "Tag", "Action", "Quantity", "Price"]],
                        use_container_width=True,
                        hide_index=True,
                    )

            with tab2:
                if bulk_recent.empty:
                    st.info(f"No bulk deals in the last {window_days} days.")
                else:
                    show = bulk_recent.copy()
                    show["Date"] = show["Date"].dt.strftime("%d %b %Y")
                    st.dataframe(
                        show[["Date", "Person / Entity", "Tag", "Action", "Quantity", "Price"]],
                        use_container_width=True,
                        hide_index=True,
                    )

            with tab3:
                if insider_df.empty:
                    st.info("No insider trades found.")
                else:
                    ins = insider_df[insider_df["Month"] >= cutoff.replace(day=1)].copy()
                    if ins.empty:
                        st.info(f"No insider trades in the last {window_days} days.")
                    else:
                        ins["Month"] = ins["Month"].dt.strftime("%b %Y")
                        ins = ins.sort_values("Month", ascending=False)
                        st.dataframe(
                            ins[["Month", "Person", "Role", "Direction", "Quantity", "Avg Price", "Value (Rs. Lacs)"]],
                            use_container_width=True,
                            hide_index=True,
                        )

            with tab4:
                combined = pd.concat([block_recent, bulk_recent], ignore_index=True) if not (
                    block_recent.empty and bulk_recent.empty
                ) else pd.DataFrame()
                if combined.empty:
                    st.info(f"No block/bulk deals in the last {window_days} days to summarise.")
                else:
                    combined["Signed Qty"] = combined.apply(
                        lambda r: r["Quantity"] if r["Action"] == "Buy" else (-r["Quantity"] if r["Action"] == "Sell" else 0),
                        axis=1,
                    )
                    net = combined.groupby("Tag")["Signed Qty"].sum().sort_values(ascending=False)
                    st.caption(
                        f"Net shares bought (+) / sold (–) via Block + Bulk deals, last {window_days} days. "
                        "Tagging is a heuristic based on entity name — verify manually before relying on it."
                    )
                    st.bar_chart(net)
                    st.dataframe(net.rename("Net Quantity").to_frame(), use_container_width=True)

    st.divider()
    st.caption(
        "Data pulled live from screener.in. This tool is for research convenience only — "
        "always cross-check against exchange filings (NSE/BSE) before making decisions."
    )


if __name__ == "__main__":
    main()
