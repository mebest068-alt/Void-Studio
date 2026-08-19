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
# SETTINGS
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден! Добавь BOT_TOKEN в Environment Variables.")

# Можно указать несколько ID через запятую:
# ADMIN_IDS=123456789,987654321
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID", "")
try:
    ADMIN_IDS = {
        int(x.strip())
        for x in ADMIN_IDS_RAW.split(",")
        if x.strip()
    }
except ValueError:
    raise RuntimeError("ADMIN_IDS/ADMIN_ID должен содержать числовые Telegram ID.")

if not ADMIN_IDS:
    raise RuntimeError("Добавь ADMIN_IDS или ADMIN_ID в Environment Variables.")

DB_PATH = os.getenv("DB_PATH", "support_bot_v3.db")

ANTI_SPAM_LIMIT = 5
ANTI_SPAM_SECONDS = 10
AUTO_MUTE_SECONDS = 60

CATEGORIES = {
    "payment": "💳 Оплата",
    "bug": "🐛 Ошибка",
    "account": "🔐 Аккаунт",
    "order": "📦 Заказ",
    "other": "❓ Другое",
}

PRIORITIES = {
    "low": "🟢 Низкий",
    "normal": "🟡 Обычный",
    "high": "🔴 Высокий",
}


# =========================================================
# BOT
# =========================================================

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


# =========================================================
# RUNTIME STATE
# =========================================================

# operator_id -> ticket_id
reply_mode: dict[int, int] = {}

# user_id -> timestamps
message_times: dict[int, list[float]] = {}


# =========================================================
# DATABASE
# =========================================================

def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with closing(db_connect()) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                username TEXT,
                created_at REAL NOT NULL,
                last_seen REAL NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS operators (
                user_id INTEGER PRIMARY KEY,
                added_at REAL NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category TEXT NOT NULL DEFAULT 'other',
                priority TEXT NOT NULL DEFAULT 'normal',
                status TEXT NOT NULL DEFAULT 'open',
                operator_id INTEGER,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                closed_at REAL,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                sender_type TEXT NOT NULL,
                message_id INTEGER,
                message_type TEXT,
                created_at REAL NOT NULL,
                FOREIGN KEY(ticket_id) REFERENCES tickets(ticket_id)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                operator_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY(ticket_id) REFERENCES tickets(ticket_id)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS ratings (
                ticket_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                rating INTEGER NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY(ticket_id) REFERENCES tickets(ticket_id)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS mutes (
                user_id INTEGER PRIMARY KEY,
                mute_until REAL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS blacklist (
                user_id INTEGER PRIMARY KEY,
                reason TEXT,
                created_at REAL NOT NULL
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
            "ratings_received",
        ):
            db.execute(
                "INSERT OR IGNORE INTO stats(key, value) VALUES(?, 0)",
                (key,)
            )

        # Первый администратор автоматически считается оператором.
        now = time.time()
        for admin_id in ADMIN_IDS:
            db.execute(
                "INSERT OR IGNORE INTO operators(user_id, added_at) VALUES(?, ?)",
                (admin_id, now)
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
# USERS
# =========================================================

def upsert_user(user_id: int, full_name: str, username: str | None):
    now = time.time()
    with closing(db_connect()) as db:
        db.execute("""
            INSERT INTO users(user_id, full_name, username, created_at, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                full_name = excluded.full_name,
                username = excluded.username,
                last_seen = excluded.last_seen
        """, (user_id, full_name, username, now, now))
        db.commit()


def get_user(user_id: int):
    with closing(db_connect()) as db:
        return db.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()


# =========================================================
# OPERATORS
# =========================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_operator(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    with closing(db_connect()) as db:
        row = db.execute(
            "SELECT 1 FROM operators WHERE user_id = ?",
            (user_id,)
        ).fetchone()
    return row is not None


def add_operator(user_id: int):
    with closing(db_connect()) as db:
        db.execute(
            "INSERT OR IGNORE INTO operators(user_id, added_at) VALUES(?, ?)",
            (user_id, time.time())
        )
        db.commit()


def remove_operator(user_id: int):
    if is_admin(user_id):
        return
    with closing(db_connect()) as db:
        db.execute(
            "DELETE FROM operators WHERE user_id = ?",
            (user_id,)
        )
        db.commit()


def get_operators():
    with closing(db_connect()) as db:
        return db.execute(
            "SELECT * FROM operators ORDER BY added_at ASC"
        ).fetchall()


# =========================================================
# TICKETS
# =========================================================

def get_open_ticket(user_id: int):
    with closing(db_connect()) as db:
        return db.execute("""
            SELECT t.*, u.full_name, u.username
            FROM tickets t
            JOIN users u ON u.user_id = t.user_id
            WHERE t.user_id = ? AND t.status = 'open'
            ORDER BY t.ticket_id DESC
            LIMIT 1
        """, (user_id,)).fetchone()


def get_ticket(ticket_id: int):
    with closing(db_connect()) as db:
        return db.execute("""
            SELECT t.*, u.full_name, u.username
            FROM tickets t
            JOIN users u ON u.user_id = t.user_id
            WHERE t.ticket_id = ?
        """, (ticket_id,)).fetchone()


def get_open_tickets(limit: int = 50):
    with closing(db_connect()) as db:
        return db.execute("""
            SELECT t.*, u.full_name, u.username
            FROM tickets t
            JOIN users u ON u.user_id = t.user_id
            WHERE t.status = 'open'
            ORDER BY
                CASE t.priority
                    WHEN 'high' THEN 0
                    WHEN 'normal' THEN 1
                    ELSE 2
                END,
                t.created_at ASC
            LIMIT ?
        """, (limit,)).fetchall()


def get_my_tickets(operator_id: int, limit: int = 50):
    with closing(db_connect()) as db:
        return db.execute("""
            SELECT t.*, u.full_name, u.username
            FROM tickets t
            JOIN users u ON u.user_id = t.user_id
            WHERE t.status = 'open' AND t.operator_id = ?
            ORDER BY t.updated_at DESC
            LIMIT ?
        """, (operator_id, limit)).fetchall()


def create_ticket(user_id: int, category: str = "other", priority: str = "normal") -> int:
    now = time.time()
    with closing(db_connect()) as db:
        cur = db.execute("""
            INSERT INTO tickets(
                user_id, category, priority, status,
                operator_id, created_at, updated_at, closed_at
            )
            VALUES (?, ?, ?, 'open', NULL, ?, ?, NULL)
        """, (user_id, category, priority, now, now))
        ticket_id = cur.lastrowid
        db.commit()

    stat_inc("tickets_created")
    return int(ticket_id)


def close_ticket(ticket_id: int):
    now = time.time()
    with closing(db_connect()) as db:
        db.execute("""
            UPDATE tickets
            SET status = 'closed', updated_at = ?, closed_at = ?
            WHERE ticket_id = ? AND status = 'open'
        """, (now, now, ticket_id))
        db.commit()

    stat_inc("tickets_closed")


def assign_ticket(ticket_id: int, operator_id: int):
    with closing(db_connect()) as db:
        db.execute("""
            UPDATE tickets
            SET operator_id = ?, updated_at = ?
            WHERE ticket_id = ? AND status = 'open'
        """, (operator_id, time.time(), ticket_id))
        db.commit()


def update_ticket_category(ticket_id: int, category: str):
    with closing(db_connect()) as db:
        db.execute("""
            UPDATE tickets
            SET category = ?, updated_at = ?
            WHERE ticket_id = ?
        """, (category, time.time(), ticket_id))
        db.commit()


def update_ticket_priority(ticket_id: int, priority: str):
    with closing(db_connect()) as db:
        db.execute("""
            UPDATE tickets
            SET priority = ?, updated_at = ?
            WHERE ticket_id = ?
        """, (priority, time.time(), ticket_id))
        db.commit()


def search_tickets(query: str, limit: int = 20):
    query = query.strip()
    with closing(db_connect()) as db:
        if query.isdigit():
            return db.execute("""
                SELECT t.*, u.full_name, u.username
                FROM tickets t
                JOIN users u ON u.user_id = t.user_id
                WHERE t.ticket_id = ? OR t.user_id = ?
                ORDER BY t.created_at DESC
                LIMIT ?
            """, (int(query), int(query), limit)).fetchall()

        like = f"%{query}%"
        return db.execute("""
            SELECT t.*, u.full_name, u.username
            FROM tickets t
            JOIN users u ON u.user_id = t.user_id
            WHERE u.full_name LIKE ?
               OR COALESCE(u.username, '') LIKE ?
            ORDER BY t.created_at DESC
            LIMIT ?
        """, (like, like, limit)).fetchall()


def count_open_tickets() -> int:
    with closing(db_connect()) as db:
        row = db.execute(
            "SELECT COUNT(*) AS c FROM tickets WHERE status = 'open'"
        ).fetchone()
    return int(row["c"])


# =========================================================
# MESSAGES / NOTES / RATINGS
# =========================================================

def save_message(
    ticket_id: int,
    sender_id: int,
    sender_type: str,
    message_id: int | None,
    message_type: str
):
    with closing(db_connect()) as db:
        db.execute("""
            INSERT INTO messages(
                ticket_id, sender_id, sender_type,
                message_id, message_type, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            ticket_id,
            sender_id,
            sender_type,
            message_id,
            message_type,
            time.time()
        ))
        db.execute(
            "UPDATE tickets SET updated_at = ? WHERE ticket_id = ?",
            (time.time(), ticket_id)
        )
        db.commit()


def save_note(ticket_id: int, operator_id: int, text: str):
    with closing(db_connect()) as db:
        db.execute("""
            INSERT INTO notes(ticket_id, operator_id, text, created_at)
            VALUES (?, ?, ?, ?)
        """, (ticket_id, operator_id, text, time.time()))
        db.commit()


def get_ticket_notes(ticket_id: int):
    with closing(db_connect()) as db:
        return db.execute("""
            SELECT * FROM notes
            WHERE ticket_id = ?
            ORDER BY created_at DESC
            LIMIT 10
        """, (ticket_id,)).fetchall()


def set_rating(ticket_id: int, user_id: int, rating: int):
    with closing(db_connect()) as db:
        db.execute("""
            INSERT OR REPLACE INTO ratings(ticket_id, user_id, rating, created_at)
            VALUES (?, ?, ?, ?)
        """, (ticket_id, user_id, rating, time.time()))
        db.commit()

    stat_inc("ratings_received")


def get_rating(ticket_id: int):
    with closing(db_connect()) as db:
        row = db.execute(
            "SELECT rating FROM ratings WHERE ticket_id = ?",
            (ticket_id,)
        ).fetchone()
    return int(row["rating"]) if row else None


def average_rating():
    with closing(db_connect()) as db:
        row = db.execute(
            "SELECT AVG(rating) AS avg_rating, COUNT(*) AS c FROM ratings"
        ).fetchone()
    return float(row["avg_rating"] or 0), int(row["c"] or 0)


# =========================================================
# MUTE / BLACKLIST
# =========================================================

def set_mute(user_id: int, duration: int):
    # duration == 0 => permanent
    mute_until = None if duration == 0 else time.time() + duration

    with closing(db_connect()) as db:
        db.execute("""
            INSERT OR REPLACE INTO mutes(user_id, mute_until)
            VALUES (?, ?)
        """, (user_id, mute_until))
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


def blacklist_user(user_id: int, reason: str = ""):
    with closing(db_connect()) as db:
        db.execute("""
            INSERT OR REPLACE INTO blacklist(user_id, reason, created_at)
            VALUES (?, ?, ?)
        """, (user_id, reason, time.time()))
        db.commit()


def unblacklist_user(user_id: int):
    with closing(db_connect()) as db:
        db.execute(
            "DELETE FROM blacklist WHERE user_id = ?",
            (user_id,)
        )
        db.commit()


def is_blacklisted(user_id: int) -> bool:
    with closing(db_connect()) as db:
        row = db.execute(
            "SELECT 1 FROM blacklist WHERE user_id = ?",
            (user_id,)
        ).fetchone()
    return row is not None


# =========================================================
# HELPERS
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
    return f"{hours // 24} д."


def format_dt(timestamp: float | None) -> str:
    if not timestamp:
        return "—"
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(timestamp))


def category_name(value: str) -> str:
    return CATEGORIES.get(value, "❓ Другое")


def priority_name(value: str) -> str:
    return PRIORITIES.get(value, "🟡 Обычный")


def ticket_label(ticket) -> str:
    username = f"@{ticket['username']}" if ticket["username"] else "нет username"
    return (
        f"🎫 Тикет #{ticket['ticket_id']}\n"
        f"👤 {ticket['full_name']} ({username})\n"
        f"🆔 ID: {ticket['user_id']}\n"
        f"📂 {category_name(ticket['category'])}\n"
        f"🚦 {priority_name(ticket['priority'])}"
    )


def message_type(message: Message) -> str:
    if message.text:
        return "text"
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.document:
        return "document"
    if message.audio:
        return "audio"
    if message.voice:
        return "voice"
    if message.video_note:
        return "video_note"
    if message.sticker:
        return "sticker"
    if message.animation:
        return "animation"
    if message.contact:
        return "contact"
    if message.location:
        return "location"
    return "other"


# =========================================================
# KEYBOARDS
# =========================================================

def user_main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="📩 Создать обращение", callback_data="create_ticket")
    kb.button(text="🎫 Мой тикет", callback_data="my_ticket")
    kb.adjust(1)
    return kb.as_markup()


def category_keyboard():
    kb = InlineKeyboardBuilder()
    for key, name in CATEGORIES.items():
        kb.button(text=name, callback_data=f"category:{key}")
    kb.adjust(1)
    return kb.as_markup()


def priority_keyboard(prefix: str = "priority"):
    kb = InlineKeyboardBuilder()
    for key, name in PRIORITIES.items():
        kb.button(text=name, callback_data=f"{prefix}:{key}")
    kb.adjust(1)
    return kb.as_markup()


def ticket_buttons(ticket_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Ответить", callback_data=f"reply:{ticket_id}")
    kb.button(text="👨‍💼 Взять", callback_data=f"take:{ticket_id}")
    kb.button(text="📂 Категория", callback_data=f"catmenu:{ticket_id}")
    kb.button(text="🚦 Приоритет", callback_data=f"priomenu:{ticket_id}")
    kb.button(text="👤 Информация", callback_data=f"info:{ticket_id}")
    kb.button(text="📝 Заметка", callback_data=f"note:{ticket_id}")
    kb.button(text="🔇 Mute", callback_data=f"mute:{ticket_id}")
    kb.button(text="🔊 Unmute", callback_data=f"unmute:{ticket_id}")
    kb.button(text="🔒 Закрыть", callback_data=f"close:{ticket_id}")
    kb.adjust(2, 2, 2, 2, 1)
    return kb.as_markup()


def operator_panel():
    kb = InlineKeyboardBuilder()
    kb.button(text="📥 Новые обращения", callback_data="admin_open")
    kb.button(text="👨‍💼 Мои тикеты", callback_data="admin_my")
    kb.button(text="🔴 Срочные", callback_data="admin_high")
    kb.button(text="📊 Статистика", callback_data="admin_stats")
    kb.button(text="👥 Операторы", callback_data="admin_operators")
    kb.button(text="📢 Рассылка", callback_data="admin_broadcast")
    kb.adjust(1)
    return kb.as_markup()


def mute_duration_keyboard(ticket_id: int):
    kb = InlineKeyboardBuilder()
    for text, seconds in (
        ("🔇 5 минут", 300),
        ("🔇 30 минут", 1800),
        ("🔇 1 час", 3600),
        ("🔇 24 часа", 86400),
        ("♾️ Навсегда", 0),
    ):
        kb.button(
            text=text,
            callback_data=f"mutetime:{ticket_id}:{seconds}"
        )
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def rating_keyboard(ticket_id: int):
    kb = InlineKeyboardBuilder()
    for rating in range(1, 6):
        kb.button(
            text="⭐" * rating,
            callback_data=f"rate:{ticket_id}:{rating}"
        )
    kb.adjust(1)
    return kb.as_markup()


def category_ticket_keyboard(ticket_id: int):
    kb = InlineKeyboardBuilder()
    for key, name in CATEGORIES.items():
        kb.button(
            text=name,
            callback_data=f"setcat:{ticket_id}:{key}"
        )
    kb.adjust(1)
    return kb.as_markup()


def priority_ticket_keyboard(ticket_id: int):
    kb = InlineKeyboardBuilder()
    for key, name in PRIORITIES.items():
        kb.button(
            text=name,
            callback_data=f"setprio:{ticket_id}:{key}"
        )
    kb.adjust(1)
    return kb.as_markup()


# =========================================================
# START
# =========================================================

@dp.message(CommandStart())
async def start(message: Message):
    user_id = message.from_user.id

    upsert_user(
        user_id,
        message.from_user.full_name,
        message.from_user.username
    )

    if is_operator(user_id):
        await message.answer(
            "👨‍💼 <b>Support Bot V3</b>\n\n"
            "Панель оператора открыта.",
            reply_markup=operator_panel()
        )
        return

    if is_blacklisted(user_id):
        await message.answer(
            "🚫 Доступ к поддержке ограничен."
        )
        return

    await message.answer(
        "👋 <b>Добро пожаловать в Support!</b>\n\n"
        "Если у тебя возникла проблема, создай обращение "
        "и подробно опиши ситуацию.",
        reply_markup=user_main_menu()
    )


# =========================================================
# USER: CREATE TICKET
# =========================================================

@dp.callback_query(F.data == "create_ticket")
async def create_ticket_start(callback: CallbackQuery):
    user_id = callback.from_user.id

    if is_blacklisted(user_id):
        await callback.answer("🚫 Доступ к поддержке ограничен.", show_alert=True)
        return

    if is_muted(user_id):
        remaining = get_mute_remaining(user_id)
        text = (
            "🔇 Постоянный мут."
            if remaining == -1
            else f"🔇 Ты находишься в муте.\n⏱ Осталось: {format_duration(remaining)}"
        )
        await callback.answer(text, show_alert=True)
        return

    if get_open_ticket(user_id):
        await callback.answer(
            "🎫 У тебя уже есть открытый тикет.",
            show_alert=True
        )
        return

    await callback.message.answer(
        "📂 <b>Выбери категорию обращения:</b>",
        reply_markup=category_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("category:"))
async def create_ticket_category(callback: CallbackQuery):
    user_id = callback.from_user.id
    category = callback.data.split(":", 1)[1]

    if category not in CATEGORIES:
        await callback.answer("❌ Неверная категория.", show_alert=True)
        return

    if get_open_ticket(user_id):
        await callback.answer("У тебя уже есть открытый тикет.", show_alert=True)
        return

    # Временно сохраняем выбранную категорию в callback message context:
    # проще сразу создаём тикет с обычным приоритетом.
    ticket_id = create_ticket(user_id, category, "normal")

    ticket = get_ticket(ticket_id)

    await callback.message.answer(
        f"📩 <b>Обращение #{ticket_id} создано!</b>\n\n"
        f"📂 Категория: {category_name(category)}\n"
        f"🚦 Приоритет: {priority_name('normal')}\n\n"
        "Опиши проблему одним или несколькими сообщениями.\n"
        "После этого дождись ответа поддержки."
    )

    username = (
        f"@{callback.from_user.username}"
        if callback.from_user.username
        else "нет username"
    )

    admin_text = (
        "🎫 <b>НОВОЕ ОБРАЩЕНИЕ</b>\n\n"
        f"#{ticket_id}\n"
        f"👤 {callback.from_user.full_name}\n"
        f"🔗 {username}\n"
        f"🆔 ID: {user_id}\n"
        f"📂 {category_name(category)}\n"
        f"🚦 {priority_name('normal')}\n\n"
        "⏳ Ожидаю сообщение от пользователя."
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                admin_text,
                reply_markup=ticket_buttons(ticket_id)
            )
        except Exception:
            pass

    await callback.answer("✅ Тикет создан!")


# =========================================================
# USER: MY TICKET
# =========================================================

@dp.callback_query(F.data == "my_ticket")
async def my_ticket(callback: CallbackQuery):
    ticket = get_open_ticket(callback.from_user.id)

    if not ticket:
        await callback.answer(
            "📭 У тебя нет открытого обращения.",
            show_alert=True
        )
        return

    operator = (
        f"ID {ticket['operator_id']}"
        if ticket["operator_id"]
        else "Не назначен"
    )

    await callback.message.answer(
        f"🎫 <b>Тикет #{ticket['ticket_id']}</b>\n\n"
        f"📂 {category_name(ticket['category'])}\n"
        f"🚦 {priority_name(ticket['priority'])}\n"
        f"👨‍💼 Оператор: {operator}\n"
        f"🕐 Создан: {format_dt(ticket['created_at'])}\n\n"
        "Ты можешь продолжать отправлять сообщения в этот тикет."
    )
    await callback.answer()


# =========================================================
# ADMIN: PANEL
# =========================================================

@dp.message(Command("admin"))
async def admin_command(message: Message):
    if not is_operator(message.from_user.id):
        return

    await message.answer(
        "👨‍💼 <b>Панель Support Bot V3</b>",
        reply_markup=operator_panel()
    )


@dp.message(Command("tickets"))
async def admin_tickets(message: Message):
    if not is_operator(message.from_user.id):
        return

    tickets = get_open_tickets()

    if not tickets:
        await message.answer("📭 Открытых обращений нет.")
        return

    await message.answer(
        f"📥 <b>Открытые обращения: {len(tickets)}</b>",
        reply_markup=ticket_list_keyboard(tickets)
    )


def ticket_list_keyboard(tickets):
    kb = InlineKeyboardBuilder()

    for ticket in tickets:
        username = (
            f" @{ticket['username']}"
            if ticket["username"]
            else ""
        )
        text = (
            f"#{ticket['ticket_id']} "
            f"{priority_name(ticket['priority'])[:2]} "
            f"{ticket['full_name'][:25]}{username[:15]}"
        )

        kb.button(
            text=text,
            callback_data=f"showticket:{ticket['ticket_id']}"
        )

    kb.adjust(1)
    return kb.as_markup()


# =========================================================
# ADMIN: OPEN / MY / HIGH
# =========================================================

@dp.callback_query(F.data == "admin_open")
async def admin_open(callback: CallbackQuery):
    if not is_operator(callback.from_user.id):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    tickets = get_open_tickets()

    if not tickets:
        await callback.message.answer("📭 Открытых обращений нет.")
    else:
        await callback.message.answer(
            f"📥 <b>Открытые обращения: {len(tickets)}</b>",
            reply_markup=ticket_list_keyboard(tickets)
        )

    await callback.answer()


@dp.callback_query(F.data == "admin_my")
async def admin_my(callback: CallbackQuery):
    if not is_operator(callback.from_user.id):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    tickets = get_my_tickets(callback.from_user.id)

    if not tickets:
        await callback.message.answer("📭 На тебе нет открытых тикетов.")
    else:
        await callback.message.answer(
            f"👨‍💼 <b>Мои тикеты: {len(tickets)}</b>",
            reply_markup=ticket_list_keyboard(tickets)
        )

    await callback.answer()


@dp.callback_query(F.data == "admin_high")
async def admin_high(callback: CallbackQuery):
    if not is_operator(callback.from_user.id):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    with closing(db_connect()) as db:
        tickets = db.execute("""
            SELECT t.*, u.full_name, u.username
            FROM tickets t
            JOIN users u ON u.user_id = t.user_id
            WHERE t.status = 'open' AND t.priority = 'high'
            ORDER BY t.created_at ASC
            LIMIT 50
        """).fetchall()

    if not tickets:
        await callback.message.answer("🟢 Срочных тикетов нет.")
    else:
        await callback.message.answer(
            f"🔴 <b>Срочные обращения: {len(tickets)}</b>",
            reply_markup=ticket_list_keyboard(tickets)
        )

    await callback.answer()


# =========================================================
# ADMIN: SHOW TICKET
# =========================================================

@dp.callback_query(F.data.startswith("showticket:"))
async def show_ticket(callback: CallbackQuery):
    if not is_operator(callback.from_user.id):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    ticket_id = int(callback.data.split(":")[1])
    ticket = get_ticket(ticket_id)

    if not ticket:
        await callback.answer("❌ Тикет не найден.", show_alert=True)
        return

    operator = (
        f"ID {ticket['operator_id']}"
        if ticket["operator_id"]
        else "Не назначен"
    )

    text = (
        f"🎫 <b>Тикет #{ticket_id}</b>\n\n"
        f"👤 {ticket['full_name']}\n"
        f"🔗 @{ticket['username'] if ticket['username'] else 'нет username'}\n"
        f"🆔 ID: {ticket['user_id']}\n\n"
        f"📂 Категория: {category_name(ticket['category'])}\n"
        f"🚦 Приоритет: {priority_name(ticket['priority'])}\n"
        f"👨‍💼 Оператор: {operator}\n"
        f"🕐 Создан: {format_dt(ticket['created_at'])}\n"
        f"🔄 Обновлён: {format_dt(ticket['updated_at'])}"
    )

    await callback.message.answer(
        text,
        reply_markup=ticket_buttons(ticket_id)
    )
    await callback.answer()


# =========================================================
# ADMIN: TAKE TICKET
# =========================================================

@dp.callback_query(F.data.startswith("take:"))
async def take_ticket(callback: CallbackQuery):
    if not is_operator(callback.from_user.id):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    ticket_id = int(callback.data.split(":")[1])
    ticket = get_ticket(ticket_id)

    if not ticket or ticket["status"] != "open":
        await callback.answer("❌ Тикет закрыт.", show_alert=True)
        return

    assign_ticket(ticket_id, callback.from_user.id)

    try:
        await bot.send_message(
            ticket["user_id"],
            f"👨‍💼 <b>Тикет #{ticket_id}</b>\n\n"
            "Вашим обращением занимается оператор.\n"
            "Ожидайте ответа."
        )
    except Exception:
        pass

    await callback.message.answer(
        f"✅ Тикет #{ticket_id} назначен на оператора "
        f"<code>{callback.from_user.id}</code>."
    )
    await callback.answer("✅ Тикет взят.")


# =========================================================
# ADMIN: REPLY
# =========================================================

@dp.callback_query(F.data.startswith("reply:"))
async def reply_ticket(callback: CallbackQuery):
    if not is_operator(callback.from_user.id):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    ticket_id = int(callback.data.split(":")[1])
    ticket = get_ticket(ticket_id)

    if not ticket or ticket["status"] != "open":
        await callback.answer("❌ Тикет закрыт.", show_alert=True)
        return

    assign_ticket(ticket_id, callback.from_user.id)
    reply_mode[callback.from_user.id] = ticket_id

    await callback.message.answer(
        "💬 <b>РЕЖИМ ОТВЕТА ВКЛЮЧЁН</b>\n\n"
        f"🎫 Тикет: #{ticket_id}\n"
        f"👤 Пользователь: {ticket['full_name']}\n\n"
        "Теперь отправь текст, фото, видео, документ или другое "
        "поддерживаемое сообщение.\n\n"
        "Для выхода: /cancel"
    )
    await callback.answer()


@dp.message(Command("cancel"))
async def admin_cancel(message: Message):
    if not is_operator(message.from_user.id):
        return

    reply_mode.pop(message.from_user.id, None)
    await message.answer("❌ Режим ответа отключён.")


# =========================================================
# ADMIN: USER INFO
# =========================================================

@dp.callback_query(F.data.startswith("info:"))
async def user_info(callback: CallbackQuery):
    if not is_operator(callback.from_user.id):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    ticket_id = int(callback.data.split(":")[1])
    ticket = get_ticket(ticket_id)

    if not ticket:
        await callback.answer("❌ Тикет не найден.", show_alert=True)
        return

    mute = "нет"
    if is_muted(ticket["user_id"]):
        remaining = get_mute_remaining(ticket["user_id"])
        mute = "навсегда" if remaining == -1 else format_duration(remaining)

    blacklist = "🚫 Да" if is_blacklisted(ticket["user_id"]) else "Нет"

    await callback.message.answer(
        "👤 <b>ИНФОРМАЦИЯ О ПОЛЬЗОВАТЕЛЕ</b>\n\n"
        f"Имя: {ticket['full_name']}\n"
        f"Username: @{ticket['username'] if ticket['username'] else 'нет'}\n"
        f"ID: {ticket['user_id']}\n\n"
        f"🎫 Тикет: #{ticket_id}\n"
        f"🕐 Создан: {format_dt(ticket['created_at'])}\n"
        f"🔇 Mute: {mute}\n"
        f"🚫 Blacklist: {blacklist}"
    )
    await callback.answer()


# =========================================================
# ADMIN: CATEGORY / PRIORITY
# =========================================================

@dp.callback_query(F.data.startswith("catmenu:"))
async def category_menu(callback: CallbackQuery):
    if not is_operator(callback.from_user.id):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    ticket_id = int(callback.data.split(":")[1])

    await callback.message.answer(
        f"📂 Выбери категорию для тикета #{ticket_id}:",
        reply_markup=category_ticket_keyboard(ticket_id)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("setcat:"))
async def set_category(callback: CallbackQuery):
    if not is_operator(callback.from_user.id):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    _, ticket_id, category = callback.data.split(":")

    if category not in CATEGORIES:
        await callback.answer("❌ Неверная категория.", show_alert=True)
        return

    ticket_id = int(ticket_id)
    ticket = get_ticket(ticket_id)

    if not ticket:
        await callback.answer("❌ Тикет не найден.", show_alert=True)
        return

    update_ticket_category(ticket_id, category)

    await callback.message.answer(
        f"✅ Категория тикета #{ticket_id} изменена:\n"
        f"{category_name(category)}"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("priomenu:"))
async def priority_menu(callback: CallbackQuery):
    if not is_operator(callback.from_user.id):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    ticket_id = int(callback.data.split(":")[1])

    await callback.message.answer(
        f"🚦 Выбери приоритет для тикета #{ticket_id}:",
        reply_markup=priority_ticket_keyboard(ticket_id)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("setprio:"))
async def set_priority(callback: CallbackQuery):
    if not is_operator(callback.from_user.id):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    _, ticket_id, priority = callback.data.split(":")

    if priority not in PRIORITIES:
        await callback.answer("❌ Неверный приоритет.", show_alert=True)
        return

    ticket_id = int(ticket_id)
    ticket = get_ticket(ticket_id)

    if not ticket:
        await callback.answer("❌ Тикет не найден.", show_alert=True)
        return

    update_ticket_priority(ticket_id, priority)

    await callback.message.answer(
        f"✅ Приоритет тикета #{ticket_id} изменён:\n"
        f"{priority_name(priority)}"
    )
    await callback.answer()


# =========================================================
# ADMIN: NOTES
# =========================================================

@dp.callback_query(F.data.startswith("note:"))
async def note_start(callback: CallbackQuery):
    if not is_operator(callback.from_user.id):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    ticket_id = int(callback.data.split(":")[1])
    ticket = get_ticket(ticket_id)

    if not ticket:
        await callback.answer("❌ Тикет не найден.", show_alert=True)
        return

    # Используем отрицательный ticket_id как специальный runtime mode.
    reply_mode[callback.from_user.id] = -ticket_id

    await callback.message.answer(
        f"📝 <b>Внутренняя заметка для тикета #{ticket_id}</b>\n\n"
        "Отправь текст заметки. Пользователь её не увидит.\n"
        "Для отмены: /cancel"
    )
    await callback.answer()


# =========================================================
# ADMIN: MUTE
# =========================================================

@dp.callback_query(F.data.startswith("mute:"))
async def mute_menu(callback: CallbackQuery):
    if not is_operator(callback.from_user.id):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    ticket_id = int(callback.data.split(":")[1])
    ticket = get_ticket(ticket_id)

    if not ticket:
        await callback.answer("❌ Тикет не найден.", show_alert=True)
        return

    await callback.message.answer(
        f"🔇 Выбери длительность мута для "
        f"пользователя из тикета #{ticket_id}:",
        reply_markup=mute_duration_keyboard(ticket_id)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("mutetime:"))
async def set_mute_callback(callback: CallbackQuery):
    if not is_operator(callback.from_user.id):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    _, ticket_id, duration = callback.data.split(":")
    ticket_id = int(ticket_id)
    duration = int(duration)

    ticket = get_ticket(ticket_id)

    if not ticket:
        await callback.answer("❌ Тикет не найден.", show_alert=True)
        return

    user_id = ticket["user_id"]
    set_mute(user_id, duration)

    duration_text = (
        "навсегда"
        if duration == 0
        else format_duration(duration)
    )

    try:
        await bot.send_message(
            user_id,
            "🔇 <b>ОГРАНИЧЕНИЕ</b>\n\n"
            f"⏱ Длительность: {duration_text}\n\n"
            "Пока ограничение действует, сообщения в поддержку "
            "передаваться не будут."
        )
    except Exception:
        pass

    await callback.message.answer(
        f"🔇 Пользователь замьючен.\n\n"
        f"🎫 Тикет: #{ticket_id}\n"
        f"⏱ Срок: {duration_text}"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("unmute:"))
async def unmute_user(callback: CallbackQuery):
    if not is_operator(callback.from_user.id):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    ticket_id = int(callback.data.split(":")[1])
    ticket = get_ticket(ticket_id)

    if not ticket:
        await callback.answer("❌ Тикет не найден.", show_alert=True)
        return

    user_id = ticket["user_id"]

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
            "🔊 <b>ОГРАНИЧЕНИЕ СНЯТО</b>\n\n"
            "Теперь ты снова можешь отправлять сообщения в поддержку."
        )
    except Exception:
        pass

    await callback.message.answer(
        f"🔊 Мут снят.\n👤 ID: {user_id}"
    )
    await callback.answer("🔊 Мут снят.")


# =========================================================
# ADMIN: CLOSE
# =========================================================

@dp.callback_query(F.data.startswith("close:"))
async def close_ticket_callback(callback: CallbackQuery):
    if not is_operator(callback.from_user.id):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    ticket_id = int(callback.data.split(":")[1])
    ticket = get_ticket(ticket_id)

    if not ticket or ticket["status"] != "open":
        await callback.answer(
            "❌ Обращение уже закрыто.",
            show_alert=True
        )
        return

    close_ticket(ticket_id)
    remove_mute(ticket["user_id"])

    # Убираем режим ответа у оператора, если он отвечал в этот тикет.
    for operator_id, active_ticket_id in list(reply_mode.items()):
        if active_ticket_id == ticket_id:
            reply_mode.pop(operator_id, None)

    try:
        await bot.send_message(
            ticket["user_id"],
            f"🔒 <b>ОБРАЩЕНИЕ #{ticket_id} ЗАКРЫТО</b>\n\n"
            "Спасибо за обращение!\n\n"
            "Пожалуйста, оцени работу поддержки:",
            reply_markup=rating_keyboard(ticket_id)
        )
    except Exception:
        pass

    await callback.message.answer(
        f"✅ Тикет #{ticket_id} закрыт."
    )
    await callback.answer("🔒 Тикет закрыт.")


# =========================================================
# USER: RATING
# =========================================================

@dp.callback_query(F.data.startswith("rate:"))
async def rate_ticket(callback: CallbackQuery):
    _, ticket_id, rating = callback.data.split(":")
    ticket_id = int(ticket_id)
    rating = int(rating)

    ticket = get_ticket(ticket_id)

    if not ticket:
        await callback.answer("❌ Тикет не найден.", show_alert=True)
        return

    if ticket["user_id"] != callback.from_user.id:
        await callback.answer("❌ Это не твой тикет.", show_alert=True)
        return

    if not 1 <= rating <= 5:
        await callback.answer("❌ Неверная оценка.", show_alert=True)
        return

    if get_rating(ticket_id) is not None:
        await callback.answer(
            "⭐ Оценка уже оставлена.",
            show_alert=True
        )
        return

    set_rating(ticket_id, callback.from_user.id, rating)

    await callback.message.answer(
        f"❤️ Спасибо! Ты поставил оценку: {'⭐' * rating}"
    )
    await callback.answer("Спасибо за оценку!")


# =========================================================
# ADMIN: STATS
# =========================================================

@dp.message(Command("stats"))
async def admin_stats_command(message: Message):
    if not is_operator(message.from_user.id):
        return

    avg, ratings_count = average_rating()

    await message.answer(
        "📊 <b>СТАТИСТИКА SUPPORT BOT V3</b>\n\n"
        f"🎫 Создано тикетов: {stat_get('tickets_created')}\n"
        f"🔒 Закрыто тикетов: {stat_get('tickets_closed')}\n"
        f"📂 Открыто сейчас: {count_open_tickets()}\n\n"
        f"📨 Получено сообщений: {stat_get('messages_received')}\n"
        f"📤 Отправлено сообщений: {stat_get('messages_sent')}\n"
        f"🚨 Автомутов: {stat_get('auto_mutes')}\n\n"
        f"⭐ Средняя оценка: {avg:.2f}/5\n"
        f"⭐ Оценок получено: {ratings_count}"
    )


@dp.callback_query(F.data == "admin_stats")
async def admin_stats_callback(callback: CallbackQuery):
    if not is_operator(callback.from_user.id):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    avg, ratings_count = average_rating()

    await callback.message.answer(
        "📊 <b>СТАТИСТИКА</b>\n\n"
        f"🎫 Создано: {stat_get('tickets_created')}\n"
        f"🔒 Закрыто: {stat_get('tickets_closed')}\n"
        f"📂 Открыто: {count_open_tickets()}\n\n"
        f"📨 Получено сообщений: {stat_get('messages_received')}\n"
        f"📤 Отправлено сообщений: {stat_get('messages_sent')}\n"
        f"🚨 Автомутов: {stat_get('auto_mutes')}\n\n"
        f"⭐ Средняя оценка: {avg:.2f}/5\n"
        f"⭐ Оценок: {ratings_count}"
    )
    await callback.answer()


# =========================================================
# ADMIN: OPERATORS
# =========================================================

@dp.callback_query(F.data == "admin_operators")
async def admin_operators(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "❌ Только главный администратор.",
            show_alert=True
        )
        return

    operators = get_operators()

    lines = ["👥 <b>ОПЕРАТОРЫ</b>\n"]

    for op in operators:
        role = "👑 Администратор" if is_admin(op["user_id"]) else "👨‍💼 Оператор"
        lines.append(f"{role}: <code>{op['user_id']}</code>")

    lines.append(
        "\nЧтобы добавить оператора:\n"
        "<code>/addoperator ID</code>\n\n"
        "Удалить:\n"
        "<code>/deloperator ID</code>"
    )

    await callback.message.answer("\n".join(lines))
    await callback.answer()


@dp.message(Command("addoperator"))
async def add_operator_command(message: Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /addoperator 123456789")
        return

    operator_id = int(parts[1])
    add_operator(operator_id)

    await message.answer(
        f"✅ Пользователь <code>{operator_id}</code> добавлен как оператор."
    )


@dp.message(Command("deloperator"))
async def del_operator_command(message: Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /deloperator 123456789")
        return

    operator_id = int(parts[1])

    if is_admin(operator_id):
        await message.answer("❌ Главного администратора удалить нельзя.")
        return

    remove_operator(operator_id)

    await message.answer(
        f"✅ Оператор <code>{operator_id}</code> удалён."
    )


# =========================================================
# ADMIN: SEARCH
# =========================================================

@dp.message(Command("search"))
async def search_command(message: Message):
    if not is_operator(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) != 2:
        await message.answer(
            "🔎 Использование:\n"
            "<code>/search username</code>\n"
            "<code>/search имя</code>\n"
            "<code>/search ticket_id</code>"
        )
        return

    results = search_tickets(parts[1])

    if not results:
        await message.answer("🔎 Ничего не найдено.")
        return

    await message.answer(
        f"🔎 Найдено: {len(results)}",
        reply_markup=ticket_list_keyboard(results)
    )


# =========================================================
# ADMIN: BROADCAST
# =========================================================

broadcast_mode: set[int] = set()


@dp.callback_query(F.data == "admin_broadcast")
async def broadcast_start(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "❌ Только главный администратор.",
            show_alert=True
        )
        return

    broadcast_mode.add(callback.from_user.id)

    await callback.message.answer(
        "📢 <b>РЕЖИМ РАССЫЛКИ</b>\n\n"
        "Отправь сообщение, которое нужно разослать пользователям.\n"
        "Можно отправить текст, фото, видео, документ и т.д.\n\n"
        "Для отмены: /cancel"
    )
    await callback.answer()


# =========================================================
# ADMIN MESSAGES
# =========================================================

@dp.message(F.chat.id.in_(ADMIN_IDS))
async def admin_message(message: Message):
    admin_id = message.from_user.id

    # Команды обрабатываются отдельными handlers.
    if message.text and message.text.startswith("/"):
        return

    # Broadcast.
    if admin_id in broadcast_mode:
        broadcast_mode.discard(admin_id)

        with closing(db_connect()) as db:
            users = db.execute("SELECT user_id FROM users").fetchall()

        sent = 0
        failed = 0

        for row in users:
            user_id = int(row["user_id"])

            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=admin_id,
                    message_id=message.message_id
                )
                sent += 1
                await asyncio.sleep(0.04)
            except Exception:
                failed += 1

        await message.answer(
            "📢 <b>Рассылка завершена</b>\n\n"
            f"✅ Отправлено: {sent}\n"
            f"❌ Ошибок: {failed}"
        )
        return

    if not is_operator(admin_id):
        return

    # Internal note mode.
    if admin_id in reply_mode and reply_mode[admin_id] < 0:
        ticket_id = abs(reply_mode[admin_id])
        ticket = get_ticket(ticket_id)

        if not ticket:
            reply_mode.pop(admin_id, None)
            await message.answer("❌ Тикет не найден.")
            return

        if not message.text:
            await message.answer("❌ Заметка должна быть текстом.")
            return

        save_note(ticket_id, admin_id, message.text)
        reply_mode.pop(admin_id, None)

        await message.answer(
            f"📝 Заметка для тикета #{ticket_id} сохранена."
        )
        return

    # Reply mode.
    if admin_id not in reply_mode:
        await message.answer(
            "📋 Выбери обращение через /tickets "
            "или нажми «💬 Ответить»."
        )
        return

    ticket_id = reply_mode[admin_id]
    ticket = get_ticket(ticket_id)

    if not ticket or ticket["status"] != "open":
        await message.answer("❌ Это обращение уже закрыто.")
        reply_mode.pop(admin_id, None)
        return

    try:
        await bot.copy_message(
            chat_id=ticket["user_id"],
            from_chat_id=admin_id,
            message_id=message.message_id
        )

        save_message(
            ticket_id,
            admin_id,
            "operator",
            message.message_id,
            message_type(message)
        )

        stat_inc("messages_sent")

        await message.answer(
            f"✅ Сообщение отправлено в тикет #{ticket_id}."
        )

    except Exception as e:
        await message.answer(
            "❌ Не удалось отправить сообщение.\n\n"
            f"<code>{e}</code>"
        )


# =========================================================
# USER MESSAGES
# =========================================================

@dp.message()
async def user_message(message: Message):
    user_id = message.from_user.id

    if is_operator(user_id):
        return

    upsert_user(
        user_id,
        message.from_user.full_name,
        message.from_user.username
    )

    stat_inc("messages_received")

    if is_blacklisted(user_id):
        await message.answer("🚫 Доступ к поддержке ограничен.")
        return

    # Mute.
    if is_muted(user_id):
        remaining = get_mute_remaining(user_id)

        if remaining == -1:
            await message.answer(
                "🔇 <b>Ты находишься в постоянном муте.</b>\n\n"
                "Твои сообщения не передаются в поддержку."
            )
        else:
            await message.answer(
                "🔇 <b>Ты сейчас находишься в муте.</b>\n\n"
                f"⏱ Осталось: {format_duration(remaining)}"
            )

        return

    # Ticket.
    ticket = get_open_ticket(user_id)

    if not ticket:
        await message.answer(
            "❗ Сначала создай обращение.",
            reply_markup=user_main_menu()
        )
        return

    # Anti-spam.
    now = time.time()
    history = message_times.setdefault(user_id, [])

    history[:] = [
        t for t in history
        if now - t < ANTI_SPAM_SECONDS
    ]

    history.append(now)

    if len(history) > ANTI_SPAM_LIMIT:
        history.clear()

        set_mute(user_id, AUTO_MUTE_SECONDS)
        stat_inc("auto_mutes")

        await message.answer(
            "⚠️ <b>Слишком много сообщений подряд.</b>\n\n"
            f"🔇 Автоматический мут на "
            f"{format_duration(AUTO_MUTE_SECONDS)}.\n\n"
            "Пожалуйста, подожди."
        )

        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    "🚨 <b>АВТОМАТИЧЕСКИЙ АНТИСПАМ</b>\n\n"
                    f"👤 {message.from_user.full_name}\n"
                    f"🆔 ID: {user_id}\n"
                    f"🎫 Тикет: #{ticket['ticket_id']}\n"
                    f"🔇 Мут: {format_duration(AUTO_MUTE_SECONDS)}",
                    reply_markup=ticket_buttons(ticket["ticket_id"])
                )
            except Exception:
                pass

        return

    # Save incoming message.
    save_message(
        ticket["ticket_id"],
        user_id,
        "user",
        message.message_id,
        message_type(message)
    )

    username = (
        f"@{message.from_user.username}"
        if message.from_user.username
        else "нет username"
    )

    header = (
        "📨 <b>НОВОЕ СООБЩЕНИЕ В ТИКЕТЕ</b>\n\n"
        f"🎫 #{ticket['ticket_id']}\n"
        f"👤 {message.from_user.full_name}\n"
        f"🔗 {username}\n"
        f"🆔 ID: {user_id}\n"
        f"📂 {category_name(ticket['category'])}\n"
        f"🚦 {priority_name(ticket['priority'])}"
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                header
            )

            await bot.copy_message(
                chat_id=admin_id,
                from_chat_id=user_id,
                message_id=message.message_id
            )

            await bot.send_message(
                admin_id,
                "🎫 <b>Управление тикетом:</b>",
                reply_markup=ticket_buttons(ticket["ticket_id"])
            )

        except Exception:
            pass

    await message.answer(
        f"📨 Сообщение отправлено в поддержку.\n"
        f"🎫 Тикет #{ticket['ticket_id']}\n"
        "⏳ Ожидай ответа."
    )


# =========================================================
# HEALTH CHECK FOR RENDER
# =========================================================

async def health(request):
    return web.Response(text="Support Bot V3 is running!")


# =========================================================
# MAIN
# =========================================================

async def main():
    init_db()

    # Polling and webhook cannot be used at the same time.
    await bot.delete_webhook(drop_pending_updates=False)

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
    print("✅ SUPPORT BOT V3 ЗАПУЩЕН")
    print(f"🌐 Port: {port}")
    print(f"👨‍💼 Admins: {sorted(ADMIN_IDS)}")
    print(f"💾 SQLite: {DB_PATH}")
    print("🛡 Anti-Spam: ON")
    print("🔇 Mute: ON")
    print("🎫 Tickets: ON")
    print("👨‍💼 Operators: ON")
    print("📂 Categories: ON")
    print("🚦 Priorities: ON")
    print("⭐ Ratings: ON")
    print("📝 Notes: ON")
    print("📢 Broadcast: ON")
    print("================================")

    try:
        await dp.start_polling(bot)
    except Exception as e:
        print("❌ Polling остановлен.")
        print(f"Причина: {e}")
        print(
            "Проверь, что этот BOT_TOKEN не используется "
            "другим запущенным ботом/сервисом."
        )
        raise
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
