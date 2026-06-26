CREATE TABLE IF NOT EXISTS disk_metrics (
    id          SERIAL PRIMARY KEY,
    server_name TEXT NOT NULL,
    disk_name   TEXT NOT NULL,
    free_gb     NUMERIC(10,2) NOT NULL,
    used_gb     NUMERIC(10,2) NOT NULL,
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS server_status (
    id          SERIAL PRIMARY KEY,
    server_name TEXT NOT NULL,
    status      TEXT NOT NULL,
    error       TEXT,
    cpu_load    NUMERIC(5,2),
    ram_total   NUMERIC(10,2),
    ram_free    NUMERIC(10,2),
    uptime_seconds BIGINT,
    checked_at  TIMESTAMP DEFAULT NOW()
);

ALTER TABLE server_status
    ADD COLUMN IF NOT EXISTS cpu_load NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS ram_total NUMERIC(10,2),
    ADD COLUMN IF NOT EXISTS ram_free NUMERIC(10,2),
    ADD COLUMN IF NOT EXISTS uptime_seconds BIGINT;

CREATE TABLE IF NOT EXISTS service_status (
    id           SERIAL PRIMARY KEY,
    server_name  TEXT NOT NULL,
    service_name TEXT NOT NULL,
    display_name TEXT,
    status       TEXT NOT NULL,
    checked_at   TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS process_metrics (
    id           SERIAL PRIMARY KEY,
    server_name  TEXT NOT NULL,
    metric_type  TEXT NOT NULL,
    process_name TEXT NOT NULL,
    process_id   INTEGER,
    cpu_percent  NUMERIC(6,2),
    cpu_seconds  NUMERIC(12,2),
    memory_mb    NUMERIC(12,2),
    created_at   TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_disk_server_created
    ON disk_metrics (server_name, disk_name, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_status_server_checked
    ON server_status (server_name, checked_at DESC);

CREATE INDEX IF NOT EXISTS idx_service_server_checked
    ON service_status (server_name, service_name, checked_at DESC);

CREATE INDEX IF NOT EXISTS idx_process_server_created
    ON process_metrics (server_name, metric_type, created_at DESC);
