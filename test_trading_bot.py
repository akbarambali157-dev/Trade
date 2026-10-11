import json
import sqlite3

import pytest

from collector import CandleBasedEngine, RealisticPortfolio, fetch_historical_candles


class FakeRedis:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None):
        self.data[key] = str(value)
        return True

    def delete(self, key):
        self.data.pop(key, None)

    def pipeline(self, transaction=True):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, client):
        self.client = client
        self.ops = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def set(self, key, value):
        self.ops.append((key, str(value)))
        return self

    def execute(self):
        for key, value in self.ops:
            self.client.data[key] = value
        return [True] * len(self.ops)


@pytest.fixture
def portfolio(tmp_path):
    client = FakeRedis()
    db = str(tmp_path / "test_trading_bot.db")
    return RealisticPortfolio(redis_client=client, db_path=db), client, db


def test_robust_rsi_flat_series():
    engine = CandleBasedEngine(rsi_period=5)
    assert engine.compute_robust_rsi([100.0] * 6) == 50.0


def test_robust_rsi_gains_only():
    engine = CandleBasedEngine(rsi_period=3)
    assert engine.compute_robust_rsi([10.0, 11.0, 12.0, 13.0]) == 100.0


def test_robust_rsi_losses_only():
    engine = CandleBasedEngine(rsi_period=3)
    assert engine.compute_robust_rsi([13.0, 12.0, 11.0, 10.0]) == 0.0


def test_true_atr_expected_value():
    engine = CandleBasedEngine(atr_period=3)
    candles = [
        {"open": 10, "high": 12, "low": 9, "close": 11},
        {"open": 11, "high": 13, "low": 10, "close": 12},
        {"open": 12, "high": 14, "low": 11, "close": 13},
        {"open": 13, "high": 15, "low": 12, "close": 14},
    ]
    assert engine.compute_true_atr(candles) == pytest.approx(3.0)


def test_atr_requires_sufficient_history():
    engine = CandleBasedEngine(atr_period=14)
    assert engine.compute_true_atr([{"high": 2, "low": 1, "close": 1.5}] * 5) is None


def test_stop_loss_trigger_and_net_pnl(portfolio):
    bot, client, db = portfolio
    bot.positions["AAPL"] = {
        "shares": 10.0, "entry_price": 150.0, "atr_stop": 140.0,
        "entry_commission": 0.75,
    }
    triggered = bot.update_trailing_stops_and_exits("AAPL", 138.0, 1700000000000, current_atr=5.0)
    assert triggered is True
    assert "AAPL" not in bot.positions
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT action, pnl_usd FROM trades ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == "SELL"
    assert row[1] < 0


def test_portfolio_state_round_trip(portfolio):
    bot, client, _ = portfolio
    bot.total_cash = 8123.45
    bot.positions["MSFT"] = {
        "shares": 2.0, "entry_price": 100.0, "atr_stop": 95.0,
        "entry_commission": 0.1,
    }
    bot.save_state()
    restored = RealisticPortfolio(redis_client=client, db_path=bot.db_path)
    assert restored.total_cash == pytest.approx(8123.45)
    assert restored.positions["MSFT"]["shares"] == pytest.approx(2.0)


def test_incomplete_portfolio_state_fails_closed(tmp_path):
    client = FakeRedis()
    client.set("portfolio:cash", "9000")
    with pytest.raises(RuntimeError):
        RealisticPortfolio(redis_client=client, db_path=str(tmp_path / "bad.db"))


def test_cash_sizing_includes_commission(portfolio):
    bot, client, _ = portfolio
    # Make all market data fresh for this symbol.
    client.set("health:last_tick:AAPL", "2000000000")
    bot.total_cash = 1000.0
    bot.process_signal("AAPL", 100.0, 1700000000000, "BUY", "test", 1.0, {"AAPL": 100.0})
    if "AAPL" in bot.positions:
        position = bot.positions["AAPL"]
        assert position["shares"] * position["entry_price"] * (1 + bot.commission_pct) <= 1000.0 + 1e-6



def test_historical_permission_cache_skips_network_request(monkeypatch):
    import time
    import collector

    client = FakeRedis()
    symbol = "AAPL"
    client.set(f"history:blocked_until:{symbol}", str(time.time() + 3600))

    def unexpected_request(*args, **kwargs):
        raise AssertionError("A cached permission denial should skip the HTTP request")

    monkeypatch.setattr(collector, "FINNHUB_KEY", "test-key")
    monkeypatch.setattr(collector.urllib.request, "urlopen", unexpected_request)

    assert fetch_historical_candles(client, symbol) == 0
    assert client.get(f"history:status:{symbol}") == "permission_denied_cached"



def test_meridian_brain_explains_aligned_bullish_conditions():
    from brain import MeridianBrain

    result = MeridianBrain().decide(
        close=105.0, sma=100.0, rsi=60.0, atr=2.0,
        previous_close=104.0, is_owned=False,
    )
    assert result["action"] == "BUY"
    assert result["score"] == 3
    assert result["trend"] == "up"
    assert result["momentum"] == "rising"
    assert result["rsi_state"] == "strong"
    assert "Entry condition" in result["reason"]


def test_meridian_brain_holds_when_signals_conflict():
    from brain import MeridianBrain

    result = MeridianBrain().decide(
        close=101.0, sma=100.0, rsi=45.0, atr=2.0,
        previous_close=102.0, is_owned=False,
    )
    assert result["action"] == "HOLD"
    assert "No sufficiently aligned" in result["reason"]


def test_meridian_brain_sells_owned_position_on_bearish_conditions():
    from brain import MeridianBrain

    result = MeridianBrain().decide(
        close=98.0, sma=100.0, rsi=40.0, atr=2.0,
        previous_close=99.0, is_owned=True,
    )
    assert result["action"] == "SELL"
    assert result["score"] == -3



@pytest.mark.parametrize("invalid_rsi", [-0.1, 100.1, float("inf"), float("nan")])
def test_meridian_brain_holds_for_invalid_rsi(invalid_rsi):
    from brain import MeridianBrain

    result = MeridianBrain().decide(
        close=105.0, sma=100.0, rsi=invalid_rsi, atr=2.0,
        previous_close=104.0, is_owned=False,
    )
    assert result["action"] == "HOLD"
    assert "valid 0–100 range" in result["reason"]
