import os
import json
import asyncio
from datetime import datetime, time
from zoneinfo import ZoneInfo

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

from charts import build_server_chart
from db import get_servers_status, get_server_detail, get_problems, build_report
from ping_tools import load_targets, ping_custom, ping_target
from refresh import refresh_server

ALLOWED_USER_ID = int(os.getenv("TELEGRAM_ALLOWED_USER_ID"))
_group_env = os.getenv("TELEGRAM_GROUP_ID")
GROUP_ID = int(_group_env) if _group_env else None
NOTIFY_ID = GROUP_ID if GROUP_ID else ALLOWED_USER_ID
ALMATY = ZoneInfo("Asia/Almaty")
ALERTS_DISABLED_FILE = "/app/data/alerts_disabled.json"

KEYBOARD = [
    ["🖥 Серверы"],
    ["📡 Пинг"],
    ["📋 Отчёт"],
    ["🚨 Проблемы"]
]


# ─── Авторизация ─────────────────────────────────────────────

def is_allowed(update: Update) -> bool:
    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None
    if user_id == ALLOWED_USER_ID:
        return True
    if GROUP_ID and chat_id == GROUP_ID:
        return True
    return False


# ─── Хендлеры команд ─────────────────────────────────────────

def load_muted() -> dict:
    try:
        with open(ALERTS_DISABLED_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_muted(data: dict):
    os.makedirs(os.path.dirname(ALERTS_DISABLED_FILE), exist_ok=True)
    with open(ALERTS_DISABLED_FILE, "w") as f:
        json.dump(data, f)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "AgentMonitor",
        reply_markup=ReplyKeyboardMarkup(KEYBOARD, resize_keyboard=True)
    )


async def cmd_servers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    text, keyboard = get_servers_status()
    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
    )


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text("⏳ Формирую отчёт...")
    await update.message.reply_text(build_report())


async def cmd_problems(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(get_problems())


def build_ping_keyboard():
    buttons = [
        InlineKeyboardButton(f"📡 {target['name']}", callback_data=f"ping:{target['name']}")
        for target in load_targets()
    ]
    keyboard = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    keyboard.append([InlineKeyboardButton("✏️ Указать IP", callback_data="ping_custom")])
    return keyboard


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    if context.args:
        host = " ".join(context.args).strip()
        text = await asyncio.to_thread(ping_custom, host)
        await update.message.reply_text(text)
        return

    await update.message.reply_text(
        "📡 Выбери сервер или укажи IP/hostname командой:\n/ping 8.8.8.8",
        reply_markup=InlineKeyboardMarkup(build_ping_keyboard())
    )


async def send_server_chart(message, server_name: str):
    try:
        path = await asyncio.to_thread(build_server_chart, server_name)
    except Exception as e:
        await message.reply_text(f"⚠️ Не удалось построить график: {e}")
        return

    try:
        with open(path, "rb") as image:
            await message.reply_photo(
                photo=image,
                caption=f"📈 {server_name} · график за 24 часа"
            )
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def cmd_graph(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /graph SERVER_NAME")
        return

    server_name = " ".join(context.args).strip()
    await update.message.reply_text("📈 Строю график...")
    await send_server_chart(update.message, server_name)


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /mute SERVER_NAME")
        return

    server_name = " ".join(context.args).strip()
    muted = load_muted()
    muted[server_name] = True
    save_muted(muted)
    await update.message.reply_text(f"🔕 Алерты отключены для {server_name}")


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /unmute SERVER_NAME")
        return

    server_name = " ".join(context.args).strip()
    muted = load_muted()
    if server_name in muted:
        del muted[server_name]
        save_muted(muted)
        await update.message.reply_text(f"🔔 Алерты включены для {server_name}")
    else:
        await update.message.reply_text(f"ℹ️ {server_name} не был в mute")


async def cmd_mutes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    muted = sorted(load_muted().keys())
    if not muted:
        await update.message.reply_text("🔔 Нет серверов с отключёнными алертами")
        return

    msg = "🔕 Алерты отключены:\n\n"
    msg += "\n".join(f"• {name}" for name in muted)
    await update.message.reply_text(msg)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    text = update.message.text
    if context.user_data.pop("awaiting_ping_host", False):
        result = await asyncio.to_thread(ping_custom, text)
        await update.message.reply_text(result)
    elif text == "🖥 Серверы":
        await cmd_servers(update, context)
    elif text == "📡 Пинг":
        await cmd_ping(update, context)
    elif text == "📋 Отчёт":
        await cmd_report(update, context)
    elif text == "🚨 Проблемы":
        await cmd_problems(update, context)


# ─── Инлайн кнопки — детали сервера ─────────────────────────

async def safe_edit_message(query, text: str, reply_markup=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        raise


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_allowed(update):
        return

    if query.data.startswith("server:"):
        server_name = query.data.split(":", 1)[1]
        text = get_server_detail(server_name)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("◀️ Назад", callback_data="servers_list"),
                InlineKeyboardButton("🔄 Обновить", callback_data=f"refresh:{server_name}")
            ],
            [
                InlineKeyboardButton("📈 График", callback_data=f"chart:{server_name}")
            ]
        ])
        await safe_edit_message(query, text, reply_markup=keyboard)

    elif query.data.startswith("refresh:"):
        server_name = query.data.split(":", 1)[1]
        ok, error = await asyncio.to_thread(refresh_server, server_name)
        text = get_server_detail(server_name)
        if not ok and error:
            first_line = error.splitlines()[0][:120]
            text += f"\n\n⚠️ Принудительное обновление не удалось:\n{first_line}"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("◀️ Назад", callback_data="servers_list"),
                InlineKeyboardButton("🔄 Обновить", callback_data=f"refresh:{server_name}")
            ],
            [
                InlineKeyboardButton("📈 График", callback_data=f"chart:{server_name}")
            ]
        ])
        await safe_edit_message(query, text, reply_markup=keyboard)

    elif query.data.startswith("chart:"):
        server_name = query.data.split(":", 1)[1]
        await query.message.reply_text("📈 Строю график...")
        await send_server_chart(query.message, server_name)

    elif query.data.startswith("ping:"):
        server_name = query.data.split(":", 1)[1]
        text = await asyncio.to_thread(ping_target, server_name)
        await safe_edit_message(
            query,
            text,
            reply_markup=InlineKeyboardMarkup(build_ping_keyboard())
        )

    elif query.data == "ping_custom":
        context.user_data["awaiting_ping_host"] = True
        await safe_edit_message(
            query,
            "✏️ Отправь IP или hostname следующим сообщением.\nНапример: 10.200.0.10",
            reply_markup=InlineKeyboardMarkup(build_ping_keyboard())
        )

    elif query.data == "servers_list":
        text, keyboard = get_servers_status()
        await safe_edit_message(
            query,
            text,
            reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
        )


# ─── Запланированный отчёт ───────────────────────────────────

async def scheduled_report(context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.send_message(
            chat_id=NOTIFY_ID,
            text=build_report()
        )
    except Exception as e:
        print(f"[bot] Ошибка планового отчёта: {e}", flush=True)


async def weekly_report(context: ContextTypes.DEFAULT_TYPE):
    if datetime.now(ALMATY).weekday() != 6:
        return

    try:
        await context.bot.send_message(
            chat_id=NOTIFY_ID,
            text=build_report("📊 ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ ПО ИНФРАСТРУКТУРЕ")
        )
    except Exception as e:
        print(f"[bot] Ошибка еженедельного отчёта: {e}", flush=True)


# ─── Запуск ──────────────────────────────────────────────────

def main():
    print("[bot] Запуск AgentMonitor Bot...", flush=True)
    print(f"[bot] Уведомления → {'группа ' + str(GROUP_ID) if GROUP_ID else 'личка ' + str(ALLOWED_USER_ID)}", flush=True)

    app = ApplicationBuilder().token(os.getenv("TELEGRAM_TOKEN")).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("servers", cmd_servers))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("problems", cmd_problems))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("graph", cmd_graph))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("mutes", cmd_mutes))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT, text_handler))

    app.job_queue.run_daily(
        scheduled_report,
        time(hour=8, minute=0, tzinfo=ALMATY)
    )
    app.job_queue.run_daily(
        scheduled_report,
        time(hour=18, minute=0, tzinfo=ALMATY)
    )
    app.job_queue.run_daily(
        weekly_report,
        time(hour=9, minute=0, tzinfo=ALMATY)
    )

    app.run_polling()


if __name__ == "__main__":
    main()
