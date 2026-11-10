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

from .db import connect, init_db

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
            models = conn.execute("SELECT COUNT(*) AS count FROM model_versions WHERE status = 'validated'").fetchone()
            forecasts = conn.execute("SELECT COUNT(*) AS count, MAX(generated_at) AS latest FROM predictions WHERE status = 'published'").fetchone()
        return {
            "api": "available", "database": "connected", "storage": "configured_database_url",
            "dataset_files": imports["count"], "accepted_source_rows": imports["rows"],
            "deduplicated_records": records["count"], "last_import_at": imports["latest"].isoformat() if imports["latest"] else None,
            "validated_models": models["count"], "published_predictions": forecasts["count"],
            "last_prediction_at": forecasts["latest"].isoformat() if forecasts["latest"] else None,
            "market_data": "awaiting_verified_source_ingestion" if not records["count"] else "archived_pending_semantic_validation",
            "models": "not_trained" if not models["count"] else "validated_model_available",
            "forecast": "not_calculated" if not forecasts["count"] else "published_forecast_available",
            "evening_scheduler": "not_configured"
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
        {"key":"nse_equity_reports","name":"NSE India — Equity reports","url":"https://www.nseindia.com/all-reports","status":"catalogued_not_connected","datasets":["CM-UDiFF Common Bhavcopy Final","Full Bhavcopy and Security Deliverable data"]},
        {"key":"nse_derivatives_reports","name":"NSE India — Derivatives reports","url":"https://www.nseindia.com/all-reports-derivatives","status":"catalogued_not_connected","datasets":["F&O-UDiFF Common Bhavcopy Final","Participant wise Open Interest","FII derivatives statistics","Daily Settlement Prices","Daily Volatility"]}
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
              len(accepted)+rejected, len(accepted), rejected, status, notes)).fetchone()
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
    historical_ready = not missing and span_years >= 4.8
    return {
        "datasets": [dict(r) for r in coverage], "missing_datasets": missing,
        "spot_history_years": round(span_years, 2), "historical_ingestion_ready": historical_ready,
        "semantic_validation": "pending", "feature_engineering": "pending",
        "walk_forward_validation": "pending", "validated_models": model_count,
        "prediction_pipeline": "blocked_until_validation_passes",
        "note": "A file archive is not proof of exchange authenticity or correct field mapping. No model is trained by this endpoint."
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

@app.get("/api/v1/audit")
def audit():
    with connect() as conn:
        rows = conn.execute("SELECT id,event_type,message,details,created_at FROM audit_events ORDER BY created_at DESC LIMIT 100").fetchall()
    return {"items": [dict(r) for r in rows]}
