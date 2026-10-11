"""Read-only Streamlit dashboard for the separate paper-trading collector."""
import hashlib
import json
import math
import os
import sqlite3
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import fitz
import plotly.graph_objects as go
import redis
import streamlit as st

st.set_page_config(
    page_title="Quant Trading Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

WATCHLIST = ["AAPL", "MSFT", "TSLA", "NVDA", "BINANCE:BTCUSDT", "OANDA:XAU_USD"]


def setting(name, default=None):
    """Read configuration from environment variables or Streamlit secrets."""
    value = os.getenv(name)
    if value:
        return value
    try:
        value = st.secrets.get(name, default)
        return value if value not in ("", None) else default
    except Exception:
        # Local runs and first-time cloud deploys may not have secrets configured.
        return default


REDIS_URL = setting("REDIS_URL")
DB_NAME = setting(
    "TRADING_DB_PATH",
    str(Path(__file__).resolve().parent / "trading_bot.db"),
)
REDIS_CONNECT_TIMEOUT = 3.0
STALE_WARNING_SECONDS = 60
STALE_POSITION_SECONDS = 120

st.title("Quant Trading Dashboard")
st.caption(
    "SIMULATION ONLY · This read-only dashboard does not place brokerage orders "
    "or use real money."
)

if not REDIS_URL:
    st.warning("Redis is not configured yet.")
    st.markdown(
        "To connect the dashboard, add a reachable Redis URL in your Streamlit "
        "app's **Settings → Secrets**. Do not commit credentials to GitHub."
    )
    st.code('REDIS_URL = "rediss://<username>:<password>@<host>:<port>"', language="toml")
    st.info(
        "The market-data collector must run separately and publish ticks and "
        "portfolio state to the same Redis instance. Streamlit Community Cloud "
        "does not start collector.py automatically."
    )
    st.stop()

try:
    r = redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=REDIS_CONNECT_TIMEOUT,
        socket_timeout=REDIS_CONNECT_TIMEOUT,
        health_check_interval=30,
    )
    r.ping()
except Exception:
    # Avoid displaying the exception because connection errors can contain host
    # details or credentials embedded in the configured URL.
    st.error(
        "Redis could not be reached. Check the REDIS_URL secret, Redis service "
        "status, network access, and whether the collector and dashboard use "
        "the same Redis instance."
    )
    st.stop()


def load_portfolio():
    cash_raw = r.get("portfolio:cash")
    positions_raw = r.get("portfolio:positions")
    if cash_raw is None and positions_raw is None:
        return None, None
    if cash_raw is None or positions_raw is None:
        raise ValueError("Portfolio state is incomplete; check the collector logs.")
    cash = float(cash_raw)
    positions = json.loads(positions_raw)
    if not math.isfinite(cash) or cash < 0 or not isinstance(positions, dict):
        raise ValueError("Portfolio state contains invalid cash or positions.")
    for symbol, pos in positions.items():
        if not isinstance(pos, dict):
            raise ValueError(f"Invalid position data for {symbol}.")
        for field in ("shares", "entry_price", "atr_stop"):
            value = float(pos[field])
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"Invalid {field} value for {symbol}.")
    return cash, positions


def load_prices():
    prices, ages = {}, {}
    for symbol in WATCHLIST:
        raw = r.lrange(f"ticks:{symbol}", -1, -1)
        health = r.get(f"health:last_tick:{symbol}")
        if raw:
            try:
                tick = json.loads(raw[0])
                price = float(tick["price"])
                if math.isfinite(price) and price > 0:
                    prices[symbol] = price
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                # Ignore a malformed latest tick rather than breaking the dashboard.
                pass
        if health:
            try:
                received_at = float(health)
                if math.isfinite(received_at) and received_at > 0:
                    ages[symbol] = max(0.0, time.time() - received_at)
            except (ValueError, TypeError):
                pass
    return prices, ages



def market_status(symbol):
    """Return scheduled session state; crypto trades continuously, other sessions use ET."""
    if symbol == "BINANCE:BTCUSDT":
        return "Open", "Crypto trades 24/7"
    now = datetime.now(ZoneInfo("America/New_York"))
    clock = now.time().replace(tzinfo=None)
    weekday = now.weekday()  # Monday=0, Sunday=6

    if symbol == "OANDA:XAU_USD":
        # Approximate OANDA spot-gold schedule: Sunday evening through Friday afternoon ET,
        # with a short daily maintenance break. Broker holidays can differ.
        if weekday == 5:
            return "Closed", "Gold session resumes Sunday at about 5:05 PM ET"
        if weekday == 6 and clock < dt_time(17, 5):
            return "Closed", "Gold session resumes Sunday at about 5:05 PM ET"
        if weekday == 4 and clock >= dt_time(16, 59):
            return "Closed", "Gold session resumes Sunday at about 5:05 PM ET"
        if dt_time(16, 59) <= clock < dt_time(17, 5):
            return "Closed", "Daily market maintenance break"
        return "Open", "Scheduled gold trading session"

    # US-listed equities regular session; exchange holidays are not included in this estimate.
    if weekday >= 5 or not (dt_time(9, 30) <= clock < dt_time(16, 0)):
        return "Closed", "Outside regular US stock hours (9:30 AM–4:00 PM ET)"
    return "Open", "Scheduled regular US stock session"


def _age_label(age):
    if age is None:
        return "No data received yet"
    if age < 60:
        return f"Last update {int(age)}s ago"
    if age < 3600:
        return f"Last update {int(age // 60)}m ago"
    return f"Last update {int(age // 3600)}h ago"


@st.fragment(run_every=3)
def connection_panel():
    """Show a compact, mobile-friendlier view of the data path."""
    now = time.time()
    try:
        redis_ok = bool(r.ping())
    except Exception:
        redis_ok = False

    heartbeat_raw = r.get("health:collector:heartbeat")
    try:
        collector_age = max(0.0, now - float(heartbeat_raw)) if heartbeat_raw else None
    except (TypeError, ValueError):
        collector_age = None
    collector_ok = collector_age is not None and collector_age <= 35

    finnhub_state = r.get("health:finnhub:status") or "unknown"
    message_raw = r.get("health:finnhub:last_message")
    try:
        message_age = max(0.0, now - float(message_raw)) if message_raw else None
    except (TypeError, ValueError):
        message_age = None

    st.subheader("Connections")
    st.caption("Finnhub → your computer → Upstash Redis → dashboard")

    row1 = st.columns(2)
    with row1[0]:
        with st.container(border=True):
            st.markdown("**01 · Finnhub feed**")
            if finnhub_state == "connected":
                st.success("Connected")
            elif finnhub_state == "error":
                st.error("Feed error")
            elif finnhub_state == "disconnected":
                st.warning("Disconnected")
            elif finnhub_state == "connecting":
                st.info("Connecting…")
            else:
                st.warning("Waiting for status")
            st.caption(_age_label(message_age))
    with row1[1]:
        with st.container(border=True):
            st.markdown("**02 · Collector**")
            if collector_ok:
                st.success("Worker online")
            elif collector_age is None:
                st.warning("No heartbeat")
            else:
                st.error("Heartbeat stale")
            st.caption(_age_label(collector_age))

    row2 = st.columns(2)
    with row2[0]:
        with st.container(border=True):
            st.markdown("**03 · Upstash Redis**")
            if redis_ok:
                st.success("Reachable")
            else:
                st.error("Unavailable")
            st.caption("Shared market data")
    with row2[1]:
        with st.container(border=True):
            st.markdown("**04 · Dashboard**")
            st.success("Online")
            st.caption("Auto-refresh: 3 seconds")

    prices, ages = load_prices()
    st.subheader("Markets")
    st.caption("Tap a market card to open its chart below.")
    pulse_cols = st.columns(2)
    for index, symbol in enumerate(WATCHLIST):
        if symbol == "BINANCE:BTCUSDT":
            label = "BTC / USDT"
        elif symbol == "OANDA:XAU_USD":
            label = "XAU / USD · Gold"
        else:
            label = symbol
        with pulse_cols[index % 2]:
            with st.container(border=True):
                price = prices.get(symbol)
                st.metric(label, f"${price:,.2f}" if price is not None else "Waiting…")
                age = ages.get(symbol)
                session_state, session_detail = market_status(symbol)
                if session_state == "Closed":
                    st.warning("Market closed")
                    st.caption(session_detail)
                    st.caption(_age_label(age))
                else:
                    st.caption(session_detail)
                    st.caption(_age_label(age))
                    if age is None:
                        st.caption("Waiting for first tick")
                    elif age > STALE_WARNING_SECONDS:
                        st.caption("Feed may be stale")
                    else:
                        st.caption("● Live feed")
                selected = st.session_state.get("selected_market", WATCHLIST[0]) == symbol
                button_label = "✓ Chart selected" if selected else "View chart →"
                if st.button(button_label, key=f"market_{symbol}", use_container_width=True):
                    st.session_state["selected_market"] = symbol
                    st.rerun()


def load_trades():
    """Read local SQLite audit records, if this deployment has access to them."""
    if not os.path.exists(DB_NAME):
        return pd.DataFrame(
            columns=[
                "timestamp", "symbol", "action", "price", "shares",
                "pnl_usd", "pnl_pct", "reason",
            ]
        )
    with sqlite3.connect(DB_NAME, timeout=5) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trades'"
        ).fetchone()
        if table is None:
            return pd.DataFrame(
                columns=[
                    "timestamp", "symbol", "action", "price", "shares",
                    "pnl_usd", "pnl_pct", "reason",
                ]
            )
        return pd.read_sql_query(
            "SELECT timestamp, symbol, action, price, shares, pnl_usd, pnl_pct, reason "
            "FROM trades ORDER BY id DESC",
            conn,
        )


@st.fragment(run_every=3)
def portfolio_panel():
    try:
        cash, positions = load_portfolio()
        prices, ages = load_prices()
        if cash is None:
            st.info("Waiting for the collector to initialize the simulated portfolio.")
            return

        equity = cash
        for symbol, pos in positions.items():
            equity += float(pos["shares"]) * prices.get(
                symbol, float(pos["entry_price"])
            )

        stale = [
            symbol for symbol in positions
            if ages.get(symbol, math.inf) > STALE_POSITION_SECONDS
            or symbol not in prices
        ]
        c1, c2, c3 = st.columns(3)
        c1.metric("Estimated portfolio equity", f"${equity:,.2f}")
        c2.metric("Available cash", f"${cash:,.2f}")
        c3.metric("Open positions", str(len(positions)))

        if stale:
            st.warning(
                "Prices are stale or missing for open positions: "
                + ", ".join(stale)
                + ". Equity uses entry prices for those symbols."
            )
        if positions:
            rows = []
            for symbol, pos in positions.items():
                age = ages.get(symbol)
                rows.append({
                    "Symbol": symbol,
                    "Shares": round(float(pos["shares"]), 4),
                    "Entry price": round(float(pos["entry_price"]), 4),
                    "Latest price": round(
                        prices.get(symbol, float(pos["entry_price"])), 4
                    ),
                    "ATR stop": round(float(pos["atr_stop"]), 4),
                    "Price age (seconds)": round(age, 1) if age is not None else "Unknown",
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.info("No active simulated positions.")
    except Exception as exc:
        st.error(f"Could not refresh portfolio state: {exc}")


@st.fragment(run_every=3)
def live_chart(symbol, candle_interval):
    try:
        # Read persistent 1-minute OHLC candles rather than reconstructing history
        # from the small rolling list of raw ticks.
        raw_history = r.lrange(f"candles:1m:{symbol}", 0, -1)
        raw_current = r.get(f"candles:current:{symbol}")
        candle_records = []
        for item in raw_history:
            try:
                candle = json.loads(item)
                candle_records.append(candle)
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
        if raw_current:
            try:
                candle_records.append(json.loads(raw_current))
            except (ValueError, TypeError, json.JSONDecodeError):
                pass

        if not candle_records:
            # Compatibility fallback for data collected before persistent candles were added.
            raw_ticks = r.lrange(f"ticks:{symbol}", 0, -1)
            records = []
            for item in raw_ticks:
                try:
                    tick = json.loads(item)
                    price = float(tick["price"])
                    if math.isfinite(price) and price > 0:
                        records.append({"timestamp": tick["timestamp"], "price": price})
                except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                    continue
            frame = pd.DataFrame(records)
            if frame.empty:
                st.info(f"No saved price history is available for {symbol} yet. History will build as the collector records candles.")
                return
            frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
            frame = frame.dropna(subset=["timestamp", "price"]).sort_values("timestamp")
            if frame.empty:
                st.info("No valid timestamps are available to chart yet.")
                return
            minute = frame.set_index("timestamp")["price"].resample("1min").ohlc().dropna()
            ohlc = minute.resample(candle_interval).agg({
                "open": "first", "high": "max", "low": "min", "close": "last"
            }).dropna()
            history_note = "Using recent tick data; persistent candle history will accumulate from the collector."
        else:
            frame = pd.DataFrame(candle_records)
            required = {"timestamp", "open", "high", "low", "close"}
            if not required.issubset(frame.columns):
                st.warning("Saved candle history has an unexpected format.")
                return
            frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
            for column in ("open", "high", "low", "close"):
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
            frame = frame.dropna(subset=["timestamp", "open", "high", "low", "close"])
            frame = frame.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
            if frame.empty:
                st.info("No valid saved candles are available yet.")
                return
            ohlc = frame.set_index("timestamp")[["open", "high", "low", "close"]].resample(
                candle_interval
            ).agg({
                "open": "first", "high": "max", "low": "min", "close": "last"
            }).dropna()
            history_note = "Candles are retained in Redis across collector restarts; older history starts accumulating after this update."

        if ohlc.empty:
            st.info("Waiting for enough price data to build candles.")
            return

        fig = go.Figure(data=[go.Candlestick(
            x=ohlc.index,
            open=ohlc["open"],
            high=ohlc["high"],
            low=ohlc["low"],
            close=ohlc["close"],
            name=symbol,
            increasing=dict(line=dict(color="#20D6C7"), fillcolor="#20D6C7"),
            decreasing=dict(line=dict(color="#F04452"), fillcolor="#F04452"),
        )])
        fig.update_layout(
            title=f"{symbol} · {candle_interval} candles",
            margin=dict(l=8, r=8, t=42, b=8),
            height=500,
            template="plotly_dark",
            paper_bgcolor="#0B0F14",
            plot_bgcolor="#0B0F14",
            dragmode="pan",
            uirevision=f"{symbol}-{candle_interval}",
            xaxis=dict(
                fixedrange=False, showgrid=True, gridcolor="rgba(150,160,175,0.14)",
                rangeslider=dict(visible=False), type="date"
            ),
            yaxis=dict(
                side="right", fixedrange=False, showgrid=True,
                gridcolor="rgba(150,160,175,0.14)"
            ),
            hovermode="x unified",
        )
        st.caption(history_note)
        st.caption("On touchscreens, pinch with two fingers to zoom; drag to pan. Use the chart toolbar to reset the view.")
        st.plotly_chart(
            fig,
            use_container_width=True,
            config={
                "scrollZoom": True,
                "doubleClick": "reset",
                "displayModeBar": True,
                "displaylogo": False,
                "responsive": True,
                "modeBarButtonsToRemove": ["lasso2d", "select2d"],
            },
        )
    except Exception:
        st.error("Could not render the chart. Check the saved candle data and Redis connection.")


# Streamlit's collapsed sidebar becomes the three-bar navigation menu.
st.markdown("""
<style>
:root { color-scheme: dark; }
.stApp { background: radial-gradient(ellipse at 10% 0%, #12212b 0%, #0a0f14 44%, #080b10 100%); }
.block-container { max-width: 1500px; padding-top: 2rem; padding-bottom: 3rem; }
[data-testid="stSidebar"] { background: #0b1219; border-right: 1px solid #23313b; }
[data-testid="stMetric"] { background: #111b24; border: 1px solid #263743; padding: 14px 16px; border-radius: 14px; }
div[data-testid="stVerticalBlockBorderWrapper"] { border-color: #253642; border-radius: 16px; background: rgba(17,27,36,.72); }
.hero-panel { padding: 28px; border-radius: 20px; border: 1px solid #2a414d; background: linear-gradient(120deg, rgba(23,54,66,.92), rgba(13,20,29,.96)); }
.eyebrow { color: #61e2d2; letter-spacing: .16em; font-size: .75rem; font-weight: 700; }
.hero-title { font-size: clamp(2rem, 4vw, 3.4rem); line-height: 1.08; font-weight: 750; margin: .5rem 0; }
.muted { color: #9aabb8; }
</style>
""", unsafe_allow_html=True)

def go_to_page(page_name):
    st.session_state["workspace_page"] = page_name

with st.sidebar:
    st.markdown("## ◈ MERIDIAN")
    st.caption("PAPER TRADING WORKSPACE")
    page = st.radio(
        "Workspace",
        ["Home", "Charts", "Markets", "Knowledge library", "Bot workbench", "Portfolio & history"],
        label_visibility="collapsed",
        key="workspace_page",
    )
    st.divider()
    st.markdown("**SUPPORTED MARKETS**")
    st.caption("₿  BTC / USDT · 24/7")
    st.caption("◉  XAU / USD · Gold · chart only")
    st.caption("AAPL · MSFT · TSLA · NVDA")
    st.divider()
    st.caption("SIMULATION ONLY")
    st.caption("No brokerage orders or real money.")

if page == "Home":
    st.markdown('<div class="hero-panel"><div class="eyebrow">YOUR MARKET RESEARCH SPACE</div><div class="hero-title">Good to see you.<br>Let’s study the markets.</div><p class="muted">Track live feeds, explore price action, build a searchable trading knowledge library, and test a rules-based bot in a paper environment.</p></div>', unsafe_allow_html=True)
    st.write("")
    prices, ages = load_prices()
    saved_ids = r.smembers("knowledge:pdf_ids")
    cash, positions = None, None
    try:
        cash, positions = load_portfolio()
    except Exception:
        pass
    a, b, c = st.columns(3)
    a.metric("Knowledge documents", len(saved_ids))
    a.metric("Paper positions", len(positions) if positions is not None else "Waiting")
    a.metric("BTC / USDT", f"${prices['BINANCE:BTCUSDT']:,.2f}" if "BINANCE:BTCUSDT" in prices else "Waiting for feed")
    st.write("")
    left, right = st.columns([1.2, 0.8], gap="large")
    with left:
        st.markdown("### Add a PDF to your knowledge base")
        st.caption("Upload trading notes, strategy guides, or chart reference PDFs. Text is indexed for search; this does not train an AI model.")
        home_pdf = st.file_uploader("Choose a PDF", type=["pdf"], key="home_pdf_upload", help="Maximum 25 MB and 200 pages.")
        if home_pdf is not None:
            if len(home_pdf.getvalue()) > 25 * 1024 * 1024:
                st.error("This PDF is larger than 25 MB.")
            elif st.button("Add PDF to knowledge library", type="primary", key="home_parse_pdf"):
                try:
                    data = home_pdf.getvalue()
                    digest = hashlib.sha256(data).hexdigest()
                    with fitz.open(stream=data, filetype="pdf") as pdf:
                        if len(pdf) > 200:
                            st.error("This PDF has more than 200 pages. Split it into smaller files.")
                        else:
                            chunks, chars = [], 0
                            for page_no, pdf_page in enumerate(pdf, start=1):
                                page_text = pdf_page.get_text("text").strip()
                                chars += len(page_text)
                                for start in range(0, len(page_text), 1200):
                                    excerpt = page_text[max(0, start - 120):start + 1200].strip()
                                    if excerpt:
                                        chunks.append({"page": page_no, "text": excerpt})
                            doc = {"filename": home_pdf.name, "sha256": digest, "pages": len(pdf), "text_chars": chars, "chunks": chunks[:3000], "saved_at": int(time.time())}
                            r.set(f"knowledge:pdf:{digest}", json.dumps(doc, ensure_ascii=False))
                            r.sadd("knowledge:pdf_ids", digest)
                            st.success(f"Added {home_pdf.name} to your library.")
                            if chars == 0:
                                st.warning("No selectable text found; this may be a scanned PDF. OCR is not enabled yet.")
                except Exception:
                    st.error("Could not parse the PDF. Try an unlocked, undamaged PDF.")
    with right:
        st.markdown("### Workspace shortcuts")
        with st.container(border=True):
            st.markdown("**01 · Charts & price action**")
            st.caption("Candles, timeframes, and market feed status.")
            st.button("Open charts →", use_container_width=True, on_click=go_to_page, args=("Charts",))
        with st.container(border=True):
            st.markdown("**02 · Knowledge library**")
            st.caption("Search excerpts and review uploaded references.")
            st.button("Explore knowledge →", use_container_width=True, on_click=go_to_page, args=("Knowledge library",))
        with st.container(border=True):
            st.markdown("**03 · Bot workbench**")
            st.caption("Inspect the paper strategy and its safety controls.")
            st.button("Open bot workbench →", use_container_width=True, on_click=go_to_page, args=("Bot workbench",))
    st.write("")
    st.markdown("### Market pulse")
    pulse = st.columns(3)
    for col, symbol, label in zip(pulse, ["BINANCE:BTCUSDT", "OANDA:XAU_USD", "AAPL"], ["BTC / USDT", "XAU / USD · Gold", "AAPL"]):
        with col:
            with st.container(border=True):
                value = prices.get(symbol)
                st.metric(label, f"${value:,.2f}" if value is not None else "Waiting…")
                status, detail = market_status(symbol)
                st.caption(f"{status} · {detail}")
                st.caption(_age_label(ages.get(symbol)))

elif page == "Charts":
    connection_panel()
    selected_symbol = st.session_state.get("selected_market", WATCHLIST[0])
    selected_label = {
        "BINANCE:BTCUSDT": "BTC / USDT · Binance",
        "OANDA:XAU_USD": "XAU / USD · Gold",
    }.get(selected_symbol, selected_symbol)
    st.subheader(f"{selected_label} chart")
    timeframe_choice = st.radio(
        "Timeframe",
        ["1m", "5m", "15m", "30m", "1h", "4h", "1D"],
        index=0,
        horizontal=True,
        key="chart_timeframe",
        help="Candles are built from ticks currently retained in Redis.",
    )
    candle_interval = {
        "1m": "1min",
        "5m": "5min",
        "15m": "15min",
        "30m": "30min",
        "1h": "1h",
        "4h": "4h",
        "1D": "1D",
    }[timeframe_choice]
    try:
        health = r.get(f"health:last_tick:{selected_symbol}")
        if health is None:
            st.info("Waiting for the collector's first tick.")
        else:
            try:
                age = max(0.0, time.time() - float(health))
                if age > STALE_WARNING_SECONDS:
                    st.warning(f"Market feed is stale ({int(age)} seconds since last tick).")
                else:
                    st.success(f"Market feed recently received data ({int(age)} seconds ago).")
            except (ValueError, TypeError):
                st.warning("The collector's feed-health timestamp is invalid.")
    except Exception:
        st.warning("Could not read feed health; check the Redis connection.")

    live_chart(selected_symbol, candle_interval)
    st.subheader("Portfolio")
    portfolio_panel()

elif page == "Portfolio & history":
    st.subheader("Completed simulated trades")
    try:
        trades = load_trades()
        if trades.empty:
            st.info(
                "No audit records are visible to this dashboard yet. If the collector "
                "runs as a separate service, its local SQLite file is not shared with "
                "Streamlit Community Cloud; use a shared persistent database to share "
                "audit history between services."
            )
        else:
            sells = trades[trades["action"] == "SELL"]
            wins = int((pd.to_numeric(sells["pnl_usd"], errors="coerce") > 0).sum())
            rate = (wins / len(sells) * 100) if len(sells) else 0.0
            c1, c2, c3 = st.columns(3)
            c1.metric("Completed trades", len(sells))
            c2.metric("Winning trades", wins)
            c3.metric("Win rate", f"{rate:.1f}%")
            st.download_button(
                "Download audit CSV",
                data=trades.to_csv(index=False).encode("utf-8"),
                file_name="paper_trading_audit.csv",
                mime="text/csv",
            )
            st.dataframe(trades.head(100), use_container_width=True, hide_index=True)
    except Exception as exc:
        st.error(f"Could not load the audit database: {exc}")


elif page == "Knowledge library":
    st.markdown("## Knowledge library")
    st.caption("Your saved reference material, searchable excerpts, and chart-heavy PDF previews.")
    try:
        knowledge_ids = sorted(r.smembers("knowledge:pdf_ids"))
        knowledge_docs, knowledge_pages, knowledge_chars, knowledge_chunks = 0, 0, 0, 0
        for knowledge_id in knowledge_ids:
            raw_knowledge = r.get(f"knowledge:pdf:{knowledge_id}")
            if not raw_knowledge:
                continue
            try:
                knowledge_doc = json.loads(raw_knowledge)
            except (ValueError, TypeError):
                continue
            knowledge_docs += 1
            knowledge_pages += int(knowledge_doc.get("pages", 0) or 0)
            knowledge_chars += int(knowledge_doc.get("text_chars", 0) or 0)
            knowledge_chunks += len(knowledge_doc.get("chunks", []))
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Saved PDFs", knowledge_docs)
        k2.metric("Pages indexed", f"{knowledge_pages:,}")
        k3.metric("Text characters", f"{knowledge_chars:,}")
        k4.metric("Searchable excerpts", f"{knowledge_chunks:,}")
    except Exception:
        st.info("Knowledge totals are temporarily unavailable.")
    st.divider()
    st.subheader("PDF learning library")
    st.caption(
        "Upload trading guides, notes, or chart-heavy PDFs. The app extracts selectable text, "
        "keeps searchable excerpts in Redis, and renders page previews so charts and diagrams "
        "can be inspected. This is a searchable reference library, not model training."
    )
    uploaded_pdf = st.file_uploader(
        "Choose a PDF",
        type=["pdf"],
        key="pdf_knowledge_upload",
        help="Maximum 25 MB and 200 pages per PDF. Password-protected PDFs are not supported yet.",
    )
    if uploaded_pdf is not None:
        pdf_bytes = uploaded_pdf.getvalue()
        if len(pdf_bytes) > 25 * 1024 * 1024:
            st.error("That PDF is larger than 25 MB. Please use a smaller file.")
        else:
            digest = hashlib.sha256(pdf_bytes).hexdigest()
            doc_key = f"knowledge:pdf:{digest}"
            if st.button("Parse and add to library", type="primary", key="parse_pdf"):
                try:
                    with fitz.open(stream=pdf_bytes, filetype="pdf") as pdf:
                        if len(pdf) > 200:
                            st.error("This PDF has more than 200 pages. Please split it into smaller files.")
                        else:
                            page_records = []
                            extracted_chars = 0
                            for page_number, page in enumerate(pdf, start=1):
                                text_content = page.get_text("text").strip()
                                extracted_chars += len(text_content)
                                if text_content:
                                    # Keep page references attached to every searchable excerpt.
                                    for start in range(0, len(text_content), 1200):
                                        excerpt = text_content[max(0, start - 120):start + 1200].strip()
                                        if excerpt:
                                            page_records.append({
                                                "page": page_number,
                                                "text": excerpt,
                                            })
                            document = {
                                "filename": uploaded_pdf.name,
                                "sha256": digest,
                                "pages": len(pdf),
                                "text_chars": extracted_chars,
                                "chunks": page_records[:3000],
                                "saved_at": int(time.time()),
                            }
                            r.set(doc_key, json.dumps(document, ensure_ascii=False))
                            r.sadd("knowledge:pdf_ids", digest)
                            st.success(
                                f"Added **{uploaded_pdf.name}** ({len(pdf)} pages). "
                                f"Extracted {extracted_chars:,} text characters across "
                                f"{len(document['chunks'])} searchable excerpts."
                            )
                            if extracted_chars == 0:
                                st.warning(
                                    "No selectable text was found. This may be a scanned PDF; "
                                    "the page previews below can still show its charts, but OCR "
                                    "is not enabled yet."
                                )
                except Exception:
                    st.error(
                        "Could not parse this PDF. It may be damaged, password-protected, "
                        "or unsupported. Try exporting an unlocked copy and uploading it again."
                    )

            st.markdown("#### Preview pages and charts")
            try:
                with fitz.open(stream=pdf_bytes, filetype="pdf") as preview_pdf:
                    page_count = len(preview_pdf)
                    if page_count:
                        preview_pages = st.multiselect(
                            "Pages to preview",
                            options=list(range(1, min(page_count, 200) + 1)),
                            default=list(range(1, min(page_count, 3) + 1)),
                            key=f"pdf_preview_{digest}",
                        )
                        for page_number in preview_pages[:6]:
                            page = preview_pdf[page_number - 1]
                            pix = page.get_pixmap(matrix=fitz.Matrix(1.35, 1.35), alpha=False)
                            st.image(
                                pix.tobytes("png"),
                                caption=f"{uploaded_pdf.name} · page {page_number}",
                                use_container_width=True,
                            )
                            page_text = page.get_text("text").strip()
                            if page_text:
                                with st.expander(f"Extracted text · page {page_number}"):
                                    st.text(page_text[:8000])
            except Exception:
                st.info("A page preview could not be generated for this PDF.")

    st.divider()
    st.markdown("#### Search the saved PDF library")
    search_query = st.text_input(
        "Search concepts, terms, or phrases",
        placeholder="e.g. support and resistance, RSI divergence, risk management",
        key="pdf_library_search",
    )
    try:
        saved_ids = sorted(r.smembers("knowledge:pdf_ids"))
        if not saved_ids:
            st.info("No PDFs have been added yet. Upload and parse one above to start the library.")
        elif search_query.strip():
            terms = [term.lower() for term in search_query.split() if len(term) > 1]
            results = []
            for saved_id in saved_ids:
                raw_document = r.get(f"knowledge:pdf:{saved_id}")
                if not raw_document:
                    continue
                try:
                    document = json.loads(raw_document)
                except (ValueError, TypeError):
                    continue
                for chunk in document.get("chunks", []):
                    lowered = chunk.get("text", "").lower()
                    score = sum(lowered.count(term) for term in terms)
                    if score:
                        results.append((score, document.get("filename", "PDF"), chunk.get("page", "?"), chunk.get("text", "")))
            results.sort(key=lambda item: item[0], reverse=True)
            if results:
                st.caption(f"Showing up to 20 matching excerpts from {len(saved_ids)} saved PDF(s).")
                for score, filename, page_number, excerpt in results[:20]:
                    with st.expander(f"{filename} · page {page_number} · match score {score}"):
                        st.write(excerpt)
            else:
                st.info("No matches found. Try fewer or different keywords.")
        else:
            st.caption(f"{len(saved_ids)} PDF(s) saved in the shared Redis library.")
            for saved_id in saved_ids:
                raw_document = r.get(f"knowledge:pdf:{saved_id}")
                if raw_document:
                    try:
                        document = json.loads(raw_document)
                        st.write(
                            f"**{document.get('filename', 'PDF')}** — "
                            f"{document.get('pages', '?')} pages, "
                            f"{document.get('text_chars', 0):,} text characters"
                        )
                    except (ValueError, TypeError):
                        continue
    except Exception:
        st.warning("The PDF library could not be read from Redis. Check the Redis connection and available memory.")


elif page == "Markets":
    st.markdown("## Supported markets")
    st.caption("Prices are provided by the collector. Scheduled session labels are estimates, not an exchange holiday calendar.")
    prices, ages = load_prices()
    for symbol in WATCHLIST:
        label = {"BINANCE:BTCUSDT":"BTC / USDT", "OANDA:XAU_USD":"XAU / USD · Gold"}.get(symbol, symbol)
        status, detail = market_status(symbol)
        with st.container(border=True):
            cols = st.columns([1.3, 1, 1.3, 1])
            cols[0].markdown(f"**{label}**")
            cols[1].metric("Latest price", f"${prices[symbol]:,.4f}" if symbol in prices else "Waiting…")
            cols[2].markdown(f"**{status}**")
            cols[2].caption(detail)
            cols[3].caption(_age_label(ages.get(symbol)))
            if symbol == "OANDA:XAU_USD":
                st.caption("Gold is currently chart-only; the bot does not open gold positions.")
            elif symbol in ["AAPL", "MSFT", "TSLA", "NVDA", "BINANCE:BTCUSDT"]:
                st.caption("Eligible for the existing paper strategy when enabled and when valid signals/data are available.")

elif page == "Bot workbench":
    st.markdown("## Bot workbench")
    st.caption("A controlled workspace for the existing rules-based paper-trading engine—not a real brokerage bot.")
    enabled_raw = r.get("bot:paper_enabled")
    bot_enabled = enabled_raw != "false"
    status_cols = st.columns(3)
    status_cols[0].metric("Paper engine", "Enabled" if bot_enabled else "Paused")
    status_cols[1].metric("Execution mode", "Paper only")
    status_cols[2].metric("Gold trading", "Disabled")
    st.warning("This switch controls new strategy entries in collector.py. Protective stop checks should continue even while entries are paused. It never enables real-money orders.")
    toggle = st.toggle("Allow new simulated entries", value=bot_enabled, key="paper_bot_toggle")
    if toggle != bot_enabled:
        r.set("bot:paper_enabled", "true" if toggle else "false")
        st.success("Paper bot setting saved. The collector reads this setting while processing incoming ticks.")
        st.rerun()
    st.markdown("### Current strategy framework")
    with st.container(border=True):
        st.markdown("**Signal layer**")
        st.write("Completed 1-minute candles → SMA trend reference + RSI oversold check → ATR volatility estimate → BUY / SELL / HOLD decision.")
        st.markdown("**Risk layer**")
        st.write("Position sizing targets 1% of estimated equity risk per trade; aggregate open downside risk is capped at 3%; an ATR-based protective stop and trailing stop are used; commissions are simulated.")
        st.markdown("**Execution and audit layer**")
        st.write("Signals are processed by the background collector, portfolio state is stored in Redis, and trade audit events are written to SQLite on the collector machine.")
    st.info("The engine already evaluates incoming ticks automatically. The dashboard does not create an additional trading loop. The strategy has not been validated and its simulated performance is not a forecast.")
    st.markdown("### Bot health")
    h1, h2, h3 = st.columns(3)
    h1.metric("Collector", r.get("health:collector:status") or "Unknown")
    h2.metric("Market feed", r.get("health:finnhub:status") or "Unknown")
    h3.metric("Last bot decision", r.get("bot:last_decision") or "Not published yet")
    st.caption("Next development milestones: publish per-symbol decisions/indicator values, add replay/backtesting on historical candles, then evaluate out-of-sample paper results before considering any strategy changes.")


st.caption(
    "Market data and paper-trading state are supplied by collector.py, which must "
    "run separately. Prices may be delayed or stale; this is a development prototype, "
    "not a production trading system."
)
