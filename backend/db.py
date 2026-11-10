import os
from psycopg import connect as psycopg_connect
from psycopg.rows import dict_row

DATABASE_URL = os.getenv("DATABASE_URL", "")

def connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured. Connect a durable PostgreSQL database.")
    url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    return psycopg_connect(url, row_factory=dict_row, connect_timeout=10)

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

        CREATE TABLE IF NOT EXISTS dataset_imports (
          id BIGSERIAL PRIMARY KEY,
          dataset_key TEXT NOT NULL,
          source_name TEXT NOT NULL,
          source_url TEXT,
          file_name TEXT NOT NULL,
          sha256 TEXT NOT NULL,
          imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          first_date DATE,
          last_date DATE,
          row_count INTEGER NOT NULL DEFAULT 0,
          accepted_rows INTEGER NOT NULL DEFAULT 0,
          rejected_rows INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL,
          notes TEXT,
          UNIQUE(dataset_key, sha256)
        );
        CREATE TABLE IF NOT EXISTS dataset_records (
          id BIGSERIAL PRIMARY KEY,
          dataset_key TEXT NOT NULL,
          trade_date DATE NOT NULL,
          record_hash TEXT NOT NULL,
          source_import_id BIGINT NOT NULL REFERENCES dataset_imports(id) ON DELETE RESTRICT,
          payload JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          UNIQUE(dataset_key, trade_date, record_hash)
        );
        CREATE INDEX IF NOT EXISTS ix_dataset_records_key_date ON dataset_records(dataset_key, trade_date);
        CREATE INDEX IF NOT EXISTS ix_dataset_records_import ON dataset_records(source_import_id);

        CREATE TABLE IF NOT EXISTS ingestion_runs (
          id BIGSERIAL PRIMARY KEY,
          run_type TEXT NOT NULL,
          status TEXT NOT NULL,
          started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          finished_at TIMESTAMPTZ,
          details JSONB NOT NULL DEFAULT '{}'::jsonb,
          error TEXT
        );
        CREATE TABLE IF NOT EXISTS source_downloads (
          id BIGSERIAL PRIMARY KEY,
          source_key TEXT NOT NULL,
          dataset_key TEXT NOT NULL,
          source_name TEXT NOT NULL,
          source_url TEXT NOT NULL,
          file_name TEXT NOT NULL,
          report_date DATE NOT NULL,
          fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          sha256 TEXT NOT NULL,
          byte_count BIGINT NOT NULL,
          status TEXT NOT NULL,
          parsed_rows INTEGER NOT NULL DEFAULT 0,
          UNIQUE(source_key, report_date, sha256)
        );
        CREATE INDEX IF NOT EXISTS ix_source_downloads_date ON source_downloads(report_date DESC);
        CREATE TABLE IF NOT EXISTS daily_feature_snapshots (
          id BIGSERIAL PRIMARY KEY,
          trade_date DATE NOT NULL,
          feature_schema_version TEXT NOT NULL,
          input_cutoff DATE NOT NULL,
          source_fingerprint TEXT NOT NULL,
          features JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          UNIQUE(trade_date, feature_schema_version)
        );
        CREATE INDEX IF NOT EXISTS ix_daily_features_date ON daily_feature_snapshots(trade_date DESC);

        CREATE TABLE IF NOT EXISTS model_versions (
          id BIGSERIAL PRIMARY KEY,
          model_name TEXT NOT NULL,
          version TEXT NOT NULL UNIQUE,
          training_started_at TIMESTAMPTZ,
          training_completed_at TIMESTAMPTZ,
          training_cutoff DATE,
          feature_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
          validation_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
          status TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS model_artifacts (
          id BIGSERIAL PRIMARY KEY,
          model_version_id BIGINT NOT NULL REFERENCES model_versions(id) ON DELETE CASCADE,
          artifact_name TEXT NOT NULL,
          media_type TEXT NOT NULL,
          sha256 TEXT NOT NULL,
          artifact_bytes BYTEA NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          UNIQUE(model_version_id, artifact_name)
        );
        CREATE TABLE IF NOT EXISTS predictions (
          id BIGSERIAL PRIMARY KEY,
          session_date DATE NOT NULL,
          generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          model_version_id BIGINT REFERENCES model_versions(id),
          input_cutoff DATE,
          data_quality JSONB NOT NULL DEFAULT '{}'::jsonb,
          scenarios JSONB NOT NULL,
          status TEXT NOT NULL,
          UNIQUE(session_date, model_version_id)
        );
        CREATE TABLE IF NOT EXISTS prediction_outcomes (
          id BIGSERIAL PRIMARY KEY,
          prediction_id BIGINT NOT NULL REFERENCES predictions(id) ON DELETE CASCADE,
          evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          outcome JSONB NOT NULL,
          UNIQUE(prediction_id)
        );
        CREATE TABLE IF NOT EXISTS audit_events (
          id BIGSERIAL PRIMARY KEY,
          event_type TEXT NOT NULL,
          message TEXT NOT NULL,
          details JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS ix_predictions_session ON predictions(session_date DESC);

        CREATE TABLE IF NOT EXISTS selected_analysis_reports (
          id BIGSERIAL PRIMARY KEY,
          trade_date DATE NOT NULL,
          expiry DATE NOT NULL,
          report JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          UNIQUE(trade_date, expiry)
        );
        CREATE INDEX IF NOT EXISTS ix_selected_analysis_created ON selected_analysis_reports(created_at DESC);

        CREATE TABLE IF NOT EXISTS historical_download_jobs (
          id BIGSERIAL PRIMARY KEY,
          status TEXT NOT NULL,
          start_date DATE NOT NULL,
          end_date DATE NOT NULL,
          total_dates INTEGER NOT NULL DEFAULT 0,
          processed_dates INTEGER NOT NULL DEFAULT 0,
          downloaded_files INTEGER NOT NULL DEFAULT 0,
          missing_files INTEGER NOT NULL DEFAULT 0,
          details JSONB NOT NULL DEFAULT '{}'::jsonb,
          error TEXT,
          started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          finished_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS ix_historical_jobs_started ON historical_download_jobs(started_at DESC);
        CREATE INDEX IF NOT EXISTS ix_ingestion_runs_started ON ingestion_runs(started_at DESC);
        """)
