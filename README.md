# NIFTY IQ — Next-Day NIFTY 50 Research System

## Architecture and current implementation

- Frontend: React + Vite static site on Render.
- API: FastAPI on Render.
- Persistence: portable PostgreSQL through `DATABASE_URL`. **Recommended free-first target: Neon Postgres**, whose current published Free plan includes 1 GB database storage per project and 100 compute-hours per month; monitor usage and keep independent exports. Render Free Postgres is not suitable for long-term history because Render documents a 30-day expiry. The API can be moved by changing `DATABASE_URL` without changing application code.
- Browser vault: IndexedDB holds imported source files locally. Nothing is sent to the cloud until the user enters the configured import token and presses **Sync local files to cloud**.
- Cloud archive: immutable source-file provenance (URL, date, retrieval time and SHA-256; compressed raw files are not retained in the database) plus row-level deduplication by dataset, trading date and row hash. Re-uploading the same file is idempotent.
- Feature store: date-keyed daily feature snapshots are versioned by feature schema and source fingerprint, so later daily runs can update only the newest session.- Model registry: database schema is prepared for versioned model metadata, binary artifacts, feature schema, validation metrics and training cutoff.
- Prediction registry: schema is prepared for daily scenarios and later outcome evaluation.
- Official-source downloader: NIFTY 50 OHLC and India VIX use NSE Indices historical endpoints; derivatives reports are requested through NSE's report archive and stored with raw bytes, source URL, report date and SHA-256.
- Scheduler: GitHub Actions runs the protected ingestion endpoint at 19:15 and 20:00 IST on weekdays. A missing report is logged as a partial run; no forecast is published without a validated model.
- Audit: import and pipeline activity is recorded.

## Five required datasets

Official source catalogue: [NSE Indices historical index data](https://www.niftyindices.com/reports/historical-data) exposes index OHLC in CSV format. [NSE Equity reports](https://www.nseindia.com/all-reports) and [NSE Derivatives reports](https://www.nseindia.com/all-reports-derivatives) list daily bhavcopy, participant OI, FII derivatives statistics, settlement and volatility reports. These sources are catalogued by the API; automatic download adapters are not yet enabled.

1. NIFTY 50 daily OHLC history
2. F&O bhavcopy
3. Participant-wise open interest
4. FII derivatives statistics
5. India VIX and settlement / volatility data

## Incremental operating model

1. One-time historical backfill: import and validate the five-year history; deduplicate; normalize source-specific fields; create features; run chronological walk-forward validation; train and register model versions.
2. Daily after market close: fetch only newly published source files; record source URL, retrieval timestamp and SHA-256; validate and upsert new rows; update rolling features; load the latest validated model; save a dated report and its input cutoff.
3. Retraining: run on a controlled periodic schedule and promote a new version only after out-of-sample comparison.
4. Missing or invalid source data must stop the affected prediction cycle. Never fill gaps with invented market values or probabilities.

## Cloud import setup

1. Create a Neon PostgreSQL project, copy its pooled or direct connection string, and set `DATABASE_URL` in the Render API service. Neon Free currently advertises 1 GB database storage and 100 CU-hours per month; this is suitable for initial development, not a guarantee of unlimited free capacity.
2. Set a strong random `INGEST_API_TOKEN` environment variable on the API service.
3. Set a separate random `SCHEDULER_TOKEN` in the Render API service.
4. In GitHub repository Settings → Secrets and variables → Actions, add `NIFTY_IQ_SCHEDULER_TOKEN` with exactly the same value as Render's `SCHEDULER_TOKEN`.
5. Set a strong `INGEST_API_TOKEN` in Render for manual file upload and historical feature backfill.
6. Redeploy the API after setting the variables. In the app's NSE Data Centre, enter the ingest token and choose **Sync local files to cloud**. This is an explicit upload; the local browser vault remains intact.
7. Run the **NIFTY IQ evening data ingestion** workflow manually once from GitHub Actions to verify the connection. It is also scheduled for 19:15 and 20:00 IST, Monday to Friday.
8. After the five-year NIFTY OHLC history has been imported, call `POST /api/v1/pipeline/features/backfill` with the `X-Ingest-Token` header once. This creates historical technical feature snapshots; it does not train a model.
9. Check `/api/v1/system/status`, `/api/v1/datasets`, `/api/v1/pipeline/readiness`, `/api/v1/pipeline/runs` and `/api/v1/pipeline/source-downloads`.

The token is sent only as an HTTPS request header and is held in the current browser page state. Do not commit it to GitHub or put it in frontend build variables.

## API endpoints

- `GET /health`
- `GET /api/v1/system/status`
- `GET /api/v1/sources`
- `GET /api/v1/datasets`
- `POST /api/v1/datasets/import` — protected by `X-Ingest-Token`; CSV upload
- `GET /api/v1/pipeline/readiness`
- `POST /api/v1/jobs/evening` — protected by `X-Scheduler-Token`; downloads official daily source files
- `POST /api/v1/pipeline/features/backfill` — protected by `X-Ingest-Token`; one-time historical feature generation
- `GET /api/v1/pipeline/runs`
- `GET /api/v1/pipeline/source-downloads`
- `GET /api/v1/models`
- `GET /api/v1/predictions/latest`
- `GET /api/v1/audit`

## Important status

The cloud archive and model/prediction registry are infrastructure, not proof that a forecast exists. NSE EOD source adapters and the scheduled ingestion workflow are implemented but still require live verification with configured secrets and database. Source-specific semantic validation, full historical feature validation, model training, walk-forward evaluation and forecast report generation remain pending. The source catalogue is not a live feed. No model is trained and no forecast is generated by importing files alone.

## Local development

```bash
npm install
npm run dev
```

Backend:

```bash
pip install -r requirements.txt
uvicorn backend.main:app --reload
```

Research software only. NIFTY 50 scope. No broker integration, order execution, or fabricated market data.
