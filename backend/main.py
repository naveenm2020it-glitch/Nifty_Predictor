import csv
import hashlib
import io
import json
import os
from datetime import date, datetime
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .db import connect, init_db

app = FastAPI(
    title="NIFTY IQ Data API",
    version="0.2.0",
    description="Integrity-first data registry and import API for the NIFTY next-day research system."
)

allowed_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
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
        return {"status": "ok", "database": "connected", "timestamp": datetime.utcnow().isoformat() + "Z"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"status": "degraded", "database": "unavailable", "reason": str(exc)[:180]})

@app.get("/api/v1/system/status")
def system_status():
    try:
        with connect() as conn:
            datasets = conn.execute("SELECT COUNT(*) AS count, COALESCE(SUM(accepted_rows),0) AS rows FROM datasets").fetchone()
            latest = conn.execute("SELECT MAX(imported_at) AS latest FROM datasets").fetchone()
        return {
            "api": "available",
            "database": "connected",
            "datasets_registered": datasets["count"],
            "validated_rows": datasets["rows"],
            "last_import_at": latest["latest"].isoformat() if latest["latest"] else None,
            "market_data": "awaiting_verified_import",
            "models": "not_trained",
            "forecast": "not_calculated",
            "report_pipeline": "not_configured"
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
          FROM datasets ORDER BY imported_at DESC LIMIT 200
        """).fetchall()
    return {"items": [dict(r) for r in rows]}

@app.get("/api/v1/sources")
def sources():
    return {"items": [
        {"key":"nse_equity_reports","name":"NSE India — Equity reports","url":"https://www.nseindia.com/all-reports","status":"catalogued_not_connected","datasets":["CM-UDiFF Common Bhavcopy Final","Full Bhavcopy and Security Deliverable data"]},
        {"key":"nse_derivatives_reports","name":"NSE India — Derivatives reports","url":"https://www.nseindia.com/all-reports-derivatives","status":"catalogued_not_connected","datasets":["F&O-UDiFF Common Bhavcopy Final","Participant wise Open Interest","FII derivatives statistics","Daily Settlement Prices","Daily Volatility"]}
    ]}

@app.post("/api/v1/datasets/import-csv")
async def import_csv(
    file: UploadFile = File(...),
    dataset_key: str = Form("nifty_ohlc"),
    source_name: str = Form(...),
    source_url: Optional[str] = Form(None),
    notes: Optional[str] = Form(None)
):
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=415, detail="Upload a CSV file.")
    raw = await file.read()
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Maximum CSV size is 20 MB.")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise ValueError("CSV header row is missing.")
        normalized = {h.strip().lower().replace(" ", "_"): h for h in reader.fieldnames if h}
        aliases = {
            "trade_date": ["trade_date","date","timestamp"],
            "open": ["open","open_price","opn"],
            "high": ["high","high_price","h"],
            "low": ["low","low_price","l"],
            "close": ["close","close_price","cls","closing_price"],
            "symbol": ["symbol","ticker","underlying"]
        }
        mapping = {}
        for target, candidates in aliases.items():
            found = next((normalized[c] for c in candidates if c in normalized), None)
            if found:
                mapping[target] = found
        missing = [x for x in ["trade_date","open","high","low","close"] if x not in mapping]
        if missing:
            raise ValueError("Required columns missing: " + ", ".join(missing) + ". Required: date, open, high, low, close.")
        accepted, rejected, dates, records = [], 0, [], []
        for line_no, row in enumerate(reader, start=2):
            try:
                raw_date = (row.get(mapping["trade_date"]) or "").strip()
                parsed_date = None
                for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%Y%m%d"):
                    try:
                        parsed_date = datetime.strptime(raw_date[:10], fmt).date()
                        break
                    except ValueError:
                        pass
                if parsed_date is None:
                    raise ValueError("invalid trade date")
                values = {k: float((row.get(mapping[k]) or "").replace(",","")) for k in ["open","high","low","close"]}
                if any(v <= 0 for v in values.values()):
                    raise ValueError("OHLC values must be positive")
                if values["high"] < max(values["open"], values["close"], values["low"]):
                    raise ValueError("high is below another OHLC value")
                if values["low"] > min(values["open"], values["close"], values["high"]):
                    raise ValueError("low is above another OHLC value")
                symbol = (row.get(mapping["symbol"]) or "NIFTY 50").strip() if "symbol" in mapping else "NIFTY 50"
                payload = {k: v for k, v in row.items() if k is not None}
                accepted.append((parsed_date, symbol, payload))
                dates.append(parsed_date)
            except Exception:
                rejected += 1
        if not accepted:
            raise ValueError("No valid rows found. Check the date and OHLC columns.")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="CSV must be UTF-8 encoded.")
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    status = "validated" if rejected == 0 else "validated_with_rejections"
    with connect() as conn:
        item = conn.execute("""
          INSERT INTO datasets(dataset_key,source_name,source_url,file_name,first_date,last_date,
            row_count,accepted_rows,rejected_rows,status,sha256,notes)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (dataset_key,source_name,source_url,file.filename,min(dates),max(dates),
              len(accepted)+rejected,len(accepted),rejected,status,digest,notes)).fetchone()
        dataset_id = item["id"]
        conn.executemany(
          "INSERT INTO market_records(dataset_id,trade_date,symbol,payload) VALUES (%s,%s,%s,%s)",
          [(dataset_id,d,s,json.dumps(p)) for d,s,p in accepted]
        )
        conn.execute(
          "INSERT INTO audit_events(event_type,message,details) VALUES (%s,%s,%s)",
          ("csv_import", "CSV imported and OHLC-validated", json.dumps({"dataset_id":dataset_id,"file":file.filename,"accepted":len(accepted),"rejected":rejected,"sha256":digest}))
        )
    return {"dataset_id":dataset_id,"status":status,"accepted_rows":len(accepted),"rejected_rows":rejected,
            "first_date":min(dates).isoformat(),"last_date":max(dates).isoformat(),"sha256":digest}

@app.get("/api/v1/audit")
def audit():
    with connect() as conn:
        rows = conn.execute("SELECT id,event_type,message,details,created_at FROM audit_events ORDER BY created_at DESC LIMIT 100").fetchall()
    return {"items":[dict(r) for r in rows]}
