import os
import tempfile
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import psycopg2

ALMATY = ZoneInfo("Asia/Almaty")


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


def _to_local(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(ALMATY)


def _setup_time_axis(ax):
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=ALMATY))
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=3))
    ax.grid(True, color="#e5e7eb", linewidth=0.8)
    ax.set_axisbelow(True)


def build_server_chart(server_name: str) -> str:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT checked_at, status, cpu_load, ram_total, ram_free
            FROM server_status
            WHERE server_name = %s
              AND checked_at >= NOW() - INTERVAL '24 hours'
            ORDER BY checked_at
        """, (server_name,))
        status_rows = cur.fetchall()

        cur.execute("""
            SELECT created_at, disk_name, free_gb, used_gb
            FROM disk_metrics
            WHERE server_name = %s
              AND created_at >= NOW() - INTERVAL '24 hours'
            ORDER BY created_at
        """, (server_name,))
        disk_rows = cur.fetchall()

    if not status_rows and not disk_rows:
        raise ValueError(f"Нет данных за 24 часа по серверу {server_name}")

    fig, axes = plt.subplots(3, 1, figsize=(12, 9), sharex=True)
    fig.patch.set_facecolor("#f8fafc")
    fig.suptitle(f"{server_name} · 24 часа", fontsize=18, fontweight="bold", color="#0f172a")

    # Доступность
    ax = axes[0]
    if status_rows:
        times = [_to_local(row[0]) for row in status_rows]
        values = [1 if row[1] == "online" else 0 for row in status_rows]
        availability = round(sum(values) / len(values) * 100, 1)
        colors = ["#16a34a" if value else "#ef4444" for value in values]
        ax.scatter(times, values, c=colors, s=22)
        ax.step(times, values, where="post", color="#334155", linewidth=1.5, alpha=0.7)
        ax.set_title(f"Доступность: {availability}%", loc="left", fontsize=12, fontweight="bold")
    else:
        ax.set_title("Доступность: нет данных", loc="left", fontsize=12, fontweight="bold")
    ax.set_ylim(-0.2, 1.2)
    ax.set_yticks([0, 1])
    ax.set_yticklabels(["down", "online"])
    _setup_time_axis(ax)

    # CPU/RAM
    ax = axes[1]
    cpu_times = []
    cpu_values = []
    ram_times = []
    ram_values = []
    for checked_at, status, cpu_load, ram_total, ram_free in status_rows:
        t = _to_local(checked_at)
        if cpu_load is not None:
            cpu_times.append(t)
            cpu_values.append(float(cpu_load))
        if ram_total and ram_free:
            ram_total = float(ram_total)
            ram_free = float(ram_free)
            if ram_total > 0:
                ram_times.append(t)
                ram_values.append(round((ram_total - ram_free) / ram_total * 100, 1))

    if cpu_times:
        ax.plot(cpu_times, cpu_values, color="#ef4444", linewidth=2.2, label="CPU")
    if ram_times:
        ax.plot(ram_times, ram_values, color="#2563eb", linewidth=2.2, label="RAM")
    ax.axhline(90, color="#ef4444", linestyle="--", linewidth=1, alpha=0.7)
    ax.axhline(80, color="#f59e0b", linestyle="--", linewidth=1, alpha=0.7)
    ax.set_title("CPU / RAM", loc="left", fontsize=12, fontweight="bold")
    ax.set_ylabel("%")
    ax.set_ylim(0, 100)
    ax.legend(loc="upper right")
    _setup_time_axis(ax)

    # Диски
    ax = axes[2]
    disks = defaultdict(lambda: {"times": [], "free_pct": []})
    for created_at, disk_name, free_gb, used_gb in disk_rows:
        free = float(free_gb)
        used = float(used_gb)
        total = free + used
        if total <= 0:
            continue
        disks[disk_name]["times"].append(_to_local(created_at))
        disks[disk_name]["free_pct"].append(round(free / total * 100, 1))

    for disk_name, data in sorted(disks.items()):
        ax.plot(data["times"], data["free_pct"], linewidth=2, label=f"{disk_name}:")
    ax.axhline(20, color="#f59e0b", linestyle="--", linewidth=1, alpha=0.7)
    ax.axhline(10, color="#ef4444", linestyle="--", linewidth=1, alpha=0.7)
    ax.set_title("Свободное место на дисках", loc="left", fontsize=12, fontweight="bold")
    ax.set_ylabel("% свободно")
    ax.set_ylim(0, 100)
    if disks:
        ax.legend(loc="upper right", ncol=2)
    _setup_time_axis(ax)

    generated_at = datetime.now(ALMATY).strftime("%d.%m.%Y %H:%M")
    fig.text(0.01, 0.01, f"AgentMonitor · сформировано {generated_at}", fontsize=9, color="#64748b")
    fig.tight_layout(rect=(0, 0.03, 1, 0.95))

    fd, path = tempfile.mkstemp(prefix=f"{server_name}_24h_", suffix=".png")
    os.close(fd)
    fig.savefig(path, dpi=140, facecolor=fig.get_facecolor())
    plt.close(fig)
    return path
