import os
from contextlib import contextmanager
import psycopg2


@contextmanager
def get_conn():
    conn = psycopg2.connect(
        host=os.getenv("POSTGRES_HOST"),
        dbname=os.getenv("POSTGRES_DB"),
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD")
    )
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_disk_metric(server_name: str, disk_name: str, free_gb: float, used_gb: float):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO disk_metrics (server_name, disk_name, free_gb, used_gb)
            VALUES (%s, %s, %s, %s)
            """,
            (server_name, disk_name, free_gb, used_gb)
        )


def save_server_status(server_name: str, status: str, error: str = None,
                       cpu_load: float = None, ram_total: float = None, ram_free: float = None,
                       uptime_seconds: int = None):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO server_status
                (server_name, status, error, cpu_load, ram_total, ram_free, uptime_seconds)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (server_name, status, error, cpu_load, ram_total, ram_free, uptime_seconds)
        )


def save_service_status(server_name: str, service_name: str, display_name: str, status: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO service_status (server_name, service_name, display_name, status)
            VALUES (%s, %s, %s, %s)
            """,
            (server_name, service_name, display_name, status)
        )


def save_process_metrics(server_name: str, metric_type: str, processes: list):
    with get_conn() as conn:
        cur = conn.cursor()
        for process in processes:
            cur.execute(
                """
                INSERT INTO process_metrics
                    (server_name, metric_type, process_name, process_id,
                     cpu_percent, cpu_seconds, memory_mb)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    server_name,
                    metric_type,
                    process.get("Name"),
                    process.get("Id"),
                    process.get("CpuPercent"),
                    process.get("CpuSeconds"),
                    process.get("MemoryMB")
                )
            )


def get_latest_server_status(server_name: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT status
            FROM server_status
            WHERE server_name = %s
            ORDER BY checked_at DESC
            LIMIT 1
            """,
            (server_name,)
        )
        row = cur.fetchone()
    return row[0] if row else None


def cleanup_removed_servers(current_names: list) -> list:
    """
    Удаляет из БД серверы которых нет в servers.json.
    Возвращает список удалённых имён.
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT server_name FROM server_status")
        db_names = {row[0] for row in cur.fetchall()}
        removed = db_names - set(current_names)
        for name in removed:
            cur.execute("DELETE FROM server_status WHERE server_name = %s", (name,))
            cur.execute("DELETE FROM disk_metrics WHERE server_name = %s", (name,))
            cur.execute("DELETE FROM service_status WHERE server_name = %s", (name,))
            cur.execute("DELETE FROM process_metrics WHERE server_name = %s", (name,))
    return list(removed)


def cleanup_old_data(retain_days: int) -> tuple:
    """
    Удаляет записи старше retain_days дней.
    Возвращает (удалено метрик, удалено статусов).
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM disk_metrics WHERE created_at < NOW() - INTERVAL '%s days'",
            (retain_days,)
        )
        deleted_metrics = cur.rowcount
        cur.execute(
            "DELETE FROM server_status WHERE checked_at < NOW() - INTERVAL '%s days'",
            (retain_days,)
        )
        deleted_status = cur.rowcount
        cur.execute(
            "DELETE FROM service_status WHERE checked_at < NOW() - INTERVAL '%s days'",
            (retain_days,)
        )
        deleted_services = cur.rowcount
        cur.execute(
            "DELETE FROM process_metrics WHERE created_at < NOW() - INTERVAL '%s days'",
            (retain_days,)
        )
        deleted_processes = cur.rowcount
    return deleted_metrics, deleted_status, deleted_services, deleted_processes
