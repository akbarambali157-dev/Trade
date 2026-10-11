"""Read-only Streamlit dashboard for the separate paper-trading collector."""
import json
import math
import os
import sqlite3
import time
from pathlib import Path

import pandas as pd
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
                st.caption(_age_label(age))
                if age is None:
                    st.caption("No tick yet")
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
        raw = r.lrange(f"ticks:{symbol}", 0, -1)
        if not raw:
            st.info(f"Waiting for market data for {symbol}.")
            return
        records = []
        for item in raw:
            try:
                tick = json.loads(item)
                price = float(tick["price"])
                if math.isfinite(price) and price > 0:
                    records.append({"timestamp": tick["timestamp"], "price": price})
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
        frame = pd.DataFrame(records)
        if frame.empty:
            st.info(f"No valid market ticks are available for {symbol} yet.")
            return

        frame["timestamp"] = pd.to_datetime(
            frame["timestamp"], utc=True, errors="coerce"
        )
        frame = frame.dropna(subset=["timestamp", "price"]).sort_values("timestamp")
        if frame.empty:
            st.info("No valid timestamps are available to chart yet.")
            return

        ohlc = frame.set_index("timestamp")["price"].resample(candle_interval).ohlc().dropna()
        if ohlc.empty:
            st.info("Waiting for enough ticks to build candles.")
            return

        fig = go.Figure(data=[go.Candlestick(
            x=ohlc.index,
            open=ohlc["open"],
            high=ohlc["high"],
            low=ohlc["low"],
            close=ohlc["close"],
            name=symbol,
        )])
        fig.update_layout(
            title=f"{symbol} · {candle_interval} OHLC",
            margin=dict(l=10, r=10, t=35, b=10),
            height=420,
            xaxis_rangeslider_visible=False,
            template="plotly_dark",
        )
        st.plotly_chart(fig, use_container_width=True)
    except Exception as exc:
        st.error(f"Could not render the chart: {exc}")


tab1, tab2 = st.tabs(["Live charts & portfolio", "Audit trail & analytics"])

with tab1:
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

with tab2:
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

st.caption(
    "Market data and paper-trading state are supplied by collector.py, which must "
    "run separately. Prices may be delayed or stale; this is a development prototype, "
    "not a production trading system."
)
