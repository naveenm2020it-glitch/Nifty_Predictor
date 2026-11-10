"""Temporary file storage adapter.

The initial Render deployment uses its ephemeral filesystem. Keep this interface
small so a Google Drive adapter can replace it later without changing API routes.
Do not treat this storage as durable: files can disappear after restart/redeploy.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATASET_KEYS = {"spot_ohlc", "fo_bhavcopy", "participant_oi", "fii_derivatives", "vix_volatility"}
DEFAULT_ROOT = "/tmp/nifty-iq-storage"


def _root() -> Path:
    return Path(os.getenv("NIFTY_TEMP_STORAGE_DIR", DEFAULT_ROOT)).expanduser()


def _safe_name(value: str) -> str:
    name = Path(value or "nse-report.csv").name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return (name or "nse-report.csv")[:140]


def save_temporary_file(dataset_key: str, filename: str, payload: bytes) -> dict[str, Any]:
    if dataset_key not in DATASET_KEYS:
        raise ValueError("Unknown dataset key.")
    digest = hashlib.sha256(payload).hexdigest()
    original_name = _safe_name(filename)
    suffix = Path(original_name).suffix.lower()
    if suffix not in {".csv", ".xlsx", ".xls", ".zip"}:
        suffix = ".csv"
    folder = _root() / dataset_key
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{digest}{suffix}"
    metadata_path = folder / f"{digest}{suffix}.meta.json"
    created = not target.exists()
    if created:
        target.write_bytes(payload)
    metadata = {
        "dataset_key": dataset_key,
        "file_name": original_name,
        "stored_name": target.name,
        "sha256": digest,
        "size_bytes": len(payload),
        "stored_at": datetime.now(timezone.utc).isoformat(),
        "storage_provider": "render_ephemeral_filesystem",
    }
    if created or not metadata_path.exists():
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    return {**metadata, "duplicate": not created}


def temporary_storage_status() -> dict[str, Any]:
    root = _root()
    files = 0
    total_bytes = 0
    latest: str | None = None
    if root.exists():
        for metadata_path in root.rglob("*.meta.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                data_path = metadata_path.with_name(metadata["stored_name"])
                if data_path.is_file():
                    files += 1
                    total_bytes += data_path.stat().st_size
                    stamp = metadata.get("stored_at")
                    if stamp and (latest is None or stamp > latest):
                        latest = stamp
            except (OSError, ValueError, KeyError, TypeError):
                continue
    return {
        "status": "available",
        "provider": "render_ephemeral_filesystem",
        "stored_files": files,
        "total_bytes": total_bytes,
        "latest_upload": latest,
        "durable": False,
        "warning": "Temporary only. Render may erase these files after restart, redeploy or instance replacement. Export important files before migrating to Google Drive or another permanent provider.",
    }
