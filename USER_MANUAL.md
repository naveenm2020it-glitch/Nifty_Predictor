# NIFTY IQ — Web Application User Manual

**Live application:** https://nifty-next-day-prediction.onrender.com  
**API health / documentation:** https://nifty-predictor-api-0gl1.onrender.com/health and https://nifty-predictor-api-0gl1.onrender.com/docs  
**Repository:** https://github.com/naveenm2020it-glitch/Nifty_Predictor

Last reviewed: 10 November 2026

---

## 1. Important: what is ready today

The website and API are deployed. The current working scope includes the research workspace, browser-local file vault, protected cloud archive, official-source daily ingestion, and a new one-click five-year historical download job. The historical downloader has been added to the codebase and requires a configured database/token plus a successful live test before its completeness can be confirmed.

**This is not yet a completed next-day prediction product.** The walk-forward training and next-session research baseline are implemented. A model is not available until you import real data, build features and run training; it is registered as validated only if it passes out-of-sample sample-count, fold-count, balanced-accuracy, Brier-score and majority-baseline gates. The prediction and model-performance screens read from the API. Options, participant, futures and pattern screens still require further semantic mapping and dedicated models; do not treat them as live trading signals.

No broker connection, order placement or trade execution is included.

## 2. Open the website and navigate

1. Open the Live application link above in your desktop browser.
2. The application opens on the NSE Data Centre screen.
3. Use the left navigation to open:
   - **Market overview** — workspace summary; values remain blank until verified data/model output exists.
   - **Next-day prediction** — forecast area; currently no model forecast is available.
   - **Options intelligence**, **FII & participant flow**, **Futures & volatility**, **Pattern intelligence**, **Scenario planner** — research areas awaiting validated data/model integration.
   - **NSE data centre** — import, inspect and sync datasets. This is the main currently usable workflow.
   - **Model performance** — currently no validated model metrics are available.
4. Use the moon/sun control in the top bar to switch dark/light appearance.
5. Use the sidebar collapse control to gain workspace width. On a narrow screen, use the menu button.

## 3. Prepare the five datasets

Use genuine historical files from official or otherwise verifiable sources. Keep an untouched backup of every original file. The intended history window is approximately five years, with matching trading dates wherever the source provides them.

The Data Centre accepts CSV and Excel (.xlsx/.xls) files locally. The first worksheet of an Excel workbook is converted to CSV by the browser. The cloud API accepts CSV uploads only; the browser performs that conversion before upload.

| Dataset in the app | Required content / useful columns |
|---|---|
| NIFTY 50 daily price history | Date, Open, High, Low, Close. Volume is optional and must not be invented. |
| F&O bhavcopy | TradDt, TckrSymb, XpryDt, StrkPric, OptnTp, OpnPric, HghPric, LwPric, ClsPric, OpnIntrst, ChngInOpnIntrst, TtlTradgVol |
| Participant-wise OI | Date and participant categories/long-short position fields |
| FII derivatives statistics | Date and index/stock futures and options position fields |
| India VIX / settlement / volatility | Date, IndiaVIX and available OHLC/settlement fields |

For the folder import feature, use descriptive filenames so the app can classify files:
- Names containing `vix`, `india-vix` or `volatility` → India VIX / volatility.
- Names containing `participant`, `client-oi` or `fii-dii-oi` → participant OI.
- Names containing `fii` or `derivative-stat` → FII derivatives statistics.
- Names containing `bhav` or `fo-contract` → F&O bhavcopy.
- Names containing `nifty` or `index-history` → NIFTY 50 spot history.

If a filename is classified incorrectly, use the individual **Import CSV** button under the correct dataset instead.

## 4. Download historical data automatically (recommended)

1. Open **NSE data centre**.
2. In the **Automatic 5-year historical data download** panel at the top, enter the Render `INGEST_API_TOKEN`.
3. Click **Download 5-Year Historical Data**.
4. Keep the page open while it runs. The progress panel refreshes every five seconds.
5. Review the **Historical data coverage** panel for each source's date count, rows, percentage and first/last date.
6. If the job stops or reports gaps, start it again after checking the error. Successfully archived source/date files are skipped; missing reports are retried.

The downloader requests NIFTY 50 OHLC and India VIX history in date chunks, then requests available NSE daily derivatives reports for each NIFTY trading date. NSE archive coverage is not guaranteed for every historical date or every report type. A completed job with gaps is not complete historical coverage. The coverage percentage is a date-count diagnostic, not proof of correct field mapping or full contract data.

## 5. Optional: import your own local files

Use this only if you already have additional CSV/Excel data that you want to compare or add.

1. Click **Select data folder** and choose the folder containing CSV/Excel files, or use the individual **Import CSV** controls.
2. The app stores these files in this browser's local IndexedDB vault.
3. Check the dataset assignments, row counts and date ranges.
4. Click **Run local validation**.
5. To archive those local files on the server, enter `INGEST_API_TOKEN` in the cloud import field and click **Sync local files to cloud**.

**Local validation is only a structural check.** It checks recognizable dates, basic OHLC consistency, date coverage and cross-dataset date overlap. It does not prove authenticity, completeness, correct field mapping or model readiness.

The local vault is tied to this browser profile and website origin. Clearing browser site data can erase it. Download backups of your original files regularly. Folder access may need to be granted again after a browser restart; Firefox may require you to select the folder again.

## 6. Configure cloud storage and secure upload

Cloud upload is optional until you configure the API. Do not put secrets in source code, GitHub commits, screenshots or public messages.

### A. Set up persistent PostgreSQL

1. Create a PostgreSQL project with a durable provider such as Neon.
2. Copy its PostgreSQL connection string.
3. In the Render dashboard, open the API service named **nifty-predictor-api**.
4. Open Environment and set `DATABASE_URL` to the connection string.
5. Save changes and redeploy the API.
6. Open the API health link. It should return status `ok` and database `connected`.

Do not rely on an ephemeral application filesystem for long-term storage. Keep independent backups of important source data.

### B. Set API security tokens

In the Render API service Environment settings, create two separate strong random secrets:

- `INGEST_API_TOKEN` — authorizes cloud file imports and historical feature backfill.
- `SCHEDULER_TOKEN` — authorizes the scheduled ingestion endpoint.

Save both in your password manager. Do not reuse the same token for both settings. Redeploy the API after saving.

### C. Set the GitHub Actions secret

1. Open https://github.com/naveenm2020it-glitch/Nifty_Predictor/settings/secrets/actions
2. Choose **New repository secret**.
3. Name it exactly `NIFTY_IQ_SCHEDULER_TOKEN`.
4. Set its value to exactly the same value as Render's `SCHEDULER_TOKEN`.
5. Save.

The GitHub workflow uses this secret to call the protected API. Never add the token to the workflow file itself.

## 7. Sync local files to cloud

1. Return to the website → **NSE data centre**.
2. In **Cloud archive**, paste the `INGEST_API_TOKEN` into the **Cloud import token** field.
3. Click **Sync local files to cloud**.
4. Wait for the result message. Repeat only for files that did not upload successfully.
5. Click **Refresh cloud status**.

The cloud upload is explicit; choosing a local folder does not upload it automatically. The new automatic historical downloader is separate: it downloads directly from the backend and does not require local files. The API accepts CSV uploads up to 30 MB each. Excel files are converted to CSV in the browser before upload. Uploaded rows are archived with dataset/date/hash deduplication. The database stores file provenance and row records; it does not act as a permanent raw-binary file archive.

If upload returns:
- **401** — the token is wrong or does not match Render's `INGEST_API_TOKEN`.
- **503** — cloud import is not enabled or the API/database configuration is unavailable.
- **413** — the CSV exceeds the 30 MB upload limit; split the file into smaller date ranges.
- **422** — check the date column and dataset format; the file may have no recognized valid dates.
- **415** — use CSV for cloud import; local Excel import is converted by the browser.


## 8. Build historical technical features

Only after NIFTY 50 daily OHLC records have been synced to the cloud:

1. Keep the ingest token entered in the Cloud archive section.
2. Click **Build historical features** once.
3. Click **Validate cloud datasets** and review the OHLC and derivatives field checks.
4. Review the response. The current backfill creates technical feature snapshots from valid NIFTY 50 OHLC rows when at least 60 valid dates are available.
5. Click **Refresh cloud status**.

This is a technical-feature backfill, **not AI model training**. It creates daily return, gap, range, close location, simple ATR and moving-average features. Training is a separate step.

## 9. Train and evaluate the daily direction research model

After the five-year NIFTY OHLC history is downloaded and the historical technical features are built:

1. Keep INGEST_API_TOKEN entered in the Data Centre.
2. Click **Train walk-forward model**.
3. Wait for the response. The cloud validator requires at least 1,000 distinct NIFTY trading dates across at least 4.8 years, with valid and non-conflicting OHLC rows. The model trainer then requires at least 300 valid next-session samples and at least three chronological test windows.
4. The model is marked **validated** only when it has at least 100 out-of-sample predictions, at least three folds, balanced accuracy of at least 0.52, Brier score below 0.26 and performance above the majority-class baseline.
5. Open **Model performance** to inspect fold-level results and aggregate out-of-sample metrics.
6. If the model is rejected or blocked, do not force a forecast. Check data coverage, source mapping and sample count before trying again.

This first model predicts whether the next available daily NIFTY close is higher or lower than the current close. Its logistic-regression scores are model estimates, not calibrated option probabilities, guaranteed win rates or intraday signals. The model does not select CE/PE strikes or predict 15-minute premium moves.

## 10. Generate and use the next-session research report

1. From **NSE data centre**, click **Generate next-day research** after a model has passed validation.
2. Open **Next-day prediction** to see the latest saved forecast.
3. Review the bias, up/down model scores, reference close, ATR-based conditional zones, triggers and invalidation references.
4. Download the **PDF report** or **Excel report** from the prediction screen.
5. On each scheduled evening ingestion, the API updates the latest features and attempts to generate a new scenario using the previously validated model. It does not retrain the model every evening.

The displayed next-session date is a weekday estimate; public holidays may shift the actual NSE session. Confirm the exchange calendar before using the report. The conditional price zones are research references, not executable entries or assured targets.

## 11. Verify the scheduled NSE ingestion

1. Open https://github.com/naveenm2020it-glitch/Nifty_Predictor/actions
2. Select **NIFTY IQ evening data ingestion**.
3. Choose **Run workflow** and confirm on the main branch.
4. Open the latest run and inspect each step and its log.
5. Return to the website → **NSE data centre** → **Refresh cloud status**.
6. Confirm the latest ingestion status and official source-download count. For detailed API diagnostics, use:
   - https://nifty-predictor-api-0gl1.onrender.com/api/v1/system/status
   - https://nifty-predictor-api-0gl1.onrender.com/api/v1/pipeline/runs
   - https://nifty-predictor-api-0gl1.onrender.com/api/v1/pipeline/source-downloads
   - https://nifty-predictor-api-0gl1.onrender.com/api/v1/pipeline/readiness

The workflow is scheduled for weekdays at 19:15 and 20:00 India time. Exchange publication timing, source availability, rate limits and hosting availability can cause partial or failed runs. A scheduled run is not proof that every source was downloaded. Inspect each source status and date.

## 12. Understand the current data limitations

- NIFTY Indices historical OHLC does not provide NIFTY index traded volume; do not fill it with invented values.
- FII derivatives statistics are aggregate index-segment context, not direct NIFTY-underlying-only positioning.
- Downloaded derivatives reports still require semantic validation before they can be treated as model-ready features.
- Missing, stale, duplicate or conflicting data must be resolved before research or model training.
- A successful API health check proves the API can reach its database; it does not prove market data is complete.

## 13. Troubleshooting

| Problem | What to do |
|---|---|
| Website appears blank or old | Refresh the page; check the Render static-site deployment status. |
| API health fails | Check the Render API service status and logs; verify `DATABASE_URL`; redeploy after environment changes. |
| Folder picker does not open | Use a normal browser window, allow file access and choose the folder manually. |
| Local files disappear | Check the same browser profile and website URL; browser site-data clearing can erase IndexedDB. Restore from your downloaded backups. |
| A file is not detected | Use the per-dataset Import CSV control or rename the file with a recognized keyword. |
| Local validation reports missing dates | Check date formats, column headers and whether the file includes the expected dataset. |
| Cloud sync fails | Verify the ingest token, API health, file size (30 MB max) and date column. |
| Evening workflow fails | Check the GitHub Actions secret and that it matches Render's scheduler token; inspect the job logs. |
| No prediction appears | Expected at this stage: no validated model is currently available. Do not treat placeholders as signals. |

## 14. Remaining work before the full multi-engine product is complete

The following work remains required:

1. Confirm the five-year historical datasets are complete, correctly mapped and semantically validated.
2. Validate NSE report parsing, contract selection, participant fields, expiry handling, trading calendar and missing-session rules.
3. Build a complete historical feature set without look-ahead leakage.
4. Train candidate models using chronological splits.
5. Run walk-forward and out-of-sample evaluation; publish sample counts, confidence intervals and calibration metrics.
6. Register only models that pass predefined validation criteria.
7. Implement and test next-day scenario generation and invalidation logic.
8. Connect verified outputs to the UI and generate reproducible PDF/Excel reports.
9. Verify the full daily cycle end-to-end using actual exchange-published data.

Until those steps pass, the app should be used as a data-import and research-readiness workspace only—not as a trading signal service.
