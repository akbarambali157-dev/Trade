# Trade — paper-trading dashboard

A **simulation-only** market-data and paper-trading prototype. It does not connect to a brokerage or place real orders.

## Components

- `app.py` — read-only Streamlit dashboard for charts, simulated portfolio state, local audit records, and a searchable PDF learning library.
- `collector.py` — separate Finnhub WebSocket collector and simulated portfolio engine.
- `test_trading_bot.py` — unit tests for the trading logic.
- `requirements.txt` — Python dependencies, including PyMuPDF for PDF text extraction and page previews.

The dashboard and collector are separate processes. Streamlit Community Cloud runs the selected Streamlit entrypoint; it does **not** automatically run `collector.py` as a second service.

## Local development

1. Create a virtual environment and install dependencies:

   ```bash
   python -m pip install -r requirements.txt
   ```

2. Set the configuration in your shell or a local `.env` file (do not commit credentials):

   ```dotenv
   FINNHUB_KEY=your_finnhub_key
   REDIS_URL=redis://localhost:6379
   TRADING_DB_PATH=trading_bot.db
   ```

3. Start Redis, then start the collector in one terminal:

   ```bash
   python collector.py
   ```

4. Start the dashboard in another terminal:

   ```bash
   streamlit run app.py
   ```

If the collector runs separately, both processes must use the same Redis instance and compatible configuration.

## Streamlit Community Cloud

1. Push this repository to GitHub.
2. In Streamlit Community Cloud, create an app using this repository, the `main` branch, and `app.py` as the entrypoint.
3. In the app's **Settings → Secrets**, configure the Redis connection. Example TOML:

   ```toml
   REDIS_URL = "rediss://<username>:<password>@<host>:<port>"
   ```

   Use the connection URL and TLS requirements supplied by your Redis provider. Do not paste actual credentials into source files or commit a `.streamlit/secrets.toml` file.
4. Deploy a separate worker service for `collector.py` and configure its `FINNHUB_KEY`, the same `REDIS_URL`, and its database path as service secrets/environment variables.
5. Check the Streamlit app and worker logs after starting both services.

### Important cloud limitations

- **A Redis service is required.** The default `localhost` Redis URL is intentionally not assumed by the cloud dashboard. The dashboard displays setup instructions if `REDIS_URL` is missing.
- **The collector must run elsewhere.** Do not start it inside the Streamlit script: Streamlit can rerun or restart the app, which can create duplicate workers or interrupt collection.
- **SQLite is local to each service.** The collector's `trading_bot.db` will not automatically appear inside Streamlit Community Cloud when they run as separate services. In that setup, the dashboard can show live ticks and portfolio state through Redis, but audit history requires a shared persistent database. The current collector uses SQLite; shared PostgreSQL support has not yet been implemented.
- **Redis persistence depends on your provider's configuration.** The collector stores current portfolio state in Redis, so use a trusted instance with appropriate access controls and persistence settings.
- This is a development prototype. Its strategy and simulated performance have not been validated for real-money use.

## Tests

Run the unit tests locally with:

```bash
pytest -q
```

A successful deployment build only verifies that the app can start; it does not prove that the collector, feed, strategy, or simulated performance is correct.

## PDF learning library

The dashboard includes a **PDF learning library** tab. Upload a PDF (up to 25 MB and 200 pages), parse it, preview pages containing trading charts/diagrams, and search extracted text excerpts. Extracted excerpts and document metadata are stored in Redis so the dashboard can retrieve them later.

- Text-based PDFs can be searched immediately.
- Chart-heavy pages are rendered as images for visual inspection.
- Scanned/image-only PDFs may have no extractable text; OCR is not implemented yet.
- The current search is keyword-based retrieval, not AI model training. It does not automatically infer a trading strategy from a chart or guarantee that the material is correct.
- Redis storage is shared with the dashboard and collector, so monitor your Redis plan's memory limits and only upload material you are allowed to use.

The chart supports touch pinch-to-zoom on compatible mobile browsers, drag-to-pan, toolbar controls, and double-click reset. Gesture behavior can vary slightly by browser/device.


## Dukascopy historical-data research importer

`historical_data.py` downloads Dukascopy XAU/USD candles through the `dukascopy-cli` command-line package and saves a validated, persistent CSV archive. It is intentionally separate from the live collector and Redis rolling candle list.

Requirements:
- Python dependencies from `requirements.txt`
- Node.js and npm installed and available in the terminal (the script uses `npx`)
- Network access to the data source

Example on Windows PowerShell:

```powershell
python historical_data.py --from 2025-01-01 --to 2025-01-08 --timeframe m1
```

The end date is exclusive. Supported timeframes are `m1`, `m5`, `m15`, `m30`, `h1`, `h4`, and `d1`. By default the archive is written to `data/historical/xauusd_m1.csv`; specify `--output` to choose another path. The filename remains the same for other timeframes unless a different output path is supplied.

The importer normalizes timestamps to UTC, validates OHLC values, deduplicates candles, and preserves existing archive rows when a timestamp already exists. It does not yet connect this archive to the dashboard chart or strategy engine; that is a separate integration step. Validate downloaded date coverage, gaps, and source-price differences before using data in offline simulations.

This is for educational historical-data analysis and paper simulations only. The project does not place real orders, and the strategy has not been shown to be profitable.
