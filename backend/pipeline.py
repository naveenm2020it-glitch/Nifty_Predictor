from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import time
import threading
import zipfile
from datetime import date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
import xlrd

from .db import connect

log = logging.getLogger("nifty_iq.pipeline")
IST = ZoneInfo("Asia/Kolkata")
NSE_HOME = "https://www.nseindia.com"
NIFTY_INDICES = "https://www.niftyindices.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
FEATURE_SCHEMA = "nifty-daily-v1"


def _session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Referer": NSE_HOME + "/all-reports-derivatives",
    })
    for url in (NSE_HOME, NSE_HOME + "/all-reports-derivatives"):
        try:
            s.get(url, timeout=(5, 12))
        except requests.RequestException as exc:
            log.warning("NSE warm-up request failed: %s", exc)
    return s


def _date_text(d: date) -> str:
    return d.strftime("%d %b %Y")


def _parse_double_json(response):
    payload = response.json()
    value = payload.get("d") if isinstance(payload, dict) else None
    if value in (None, False, "false", "[]", ""):
        return []
    if isinstance(value, str):
        return json.loads(value)
    return value if isinstance(value, list) else []


def _nifty_index_rows(session, endpoint, start, end, index_name):
    url = NIFTY_INDICES + "/Backpage.aspx/" + endpoint
    body = {
        "cinfo": "{'name':'" + index_name + "','startDate':'" + _date_text(start) +
                 "','endDate':'" + _date_text(end) + "','indexName':'" + index_name + "'}"
    }
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": NIFTY_INDICES + "/reports/historical-data",
        "User-Agent": UA,
    }
    response = session.post(url, json=body, headers=headers, timeout=(10, 60))
    response.raise_for_status()
    return _parse_double_json(response)


def _vix_rows(session, start, end):
    url = NIFTY_INDICES + "/Backpage.aspx/BindHistoricalIndiaVixData"
    body = "{'name':'India VIX','startDate':'" + _date_text(start) + "','endDate':'" + _date_text(end) + "'}"
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": NIFTY_INDICES + "/reports/historical-data",
        "User-Agent": UA,
    }
    response = session.post(url, data=body.encode("utf-8"), headers=headers, timeout=(10, 60))
    response.raise_for_status()
    return _parse_double_json(response)


def _report_url(report_name, day):
    archive = [{
        "name": report_name,
        "type": "daily-reports",
        "category": "derivatives",
    }]
    return (
        NSE_HOME + "/api/reports?archives=" +
        quote(json.dumps(archive, separators=(",", ":"))) +
        "&date=" + quote(day.strftime("%d-%b-%Y")) +
        "&type=derivatives&mode=single"
    )


def _valid_file(response):
    if response.status_code != 200 or not response.content:
        return False
    ctype = response.headers.get("Content-Type", "").lower()
    head = response.content[:300].lstrip().lower()
    return "html" not in ctype and not head.startswith(b"<!doctype html") and not head.startswith(b"<html")


def _download_nse(session, day, report_name, direct_paths=(), timeout=(10, 45)):
    errors = []
    urls = [_report_url(report_name, day)]
    urls.extend("https://nsearchives.nseindia.com/" + p.lstrip("/") for p in direct_paths)
    urls.extend("https://archives.nseindia.com/" + p.lstrip("/") for p in direct_paths)
    for url in urls:
        try:
            response = session.get(url, timeout=timeout, headers={"Referer": NSE_HOME + "/all-reports-derivatives"})
            if _valid_file(response):
                return response.content, url
            errors.append(str(response.status_code) + " " + url)
        except requests.RequestException as exc:
            errors.append(type(exc).__name__ + " " + url)
        time.sleep(0.7)
    raise RuntimeError("Report unavailable: " + report_name + "; " + " | ".join(errors[-3:]))


def _csv_rows(raw, zipped=False):
    if zipped:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = [n for n in archive.namelist() if n.lower().endswith(".csv")]
            if not members:
                raise ValueError("NSE ZIP contained no CSV file.")
            raw = archive.read(members[0])
    text = raw.decode("utf-8-sig", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    return [{str(k).strip(): (v.strip() if isinstance(v, str) else v)
             for k, v in row.items() if k is not None} for row in reader]


def _participant_rows(raw):
    text = raw.decode("utf-8-sig", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    header_index = next((i for i, line in enumerate(lines[:12])
                         if any(token in line.lower() for token in ("client type", "participant", "future index long"))), None)
    if header_index is None:
        header_index = 1 if len(lines) > 1 else 0
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_index:])))
    return [{str(k).strip(): (v.strip() if isinstance(v, str) else v)
             for k, v in row.items() if k is not None} for row in reader]


def _xls_rows(raw):
    book = xlrd.open_workbook(file_contents=raw)
    rows_out = []
    for sheet in book.sheets():
        matrix = [[sheet.cell_value(r, c) for c in range(sheet.ncols)] for r in range(sheet.nrows)]
        header_index = None
        for i, row in enumerate(matrix[:20]):
            vals = [str(x).strip() for x in row if str(x).strip()]
            joined = " ".join(vals).lower()
            if len(vals) >= 2 and any(k in joined for k in ("fii", "index", "future", "option", "long", "short", "particular")):
                header_index = i
                break
        if header_index is None:
            continue
        headers = []
        for i, value in enumerate(matrix[header_index]):
            name = str(value).strip() or "column_" + str(i + 1)
            headers.append(name)
        for row in matrix[header_index + 1:]:
            if not any(str(v).strip() for v in row):
                continue
            rows_out.append({headers[i]: row[i] for i in range(min(len(headers), len(row)))})
    if not rows_out:
        raise ValueError("FII statistics workbook could not be mapped to tabular rows.")
    return rows_out


def _get(row, aliases):
    norm = {''.join(ch.lower() for ch in str(k) if ch.isalnum()): v for k, v in row.items()}
    for alias in aliases:
        value = norm.get(''.join(ch.lower() for ch in alias if ch.isalnum()))
        if value not in (None, ""):
            return value
    return None


def _parse_date(value):
    raw = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%Y%m%d"):
        try:
            return datetime.strptime(raw[:20], fmt).date()
        except ValueError:
            pass
    return None


def _clean_number(value):
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _insert_source(dataset_key, source_key, source_name, source_url, file_name, report_date, raw, rows):
    digest = hashlib.sha256(raw).hexdigest()
    with connect() as conn:
        conn.execute("""
          INSERT INTO source_downloads(source_key,dataset_key,source_name,source_url,file_name,report_date,
            fetched_at,sha256,byte_count,status,parsed_rows)
          VALUES (%s,%s,%s,%s,%s,%s,NOW(),%s,%s,'downloaded',%s)
          ON CONFLICT(source_key,report_date,sha256) DO NOTHING
        """, (source_key, dataset_key, source_name, source_url, file_name, report_date,
              digest, len(raw), len(rows)))
        existing = conn.execute(
            "SELECT id,accepted_rows FROM dataset_imports WHERE dataset_key=%s AND sha256=%s",
            (dataset_key, digest)
        ).fetchone()
        if existing:
            return {"source": source_key, "status": "already_archived", "rows": existing["accepted_rows"], "sha256": digest}
        imported = conn.execute("""
          INSERT INTO dataset_imports(dataset_key,source_name,source_url,file_name,sha256,first_date,last_date,
            row_count,accepted_rows,rejected_rows,status,notes)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0,'ingested_pending_semantic_validation',%s)
          RETURNING id
        """, (dataset_key, source_name, source_url, file_name, digest, report_date, report_date,
              len(rows), len(rows), "Downloaded from official public source; field semantics require validation.")).fetchone()
        import_id = imported["id"]
        records = []
        for row in rows:
            payload = dict(row)
            payload.setdefault("_source_key", source_key)
            payload.setdefault("_report_date", report_date.isoformat())
            payload_text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
            row_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
            trade_date = _parse_date(_get(payload, ["TradDt", "Trade Date", "Date", "HistoricalDate", "date", "TIMESTAMP"])) or report_date
            records.append((dataset_key, trade_date, row_hash, import_id, payload_text))
        if records:
            conn.executemany("""
              INSERT INTO dataset_records(dataset_key,trade_date,record_hash,source_import_id,payload)
              VALUES (%s,%s,%s,%s,%s)
              ON CONFLICT(dataset_key,trade_date,record_hash) DO NOTHING
            """, records)
        conn.execute("""
          INSERT INTO audit_events(event_type,message,details)
          VALUES (%s,%s,%s)
        """, ("official_source_download", "Official source file downloaded and archived",
              json.dumps({"source_key": source_key, "dataset_key": dataset_key, "report_date": report_date.isoformat(),
                          "file_name": file_name, "sha256": digest, "rows": len(rows), "url": source_url})))
    return {"source": source_key, "status": "downloaded", "rows": len(rows), "sha256": digest}


def _store_feature_rows():
    with connect() as conn:
        rows = conn.execute("""
          SELECT trade_date,jsonb_agg(payload) AS payloads,jsonb_agg(record_hash) AS hashes
          FROM dataset_records WHERE dataset_key='spot_ohlc'
          GROUP BY trade_date ORDER BY trade_date DESC LIMIT 80
        """).fetchall()
    rows = list(reversed(rows))
    if len(rows) < 2:
        return {"status": "waiting_for_spot_history", "rows": len(rows)}
    parsed = []
    conflicts = 0
    for record in rows:
        candidates = []
        for p in record["payloads"]:
            o = _clean_number(_get(p, ["OPEN", "Open", "open"]))
            h = _clean_number(_get(p, ["HIGH", "High", "high"]))
            l = _clean_number(_get(p, ["LOW", "Low", "low"]))
            c = _clean_number(_get(p, ["CLOSE", "Close", "close"]))
            if None not in (o, h, l, c) and min(o, h, l, c) > 0 and h >= max(o, c, l) and l <= min(o, c, h):
                candidates.append((o, h, l, c))
        if not candidates:
            continue
        if len(set(candidates)) > 1:
            conflicts += 1
            continue
        o, h, l, c = candidates[0]
        combined_hash = hashlib.sha256("".join(sorted(record["hashes"])).encode()).hexdigest()
        parsed.append({"date": record["trade_date"], "open": o, "high": h, "low": l, "close": c, "hash": combined_hash})
    if len(parsed) < 2:
        return {"status": "blocked_invalid_spot_rows", "rows": len(parsed)}
    last = parsed[-1]
    previous = parsed[-2]
    ranges = [x["high"] - x["low"] for x in parsed[-14:]]
    closes = [x["close"] for x in parsed]
    feature = {
        "open": last["open"], "high": last["high"], "low": last["low"], "close": last["close"],
        "return_1d_pct": (last["close"] / previous["close"] - 1) * 100,
        "gap_pct": (last["open"] / previous["close"] - 1) * 100,
        "range_pct": (last["high"] - last["low"]) / previous["close"] * 100,
        "close_location": (last["close"] - last["low"]) / max(last["high"] - last["low"], 1e-9),
        "atr14_simple": sum(ranges) / len(ranges),
        "sma_5": sum(closes[-5:]) / len(closes[-5:]),
        "sma_10": sum(closes[-10:]) / len(closes[-10:]),
        "sma_20": sum(closes[-20:]) / len(closes[-20:]),
        "sma_50": sum(closes[-50:]) / len(closes[-50:]),
        "available_history_rows": len(parsed),
        "feature_readiness": "incremental_technical_features_only",
    }
    fingerprint = hashlib.sha256((last["hash"] + previous["hash"]).encode()).hexdigest()
    with connect() as conn:
        conn.execute("""
          INSERT INTO daily_feature_snapshots(trade_date,feature_schema_version,input_cutoff,source_fingerprint,features)
          VALUES (%s,%s,%s,%s,%s)
          ON CONFLICT(trade_date,feature_schema_version) DO UPDATE
          SET input_cutoff=EXCLUDED.input_cutoff,source_fingerprint=EXCLUDED.source_fingerprint,
              features=EXCLUDED.features,created_at=NOW()
        """, (last["date"], FEATURE_SCHEMA, last["date"], fingerprint, json.dumps(feature)))
    return {"status": "stored", "trade_date": last["date"].isoformat(), "feature_schema": FEATURE_SCHEMA,
            "history_rows": len(parsed), "conflicting_spot_dates_skipped": conflicts, "features": list(feature.keys())}


def run_evening_ingestion():
    started = datetime.now(IST)
    with connect() as conn:
        run_id = conn.execute("""
          INSERT INTO ingestion_runs(run_type,status,details)
          VALUES ('official_nse_eod','running',%s) RETURNING id
        """, (json.dumps({"started_at_ist": started.isoformat()}),)).fetchone()["id"]
    outcomes = []
    try:
        nifty_session = requests.Session()
        nifty_session.headers.update({"User-Agent": UA, "Content-Type": "application/json; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest", "Referer": NIFTY_INDICES + "/reports/historical-data"})
        today = datetime.now(IST).date()
        start = today - timedelta(days=8)
        spot_rows = _nifty_index_rows(nifty_session, "getHistoricaldatatabletoString", start, today, "NIFTY 50")
        if not spot_rows:
            raise RuntimeError("NIFTY Indices returned no NIFTY 50 OHLC rows for the recent date window.")
        spot_rows = sorted(spot_rows, key=lambda r: _parse_date(r.get("HistoricalDate")) or date.min)
        trade_date = _parse_date(spot_rows[-1].get("HistoricalDate"))
        if not trade_date:
            raise RuntimeError("NIFTY 50 response did not contain a valid trading date.")
        spot = spot_rows[-1]
        for key in ("OPEN", "HIGH", "LOW", "CLOSE"):
            if _clean_number(spot.get(key)) is None:
                raise RuntimeError("NIFTY 50 OHLC row is incomplete; daily run stopped.")
        outcomes.append(_insert_source("spot_ohlc", "nifty50_daily_ohlc_5y", "NSE Indices — NIFTY 50 five-year historical OHLC",
            NIFTY_INDICES + "/reports/historical-data", "nifty50_" + trade_date.isoformat() + ".json",
            trade_date, json.dumps(spot_rows).encode(), spot_rows))

        vix_rows = _vix_rows(nifty_session, start, trade_date)
        vix_for_day = [r for r in vix_rows if _parse_date(r.get("date") or r.get("HistoricalDate")) == trade_date]
        if not vix_for_day:
            outcomes.append({"source": "india_vix", "status": "missing", "error": "No India VIX row for NIFTY trade date."})
        else:
            outcomes.append(_insert_source("vix_volatility", "india_vix", "NSE Indices — India VIX historical OHLC",
                NIFTY_INDICES + "/reports/historical-data", "india_vix_" + trade_date.isoformat() + ".json",
                trade_date, json.dumps(vix_for_day).encode(), vix_for_day))

        nse = _session()
        ymd = trade_date.strftime("%Y%m%d")
        dmy = trade_date.strftime("%d%m%Y")
        fo_name = "BhavCopy_NSE_FO_0_0_0_" + ymd + "_F_0000.csv.zip"
        tasks = [
            ("fo_bhavcopy", "fo_bhavcopy", "F&O - UDiFF Common Bhavcopy Final (zip)", fo_name,
             ["content/fo/" + fo_name], True),
            ("participant_oi", "participant_oi", "F&O - Participant wise Open Interest(csv)",
             "fao_participant_oi_" + dmy + ".csv", ["content/nsccl/fao_participant_oi_" + dmy + ".csv"], False),
            ("fii_derivatives", "fii_derivatives", "F&O - FII Derivatives Statistics",
             "fii_stats_" + trade_date.strftime("%d-%b-%Y") + ".xls", [], "xls"),
            ("vix_volatility", "fo_settlement", "F&O - Daily Settlement Prices",
             "FOSett_prce_" + dmy + ".csv", ["content/fo/FOSett_prce_" + dmy + ".csv"], False),
            ("vix_volatility", "fo_volatility", "F&O - Daily Volatility",
             "FOVOLT_" + dmy + ".csv", ["content/fo/FOVOLT_" + dmy + ".csv"], False),
        ]
        for dataset_key, source_key, report_name, filename, paths, kind in tasks:
            try:
                raw, source_url = _download_nse(nse, trade_date, report_name, paths)
                if kind is True:
                    rows = _csv_rows(raw, zipped=True)
                    nifty_rows = []
                    for row in rows:
                        symbol = str(_get(row, ["TckrSymb", "Symbol", "Ticker", "Undrlyg"])) .upper()
                        if symbol == "NIFTY":
                            nifty_rows.append(row)
                    if not nifty_rows:
                        raise ValueError("F&O bhavcopy downloaded but no NIFTY contracts were found.")
                    rows = nifty_rows
                elif kind == "xls":
                    rows = _xls_rows(raw)
                elif source_key == "participant_oi":
                    rows = _participant_rows(raw)
                else:
                    rows = _csv_rows(raw)
                if source_key == "participant_oi" and rows:
                    rows = [dict(r, _trade_date=trade_date.isoformat()) for r in rows]
                if source_key in ("fo_settlement", "fo_volatility") and rows:
                    filtered_rows = []
                    for row in rows:
                        symbol = _get(row, ["TckrSymb", "Symbol", "Ticker", "Underlying", "Undrlyg", "FinInstrmNm"])
                        if symbol is not None and "NIFTY" not in str(symbol).upper():
                            continue
                        item = dict(row)
                        if symbol is None:
                            item["_scope_note"] = "Exchange-wide report; symbol column unavailable, NIFTY-only semantic mapping required."
                        filtered_rows.append(item)
                    rows = filtered_rows
                if source_key == "fii_derivatives" and rows:
                    rows = [dict(r, _scope_note="FII index derivatives statistics are aggregate index-segment context, not a NIFTY-underlying-only position file.") for r in rows]
                if not rows:
                    raise ValueError("Downloaded report contained no data rows.")
                outcomes.append(_insert_source(dataset_key, source_key, "NSE India — " + report_name,
                    source_url, filename, trade_date, raw, rows))
            except Exception as exc:
                log.exception("Source %s failed", source_key)
                outcomes.append({"source": source_key, "status": "missing", "error": str(exc)[:260]})
        feature_result = _store_feature_rows()
        forecast_result = generate_next_session_prediction()
        required = {"nifty50_daily_ohlc_5y", "india_vix", "fo_bhavcopy", "participant_oi", "fii_derivatives", "fo_settlement", "fo_volatility"}
        good = {x.get("source") for x in outcomes if x.get("status") in ("downloaded", "already_archived")}
        complete = required.issubset(good)
        result = {"trade_date": trade_date.isoformat(), "complete_source_set": complete,
                  "sources": outcomes, "features": feature_result, "forecast": forecast_result,
                  "prediction_status": forecast_result.get("status", "not_generated"),
                  "note": "Downloads are archived with provenance. Model inference remains disabled until semantic validation and walk-forward model approval."}
        with connect() as conn:
            conn.execute("""
              UPDATE ingestion_runs SET status=%s,finished_at=NOW(),details=%s
              WHERE id=%s
            """, ("completed" if complete else "partial", json.dumps(result, default=str), run_id))
            conn.execute("INSERT INTO audit_events(event_type,message,details) VALUES (%s,%s,%s)",
                ("evening_ingestion", "Official-source EOD ingestion completed" if complete else "Official-source EOD ingestion incomplete",
                 json.dumps({"run_id": run_id, "trade_date": trade_date.isoformat(), "complete": complete, "sources": outcomes}, default=str)))
        return {"run_id": run_id, **result}
    except Exception as exc:
        log.exception("Evening ingestion failed")
        with connect() as conn:
            conn.execute("UPDATE ingestion_runs SET status='failed',finished_at=NOW(),error=%s,details=%s WHERE id=%s",
                         (str(exc)[:1000], json.dumps({"sources": outcomes}, default=str), run_id))
        raise



def backfill_historical_features():
    """One-time chronological feature build from already archived NIFTY OHLC rows."""
    with connect() as conn:
        records = conn.execute("""
          SELECT trade_date,jsonb_agg(payload) AS payloads,jsonb_agg(record_hash) AS hashes
          FROM dataset_records WHERE dataset_key='spot_ohlc'
          GROUP BY trade_date ORDER BY trade_date ASC
        """).fetchall()
    parsed = []
    conflicts = 0
    for record in records:
        candidates = []
        for p in record["payloads"]:
            values = {
                "open": _clean_number(_get(p, ["OPEN", "Open", "open"])),
                "high": _clean_number(_get(p, ["HIGH", "High", "high"])),
                "low": _clean_number(_get(p, ["LOW", "Low", "low"])),
                "close": _clean_number(_get(p, ["CLOSE", "Close", "close"])),
            }
            if any(v is None or v <= 0 for v in values.values()):
                continue
            if values["high"] < max(values["open"], values["close"], values["low"]) or values["low"] > min(values["open"], values["close"], values["high"]):
                continue
            candidates.append(values)
        if not candidates:
            continue
        signatures = {tuple(round(v[k], 6) for k in ("open", "high", "low", "close")) for v in candidates}
        if len(signatures) > 1:
            conflicts += 1
            continue
        parsed.append({"date": record["trade_date"], "hash": hashlib.sha256("".join(sorted(record["hashes"])).encode()).hexdigest(), **candidates[0]})
    if len(parsed) < 60:
        return {"status": "blocked_insufficient_history", "valid_spot_rows": len(parsed), "conflicting_spot_dates_skipped": conflicts, "required_minimum": 60}
    feature_rows = []
    for i in range(1, len(parsed)):
        window = parsed[max(0, i - 49):i + 1]
        current = parsed[i]
        previous = parsed[i - 1]
        closes = [x["close"] for x in window]
        ranges = [x["high"] - x["low"] for x in window[-14:]]
        features = {
            "open": current["open"], "high": current["high"], "low": current["low"], "close": current["close"],
            "return_1d_pct": (current["close"] / previous["close"] - 1) * 100,
            "gap_pct": (current["open"] / previous["close"] - 1) * 100,
            "range_pct": (current["high"] - current["low"]) / previous["close"] * 100,
            "close_location": (current["close"] - current["low"]) / max(current["high"] - current["low"], 1e-9),
            "atr14_simple": sum(ranges) / len(ranges),
            "sma_5": sum(closes[-5:]) / len(closes[-5:]),
            "sma_10": sum(closes[-10:]) / len(closes[-10:]),
            "sma_20": sum(closes[-20:]) / len(closes[-20:]),
            "sma_50": sum(closes[-50:]) / len(closes[-50:]),
            "available_history_rows": len(window),
            "feature_readiness": "technical_features_only_not_a_prediction",
        }
        fingerprint = hashlib.sha256((current["hash"] + previous["hash"]).encode()).hexdigest()
        feature_rows.append((current["date"], FEATURE_SCHEMA, current["date"], fingerprint, json.dumps(features)))
    with connect() as conn:
        conn.executemany("""
          INSERT INTO daily_feature_snapshots(trade_date,feature_schema_version,input_cutoff,source_fingerprint,features)
          VALUES (%s,%s,%s,%s,%s)
          ON CONFLICT(trade_date,feature_schema_version) DO UPDATE
          SET input_cutoff=EXCLUDED.input_cutoff,source_fingerprint=EXCLUDED.source_fingerprint,
              features=EXCLUDED.features,created_at=NOW()
        """, feature_rows)
        conn.execute("INSERT INTO audit_events(event_type,message,details) VALUES (%s,%s,%s)",
            ("historical_feature_backfill", "Chronological technical feature snapshots generated from archived NIFTY OHLC",
             json.dumps({"rows": len(feature_rows), "feature_schema": FEATURE_SCHEMA,
                         "first_date": feature_rows[0][0].isoformat(), "last_date": feature_rows[-1][0].isoformat()})))
    return {"status": "completed", "feature_rows": len(feature_rows), "conflicting_spot_dates_skipped": conflicts, "feature_schema": FEATURE_SCHEMA,
            "first_date": feature_rows[0][0].isoformat(), "last_date": feature_rows[-1][0].isoformat(),
            "note": "Feature generation only; no model training or prediction was performed."}


def pipeline_runs(limit=30):
    with connect() as conn:
        rows = conn.execute("""
          SELECT id,run_type,status,started_at,finished_at,details,error
          FROM ingestion_runs ORDER BY started_at DESC LIMIT %s
        """, (limit,)).fetchall()
    return {"items": [dict(r) for r in rows]}


# Long-running five-year historical backfill. Progress is persisted after each
# trading date so a failed run can be restarted without duplicating records.
_history_lock = threading.Lock()

def _history_job_update(job_id, **fields):
    allowed = {"status", "total_dates", "processed_dates", "downloaded_files", "missing_files", "details", "error", "finished_at"}
    sets, values = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        sets.append(key + "=%s")
        values.append(json.dumps(value, default=str) if key == "details" else value)
    sets.append("updated_at=NOW()")
    values.append(job_id)
    with connect() as conn:
        conn.execute("UPDATE historical_download_jobs SET " + ",".join(sets) + " WHERE id=%s", values)

def _source_exists(source_key, report_date):
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM source_downloads WHERE source_key=%s AND report_date=%s AND status='downloaded' LIMIT 1",
            (source_key, report_date)
        ).fetchone()
    return bool(row)

def _run_five_year_history(job_id, start_date, end_date):
    downloaded = 0
    missing = 0
    outcomes = []
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": UA, "Content-Type": "application/json; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest", "Referer": NIFTY_INDICES + "/reports/historical-data"})
        spot_rows = []
        chunk_start = start_date
        while chunk_start <= end_date:
            chunk_end = min(end_date, chunk_start + timedelta(days=364))
            spot_rows.extend(_nifty_index_rows(session, "getHistoricaldatatabletoString", chunk_start, chunk_end, "NIFTY 50"))
            chunk_start = chunk_end + timedelta(days=1)
        spot_rows = sorted(spot_rows, key=lambda r: _parse_date(r.get("HistoricalDate")) or date.min)
        valid_spot = []
        for row in spot_rows:
            d = _parse_date(row.get("HistoricalDate"))
            if not d or d < start_date or d > end_date:
                continue
            if any(_clean_number(row.get(k)) is None for k in ("OPEN", "HIGH", "LOW", "CLOSE")):
                continue
            valid_spot.append((d, row))
        if not valid_spot:
            raise RuntimeError("NSE Indices returned no valid NIFTY 50 OHLC rows for the requested five-year range.")
        trade_dates = sorted({d for d, _ in valid_spot})
        _history_job_update(job_id, total_dates=len(trade_dates), details={"phase":"downloading_index_history","spot_rows":len(valid_spot)})
        # Store the full official index history as one source import, with each row dated individually.
        spot_payload = [row for _, row in valid_spot]
        if not _source_exists("nifty50_daily_ohlc_5y", trade_dates[-1]):
            _insert_source("spot_ohlc", "nifty50_daily_ohlc_5y", "NSE Indices — NIFTY 50 five-year historical OHLC",
                NIFTY_INDICES + "/reports/historical-data", "nifty50_history_" + start_date.isoformat() + "_" + end_date.isoformat() + ".json",
                trade_dates[-1], json.dumps(spot_payload).encode(), spot_payload)
            downloaded += 1

        try:
            vix_rows = []
            chunk_start = start_date
            while chunk_start <= end_date:
                chunk_end = min(end_date, chunk_start + timedelta(days=364))
                vix_rows.extend(_vix_rows(session, chunk_start, chunk_end))
                chunk_start = chunk_end + timedelta(days=1)
            vix_by_date = {}
            for row in vix_rows:
                d = _parse_date(row.get("date") or row.get("HistoricalDate"))
                if d and start_date <= d <= end_date:
                    vix_by_date.setdefault(d, []).append(row)
            for d, rows in sorted(vix_by_date.items()):
                if _source_exists("india_vix", d):
                    continue
                _insert_source("vix_volatility", "india_vix", "NSE Indices — India VIX historical OHLC",
                    NIFTY_INDICES + "/reports/historical-data", "india_vix_" + d.isoformat() + ".json",
                    d, json.dumps(rows).encode(), rows)
                downloaded += 1
        except Exception as exc:
            missing += 1
            outcomes.append({"source":"india_vix_history","status":"missing","error":str(exc)[:240]})

        nse = _session()
        _history_job_update(job_id, status="running", processed_dates=0, downloaded_files=downloaded,
                            missing_files=missing, details={"phase":"downloading_daily_derivatives","trading_dates":len(trade_dates)})
        for index, trade_date in enumerate(trade_dates, start=1):
            ymd = trade_date.strftime("%Y%m%d")
            dmy = trade_date.strftime("%d%m%Y")
            legacy = trade_date.strftime("%d%b%Y").upper()
            tasks = [
                ("fo_bhavcopy","fo_bhavcopy","F&O - UDiFF Common Bhavcopy Final (zip)",
                 "BhavCopy_NSE_FO_0_0_0_" + ymd + "_F_0000.csv.zip",
                 ["content/fo/BhavCopy_NSE_FO_0_0_0_" + ymd + "_F_0000.csv.zip",
                  "content/historical/DERIVATIVES/" + str(trade_date.year) + "/" + trade_date.strftime("%b").upper() + "/fo" + legacy[:2] + legacy[2:5] + legacy[5:] + "bhav.csv.zip"], "zip"),
                ("participant_oi","participant_oi","F&O - Participant wise Open Interest(csv)",
                 "fao_participant_oi_" + dmy + ".csv",["content/nsccl/fao_participant_oi_" + dmy + ".csv"],"participant"),
                ("fii_derivatives","fii_derivatives","F&O - FII Derivatives Statistics",
                 "fii_stats_" + trade_date.strftime("%d-%b-%Y") + ".xls",[],"xls"),
                ("vix_volatility","fo_settlement","F&O - Daily Settlement Prices",
                 "FOSett_prce_" + dmy + ".csv",["content/fo/FOSett_prce_" + dmy + ".csv"],"csv"),
                ("vix_volatility","fo_volatility","F&O - Daily Volatility",
                 "FOVOLT_" + dmy + ".csv",["content/fo/FOVOLT_" + dmy + ".csv"],"csv"),
            ]
            for dataset_key, source_key, report_name, filename, paths, kind in tasks:
                if _source_exists(source_key, trade_date):
                    continue
                try:
                    raw, source_url = _download_nse(nse, trade_date, report_name, paths, timeout=(5, 12))
                    if kind == "zip":
                        rows = _csv_rows(raw, zipped=True)
                        rows = [r for r in rows if str(_get(r, ["TckrSymb","Symbol","Ticker","Undrlyg"] ) or "").upper() == "NIFTY"]
                        if not rows:
                            raise ValueError("Bhavcopy has no NIFTY contracts after symbol filtering.")
                    elif kind == "xls":
                        rows = _xls_rows(raw)
                        rows = [dict(r, _scope_note="Aggregate FII index-derivatives context; not NIFTY-underlying-only.") for r in rows]
                    elif kind == "participant":
                        rows = _participant_rows(raw)
                        rows = [dict(r, _trade_date=trade_date.isoformat()) for r in rows]
                    else:
                        rows = _csv_rows(raw)
                        filtered = []
                        for row in rows:
                            symbol = _get(row, ["TckrSymb","Symbol","Ticker","Underlying","Undrlyg","FinInstrmNm"])
                            if symbol is not None and "NIFTY" not in str(symbol).upper():
                                continue
                            item = dict(row)
                            if symbol is None:
                                item["_scope_note"] = "Exchange-wide report; symbol unavailable; semantic NIFTY mapping required."
                            filtered.append(item)
                        rows = filtered
                    if not rows:
                        raise ValueError("Downloaded report contained no usable rows.")
                    _insert_source(dataset_key, source_key, "NSE India — " + report_name,
                        source_url, filename, trade_date, raw, rows)
                    downloaded += 1
                except Exception as exc:
                    missing += 1
                    outcomes.append({"date":trade_date.isoformat(),"source":source_key,"status":"missing","error":str(exc)[:180]})
                time.sleep(0.25)
            _history_job_update(job_id, status="running", processed_dates=index, downloaded_files=downloaded,
                missing_files=missing, details={"phase":"downloading_daily_derivatives","current_date":trade_date.isoformat(),
                "trading_dates":len(trade_dates),"recent_missing":outcomes[-20:]})
        status = "completed" if missing == 0 else "completed_with_gaps"
        _history_job_update(job_id, status=status, processed_dates=len(trade_dates), downloaded_files=downloaded,
            missing_files=missing, details={"phase":"finished","trading_dates":len(trade_dates),
            "first_trade_date":trade_dates[0].isoformat(),"last_trade_date":trade_dates[-1].isoformat(),
            "recent_missing":outcomes[-100:]}, finished_at=datetime.now(IST))
        with connect() as conn:
            conn.execute("INSERT INTO audit_events(event_type,message,details) VALUES (%s,%s,%s)",
                ("five_year_historical_download","Five-year historical source download finished",
                 json.dumps({"job_id":job_id,"status":status,"downloaded_files":downloaded,"missing_files":missing,
                 "trade_dates":len(trade_dates)},default=str)))
    except Exception as exc:
        log.exception("Five-year historical download failed")
        _history_job_update(job_id, status="failed", downloaded_files=downloaded, missing_files=missing,
            error=str(exc)[:1000], details={"phase":"failed","recent_missing":outcomes[-100:]},
            finished_at=datetime.now(IST))

def start_five_year_historical_download():
    acquired = _history_lock.acquire(blocking=False)
    if not acquired:
        return {"status":"already_running","message":"A historical download is already running."}
    try:
        end_date = datetime.now(IST).date()
        start_date = end_date - timedelta(days=365*5)
        with connect() as conn:
            active = conn.execute("SELECT id,status FROM historical_download_jobs WHERE status IN ('queued','running') ORDER BY started_at DESC LIMIT 1").fetchone()
            if active:
                return {"job_id":active["id"],"status":"already_running"}
            job = conn.execute("""
              INSERT INTO historical_download_jobs(status,start_date,end_date,details)
              VALUES ('queued',%s,%s,%s) RETURNING id
            """,(start_date,end_date,json.dumps({"phase":"queued"}))).fetchone()
        thread = threading.Thread(target=_run_five_year_history,args=(job["id"],start_date,end_date),daemon=True)
        thread.start()
        return {"job_id":job["id"],"status":"started","start_date":start_date.isoformat(),"end_date":end_date.isoformat(),
                "message":"Five-year historical download started. Keep this service available while it runs; progress is saved in the database."}
    finally:
        _history_lock.release()

def historical_download_status():
    with connect() as conn:
        row = conn.execute("SELECT * FROM historical_download_jobs ORDER BY started_at DESC LIMIT 1").fetchone()
    return {"job":dict(row) if row else None}


MODEL_FEATURES = ["return_1d_pct","gap_pct","range_pct","close_location","atr14_pct","close_vs_sma5_pct","close_vs_sma10_pct","close_vs_sma20_pct","close_vs_sma50_pct","sma5_vs_sma20_pct","sma20_vs_sma50_pct"]

def _model_feature_vector(features):
    close = _clean_number(features.get("close"))
    if not close or close <= 0:
        return None
    def val(key):
        value = _clean_number(features.get(key))
        return value if value is not None else 0.0
    return [
        val("return_1d_pct"), val("gap_pct"), val("range_pct"), val("close_location"),
        val("atr14_simple") / close * 100.0,
        (close / val("sma_5") - 1) * 100 if val("sma_5") else 0.0,
        (close / val("sma_10") - 1) * 100 if val("sma_10") else 0.0,
        (close / val("sma_20") - 1) * 100 if val("sma_20") else 0.0,
        (close / val("sma_50") - 1) * 100 if val("sma_50") else 0.0,
        (val("sma_5") / val("sma_20") - 1) * 100 if val("sma_20") else 0.0,
        (val("sma_20") / val("sma_50") - 1) * 100 if val("sma_50") else 0.0,
    ]

def train_walk_forward_model():
    """Train a next-session direction research baseline with chronological evaluation."""
    import pickle
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import accuracy_score, balanced_accuracy_score, brier_score_loss, precision_score, recall_score
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    with connect() as conn:
        rows = conn.execute("SELECT trade_date,features FROM daily_feature_snapshots WHERE feature_schema_version=%s ORDER BY trade_date ASC", (FEATURE_SCHEMA,)).fetchall()
    samples = []
    for i in range(len(rows)-1):
        current, following = rows[i]["features"], rows[i+1]["features"]
        vector = _model_feature_vector(current)
        close, next_close = _clean_number(current.get("close")), _clean_number(following.get("close"))
        if vector is None or not close or not next_close or close == next_close:
            continue
        samples.append({"date": rows[i]["trade_date"], "x": vector, "y": int(next_close > close)})
    n = len(samples)
    if n < 300:
        return {"status":"blocked_insufficient_samples","samples":n,"required":300,
                "note":"At least 300 valid next-session labels are required."}
    X = np.asarray([s["x"] for s in samples], dtype=float)
    y = np.asarray([s["y"] for s in samples], dtype=int)
    initial = max(200, int(n * 0.60))
    remaining = n - initial
    folds = min(5, remaining // 25)
    if folds < 3:
        return {"status":"blocked_insufficient_walk_forward_windows","samples":n}
    fold_size = remaining // folds
    probs, truths, baselines, fold_metrics = [], [], [], []
    for fold in range(folds):
        train_end = initial + fold * fold_size
        test_end = n if fold == folds-1 else min(n, train_end+fold_size)
        if test_end <= train_end:
            continue
        model = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1500,class_weight="balanced",random_state=42))
        model.fit(X[:train_end],y[:train_end])
        probability = model.predict_proba(X[train_end:test_end])[:,1]
        pred = (probability >= .5).astype(int)
        truth = y[train_end:test_end]
        probs.extend(probability.tolist()); truths.extend(truth.tolist())
        baselines.extend([int(np.mean(y[:train_end]) >= .5)] * len(truth))
        fold_metrics.append({"fold":fold+1,"train_samples":train_end,"test_samples":len(truth),
          "test_start":samples[train_end]["date"].isoformat(),"test_end":samples[test_end-1]["date"].isoformat(),
          "accuracy":float(accuracy_score(truth,pred)),"balanced_accuracy":float(balanced_accuracy_score(truth,pred)),
          "brier_score":float(brier_score_loss(truth,probability))})
    p=np.asarray(probs); actual=np.asarray(truths); pred=(p>=.5).astype(int); base=np.asarray(baselines)
    metrics={"out_of_sample_samples":int(len(actual)),"folds":fold_metrics,
      "accuracy":float(accuracy_score(actual,pred)),"balanced_accuracy":float(balanced_accuracy_score(actual,pred)),
      "precision_up":float(precision_score(actual,pred,zero_division=0)),
      "recall_up":float(recall_score(actual,pred,zero_division=0)),
      "brier_score":float(brier_score_loss(actual,p)),
      "majority_baseline_accuracy":float(accuracy_score(actual,base)),
      "beats_majority_baseline":bool(accuracy_score(actual,pred)>accuracy_score(actual,base)),
      "method":"Expanding-window chronological evaluation; preprocessing fitted within each training window.",
      "validation_gate":{"minimum_oos_samples":100,"minimum_folds":3,"minimum_balanced_accuracy":0.52,"maximum_brier_score":0.26,"must_beat_majority_baseline":True},
      "interpretation":"Research evaluation only; not proof of future profitability or calibrated trading odds."}
    final_model=make_pipeline(StandardScaler(),LogisticRegression(max_iter=1500,class_weight="balanced",random_state=42))
    final_model.fit(X,y)
    artifact=pickle.dumps(final_model,protocol=pickle.HIGHEST_PROTOCOL)
    version="nifty-direction-"+datetime.now(IST).strftime("%Y%m%d-%H%M%S")
    schema={"feature_schema":FEATURE_SCHEMA,"features":MODEL_FEATURES,"target":"next_available_session_close_up",
      "training_samples":n,"first_sample":samples[0]["date"].isoformat(),"last_sample":samples[-1]["date"].isoformat(),
      "model_type":"StandardScaler + LogisticRegression",
      "label_definition":"1 when next available NIFTY daily close is greater than current close; ties excluded."}
    status="validated" if len(actual)>=100 and len(fold_metrics)>=3 and metrics["balanced_accuracy"]>=0.52 and metrics["brier_score"]<0.26 and metrics["beats_majority_baseline"] else "rejected"
    with connect() as conn:
        model_row=conn.execute("""INSERT INTO model_versions(model_name,version,training_started_at,training_completed_at,training_cutoff,feature_schema,validation_metrics,status)
          VALUES (%s,%s,NOW(),NOW(),%s,%s,%s,%s) RETURNING id""",
          ("NIFTY next-session direction baseline",version,samples[-1]["date"],json.dumps(schema),json.dumps(metrics),status)).fetchone()
        conn.execute("""INSERT INTO model_artifacts(model_version_id,artifact_name,media_type,sha256,artifact_bytes)
          VALUES (%s,%s,%s,%s,%s)""",
          (model_row["id"],"direction-model.pkl","application/octet-stream",hashlib.sha256(artifact).hexdigest(),artifact))
        conn.execute("INSERT INTO audit_events(event_type,message,details) VALUES (%s,%s,%s)",
          ("model_walk_forward_training","Chronological next-session direction model evaluated and stored",
           json.dumps({"model_id":model_row["id"],"version":version,"status":status,"metrics":metrics})))
    return {"status":status,"model_id":model_row["id"],"version":version,"training_samples":n,"metrics":metrics,"schema":schema}


def generate_next_session_prediction():
    """Generate an evidence-labelled next-session scenario from the latest validated model."""
    import pickle
    import numpy as np
    with connect() as conn:
        model_row=conn.execute("""SELECT m.id,m.version,m.validation_metrics,a.artifact_bytes
          FROM model_versions m JOIN model_artifacts a ON a.model_version_id=m.id
          WHERE m.status='validated' AND a.artifact_name='direction-model.pkl'
          ORDER BY m.created_at DESC LIMIT 1""").fetchone()
        latest=conn.execute("""SELECT trade_date,features FROM daily_feature_snapshots
          WHERE feature_schema_version=%s ORDER BY trade_date DESC LIMIT 1""",(FEATURE_SCHEMA,)).fetchone()
    if not model_row:
        return {"status":"blocked_no_validated_model","note":"Run historical features and walk-forward training first."}
    if not latest:
        return {"status":"blocked_no_features"}
    vector=_model_feature_vector(latest["features"])
    if vector is None:
        return {"status":"blocked_invalid_features"}
    model=pickle.loads(bytes(model_row["artifact_bytes"]))
    p_up=float(model.predict_proba(np.asarray([vector],dtype=float))[0][1])
    p_down=1.0-p_up
    close=float(latest["features"]["close"])
    atr=float(latest["features"].get("atr14_simple") or 0)
    if atr<=0:
        return {"status":"blocked_invalid_atr"}
    bias="bullish" if p_up>=0.55 else "bearish" if p_up<=0.45 else "range_or_uncertain"
    next_day=latest["trade_date"]+timedelta(days=1)
    while next_day.weekday()>=5:
        next_day+=timedelta(days=1)
    scenarios={
      "bias":bias,"probability_up":round(p_up,4),"probability_down":round(p_down,4),
      "reference_close":close,"atr14":atr,
      "bullish":{"condition":"Price holds above reference close and confirms upside continuation.",
        "trigger_reference":round(close+.25*atr,2),"target_zone":[round(close+.5*atr,2),round(close+atr,2)],
        "invalidation":round(close-.35*atr,2)},
      "bearish":{"condition":"Price sustains below reference close and confirms downside continuation.",
        "trigger_reference":round(close-.25*atr,2),"target_zone":[round(close-atr,2),round(close-.5*atr,2)],
        "invalidation":round(close+.35*atr,2)},
      "range":{"condition":"No directional confirmation; avoid forcing a trade.",
        "lower_reference":round(close-.35*atr,2),"upper_reference":round(close+.35*atr,2)},
      "latest_input_date":latest["trade_date"].isoformat(),
      "warning":"Daily index-close direction research only. Not an options entry, intraday signal, guaranteed target or investment advice. Next session date is a weekday estimate and may shift for NSE holidays."}
    with connect() as conn:
        saved=conn.execute("""INSERT INTO predictions(session_date,model_version_id,input_cutoff,data_quality,scenarios,status)
          VALUES (%s,%s,%s,%s,%s,'published')
          ON CONFLICT(session_date,model_version_id) DO UPDATE SET generated_at=NOW(),
          input_cutoff=EXCLUDED.input_cutoff,data_quality=EXCLUDED.data_quality,
          scenarios=EXCLUDED.scenarios,status='published' RETURNING id""",
          (next_day,model_row["id"],latest["trade_date"],
           json.dumps({"feature_schema":FEATURE_SCHEMA,"source_date":latest["trade_date"].isoformat(),
             "walk_forward_metrics":model_row["validation_metrics"]}),json.dumps(scenarios))).fetchone()
        conn.execute("INSERT INTO audit_events(event_type,message,details) VALUES (%s,%s,%s)",
          ("next_session_prediction","Next-session research scenario generated from validated direction model",
           json.dumps({"prediction_id":saved["id"],"model_version":model_row["version"],"session_date":next_day.isoformat()})))
    return {"status":"published","prediction_id":saved["id"],"session_date":next_day.isoformat(),
      "model_version":model_row["version"],"scenarios":scenarios}
