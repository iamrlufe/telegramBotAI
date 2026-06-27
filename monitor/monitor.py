import json
import time
import os
import subprocess
import threading
from datetime import datetime, timezone

from check_disks import check_server
from db import (
    save_disk_metric,
    save_server_status,
    save_service_status,
    save_process_metrics,
    get_latest_server_status,
    cleanup_removed_servers,
    cleanup_old_data
)
from alerts import (
    check_disk_alert,
    alert_server_online,
    alert_server_offline,
    alert_server_down,
    check_cpu_alert,
    check_ram_alert,
    check_service_alert
)

INTERVAL = 300
PING_INTERVAL = 30
RETRY_DELAY = 60       # пауза перед повторной попыткой WinRM (сек)
RETRY_COUNT = 2        # количество попыток WinRM перед алертом офлайн
PING_FAIL_THRESHOLD = 15  # сколько подряд неудачных пингов = сервер упал
SERVERS_FILE = "/app/config/servers.json"

DISK_STATE_FILE = "/app/data/disk_alert_state.json"
SERVER_STATE_FILE = "/app/data/server_alert_state.json"
ALERTS_DISABLED_FILE = "/app/data/alerts_disabled.json"
CLEANUP_STATE_FILE = "/app/data/last_cleanup.txt"
CPU_STATE_FILE = "/app/data/cpu_alert_state.json"
RAM_STATE_FILE = "/app/data/ram_alert_state.json"

RETAIN_DAYS = 30

# Счётчики неудачных пингов — хранятся в памяти
# { server_name: int }
_ping_fail_counts: dict = {}
_ping_lock = threading.Lock()


def ping_host(host: str) -> bool:
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "2", host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=4
        )
        return result.returncode == 0
    except Exception:
        return False


def load_servers() -> list:
    with open(SERVERS_FILE) as f:
        return json.load(f)


def parse_status(error_text: str) -> str:
    e = error_text.lower()
    if "credentials were rejected" in e:
        return "auth_failed"
    if "access is denied" in e:
        return "access_denied"
    if "timed out" in e:
        return "timeout"
    if "name or service not known" in e:
        return "dns_error"
    if "connection refused" in e:
        return "winrm_refused"
    if "max retries exceeded" in e:
        return "host_unreachable"
    return "unknown"


def try_check_server(server: dict) -> dict:
    name = server["name"]
    last_error = None

    for attempt in range(1, RETRY_COUNT + 1):
        try:
            return check_server(server)
        except Exception as e:
            last_error = e
            if attempt < RETRY_COUNT:
                print(f"  ⚠️ Попытка {attempt}/{RETRY_COUNT} не удалась: {e}", flush=True)
                print(f"  ⏳ Повтор через {RETRY_DELAY} сек...", flush=True)
                time.sleep(RETRY_DELAY)
            else:
                print(f"  ❌ Все {RETRY_COUNT} попытки исчерпаны для {name}", flush=True)

    raise last_error


def maybe_cleanup():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with open(CLEANUP_STATE_FILE) as f:
            last = f.read().strip()
        if last == today:
            return
    except FileNotFoundError:
        pass

    print(f"[monitor] Очистка данных старше {RETAIN_DAYS} дней...", flush=True)
    deleted_metrics, deleted_status, deleted_services, deleted_processes = cleanup_old_data(RETAIN_DAYS)
    print(
        f"[monitor] Удалено: {deleted_metrics} метрик, "
        f"{deleted_status} статусов, {deleted_services} сервисов, "
        f"{deleted_processes} процессов",
        flush=True
    )

    with open(CLEANUP_STATE_FILE, "w") as f:
        f.write(today)


def run_ping_cycle():
    try:
        servers = load_servers()
    except Exception as e:
        print(f"[ping] Не могу прочитать {SERVERS_FILE}: {e}", flush=True)
        return

    for server in servers:
        name = server["name"]
        host = server["host"]
        is_alive = ping_host(host)

        with _ping_lock:
            if is_alive:
                was_down = _ping_fail_counts.get(name, 0) >= PING_FAIL_THRESHOLD
                _ping_fail_counts[name] = 0

                if was_down:
                    # Сервер восстановился — снимаем статус ping_down
                    print(f"[ping] ВОССТАНОВЛЕН {name} ({host})", flush=True)
                    save_server_status(name, "online", error="Ping восстановлен")
                    alert_server_online(server)
            else:
                count = _ping_fail_counts.get(name, 0) + 1
                _ping_fail_counts[name] = count
                print(
                    f"[ping] НЕТ ОТВЕТА {name} ({host}) — "
                    f"{count}/{PING_FAIL_THRESHOLD}",
                    flush=True
                )

                if count == PING_FAIL_THRESHOLD:
                    # Только сейчас фиксируем падение
                    print(f"[ping] DOWN {name} ({host}) — порог достигнут", flush=True)
                    save_server_status(name, "ping_down", error="Ping не отвечает")
                    alert_server_down(server)
                elif count > PING_FAIL_THRESHOLD:
                    # Уже зафиксировано — просто логируем, не спамим
                    print(f"[ping] Всё ещё недоступен {name} ({host})", flush=True)


def ping_loop():
    while True:
        run_ping_cycle()
        time.sleep(PING_INTERVAL)


def run_cycle():
    try:
        servers = load_servers()
    except Exception as e:
        print(f"[monitor] Не могу прочитать {SERVERS_FILE}: {e}", flush=True)
        return

    current_names = [s["name"] for s in servers]
    removed = cleanup_removed_servers(current_names)
    for name in removed:
        print(f"[monitor] Удалён из БД: {name}", flush=True)

    maybe_cleanup()

    for server in servers:
        name = server["name"]
        host = server["host"]
        print(f"[monitor] Проверяю: {name} ({host})", flush=True)

        try:
            if not ping_host(host):
                # В основном цикле просто логируем — алерт идёт из ping_loop
                print(f"[monitor] Пинг не прошёл {name}, пропускаю WinRM", flush=True)
                continue

            info = try_check_server(server)

            for disk in info["disks"]:
                free = float(disk["FreeGB"])
                used = float(disk["UsedGB"])
                print(f"  💽 {disk['Name']}: free={free}GB used={used}GB", flush=True)
                save_disk_metric(name, disk["Name"], free, used)
                check_disk_alert(name, disk)

            uptime_hours = round(info["uptime_seconds"] / 3600, 1) if info["uptime_seconds"] else 0
            print(
                f"  🖥 CPU={info['cpu_load']}% RAM free={info['ram_free']}GB "
                f"uptime={uptime_hours}h",
                flush=True
            )

            save_server_status(
                name, "online",
                cpu_load=info["cpu_load"],
                ram_total=info["ram_total"],
                ram_free=info["ram_free"],
                uptime_seconds=info["uptime_seconds"]
            )
            alert_server_online(server)
            check_cpu_alert(name, info["cpu_load"])
            check_ram_alert(name, info["ram_total"], info["ram_free"])
            save_process_metrics(name, "cpu", info.get("top_cpu", []))
            save_process_metrics(name, "memory", info.get("top_memory", []))

            for service in info.get("services", []):
                service_name = service.get("Name")
                display_name = service.get("Label") or service.get("DisplayName") or service_name
                service_status = service.get("Status", "unknown")
                if not service_name:
                    continue

                print(f"  ⚙️ {service_name}: {service_status}", flush=True)
                save_service_status(name, service_name, display_name, service_status)
                check_service_alert(name, service)

        except Exception as e:
            error_str = str(e)
            status = parse_status(error_str)
            print(f"[monitor] ОФЛАЙН {name}: {status}", flush=True)
            save_server_status(name, status, error=error_str)
            alert_server_offline(server, error_str)

    print("[monitor] Цикл завершён\n", flush=True)


def main():
    print("[monitor] AgentMonitor запущен", flush=True)
    print(f"[monitor] Интервал: {INTERVAL} сек, retry: {RETRY_COUNT}x{RETRY_DELAY}сек", flush=True)
    print(f"[monitor] Ping-мониторинг: каждые {PING_INTERVAL} сек, порог: {PING_FAIL_THRESHOLD} неудач", flush=True)
    print(f"[monitor] Хранение данных: {RETAIN_DAYS} дней", flush=True)

    os.makedirs("/app/data", exist_ok=True)
    for path in [
        DISK_STATE_FILE,
        SERVER_STATE_FILE,
        ALERTS_DISABLED_FILE,
        CPU_STATE_FILE,
        RAM_STATE_FILE,
        "/app/data/service_alert_state.json"
    ]:
        if not os.path.exists(path):
            with open(path, "w") as f:
                f.write("{}")
            print(f"[monitor] Создан файл состояния: {path}", flush=True)

    threading.Thread(target=ping_loop, daemon=True).start()

    while True:
        run_cycle()
        print(f"[monitor] Следующая проверка через {INTERVAL} сек...", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()