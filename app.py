"""
Screener.in Shareholding & Deals Scraper
=========================================
Two modes:

  1. Single Company  - shareholding split (FII/DII/Promoter/Public) + trend,
     and that company's Block/Bulk/Insider deals.
  2. Bulk Scan       - scan many companies at once (paste a list, upload a
     watchlist CSV, or use the full bundled NSE/BSE universe) and pull only
     the Block/Bulk deals that happened in the last N days (default: 3).

Screener.in gates the "Trades" modal behind login, so this app authenticates
using a cookie jar exported from a logged-in browser session
(`cookies.pkl` in the repo root - see README).

Run with:  streamlit run app.py
"""

import os
import pickle
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

BASE = "https://www.screener.in"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = APP_DIR
NSE_CSV = os.path.join(DATA_DIR, "NSE_Tickers_List.csv")
BSE_CSV = os.path.join(DATA_DIR, "BSE_Codes_List.csv")

# Any of these (checked in order) will be used as the cookie jar.
COOKIE_CANDIDATES = [
    os.path.join(APP_DIR, "cookies.pkl"),
    os.path.join(APP_DIR, "screener_cookies.pkl"),
    os.path.join(APP_DIR, "data", "cookies.pkl"),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE + "/",
}

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
# Auth / session
# --------------------------------------------------------------------------- #


def _cookie_obj_to_dict(obj) -> dict:
    """Normalise whatever shape the pickle is in (plain dict, Selenium-style
    list of {"name","value"} dicts, or a requests/http.cookiejar jar) into a
    simple {name: value} dict."""
    if isinstance(obj, dict):
        return {str(k): str(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        out = {}
        for item in obj:
            if isinstance(item, dict) and "name" in item and "value" in item:
                out[item["name"]] = item["value"]
        return out
    # requests.cookies.RequestsCookieJar / http.cookiejar.CookieJar
    if hasattr(obj, "get_dict"):
        return obj.get_dict()
    try:
        return dict(obj)
    except (TypeError, ValueError):
        return {}


def find_cookie_file() -> str | None:
    for path in COOKIE_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


@st.cache_resource(show_spinner=False)
def build_session(cookie_path: str | None, _bust: float = 0.0):
    """One shared authenticated requests.Session. `_bust` lets us force a
    fresh session after the user uploads a new cookie file mid-run."""
    session = requests.Session()
    session.headers.update(HEADERS)
    # Cloud egress to screener.in can be flaky (transient connection-refused /
    # reset), so retry a few times with backoff before giving up, instead of
    # failing the whole page load on one bad attempt.
    retry = Retry(
        total=3,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    cookie_dict = {}
    if cookie_path and os.path.exists(cookie_path):
        try:
            with open(cookie_path, "rb") as f:
                cookie_dict = _cookie_obj_to_dict(pickle.load(f))
            for name, value in cookie_dict.items():
                # single domain only - setting the same cookie under both
                # "www.screener.in" and ".screener.in" creates a duplicate
                # that raises CookieConflictError the moment anything reads
                # session.cookies as a plain dict (dict(), .get_dict(), etc).
                session.cookies.set(name, value, domain=".screener.in")
        except Exception:
            cookie_dict = {}
    return session, bool(cookie_dict)


def check_login(session: requests.Session) -> tuple[bool, str]:
    try:
        r = session.get(BASE + "/", timeout=15)
    except requests.RequestException as e:
        return False, f"Could not reach screener.in ({e})"
    if r.status_code != 200:
        return False, f"Homepage returned HTTP {r.status_code}"
    html = r.text
    if re.search(r'href="/logout/"', html) or "Logout" in html:
        return True, "Logged in"
    if re.search(r'href="/login/\?[^"]*"', html) or re.search(r"Log ?in", html):
        return False, "Cookies look expired / not logged in (login link visible)"
    return False, "Couldn't confirm login state - proceeding cautiously"


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
    hits["_rank"] = hits["Display Name"].str.upper().apply(
        lambda n: 0 if n == q else (1 if n.startswith(q) else 2)
    )
    hits = hits.sort_values(["_rank", "Display Name"]).head(limit)
    return hits.drop(columns=["_rank"])


def resolve_identifier_line(line: str, idx: pd.DataFrame):
    """For bulk paste/CSV input: turn a free-text line (name, NSE ticker or
    BSE code) into (display_name, nse_ticker, bse_code) or None."""
    raw = line.strip()
    if not raw:
        return None
    key = _norm(raw)
    exact = idx[idx["_key"] == key]
    if not exact.empty:
        row = exact.iloc[0]
        return row["Display Name"], row.get("NSE Ticker"), row.get("BSE Code")
    exact_ticker = idx[idx["NSE Ticker"].astype(str).str.upper() == raw.upper()]
    if not exact_ticker.empty:
        row = exact_ticker.iloc[0]
        return row["Display Name"], row.get("NSE Ticker"), row.get("BSE Code")
    exact_bse = idx[idx["BSE Code"].astype(str) == raw]
    if not exact_bse.empty:
        row = exact_bse.iloc[0]
        return row["Display Name"], row.get("NSE Ticker"), row.get("BSE Code")
    # fall back: treat the raw text itself as an identifier screener might
    # accept directly (covers tickers/codes not present in our CSVs)
    return raw, raw, None


# --------------------------------------------------------------------------- #
# Screener page fetching
# --------------------------------------------------------------------------- #


def fetch_url(session: requests.Session, url: str, delay: float = 0.0):
    if delay:
        time.sleep(delay)
    try:
        r = session.get(url, timeout=15)
        if r.status_code != 200:
            return None
        return r.text
    except requests.RequestException:
        return None


def _has_shareholding_data(html: str, period: str = "quarterly") -> bool:
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one(f"#{period}-shp table")
    if not table:
        return False
    return len(table.select("tbody tr")) > 0


def resolve_company_full(session, nse_ticker, bse_code):
    """Single-company mode: try consolidated -> standalone, NSE -> BSE,
    preferring the first with actual shareholding data."""
    candidates = [c for c in [nse_ticker, bse_code] if c and str(c).lower() != "nan"]
    for view in ["consolidated", ""]:
        for ident in candidates:
            url = f"{BASE}/company/{ident}/" + (f"{view}/" if view else "")
            html = fetch_url(session, url)
            if html and _has_shareholding_data(html):
                return {"html": html, "url": url, "id_used": ident, "view": view or "standalone"}
    for view in ["consolidated", ""]:
        for ident in candidates:
            url = f"{BASE}/company/{ident}/" + (f"{view}/" if view else "")
            html = fetch_url(session, url)
            if html:
                return {"html": html, "url": url, "id_used": ident,
                        "view": (view or "standalone") + " (no shareholding data)"}
    return None


def resolve_company_light(session, nse_ticker, bse_code, delay: float = 0.0):
    """Bulk-scan mode: just need ONE page that loads and exposes the Trades
    link - skip the shareholding-emptiness check to save a request."""
    candidates = [c for c in [nse_ticker, bse_code] if c and str(c).lower() != "nan"]
    for ident in candidates:
        url = f"{BASE}/company/{ident}/consolidated/"
        html = fetch_url(session, url, delay=delay)
        if html:
            return html, url
    for ident in candidates:
        url = f"{BASE}/company/{ident}/"
        html = fetch_url(session, url, delay=delay)
        if html:
            return html, url
    return None, None


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

ROW_LABELS = {"Promoters": "Promoters", "FIIs": "FIIs", "DIIs": "DIIs",
              "Government": "Government", "Public": "Public"}


def parse_shareholding(html: str, period: str = "quarterly") -> pd.DataFrame:
    """period: 'quarterly' or 'yearly' - both tables ship in the same page
    load (screener toggles them client-side with a `hidden` class), so no
    extra request is needed to switch views."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one(f"#{period}-shp table")
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
        return float(x) if "." in x else int(x)
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
        rows.append({
            "Date": current_date,
            "Person / Entity": tds[0].get_text(" ", strip=True),
            "Action": tds[1].get_text(strip=True),
            "Quantity": _clean_number(tds[2].get_text(strip=True)),
            "Price": _clean_number(tds[3].get_text(strip=True)),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.dropna(subset=["Date"])
    return df


def looks_login_gated(html: str) -> bool:
    """Heuristic: trades page loaded but the tab containers are missing
    entirely, which is what we'd expect if the Trades modal content itself
    requires auth and we're not authenticated."""
    if not html:
        return True
    soup = BeautifulSoup(html, "html.parser")
    return not any(soup.select_one(f"#{tid}") for tid in DEAL_TABS.values())


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
# Bulk scan engine
# --------------------------------------------------------------------------- #


def scan_one_company(session, name, nse_ticker, bse_code, cutoff, delay):
    """Runs in a worker thread. Returns a dict describing what happened."""
    html, url = resolve_company_light(session, nse_ticker, bse_code, delay=delay)
    if not html:
        return {"name": name, "status": "fetch_failed", "deals": pd.DataFrame(), "url": url}

    trades_path = parse_trades_url(html)
    if not trades_path:
        return {"name": name, "status": "no_trades_link", "deals": pd.DataFrame(), "url": url}

    trades_html = fetch_url(session, BASE + trades_path, delay=delay)
    if not trades_html:
        return {"name": name, "status": "trades_fetch_failed", "deals": pd.DataFrame(), "url": url}

    if looks_login_gated(trades_html):
        return {"name": name, "status": "login_required", "deals": pd.DataFrame(), "url": url}

    block_df = parse_block_or_bulk(trades_html, DEAL_TABS["block"])
    bulk_df = parse_block_or_bulk(trades_html, DEAL_TABS["bulk"])
    for df, kind in ((block_df, "Block"), (bulk_df, "Bulk")):
        if not df.empty:
            df["Deal Type"] = kind

    combined = pd.concat([block_df, bulk_df], ignore_index=True) if not (block_df.empty and bulk_df.empty) else pd.DataFrame()
    if combined.empty:
        return {"name": name, "status": "ok_no_deals", "deals": pd.DataFrame(), "url": url}

    recent = combined[combined["Date"] >= cutoff].copy()
    if recent.empty:
        return {"name": name, "status": "ok_no_recent_deals", "deals": pd.DataFrame(), "url": url}

    recent["Company"] = name
    recent["Action"] = recent["Action"].map(normalise_action)
    recent["Tag"] = recent["Person / Entity"].map(classify_investor)
    return {"name": name, "status": "ok_deals_found", "deals": recent, "url": url}


def run_bulk_scan(session, companies, window_days, max_workers, delay, progress_cb=None):
    """companies: list of (name, nse_ticker, bse_code). Returns
    (all_deals_df, status_counts dict, per_company_log list)."""
    cutoff = datetime.now() - timedelta(days=window_days)
    all_deals = []
    status_counts = {}
    log = []
    total = len(companies)
    done = 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(scan_one_company, session, name, nse, bse, cutoff, delay): name
            for name, nse, bse in companies
        }
        for fut in as_completed(futures):
            done += 1
            try:
                result = fut.result()
            except Exception as e:
                result = {"name": futures[fut], "status": f"error: {e}", "deals": pd.DataFrame(), "url": None}
            status_counts[result["status"]] = status_counts.get(result["status"], 0) + 1
            log.append({"Company": result["name"], "Status": result["status"], "URL": result.get("url")})
            if not result["deals"].empty:
                all_deals.append(result["deals"])
            if progress_cb:
                progress_cb(done, total, result)

    deals_df = pd.concat(all_deals, ignore_index=True) if all_deals else pd.DataFrame()
    return deals_df, status_counts, log


# --------------------------------------------------------------------------- #
# Streamlit UI
# --------------------------------------------------------------------------- #


def render_login_banner(session, cookie_path, has_cookies):
    if not has_cookies:
        st.error(
            "No `cookies.pkl` found in the app folder. Screener.in's Trades modal "
            "needs a logged-in session, so Block/Bulk deal data will likely come "
            "back empty. Export cookies (`csrftoken` + `sessionid`) from a logged-in "
            "browser session and save them as `cookies.pkl` next to `app.py`."
        )
        return
    with st.spinner("Checking login status..."):
        ok, msg = check_login(session)
    if ok:
        st.success(f"🔓 {msg} (`{os.path.basename(cookie_path)}`)")
    else:
        st.warning(f"⚠️ {msg}. Deals data may come back empty — re-export `cookies.pkl` if so.")


def single_company_mode(session, idx):
    query = st.text_input("Search by name, NSE ticker or BSE code", value="RELIANCE")
    results = search_companies(query) if query else pd.DataFrame()
    if results.empty:
        st.info("Type a company name, NSE ticker (e.g. RELIANCE) or BSE code (e.g. 500325).")
        return

    options = []
    for _, r in results.iterrows():
        bits = []
        if pd.notna(r.get("NSE Ticker")):
            bits.append(f"NSE:{r['NSE Ticker']}")
        if pd.notna(r.get("BSE Code")):
            bits.append(f"BSE:{r['BSE Code']}")
        options.append(f"{r['Display Name']}  ({', '.join(bits)})")
    choice = st.selectbox("Matches", options, index=0)
    row = results.iloc[options.index(choice)]

    window_days = st.slider("Deals window (days)", 1, 730, 3, step=1, key="single_window")

    nse_ticker = None if pd.isna(row.get("NSE Ticker")) else str(row["NSE Ticker"])
    bse_code = row.get("BSE Code")
    bse_code = None if pd.isna(bse_code) else (
        str(int(float(bse_code))) if str(bse_code).replace(".", "").isdigit() else str(bse_code)
    )

    with st.spinner("Fetching from screener.in ..."):
        resolved = resolve_company_full(session, nse_ticker, bse_code)
    if resolved is None:
        st.error("Couldn't reach screener.in for this company.")
        return

    company_name = parse_company_name(resolved["html"])
    st.subheader(company_name)
    st.caption(
        f"Source: [{resolved['url']}]({resolved['url']})  •  view: **{resolved['view']}**  "
        f"•  identifier used: `{resolved['id_used']}`"
    )

    sh_header_col, sh_toggle_col = st.columns([3, 1])
    sh_header_col.markdown("### Shareholding Pattern")
    period_choice = sh_toggle_col.radio(
        "Period", ["Quarterly", "Yearly"], horizontal=True,
        label_visibility="collapsed", key="shp_period",
    )
    period = "quarterly" if period_choice == "Quarterly" else "yearly"

    # Both the quarterly and yearly tables ship in the same page load
    # (screener just toggles visibility client-side), so switching the
    # radio re-parses already-fetched HTML - no extra network round trip.
    sh_df = parse_shareholding(resolved["html"], period=period)
    if sh_df.empty:
        st.warning(f"No {period_choice.lower()} shareholding pattern table found on this page.")
    else:
        periods = list(sh_df.columns)
        latest_p, prev_p = periods[-1], (periods[-2] if len(periods) > 1 else None)
        cat_order = [c for c in ["Promoters", "FIIs", "DIIs", "Government", "Public"] if c in sh_df.index]
        cols = st.columns(len(cat_order) + 1)
        for i, cat in enumerate(cat_order):
            latest_val = pct_to_float(sh_df.loc[cat, latest_p])
            prev_val = pct_to_float(sh_df.loc[cat, prev_p]) if prev_p else None
            delta = None if (latest_val is None or prev_val is None) else round(latest_val - prev_val, 2)
            cols[i].metric(cat, f"{latest_val:.2f}%" if latest_val is not None else "-",
                            f"{delta:+.2f} pp" if delta is not None else None)
        if "No. of Shareholders" in sh_df.index:
            cols[-1].metric("No. of Shareholders", sh_df.loc["No. of Shareholders", latest_p])

        trend = sh_df.loc[cat_order].T.apply(lambda col: col.map(pct_to_float))
        st.caption(f"{period_choice} trend (%)")
        st.line_chart(trend)
        with st.expander("Full shareholding table"):
            st.dataframe(sh_df, use_container_width=True)

    st.markdown("### Recent Trades")
    trades_path = parse_trades_url(resolved["html"])
    if not trades_path:
        st.warning("No Trades link found for this company.")
        return
    with st.spinner("Fetching trades ..."):
        trades_html = fetch_url(session, BASE + trades_path)
    if not trades_html:
        st.error("Could not fetch the Trades page.")
        return
    if looks_login_gated(trades_html):
        st.error("Trades page looks login-gated — check your `cookies.pkl` is valid.")
        return

    cutoff = datetime.now() - timedelta(days=window_days)
    block_df = parse_block_or_bulk(trades_html, DEAL_TABS["block"])
    bulk_df = parse_block_or_bulk(trades_html, DEAL_TABS["bulk"])

    def prep(df):
        if df.empty:
            return df
        df = df[df["Date"] >= cutoff].copy()
        df["Action"] = df["Action"].map(normalise_action)
        df["Tag"] = df["Person / Entity"].map(classify_investor)
        return df.sort_values("Date", ascending=False)

    block_recent, bulk_recent = prep(block_df), prep(bulk_df)
    tab1, tab2 = st.tabs(["📌 Block Deals", "📦 Bulk Deals"])
    for tab, df, label in [(tab1, block_recent, "block"), (tab2, bulk_recent, "bulk")]:
        with tab:
            if df.empty:
                st.info(f"No {label} deals in the last {window_days} day(s).")
            else:
                show = df.copy()
                show["Date"] = show["Date"].dt.strftime("%d %b %Y")
                st.dataframe(show[["Date", "Person / Entity", "Tag", "Action", "Quantity", "Price"]],
                             use_container_width=True, hide_index=True)


def bulk_scan_mode(session, idx):
    st.caption(
        "Scan many companies at once for Block/Bulk deals in the last N days. "
        "Each company costs 2 page fetches, so keep the list reasonable and use "
        "a few workers — screener.in will rate-limit or log you out if hammered."
    )

    source = st.radio(
        "Company list source",
        ["Paste tickers/codes/names", "Upload watchlist CSV", "Full NSE universe", "Full BSE universe"],
        horizontal=False,
    )

    companies = []
    if source == "Paste tickers/codes/names":
        text = st.text_area(
            "One per line (NSE ticker, BSE code, or company name)",
            height=160,
            placeholder="RELIANCE\nTCS\n500325\nHDFC BANK",
        )
        for line in text.splitlines():
            resolved = resolve_identifier_line(line, idx)
            if resolved:
                companies.append(resolved)

    elif source == "Upload watchlist CSV":
        up = st.file_uploader("CSV with a column of names / NSE tickers / BSE codes", type=["csv"])
        if up is not None:
            wl = pd.read_csv(up)
            col = wl.columns[0]
            st.caption(f"Using column `{col}` ({len(wl)} rows)")
            for val in wl[col].astype(str):
                resolved = resolve_identifier_line(val, idx)
                if resolved:
                    companies.append(resolved)

    else:
        nse, bse = load_ticker_lists()
        pool_df = nse if source == "Full NSE universe" else bse
        max_n = st.number_input(
            f"Max companies to scan (universe has {len(pool_df)})",
            min_value=1, max_value=int(len(pool_df)), value=min(100, len(pool_df)), step=10,
        )
        subset = pool_df.head(int(max_n))
        for _, r in subset.iterrows():
            if source == "Full NSE universe":
                companies.append((r["Name"], r["NSE Ticker"], None))
            else:
                companies.append((r["Name"], None, r["BSE Code"]))

    companies = list({c[0]: c for c in companies}.values())  # de-dupe by name

    c1, c2, c3 = st.columns(3)
    window_days = c1.number_input("Deals in last N days", min_value=1, max_value=365, value=3, step=1)
    max_workers = c2.slider("Parallel workers", 1, 16, 6)
    delay_ms = c3.slider("Polite delay per request (ms)", 0, 1000, 150, step=50)

    st.write(f"**{len(companies)}** companies queued.")

    if st.button("▶️ Run scan", type="primary", disabled=(len(companies) == 0)):
        progress = st.progress(0.0, text="Starting...")
        live_table = st.empty()
        collected = []

        def on_progress(done, total, result):
            progress.progress(done / total, text=f"{done}/{total} — {result['name']} ({result['status']})")
            if not result["deals"].empty:
                collected.append(result["deals"])
                merged = pd.concat(collected, ignore_index=True).sort_values("Date", ascending=False)
                shown = merged.copy()
                shown["Date"] = shown["Date"].dt.strftime("%d %b %Y")
                live_table.dataframe(
                    shown[["Date", "Company", "Deal Type", "Person / Entity", "Tag", "Action", "Quantity", "Price"]],
                    use_container_width=True, hide_index=True,
                )

        deals_df, status_counts, log = run_bulk_scan(
            session, companies, window_days, max_workers, delay_ms / 1000.0, progress_cb=on_progress
        )
        progress.progress(1.0, text="Done.")

        st.markdown("#### Summary")
        st.write(status_counts)
        if status_counts.get("login_required"):
            st.warning(
                f"{status_counts['login_required']} companies looked login-gated — "
                "your session cookie may have expired mid-scan."
            )

        if deals_df.empty:
            st.info(f"No Block/Bulk deals found in the last {window_days} day(s) across the scanned companies.")
        else:
            deals_df = deals_df.sort_values("Date", ascending=False)
            show = deals_df.copy()
            show["Date"] = show["Date"].dt.strftime("%d %b %Y")
            st.dataframe(
                show[["Date", "Company", "Deal Type", "Person / Entity", "Tag", "Action", "Quantity", "Price"]],
                use_container_width=True, hide_index=True,
            )
            st.download_button(
                "⬇️ Download results (CSV)",
                show.to_csv(index=False).encode("utf-8"),
                file_name=f"screener_deals_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv",
            )

        with st.expander("Per-company log"):
            st.dataframe(pd.DataFrame(log), use_container_width=True, hide_index=True)


def main():
    st.set_page_config(page_title="Screener FII/DII & Block Deal Scraper", layout="wide")
    st.title("📊 Screener.in Shareholding & Deals Scraper")

    cookie_path = find_cookie_file()
    session, has_cookies = build_session(cookie_path)
    render_login_banner(session, cookie_path, has_cookies)

    with st.sidebar:
        st.header("Cookies")
        st.caption(f"Looking for: {', '.join(os.path.basename(p) for p in COOKIE_CANDIDATES)}")
        if cookie_path:
            st.caption(f"✅ Using `{cookie_path}`")
        uploaded = st.file_uploader("...or upload cookies.pkl for this session", type=["pkl"])
        if uploaded is not None:
            tmp_path = os.path.join(APP_DIR, "cookies.pkl")
            with open(tmp_path, "wb") as f:
                f.write(uploaded.getbuffer())
            st.cache_resource.clear()
            st.success("Cookie file saved — rerunning with new session.")
            st.rerun()

        st.divider()
        mode = st.radio("Mode", ["🔎 Single Company", "📡 Bulk Scan – Recent Deals"])

    idx = build_search_index()
    if mode == "🔎 Single Company":
        single_company_mode(session, idx)
    else:
        bulk_scan_mode(session, idx)

    st.divider()
    st.caption(
        "Data pulled live from screener.in using your authenticated session. "
        "FII/DII tagging is a name-based heuristic, not authoritative — verify "
        "before relying on it. Research convenience only, not investment advice."
    )


if __name__ == "__main__":
    main()
