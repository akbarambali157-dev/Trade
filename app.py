"""Read-only Streamlit dashboard for the background paper-trading collector."""
import json
import os
import sqlite3
import time

import pandas as pd
import plotly.graph_objects as go
import redis
import streamlit as st

st.set_page_config(page_title="Quant Trading Dashboard", layout="wide", initial_sidebar_state="expanded")

WATCHLIST = ["AAPL", "MSFT", "TSLA", "NVDA"]
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
DB_NAME = os.getenv("TRADING_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "trading_bot.db"))

st.title("Quant Trading Dashboard")
st.caption("Paper trading only — this dashboard does not place real brokerage orders.")

try:
    r = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=3.0)
    r.ping()
except Exception as exc:
    st.error(f"Redis is unavailable. Start Redis and the collector, then refresh. Details: {exc}")
    st.stop()


def load_portfolio():
    cash_raw = r.get("portfolio:cash")
    positions_raw = r.get("portfolio:positions")
    if cash_raw is None and positions_raw is None:
        return None, None
    if cash_raw is None or positions_raw is None:
        raise ValueError("Portfolio state is incomplete.")
    cash = float(cash_raw)
    positions = json.loads(positions_raw)
    if not isinstance(positions, dict):
        raise ValueError("Portfolio positions are invalid.")
    return cash, positions


def load_prices():
    prices, ages = {}, {}
    for symbol in WATCHLIST:
        raw = r.lrange(f"ticks:{symbol}", -1, -1)
        health = r.get(f"health:last_tick:{symbol}")
        if raw:
            tick = json.loads(raw[0])
            price = float(tick["price"])
            if price > 0:
                prices[symbol] = price
        if health:
            ages[symbol] = max(0.0, time.time() - float(health))
    return prices, ages


def load_trades():
    if not os.path.exists(DB_NAME):
        return pd.DataFrame(columns=["timestamp", "symbol", "action", "price", "shares", "pnl_usd", "pnl_pct", "reason"])
    with sqlite3.connect(DB_NAME, timeout=5) as conn:
        return pd.read_sql_query(
            "SELECT timestamp, symbol, action, price, shares, pnl_usd, pnl_pct, reason FROM trades ORDER BY id DESC",
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
            equity += float(pos["shares"]) * prices.get(symbol, float(pos["entry_price"]))
        stale = [s for s in positions if ages.get(s, float("inf")) > 120 or s not in prices]
        c1, c2, c3 = st.columns(3)
        c1.metric("Estimated portfolio equity", f"${equity:,.2f}")
        c2.metric("Available cash", f"${cash:,.2f}")
        c3.metric("Open positions", str(len(positions)))
        if stale:
            st.warning("Stale or missing market prices for open positions: " + ", ".join(stale))
        if positions:
            rows = []
            for symbol, pos in positions.items():
                age = ages.get(symbol)
                rows.append({
                    "Symbol": symbol,
                    "Shares": round(float(pos["shares"]), 4),
                    "Entry price": round(float(pos["entry_price"]), 4),
                    "Latest price": round(prices.get(symbol, float(pos["entry_price"])), 4),
                    "ATR stop": round(float(pos["atr_stop"]), 4),
                    "Price age (seconds)": round(age, 1) if age is not None else "Unknown",
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True)
        else:
            st.info("No active simulated positions.")
    except Exception as exc:
        st.error(f"Could not refresh portfolio state: {exc}")


@st.fragment(run_every=3)
def live_chart(symbol):
    try:
        raw = r.lrange(f"ticks:{symbol}", 0, -1)
        if not raw:
            st.info(f"Waiting for market data for {symbol}.")
            return
        frame = pd.DataFrame([json.loads(item) for item in raw])
        if frame.empty:
            st.info(f"No market data recorded for {symbol}.")
            return
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
        frame["price"] = pd.to_numeric(frame["price"], errors="coerce")
        frame = frame.dropna(subset=["timestamp", "price"]).sort_values("timestamp")
        if frame.empty:
            st.info("No valid ticks available yet.")
            return
        ohlc = frame.set_index("timestamp")["price"].resample("1min").ohlc().dropna()
        if ohlc.empty:
            st.info("Waiting for enough ticks to build candles.")
            return
        fig = go.Figure(data=[go.Candlestick(
            x=ohlc.index, open=ohlc["open"], high=ohlc["high"],
            low=ohlc["low"], close=ohlc["close"]
        )])
        fig.update_layout(
            title=f"{symbol} — 1-minute candles",
            margin=dict(l=10, r=10, t=35, b=10),
            height=420,
            xaxis_rangeslider_visible=False,
            template="plotly_dark",
        )
        st.plotly_chart(fig, use_container_width=True)
    except Exception as exc:
        st.error(f"Could not render chart: {exc}")


tab1, tab2 = st.tabs(["Live charts & portfolio", "Audit trail & analytics"])
with tab1:
    selected_symbol = st.selectbox("Watchlist symbol", WATCHLIST)
    try:
        health = r.get(f"health:last_tick:{selected_symbol}")
        if health is None:
            st.info("Waiting for the collector's first tick.")
        else:
            age = max(0, time.time() - float(health))
            if age > 60:
                st.warning(f"Market feed is stale ({int(age)} seconds since last tick).")
            else:
                st.success(f"Market feed recently received data ({int(age)} seconds ago).")
    except Exception as exc:
        st.warning(f"Could not read feed health: {exc}")
    live_chart(selected_symbol)
    st.subheader("Portfolio")
    portfolio_panel()

with tab2:
    st.subheader("Completed simulated trades")
    try:
        trades = load_trades()
        if trades.empty:
            st.info("No completed trade records are available yet.")
        else:
            sells = trades[trades["action"] == "SELL"]
            wins = int((sells["pnl_usd"] > 0).sum())
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
            st.dataframe(trades.head(100), use_container_width=True)
    except Exception as exc:
        st.error(f"Could not load the audit database: {exc}")
