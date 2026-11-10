# NIFTY IQ — Web Application User Manual

**Live application:** https://nifty-next-day-prediction.onrender.com  
**API health / documentation:** https://nifty-predictor-api-0gl1.onrender.com/health and https://nifty-predictor-api-0gl1.onrender.com/docs  
**Repository:** https://github.com/naveenm2020it-glitch/Nifty_Predictor

Last reviewed: 10 November 2026

---

## 1. Important: what is ready today

The website and API are deployed. The working daily workflow is designed to fetch only the latest available NSE session reports, append them to cloud PostgreSQL, update rolling indicators and generate a next-session evidence report. It does not require a five-year historical download or a trained ML model. The five analysis agents cover price structure, options/derivatives, participant and FII positioning, futures/volatility, and recent patterns. Each agent reports unavailable or unmapped inputs explicitly. Automatic source fetching and every parser still require successful live verification against the exchange's actual responses before data completeness can be claimed.

**The daily workflow is implemented in code but still requires a successful live NSE-source run to verify it end to end.** The daily report is evidence-based, not a statistically validated ML forecast. The five agents do not fabricate missing inputs or probabilities. Do not treat the report as a live trading signal.

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

## 3. Daily source datasets

The automatic workflow fetches the latest available trading-session documents from the configured public NSE/Nifty Indices sources. Keep the cloud PostgreSQL database persistent so each daily session is appended to the existing archive. Historical CSV/Excel imports remain optional for additional research.

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

## 4. Automatic daily data download and next-day analysis

1. Open **NSE data centre**.
2. No historical backfill button is required for daily prediction.
3. The GitHub Actions workflow runs automatically on weekdays at 19:15 and 20:00 IST.
4. It asks the cloud API to fetch the latest available NIFTY 50 OHLC, India VIX, F&O bhavcopy, participant OI, FII derivatives statistics, settlement and volatility reports.
5. The API appends valid records to PostgreSQL, updates rolling indicators and runs five analysis agents.
6. Open **Next-day prediction** or the **Latest saved next-day report** section to inspect the result. Refresh the report if a run has just completed.

Automatic daily ingestion is triggered by the scheduled GitHub Actions workflow and protected by `SCHEDULER_TOKEN`. Manual file uploads and optional historical model training use the separate `INGEST_API_TOKEN`.

The daily downloader requests the latest available session only; it does not repeat a five-year download. NSE report publication timing, holidays, cloud IP restrictions and rate limits can cause partial runs. A scheduled run is not proof that every report was retrieved. Inspect each source's status and date. The system stores only source provenance and parsed records in the current database schema; it does not yet preserve every original binary report.

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

The cloud upload is explicit; choosing a local folder does not upload it automatically. The automatic historical downloader is separate: it downloads directly from public source adapters and does not require local files or a browser token. The API accepts CSV uploads up to 30 MB each. Excel files are converted to CSV in the browser before upload. Uploaded rows are archived with dataset/date/hash deduplication. The database stores file provenance and row records; it does not act as a permanent raw-binary file archive.

If upload returns:
- **401** — the token is wrong or does not match Render's `INGEST_API_TOKEN`.
- **503** — cloud import is not enabled or the API/database configuration is unavailable.
- **413** — the CSV exceeds the 30 MB upload limit; split the file into smaller date ranges.
- **422** — check the date column and dataset format; the file may have no recognized valid dates.
- **415** — use CSV for cloud import; local Excel import is converted by the browser.


## 8. Daily feature updates and five-agent analysis

After each daily source fetch, the backend updates rolling NIFTY features from the latest stored sessions and creates a daily evidence report. The report contains five agents: (1) price structure, (2) options and derivatives, (3) participant and FII positioning, (4) futures and volatility, and (5) recent price patterns. Agents with missing or unmapped inputs are marked unavailable. The coordinator publishes a bullish, bearish or wait/range scenario without inventing probabilities.

Rolling features include daily return, gap, range, close location, available ATR and moving averages. A feature build by itself is not proof of a validated predictive model.

## 9. Optional historical ML model research

Historical model training is optional and separate from the daily five-agent report. Use it only if you later import enough genuine chronological price history:

1. Keep INGEST_API_TOKEN entered in the Data Centre.
2. Click **Train walk-forward model**.
3. Wait for the response. The cloud validator requires at least 1,000 distinct NIFTY trading dates across at least 4.8 years, with valid and non-conflicting OHLC rows. The model trainer then requires at least 300 valid next-session samples and at least three chronological test windows.
4. The model is marked **validated** only when it has at least 100 out-of-sample predictions, at least three folds, balanced accuracy of at least 0.52, Brier score below 0.26 and performance above the majority-class baseline.
5. Open **Model performance** to inspect fold-level results and aggregate out-of-sample metrics.
6. If the model is rejected or blocked, do not force a forecast. Check data coverage, source mapping and sample count before trying again.

This first model predicts whether the next available daily NIFTY close is higher or lower than the current close. Its logistic-regression scores are model estimates, not calibrated option probabilities, guaranteed win rates or intraday signals. The model does not select CE/PE strikes or predict 15-minute premium moves.

## 10. Generate and use the next-session daily report

1. The scheduled evening ingestion generates the daily report automatically after source retrieval.
2. Open **Next-day prediction** to see the latest saved report.
3. Review the input date, next-session estimate, five agent evidence summaries, source gaps, bias, triggers, target zones and invalidation references.
4. Download the **PDF report** or **Excel report** from the prediction screen.
5. If you need to regenerate manually, use **Generate daily next-day report** in the Data Centre after configuring the cloud ingest token.

The displayed next-session date is a weekday estimate; public holidays may shift the actual NSE session. Confirm the exchange calendar before using the report. The conditional price zones are research references, not executable entries or assured targets. The daily evidence report does not produce calibrated probabilities.

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
| No prediction appears | Check the historical job result, feature backfill, semantic validation and walk-forward training gate. If the model is rejected or blocked, inspect its metrics and missing data; do not force a forecast. |

## 14. Current module status and remaining work

Implemented in the web application and API:
- Historical EOD NIFTY option-chain summary from archived NSE F&O bhavcopy, including nearest expiry, max pain, put/call OI ratio and highest-OI strike references.
- Historical NIFTY futures summary from archived bhavcopy.
- Participant OI archive display (raw published fields; source-specific interpretation is not yet certified).
- Daily candle-pattern summaries with occurrence counts, next-session outcome rate and 95% Wilson confidence intervals.
- Scenario planner connected to the latest saved validated daily forecast.
- PDF and Excel export endpoints for the daily forecast.
- Automatic post-download technical feature generation, semantic OHLC validation and a guarded attempt at walk-forward training.

Still not complete or not yet verified:
1. A successful end-to-end five-year download on this Render deployment, with actual NSE responses and a date-by-date gap report.
2. Full source-specific semantic mapping for participant OI, FII derivatives, settlement and volatility data.
3. A multi-engine predictive model that uses independently validated options, participant/FII, futures/volatility and pattern features. The current model candidate is a daily NIFTY close-direction baseline using technical OHLC features only.
4. Implied volatility and Greeks are not calculated unless complete, appropriate option inputs are available; the current historical bhavcopy chain does not guarantee these fields.
5. Intraday or 15-minute option-premium targets, strike selection, entry/stop-loss alerts and live option-chain prediction are not implemented in this next-day application.
6. Historical scenario outcome tracking and full future-session calibration/monitoring.
7. End-to-end operational verification of scheduler, NSE availability, holiday handling, retries, database durability and report exports using real downloaded data.

Use the application as a research and data-validation workspace until the source download and model gates pass. Do not treat historical option-chain summaries or the daily direction baseline as live or intraday trading signals.

**Database retention warning:** the currently attached Render Free PostgreSQL instance is scheduled to expire on 10 December 2026. Before relying on this as a permanent archive, move the database connection string to a durable PostgreSQL provider and verify that data has been retained or backed up.