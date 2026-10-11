import json
from datetime import datetime, timedelta, timezone

from candle_storage import persist_completed_minute


class CandleRedis:
    def __init__(self):
        self.lists = {}

    def lrange(self, key, start, end):
        values = self.lists.get(key, [])
        return values[start:] if end == -1 and start < 0 else values[start:end + 1]

    def rpush(self, key, *values):
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])

    def ltrim(self, key, start, end):
        values = self.lists.get(key, [])
        self.lists[key] = values[start:] if end == -1 else values[start:end + 1]


def candle(timestamp, price):
    return {
        "timestamp": timestamp.isoformat(timespec="seconds"),
        "open": price,
        "high": price + 1,
        "low": price - 1,
        "close": price + 0.5,
    }


def test_five_minute_candle_created_only_after_complete_bucket(tmp_path):
    client = CandleRedis()
    db = str(tmp_path / "candles.db")
    start = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)

    for minute in range(4):
        persist_completed_minute(client, "OANDA:XAU_USD", candle(start + timedelta(minutes=minute), 100 + minute), db)
    assert client.lists.get("candles:5m:OANDA:XAU_USD", []) == []

    persist_completed_minute(client, "OANDA:XAU_USD", candle(start + timedelta(minutes=4), 104), db)
    rows = [json.loads(value) for value in client.lists["candles:5m:OANDA:XAU_USD"]]
    assert len(rows) == 1
    assert rows[0]["timestamp"] == start.isoformat(timespec="seconds")
    assert rows[0]["open"] == 100
    assert rows[0]["high"] == 105
    assert rows[0]["low"] == 99
    assert rows[0]["close"] == 104.5


def test_missing_minute_does_not_create_false_five_minute_candle(tmp_path):
    client = CandleRedis()
    db = str(tmp_path / "candles.db")
    start = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)

    for minute in (0, 1, 3, 4):
        persist_completed_minute(client, "OANDA:XAU_USD", candle(start + timedelta(minutes=minute), 100 + minute), db)

    assert client.lists.get("candles:5m:OANDA:XAU_USD", []) == []


def test_thirty_minute_aggregation_and_durable_database(tmp_path):
    import sqlite3

    client = CandleRedis()
    db = str(tmp_path / "candles.db")
    start = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)

    for minute in range(30):
        persist_completed_minute(client, "OANDA:XAU_USD", candle(start + timedelta(minutes=minute), 100 + minute), db)

    rows = [json.loads(value) for value in client.lists["candles:30m:OANDA:XAU_USD"]]
    assert len(rows) == 1
    assert rows[0]["open"] == 100
    assert rows[0]["close"] == 129.5

    with sqlite3.connect(db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM market_candles WHERE symbol=? AND timeframe_minutes=1",
            ("OANDA:XAU_USD",),
        ).fetchone()[0]
    assert count == 30
