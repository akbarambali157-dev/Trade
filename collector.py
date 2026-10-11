"""Background paper-trading collector.

Importing this module has no network or database side effects. Run with:
    python collector.py
"""
import json
import logging
import math
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

import redis
import websocket
from dotenv import load_dotenv

load_dotenv()
WATCHLIST = ["AAPL", "MSFT", "TSLA", "NVDA"]
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
FINNHUB_KEY = os.getenv("FINNHUB_KEY")
DB_NAME = os.getenv("TRADING_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "trading_bot.db"))
MAX_TICKS_PER_SYMBOL = 500
TICK_FRESH_SECONDS = 120

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("collector")


def utc_timestamp(timestamp_ms=None):
    if timestamp_ms is None:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc).isoformat(timespec="seconds")


def init_db(db_path=DB_NAME):
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                price REAL NOT NULL,
                shares REAL NOT NULL,
                pnl_usd REAL NOT NULL,
                pnl_pct TEXT NOT NULL,
                reason TEXT NOT NULL
            )
        """)
        # Migrate databases created by earlier versions that did not have event_id.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
        if "event_id" not in columns:
            conn.execute("ALTER TABLE trades ADD COLUMN event_id TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_event_id ON trades(event_id)")
        conn.commit()


class RealisticPortfolio:
    """Redis-backed simulated portfolio with guarded state and SQLite audit records."""

    def __init__(self, redis_client=None, db_path=DB_NAME, default_cash=10000.0,
                 atr_multiplier=2.0, risk_pct=0.01, max_aggregate_risk_pct=0.03,
                 commission_pct=0.0005):
        self.redis = redis_client
        self.db_path = db_path
        self.default_cash = float(default_cash)
        self.atr_multiplier = float(atr_multiplier)
        self.risk_pct = float(risk_pct)
        self.max_aggregate_risk_pct = float(max_aggregate_risk_pct)
        self.commission_pct = float(commission_pct)
        self.lock = threading.RLock()
        self.total_cash = self.default_cash
        self.positions = {}
        init_db(self.db_path)
        self.load_state()

    def load_state(self):
        if self.redis is None:
            return
        saved_cash = self.redis.get("portfolio:cash")
        saved_positions = self.redis.get("portfolio:positions")
        if saved_cash is None and saved_positions is None:
            self.total_cash, self.positions = self.default_cash, {}
            self.save_state()
            return
        if saved_cash is None or saved_positions is None:
            raise RuntimeError("Incomplete saved portfolio state; refusing to reset it.")
        try:
            cash = float(saved_cash)
            positions = json.loads(saved_positions)
            if not math.isfinite(cash) or cash < 0 or not isinstance(positions, dict):
                raise ValueError("Invalid portfolio cash or positions.")
            for symbol, pos in positions.items():
                required = ("shares", "entry_price", "atr_stop", "entry_commission")
                if not isinstance(pos, dict) or any(k not in pos for k in required):
                    raise ValueError(f"Invalid saved position for {symbol}.")
                if any(not math.isfinite(float(pos[k])) for k in required):
                    raise ValueError(f"Non-finite saved position value for {symbol}.")
                if float(pos["shares"]) <= 0 or float(pos["entry_price"]) <= 0:
                    raise ValueError(f"Invalid shares or entry price for {symbol}.")
            self.total_cash, self.positions = cash, positions
            log.info("Portfolio restored: cash=$%.2f positions=%s", cash, list(positions))
        except Exception as exc:
            raise RuntimeError("Saved portfolio is invalid; refusing to trade until it is repaired.") from exc

    def save_state(self):
        if self.redis is None:
            return
        # Use a Redis transaction so cash and positions are written together.
        with self.redis.pipeline(transaction=True) as pipe:
            pipe.set("portfolio:cash", repr(self.total_cash))
            pipe.set("portfolio:positions", json.dumps(self.positions))
            pipe.execute()

    def get_current_portfolio_equity(self, latest_prices, now=None):
        with self.lock:
            equity = self.total_cash
            stale = []
            now = time.time() if now is None else now
            for sym, pos in self.positions.items():
                current = latest_prices.get(sym)
                received = None
                if self.redis is not None:
                    raw = self.redis.get(f"health:last_tick:{sym}")
                    try:
                        received = float(raw) if raw is not None else None
                    except (TypeError, ValueError):
                        received = None
                if current is None or received is None or now - received > TICK_FRESH_SECONDS or now < received:
                    stale.append(sym)
                    current = float(pos["entry_price"])
                    if self.redis is not None:
                        self.redis.set(f"health:stale:{sym}", "true", ex=300)
                else:
                    if self.redis is not None:
                        self.redis.delete(f"health:stale:{sym}")
                equity += float(pos["shares"]) * float(current)
            return equity, stale

    def calculate_total_aggregate_risk(self):
        with self.lock:
            total = 0.0
            for pos in self.positions.values():
                entry = float(pos["entry_price"])
                stop = float(pos["atr_stop"])
                shares = float(pos["shares"])
                if not all(math.isfinite(v) for v in (entry, stop, shares)) or shares <= 0 or entry <= 0 or stop <= 0:
                    return math.inf
                # A trailing stop above entry protects profit; it contributes zero downside risk.
                total += shares * max(entry - stop, 0.0)
            return total

    def log_trade(self, timestamp, symbol, action, price, shares, pnl_usd, pnl_pct, reason):
        event_id = f"{timestamp}|{symbol}|{action}|{price:.8f}|{shares:.8f}"
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("""
                INSERT OR IGNORE INTO trades
                (event_id, timestamp, symbol, action, price, shares, pnl_usd, pnl_pct, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (event_id, timestamp, symbol, action, float(price), float(shares),
                  float(pnl_usd), str(pnl_pct), str(reason)))
            conn.commit()

    def update_trailing_stops_and_exits(self, symbol, current_price, timestamp, current_atr):
        with self.lock:
            if symbol not in self.positions:
                return False
            pos = self.positions[symbol]
            stop = float(pos["atr_stop"])
            if current_price <= stop:
                shares = float(pos["shares"])
                gross_exit = shares * current_price
                net_proceeds = gross_exit * (1.0 - self.commission_pct)
                total_entry_cost = shares * float(pos["entry_price"]) + float(pos["entry_commission"])
                pnl = net_proceeds - total_entry_cost
                pnl_pct = pnl / total_entry_cost * 100.0 if total_entry_cost else 0.0
                self.log_trade(utc_timestamp(timestamp), symbol, "SELL", current_price, shares,
                               round(pnl, 2), f"{pnl_pct:+.2f}%", f"ATR stop triggered at {pnl_pct:+.2f}%")
                self.total_cash += net_proceeds
                del self.positions[symbol]
                self.save_state()
                return True
            if math.isfinite(current_atr) and current_atr > 0:
                proposed = current_price - current_atr * self.atr_multiplier
                if proposed > stop:
                    pos["atr_stop"] = proposed
                    self.save_state()
            return False

    def process_signal(self, symbol, price, timestamp, decision, reason, current_atr, latest_prices=None):
        if not (isinstance(price, (int, float)) and math.isfinite(price) and price > 0):
            return
        latest_prices = dict(latest_prices or {})
        latest_prices[symbol] = price
        with self.lock:
            equity, stale = self.get_current_portfolio_equity(latest_prices)
            if self.update_trailing_stops_and_exits(symbol, price, timestamp, current_atr):
                return
            dt = utc_timestamp(timestamp)
            if decision == "BUY" and symbol not in self.positions:
                if symbol in stale or not (math.isfinite(current_atr) and current_atr > 0):
                    log.warning("Rejecting BUY for %s: stale data or invalid ATR", symbol)
                    return
                risk_per_share = max(current_atr * self.atr_multiplier, price * 0.01)
                if not math.isfinite(risk_per_share) or risk_per_share <= 0:
                    return
                risk_budget = equity * self.risk_pct
                risk_shares = risk_budget / risk_per_share
                cash_shares = self.total_cash / (price * (1.0 + self.commission_pct))
                shares = min(risk_shares, cash_shares)
                if not math.isfinite(shares) or shares <= 0:
                    return
                new_risk = shares * risk_per_share
                max_risk = equity * self.max_aggregate_risk_pct
                if self.calculate_total_aggregate_risk() + new_risk > max_risk:
                    log.warning("Rejecting BUY for %s: aggregate risk cap reached", symbol)
                    return
                cost = shares * price * (1.0 + self.commission_pct)
                if cost > self.total_cash:
                    return
                pos = {"shares": shares, "entry_price": price,
                       "atr_stop": price - risk_per_share,
                       "entry_commission": shares * price * self.commission_pct}
                # Record first; do not mutate portfolio if the audit write fails.
                self.log_trade(dt, symbol, "BUY", price, shares, 0.0, "0.00%", reason)
                self.positions[symbol] = pos
                self.total_cash -= cost
                self.save_state()
            elif decision == "SELL" and symbol in self.positions:
                pos = self.positions[symbol]
                shares = float(pos["shares"])
                proceeds = shares * price * (1.0 - self.commission_pct)
                entry_cost = shares * float(pos["entry_price"]) + float(pos["entry_commission"])
                pnl = proceeds - entry_cost
                pnl_pct = pnl / entry_cost * 100.0 if entry_cost else 0.0
                self.log_trade(dt, symbol, "SELL", price, shares, round(pnl, 2), f"{pnl_pct:+.2f}%", reason)
                self.total_cash += proceeds
                del self.positions[symbol]
                self.save_state()


class CandleBasedEngine:
    def __init__(self, sma_period=20, rsi_period=14, oversold_rsi=30.0, atr_period=14):
        self.sma_period = int(sma_period)
        self.rsi_period = int(rsi_period)
        self.oversold_rsi = float(oversold_rsi)
        self.atr_period = int(atr_period)
        if min(self.sma_period, self.rsi_period, self.atr_period) < 1:
            raise ValueError("Indicator periods must be positive.")
        self.candle_history = {sym: [] for sym in WATCHLIST}
        self.current_candles = {}

    def compute_robust_rsi(self, closes):
        if len(closes) < self.rsi_period + 1:
            return 50.0
        deltas = [float(closes[i]) - float(closes[i - 1]) for i in range(1, len(closes))]
        gains = [max(d, 0.0) for d in deltas[-self.rsi_period:]]
        losses = [max(-d, 0.0) for d in deltas[-self.rsi_period:]]
        avg_gain, avg_loss = sum(gains) / self.rsi_period, sum(losses) / self.rsi_period
        if avg_gain == 0 and avg_loss == 0:
            return 50.0
        if avg_loss == 0:
            return 100.0
        if avg_gain == 0:
            return 0.0
        return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    def compute_true_atr(self, candles):
        if len(candles) < self.atr_period + 1:
            return None
        true_ranges = []
        for i in range(1, len(candles)):
            curr, prev = candles[i], candles[i - 1]
            tr = max(float(curr["high"]) - float(curr["low"]),
                     abs(float(curr["high"]) - float(prev["close"])),
                     abs(float(curr["low"]) - float(prev["close"])))
            if not math.isfinite(tr) or tr < 0:
                return None
            true_ranges.append(tr)
        if len(true_ranges) < self.atr_period:
            return None
        return sum(true_ranges[-self.atr_period:]) / self.atr_period

    def process_incoming_tick(self, symbol, price, timestamp, is_owned=False):
        if symbol not in self.candle_history:
            self.candle_history[symbol] = []
        minute_key = int(timestamp // 60000)
        current = self.current_candles.get(symbol)
        if current is None or current["minute"] != minute_key:
            if current is not None:
                self.candle_history[symbol].append({k: current[k] for k in ("open", "high", "low", "close")})
                self.candle_history[symbol] = self.candle_history[symbol][-250:]
            self.current_candles[symbol] = {
                "minute": minute_key, "open": price, "high": price, "low": price, "close": price
            }
        else:
            current["high"] = max(current["high"], price)
            current["low"] = min(current["low"], price)
            current["close"] = price
        candles = self.candle_history[symbol]
        closes = [c["close"] for c in candles]
        required = max(self.sma_period, self.rsi_period + 1, self.atr_period + 1)
        if len(candles) < required:
            return "HOLD", "Accumulating completed 1-minute candles", None
        sma = sum(closes[-self.sma_period:]) / self.sma_period
        rsi = self.compute_robust_rsi(closes)
        atr = self.compute_true_atr(candles)
        if atr is None or not math.isfinite(atr) or atr <= 0:
            return "HOLD", "Waiting for valid ATR", None
        close = closes[-1]
        if not is_owned and close < sma and rsi <= self.oversold_rsi:
            return "BUY", f"Close below SMA and RSI oversold ({rsi:.1f}); ATR {atr:.4f}", atr
        if is_owned and close > sma:
            return "SELL", f"Close above SMA; ATR {atr:.4f}", atr
        return "HOLD", f"SMA {sma:.4f} | RSI {rsi:.1f} | ATR {atr:.4f}", atr


def run_collector():
    if not FINNHUB_KEY:
        raise RuntimeError("Missing FINNHUB_KEY environment variable.")
    client = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=3.0)
    client.ping()
    log.info("Connected to Redis")
    portfolio = RealisticPortfolio(redis_client=client)
    engine = CandleBasedEngine()
    latest_prices = {}
    last_timestamps = {sym: 0 for sym in WATCHLIST}
    state_lock = threading.RLock()

    def on_message(ws, message):
        try:
            data = json.loads(message)
            if data.get("type") != "trade" or not isinstance(data.get("data"), list):
                return
            for trade in data["data"]:
                symbol, price, timestamp = trade.get("s"), trade.get("p"), trade.get("t")
                if symbol not in WATCHLIST:
                    continue
                if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
                    continue
                if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or timestamp <= 0:
                    continue
                if timestamp < last_timestamps[symbol]:
                    continue
                last_timestamps[symbol] = timestamp
                received_at = time.time()
                dt = utc_timestamp(timestamp)
                tick = {"timestamp": dt, "price": float(price), "volume": trade.get("v", 0)}
                with state_lock:
                    latest_prices[symbol] = float(price)
                client.set(f"health:last_tick:{symbol}", str(received_at), ex=3600)
                client.rpush(f"ticks:{symbol}", json.dumps(tick))
                client.ltrim(f"ticks:{symbol}", -MAX_TICKS_PER_SYMBOL, -1)
                owned = symbol in portfolio.positions
                decision, reason, atr = engine.process_incoming_tick(symbol, float(price), timestamp, owned)
                # Evaluate stop-losses and trail stops on every valid tick, even when
                # the strategy's entry/exit signal is HOLD.
                if atr is not None:
                    portfolio.process_signal(symbol, float(price), timestamp, decision, reason, atr, latest_prices)
        except Exception:
            log.exception("Message processing failed")

    def on_open(ws):
        log.info("Connected to Finnhub; subscribing to %s", WATCHLIST)
        for sym in WATCHLIST:
            ws.send(json.dumps({"type": "subscribe", "symbol": sym}))
            time.sleep(0.05)

    def on_error(ws, error):
        log.error("WebSocket error: %s", error)

    def on_close(ws, status_code, message):
        log.warning("WebSocket closed (%s): %s", status_code, message)

    socket_url = f"wss://ws.finnhub.io?token={FINNHUB_KEY}"
    retry_delay = 1
    while True:
        try:
            log.info("Connecting to Finnhub")
            ws = websocket.WebSocketApp(socket_url, on_open=on_open, on_message=on_message,
                                        on_error=on_error, on_close=on_close)
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception:
            log.exception("Collector connection failed")
        log.info("Reconnecting in %s seconds", retry_delay)
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 60)


if __name__ == "__main__":
    run_collector()
