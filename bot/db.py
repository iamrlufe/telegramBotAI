import os
import psycopg2
from contextlib import contextmanager
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from telegram import InlineKeyboardButton

ALMATY = ZoneInfo("Asia/Almaty")
STALE_MINUTES = 15


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


def _make_bar(used_pct: float, width: int = 10) -> str:
    filled = round(used_pct / 100 * width)
    empty = width - filled
    return f"[{'█' * filled}{'░' * empty}] {used_pct}%"


def _format_duration(seconds) -> str:
    if seconds is None:
        return "нет данных"
    seconds = int(seconds)
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    if days:
        return f"{days} д {hours} ч"
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин"


# ─── Состояние серверов со списком кнопок ────────────────────

def get_servers_status() -> tuple:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT ON (server_name)
                   server_name, status, checked_at
            FROM server_status
            ORDER BY server_name, checked_at DESC
        """)
        rows = cur.fetchall()

    if not rows:
        return "⚠️ Нет данных — мониторинг ещё не запускался", []

    now_utc = datetime.now(timezone.utc)
    online = 0
    offline = 0
    buttons = []

    msg = "🖥 СОСТОЯНИЕ СЕРВЕРОВ\n\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n\n"

    for server_name, status, checked_at in sorted(rows):
        checked_utc = checked_at.replace(tzinfo=timezone.utc)
        age_min = (now_utc - checked_utc).total_seconds() / 60
        stale = " ⚠️" if age_min > STALE_MINUTES else ""
        time_str = checked_utc.astimezone(ALMATY).strftime("%H:%M")

        if status == "online":
            icon = "🟢"
            msg += f"🟢 {server_name}{stale} ({time_str})\n"
            online += 1
        else:
            icon = "🔴"
            msg += f"🔴 {server_name} — {status}{stale} ({time_str})\n"
            offline += 1

        buttons.append(
            InlineKeyboardButton(f"{icon} {server_name}", callback_data=f"server:{server_name}")
        )

    # Кнопки по 2 в ряд
    keyboard = [buttons[i:i+2] for i in range(0, len(buttons), 2)]

    total = online + offline
    availability = round((online / total) * 100, 1) if total > 0 else 0

    msg += "\n━━━━━━━━━━━━━━━━━━━━\n\n"
    msg += (
        f"📊 ИТОГО\n\n"
        f"🟢 Онлайн: {online}\n"
        f"🔴 Оффлайн: {offline}\n"
        f"📈 Доступность: {availability}%\n\n"
        f"🕒 Обновляется каждые 5 минут"
    )

    return msg, keyboard


# ─── Детали конкретного сервера ──────────────────────────────

def get_server_detail(server_name: str) -> str:
    with get_conn() as conn:
        cur = conn.cursor()

        cur.execute("""
            SELECT DISTINCT ON (server_name)
                   status, error, checked_at, cpu_load, ram_total, ram_free, uptime_seconds
            FROM server_status
            WHERE server_name = %s
            ORDER BY server_name, checked_at DESC
        """, (server_name,))
        status_row = cur.fetchone()

        cur.execute("""
            SELECT DISTINCT ON (disk_name)
                   disk_name, free_gb, used_gb
            FROM disk_metrics
            WHERE server_name = %s
            ORDER BY disk_name, created_at DESC
        """, (server_name,))
        disk_rows = cur.fetchall()

        cur.execute("""
            SELECT DISTINCT ON (service_name)
                   service_name, display_name, status, checked_at
            FROM service_status
            WHERE server_name = %s
              AND checked_at >= NOW() - INTERVAL '15 minutes'
            ORDER BY service_name, checked_at DESC
        """, (server_name,))
        service_rows = cur.fetchall()

        cur.execute("""
            SELECT process_name, process_id, cpu_percent, cpu_seconds, memory_mb
            FROM process_metrics
            WHERE server_name = %s
              AND metric_type = 'cpu'
              AND created_at = (
                  SELECT MAX(created_at)
                  FROM process_metrics
                  WHERE server_name = %s
                    AND metric_type = 'cpu'
              )
            ORDER BY cpu_percent DESC NULLS LAST
            LIMIT 5
        """, (server_name, server_name))
        top_cpu_rows = cur.fetchall()

        cur.execute("""
            SELECT process_name, process_id, cpu_percent, cpu_seconds, memory_mb
            FROM process_metrics
            WHERE server_name = %s
              AND metric_type = 'memory'
              AND created_at = (
                  SELECT MAX(created_at)
                  FROM process_metrics
                  WHERE server_name = %s
                    AND metric_type = 'memory'
              )
            ORDER BY memory_mb DESC NULLS LAST
            LIMIT 5
        """, (server_name, server_name))
        top_memory_rows = cur.fetchall()

        cur.execute("""
            SELECT
                MIN(cpu_load),
                ROUND(AVG(cpu_load)::numeric, 1),
                MAX(cpu_load),
                MIN(ROUND(((ram_total - ram_free) / NULLIF(ram_total, 0) * 100)::numeric, 1)),
                ROUND(AVG((ram_total - ram_free) / NULLIF(ram_total, 0) * 100)::numeric, 1),
                MAX(ROUND(((ram_total - ram_free) / NULLIF(ram_total, 0) * 100)::numeric, 1))
            FROM server_status
            WHERE server_name = %s
              AND checked_at >= NOW() - INTERVAL '24 hours'
              AND status = 'online'
        """, (server_name,))
        resource_history = cur.fetchone()

        cur.execute("""
            SELECT
                disk_name,
                MIN(ROUND((free_gb / NULLIF(free_gb + used_gb, 0) * 100)::numeric, 1)),
                ROUND(AVG(free_gb / NULLIF(free_gb + used_gb, 0) * 100)::numeric, 1),
                MAX(ROUND((free_gb / NULLIF(free_gb + used_gb, 0) * 100)::numeric, 1))
            FROM disk_metrics
            WHERE server_name = %s
              AND created_at >= NOW() - INTERVAL '24 hours'
            GROUP BY disk_name
            ORDER BY disk_name
        """, (server_name,))
        disk_history = cur.fetchall()

    if not status_row:
        return f"❓ Нет данных по серверу {server_name}"

    status, error, checked_at, cpu_load, ram_total, ram_free, uptime_seconds = status_row
    checked_local = checked_at.replace(tzinfo=timezone.utc).astimezone(ALMATY)
    time_str = checked_local.strftime("%d.%m.%Y %H:%M")
    status_line = "🟢 Онлайн" if status == "online" else f"🔴 {status}"

    msg = f"🖥 {server_name}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n\n"
    msg += f"Статус:   {status_line}\n"
    msg += f"Проверен: {time_str}\n"
    msg += f"Uptime:   {_format_duration(uptime_seconds)}\n"

    if error and status != "online":
        first_line = error.splitlines()[0][:100]
        msg += f"Ошибка:   {first_line}\n"

    # CPU
    if cpu_load is not None:
        cpu_load = float(cpu_load)
        cpu_icon = "🔴" if cpu_load >= 90 else "🟠" if cpu_load >= 70 else "🟢"
        msg += f"\n{cpu_icon} CPU\n"
        msg += f"   {_make_bar(cpu_load)}\n"
        msg += f"   Загрузка: {cpu_load}%\n"

    # RAM
    if ram_total and ram_free:
        ram_total = float(ram_total)
        ram_free = float(ram_free)
        ram_used = ram_total - ram_free
        ram_used_pct = round((ram_used / ram_total) * 100, 1) if ram_total > 0 else 0
        ram_icon = "🔴" if ram_used_pct >= 90 else "🟠" if ram_used_pct >= 70 else "🟢"
        msg += f"\n{ram_icon} RAM\n"
        msg += f"   {_make_bar(ram_used_pct)}\n"
        msg += f"   Занято:  {round(ram_used, 1)} ГБ\n"
        msg += f"   Свободно: {round(ram_free, 1)} ГБ\n"
        msg += f"   Всего:   {round(ram_total, 1)} ГБ\n"

    # Диски
    if disk_rows:
        msg += "\n💽 ДИСКИ\n"
        for disk_name, free, used in disk_rows:
            free = float(free)
            used = float(used)
            total = free + used
            pct_used = round((used / total) * 100, 1) if total > 0 else 0
            pct_free = round(100 - pct_used, 1)
            disk_icon = "🔴" if pct_free < 10 else "🟠" if pct_free < 20 else "🟢"
            msg += f"\n{disk_icon} {disk_name}:\n"
            msg += f"   {_make_bar(pct_used)}\n"
            msg += f"   Свободно: {free} ГБ ({pct_free}%)\n"
            msg += f"   Занято:   {used} ГБ\n"
            msg += f"   Всего:    {round(total, 1)} ГБ\n"
    else:
        msg += "\n💽 Нет данных по дискам\n"

    # Windows-сервисы
    if service_rows:
        msg += "\n⚙️ СЕРВИСЫ\n"
        for service_name, display_name, service_status, service_checked_at in service_rows:
            icon = "🟢" if str(service_status).lower() == "running" else "🔴"
            label = display_name if display_name and display_name != service_name else service_name
            msg += f"   {icon} {label}: {service_status}\n"

    if top_cpu_rows or top_memory_rows:
        msg += "\n🔥 ТОП ПРОЦЕССОВ\n"
        if top_cpu_rows:
            msg += "   CPU:\n"
            for process_name, process_id, cpu_percent, cpu_seconds, memory_mb in top_cpu_rows:
                cpu_percent = cpu_percent if cpu_percent is not None else 0
                msg += f"      {process_name} ({process_id}): {cpu_percent}% CPU, {memory_mb} MB\n"
        if top_memory_rows:
            msg += "   RAM:\n"
            for process_name, process_id, cpu_percent, cpu_seconds, memory_mb in top_memory_rows:
                cpu_percent = cpu_percent if cpu_percent is not None else 0
                msg += f"      {process_name} ({process_id}): {memory_mb} MB, {cpu_percent}% CPU\n"

    # История за 24 часа
    msg += "\n📈 ИСТОРИЯ 24 ЧАСА\n"
    if resource_history and resource_history[0] is not None:
        cpu_min, cpu_avg, cpu_max, ram_min, ram_avg, ram_max = resource_history
        msg += f"   CPU: min {cpu_min}% / avg {cpu_avg}% / max {cpu_max}%\n"
        if ram_min is not None:
            msg += f"   RAM: min {ram_min}% / avg {ram_avg}% / max {ram_max}%\n"
    else:
        msg += "   CPU/RAM: нет данных\n"

    if disk_history:
        for disk_name, free_min, free_avg, free_max in disk_history:
            msg += (
                f"   {disk_name}: свободно min {free_min}% / "
                f"avg {free_avg}% / max {free_max}%\n"
            )
    else:
        msg += "   Диски: нет данных\n"

    return msg


# ─── Проблемы ────────────────────────────────────────────────

def get_problems() -> str:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT server_name, status, error, checked_at
            FROM (
                SELECT DISTINCT ON (server_name)
                       server_name, status, error, checked_at
                FROM server_status
                ORDER BY server_name, checked_at DESC
            ) last
            WHERE status != 'online'
        """)
        rows = cur.fetchall()

    if not rows:
        return "✅ Проблем не обнаружено"

    icons = {
        "auth_failed":      "🔑",
        "access_denied":    "⛔",
        "timeout":          "⏱",
        "dns_error":        "🌐",
        "winrm_refused":    "⚠️",
        "host_unreachable": "🚨",
        "ping_down":        "🚨",
    }

    msg = "🚨 ПРОБЛЕМНЫЕ СЕРВЕРЫ\n\n"
    for server_name, status, error, checked_at in rows:
        icon = icons.get(status, "❓")
        msg += f"{icon} {server_name} ({status})\n"
        if error:
            first_line = error.splitlines()[0][:80]
            msg += f"   {first_line}\n"
    return msg


# ─── Отчёт ───────────────────────────────────────────────────

def build_report(title: str = "📊 ОТЧЁТ ПО ИНФРАСТРУКТУРЕ") -> str:
    with get_conn() as conn:
        cur = conn.cursor()

        cur.execute("SELECT MAX(created_at) FROM disk_metrics")
        last_update = cur.fetchone()[0]

        cur.execute("""
            SELECT DISTINCT ON (server_name, disk_name)
                   server_name, disk_name, free_gb, used_gb
            FROM disk_metrics
            ORDER BY server_name, disk_name, created_at DESC
        """)
        disk_rows = cur.fetchall()

        cur.execute("""
            SELECT DISTINCT ON (server_name)
                   server_name, status, cpu_load, ram_total, ram_free, uptime_seconds
            FROM server_status
            ORDER BY server_name, checked_at DESC
        """)
        status_rows = cur.fetchall()

    server_statuses = {row[0]: row[1:] for row in status_rows}
    disks_by_server = defaultdict(list)
    for server, disk, free, used in disk_rows:
        disks_by_server[server].append((disk, float(free), float(used)))

    all_servers = sorted(set(disks_by_server.keys()) | set(server_statuses.keys()))

    critical = []
    warning = []
    msg = f"{title}\n\n"

    for server in all_servers:
        status_row = server_statuses.get(server)
        status = status_row[0] if status_row else "unknown"
        if status != "online":
            msg += f"🔴 {server} ({status})\n\n"
            continue

        msg += f"🖥 {server}\n"
        cpu_load, ram_total, ram_free, uptime_seconds = status_row[1:]
        if cpu_load is not None:
            cpu_load = float(cpu_load)
            cpu_icon = "🔴" if cpu_load >= 90 else "🟠" if cpu_load >= 70 else "🟢"
            msg += f"   {cpu_icon} CPU: {cpu_load}%\n"
        if ram_total and ram_free:
            ram_total = float(ram_total)
            ram_free = float(ram_free)
            ram_used_pct = round((ram_total - ram_free) / ram_total * 100, 1)
            ram_icon = "🔴" if ram_used_pct >= 90 else "🟠" if ram_used_pct >= 70 else "🟢"
            msg += f"   {ram_icon} RAM: {ram_used_pct}% занято ({ram_free} ГБ свободно)\n"
        if uptime_seconds is not None:
            msg += f"   ⏱ Uptime: {_format_duration(uptime_seconds)}\n"

        for disk, free, used in disks_by_server.get(server, []):
            total = free + used
            pct = round((free / total) * 100, 1) if total > 0 else 0
            if pct < 10:
                icon = "🔴"
                critical.append(f"🔴 {server} → {disk} ({pct}%)")
            elif pct < 20:
                icon = "🟠"
                warning.append(f"🟠 {server} → {disk} ({pct}%)")
            else:
                icon = "🟢"
            msg += f"   {icon} {disk}: {pct}% свободно ({free} ГБ)\n"
        msg += "\n"

    msg += "━━━━━━━━━━━━━━━━━━━━\n"

    if critical:
        msg += "\n🚨 КРИТИЧЕСКИЕ ДИСКИ\n\n"
        for item in critical:
            msg += item + "\n"

    if warning:
        msg += "\n🟠 ПРЕДУПРЕЖДЕНИЕ\n\n"
        for item in warning:
            msg += item + "\n"

    if last_update:
        t = last_update.replace(tzinfo=timezone.utc).astimezone(ALMATY)
        msg += f"\n\n📅 Данные актуальны на: {t.strftime('%d.%m.%Y %H:%M')}"

    return msg
