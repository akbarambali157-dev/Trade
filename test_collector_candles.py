import json

from collector import store_minute_candle, utc_timestamp


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.lists = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    def ltrim(self, key, start, end):
        values = self.lists.get(key, [])
        self.lists[key] = values[start:] if end == -1 else values[start:end + 1]


def test_out_of_order_tick_does_not_roll_current_candle_back():
    client = FakeRedis()
    symbol = "TEST"
    current_ms = 1_735_689_720_000  # 2025-01-01 00:02:00 UTC
    older_ms = current_ms - 60_000

    assert store_minute_candle(client, symbol, current_ms, 100.0) is True
    original = json.loads(client.get(f"candles:current:{symbol}"))

    assert store_minute_candle(client, symbol, older_ms, 90.0) is False
    assert json.loads(client.get(f"candles:current:{symbol}")) == original
    assert client.lists.get(f"candles:1m:{symbol}", []) == []


def test_tick_in_current_minute_updates_high_low_and_close():
    client = FakeRedis()
    symbol = "TEST"
    minute_ms = 1_735_689_720_000

    store_minute_candle(client, symbol, minute_ms, 100.0)
    assert store_minute_candle(client, symbol, minute_ms + 10_000, 105.0) is True
    candle = json.loads(client.get(f"candles:current:{symbol}"))

    assert candle["timestamp"] == utc_timestamp(minute_ms)
    assert candle["open"] == 100.0
    assert candle["high"] == 105.0
    assert candle["low"] == 100.0
    assert candle["close"] == 105.0
