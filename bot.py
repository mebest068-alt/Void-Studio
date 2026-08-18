import os
import asyncio
import time
import sqlite3
from contextlib import closing

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder


# =========================================================
# НАСТРОЙКИ
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не найден! Добавь BOT_TOKEN в Environment Variables на Render."
    )

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "1706479196"))
except ValueError:
    raise RuntimeError("ADMIN_ID должен быть числом.")

DB_PATH = os.getenv("DB_PATH", "support_bot.db")

ANTI_SPAM_LIMIT = 5
ANTI_SPAM_SECONDS = 10
AUTO_MUTE_SECONDS = 60


# =========================================================
# BOT / DISPATCHER
# =========================================================

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


# =========================================================
# SQLITE
# =========================================================

def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db_connect()) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                username TEXT,
                created_at REAL NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS mutes (
                user_id INTEGER PRIMARY KEY,
                mute_until REAL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL DEFAULT 0
            )
        """)

        for key in (
            "tickets_created",
            "tickets_closed",
            "messages_received",
            "messages_sent",
            "auto_mutes",
        ):
            db.execute(
                "INSERT OR IGNORE INTO stats(key, value) VALUES(?, 0)",
                (key,)
            )

        db.commit()


def stat_inc(key: str, amount: int = 1):
    with closing(db_connect()) as db:
        db.execute(
            "UPDATE stats SET value = value + ? WHERE key = ?",
            (amount, key)
        )
        db.commit()


def stat_get(key: str) -> int:
    with closing(db_connect()) as db:
        row = db.execute(
            "SELECT value FROM stats WHERE key = ?",
            (key,)
        ).fetchone()

    return int(row["value"]) if row else 0


# =========================================================
# TICKETS
# =========================================================

def ticket_exists(user_id: int) -> bool:
    with closing(db_connect()) as db:
        row = db.execute(
            "SELECT 1 FROM tickets WHERE user_id = ?",
            (user_id,)
        ).fetchone()

    return row is not None


def create_ticket(user_id: int, full_name: str, username: str | None):
    with closing(db_connect()) as db:
        db.execute(
            """
            INSERT OR REPLACE INTO tickets
            (user_id, full_name, username, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, full_name, username, time.time())
        )
        db.commit()

    stat_inc("tickets_created")


def close_ticket_db(user_id: int):
    with closing(db_connect()) as db:
        db.execute(
            "DELETE FROM tickets WHERE user_id = ?",
            (user_id,)
        )
        db.commit()

    stat_inc("tickets_closed")


def get_ticket(user_id: int):
    with closing(db_connect()) as db:
        return db.execute(
            "SELECT * FROM tickets WHERE user_id = ?",
            (user_id,)
        ).fetchone()


def get_open_tickets():
    with closing(db_connect()) as db:
        return db.execute(
            "SELECT * FROM tickets ORDER BY created_at ASC"
        ).fetchall()


# =========================================================
# MUTE
# =========================================================

def set_mute(user_id: int, duration: int):
    # duration == 0 = permanent
    mute_until = None if duration == 0 else time.time() + duration

    with closing(db_connect()) as db:
        db.execute(
            """
            INSERT OR REPLACE INTO mutes(user_id, mute_until)
            VALUES (?, ?)
            """,
            (user_id, mute_until)
        )
        db.commit()


def remove_mute(user_id: int):
    with closing(db_connect()) as db:
        db.execute(
            "DELETE FROM mutes WHERE user_id = ?",
            (user_id,)
        )
        db.commit()


def is_muted(user_id: int) -> bool:
    with closing(db_connect()) as db:
        row = db.execute(
            "SELECT mute_until FROM mutes WHERE user_id = ?",
            (user_id,)
        ).fetchone()

    if not row:
        return False

    # NULL = permanent mute
    if row["mute_until"] is None:
        return True

    if time.time() >= float(row["mute_until"]):
        remove_mute(user_id)
        return False

    return True


def get_mute_remaining(user_id: int) -> int:
    with closing(db_connect()) as db:
        row = db.execute(
            "SELECT mute_until FROM mutes WHERE user_id = ?",
            (user_id,)
        ).fetchone()

    if not row:
        return 0

    if row["mute_until"] is None:
        return -1

    return max(0, int(float(row["mute_until"]) - time.time()))


# =========================================================
# REPLY MODE
# =========================================================

# admin_id -> user_id
reply_mode: dict[int, int] = {}


# =========================================================
# ANTI-SPAM
# =========================================================

message_times: dict[int, list[float]] = {}


def check_antispam(user_id: int) -> bool:
    now = time.time()

    history = message_times.setdefault(user_id, [])

    history[:] = [
        t for t in history
        if now - t < ANTI_SPAM_SECONDS
    ]

    history.append(now)

    if len(history) > ANTI_SPAM_LIMIT:
        history.clear()
        return True

    return False


# =========================================================
# ФОРМАТИРОВАНИЕ
# =========================================================

def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} сек."

    minutes = seconds // 60

    if minutes < 60:
        return f"{minutes} мин."

    hours = minutes // 60

    if hours < 24:
        return f"{hours} ч."

    days = hours // 24
    return f"{days} д."


def user_label(user_id: int) -> str:
    ticket = get_ticket(user_id)

    if not ticket:
        return f"ID {user_id}"

    username = ticket["username"]

    if username:
        return f'{ticket["full_name"]} (@{username})'

    return ticket["full_name"]


# =========================================================
# КЛАВИАТУРЫ
# =========================================================

def main_menu():
    kb = InlineKeyboardBuilder()

    kb.button(
        text="📩 Создать обращение",
        callback_data="create_ticket"
    )

    kb.button(
        text="📊 Статус обращения",
        callback_data="ticket_status"
    )

    kb.adjust(1)

    return kb.as_markup()


def ticket_buttons(user_id: int):
    kb = InlineKeyboardBuilder()

    kb.button(
        text="💬 Ответить",
        callback_data=f"reply:{user_id}"
    )

    kb.button(
        text="👤 Информация",
        callback_data=f"info:{user_id}"
    )

    kb.button(
        text="🔇 Mute",
        callback_data=f"mute:{user_id}"
    )

    kb.button(
        text="🔊 Unmute",
        callback_data=f"unmute:{user_id}"
    )

    kb.button(
        text="🔒 Закрыть",
        callback_data=f"close:{user_id}"
    )

    kb.adjust(2, 2, 1)

    return kb.as_markup()


def mute_duration_keyboard(user_id: int):
    kb = InlineKeyboardBuilder()

    kb.button(
        text="🔇 5 минут",
        callback_data=f"mutetime:{user_id}:300"
    )

    kb.button(
        text="🔇 30 минут",
        callback_data=f"mutetime:{user_id}:1800"
    )

    kb.button(
        text="🔇 1 час",
        callback_data=f"mutetime:{user_id}:3600"
    )

    kb.button(
        text="🔇 24 часа",
        callback_data=f"mutetime:{user_id}:86400"
    )

    kb.button(
        text="♾️ Навсегда",
        callback_data=f"mutetime:{user_id}:0"
    )

    kb.adjust(2, 2, 1)

    return kb.as_markup()


def cancel_reply_keyboard():
    kb = InlineKeyboardBuilder()

    kb.button(
        text="❌ Отменить ответ",
        callback_data="cancel_reply"
    )

    return kb.as_markup()


def ticket_list_keyboard():
    kb = InlineKeyboardBuilder()

    for ticket in get_open_tickets():
        uid = int(ticket["user_id"])
        name = ticket["full_name"][:28]

        kb.button(
            text=f"🎫 {name}",
            callback_data=f"reply:{uid}"
        )

    kb.adjust(1)
    return kb.as_markup()


# =========================================================
# START
# =========================================================

@dp.message(CommandStart())
async def start(message: Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer(
            "👨‍💼 Панель администратора\n\n"
            "Используй:\n"
            "/tickets — открытые обращения\n"
            "/stats — статистика\n"
            "/cancel — выйти из режима ответа"
        )
        return

    await message.answer(
        "👋 Добро пожаловать в Support!\n\n"
        "Если у тебя возникла проблема, "
        "создай обращение и напиши подробно, "
        "что произошло.",
        reply_markup=main_menu()
    )


# =========================================================
# CREATE TICKET
# =========================================================

@dp.callback_query(F.data == "create_ticket")
async def create_ticket(callback: CallbackQuery):
    user_id = callback.from_user.id

    if is_muted(user_id):
        remaining = get_mute_remaining(user_id)

        if remaining == -1:
            text = (
                "🔇 Ты находишься в постоянном муте.\n\n"
                "Твои сообщения не передаются в поддержку."
            )
        else:
            text = (
                "🔇 Ты находишься в муте.\n\n"
                f"⏱ Осталось: {format_duration(remaining)}"
            )

        await callback.answer(text, show_alert=True)
        return

    if ticket_exists(user_id):
        await callback.answer(
            "У тебя уже есть открытое обращение.",
            show_alert=True
        )
        return

    create_ticket(
        user_id,
        callback.from_user.full_name,
        callback.from_user.username
    )

    await callback.message.answer(
        "📩 Обращение создано!\n\n"
        "Опиши проблему одним или несколькими сообщениями.\n"
        "После этого дождись ответа поддержки."
    )

    username = (
        f"@{callback.from_user.username}"
        if callback.from_user.username
        else "нет username"
    )

    await bot.send_message(
        ADMIN_ID,
        "🎫 НОВОЕ ОБРАЩЕНИЕ\n\n"
        f"👤 {callback.from_user.full_name}\n"
        f"🔗 {username}\n"
        f"🆔 ID: {user_id}\n\n"
        "💬 Ожидаю сообщение от пользователя.",
        reply_markup=ticket_buttons(user_id)
    )

    await callback.answer("✅ Обращение создано!")


# =========================================================
# TICKET STATUS
# =========================================================

@dp.callback_query(F.data == "ticket_status")
async def ticket_status(callback: CallbackQuery):
    user_id = callback.from_user.id

    if ticket_exists(user_id):
        await callback.answer(
            "🎫 У тебя есть открытое обращение.",
            show_alert=True
        )
    else:
        await callback.answer(
            "📭 У тебя нет открытого обращения.",
            show_alert=True
        )


# =========================================================
# ADMIN: TICKETS
# =========================================================

@dp.message(Command("tickets"))
async def admin_tickets(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    tickets_list = get_open_tickets()

    if not tickets_list:
        await message.answer("📭 Открытых обращений нет.")
        return

    text = (
        f"🎫 Открытые обращения: {len(tickets_list)}\n\n"
        "Нажми на пользователя, чтобы включить режим ответа:"
    )

    await message.answer(
        text,
        reply_markup=ticket_list_keyboard()
    )


# =========================================================
# ADMIN: STATS
# =========================================================

@dp.message(Command("stats"))
async def admin_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    open_count = len(get_open_tickets())

    await message.answer(
        "📊 СТАТИСТИКА SUPPORT\n\n"
        f"🎫 Создано тикетов: {stat_get('tickets_created')}\n"
        f"🔒 Закрыто тикетов: {stat_get('tickets_closed')}\n"
        f"📨 Получено сообщений: {stat_get('messages_received')}\n"
        f"📤 Отправлено сообщений: {stat_get('messages_sent')}\n"
        f"🚨 Автомутов: {stat_get('auto_mutes')}\n"
        f"📂 Открыто сейчас: {open_count}"
    )


# =========================================================
# ADMIN: CANCEL
# =========================================================

@dp.message(Command("cancel"))
async def admin_cancel(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    reply_mode.pop(ADMIN_ID, None)

    await message.answer("❌ Режим ответа отключён.")


@dp.callback_query(F.data == "cancel_reply")
async def cancel_reply(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    reply_mode.pop(ADMIN_ID, None)

    await callback.message.answer("❌ Режим ответа отключён.")
    await callback.answer()


# =========================================================
# ADMIN: REPLY
# =========================================================

@dp.callback_query(F.data.startswith("reply:"))
async def reply_ticket(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    user_id = int(callback.data.split(":")[1])

    if not ticket_exists(user_id):
        await callback.answer(
            "❌ Обращение уже закрыто.",
            show_alert=True
        )
        return

    reply_mode[ADMIN_ID] = user_id

    await callback.message.answer(
        "💬 РЕЖИМ ОТВЕТА ВКЛЮЧЁН\n\n"
        f"👤 Пользователь: {user_label(user_id)}\n"
        f"🆔 ID: {user_id}\n\n"
        "Теперь отправь сообщение, фото, видео, файл или другой поддерживаемый тип сообщения.\n"
        "Оно будет передано пользователю.",
        reply_markup=cancel_reply_keyboard()
    )

    await callback.answer()


# =========================================================
# ADMIN: USER INFO
# =========================================================

@dp.callback_query(F.data.startswith("info:"))
async def user_info(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    user_id = int(callback.data.split(":")[1])
    ticket = get_ticket(user_id)

    if not ticket:
        await callback.answer(
            "❌ Тикет не найден.",
            show_alert=True
        )
        return

    username = (
        f"@{ticket['username']}"
        if ticket["username"]
        else "нет"
    )

    created = time.strftime(
        "%d.%m.%Y %H:%M:%S",
        time.localtime(ticket["created_at"])
    )

    mute = "нет"

    if is_muted(user_id):
        remaining = get_mute_remaining(user_id)
        mute = "навсегда" if remaining == -1 else format_duration(remaining)

    await callback.message.answer(
        "👤 ИНФОРМАЦИЯ О ПОЛЬЗОВАТЕЛЕ\n\n"
        f"Имя: {ticket['full_name']}\n"
        f"Username: {username}\n"
        f"ID: {user_id}\n"
        f"🎫 Создан: {created}\n"
        f"🔇 Мут: {mute}"
    )

    await callback.answer()


# =========================================================
# ADMIN: MUTE MENU
# =========================================================

@dp.callback_query(F.data.startswith("mute:"))
async def mute_menu(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    user_id = int(callback.data.split(":")[1])

    if not ticket_exists(user_id):
        await callback.answer(
            "❌ Обращение уже закрыто.",
            show_alert=True
        )
        return

    await callback.message.answer(
        "🔇 ВЫБЕРИ ДЛИТЕЛЬНОСТЬ МУТА\n\n"
        f"👤 Пользователь: {user_label(user_id)}",
        reply_markup=mute_duration_keyboard(user_id)
    )

    await callback.answer()


# =========================================================
# ADMIN: SET MUTE
# =========================================================

@dp.callback_query(F.data.startswith("mutetime:"))
async def set_mute_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    parts = callback.data.split(":")

    user_id = int(parts[1])
    duration = int(parts[2])

    if not ticket_exists(user_id):
        await callback.answer(
            "❌ Обращение уже закрыто.",
            show_alert=True
        )
        return

    set_mute(user_id, duration)

    if duration == 0:
        duration_text = "навсегда"
    else:
        duration_text = format_duration(duration)

    try:
        await bot.send_message(
            user_id,
            "🔇 ОГРАНИЧЕНИЕ\n\n"
            f"⏱ Длительность: {duration_text}\n\n"
            "Пока ограничение действует, "
            "сообщения в поддержку передаваться не будут."
        )
    except Exception:
        pass

    await callback.message.answer(
        "🔇 Пользователь замьючен.\n\n"
        f"👤 ID: {user_id}\n"
        f"⏱ Срок: {duration_text}"
    )

    await callback.answer("🔇 Мут установлен.")


# =========================================================
# ADMIN: UNMUTE
# =========================================================

@dp.callback_query(F.data.startswith("unmute:"))
async def unmute_user(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    user_id = int(callback.data.split(":")[1])

    if not is_muted(user_id):
        await callback.answer(
            "ℹ️ Пользователь не находится в муте.",
            show_alert=True
        )
        return

    remove_mute(user_id)

    try:
        await bot.send_message(
            user_id,
            "🔊 ОГРАНИЧЕНИЕ СНЯТО\n\n"
            "Теперь ты снова можешь отправлять сообщения в поддержку."
        )
    except Exception:
        pass

    await callback.message.answer(
        "🔊 Мут снят.\n\n"
        f"👤 ID: {user_id}"
    )

    await callback.answer("🔊 Мут снят.")


# =========================================================
# ADMIN: CLOSE
# =========================================================

@dp.callback_query(F.data.startswith("close:"))
async def close_ticket_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    user_id = int(callback.data.split(":")[1])

    if not ticket_exists(user_id):
        await callback.answer(
            "❌ Обращение уже закрыто.",
            show_alert=True
        )
        return

    close_ticket_db(user_id)
    remove_mute(user_id)
    reply_mode.pop(ADMIN_ID, None)

    try:
        await bot.send_message(
            user_id,
            "🔒 ОБРАЩЕНИЕ ЗАКРЫТО\n\n"
            "Если у тебя появилась новая проблема, "
            "можешь создать новое обращение.",
            reply_markup=main_menu()
        )
    except Exception:
        pass

    await callback.message.answer(
        f"✅ Обращение пользователя {user_id} закрыто."
    )

    await callback.answer("🔒 Тикет закрыт.")


# =========================================================
# ADMIN MESSAGES
# =========================================================

@dp.message(F.chat.id == ADMIN_ID)
async def admin_message(message: Message):
    admin_id = message.from_user.id

    # Команды обрабатываются отдельными handlers
    if message.text and message.text.startswith("/"):
        return

    if admin_id not in reply_mode:
        await message.answer(
            "📋 Выбери обращение через /tickets "
            "или нажми «💬 Ответить»."
        )
        return

    user_id = reply_mode[admin_id]

    if not ticket_exists(user_id):
        await message.answer("❌ Это обращение уже закрыто.")
        reply_mode.pop(admin_id, None)
        return

    try:
        # Копируем практически любой тип сообщения:
        # текст, фото, видео, документ, стикер и т.д.
        await bot.copy_message(
            chat_id=user_id,
            from_chat_id=ADMIN_ID,
            message_id=message.message_id
        )

        stat_inc("messages_sent")

        await message.answer(
            "✅ Сообщение отправлено пользователю."
        )

    except Exception as e:
        await message.answer(
            "❌ Не удалось отправить сообщение.\n\n"
            f"{e}"
        )


# =========================================================
# USER MESSAGES
# =========================================================

@dp.message()
async def user_message(message: Message):
    user_id = message.from_user.id

    if user_id == ADMIN_ID:
        return

    stat_inc("messages_received")

    # -----------------------------------------------------
    # MUTE
    # -----------------------------------------------------

    if is_muted(user_id):
        remaining = get_mute_remaining(user_id)

        if remaining == -1:
            await message.answer(
                "🔇 Ты находишься в постоянном муте.\n\n"
                "Твои сообщения не передаются в поддержку."
            )
        else:
            await message.answer(
                "🔇 Ты сейчас находишься в муте.\n\n"
                f"⏱ Осталось: {format_duration(remaining)}"
            )

        return

    # -----------------------------------------------------
    # TICKET
    # -----------------------------------------------------

    if not ticket_exists(user_id):
        await message.answer(
            "❗ Сначала создай обращение.",
            reply_markup=main_menu()
        )
        return

    # -----------------------------------------------------
    # ANTISPAM
    # -----------------------------------------------------

    if check_antispam(user_id):
        set_mute(user_id, AUTO_MUTE_SECONDS)
        stat_inc("auto_mutes")

        await message.answer(
            "⚠️ Слишком много сообщений подряд.\n\n"
            f"🔇 Автоматический мут на "
            f"{format_duration(AUTO_MUTE_SECONDS)}.\n\n"
            "Пожалуйста, подожди."
        )

        try:
            await bot.send_message(
                ADMIN_ID,
                "🚨 АВТОМАТИЧЕСКИЙ АНТИСПАМ\n\n"
                f"👤 {message.from_user.full_name}\n"
                f"🆔 ID: {user_id}\n"
                f"🔇 Мут: {format_duration(AUTO_MUTE_SECONDS)}",
                reply_markup=ticket_buttons(user_id)
            )
        except Exception:
            pass

        return

    # -----------------------------------------------------
    # SEND MESSAGE TO ADMIN
    # -----------------------------------------------------

    try:
        username = (
            f"@{message.from_user.username}"
            if message.from_user.username
            else "нет username"
        )

        await bot.send_message(
            ADMIN_ID,
            "📨 НОВОЕ СООБЩЕНИЕ В ТИКЕТЕ\n\n"
            f"👤 {message.from_user.full_name}\n"
            f"🔗 {username}\n"
            f"🆔 ID: {user_id}"
        )

        # Копируем оригинальное сообщение:
        # фото / видео / файл / текст / стикер и т.д.
        await bot.copy_message(
            chat_id=ADMIN_ID,
            from_chat_id=user_id,
            message_id=message.message_id
        )

        await bot.send_message(
            ADMIN_ID,
            "🎫 Управление тикетом:",
            reply_markup=ticket_buttons(user_id)
        )

        await message.answer(
            "📨 Сообщение отправлено в поддержку.\n"
            "⏳ Ожидай ответа."
        )

    except Exception as e:
        await message.answer(
            "❌ Не удалось передать сообщение в поддержку."
        )

        try:
            await bot.send_message(
                ADMIN_ID,
                f"⚠️ Ошибка передачи сообщения пользователя {user_id}:\n{e}"
            )
        except Exception:
            pass


# =========================================================
# HEALTH CHECK ДЛЯ RENDER
# =========================================================

async def health(request):
    return web.Response(
        text="Support bot is running!"
    )


# =========================================================
# MAIN
# =========================================================

async def main():
    init_db()

    app = web.Application()

    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    print("================================")
    print("✅ SUPPORT BOT V2 ЗАПУЩЕН")
    print(f"🌐 Port: {port}")
    print(f"👨‍💼 Admin ID: {ADMIN_ID}")
    print("💾 SQLite: ON")
    print("🛡 Anti-Spam: ON")
    print("🔇 Mute: ON")
    print("🎫 Tickets: ON")
    print("📊 Stats: ON")
    print("📎 Media: ON")
    print("================================")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
