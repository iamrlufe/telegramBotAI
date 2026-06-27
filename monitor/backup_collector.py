"""
monitor/backup_collector.py

Сборщик метрик бэкапов. Запускается из monitor.py в отдельном потоке.
Период: каждые 5 минут (совпадает с основным циклом).
"""
import json
import os
from datetime import datetime, timezone
from winrm_client import run_ps
from alerts import send_telegram

SERVERS_FILE = "/app/config/servers.json"

# Расширения по типу бэкапа
EXTENSIONS = {
    "sql":  [".bak", ".trn"],
    "veeam": [],          # не фильтруем, но и не удаляем
    "1c":   [".dt", ".zip"],
}

# Алерт: бэкап старше N часов
BACKUP_ALERT_HOURS = 24
# Алерт: свободное место на диске меньше N %
DISK_ALERT_PCT = 10


# ─── PostgreSQL ───────────────────────────────────────────────

import psycopg2
from contextlib import contextmanager

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


# ─── WinRM: метрики папки бэкапов ────────────────────────────

def collect_backup_path(host: str, backup_path: str, backup_type: str,
                         username: str = None, password: str = None) -> dict:
    """
    Возвращает метрики папки бэкапов через PowerShell.
    """
    exts = EXTENSIONS.get(backup_type, [])
    ext_filter = ""
    if exts:
        conditions = " -or ".join(f'$_.Extension -eq "{e}"' for e in exts)
        ext_filter = f"| Where-Object {{ {conditions} }}"

    script = f"""
    $path = "{backup_path}"
    $extFilter = $true

    if (-not (Test-Path $path)) {{
        @{{ Error = "Path not found" }} | ConvertTo-Json
        return
    }}

    $files = Get-ChildItem -Path $path -Recurse -File -ErrorAction SilentlyContinue {ext_filter}

    $disk = Get-PSDrive -PSProvider FileSystem |
        Where-Object {{ $path.StartsWith($_.Root) }} |
        Select-Object -First 1

    $diskTotal = 0
    $diskFree  = 0
    if ($disk) {{
        $diskTotal = [math]::Round(($disk.Used + $disk.Free) / 1GB, 2)
        $diskFree  = [math]::Round($disk.Free / 1GB, 2)
    }}

    if (-not $files) {{
        @{{
            FileCount  = 0
            TotalGB    = 0
            OldestFile = $null
            NewestFile = $null
            DiskTotalGB = $diskTotal
            DiskFreeGB  = $diskFree
        }} | ConvertTo-Json
        return
    }}

    $sorted  = $files | Sort-Object LastWriteTime
    $oldest  = $sorted | Select-Object -First 1
    $newest  = $sorted | Select-Object -Last 1
    $totalGB = [math]::Round(($files | Measure-Object -Property Length -Sum).Sum / 1GB, 2)

    @{{
        FileCount   = $files.Count
        TotalGB     = $totalGB
        OldestFile  = $oldest.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss")
        NewestFile  = $newest.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss")
        DiskTotalGB = $diskTotal
        DiskFreeGB  = $diskFree
    }} | ConvertTo-Json
    """

    result = run_ps(host, script, username, password)
    data = json.loads(result)

    if data.get("Error"):
        raise RuntimeError(data["Error"])

    def parse_dt(s):
        if not s:
            return None
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")

    return {
        "file_count":    int(data.get("FileCount", 0)),
        "total_size_gb": float(data.get("TotalGB", 0)),
        "oldest_file":   parse_dt(data.get("OldestFile")),
        "newest_file":   parse_dt(data.get("NewestFile")),
        "disk_total_gb": float(data.get("DiskTotalGB", 0)),
        "disk_free_gb":  float(data.get("DiskFreeGB", 0)),
    }


# ─── WinRM: размеры MSSQL баз ────────────────────────────────

def collect_mssql_sizes(host: str, username: str = None, password: str = None) -> list:
    """
    Возвращает список (db_name, size_gb) для всех баз MSSQL.
    """
    script = r"""
    $result = Invoke-Sqlcmd -Query "
        SELECT
            DB_NAME(database_id) AS dbname,
            CAST(SUM(size) * 8.0 / 1024.0 / 1024.0 AS DECIMAL(18,2)) AS size_gb
        FROM sys.master_files
        GROUP BY database_id
        ORDER BY size_gb DESC
    " -ServerInstance "localhost" -ErrorAction Stop
    $result | Select-Object dbname, size_gb | ConvertTo-Json -Depth 2
    """
    result = run_ps(host, script, username, password)
    if not result:
        return []
    data = json.loads(result)
    if isinstance(data, dict):
        data = [data]
    return [(row["dbname"], float(row["size_gb"])) for row in data]


# ─── Сохранение в БД ─────────────────────────────────────────

def save_backup_metric(server_name: str, backup_type: str, backup_path: str, metrics: dict):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO backup_metrics
                (server_name, backup_type, backup_path, file_count,
                 oldest_file, newest_file, total_size_gb, disk_total_gb, disk_free_gb)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            server_name, backup_type, backup_path,
            metrics["file_count"],
            metrics["oldest_file"],
            metrics["newest_file"],
            metrics["total_size_gb"],
            metrics["disk_total_gb"],
            metrics["disk_free_gb"],
        ))


def save_db_sizes(server_name: str, sizes: list):
    with get_conn() as conn:
        cur = conn.cursor()
        for db_name, size_gb in sizes:
            cur.execute("""
                INSERT INTO database_sizes (server_name, database_name, size_gb)
                VALUES (%s, %s, %s)
            """, (server_name, db_name, size_gb))


# ─── Алерты ──────────────────────────────────────────────────

# Состояние алертов в памяти { "server:type:path": "ok"|"warn"|"crit" }
_alert_state: dict = {}


def _check_backup_alerts(server_name: str, backup_type: str,
                          backup_path: str, metrics: dict):
    key = f"{server_name}:{backup_type}:{backup_path}"

    if metrics["file_count"] == 0:
        if _alert_state.get(key) != "empty":
            _alert_state[key] = "empty"
            send_telegram(
                f"🚨 Backup Alert\n\n"
                f"🖥 {server_name}\n"
                f"📁 {backup_path} ({backup_type})\n\n"
                f"❌ Каталог пуст — нет файлов бэкапов"
            )
        return

    # Возраст последнего бэкапа
    if metrics["newest_file"]:
        now = datetime.now()
        age_hours = (now - metrics["newest_file"]).total_seconds() / 3600
        if age_hours > BACKUP_ALERT_HOURS:
            age_days = round(age_hours / 24, 1)
            if _alert_state.get(key) != "old":
                _alert_state[key] = "old"
                send_telegram(
                    f"🚨 Backup Alert\n\n"
                    f"🖥 {server_name}\n"
                    f"📁 {backup_path} ({backup_type})\n\n"
                    f"⏰ Последний backup: {age_days} дн назад\n"
                    f"📅 {metrics['newest_file'].strftime('%d.%m.%Y %H:%M')}"
                )
            return

    # Место на диске
    if metrics["disk_total_gb"] > 0:
        free_pct = round(metrics["disk_free_gb"] / metrics["disk_total_gb"] * 100, 1)
        if free_pct < DISK_ALERT_PCT:
            if _alert_state.get(key + ":disk") != "low":
                _alert_state[key + ":disk"] = "low"
                send_telegram(
                    f"🚨 Backup Alert\n\n"
                    f"🖥 {server_name}\n"
                    f"💽 Диск под backup почти заполнен\n\n"
                    f"Свободно: {metrics['disk_free_gb']} ГБ ({free_pct}%)\n"
                    f"Всего:    {metrics['disk_total_gb']} ГБ"
                )
            return

    # Всё хорошо — сбрасываем состояние
    _alert_state.pop(key, None)
    _alert_state.pop(key + ":disk", None)


# ─── Основной цикл сборщика ──────────────────────────────────

def load_servers() -> list:
    with open(SERVERS_FILE) as f:
        return json.load(f)


def run_backup_cycle():
    try:
        servers = load_servers()
    except Exception as e:
        print(f"[backup] Не могу прочитать {SERVERS_FILE}: {e}", flush=True)
        return

    for server in servers:
        name = server["name"]
        host = server["host"]
        backups = server.get("backups", {})
        dbsize = server.get("dbsize", False)

        if not backups and not dbsize:
            continue

        print(f"[backup] Проверяю: {name} ({host})", flush=True)

        # Собираем метрики по каждому типу и пути
        for backup_type, paths in backups.items():
            if not isinstance(paths, list):
                paths = [paths]
            for backup_path in paths:
                try:
                    metrics = collect_backup_path(
                        host, backup_path, backup_type,
                        server.get("username"), server.get("password")
                    )
                    save_backup_metric(name, backup_type, backup_path, metrics)
                    _check_backup_alerts(name, backup_type, backup_path, metrics)
                    print(
                        f"  [{backup_type}] {backup_path}: "
                        f"{metrics['file_count']} файлов, "
                        f"{metrics['total_size_gb']} ГБ, "
                        f"диск свободно: {metrics['disk_free_gb']} ГБ",
                        flush=True
                    )
                except Exception as e:
                    print(f"  ❌ {backup_type} {backup_path}: {e}", flush=True)

        # Размеры MSSQL баз
        if dbsize:
            try:
                sizes = collect_mssql_sizes(
                    host,
                    server.get("username"),
                    server.get("password")
                )
                save_db_sizes(name, sizes)
                print(f"  🗄 MSSQL: {len(sizes)} баз", flush=True)
            except Exception as e:
                print(f"  ❌ MSSQL dbsize: {e}", flush=True)

    print("[backup] Цикл завершён", flush=True)