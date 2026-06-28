"""
bot/backup_bot.py

Telegram-интерфейс раздела 💾 Бэкапы.
"""
import json
import os
import asyncio
from datetime import datetime, timezone

from telegram import InlineKeyboardMarkup, InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from backup_bot_db import (
    get_latest_backup_metrics,
    get_backup_report,
    get_db_sizes,
    get_files_for_cleanup,
    get_backup_servers,
)
from backup_bot_winrm import delete_backup_files

CLEANUP_PENDING_FILE = "/app/data/cleanup_pending.json"

# Расширения которые разрешено удалять
DELETABLE_EXTENSIONS = {".bak", ".trn", ".dt", ".zip"}
# Типы бэкапов которые нельзя удалять
NO_DELETE_TYPES = {"veeam"}


# ─── Helpers ─────────────────────────────────────────────────

def fmt_size(gb: float) -> str:
    if gb >= 1000:
        return f"{gb/1024:.2f} ТБ"
    if gb >= 1:
        return f"{gb:.2f} ГБ"
    return f"{gb*1024:.1f} МБ"


def fmt_date(dt) -> str:
    if not dt:
        return "нет данных"
    return dt.strftime("%d.%m.%Y %H:%M") if hasattr(dt, "strftime") else str(dt)


def fmt_age(dt) -> str:
    if not dt:
        return "?"

    if isinstance(dt, str):
        try:
            dt = datetime.strptime(dt, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return "?"

    if getattr(dt, "tzinfo", None):
        dt = dt.replace(tzinfo=None)

    delta = datetime.now() - dt

    days = delta.days
    if days >= 1:
        return f"{days} дн назад"

    hours = int(delta.total_seconds() // 3600)
    if hours >= 1:
        return f"{hours} ч назад"

    minutes = int(delta.total_seconds() // 60)
    return f"{minutes} мин назад"


def back_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("◀️ Назад", callback_data="backup_menu")
    ]])


def backup_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Backup Health", callback_data="backup_health")],
        [InlineKeyboardButton("📦 Backup Report", callback_data="backup_report_servers")],
        [InlineKeyboardButton("🗄 DB Size",        callback_data="backup_dbsize")],
        [InlineKeyboardButton("🧹 Cleanup",        callback_data="backup_cleanup_servers")],
    ])


# ─── Меню ────────────────────────────────────────────────────

async def cmd_backup_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💾 БЭКАПЫ\n\nВыбери раздел:",
        reply_markup=backup_menu_kb()
    )


# ─── Backup Health ────────────────────────────────────────────

async def show_backup_health(query, context):
    rows = await asyncio.to_thread(get_latest_backup_metrics)

    if not rows:
        await query.edit_message_text(
            "⚠️ Нет данных. Дождитесь первого цикла сбора (до 5 минут).",
            reply_markup=back_kb()
        )
        return

    # Группируем по серверу
    from collections import defaultdict
    servers = defaultdict(list)
    for row in rows:
        servers[row["server_name"]].append(row)

    ok = warn = crit = 0
    details = []

    for server_name, items in sorted(servers.items()):
        block = [f"🖥 {server_name}"]
        for item in items:
            btype = item["backup_type"]
            path = item["backup_path"]
            file_count = item["file_count"] or 0
            newest = item["newest_file"]
            disk_total = float(item["disk_total_gb"] or 0)
            disk_free = float(item["disk_free_gb"] or 0)
            free_pct = round(disk_free / disk_total * 100, 1) if disk_total > 0 else 100

            if file_count == 0 or free_pct < 10:
                icon = "🔴"
                crit += 1
            elif newest:
                newest_naive = newest.replace(tzinfo=None) if hasattr(newest, "replace") else newest
                age_h = (datetime.now() - newest_naive).total_seconds() / 3600
                if age_h > 24:
                    icon = "🟠"
                    warn += 1
                else:
                    icon = "✅"
                    ok += 1
            else:
                icon = "🔴"
                crit += 1

            block.append(f"   {icon} {btype.upper()} — {path}")
            if file_count == 0:
                block.append(f"      ❌ Каталог пуст")
            else:
                block.append(f"      Файлов: {file_count} | Новый: {fmt_age(newest)}")
            if free_pct < 10:
                block.append(f"      ⚠️ Свободно: {free_pct}%")

        details.append("\n".join(block))

    total = ok + warn + crit
    header = (
        f"📊 BACKUP HEALTH\n\n"
        f"Серверов: {total}\n"
        f"✅ Норма: {ok}\n"
        f"🟠 Предупреждение: {warn}\n"
        f"🔴 Ошибка: {crit}\n\n"
        f"{'━'*20}\n\n"
    )

    text = header + "\n\n".join(details)
    if len(text) > 4000:
        text = text[:3950] + "\n\n⚠️ Обрезано"

    await query.edit_message_text(text, reply_markup=back_kb())


# ─── Backup Report: выбор сервера ────────────────────────────

async def show_report_servers(query, context):
    servers = await asyncio.to_thread(get_backup_servers)
    if not servers:
        await query.edit_message_text("⚠️ Нет данных по бэкапам.", reply_markup=back_kb())
        return

    buttons = [
        [InlineKeyboardButton(s, callback_data=f"backup_report:{s}")]
        for s in servers
    ]
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="backup_menu")])
    await query.edit_message_text(
        "📦 BACKUP REPORT\n\nВыбери сервер:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def show_report_server(query, context, server_name: str):
    rows = await asyncio.to_thread(get_backup_report, server_name)
    if not rows:
        await query.edit_message_text(
            f"⚠️ Нет данных по {server_name}",
            reply_markup=back_kb()
        )
        return

    lines = [f"💾 {server_name}\n"]
    for row in rows:
        btype = row["backup_type"]
        path = row["backup_path"]
        file_count = row["file_count"] or 0
        oldest = row["oldest_file"]
        newest = row["newest_file"]
        total_gb = float(row["total_size_gb"] or 0)
        disk_total = float(row["disk_total_gb"] or 0)
        disk_free = float(row["disk_free_gb"] or 0)
        free_pct = round(disk_free / disk_total * 100, 1) if disk_total > 0 else 0
        used_pct = round(100 - free_pct, 1)

        # Срок хранения
        retention = "?"
        if oldest and newest:
            oldest_n = oldest.replace(tzinfo=None)
            newest_n = newest.replace(tzinfo=None)
            retention = f"{(newest_n - oldest_n).days} дней"

        lines += [
            f"{'━'*20}",
            f"Тип:      {btype.upper()}",
            f"Путь:     {path}",
            f"Файлов:   {file_count}",
            f"Старый:   {fmt_date(oldest)}",
            f"Новый:    {fmt_date(newest)} ({fmt_age(newest)})",
            f"Хранение: {retention}",
            f"Размер:   {fmt_size(total_gb)}",
            f"Диск:     {fmt_size(disk_total)}",
            f"Свободно: {fmt_size(disk_free)} ({free_pct}%)",
            f"Занято:   {used_pct}%",
            "",
        ]

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3950] + "\n\n⚠️ Обрезано"

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("◀️ Назад", callback_data="backup_report_servers")
    ]])
    await query.edit_message_text(text, reply_markup=kb)


# ─── DB Size ─────────────────────────────────────────────────

async def show_dbsize(query, context):
    rows = await asyncio.to_thread(get_db_sizes)
    if not rows:
        await query.edit_message_text(
            "⚠️ Нет данных. Убедитесь что dbsize=true в servers.json.",
            reply_markup=back_kb()
        )
        return

    from collections import defaultdict
    by_server = defaultdict(list)
    for row in rows:
        by_server[row["server_name"]].append(row)

    lines = ["🗄 РАЗМЕР БАЗ ДАННЫХ\n"]
    for server_name, dbs in sorted(by_server.items()):
        lines.append(f"🖥 {server_name}\n")
        for db in dbs:
            size_gb = float(db["size_gb"] or 0)
            lines.append(f"   📊 {db['database_name']}: {fmt_size(size_gb)}")
        lines.append("")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3950] + "\n\n⚠️ Обрезано"

    await query.edit_message_text(text, reply_markup=back_kb())


# ─── Cleanup: выбор сервера ───────────────────────────────────

async def show_cleanup_servers(query, context):
    servers = await asyncio.to_thread(get_backup_servers)
    if not servers:
        await query.edit_message_text("⚠️ Нет данных по бэкапам.", reply_markup=back_kb())
        return

    buttons = [
        [InlineKeyboardButton(s, callback_data=f"backup_cleanup:{s}")]
        for s in servers
    ]
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="backup_menu")])
    await query.edit_message_text(
        "🧹 CLEANUP\n\nВыбери сервер:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def show_cleanup_server(query, context, server_name: str):
    """Показать анализ и кнопки выбора возраста для удаления."""
    rows = await asyncio.to_thread(get_backup_report, server_name)
    if not rows:
        await query.edit_message_text(f"⚠️ Нет данных по {server_name}", reply_markup=back_kb())
        return

    lines = [f"🧹 {server_name}\n"]
    has_deletable = False

    for row in rows:
        btype = row["backup_type"]
        if btype in NO_DELETE_TYPES:
            lines.append(f"{'━'*20}")
            lines.append(f"📁 {btype.upper()} — {row['backup_path']}")
            lines.append(f"   ⛔ Veeam не удаляется")
            lines.append("")
            continue

        has_deletable = True
        path = row["backup_path"]
        file_count = row["file_count"] or 0
        oldest = row["oldest_file"]
        newest = row["newest_file"]
        total_gb = float(row["total_size_gb"] or 0)
        disk_total = float(row["disk_total_gb"] or 0)
        disk_free = float(row["disk_free_gb"] or 0)
        free_pct = round(disk_free / disk_total * 100, 1) if disk_total > 0 else 0

        lines += [
            f"{'━'*20}",
            f"Тип:     {btype.upper()}",
            f"Путь:    {path}",
            f"Файлов:  {file_count}",
            f"Старый:  {fmt_date(oldest)}",
            f"Новый:   {fmt_date(newest)}",
            f"Размер:  {fmt_size(total_gb)}",
            f"Свободно: {free_pct}%",
            "",
        ]

    text = "\n".join(lines)
    if not has_deletable:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Назад", callback_data="backup_cleanup_servers")
        ]]))
        return

    # Кнопки выбора возраста
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗑 Старше 7 дн",  callback_data=f"backup_preview:{server_name}:7"),
            InlineKeyboardButton("🗑 Старше 14 дн", callback_data=f"backup_preview:{server_name}:14"),
        ],
        [
            InlineKeyboardButton("🗑 Старше 30 дн", callback_data=f"backup_preview:{server_name}:30"),
            InlineKeyboardButton("🗑 Старше 60 дн", callback_data=f"backup_preview:{server_name}:60"),
        ],
        [InlineKeyboardButton("◀️ Назад", callback_data="backup_cleanup_servers")],
    ])
    await query.edit_message_text(text, reply_markup=kb)


async def show_cleanup_preview(query, context, server_name: str, age_days: int):
    """Показать файлы которые будут удалены и запросить подтверждение."""
    files = await asyncio.to_thread(get_files_for_cleanup, server_name, age_days)

    if not files:
        await query.edit_message_text(
            f"✅ {server_name}\n\nНет файлов старше {age_days} дней.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Назад", callback_data=f"backup_cleanup:{server_name}")
            ]])
        )
        return

    total_size = sum(f["size_gb"] for f in files)
    lines = [
        f"🗑 ПРЕДПРОСМОТР УДАЛЕНИЯ\n",
        f"Сервер:    {server_name}",
        f"Старше:    {age_days} дней",
        f"Файлов:    {len(files)}",
        f"Освободится: {fmt_size(total_size)}\n",
        f"{'━'*20}",
        "Будут удалены:\n",
    ]

    for f in files[:30]:  # показываем первые 30
        lines.append(
            f"🗑 {f['file_name']}\n"
            f"   {fmt_size(f['size_gb'])} | {fmt_date(f['modified'])} ({fmt_age(f['modified'])})"
        )

    if len(files) > 30:
        lines.append(f"\n... и ещё {len(files) - 30} файлов")

    # Сохраняем pending
    pending = {
        "server_name": server_name,
        "age_days": age_days,
        "files": [
            {
                "full_path": f["full_path"],
                "file_name": f["file_name"],
                "size_gb":   f["size_gb"],
                "host":      f["host"],
                "username":  f.get("username"),
                "password":  f.get("password"),
            }
            for f in files
        ]
    }
    os.makedirs("/app/data", exist_ok=True)
    with open(CLEANUP_PENDING_FILE, "w") as fp:
        json.dump(pending, fp, ensure_ascii=False, default=str)

    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3750] + "\n\n⚠️ Список обрезан"

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Подтвердить удаление", callback_data="backup_cleanup_confirm"),
            InlineKeyboardButton("❌ Отмена", callback_data=f"backup_cleanup:{server_name}"),
        ]
    ])
    await query.edit_message_text(text, reply_markup=kb)


async def do_cleanup_confirm(query, context):
    """Выполнить удаление после подтверждения."""
    try:
        with open(CLEANUP_PENDING_FILE) as fp:
            pending = json.load(fp)
    except Exception:
        await query.edit_message_text(
            "❌ Список устарел. Запустите Cleanup заново.",
            reply_markup=back_kb()
        )
        return

    server_name = pending["server_name"]
    files = pending["files"]

    # Группируем по хосту (на случай если несколько путей на разных хостах)
    from collections import defaultdict
    by_host = defaultdict(list)
    for f in files:
        by_host[(f["host"], f.get("username"), f.get("password"))].append(f)

    lines = [f"🧹 РЕЗУЛЬТАТ ОЧИСТКИ\n🖥 {server_name}\n"]
    total_deleted = 0
    total_freed = 0.0

    for (host, username, password), host_files in by_host.items():
        paths = [f["full_path"] for f in host_files]
        try:
            results = await asyncio.to_thread(
                delete_backup_files, host, paths, username, password
            )
            for full_path, ok, err in results:
                f_info = next((f for f in host_files if f["full_path"] == full_path), {})
                fname = f_info.get("file_name", os.path.basename(full_path))
                size_gb = f_info.get("size_gb", 0)
                if ok:
                    lines.append(f"✅ {fname} ({fmt_size(size_gb)})")
                    total_deleted += 1
                    total_freed += size_gb
                else:
                    lines.append(f"❌ {fname}: {err[:60]}")
        except Exception as e:
            lines.append(f"❌ Ошибка подключения к {host}: {str(e)[:80]}")

    lines += [
        f"\n{'━'*20}",
        f"Удалено:    {total_deleted} файлов",
        f"Освобождено: {fmt_size(total_freed)}",
    ]

    # Лог удаления
    log_path = "/app/data/cleanup_log.txt"
    try:
        with open(log_path, "a") as log:
            log.write(
                f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                f"{server_name}: удалено {total_deleted} файлов, "
                f"освобождено {fmt_size(total_freed)}\n"
            )
            for f in files:
                log.write(f"  {f['full_path']}\n")
    except Exception:
        pass

    try:
        os.remove(CLEANUP_PENDING_FILE)
    except Exception:
        pass

    text = "\n".join(lines)
    await query.edit_message_text(text, reply_markup=back_kb())


# ─── Главный callback-роутер ─────────────────────────────────

async def backup_callback(query, context: ContextTypes.DEFAULT_TYPE):
    data = query.data

    if data == "backup_menu":
        await query.edit_message_text("💾 БЭКАПЫ\n\nВыбери раздел:", reply_markup=backup_menu_kb())

    elif data == "backup_health":
        await query.edit_message_text("⏳ Получаю данные...")
        await show_backup_health(query, context)

    elif data == "backup_report_servers":
        await show_report_servers(query, context)

    elif data.startswith("backup_report:"):
        server_name = data.split(":", 1)[1]
        await query.edit_message_text("⏳ Формирую отчёт...")
        await show_report_server(query, context, server_name)

    elif data == "backup_dbsize":
        await query.edit_message_text("⏳ Получаю размеры БД...")
        await show_dbsize(query, context)

    elif data == "backup_cleanup_servers":
        await show_cleanup_servers(query, context)

    elif data.startswith("backup_cleanup:"):
        server_name = data.split(":", 1)[1]
        await query.edit_message_text("⏳ Анализирую бэкапы...")
        await show_cleanup_server(query, context, server_name)

    elif data.startswith("backup_preview:"):
        _, server_name, age_str = data.split(":", 2)
        await query.edit_message_text("⏳ Собираю список файлов...")
        await show_cleanup_preview(query, context, server_name, int(age_str))

    elif data == "backup_cleanup_confirm":
        await query.edit_message_text("⏳ Удаляю файлы...")
        await do_cleanup_confirm(query, context)