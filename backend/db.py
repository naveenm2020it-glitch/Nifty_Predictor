import os
from contextlib import contextmanager
import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost/nifty_predictor")

def connect():
    url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=8)

def init_db():
    with connect() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS datasets (
          id BIGSERIAL PRIMARY KEY,
          dataset_key TEXT NOT NULL,
          source_name TEXT NOT NULL,
          source_url TEXT,
          file_name TEXT NOT NULL,
          imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          first_date DATE,
          last_date DATE,
          row_count INTEGER NOT NULL DEFAULT 0,
          accepted_rows INTEGER NOT NULL DEFAULT 0,
          rejected_rows INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL,
          sha256 TEXT NOT NULL,
          notes TEXT
        );
        CREATE TABLE IF NOT EXISTS market_records (
          id BIGSERIAL PRIMARY KEY,
          dataset_id BIGINT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
          trade_date DATE NOT NULL,
          symbol TEXT,
          payload JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_market_records_date ON market_records(trade_date);
        CREATE INDEX IF NOT EXISTS ix_market_records_dataset ON market_records(dataset_id);
        CREATE TABLE IF NOT EXISTS audit_events (
          id BIGSERIAL PRIMARY KEY,
          event_type TEXT NOT NULL,
          message TEXT NOT NULL,
          details JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)
