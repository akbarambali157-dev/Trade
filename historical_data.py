"""Download and normalize Dukascopy XAU/USD history for Meridian's research archive.

This module only downloads historical data to disk. It does not connect to a broker,
place orders, or change the live collector's Redis candles.

Requires Node.js/npm and the documented dukascopy-cli package:
    python historical_data.py --from 2025-01-01 --to 2025-01-08 --timeframe m1
"""
import argparse
import csv
import math
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ARCHIVE_DIR = Path(__file__).resolve().parent / "data" / "historical"
ARCHIVE_FILE = ARCHIVE_DIR / "xauusd_m1.csv"
SUPPORTED_TIMEFRAMES = {"m1", "m5", "m15", "m30", "h1", "h4", "d1"}
FIELDS = ("timestamp", "open", "high", "low", "close", "volume")


def parse_timestamp(value):
    """Normalize a CLI timestamp (Unix milliseconds/seconds or ISO string) to UTC ISO."""
    value = str(value).strip()
    if not value:
        raise ValueError("empty timestamp")
    try:
        number = float(value)
        if math.isfinite(number):
            # Dukascopy-node JSON/CSV examples use Unix milliseconds.
            seconds = number / 1000.0 if abs(number) >= 100_000_000_000 else number
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        pass
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone; refusing to assume local time")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def normalize_row(row):
    """Validate one row from dukascopy-cli CSV output."""
    # Handle either named columns or the common array format:
    # timestamp, open, high, low, close, volume.
    lower = {str(k).strip().lower(): v for k, v in row.items() if k is not None}
    if not all(key in lower for key in ("timestamp", "open", "high", "low", "close")):
        raise ValueError("CSV is missing one or more required OHLC columns")
    timestamp = parse_timestamp(lower["timestamp"])
    values = {key: float(lower[key]) for key in ("open", "high", "low", "close")}
    if not all(math.isfinite(v) and v > 0 for v in values.values()):
        raise ValueError("OHLC values must be finite positive numbers")
    if values["high"] < max(values["open"], values["close"], values["low"]):
        raise ValueError("high is below another candle price")
    if values["low"] > min(values["open"], values["close"], values["high"]):
        raise ValueError("low is above another candle price")
    volume_raw = lower.get("volume", "")
    volume = float(volume_raw) if str(volume_raw).strip() else 0.0
    if not math.isfinite(volume) or volume < 0:
        raise ValueError("volume must be finite and non-negative")
    return {
        "timestamp": timestamp,
        "open": values["open"],
        "high": values["high"],
        "low": values["low"],
        "close": values["close"],
        "volume": volume,
    }


def read_cli_csv(path):
    """Read CSV rows, accepting a headered Dukascopy-node CSV."""
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"{path.name}: missing CSV header")
        return [normalize_row(row) for row in reader]


def merge_archive(rows, archive_path=ARCHIVE_FILE):
    """Merge new candles by timestamp, keeping existing archive rows on duplicates."""
    archive_path = Path(archive_path)
    merged = {}
    if archive_path.exists():
        try:
            for row in read_cli_csv(archive_path):
                merged[row["timestamp"]] = row
        except (ValueError, csv.Error) as exc:
            raise ValueError(f"Existing archive could not be read safely: {exc}") from exc
    # Existing archive rows win on duplicates, so reruns do not overwrite saved data.
    for row in rows:
        merged.setdefault(row["timestamp"], row)
    ordered = [merged[key] for key in sorted(merged)]
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = archive_path.with_suffix(archive_path.suffix + ".tmp")
    with temp_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(ordered)
    os.replace(temp_path, archive_path)
    return len(ordered)


def download_history(date_from, date_to, timeframe="m1", archive_path=ARCHIVE_FILE):
    """Use the maintained CLI interface to fetch history, then validate and archive it."""
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe {timeframe!r}; choose from {sorted(SUPPORTED_TIMEFRAMES)}")
    start = datetime.strptime(date_from, "%Y-%m-%d").date()
    end = datetime.strptime(date_to, "%Y-%m-%d").date()
    if end <= start:
        raise ValueError("--to must be later than --from")

    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        raise RuntimeError("Node.js/npm is required: install Node.js, then reopen your terminal.")
    with tempfile.TemporaryDirectory(prefix="meridian-dukascopy-") as temp_dir:
        command = [
            npx, "--yes", "dukascopy-cli",
            "--instrument", "xauusd",
            "--date-from", start.isoformat(),
            "--date-to", end.isoformat(),
            "--timeframe", timeframe,
            "--format", "csv",
            "--directory", temp_dir,
            "--retries", "2",
            "--retry-pause", "1000",
        ]
        print("Downloading Dukascopy historical candles (paper-research dataset only)…")
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "Downloader returned an error").strip()
            raise RuntimeError(f"Dukascopy download failed: {detail[-1600:]}")
        csv_files = sorted(Path(temp_dir).rglob("*.csv"))
        if not csv_files:
            detail = (result.stdout or result.stderr or "No CSV files were produced").strip()
            raise RuntimeError(f"No CSV output was found. CLI output: {detail[-1000:]}")
        rows = []
        for csv_file in csv_files:
            rows.extend(read_cli_csv(csv_file))
        if not rows:
            raise RuntimeError("The download produced no valid candles.")
        # Deduplicate incoming rows by UTC timestamp, preferring the first downloaded row.
        incoming = {}
        for row in rows:
            incoming.setdefault(row["timestamp"], row)
        total = merge_archive(list(incoming.values()), archive_path)
        print(f"Validated {len(incoming):,} downloaded candles.")
        print(f"Archive now contains {total:,} candles: {Path(archive_path)}")
        print("Check the date range and missing intervals before using the data in any simulation.")
        return total


def main(argv=None):
    parser = argparse.ArgumentParser(description="Download Dukascopy XAU/USD candles into Meridian's historical archive.")
    parser.add_argument("--from", dest="date_from", required=True, help="Start date YYYY-MM-DD (inclusive)")
    parser.add_argument("--to", dest="date_to", required=True, help="End date YYYY-MM-DD (exclusive)")
    parser.add_argument("--timeframe", default="m1", choices=sorted(SUPPORTED_TIMEFRAMES))
    parser.add_argument("--output", default=str(ARCHIVE_FILE), help="Archive CSV path")
    args = parser.parse_args(argv)
    try:
        download_history(args.date_from, args.date_to, args.timeframe, args.output)
    except (ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
