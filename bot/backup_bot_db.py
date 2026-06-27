"""
bot/backup_bot_db.py

Запросы к PostgreSQL для модуля бэкапов.
"""
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta

import psycopg2

SERVERS_FILE = "/app/config/servers.json"
DELETABLE_EXTENSIONS = {".bak", ".trn", ".dt", ".zip"}
NO_DELETE_TYPES = {"veeam"}


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
    finally:
        conn.close()


def get_backup_servers() -> list:
    """Список серверов у которых есть данные в backup_metrics."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT server_name
            FROM backup_metrics
            ORDER BY server_name
        """)
        return [row[0] for row in cur.fetchall()]


def get_latest_backup_metrics() -> list:
    """Последние метрики по каждому серверу/типу/пути."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT ON (server_name, backup_type, backup_path)
                server_name, backup_type, backup_path,
                file_count, oldest_file, newest_file,
                total_size_gb, disk_total_gb, disk_free_gb,
                created_at
            FROM backup_metrics
            ORDER BY server_name, backup_type, backup_path, created_at DESC
        """)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_backup_report(server_name: str) -> list:
    """Последние метрики конкретного сервера."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT ON (backup_type, backup_path)
                backup_type, backup_path,
                file_count, oldest_file, newest_file,
                total_size_gb, disk_total_gb, disk_free_gb
            FROM backup_metrics
            WHERE server_name = %s
            ORDER BY backup_type, backup_path, created_at DESC
        """, (server_name,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_db_sizes() -> list:
    """Последние данные о размерах БД по всем серверам."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT ON (server_name, database_name)
                server_name, database_name, size_gb
            FROM database_sizes
            ORDER BY server_name, database_name, collected_at DESC
        """)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_files_for_cleanup(server_name: str, age_days: int) -> list:
    """
    Возвращает список файлов старше age_days для данного сервера.
    Читает servers.json чтобы получить host/username/password и пути.
    Только типы sql и 1c, только разрешённые расширения.
    """
    with open(SERVERS_FILE) as f:
        servers = json.load(f)

    server = next((s for s in servers if s["name"] == server_name), None)
    if not server:
        return []

    host = server["host"]
    username = server.get("username")
    password = server.get("password")
    backups = server.get("backups", {})

    cutoff = datetime.now() - timedelta(days=age_days)

    # Получаем реальный список файлов через WinRM
    from backup_bot_winrm import list_backup_files

    result = []
    for btype, paths in backups.items():
        if btype in NO_DELETE_TYPES:
            continue
        if not isinstance(paths, list):
            paths = [paths]
        for backup_path in paths:
            try:
                files = list_backup_files(host, backup_path, username, password)
                for f in files:
                    ext = os.path.splitext(f["file_name"])[1].lower()
                    if ext not in DELETABLE_EXTENSIONS:
                        continue
                    mod = f["modified"]
                    if isinstance(mod, str):
                        mod = datetime.strptime(mod, "%Y-%m-%d %H:%M:%S")
                    if mod < cutoff:
                        result.append({
                            **f,
                            "host":     host,
                            "username": username,
                            "password": password,
                        })
            except Exception as e:
                print(f"[backup_db] Ошибка listing {server_name} {backup_path}: {e}")

    # Сортируем от старых к новым
    result.sort(key=lambda x: x["modified"])
    return result