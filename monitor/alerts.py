import os
import json
import requests

DISK_STATE_FILE = "/app/data/disk_alert_state.json"
SERVER_STATE_FILE = "/app/data/server_alert_state.json"


# ─── Telegram ────────────────────────────────────────────────

def _get_notify_id() -> str:
    """Группа если задана, иначе личка."""
    group = os.getenv("TELEGRAM_GROUP_ID")
    if group:
        return group
    return os.getenv("TELEGRAM_ALLOWED_USER_ID")


def send_telegram(text: str):
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = _get_notify_id()
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10
        )
    except Exception as e:
        print(f"[alerts] Ошибка отправки в Telegram: {e}", flush=True)


# ─── Состояние (JSON файлы) ──────────────────────────────────

def load_json(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_json(path: str, data: dict):
    with open(path, "w") as f:
        json.dump(data, f)


# ─── Алерты по дискам ────────────────────────────────────────

def check_disk_alert(server_name: str, disk: dict):
    disabled = load_json("/app/data/alerts_disabled.json")
    if server_name in disabled:
        return

    free = float(disk["FreeGB"])
    used = float(disk["UsedGB"])
    total = free + used
    if total <= 0:
        return

    free_pct = round((free / total) * 100, 1)
    key = f"{server_name}:{disk['Name']}"
    state = load_json(DISK_STATE_FILE)
    old_level = state.get(key)

    if free_pct < 5:
        new_level = 5
    elif free_pct < 10:
        new_level = 10
    elif free_pct < 15:
        new_level = 15
    else:
        if key in state:
            del state[key]
            save_json(DISK_STATE_FILE, state)
        return

    if old_level == new_level:
        return

    state[key] = new_level
    save_json(DISK_STATE_FILE, state)

    send_telegram(
        f"🚨 НИЗКОЕ СВОБОДНОЕ МЕСТО\n"
        f"🖥 Сервер: {server_name}\n"
        f"💽 Диск: {disk['Name']}\n"
        f"🔓 Свободно: {free} ГБ ({free_pct}%)\n"
        f"📦 Занято: {used} ГБ\n"
        f"⚠️ Рекомендуется проверить диск"
    )


# ─── Алерты по серверам ──────────────────────────────────────

def _error_to_status(error_text: str) -> tuple:
    e = error_text.lower()
    if "credentials were rejected" in e:
        return "auth_failed", "🔑 Authentication Failed"
    if "access is denied" in e:
        return "access_denied", "⛔ Access Denied"
    if "timed out" in e:
        return "timeout", "⏱ Connection Timeout"
    if "name or service not known" in e:
        return "dns_error", "🌐 DNS Error"
    if "connection refused" in e:
        return "winrm_refused", "⚠️ WinRM Connection Refused"
    if "max retries exceeded" in e:
        return "host_unreachable", "🚨 Host Unreachable"
    return "unknown", "❓ Unknown Error"


def alert_server_online(server: dict):
    state = load_json(SERVER_STATE_FILE)
    name = server["name"]
    old_status = state.get(name)

    if old_status is None:
        state[name] = "online"
        save_json(SERVER_STATE_FILE, state)
        return

    if old_status == "online":
        return

    state[name] = "online"
    save_json(SERVER_STATE_FILE, state)
    send_telegram(
        f"✅ Сервер восстановлен\n"
        f"🖥 {server['name']}\n"
        f"🌐 {server['host']}\n"
        f"Предыдущий статус: {old_status}"
    )


def alert_server_offline(server: dict, error: str):
    disabled = load_json("/app/data/alerts_disabled.json")
    if server["name"] in disabled:
        return

    state = load_json(SERVER_STATE_FILE)
    name = server["name"]
    status, title = _error_to_status(error)

    if state.get(name) == status:
        return

    state[name] = status
    save_json(SERVER_STATE_FILE, state)

    send_telegram(
        f"{title}\n"
        f"🖥 Сервер: {server['name']}\n"
        f"🌐 Хост: {server['host']}\n"
        f"❌ Ошибка: {error}"
    )


def alert_server_down(server: dict):
    disabled = load_json("/app/data/alerts_disabled.json")
    if server["name"] in disabled:
        return

    state = load_json(SERVER_STATE_FILE)
    name = server["name"]

    if state.get(name) == "ping_down":
        return

    state[name] = "ping_down"
    save_json(SERVER_STATE_FILE, state)

    send_telegram(
        f"🚨 Сервер упал\n"
        f"🖥 Сервер: {server['name']}\n"
        f"🌐 Хост: {server['host']}\n"
        f"❌ Ping не отвечает"
    )


# ─── Алерты CPU / RAM ────────────────────────────────────────

CPU_STATE_FILE = "/app/data/cpu_alert_state.json"
RAM_STATE_FILE = "/app/data/ram_alert_state.json"
SERVICE_STATE_FILE = "/app/data/service_alert_state.json"


def check_cpu_alert(server_name: str, cpu_load: float):
    disabled = load_json("/app/data/alerts_disabled.json")
    if server_name in disabled:
        return

    state = load_json(CPU_STATE_FILE)

    if cpu_load >= 90:
        new_level = 90
    elif cpu_load >= 80:
        new_level = 80
    else:
        # Нагрузка в норме — сбрасываем
        if server_name in state:
            del state[server_name]
            save_json(CPU_STATE_FILE, state)
        return

    if state.get(server_name) == new_level:
        return

    state[server_name] = new_level
    save_json(CPU_STATE_FILE, state)

    icon = "🔴" if new_level >= 90 else "🟠"
    send_telegram(
        f"{icon} ВЫСОКАЯ НАГРУЗКА CPU\n"
        f"🖥 Сервер: {server_name}\n"
        f"📊 CPU: {cpu_load}%\n"
        f"⚠️ Рекомендуется проверить процессы"
    )


def check_ram_alert(server_name: str, ram_total: float, ram_free: float):
    disabled = load_json("/app/data/alerts_disabled.json")
    if server_name in disabled:
        return

    if ram_total <= 0:
        return

    ram_used_pct = round((ram_total - ram_free) / ram_total * 100, 1)
    state = load_json(RAM_STATE_FILE)

    if ram_used_pct >= 90:
        new_level = 90
    elif ram_used_pct >= 80:
        new_level = 80
    else:
        if server_name in state:
            del state[server_name]
            save_json(RAM_STATE_FILE, state)
        return

    if state.get(server_name) == new_level:
        return

    state[server_name] = new_level
    save_json(RAM_STATE_FILE, state)

    ram_used = round(ram_total - ram_free, 1)
    icon = "🔴" if new_level >= 90 else "🟠"
    send_telegram(
        f"{icon} ВЫСОКОЕ ПОТРЕБЛЕНИЕ RAM\n"
        f"🖥 Сервер: {server_name}\n"
        f"📊 RAM: {ram_used_pct}% занято\n"
        f"💾 Занято: {ram_used} ГБ из {round(ram_total, 1)} ГБ\n"
        f"⚠️ Рекомендуется проверить процессы"
    )


# ─── Алерты Windows-сервисов ─────────────────────────────────

def check_service_alert(server_name: str, service: dict):
    disabled = load_json("/app/data/alerts_disabled.json")
    if server_name in disabled:
        return

    service_name = service.get("Name") or service.get("name")
    display_name = service.get("DisplayName") or service_name
    status = str(service.get("Status") or service.get("status") or "unknown")
    key = f"{server_name}:{service_name}"
    state = load_json(SERVICE_STATE_FILE)

    if status.lower() == "running":
        if key in state:
            old_status = state.pop(key)
            save_json(SERVICE_STATE_FILE, state)
            send_telegram(
                f"✅ Windows-сервис восстановлен\n"
                f"🖥 Сервер: {server_name}\n"
                f"⚙️ Сервис: {display_name} ({service_name})\n"
                f"Предыдущий статус: {old_status}"
            )
        return

    if state.get(key) == status:
        return

    state[key] = status
    save_json(SERVICE_STATE_FILE, state)
    send_telegram(
        f"🚨 Windows-сервис не запущен\n"
        f"🖥 Сервер: {server_name}\n"
        f"⚙️ Сервис: {display_name} ({service_name})\n"
        f"Статус: {status}"
    )
