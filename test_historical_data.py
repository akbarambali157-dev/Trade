import csv
from pathlib import Path

import pytest

from historical_data import merge_archive, normalize_row, parse_timestamp, read_cli_csv


def test_parse_unix_milliseconds_to_utc():
    assert parse_timestamp("1735689600000") == "2025-01-01T00:00:00+00:00"


def test_parse_iso_timestamp_normalizes_timezone():
    assert parse_timestamp("2025-01-01T01:00:00+01:00") == "2025-01-01T00:00:00+00:00"


def test_naive_timestamp_is_rejected():
    with pytest.raises(ValueError, match="no timezone"):
        parse_timestamp("2025-01-01T00:00:00")


def test_normalize_valid_candle():
    row = normalize_row({
        "timestamp": "1735689600000",
        "open": "2600.0", "high": "2610.0", "low": "2590.0",
        "close": "2605.0", "volume": "10",
    })
    assert row["timestamp"] == "2025-01-01T00:00:00+00:00"
    assert row["high"] == 2610.0
    assert row["volume"] == 10.0


@pytest.mark.parametrize("high,low", [("2599", "2590"), ("2610", "2611")])
def test_invalid_candle_range_is_rejected(high, low):
    with pytest.raises(ValueError):
        normalize_row({
            "timestamp": "1735689600000",
            "open": "2600", "high": high, "low": low,
            "close": "2605", "volume": "0",
        })


def test_archive_merge_is_sorted_and_existing_duplicate_wins(tmp_path):
    archive = tmp_path / "archive.csv"
    existing = normalize_row({
        "timestamp": "1735689600000",
        "open": "2600", "high": "2610", "low": "2590",
        "close": "2605", "volume": "10",
    })
    assert merge_archive([existing], archive) == 1
    duplicate = dict(existing, close=9999.0)
    later = dict(existing, timestamp="2025-01-01T00:01:00+00:00", close=2606.0)
    assert merge_archive([duplicate, later], archive) == 2
    rows = read_cli_csv(archive)
    assert len(rows) == 2
    assert rows[0]["close"] == 2605.0
    assert rows[1]["timestamp"] == "2025-01-01T00:01:00+00:00"


def test_header_only_csv_returns_no_rows(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("timestamp,open,high,low,close,volume\n", encoding="utf-8")
    assert read_cli_csv(path) == []
