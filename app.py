import os
import json
import time
import re
import threading
from datetime import datetime
import websocket
from dotenv import load_dotenv
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from pypdf import PdfReader
import redis

load_dotenv()
FINNHUB_KEY = os.getenv("FINNHUB_KEY")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

# Initialize Redis Connection
try:
    r = redis.from_url(REDIS_URL, decode_responses=True)
    r.ping()
    print("Successfully connected to Redis!")
except Exception as e:
    print(f"Redis connection error: {e}")

WATCHLIST = ["AAPL", "MSFT", "TSLA", "NVDA"]


# --- 1. MULTI-SYMBOL PORTFOLIO & ATR RISK MANAGER ---
class AdvancedPortfolio:
    def __init__(self, initial_cash=10000.0, atr_multiplier=2.0):
        self.total_cash = initial_cash
        self.initial_cash = initial_cash
        self.positions = {}  # {symbol: {"shares": float, "entry_price": float, "atr_stop": float}}
        self.atr_multiplier = atr_multiplier

    def check_atr_stop(self, symbol, current_price, timestamp):
        if symbol in self.positions:
            pos = self.positions[symbol]
            if current_price <= pos["atr_stop"]:
                dt = datetime.fromtimestamp(timestamp / 1000.0).strftime('%Y-%m-%d %H:%M:%S')
                exit_val = pos["shares"] * current_price
                pnl_usd = exit_val - (pos["shares"] * pos["entry_price"])
                pnl_pct = ((current_price - pos["entry_price"]) / pos["entry_price"]) * 100
                
                self.total_cash += exit_val
                reason = f"ATR VOLATILITY STOP TRIGGERED at {pnl_pct:+.2f}%"
                self.log_trade(dt, symbol, "SELL", current_price, pos["shares"], round(pnl_usd, 2), f"{pnl_pct:+.2f}%", reason)
                
                del self.positions[symbol]
                return True
        return False

    def process_signal(self, symbol, price, timestamp, decision, reason, current_atr):
        if self.check_atr_stop(symbol, price, timestamp):
            return

        dt = datetime.fromtimestamp(timestamp / 1000.0).strftime('%Y-%m-%d %H:%M:%S')
        
        if decision == "BUY" and symbol not in self.positions:
            allocated_cash = self.total_cash / max(len(WATCHLIST) - len(self.positions), 1)
            if allocated_cash > self.total_cash:
                allocated_cash = self.total_cash
                
            if allocated_cash > 10:
                shares = allocated_cash / price
                atr_stop_price = price - (current_atr * self.atr_multiplier)
                self.positions[symbol] = {
                    "shares": shares, 
                    "entry_price": price, 
                    "atr_stop": atr_stop_price
                }
                self.total_cash -= allocated_cash
                self.log_trade(dt, symbol, "BUY", price, shares, 0.0, "0.0%", reason)
            
        elif decision == "SELL" and symbol in self.positions:
            pos = self.positions[symbol]
            exit_val = pos["shares"] * price
            pnl_usd = exit_val - (pos["shares"] * pos["entry_price"])
            pnl_pct = ((price - pos["entry_price"]) / pos["entry_price"]) * 100
            
            self.total_cash += exit_val
            self.log_trade(dt, symbol, "SELL", price, pos["shares"], round(pnl_usd, 2), f"{pnl_pct:+.2f}%", reason)
            del self.positions[symbol]

    def log_trade(self, timestamp, symbol, action, price, shares, pnl_usd, pnl_pct, reason):
        trade_entry = json.dumps({
            "timestamp": timestamp,
            "symbol": symbol,
            "action": action,
            "price": price,
            "shares": shares,
            "pnl_usd": pnl_usd,
            "pnl_pct": pnl_pct,
            "reason": reason
        })
        r.rpush("trades", trade_entry)

    def get_portfolio_value(self, latest_prices):
        val = self.total_cash
        for sym, pos in self.positions.items():
            current_p = latest_prices.get(sym, pos["entry_price"])
            val += pos["shares"] * current_p
        return val


portfolio = AdvancedPortfolio()


# --- 2. ENHANCED REASONING ENGINE (SMA + RSI + ATR) ---
class AdvancedEngine:
    def __init__(self, sma_period=20, rsi_period=14, oversold_rsi=30.0, atr_period=14):
        self.sma_period = sma_period
        self.rsi_period = rsi_period
        self.oversold_rsi = oversold_rsi
        self.atr_period = atr_period
        self.price_history = {sym: [] for sym in WATCHLIST}

    def calculate_indicators(self, prices):
        if len(prices) < max(self.sma_period, self.rsi_period, self.atr_period) + 1:
            return None, 50.0, 1.0

        sma = sum(prices[-self.sma_period:]) / self.sma_period

        deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        gains = [d if d > 0 else 0 for d in deltas]
        losses = [-d if d < 0 else 0 for d in deltas]
        avg_gain = sum(gains[-self.rsi_period:]) / self.rsi_period
        avg_loss = sum(losses[-self.rsi_period:]) / self.rsi_period
        rsi = 100.0 if avg_loss == 0 else 100 - (100 / (1 + (avg_gain / avg_loss)))

        high_lows = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
        atr = sum(high_lows[-self.atr_period:]) / self.atr_period if len(high_lows) >= self.atr_period else 1.0

        return sma, rsi, max(atr, 0.5)

    def evaluate(self, symbol, current_price):
        if symbol not in self.price_history:
            self.price_history[symbol] = []
            
        history = self.price_history[symbol]
        history.append(current_price)
        if len(history) > 100:
            history.pop(0)

        indicators = self.calculate_indicators(history)
        if not indicators:
            return "HOLD", "Accumulating deep price metrics...", 1.0

        sma, rsi, atr = indicators
        is_owned = symbol in portfolio.positions

        if not is_owned and current_price < sma and rsi <= self.oversold_rsi:
            return "BUY", f"Price < SMA & RSI oversold ({rsi:.1f}) | ATR: {atr:.2f}", atr
        elif is_owned and current_price > sma:
            return "SELL", f"Price recovered above SMA | ATR: {atr:.2f}", atr
        
        return "HOLD", f"SMA: {sma:.2f} | RSI: {rsi:.1f} | ATR: {atr:.2f}", atr


strategy_engine = AdvancedEngine()


# --- 3. HIGH-PERFORMANCE REDIS WEBSOCKET THREAD ---
def run_websocket_bot():
    def on_message(ws, message):
        data = json.loads(message)
        if data.get("type") == "trade":
            for trade in data["data"]:
                symbol = trade["s"]
                if symbol in WATCHLIST:
                    price = trade["p"]
                    volume = trade["v"]
                    timestamp = trade["t"]
                    dt = datetime.fromtimestamp(timestamp / 1000.0).strftime('%Y-%m-%d %H:%M:%S')

                    # Push tick to Redis List
                    tick_entry = json.dumps({"timestamp": dt, "price": price, "volume": volume})
                    r.rpush(f"ticks:{symbol}", tick_entry)
                    # Keep only last 500 ticks per symbol in Redis memory
                    r.ltrim(f"ticks:{symbol}", -500, -1)

                    decision, reason, atr_val = strategy_engine.evaluate(symbol, price)
                    portfolio.process_signal(symbol, price, timestamp, decision, reason, atr_val)

    def on_open(ws):
        print("Connected to Finnhub Live Stream via Redis Cloud!")
        for sym in WATCHLIST:
            ws.send(json.dumps({"type": "subscribe", "symbol": sym}))
            time.sleep(0.05)

    socket_url = f"wss://ws.finnhub.io?token={FINNHUB_KEY}"
    while True:
        try:
            ws = websocket.WebSocketApp(socket_url, on_open=on_open, on_message=on_message)
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception:
            pass
        time.sleep(5)

if "bot_started" not in st.session_state:
    st.session_state.bot_started = True
    threading.Thread(target=run_websocket_bot, daemon=True).start()


# --- 4. STREAMLIT DASHBOARD UI ---
st.set_page_config(page_title="Cloud Redis Trading Bot", layout="wide")
st.title("⚡ Cloud Redis Quant Trading Bot & Dashboard")

# Sidebar
st.sidebar.header("📁 Strategy PDF Parser")
uploaded_file = st.sidebar.file_uploader("Upload Strategy PDF", type=["pdf"])

if uploaded_file is not None:
    try:
        reader = PdfReader(uploaded_file)
        pdf_text = ""
        for page in reader.pages:
            pdf_text += page.extract_text()
        st.sidebar.success("PDF Parsed Successfully!")
        
        rsi_match = re.search(r'rsi.*?(\d+)', pdf_text, re.IGNORECASE)
        if rsi_match:
            extracted_rsi = int(rsi_match.group(1))
            strategy_engine.oversold_rsi = float(extracted_rsi)
            st.sidebar.info(f"Auto-configured RSI Threshold: {extracted_rsi}")
    except Exception as e:
        st.sidebar.error(f"Error parsing PDF: {e}")

st.sidebar.divider()
st.sidebar.header("⚙️ Risk Controls")
atr_slider = st.sidebar.slider("ATR Stop-Loss Multiplier", 1.0, 4.0, 2.0, 0.2)
portfolio.atr_multiplier = atr_slider

# Portfolio Metrics Header
st.subheader("Simulated Portfolio Overview")
col1, col2, col3 = st.columns(3)

# Fetch latest prices from Redis for portfolio calculation
latest_prices_cache = {}
for sym in WATCHLIST:
    last_tick = r.lrange(f"ticks:{sym}", -1, -1)
    if last_tick:
        tick_data = json.loads(last_tick[0])
        latest_prices_cache[sym] = tick_data["price"]

total_equity = portfolio.get_portfolio_value(latest_prices_cache)

raw_trades = r.lrange("trades", 0, -1)
df_trades = pd.DataFrame([json.loads(t) for t in raw_trades]) if raw_trades else pd.DataFrame()

total_trades = len(df_trades[df_trades["action"] == "SELL"]) if not df_trades.empty else 0
winning_trades = len(df_trades[(df_trades["action"] == "SELL") & (df_trades["pnl_usd"] > 0)]) if not df_trades.empty else 0
win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0.0

col1.metric("Total Portfolio Equity", f"${total_equity:,.2f}")
col2.metric("Total Completed Trades", total_trades)
col3.metric("Strategy Win Rate", f"{win_rate:.1f}%")

# Main Interface Tabs
tab1, tab2 = st.tabs(["📈 Live Charts & Watchlist", "🔍 Audit Trail & Analytics"])

with tab1:
    selected_symbol = st.selectbox("Select Asset Watchlist:", WATCHLIST)
    chart_placeholder = st.empty()
    
    st.subheader("Active Paper Positions")
    positions_placeholder = st.empty()

with tab2:
    st.subheader("Redis Audit Trail & Report Export")
    
    if not df_trades.empty:
        csv_bytes = df_trades.to_csv(index=False).encode('utf-8')
        st.download_button(
            label="📥 Download Cloud Audit Report (CSV)",
            data=csv_bytes,
            file_name="redis_trading_audit_report.csv",
            mime="text/csv"
        )
    
    audit_placeholder = st.empty()

# UI Refresh Loop
while True:
    # Fetch live ticks from Redis for chart rendering
    raw_ticks = r.lrange(f"ticks:{selected_symbol}", 0, -1)
    if raw_ticks:
        sym_ticks = pd.DataFrame([json.loads(t) for t in raw_ticks])
        if not sym_ticks.empty:
            sym_ticks["timestamp"] = pd.to_datetime(sym_ticks["timestamp"])
            sym_df = sym_ticks.set_index("timestamp")
            if len(sym_df) > 5:
                ohlc = sym_df['price'].resample('1min').ohlc().dropna()
                if not ohlc.empty:
                    fig = go.Figure(data=[go.Candlestick(
                        x=ohlc.index, open=ohlc['open'], high=ohlc['high'],
                        low=ohlc['low'], close=ohlc['close'],
                        increasing_line_color='#26a69a', decreasing_line_color='#ef5350'
                    )])
                    fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), height=400, xaxis_rangeslider_visible=False, template="plotly_dark")
                    chart_placeholder.plotly_chart(fig, use_container_width=True)

    if portfolio.positions:
        pos_data = [{"Symbol": s, "Shares": round(p["shares"], 4), "Entry Price": f"${p['entry_price']:.2f}", "ATR Stop": f"${p['atr_stop']:.2f}"} for s, p in portfolio.positions.items()]
        positions_placeholder.dataframe(pd.DataFrame(pos_data), use_container_width=True)
    else:
        positions_placeholder.info("No active open positions currently.")

    fresh_trades = r.lrange("trades", 0, -1)
    if fresh_trades:
        df_live_trades = pd.DataFrame([json.loads(t) for t in fresh_trades])
        audit_placeholder.dataframe(df_live_trades.tail(15), use_container_width=True)

    time.sleep(2)
