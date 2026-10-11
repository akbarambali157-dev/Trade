"""Persistent candle storage and strict multi-timeframe aggregation for Meridian.

Completed one-minute candles are stored in SQLite. Five- and thirty-minute candles
are emitted only when every expected one-minute timestamp exists in the bucket.
All timestamps are UTC ISO strings and candles are simulation/research data only.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

DEFAULT_DB_PATH = os.getenv(
    "TRADING_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "trading_bot.db"),
)
TIMEFRAMES = (5, 30)
MAX_REDIS_CANDLES = 5000


def _parse_timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _db_connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_candles (
            symbol TEXT NOT NULL,
            timeframe_minutes INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            PRIMARY KEY (symbol, timeframe_minutes, timestamp)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_candles_lookup "
        "ON market_candles(symbol, timeframe_minutes, timestamp)"
    )
    return conn


def persist_completed_minute(client, symbol, candle, db_path=None):
    """Persist a completed 1m candle and any fully covered 5m/30m buckets.

    A timeframe bucket is skipped when one or more minute timestamps are absent;
    the function never fabricates missing prices to fill a gap.
    """
    path = db_path or DEFAULT_DB_PATH
    timestamp = candle.get("timestamp")
    if not timestamp:
        raise ValueError("Candle timestamp is required")
    start = _parse_timestamp(timestamp).replace(second=0, microsecond=0)
    normalized = {
        "timestamp": start.isoformat(timespec="seconds"),
        "open": float(candle["open"]),
        "high": float(candle["high"]),
        "low": float(candle["low"]),
        "close": float(candle["close"]),
    }
    with _db_connect(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO market_candles "
            "(symbol, timeframe_minutes, timestamp, open, high, low, close) "
            "VALUES (?, 1, ?, ?, ?, ?, ?)",
            (symbol, normalized["timestamp"], normalized["open"], normalized["high"],
             normalized["low"], normalized["close"]),
        )
        for timeframe in TIMEFRAMES:
            bucket_minute = (start.minute // timeframe) * timeframe
            bucket_start = start.replace(minute=bucket_minute, second=0, microsecond=0)
            bucket_end = bucket_start.timestamp() + timeframe * 60
            rows = conn.execute(
                "SELECT timestamp, open, high, low, close FROM market_candles "
                "WHERE symbol = ? AND timeframe_minutes = 1 AND timestamp >= ? "
                "AND timestamp < ? ORDER BY timestamp ASC",
                (symbol, bucket_start.isoformat(timespec="seconds"),
                 datetime.fromtimestamp(bucket_end, timezone.utc).isoformat(timespec="seconds")),
            ).fetchall()
            expected = timeframe
            if len(rows) != expected:
                continue
            times = [_parse_timestamp(row[0]) for row in rows]
            if any((times[i] - times[i - 1]).total_seconds() != 60 for i in range(1, len(times))):
                continue
            aggregated = {
                "timestamp": bucket_start.isoformat(timespec="seconds"),
                "open": float(rows[0][1]),
                "high": max(float(row[2]) for row in rows),
                "low": min(float(row[3]) for row in rows),
                "close": float(rows[-1][4]),
            }
            conn.execute(
                "INSERT OR REPLACE INTO market_candles "
                "(symbol, timeframe_minutes, timestamp, open, high, low, close) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (symbol, timeframe, aggregated["timestamp"], aggregated["open"],
                 aggregated["high"], aggregated["low"], aggregated["close"]),
            )
            key = f"candles:{timeframe}m:{symbol}"
            encoded = json.dumps(aggregated)
            # De-duplicate in Redis before appending, so a replay/restart is safe.
            existing = client.lrange(key, -1, -1)
            if not existing or json.loads(existing[0]).get("timestamp") != aggregated["timestamp"]:
                client.rpush(key, encoded)
                client.ltrim(key, -MAX_REDIS_CANDLES, -1)
    return normalized
