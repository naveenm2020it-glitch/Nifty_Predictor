import csv
import hashlib
import hmac
import io
import json
import os
from datetime import date, datetime
from typing import Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .db import connect, init_db
from .pipeline import backfill_historical_features, generate_next_session_prediction, historical_download_status, pipeline_runs, run_evening_ingestion, start_five_year_historical_download, train_walk_forward_model, validate_cloud_datasets

DATASET_KEYS = {"spot_ohlc", "fo_bhavcopy", "participant_oi", "fii_derivatives", "vix_volatility"}
MAX_UPLOAD_BYTES = 30 * 1024 * 1024

app = FastAPI(
    title="NIFTY IQ Data API",
    version="0.3.0",
    description="Provenance-aware incremental data archive and model registry for NIFTY research."
)

allowed_origins = [x.strip() for x in os.getenv("ALLOWED_ORIGINS", "*").split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins or ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

@app.on_event("startup")
def startup():
    init_db()
    with connect() as conn:
        conn.execute("""
          UPDATE historical_download_jobs
          SET status='interrupted', updated_at=NOW(),
              error=COALESCE(error, 'API process restarted; start the download again to continue missing dates.')
          WHERE status IN ('queued','running')
        """)

@app.get("/")
def root():
    return {"service": "NIFTY IQ Data API", "version": app.version, "docs": "/docs"}

@app.get("/health")
def health():
    try:
        with connect() as conn:
            conn.execute("SELECT 1")
        return {"status": "ok", "database": "connected", "persistence": "external_postgresql"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"status": "degraded", "database": "unavailable", "reason": str(exc)[:180]})

@app.get("/api/v1/system/status")
def system_status():
    try:
        with connect() as conn:
            imports = conn.execute("SELECT COUNT(*) AS count, COALESCE(SUM(accepted_rows),0) AS rows, MAX(imported_at) AS latest FROM dataset_imports").fetchone()
            records = conn.execute("SELECT COUNT(*) AS count FROM dataset_records").fetchone()
            source_files = conn.execute("SELECT COUNT(*) AS count, MAX(fetched_at) AS latest FROM source_downloads").fetchone()
            features = conn.execute("SELECT COUNT(*) AS count, MAX(trade_date) AS latest FROM daily_feature_snapshots").fetchone()
            latest_run = conn.execute("SELECT status,finished_at FROM ingestion_runs ORDER BY started_at DESC LIMIT 1").fetchone()
            models = conn.execute("SELECT COUNT(*) AS count FROM model_versions WHERE status = 'validated'").fetchone()
            forecasts = conn.execute("SELECT COUNT(*) AS count, MAX(generated_at) AS latest FROM predictions WHERE status = 'published'").fetchone()
        return {
            "api": "available", "database": "connected", "storage": "configured_database_url",
            "dataset_files": imports["count"], "accepted_source_rows": imports["rows"],
            "deduplicated_records": records["count"], "last_import_at": imports["latest"].isoformat() if imports["latest"] else None,
            "official_source_files": source_files["count"], "last_official_download_at": source_files["latest"].isoformat() if source_files["latest"] else None,
            "feature_snapshots": features["count"], "latest_feature_date": features["latest"].isoformat() if features["latest"] else None,
            "last_ingestion_status": latest_run["status"] if latest_run else "not_run",
            "validated_models": models["count"], "published_predictions": forecasts["count"],
            "last_prediction_at": forecasts["latest"].isoformat() if forecasts["latest"] else None,
            "market_data": "awaiting_verified_source_ingestion" if not records["count"] else "archived_pending_semantic_validation",
            "models": "not_trained" if not models["count"] else "validated_model_available",
            "forecast": "not_calculated" if not forecasts["count"] else "published_forecast_available",
            "evening_scheduler": "GitHub Actions schedule requires NIFTY_IQ_SCHEDULER_TOKEN secret" if os.getenv("SCHEDULER_TOKEN") else "not_configured"
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {str(exc)[:160]}")

@app.get("/api/v1/datasets")
def list_datasets():
    with connect() as conn:
        rows = conn.execute("""
          SELECT id, dataset_key, source_name, source_url, file_name, imported_at,
                 first_date, last_date, row_count, accepted_rows, rejected_rows,
                 status, sha256, notes
          FROM dataset_imports ORDER BY imported_at DESC LIMIT 200
        """).fetchall()
        counts = conn.execute("""
          SELECT dataset_key, COUNT(*) AS rows, COUNT(DISTINCT trade_date) AS dates,
                 MIN(trade_date) AS first_date, MAX(trade_date) AS last_date
          FROM dataset_records GROUP BY dataset_key
        """).fetchall()
    return {"items": [dict(r) for r in rows], "coverage": [dict(r) for r in counts]}

@app.get("/api/v1/sources")
def sources():
    return {"items": [
        {"key":"nifty50_index_history","name":"NSE Indices — Historical Index Data","url":"https://www.niftyindices.com/reports/historical-data","status":"integration_implemented_live_test_pending","datasets":["NIFTY 50 daily OHLC (CSV)","India VIX historical OHLC"]},
        {"key":"nse_equity_reports","name":"NSE India — Equity reports","url":"https://www.nseindia.com/all-reports","status":"catalogued_not_connected","datasets":["CM-UDiFF Common Bhavcopy Final","Full Bhavcopy and Security Deliverable data"]},
        {"key":"nse_derivatives_reports","name":"NSE India — Derivatives reports","url":"https://www.nseindia.com/all-reports-derivatives","status":"integration_implemented_live_test_pending","datasets":["F&O-UDiFF Common Bhavcopy Final","Participant wise Open Interest","FII derivatives statistics","Daily Settlement Prices","Daily Volatility"]}
    ]}

def parse_trade_date(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw[:10]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%Y%m%d", "%d %b %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None

def normalized_key(value):
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())

def find_value(row, candidates):
    by_norm = {normalized_key(k): v for k, v in row.items() if k is not None}
    for candidate in candidates:
        value = by_norm.get(normalized_key(candidate))
        if value is not None and str(value).strip() != "":
            return value
    return None

@app.post("/api/v1/datasets/import")
async def import_dataset(
    file: UploadFile = File(...),
    dataset_key: str = Form(...),
    source_name: str = Form("User-selected local dataset"),
    source_url: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    ingest_token: Optional[str] = Header(None, alias="X-Ingest-Token")
):
    expected = os.getenv("INGEST_API_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Cloud import is disabled until INGEST_API_TOKEN is configured on the API service.")
    if not ingest_token or not hmac.compare_digest(ingest_token, expected):
        raise HTTPException(status_code=401, detail="Cloud import token is missing or invalid.")
    if dataset_key not in DATASET_KEYS:
        raise HTTPException(status_code=422, detail="Unknown dataset_key.")
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=415, detail="Upload a CSV file. Excel files are converted in the browser before upload.")
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Maximum upload size is 30 MB.")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise ValueError("CSV header row is missing.")
        date_aliases = ["trade_date", "date", "timestamp", "trad_dt", "biz_dt", "date1"]
        accepted = []
        rejected = 0
        filtered = 0
        fo_symbol_filter_applied = False
        dates = []
        for row in reader:
            parsed = parse_trade_date(find_value(row, date_aliases))
            if parsed is None:
                rejected += 1
                continue
            clean = {str(k).strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k is not None}
            if dataset_key == "fo_bhavcopy":
                symbol_value = find_value(clean, ["TckrSymb", "Symbol", "Ticker", "Underlying", "Undrlyg"])
                if symbol_value is not None:
                    fo_symbol_filter_applied = True
                    if "NIFTY" not in str(symbol_value).upper():
                        filtered += 1
                        continue
            if dataset_key == "spot_ohlc":
                aliases = {
                    "open": ["open", "open_price", "opn", "opn_pric"],
                    "high": ["high", "high_price", "hgh_pric"],
                    "low": ["low", "low_price", "lw_pric"],
                    "close": ["close", "close_price", "closing_price", "cls_pric"]
                }
                values = {}
                for field, names in aliases.items():
                    raw_value = find_value(clean, names)
                    try:
                        values[field] = float(str(raw_value).replace(",", "")) if raw_value is not None else None
                    except ValueError:
                        values[field] = None
                if any(v is None or v <= 0 for v in values.values()):
                    rejected += 1
                    continue
                if values["high"] < max(values["open"], values["close"], values["low"]) or values["low"] > min(values["open"], values["close"], values["high"]):
                    rejected += 1
                    continue
            record_hash = hashlib.sha256(json.dumps(clean, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
            accepted.append((parsed, record_hash, clean))
            dates.append(parsed)
        if not accepted:
            raise ValueError("No date-valid rows found. Verify the date column and dataset file.")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="CSV must be UTF-8 encoded.")
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)[:300])

    status = "structural_ohlc_validated" if dataset_key == "spot_ohlc" and rejected == 0 else "ingested_pending_semantic_validation"
    with connect() as conn:
        existing = conn.execute("SELECT id, accepted_rows FROM dataset_imports WHERE dataset_key=%s AND sha256=%s", (dataset_key, digest)).fetchone()
        if existing:
            return {"dataset_id": existing["id"], "status": "already_imported", "accepted_rows": existing["accepted_rows"], "rejected_rows": rejected, "filtered_rows": filtered, "sha256": digest, "duplicate_file": True}
        item = conn.execute("""
          INSERT INTO dataset_imports(dataset_key,source_name,source_url,file_name,sha256,first_date,last_date,
            row_count,accepted_rows,rejected_rows,status,notes)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (dataset_key, source_name, source_url, file.filename, digest, min(dates), max(dates),
              len(accepted)+rejected+filtered, len(accepted), rejected, status, notes)).fetchone()
        import_id = item["id"]
        conn.executemany("""
          INSERT INTO dataset_records(dataset_key,trade_date,record_hash,source_import_id,payload)
          VALUES (%s,%s,%s,%s,%s)
          ON CONFLICT(dataset_key,trade_date,record_hash) DO NOTHING
        """, [(dataset_key, d, h, import_id, json.dumps(payload)) for d,h,payload in accepted])
        conn.execute(
          "INSERT INTO audit_events(event_type,message,details) VALUES (%s,%s,%s)",
          ("cloud_dataset_import", "User-authorized dataset file archived with row-level deduplication",
           json.dumps({"dataset_id":import_id,"dataset_key":dataset_key,"file":file.filename,"accepted":len(accepted),"rejected":rejected,"sha256":digest}))
        )
        inserted = conn.execute("SELECT COUNT(*) AS count FROM dataset_records WHERE source_import_id=%s", (import_id,)).fetchone()["count"]
    return {"dataset_id": import_id, "status": status, "accepted_rows": len(accepted), "rejected_rows": rejected,
            "new_records_from_file": inserted, "first_date": min(dates).isoformat(), "last_date": max(dates).isoformat(),
            "sha256": digest, "duplicate_file": False, "filtered_rows": filtered, "nifty_symbol_filter_applied": fo_symbol_filter_applied}

@app.post("/api/v1/jobs/evening")
def run_evening_job(scheduler_token: Optional[str] = Header(None, alias="X-Scheduler-Token")):
    expected = os.getenv("SCHEDULER_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Scheduler is disabled until SCHEDULER_TOKEN is configured on the API service.")
    if not scheduler_token or not hmac.compare_digest(scheduler_token, expected):
        raise HTTPException(status_code=401, detail="Scheduler token is missing or invalid.")
    try:
        return run_evening_ingestion()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Evening ingestion failed: " + str(exc)[:300])


@app.post("/api/v1/pipeline/history/download")
def start_history_download(ingest_token: Optional[str] = Header(None, alias="X-Ingest-Token")):
    expected = os.getenv("INGEST_API_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Historical download is disabled until INGEST_API_TOKEN is configured.")
    if not ingest_token or not hmac.compare_digest(ingest_token, expected):
        raise HTTPException(status_code=401, detail="Ingest token is missing or invalid.")
    try:
        return start_five_year_historical_download()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not start historical download: " + str(exc)[:250])


@app.get("/api/v1/pipeline/history/status")
def get_history_download_status():
    try:
        return historical_download_status()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Historical download status unavailable: " + str(exc)[:200])


@app.get("/api/v1/pipeline/history/coverage")
def get_history_coverage():
    try:
        with connect() as conn:
            spot = conn.execute("""
              SELECT COUNT(DISTINCT trade_date) AS total,
                     MIN(trade_date) AS first_date, MAX(trade_date) AS last_date
              FROM dataset_records WHERE dataset_key='spot_ohlc'
            """).fetchone()
            sources = conn.execute("""
              SELECT payload->>'_source_key' AS source_key,
                     COUNT(DISTINCT trade_date) AS covered_dates,
                     MIN(trade_date) AS first_date, MAX(trade_date) AS last_date,
                     COUNT(*) AS rows
              FROM dataset_records
              WHERE payload ? '_source_key'
              GROUP BY payload->>'_source_key'
            """).fetchall()
            spot_dates = conn.execute("""
              SELECT DISTINCT trade_date FROM dataset_records
              WHERE dataset_key='spot_ohlc' ORDER BY trade_date
            """).fetchall()
            coverage = {r["source_key"]: dict(r) for r in sources if r["source_key"]}
            expected = {
              "nifty50_daily_ohlc_5y": "NIFTY 50 daily OHLC",
              "india_vix": "India VIX",
              "fo_bhavcopy": "NIFTY F&O bhavcopy",
              "participant_oi": "Participant-wise OI",
              "fii_derivatives": "FII derivatives statistics",
              "fo_settlement": "Daily settlement prices",
              "fo_volatility": "Daily volatility"
            }
            result = []
            for key, label in expected.items():
                item = coverage.get(key, {})
                dates = int(item.get("covered_dates") or 0)
                total = int(spot["total"] or 0)
                result.append({
                  "source_key": key, "dataset": label, "covered_dates": dates,
                  "spot_trading_dates": total,
                  "coverage_pct": round(dates / total * 100, 1) if total else 0,
                  "first_date": item.get("first_date").isoformat() if item.get("first_date") else None,
                  "last_date": item.get("last_date").isoformat() if item.get("last_date") else None,
                  "rows": int(item.get("rows") or 0)
                })
            return {
              "spot_trading_dates": int(spot["total"] or 0),
              "first_date": spot["first_date"].isoformat() if spot["first_date"] else None,
              "last_date": spot["last_date"].isoformat() if spot["last_date"] else None,
              "sources": result,
              "note": "Coverage compares source dates with imported NIFTY 50 trading dates. A date count does not prove every report's field semantics or completeness."
            }
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Historical coverage unavailable: " + str(exc)[:220])


@app.post("/api/v1/pipeline/features/backfill")
def backfill_features(ingest_token: Optional[str] = Header(None, alias="X-Ingest-Token")):
    expected = os.getenv("INGEST_API_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Backfill is disabled until INGEST_API_TOKEN is configured.")
    if not ingest_token or not hmac.compare_digest(ingest_token, expected):
        raise HTTPException(status_code=401, detail="Ingest token is missing or invalid.")
    return backfill_historical_features()


@app.get("/api/v1/pipeline/runs")
def list_pipeline_runs(limit: int = 30):
    return pipeline_runs(max(1, min(limit, 100)))


@app.get("/api/v1/pipeline/source-downloads")
def list_source_downloads(limit: int = 50):
    with connect() as conn:
        rows = conn.execute("""
          SELECT source_key,dataset_key,source_name,source_url,file_name,report_date,
                 fetched_at,sha256,byte_count,status,parsed_rows
          FROM source_downloads ORDER BY fetched_at DESC LIMIT %s
        """, (max(1, min(limit, 200)),)).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/v1/pipeline/readiness")
def pipeline_readiness():
    with connect() as conn:
        coverage = conn.execute("""
          SELECT dataset_key, COUNT(*) AS rows, COUNT(DISTINCT trade_date) AS dates,
                 MIN(trade_date) AS first_date, MAX(trade_date) AS last_date
          FROM dataset_records GROUP BY dataset_key
        """).fetchall()
        model_count = conn.execute("SELECT COUNT(*) AS count FROM model_versions WHERE status='validated'").fetchone()["count"]
    by_key = {r["dataset_key"]: r for r in coverage}
    spot = by_key.get("spot_ohlc")
    span_years = 0.0
    if spot and spot["first_date"] and spot["last_date"]:
        span_years = (spot["last_date"] - spot["first_date"]).days / 365.25
    missing = sorted(DATASET_KEYS - set(by_key))
    historical_ready = not missing and span_years >= 4.8 and int(spot["dates"] if spot else 0) >= 1000
    return {
        "datasets": [dict(r) for r in coverage], "missing_datasets": missing,
        "spot_history_years": round(span_years, 2), "spot_trading_dates": int(spot["dates"] if spot else 0), "required_spot_trading_dates": 1000, "historical_ingestion_ready": historical_ready,
        "semantic_validation": "pending", "feature_engineering": "pending",
        "walk_forward_validation": "pending", "validated_models": model_count,
        "prediction_pipeline": "blocked_until_validation_passes",
        "note": "A file archive is not proof of exchange authenticity or correct field mapping. No model is trained by this endpoint."
    }



@app.post("/api/v1/pipeline/validate")
def validate_pipeline(ingest_token: Optional[str] = Header(None, alias="X-Ingest-Token")):
    expected = os.getenv("INGEST_API_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Validation is disabled until INGEST_API_TOKEN is configured.")
    if not ingest_token or not hmac.compare_digest(ingest_token, expected):
        raise HTTPException(status_code=401, detail="Ingest token is missing or invalid.")
    try:
        return validate_cloud_datasets()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Cloud validation failed: " + str(exc)[:300])


@app.post("/api/v1/models/train")
def train_model(ingest_token: Optional[str] = Header(None, alias="X-Ingest-Token")):
    expected = os.getenv("INGEST_API_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Training is disabled until INGEST_API_TOKEN is configured.")
    if not ingest_token or not hmac.compare_digest(ingest_token, expected):
        raise HTTPException(status_code=401, detail="Ingest token is missing or invalid.")
    try:
        return train_walk_forward_model()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Walk-forward training failed: " + str(exc)[:300])


@app.post("/api/v1/predictions/generate")
def generate_prediction(ingest_token: Optional[str] = Header(None, alias="X-Ingest-Token")):
    expected = os.getenv("INGEST_API_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Prediction generation is disabled until INGEST_API_TOKEN is configured.")
    if not ingest_token or not hmac.compare_digest(ingest_token, expected):
        raise HTTPException(status_code=401, detail="Ingest token is missing or invalid.")
    try:
        return generate_next_session_prediction()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Prediction generation failed: " + str(exc)[:300])


@app.get("/api/v1/research/summary")
def research_summary():
    with connect() as conn:
        latest_feature = conn.execute("SELECT trade_date,features FROM daily_feature_snapshots ORDER BY trade_date DESC LIMIT 1").fetchone()
        latest_model = conn.execute("""SELECT id,model_name,version,training_cutoff,validation_metrics,status,created_at
          FROM model_versions ORDER BY created_at DESC LIMIT 1""").fetchone()
        latest_prediction = conn.execute("""SELECT id,session_date,generated_at,input_cutoff,scenarios,status
          FROM predictions ORDER BY generated_at DESC LIMIT 1""").fetchone()
        feature_count = conn.execute("SELECT COUNT(*) AS count FROM daily_feature_snapshots").fetchone()["count"]
        spot_count = conn.execute("SELECT COUNT(DISTINCT trade_date) AS count FROM dataset_records WHERE dataset_key='spot_ohlc'").fetchone()["count"]
    return {
      "spot_trading_dates": int(spot_count or 0), "feature_snapshots": int(feature_count or 0),
      "latest_feature_date": latest_feature["trade_date"].isoformat() if latest_feature else None,
      "latest_model": dict(latest_model) if latest_model else None,
      "latest_prediction": dict(latest_prediction) if latest_prediction else None,
      "training_available": int(feature_count or 0) >= 301,
      "prediction_available": bool(latest_model and latest_model["status"] == "validated"),
      "important_scope": "The current trained baseline predicts next available daily NIFTY close direction only. It does not model option premiums, intraday 15-minute targets, Greeks or execution."
    }


@app.get("/api/v1/models")
def list_models():
    with connect() as conn:
        rows = conn.execute("""
          SELECT id,model_name,version,training_started_at,training_completed_at,training_cutoff,
                 feature_schema,validation_metrics,status,created_at
          FROM model_versions ORDER BY created_at DESC LIMIT 50
        """).fetchall()
    return {"items": [dict(r) for r in rows]}

@app.get("/api/v1/predictions/latest")
def latest_prediction():
    with connect() as conn:
        row = conn.execute("""
          SELECT p.id,p.session_date,p.generated_at,p.input_cutoff,p.data_quality,p.scenarios,p.status,
                 m.version AS model_version
          FROM predictions p LEFT JOIN model_versions m ON m.id=p.model_version_id
          WHERE p.status='published' ORDER BY p.session_date DESC LIMIT 1
        """).fetchone()
    return {"item": dict(row) if row else None}


def _latest_report_payload():
    with connect() as conn:
        row=conn.execute("""SELECT p.id,p.session_date,p.generated_at,p.input_cutoff,p.data_quality,p.scenarios,p.status,
          m.model_name,m.version,m.validation_metrics,m.status AS model_status
          FROM predictions p LEFT JOIN model_versions m ON m.id=p.model_version_id
          WHERE p.status='published' ORDER BY p.generated_at DESC LIMIT 1""").fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No published research forecast is available to export.")
    return dict(row)


@app.get("/api/v1/reports/latest.xlsx")
def export_latest_excel():
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    report=_latest_report_payload()
    scenarios=report["scenarios"]
    wb=Workbook()
    ws=wb.active; ws.title="Next-day research"
    rows=[
      ["NIFTY IQ — Next-session research report"],
      ["Session date (weekday estimate)",report["session_date"].isoformat()],
      ["Input cutoff",report["input_cutoff"].isoformat() if report["input_cutoff"] else ""],
      ["Generated at",report["generated_at"].isoformat()],
      ["Model",report.get("version") or ""],
      ["Model status",report.get("model_status") or ""],
      ["Research bias",scenarios.get("bias","")],
      ["Model up score",scenarios.get("probability_up","")],
      ["Model down score",scenarios.get("probability_down","")],
      ["Reference close",scenarios.get("reference_close","")],
      ["ATR14",scenarios.get("atr14","")],
      ["Bullish trigger reference",scenarios.get("bullish",{}).get("trigger_reference","")],
      ["Bullish target zone",str(scenarios.get("bullish",{}).get("target_zone",""))],
      ["Bullish invalidation",scenarios.get("bullish",{}).get("invalidation","")],
      ["Bearish trigger reference",scenarios.get("bearish",{}).get("trigger_reference","")],
      ["Bearish target zone",str(scenarios.get("bearish",{}).get("target_zone",""))],
      ["Bearish invalidation",scenarios.get("bearish",{}).get("invalidation","")],
      ["Range lower reference",scenarios.get("range",{}).get("lower_reference","")],
      ["Range upper reference",scenarios.get("range",{}).get("upper_reference","")],
      ["Important limitation",scenarios.get("warning","")],
    ]
    for row in rows: ws.append(row)
    ws.column_dimensions["A"].width=34; ws.column_dimensions["B"].width=100
    ws.freeze_panes="A2"
    for cell in ws[1]:
        cell.font=Font(bold=True,color="FFFFFF",size=14)
        cell.fill=PatternFill("solid",fgColor="243B53")
    for row in ws.iter_rows(min_row=2):
        row[0].font=Font(bold=True)
        row[1].alignment=Alignment(wrap_text=True,vertical="top")
    metrics=report.get("validation_metrics") or {}
    ms=wb.create_sheet("Walk-forward metrics")
    ms.append(["Metric","Value"])
    for key,value in metrics.items():
        if key!="folds": ms.append([key,json.dumps(value) if isinstance(value,(dict,list)) else value])
    fs=wb.create_sheet("Fold results"); fs.append(["Fold","Train samples","Test samples","Test start","Test end","Accuracy","Balanced accuracy","Brier score"])
    for fold in metrics.get("folds",[]):
        fs.append([fold.get("fold"),fold.get("train_samples"),fold.get("test_samples"),fold.get("test_start"),fold.get("test_end"),fold.get("accuracy"),fold.get("balanced_accuracy"),fold.get("brier_score")])
    out=BytesIO(); wb.save(out); out.seek(0)
    return StreamingResponse(out,media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      headers={"Content-Disposition":'attachment; filename="NIFTY_IQ_Next_Day_Research.xlsx"'})


@app.get("/api/v1/reports/latest.pdf")
def export_latest_pdf():
    from io import BytesIO
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    report=_latest_report_payload(); s=report["scenarios"]; metrics=report.get("validation_metrics") or {}
    out=BytesIO(); doc=SimpleDocTemplate(out,pagesize=A4,rightMargin=18*mm,leftMargin=18*mm,topMargin=16*mm,bottomMargin=16*mm)
    styles=getSampleStyleSheet(); styles.add(ParagraphStyle(name="SmallNote",parent=styles["BodyText"],fontSize=8,leading=11,textColor=colors.HexColor("#52616b")))
    story=[Paragraph("NIFTY IQ — Next-session research report",styles["Title"]),
      Paragraph("Research output only · No order execution",styles["SmallNote"]),Spacer(1,8)]
    summary=[["Session date (weekday estimate)",report["session_date"].isoformat()],
      ["Input cutoff",report["input_cutoff"].isoformat() if report["input_cutoff"] else "—"],
      ["Generated",report["generated_at"].isoformat()],
      ["Model",report.get("version") or "—"],["Model status",report.get("model_status") or "—"],
      ["Research bias",s.get("bias","—")],["Model up score",str(s.get("probability_up","—"))],
      ["Model down score",str(s.get("probability_down","—"))],["Reference close",str(s.get("reference_close","—"))],
      ["ATR14",str(s.get("atr14","—"))]]
    t=Table(summary,colWidths=[55*mm,115*mm]); t.setStyle(TableStyle([
      ("BACKGROUND",(0,0),(0,-1),colors.HexColor("#eaf0f6")),("GRID",(0,0),(-1,-1),.35,colors.HexColor("#cbd5e1")),
      ("FONTNAME",(0,0),(0,-1),"Helvetica-Bold"),("FONTSIZE",(0,0),(-1,-1),8),("VALIGN",(0,0),(-1,-1),"TOP"),("PADDING",(0,0),(-1,-1),6)]))
    story += [t,Spacer(1,12),Paragraph("Conditional scenarios",styles["Heading2"])]
    scenario_rows=[["Path","Trigger reference","Target zone","Invalidation / boundary"],
      ["Bullish",str(s.get("bullish",{}).get("trigger_reference","—")),str(s.get("bullish",{}).get("target_zone","—")),str(s.get("bullish",{}).get("invalidation","—"))],
      ["Bearish",str(s.get("bearish",{}).get("trigger_reference","—")),str(s.get("bearish",{}).get("target_zone","—")),str(s.get("bearish",{}).get("invalidation","—"))],
      ["Range", "—","—",str([s.get("range",{}).get("lower_reference"),s.get("range",{}).get("upper_reference")])]]
    st=Table(scenario_rows,colWidths=[25*mm,38*mm,48*mm,59*mm]); st.setStyle(TableStyle([
      ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#243b53")),("TEXTCOLOR",(0,0),(-1,0),colors.white),
      ("GRID",(0,0),(-1,-1),.35,colors.HexColor("#cbd5e1")),("FONTSIZE",(0,0),(-1,-1),7),("PADDING",(0,0),(-1,-1),5)]))
    story += [st,Spacer(1,12),Paragraph("Walk-forward evaluation",styles["Heading2"])]
    for key in ("out_of_sample_samples","accuracy","balanced_accuracy","precision_up","recall_up","brier_score","majority_baseline_accuracy","beats_majority_baseline"):
        if key in metrics: story.append(Paragraph(key.replace("_"," ").title()+": "+str(metrics[key]),styles["BodyText"]))
    story += [Spacer(1,10),Paragraph(s.get("warning","Research only."),styles["SmallNote"])]
    doc.build(story); out.seek(0)
    return StreamingResponse(out,media_type="application/pdf",
      headers={"Content-Disposition":'attachment; filename="NIFTY_IQ_Next_Day_Research.pdf"'})


@app.get("/api/v1/audit")
def audit():
    with connect() as conn:
        rows = conn.execute("SELECT id,event_type,message,details,created_at FROM audit_events ORDER BY created_at DESC LIMIT 100").fetchall()
    return {"items": [dict(r) for r in rows]}
